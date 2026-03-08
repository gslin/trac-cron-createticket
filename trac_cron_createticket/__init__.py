import re
from datetime import datetime, timedelta
from threading import Thread
from time import mktime, sleep, time

from croniter import croniter

from trac.admin import IAdminPanelProvider
from trac.config import BoolOption, IntOption, ListOption
from trac.core import Component, implements
from trac.db import DatabaseManager
from trac.perm import IPermissionPolicy, IPermissionRequestor
from trac.ticket import Ticket
from trac.ticket.model import Component as ComponentModel, Priority
from trac.util.html import html
from trac.web import IRequestHandler
from trac.web.chrome import INavigationContributor, ITemplateProvider


class CronCreateTicketPlugin(Component):
    implements(
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
            enabled = self.env.config.getbool(
                "trac_cron_createticket", f"{prefix}.enabled", False
            )
            if not enabled:
                continue

            frequency = self.env.config.get(
                "trac_cron_createticket", f"{prefix}.frequency", ""
            )
            cron_expr = self._get_cron_expression(frequency)
            if not cron_expr:
                continue

            title = self.env.config.get("trac_cron_createticket", f"{prefix}.title", "")
            owner = self.env.config.get("trac_cron_createticket", f"{prefix}.owner", "")
            description = self.env.config.get(
                "trac_cron_createticket", f"{prefix}.description", ""
            )
            component = self.env.config.get(
                "trac_cron_createticket", f"{prefix}.component", ""
            )
            priority = self.env.config.get(
                "trac_cron_createticket", f"{prefix}.priority", ""
            )
            offset = self.env.config.getint(
                "trac_cron_createticket", f"{prefix}.offset", 0
            )

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
                    "last_run": self.env.config.getint(
                        "trac_cron_createticket", f"{prefix}.last_run", 0
                    ),
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

        db = self.env.get_db_cnx()
        try:
            with db.cursor() as cursor:
                ticket = Ticket(self.env, db=db)
                values = {
                    "summary": title,
                    "reporter": "cron_create_ticket",
                    "owner": owner,
                    "description": description,
                }
                if component:
                    values["component"] = component
                if priority:
                    values["priority"] = priority

                ticket.insert(values=values)
                db.commit()

            self.env.log.info(f"Created ticket #{ticket.id}: {title}")
        except Exception as e:
            self.env.log.error(f"Failed to create ticket: {e}")

    def _run_scheduler(self):
        while not self._stop_ticker:
            self._load_jobs()
            current_time = time()

            for job in self._jobs:
                try:
                    cron = croniter(job["cron"], current_time)
                    next_run = cron.get_next()

                    if next_run - current_time <= self.ticker_interval:
                        if job["last_run"] == 0 or (current_time - job["last_run"]) >= (
                            60 * 60
                        ):
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
            html.a(
                "Cron Create Ticket", href=self.env.href.admin("trac_cron_createticket")
            ),
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
                if perm.has_permission("TRAC_ADMIN") or perm.has_permission(
                    "TICKET_VIEW"
                ):
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
                "enabled": self.env.config.getbool(
                    "trac_cron_createticket", f"{prefix}.enabled", False
                ),
                "frequency": self.env.config.get(
                    "trac_cron_createticket", f"{prefix}.frequency", ""
                ),
                "title": self.env.config.get(
                    "trac_cron_createticket", f"{prefix}.title", ""
                ),
                "owner": self.env.config.get(
                    "trac_cron_createticket", f"{prefix}.owner", ""
                ),
                "description": self.env.config.get(
                    "trac_cron_createticket", f"{prefix}.description", ""
                ),
                "component": self.env.config.get(
                    "trac_cron_createticket", f"{prefix}.component", ""
                ),
                "priority": self.env.config.get(
                    "trac_cron_createticket", f"{prefix}.priority", ""
                ),
                "offset": self.env.config.getint(
                    "trac_cron_createticket", f"{prefix}.offset", 0
                ),
            }
            jobs.append(job)

        data["jobs"] = jobs
        data["ticker_enabled"] = self.ticker_enabled
        data["ticker_interval"] = self.ticker_interval

        data["components"] = self._get_components()
        data["priorities"] = self._get_priorities()

        return "admin_cron_createticket.html", data

    def _get_components(self):
        db = self.env.get_db_cnx()
        with db.cursor() as cursor:
            cursor.execute("SELECT name FROM component ORDER BY name")
            return [row[0] for row in cursor.fetchall()]

    def _get_priorities(self):
        db = self.env.get_db_cnx()
        with db.cursor() as cursor:
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
            self.env.config.set(
                "trac_cron_createticket",
                f"{prefix}.frequency",
                req.args.get(f"frequency_{i}", ""),
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
            elif action == "test_create":
                self._test_create_ticket(req)
        return self._render_admin_page(req)

    def get_templates_dirs(self):
        return [
            __import__("trac_cron_createticket", fromlist=["templates"]).__path__[0]
            + "/templates"
        ]

    def get_htdocs_dirs(self):
        return []
