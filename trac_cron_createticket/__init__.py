import re
from datetime import datetime, timedelta
from threading import Thread
from time import sleep, time

from croniter import croniter

from trac.admin import IAdminPanelProvider
from trac.config import BoolOption, IntOption
from trac.core import Component, implements
from trac.db import DatabaseManager
from trac.env import IEnvironmentSetupParticipant
from trac.perm import IPermissionPolicy, IPermissionRequestor
from trac.ticket import Ticket
from trac.util.html import html
from trac.web import IRequestHandler
from trac.web.chrome import INavigationContributor, ITemplateProvider


class CronCreateTicketPlugin(Component):
    implements(
        IEnvironmentSetupParticipant,
        INavigationContributor,
        IRequestHandler,
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

    def _load_jobs(self):
        jobs = []
        for i in range(1, 100):
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
            offset = self.env.config.getint("trac_cron_createticket", f"{prefix}.offset", 0)

            jobs.append(
                {
                    "name": prefix,
                    "cron": cron_expr,
                    "title": title,
                    "owner": owner,
                    "description": description,
                    "component": component,
                    "priority": priority,
                    "offset": offset,
                    "last_run": self.env.config.getint("trac_cron_createticket", f"{prefix}.last_run", 0),
                }
            )

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

    def _create_ticket(self, job):
        offset = job.get("offset", 0)
        title = self._expand_template(job["title"], offset=offset)
        owner = self._expand_template(job["owner"], offset=offset)
        description = self._expand_template(job["description"], offset=offset)
        component = self._expand_template(job["component"], offset=offset)
        priority = self._expand_template(job["priority"], offset=offset)

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

            with self.env.db_transaction:
                ticket.insert()

            self.env.log.info(f"Created ticket #{ticket.id}: {title}")
        except Exception as e:
            self.env.log.error(f"Failed to create ticket: {e}")

    def _run_scheduler(self):
        self.env.log.info("CronCreateTicket scheduler started")
        while not self._stop_ticker:
            self._load_jobs()
            current_time = time()

            for job in self._jobs:
                try:
                    cron = croniter(job["cron"], current_time)
                    next_run = cron.get_prev()
                    if job["last_run"] == 0 or next_run > job["last_run"]:
                        self.env.log.info(f"Creating ticket for job {job['name']}: {job['title']}")
                        self._create_ticket(job)
                        job["last_run"] = int(current_time)

                        prefix = job["name"]
                        self.env.config.set(
                            "trac_cron_createticket",
                            f"{prefix}.last_run",
                            str(int(current_time)),
                        )
                        self.env.config.save()
                except Exception as e:
                    self.env.log.error(f"Error processing job {job['name']}: {e}")

            sleep(self.ticker_interval)
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

    def _get_db_schema(self):
        return {"version": 1, "tables": {}}

    def _upgrade_db(self):
        db_version = self.env.config.getint("trac_cron_createticket", "db_version", 0)
        if db_version < 1:
            self.env.config.set("trac_cron_createticket", "db_version", "1")
            self.env.config.save()

    def _init_db(self):
        self._upgrade_db()

    def environment_created(self):
        self._init_db()

    def environment_needs_upgrade(self, db):
        return False

    def upgrade_environment(self, db):
        self._init_db()

    def initialize(self):
        self._init_db()
        if self.ticker_enabled:
            self._start_ticker()

    def shutdown(self):
        self._stop_ticker_thread()

    def get_active_navigation_item(self, req):
        return "trac_cron_createticket"

    def get_navigation_items(self, req):
        yield (
            "mainnav",
            "trac_cron_createticket",
            html.a("Cron Create Ticket", href=self.env.href.admin("trac_cron_createticket")),
        )

    def get_permission_actions(self):
        return [
            "TRAC_CRON_CREATE_TICKET_ADMIN",
            ("TRAC_CRON_CREATE_TICKET_VIEW", ["TRAC_CRON_CREATE_TICKET_ADMIN"]),
        ]

    def check_permission(self, action, username, resource, perm):
        if action in ("TRAC_CRON_CREATE_TICKET_ADMIN", "TRAC_CRON_CREATE_TICKET_VIEW"):
            if action == "TRAC_CRON_CREATE_TICKET_ADMIN":
                if perm.has_permission("TRAC_ADMIN"):
                    return True
            if action == "TRAC_CRON_CREATE_TICKET_VIEW":
                if perm.has_permission("TRAC_ADMIN") or perm.has_permission("TICKET_VIEW"):
                    return True
        return None

    def match_request(self, req):
        return req.path_info.startswith("/admin/trac_cron_createticket")

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

        max_jobs = 10
        jobs = []
        for i in range(1, max_jobs + 1):
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
                "offset": self.env.config.getint("trac_cron_createticket", f"{prefix}.offset", 0),
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
        data["ticker_interval"] = self.ticker_interval

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

    def _save_jobs_from_form(self, req):
        max_jobs = 10
        for i in range(1, max_jobs + 1):
            prefix = f"job{i}"
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.enabled",
                str(bool(req.args.get(f"enabled_{i}"))).lower(),
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
                f"{prefix}.offset",
                req.args.get(f"offset_{i}", "0"),
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
        offset = int(req.args.get("new_offset", "0"))
        enabled = bool(req.args.get("new_enabled"))

        if not title:
            return

        max_jobs = 10
        for i in range(1, max_jobs + 1):
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
        self.env.config.remove("trac_cron_createticket", f"{prefix}.offset")
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
        if req.method == "POST":
            action = req.args.get("action")
            if action == "save_jobs":
                self._save_jobs_from_form(req)
                req.redirect(req.href.admin(cat, page))
            elif action == "create_job":
                self._create_job_from_form(req)
                req.redirect(req.href.admin(cat, page))
            elif action and action.startswith("delete_job_"):
                job_index = int(action.split("_")[-1])
                self._delete_job(job_index)
                req.redirect(req.href.admin(cat, page))
        return self._render_admin_page(req)

    def get_templates_dirs(self):
        from trac_cron_createticket import __file__ as module_path
        import os

        return [os.path.join(os.path.dirname(module_path), "templates")]

    def get_htdocs_dirs(self):
        return []
