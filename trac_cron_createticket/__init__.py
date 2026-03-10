import os
import re
from datetime import datetime, timedelta
from threading import Lock, Thread
from time import sleep, time

from croniter import croniter

from trac.admin import IAdminPanelProvider
from trac.config import BoolOption, IntOption
from trac.core import Component, implements
from trac.env import IEnvironmentSetupParticipant
from trac.perm import IPermissionPolicy, IPermissionRequestor
from trac.ticket import Ticket
from trac.util.html import html
from trac.web.chrome import INavigationContributor, ITemplateProvider

MAX_JOBS = 10
DB_VERSION = 2


class CronCreateTicketPlugin(Component):
    implements(
        IEnvironmentSetupParticipant,
        INavigationContributor,
        ITemplateProvider,
        IAdminPanelProvider,
        IPermissionRequestor,
        IPermissionPolicy,
    )

    ticker_enabled = BoolOption(
        "trac_cron_createticket",
        "ticker_enabled",
        "true",
        "Enable the ticker for scheduled ticket creation",
    )

    ticker_interval = IntOption(
        "trac_cron_createticket",
        "ticker_interval",
        60,
        "Interval in seconds between ticker wake-ups",
    )

    PRESETS = {
        "hourly": "0 * * * *",
        "daily": "0 0 * * *",
        "weekly": "0 0 * * 0",
        "monthly": "0 0 1 * *",
        "quarterly": "0 0 1 1,4,7,10 *",
        "yearly": "0 0 1 1 *",
    }

    def __init__(self):
        self._ticker_thread = None
        self._stop_ticker = False
        self._jobs = []
        self._lock = Lock()

    # -- DB schema & migration --

    def _create_job_state_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS cron_createticket_jobs ("
                "  job_name TEXT PRIMARY KEY,"
                "  last_run INTEGER NOT NULL DEFAULT 0"
                ")"
            )

    def _migrate_last_run_to_db(self):
        """Migrate last_run values from trac.ini config to the DB table."""
        for i in range(1, MAX_JOBS + 1):
            prefix = f"job{i}"
            config_key = f"{prefix}.last_run"
            value = self.env.config.get("trac_cron_createticket", config_key, "")
            if value:
                last_run = self._safe_int(value, default=0, minimum=0, field_name=config_key)
                if last_run > 0:
                    self._db_upsert_last_run(prefix, last_run)
                self.env.config.remove("trac_cron_createticket", config_key)
        self.env.config.save()

    def _upgrade_db(self):
        db_version = self.env.config.getint("trac_cron_createticket", "db_version", 0)

        if db_version < 1:
            self.env.config.set("trac_cron_createticket", "db_version", str(DB_VERSION))
            self._create_job_state_table()
            self.env.config.save()
            return

        if db_version < 2:
            self._create_job_state_table()
            self._migrate_last_run_to_db()
            self.env.config.set("trac_cron_createticket", "db_version", str(DB_VERSION))
            self.env.config.save()

    def _init_db(self):
        self._upgrade_db()

    def environment_created(self):
        self._init_db()

    def environment_needs_upgrade(self, db=None):
        db_version = self.env.config.getint("trac_cron_createticket", "db_version", 0)
        return db_version < DB_VERSION

    def upgrade_environment(self, db=None):
        self._init_db()

    # -- DB operations for job state --

    def _db_get_last_run(self, job_name):
        """Read last_run from the DB for a given job."""
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(
                "SELECT last_run FROM cron_createticket_jobs WHERE job_name=%s",
                (job_name,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def _db_upsert_last_run(self, job_name, last_run):
        """Insert or update last_run for a job (used during migration and init)."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                "SELECT 1 FROM cron_createticket_jobs WHERE job_name=%s",
                (job_name,),
            )
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE cron_createticket_jobs SET last_run=%s WHERE job_name=%s",
                    (int(last_run), job_name),
                )
            else:
                cursor.execute(
                    "INSERT INTO cron_createticket_jobs (job_name, last_run) VALUES (%s, %s)",
                    (job_name, int(last_run)),
                )

    def _db_try_claim_job(self, job_name, expected_last_run, new_last_run):
        """Atomically try to claim a job execution via compare-and-swap.

        Returns True if this process successfully claimed the job (i.e., the
        UPDATE matched a row), False if another process already claimed it.
        """
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                "UPDATE cron_createticket_jobs SET last_run=%s "
                "WHERE job_name=%s AND last_run=%s",
                (int(new_last_run), job_name, int(expected_last_run)),
            )
            return cursor.rowcount > 0

    def _db_init_job(self, job_name, last_run):
        """Initialize a job's last_run in DB if it doesn't exist yet.

        Returns True if the row was inserted (first time), False if it
        already existed (another process initialized it first).
        """
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                "SELECT 1 FROM cron_createticket_jobs WHERE job_name=%s",
                (job_name,),
            )
            if cursor.fetchone():
                return False
            cursor.execute(
                "INSERT INTO cron_createticket_jobs (job_name, last_run) VALUES (%s, %s)",
                (job_name, int(last_run)),
            )
            return True

    def _db_delete_job(self, job_name):
        """Remove a job's state from the DB."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                "DELETE FROM cron_createticket_jobs WHERE job_name=%s",
                (job_name,),
            )

    # -- Job loading --

    def _load_jobs(self):
        jobs = []
        for i in range(1, MAX_JOBS + 1):
            prefix = f"job{i}"
            enabled = self.env.config.getbool("trac_cron_createticket", f"{prefix}.enabled", False)
            if not enabled:
                continue

            frequency = self.env.config.get("trac_cron_createticket", f"{prefix}.frequency", "")
            cron_expr = self._get_cron_expression(frequency)
            if not cron_expr:
                continue

            title = self.env.config.get("trac_cron_createticket", f"{prefix}.title", "")
            owner = self.env.config.get("trac_cron_createticket", f"{prefix}.owner", "")
            description = self.env.config.get("trac_cron_createticket", f"{prefix}.description", "")
            component = self.env.config.get("trac_cron_createticket", f"{prefix}.component", "")
            priority = self.env.config.get("trac_cron_createticket", f"{prefix}.priority", "")
            status = self.env.config.get("trac_cron_createticket", f"{prefix}.status", "new")
            offset = self._get_config_int(f"{prefix}.offset", default=0, minimum=0)

            jobs.append(
                {
                    "name": prefix,
                    "cron": cron_expr,
                    "title": title,
                    "owner": owner,
                    "description": description,
                    "component": component,
                    "priority": priority,
                    "status": status,
                    "offset": offset,
                    "last_run": self._db_get_last_run(prefix),
                }
            )

        with self._lock:
            self._jobs = jobs

    def _get_cron_expression(self, frequency):
        if not frequency:
            return None

        if frequency in self.PRESETS:
            return self.PRESETS[frequency]

        try:
            croniter(frequency)
            return frequency
        except (KeyError, ValueError):
            pass

        return None

    # -- Template expansion --

    def _expand_template(self, template, base_time=None, offset=0):
        if base_time is None:
            base_time = datetime.now()

        if offset != 0:
            base_time = base_time + timedelta(seconds=offset)

        now = base_time

        placeholders = {
            "now": now,
            "now_unix": int(now.timestamp()),
            "today": now.strftime("%Y-%m-%d"),
            "tomorrow": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            "yesterday": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        }

        offset_pattern = re.compile(r"\[offset:(\d+)\]")

        def replace_offset(match):
            seconds = int(match.group(1))
            adjusted_time = base_time + timedelta(seconds=seconds)
            return adjusted_time.strftime("%Y-%m-%d")

        template = offset_pattern.sub(replace_offset, template)

        for key, value in placeholders.items():
            if isinstance(value, datetime):
                value = value.strftime("%Y-%m-%d %H:%M:%S")
            template = template.replace(f"[{key}]", str(value))

        return template

    # -- Ticket creation --

    def _create_ticket(self, job):
        offset = job.get("offset", 0)
        title = self._expand_template(job["title"], offset=offset)
        owner = self._expand_template(job["owner"], offset=offset)
        description = self._expand_template(job["description"], offset=offset)
        component = self._expand_template(job["component"], offset=offset)
        priority = self._expand_template(job["priority"], offset=offset)
        status = job.get("status", "new") or "new"

        try:
            ticket = Ticket(self.env)
            ticket["summary"] = title
            ticket["reporter"] = "cron_create_ticket"
            ticket["owner"] = owner
            ticket["description"] = description
            if component:
                ticket["component"] = component
            if priority:
                ticket["priority"] = priority
            ticket["status"] = status

            with self.env.db_transaction:
                ticket.insert()

            self.env.log.info(f"Created ticket #{ticket.id}: {title}")
            return True
        except Exception as e:
            self.env.log.error(f"Failed to create ticket: {e}")
            return False

    # -- Scheduler --

    def _run_scheduler(self):
        self.env.log.info("CronCreateTicket scheduler started")
        while not self._stop_ticker:
            self._load_jobs()
            current_time = time()

            with self._lock:
                jobs_snapshot = list(self._jobs)

            for job in jobs_snapshot:
                try:
                    if job["last_run"] == 0:
                        # First time seeing this job: initialize its last_run
                        # in DB. If another process already did this,
                        # _db_init_job returns False and we skip.
                        init_time = int(current_time)
                        self._db_init_job(job["name"], init_time)
                        continue

                    cron = croniter(job["cron"], current_time)
                    due_run = int(cron.get_prev())
                    if due_run > job["last_run"]:
                        # Try to atomically claim this job execution.
                        # Only one process will succeed.
                        if not self._db_try_claim_job(job["name"], job["last_run"], due_run):
                            self.env.log.debug(
                                f"Job {job['name']} already claimed by another process"
                            )
                            continue

                        self.env.log.info(f"Creating ticket for job {job['name']}: {job['title']}")
                        if self._create_ticket(job):
                            self.env.log.info(f"Ticket created for job {job['name']}")
                        else:
                            # Ticket creation failed; revert last_run so it
                            # can be retried on the next cycle.
                            self._db_try_claim_job(job["name"], due_run, job["last_run"])
                except Exception as e:
                    self.env.log.error(f"Error processing job {job['name']}: {e}")

            sleep(self._get_ticker_interval())
        self.env.log.info("CronCreateTicket scheduler stopped")

    def _start_ticker(self):
        if self._ticker_thread is None or not self._ticker_thread.is_alive():
            self._stop_ticker = False
            self._ticker_thread = Thread(target=self._run_scheduler, daemon=True)
            self._ticker_thread.start()

    def _stop_ticker_thread(self):
        self._stop_ticker = True
        if self._ticker_thread:
            self._ticker_thread.join(timeout=5)

    def _ensure_ticker_state(self):
        if self.ticker_enabled:
            self._start_ticker()
        else:
            self._stop_ticker_thread()

    # -- Lifecycle --

    def initialize(self):
        self._init_db()
        self._ensure_ticker_state()

    def shutdown(self):
        self._stop_ticker_thread()

    # -- Navigation --

    def get_active_navigation_item(self, req):
        return "trac_cron_createticket"

    def get_navigation_items(self, req):
        yield (
            "mainnav",
            "trac_cron_createticket",
            html.a("Cron Create Ticket", href=self.env.href.admin("trac_cron_createticket")),
        )

    # -- Permissions --

    def get_permission_actions(self):
        return [
            "TRAC_CRON_CREATE_TICKET_ADMIN",
            ("TRAC_CRON_CREATE_TICKET_VIEW", ["TRAC_CRON_CREATE_TICKET_ADMIN"]),
        ]

    def check_permission(self, action, username, resource, perm):
        if action == "TRAC_CRON_CREATE_TICKET_ADMIN":
            if "TRAC_ADMIN" in perm:
                return True
        elif action == "TRAC_CRON_CREATE_TICKET_VIEW":
            if "TRAC_ADMIN" in perm or "TICKET_VIEW" in perm:
                return True
        return None

    # -- Admin panel --

    def _render_admin_page(self, req):
        data = {}
        data["presets"] = [
            ("hourly", "Hourly"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
            ("monthly", "Monthly"),
            ("quarterly", "Quarterly"),
            ("yearly", "Yearly"),
        ]

        jobs = []
        for i in range(1, MAX_JOBS + 1):
            prefix = f"job{i}"
            job = {
                "index": i,
                "enabled": self.env.config.getbool("trac_cron_createticket", f"{prefix}.enabled", False),
                "frequency": self.env.config.get("trac_cron_createticket", f"{prefix}.frequency", ""),
                "title": self.env.config.get("trac_cron_createticket", f"{prefix}.title", ""),
                "owner": self.env.config.get("trac_cron_createticket", f"{prefix}.owner", ""),
                "description": self.env.config.get("trac_cron_createticket", f"{prefix}.description", ""),
                "component": self.env.config.get("trac_cron_createticket", f"{prefix}.component", ""),
                "priority": self.env.config.get("trac_cron_createticket", f"{prefix}.priority", ""),
                "status": self.env.config.get("trac_cron_createticket", f"{prefix}.status", "new"),
                "offset": self._get_config_int(f"{prefix}.offset", default=0, minimum=0),
            }
            has_data = any(
                [
                    job["enabled"],
                    job["frequency"],
                    job["title"],
                    job["owner"],
                    job["description"],
                    job["component"],
                    job["priority"],
                    job["offset"] != 0,
                ]
            )
            if has_data:
                jobs.append(job)

        data["jobs"] = jobs
        data["ticker_enabled"] = self.ticker_enabled
        data["ticker_interval"] = self._get_ticker_interval()

        data["components"] = self._get_components()
        data["priorities"] = self._get_priorities()

        return "admin_cron_createticket.html", data

    def _get_components(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT name FROM component ORDER BY name")
            return [row[0] for row in cursor.fetchall()]

    def _get_priorities(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT name FROM enum WHERE type='priority' ORDER BY value")
            return [row[0] for row in cursor.fetchall()]

    # -- Utility --

    def _safe_int(self, value, default=0, minimum=None, field_name="value"):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            self.env.log.warning(f"Invalid integer for {field_name}: {value!r}, using {default}.")
            return default

        if minimum is not None and parsed < minimum:
            self.env.log.warning(
                f"Integer for {field_name} below minimum {minimum}: {value!r}, using {default}."
            )
            return default
        return parsed

    def _get_config_int(self, option, default=0, minimum=None):
        value = self.env.config.get("trac_cron_createticket", option, str(default))
        return self._safe_int(value, default=default, minimum=minimum, field_name=option)

    def _get_request_int(self, req, field_name, default=0, minimum=None):
        value = req.args.get(field_name, str(default))
        return self._safe_int(value, default=default, minimum=minimum, field_name=field_name)

    def _get_ticker_interval(self):
        return self._get_config_int("ticker_interval", default=60, minimum=1)

    def _is_checked(self, req, field_name):
        value = req.args.get(field_name)
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "off", "no")
        return bool(value)

    # -- Form handlers --

    def _save_jobs_from_form(self, req):
        current_interval = self._get_ticker_interval()
        ticker_interval = self._get_request_int(req, "ticker_interval", default=current_interval, minimum=1)

        self.env.config.set(
            "trac_cron_createticket",
            "ticker_enabled",
            str(self._is_checked(req, "ticker_enabled")).lower(),
        )
        self.env.config.set(
            "trac_cron_createticket",
            "ticker_interval",
            str(ticker_interval),
        )

        for i in range(1, MAX_JOBS + 1):
            prefix = f"job{i}"
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.enabled",
                str(self._is_checked(req, f"enabled_{i}")).lower(),
            )
            frequency = req.args.get(f"frequency_{i}", "")
            if frequency == "custom":
                frequency = req.args.get(f"frequency_custom_{i}", "")
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.frequency",
                frequency,
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.title",
                req.args.get(f"title_{i}", ""),
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.owner",
                req.args.get(f"owner_{i}", ""),
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.description",
                req.args.get(f"description_{i}", ""),
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.component",
                req.args.get(f"component_{i}", ""),
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.priority",
                req.args.get(f"priority_{i}", ""),
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.status",
                req.args.get(f"status_{i}", "new") or "new",
            )
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.offset",
                str(self._get_request_int(req, f"offset_{i}", default=0, minimum=0)),
            )

        self.env.config.save()
        self._load_jobs()

    def _test_create_ticket(self, req):
        job = {
            "title": req.args.get("test_title", "Test Ticket"),
            "owner": req.args.get("test_owner", "admin"),
            "description": req.args.get("test_description", "Test description"),
            "component": req.args.get("test_component", ""),
            "priority": req.args.get("test_priority", ""),
            "offset": 0,
        }
        self._create_ticket(job)

    def _create_job_from_form(self, req):
        frequency = req.args.get("new_frequency")
        if frequency == "custom":
            frequency = req.args.get("new_frequency_custom", "")

        title = req.args.get("new_title", "")
        owner = req.args.get("new_owner", "")
        description = req.args.get("new_description", "")
        component = req.args.get("new_component", "")
        priority = req.args.get("new_priority", "")
        offset = self._get_request_int(req, "new_offset", default=0, minimum=0)
        enabled = self._is_checked(req, "new_enabled")

        if not title:
            return

        for i in range(1, MAX_JOBS + 1):
            prefix = f"job{i}"
            existing_title = self.env.config.get("trac_cron_createticket", f"{prefix}.title", "")
            if not existing_title:
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.enabled",
                    str(enabled).lower(),
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.frequency",
                    frequency,
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.title",
                    title,
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.owner",
                    owner,
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.description",
                    description,
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.component",
                    component,
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.priority",
                    priority,
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.status",
                    "new",
                )
                self.env.config.set(
                    "trac_cron_createticket",
                    f"{prefix}.offset",
                    str(offset),
                )
                self.env.config.save()
                self._load_jobs()
                return

    def _delete_job(self, job_index):
        prefix = f"job{job_index}"
        self.env.config.remove("trac_cron_createticket", f"{prefix}.enabled")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.frequency")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.title")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.owner")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.description")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.component")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.priority")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.status")
        self.env.config.remove("trac_cron_createticket", f"{prefix}.offset")
        self._db_delete_job(prefix)
        self.env.config.save()
        self._load_jobs()

    def get_admin_panels(self, req):
        yield (
            "trac_cron_createticket",
            "Cron Create Ticket",
            "cron_createticket",
            "Cron Create Ticket",
        )

    def render_admin_panel(self, req, cat, page, path_info):
        self._ensure_ticker_state()
        if req.method == "POST":
            action = req.args.get("action")
            if action == "save_jobs":
                self._save_jobs_from_form(req)
                req.redirect(req.href.admin(cat, page))
            elif action == "create_job":
                self._create_job_from_form(req)
                req.redirect(req.href.admin(cat, page))
            elif action and action.startswith("delete_job_"):
                job_index = self._safe_int(
                    action.rsplit("_", 1)[-1],
                    default=0,
                    minimum=1,
                    field_name="delete_job_index",
                )
                if 1 <= job_index <= MAX_JOBS:
                    self._delete_job(job_index)
                else:
                    self.env.log.warning(f"Ignoring invalid delete action: {action!r}")
                req.redirect(req.href.admin(cat, page))
        return self._render_admin_page(req)

    def get_templates_dirs(self):
        return [os.path.join(os.path.dirname(__file__), "templates")]

    def get_htdocs_dirs(self):
        return []
