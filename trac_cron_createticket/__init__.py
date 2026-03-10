import os
import re
from datetime import datetime, timedelta, timezone
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
DB_VERSION = 4


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
        'trac_cron_createticket',
        'ticker_enabled',
        'true',
        'Enable the ticker for scheduled ticket creation',
    )

    ticker_interval = IntOption(
        'trac_cron_createticket',
        'ticker_interval',
        60,
        'Interval in seconds between ticker wake-ups',
    )

    PRESETS = {
        'hourly': '0 * * * *',
        'daily': '0 0 * * *',
        'weekly': '0 0 * * 0',
        'monthly': '0 0 1 * *',
        'quarterly': '0 0 1 1,4,7,10 *',
        'yearly': '0 0 1 1 *',
    }

    def __init__(self):
        self._ticker_thread = None
        self._stop_ticker = False
        self._jobs = []
        self._lock = Lock()

    # -- DB schema & migration --

    def _create_job_table_v4(self):
        """Create the full job table (v4 schema)."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS cron_createticket_jobs ("
                "  job_name VARCHAR(64) NOT NULL PRIMARY KEY,"
                "  last_run INTEGER NOT NULL DEFAULT 0,"
                "  enabled INTEGER NOT NULL DEFAULT 0,"
                "  frequency VARCHAR(255) NOT NULL DEFAULT '',"
                "  title VARCHAR(255) NOT NULL DEFAULT '',"
                "  owner VARCHAR(255) NOT NULL DEFAULT '',"
                "  description TEXT NOT NULL,"
                "  component VARCHAR(255) NOT NULL DEFAULT '',"
                "  priority VARCHAR(255) NOT NULL DEFAULT '',"
                "  status VARCHAR(64) NOT NULL DEFAULT 'new'"
                ")"
            )

    def _migrate_v3_to_v4(self):
        """Add job config columns and migrate data from trac.ini to DB."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            for col, col_type in [
                ('frequency', "VARCHAR(255) NOT NULL DEFAULT ''"),
                ('title', "VARCHAR(255) NOT NULL DEFAULT ''"),
                ('owner', "VARCHAR(255) NOT NULL DEFAULT ''"),
                ('description', 'TEXT NOT NULL'),
                ('component', "VARCHAR(255) NOT NULL DEFAULT ''"),
                ('priority', "VARCHAR(255) NOT NULL DEFAULT ''"),
                ('status', "VARCHAR(64) NOT NULL DEFAULT 'new'"),
            ]:
                try:
                    cursor.execute(
                        f'ALTER TABLE cron_createticket_jobs ADD COLUMN {col} {col_type}'
                    )
                except Exception:
                    pass  # Column may already exist

        # Migrate job config from trac.ini to DB
        section = 'trac_cron_createticket'
        for i in range(1, MAX_JOBS + 1):
            prefix = f'job{i}'
            title = self.env.config.get(section, f'{prefix}.title', '')
            if not title:
                continue

            frequency = self.env.config.get(section, f'{prefix}.frequency', '')
            owner = self.env.config.get(section, f'{prefix}.owner', '')
            description = self.env.config.get(section, f'{prefix}.description', '')
            component = self.env.config.get(section, f'{prefix}.component', '')
            priority = self.env.config.get(section, f'{prefix}.priority', '')
            status = self.env.config.get(section, f'{prefix}.status', 'new')

            self._db_save_job(prefix, {
                'frequency': frequency,
                'title': title,
                'owner': owner,
                'description': description,
                'component': component,
                'priority': priority,
                'status': status,
            })

            # Remove migrated keys from trac.ini
            for key in ('enabled', 'frequency', 'title', 'owner',
                        'description', 'component', 'priority', 'status'):
                self.env.config.remove(section, f'{prefix}.{key}')

        self.env.config.save()

    def _create_job_state_table(self):
        """Create the v2 table (used during sequential upgrade)."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                'CREATE TABLE IF NOT EXISTS cron_createticket_jobs ('
                '  job_name VARCHAR(64) NOT NULL PRIMARY KEY,'
                '  last_run INTEGER NOT NULL DEFAULT 0'
                ')'
            )

    def _migrate_last_run_to_db(self):
        """Migrate last_run values from trac.ini config to the DB table."""
        for i in range(1, MAX_JOBS + 1):
            prefix = f'job{i}'
            config_key = f'{prefix}.last_run'
            value = self.env.config.get('trac_cron_createticket', config_key, '')
            if value:
                last_run = self._safe_int(value, default=0, minimum=0, field_name=config_key)
                if last_run > 0:
                    self._db_upsert_last_run(prefix, last_run)
                self.env.config.remove('trac_cron_createticket', config_key)
        self.env.config.save()

    def _add_enabled_column(self):
        """Add the enabled column to the existing table and migrate values."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                'ALTER TABLE cron_createticket_jobs '
                'ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1'
            )

        for i in range(1, MAX_JOBS + 1):
            prefix = f'job{i}'
            enabled = self.env.config.getbool(
                'trac_cron_createticket', f'{prefix}.enabled', False
            )
            self._db_set_enabled(prefix, enabled)

    def _upgrade_db(self):
        db_version = self.env.config.getint('trac_cron_createticket', 'db_version', 0)

        if db_version < 1:
            # Fresh install: create v4 table directly
            self._create_job_table_v4()
            self.env.config.set('trac_cron_createticket', 'db_version', str(DB_VERSION))
            self.env.config.save()
            return

        if db_version < 2:
            self._create_job_state_table()
            self._migrate_last_run_to_db()
            db_version = 2

        if db_version < 3:
            self._add_enabled_column()
            db_version = 3

        if db_version < 4:
            self._migrate_v3_to_v4()

        self.env.config.set('trac_cron_createticket', 'db_version', str(DB_VERSION))
        self.env.config.save()

    def _init_db(self):
        self._upgrade_db()

    def environment_created(self):
        self._init_db()

    def environment_needs_upgrade(self, db=None):
        db_version = self.env.config.getint('trac_cron_createticket', 'db_version', 0)
        return db_version < DB_VERSION

    def upgrade_environment(self, db=None):
        self._init_db()

    # -- DB operations --

    def _db_get_job(self, job_name):
        """Read a full job record from the DB."""
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT job_name, last_run, enabled, frequency, title, owner, '
                'description, component, priority, status '
                'FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                'name': row[0],
                'last_run': row[1],
                'enabled': bool(row[2]),
                'frequency': row[3],
                'title': row[4],
                'owner': row[5],
                'description': row[6],
                'component': row[7],
                'priority': row[8],
                'status': row[9],
            }

    def _db_get_all_jobs(self):
        """Read all job records from the DB that have a title."""
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT job_name, last_run, enabled, frequency, title, owner, '
                'description, component, priority, status '
                'FROM cron_createticket_jobs ORDER BY job_name'
            )
            jobs = []
            for row in cursor.fetchall():
                jobs.append({
                    'name': row[0],
                    'last_run': row[1],
                    'enabled': bool(row[2]),
                    'frequency': row[3],
                    'title': row[4],
                    'owner': row[5],
                    'description': row[6],
                    'component': row[7],
                    'priority': row[8],
                    'status': row[9],
                })
            return jobs

    def _db_save_job(self, job_name, data):
        """Insert or update a full job record in the DB."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT 1 FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )
            if cursor.fetchone():
                cursor.execute(
                    'UPDATE cron_createticket_jobs SET '
                    'frequency=%s, title=%s, owner=%s, description=%s, '
                    'component=%s, priority=%s, status=%s '
                    'WHERE job_name=%s',
                    (
                        data.get('frequency', ''),
                        data.get('title', ''),
                        data.get('owner', ''),
                        data.get('description', ''),
                        data.get('component', ''),
                        data.get('priority', ''),
                        data.get('status', 'new'),
                        job_name,
                    ),
                )
            else:
                cursor.execute(
                    'INSERT INTO cron_createticket_jobs '
                    '(job_name, last_run, enabled, frequency, title, owner, '
                    'description, component, priority, status) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (
                        job_name,
                        0,
                        0,
                        data.get('frequency', ''),
                        data.get('title', ''),
                        data.get('owner', ''),
                        data.get('description', ''),
                        data.get('component', ''),
                        data.get('priority', ''),
                        data.get('status', 'new'),
                    ),
                )

    def _db_get_last_run(self, job_name):
        """Read last_run from the DB for a given job."""
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT last_run FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    def _db_upsert_last_run(self, job_name, last_run):
        """Insert or update last_run for a job (used during migration and init)."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT 1 FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )
            if cursor.fetchone():
                cursor.execute(
                    'UPDATE cron_createticket_jobs SET last_run=%s WHERE job_name=%s',
                    (int(last_run), job_name),
                )
            else:
                cursor.execute(
                    "INSERT INTO cron_createticket_jobs "
                    "(job_name, last_run, enabled, frequency, title, owner, "
                    "description, component, priority, status) "
                    "VALUES (%s, %s, 0, '', '', '', '', '', '', 'new')",
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
                'UPDATE cron_createticket_jobs SET last_run=%s '
                'WHERE job_name=%s AND last_run=%s',
                (int(new_last_run), job_name, int(expected_last_run)),
            )
            return cursor.rowcount > 0

    def _db_init_job(self, job_name, last_run):
        """Set last_run for a job that has last_run=0 (first run).

        Uses CAS to avoid race conditions with other processes.
        Returns True if successfully updated, False otherwise.
        """
        return self._db_try_claim_job(job_name, 0, last_run)

    def _db_delete_job(self, job_name):
        """Remove a job's record from the DB."""
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                'DELETE FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )

    def _db_get_enabled(self, job_name):
        """Read the enabled flag from the DB for a given job."""
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT enabled FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )
            row = cursor.fetchone()
            return bool(row[0]) if row else False

    def _db_set_enabled(self, job_name, enabled):
        """Set the enabled flag in DB, creating the row if needed."""
        enabled_int = 1 if enabled else 0
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute(
                'SELECT 1 FROM cron_createticket_jobs WHERE job_name=%s',
                (job_name,),
            )
            if cursor.fetchone():
                cursor.execute(
                    'UPDATE cron_createticket_jobs SET enabled=%s WHERE job_name=%s',
                    (enabled_int, job_name),
                )
            else:
                cursor.execute(
                    "INSERT INTO cron_createticket_jobs "
                    "(job_name, last_run, enabled, frequency, title, owner, "
                    "description, component, priority, status) "
                    "VALUES (%s, 0, %s, '', '', '', '', '', '', 'new')",
                    (job_name, enabled_int),
                )

    # -- Job loading --

    def _load_jobs(self):
        all_jobs = self._db_get_all_jobs()
        jobs = []
        for job in all_jobs:
            if not job['enabled']:
                continue

            cron_expr = self._get_cron_expression(job['frequency'])
            if not cron_expr:
                continue

            jobs.append({
                'name': job['name'],
                'cron': cron_expr,
                'title': job['title'],
                'owner': job['owner'],
                'description': job['description'],
                'component': job['component'],
                'priority': job['priority'],
                'status': job['status'],
                'last_run': job['last_run'],
            })

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

    def _expand_template(self, template, base_time=None):
        if base_time is None:
            base_time = datetime.now(timezone.utc)

        now = base_time

        placeholders = {
            'now': now,
            'now_unix': int(now.timestamp()),
            'today': now.strftime('%Y-%m-%d'),
            'tomorrow': (now + timedelta(days=1)).strftime('%Y-%m-%d'),
            'yesterday': (now - timedelta(days=1)).strftime('%Y-%m-%d'),
        }

        offset_pattern = re.compile(r'\[offset:(\d+)\]')

        def replace_offset(match):
            seconds = int(match.group(1))
            adjusted_time = base_time + timedelta(seconds=seconds)
            return adjusted_time.strftime('%Y-%m-%d')

        template = offset_pattern.sub(replace_offset, template)

        for key, value in placeholders.items():
            if isinstance(value, datetime):
                value = value.strftime('%Y-%m-%d %H:%M:%S')
            template = template.replace(f'[{key}]', str(value))

        return template

    # -- Ticket creation --

    def _create_ticket(self, job):
        title = self._expand_template(job['title'])
        owner = job.get('owner', '')
        description = self._expand_template(job['description'])
        component = job.get('component', '')
        priority = self._expand_template(job['priority'])
        status = job.get('status', 'new') or 'new'

        try:
            ticket = Ticket(self.env)
            ticket['summary'] = title
            ticket['reporter'] = 'cron_create_ticket'
            ticket['owner'] = owner
            ticket['description'] = description
            if component:
                ticket['component'] = component
            if priority:
                ticket['priority'] = priority
            ticket['status'] = status

            with self.env.db_transaction:
                ticket.insert()

            self.env.log.info(f'Created ticket #{ticket.id}: {title}')
            return True
        except Exception as e:
            self.env.log.error(f'Failed to create ticket: {e}')
            return False

    # -- Scheduler --

    def _run_scheduler(self):
        self.env.log.info('CronCreateTicket scheduler started')
        while not self._stop_ticker:
            self._load_jobs()
            current_time = time()

            with self._lock:
                jobs_snapshot = list(self._jobs)

            for job in jobs_snapshot:
                try:
                    if job['last_run'] == 0:
                        # First time seeing this job: initialize its last_run
                        # in DB. If another process already did this,
                        # _db_init_job returns False and we skip.
                        init_time = int(current_time)
                        self._db_init_job(job['name'], init_time)
                        continue

                    cron = croniter(job['cron'], current_time)
                    due_run = int(cron.get_prev())
                    if due_run > job['last_run']:
                        # Try to atomically claim this job execution.
                        # Only one process will succeed.
                        if not self._db_try_claim_job(job['name'], job['last_run'], due_run):
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
                            self._db_try_claim_job(job['name'], due_run, job['last_run'])
                except Exception as e:
                    self.env.log.error(f"Error processing job {job['name']}: {e}")

            sleep(self._get_ticker_interval())
        self.env.log.info('CronCreateTicket scheduler stopped')

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
        return 'trac_cron_createticket'

    def get_navigation_items(self, req):
        yield (
            'mainnav',
            'trac_cron_createticket',
            html.a('Cron Create Ticket', href=self.env.href.admin('trac_cron_createticket')),
        )

    # -- Permissions --

    def get_permission_actions(self):
        return [
            'TRAC_CRON_CREATE_TICKET_ADMIN',
            ('TRAC_CRON_CREATE_TICKET_VIEW', ['TRAC_CRON_CREATE_TICKET_ADMIN']),
        ]

    def check_permission(self, action, username, resource, perm):
        if action == 'TRAC_CRON_CREATE_TICKET_ADMIN':
            if 'TRAC_ADMIN' in perm:
                return True
        elif action == 'TRAC_CRON_CREATE_TICKET_VIEW':
            if 'TRAC_ADMIN' in perm or 'TICKET_VIEW' in perm:
                return True
        return None

    # -- Admin panel --

    def _render_admin_page(self, req):
        data = {}
        data['presets'] = [
            ('hourly', 'Hourly'),
            ('daily', 'Daily'),
            ('weekly', 'Weekly'),
            ('monthly', 'Monthly'),
            ('quarterly', 'Quarterly'),
            ('yearly', 'Yearly'),
        ]

        all_jobs = self._db_get_all_jobs()
        jobs = []
        for job in all_jobs:
            has_data = any([
                job['enabled'],
                job['frequency'],
                job['title'],
                job['owner'],
                job['description'],
                job['component'],
                job['priority'],
            ])
            if has_data:
                # Extract index number from job_name (e.g. "job1" -> 1)
                index = int(job['name'].replace('job', ''))
                jobs.append({
                    'index': index,
                    'enabled': job['enabled'],
                    'frequency': job['frequency'],
                    'title': job['title'],
                    'owner': job['owner'],
                    'description': job['description'],
                    'component': job['component'],
                    'priority': job['priority'],
                })

        data['jobs'] = jobs
        data['ticker_enabled'] = self.ticker_enabled
        data['ticker_interval'] = self._get_ticker_interval()

        data['components'] = self._get_components()
        data['priorities'] = self._get_priorities()

        return 'admin_cron_createticket.html', data

    def _get_components(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT name FROM component ORDER BY name')
            return [row[0] for row in cursor.fetchall()]

    def _get_priorities(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT name FROM enum WHERE type='priority' ORDER BY value")
            return [row[0] for row in cursor.fetchall()]

    # -- Utility --

    def _safe_int(self, value, default=0, minimum=None, field_name='value'):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            self.env.log.warning(f'Invalid integer for {field_name}: {value!r}, using {default}.')
            return default

        if minimum is not None and parsed < minimum:
            self.env.log.warning(
                f'Integer for {field_name} below minimum {minimum}: {value!r}, using {default}.'
            )
            return default
        return parsed

    def _get_config_int(self, option, default=0, minimum=None):
        value = self.env.config.get('trac_cron_createticket', option, str(default))
        return self._safe_int(value, default=default, minimum=minimum, field_name=option)

    def _get_request_int(self, req, field_name, default=0, minimum=None):
        value = req.args.get(field_name, str(default))
        return self._safe_int(value, default=default, minimum=minimum, field_name=field_name)

    def _get_ticker_interval(self):
        return self._get_config_int('ticker_interval', default=60, minimum=1)

    def _is_checked(self, req, field_name):
        value = req.args.get(field_name)
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() not in ('', '0', 'false', 'off', 'no')
        return bool(value)

    # -- Form handlers --

    def _save_jobs_from_form(self, req):
        current_interval = self._get_ticker_interval()
        ticker_interval = self._get_request_int(req, 'ticker_interval', default=current_interval, minimum=1)

        self.env.config.set(
            'trac_cron_createticket',
            'ticker_enabled',
            str(self._is_checked(req, 'ticker_enabled')).lower(),
        )
        self.env.config.set(
            'trac_cron_createticket',
            'ticker_interval',
            str(ticker_interval),
        )
        self.env.config.save()

        for i in range(1, MAX_JOBS + 1):
            prefix = f'job{i}'
            enabled = self._is_checked(req, f'enabled_{i}')
            self._db_set_enabled(prefix, enabled)

            frequency = req.args.get(f'frequency_{i}', '')
            if frequency == 'custom':
                frequency = req.args.get(f'frequency_custom_{i}', '')

            self._db_save_job(prefix, {
                'frequency': frequency,
                'title': req.args.get(f'title_{i}', ''),
                'owner': req.args.get(f'owner_{i}', ''),
                'description': req.args.get(f'description_{i}', ''),
                'component': req.args.get(f'component_{i}', ''),
                'priority': req.args.get(f'priority_{i}', ''),
                'status': req.args.get(f'status_{i}', 'new') or 'new',
            })

        self._load_jobs()

    def _create_job_from_form(self, req):
        frequency = req.args.get('new_frequency')
        if frequency == 'custom':
            frequency = req.args.get('new_frequency_custom', '')

        title = req.args.get('new_title', '')
        owner = req.args.get('new_owner', '')
        description = req.args.get('new_description', '')
        component = req.args.get('new_component', '')
        priority = req.args.get('new_priority', '')
        enabled = self._is_checked(req, 'new_enabled')

        if not title:
            return

        # Find the first available slot
        all_jobs = self._db_get_all_jobs()
        used_names = {j['name'] for j in all_jobs if j['title']}

        for i in range(1, MAX_JOBS + 1):
            prefix = f'job{i}'
            if prefix not in used_names:
                self._db_save_job(prefix, {
                    'frequency': frequency,
                    'title': title,
                    'owner': owner,
                    'description': description,
                    'component': component,
                    'priority': priority,
                    'status': 'new',
                })
                self._db_set_enabled(prefix, enabled)
                self._load_jobs()
                return

    def _delete_job(self, job_index):
        prefix = f'job{job_index}'
        self._db_delete_job(prefix)
        self._load_jobs()

    def get_admin_panels(self, req):
        yield (
            'trac_cron_createticket',
            'Cron Create Ticket',
            'cron_createticket',
            'Cron Create Ticket',
        )

    def render_admin_panel(self, req, cat, page, path_info):
        self._ensure_ticker_state()
        if req.method == 'POST':
            action = req.args.get('action')
            if action == 'save_jobs':
                self._save_jobs_from_form(req)
                req.redirect(req.href.admin(cat, page))
            elif action == 'create_job':
                self._create_job_from_form(req)
                req.redirect(req.href.admin(cat, page))
            elif action and action.startswith('delete_job_'):
                job_index = self._safe_int(
                    action.rsplit('_', 1)[-1],
                    default=0,
                    minimum=1,
                    field_name='delete_job_index',
                )
                if 1 <= job_index <= MAX_JOBS:
                    self._delete_job(job_index)
                else:
                    self.env.log.warning(f'Ignoring invalid delete action: {action!r}')
                req.redirect(req.href.admin(cat, page))
        return self._render_admin_page(req)

    def get_templates_dirs(self):
        return [os.path.join(os.path.dirname(__file__), 'templates')]

    def get_htdocs_dirs(self):
        return []
