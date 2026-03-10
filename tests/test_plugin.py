import pytest
from datetime import datetime, timedelta
from threading import Lock
from unittest.mock import Mock, MagicMock, patch

from trac.env import Environment
from trac_cron_createticket import CronCreateTicketPlugin


def _make_mock_db(query_rows=None):
    """Create a mock DB connection with cursor support."""
    mock_db = MagicMock()
    mock_cursor = Mock()
    mock_cursor.fetchone = Mock(return_value=query_rows[0] if query_rows else None)
    mock_cursor.fetchall = Mock(return_value=query_rows if query_rows else [])
    mock_cursor.rowcount = 0
    mock_db.cursor = Mock(return_value=mock_cursor)
    return mock_db, mock_cursor


@pytest.fixture
def mock_env():
    env = MagicMock(spec=Environment)
    env.config = Mock()
    env.config.getbool = Mock(return_value=False)
    env.config.get = Mock(return_value="")
    env.config.getint = Mock(return_value=0)
    env.config.set = Mock()
    env.config.save = Mock()
    env.config.remove = Mock()
    env.log = Mock()
    env.href = Mock()
    env.href.admin = Mock(return_value="/admin/endpoint")
    env.db_transaction = MagicMock()
    env.db_transaction.__enter__ = Mock()
    env.db_transaction.__exit__ = Mock()
    env.db_query = MagicMock()
    env.db_query.__enter__ = Mock()
    env.db_query.__exit__ = Mock()
    return env


@pytest.fixture
def plugin(mock_env):
    plugin = CronCreateTicketPlugin.__new__(CronCreateTicketPlugin)
    plugin.env = mock_env
    plugin._jobs = []
    plugin._stop_ticker = False
    plugin._ticker_thread = None
    plugin._lock = Lock()
    return plugin


class TestCronExpression:
    def test_preset_frequency_daily(self, plugin):
        result = plugin._get_cron_expression("daily")
        assert result == "0 0 * * *"

    def test_preset_frequency_weekly(self, plugin):
        result = plugin._get_cron_expression("weekly")
        assert result == "0 0 * * 0"

    def test_custom_cron_expression(self, plugin):
        result = plugin._get_cron_expression("5 * * * *")
        assert result == "5 * * * *"

    def test_invalid_frequency(self, plugin):
        result = plugin._get_cron_expression("invalid")
        assert result is None

    def test_empty_frequency(self, plugin):
        result = plugin._get_cron_expression("")
        assert result is None


class TestTemplateExpansion:
    def test_expand_now_placeholder(self, plugin):
        template = "Ticket created at [now]"
        result = plugin._expand_template(template)
        assert "Ticket created at" in result
        assert result != template  # Should be expanded

    def test_expand_now_unix_placeholder(self, plugin):
        template = "Timestamp: [now_unix]"
        result = plugin._expand_template(template)
        assert "Timestamp:" in result

    def test_expand_today_placeholder(self, plugin):
        template = "Due: [today]"
        result = plugin._expand_template(template)
        expected = (datetime.now()).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_tomorrow_placeholder(self, plugin):
        template = "Due: [tomorrow]"
        result = plugin._expand_template(template)
        expected = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_yesterday_placeholder(self, plugin):
        template = "Started: [yesterday]"
        result = plugin._expand_template(template)
        expected = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_offset_placeholder(self, plugin):
        template = "Schedule: [offset:86400]"
        result = plugin._expand_template(template)
        expected = (datetime.now() + timedelta(seconds=86400)).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_template_with_offset(self, plugin):
        template = "Date: [today]"
        base_time = datetime(2024, 1, 1, 12, 0, 0)
        result = plugin._expand_template(template, base_time=base_time, offset=86400)
        assert "2024-01-02" in result


class TestDatabaseOperations:
    def test_create_ticket_basic(self, plugin, mock_env):
        mock_ticket = Mock()
        mock_ticket.id = 123
        mock_ticket.insert = Mock()
        mock_ticket.__setitem__ = Mock()

        with patch("trac_cron_createticket.Ticket", return_value=mock_ticket):
            job = {
                "title": "Test Ticket",
                "owner": "test_user",
                "description": "Test description",
                "component": "",
                "priority": "",
                "offset": 0,
            }
            assert plugin._create_ticket(job) is True
            mock_ticket.insert.assert_called_once()
            mock_env.log.info.assert_called_once()

    @patch("trac_cron_createticket.Ticket")
    def test_create_ticket_with_component_priority(self, mock_ticket_class, plugin, mock_env):
        mock_ticket = Mock()
        mock_ticket.id = 456
        mock_ticket.insert = Mock()
        mock_ticket.__setitem__ = Mock()
        mock_ticket_class.return_value = mock_ticket

        job = {
            "title": "Test Ticket [today]",
            "owner": "test_user",
            "description": "Test description",
            "component": "Testing",
            "priority": "High",
            "offset": 0,
        }
        assert plugin._create_ticket(job) is True
        mock_ticket.insert.assert_called_once()
        mock_env.log.info.assert_called_once()

    @patch("trac_cron_createticket.Ticket", side_effect=Exception("insert failed"))
    def test_create_ticket_failure_returns_false(self, _mock_ticket_class, plugin, mock_env):
        job = {
            "title": "Test Ticket [today]",
            "owner": "test_user",
            "description": "Test description",
            "component": "Testing",
            "priority": "High",
            "offset": 0,
        }
        assert plugin._create_ticket(job) is False
        mock_env.log.error.assert_called_once()


class TestJobLoading:
    def test_load_jobs_empty_config(self, plugin, mock_env):
        plugin._db_get_enabled = Mock(return_value=False)
        plugin._db_get_last_run = Mock(return_value=0)
        plugin._load_jobs()
        assert plugin._jobs == []

    def test_load_jobs_with_enabled_job(self, plugin, mock_env):

        def mock_get_enabled(job_name):
            return job_name == "job1"

        def mock_get(section, option, default=""):
            mapping = {
                "job1.frequency": "daily",
                "job1.title": "Daily Report",
                "job1.owner": "admin",
                "job1.description": "Automated report",
                "job1.component": "Reports",
                "job1.priority": "Normal",
            }
            return mapping.get(option, default)

        plugin._db_get_enabled = Mock(side_effect=mock_get_enabled)
        mock_env.config.get = Mock(side_effect=mock_get)
        plugin._db_get_last_run = Mock(return_value=1000000)

        plugin._load_jobs()
        assert len(plugin._jobs) == 1
        assert plugin._jobs[0]["name"] == "job1"
        assert plugin._jobs[0]["title"] == "Daily Report"
        assert plugin._jobs[0]["owner"] == "admin"
        assert plugin._jobs[0]["last_run"] == 1000000

    def test_load_jobs_with_invalid_frequency(self, plugin, mock_env):

        def mock_get_enabled(job_name):
            return job_name == "job1"

        def mock_get(section, option, default=""):
            if option == "job1.frequency":
                return "invalid"
            return default

        plugin._db_get_enabled = Mock(side_effect=mock_get_enabled)
        mock_env.config.get = Mock(side_effect=mock_get)

        plugin._load_jobs()
        assert len(plugin._jobs) == 0

    def test_load_jobs_with_invalid_offset_uses_default(self, plugin, mock_env):
        def mock_get_enabled(job_name):
            return job_name == "job1"

        def mock_get(section, option, default=""):
            mapping = {
                "job1.frequency": "daily",
                "job1.title": "Daily Report",
                "job1.owner": "admin",
                "job1.description": "Automated report",
                "job1.component": "Reports",
                "job1.priority": "Normal",
                "job1.offset": "invalid-offset",
            }
            return mapping.get(option, default)

        plugin._db_get_enabled = Mock(side_effect=mock_get_enabled)
        mock_env.config.get = Mock(side_effect=mock_get)
        plugin._db_get_last_run = Mock(return_value=0)

        plugin._load_jobs()
        assert len(plugin._jobs) == 1
        assert plugin._jobs[0]["offset"] == 0
        assert plugin._jobs[0]["last_run"] == 0


class TestComponentsAndPriorities:
    def test_get_components(self, plugin, mock_env):
        mock_db = MagicMock()
        mock_cursor = Mock()
        mock_cursor.fetchall = Mock(return_value=[("Component1",), ("Component2",)])

        def mock_cursor_func():
            return mock_cursor

        mock_db.cursor = Mock(side_effect=mock_cursor_func)
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        result = plugin._get_components()
        assert result == ["Component1", "Component2"]

    def test_get_priorities(self, plugin, mock_env):
        mock_db = MagicMock()
        mock_cursor = Mock()
        mock_cursor.fetchall = Mock(return_value=[("Low",), ("Normal",), ("High",)])

        def mock_cursor_func():
            return mock_cursor

        mock_db.cursor = Mock(side_effect=mock_cursor_func)
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        result = plugin._get_priorities()
        assert result == ["Low", "Normal", "High"]


class TestFormHandling:
    def test_save_jobs_from_form_persists_scheduler_and_false_checkbox(self, plugin, mock_env):
        req = Mock()
        req.args = {
            "ticker_interval": "30",
            "enabled_1": "false",
            "frequency_1": "daily",
            "title_1": "Daily Report",
            "owner_1": "admin",
            "description_1": "Auto ticket",
            "component_1": "Reports",
            "priority_1": "Normal",
            "offset_1": "0",
        }
        plugin._db_get_enabled = Mock(return_value=False)
        plugin._db_get_last_run = Mock(return_value=0)
        plugin._db_set_enabled = Mock()

        plugin._save_jobs_from_form(req)

        mock_env.config.set.assert_any_call("trac_cron_createticket", "ticker_enabled", "false")
        mock_env.config.set.assert_any_call("trac_cron_createticket", "ticker_interval", "30")
        mock_env.config.set.assert_any_call("trac_cron_createticket", "job1.enabled", "false")
        plugin._db_set_enabled.assert_any_call("job1", False)
        mock_env.config.save.assert_called_once()

    def test_create_job_from_form_treats_false_checkbox_as_disabled(self, plugin, mock_env):
        req = Mock()
        req.args = {
            "new_enabled": "false",
            "new_frequency": "daily",
            "new_title": "Create Daily Report",
            "new_owner": "admin",
            "new_description": "Auto ticket",
            "new_component": "Reports",
            "new_priority": "Normal",
            "new_offset": "0",
        }
        plugin._db_get_enabled = Mock(return_value=False)
        plugin._db_get_last_run = Mock(return_value=0)
        plugin._db_set_enabled = Mock()

        plugin._create_job_from_form(req)

        mock_env.config.set.assert_any_call("trac_cron_createticket", "job1.enabled", "false")
        plugin._db_set_enabled.assert_called_once_with("job1", False)

    def test_save_jobs_from_form_invalid_integers_are_sanitized(self, plugin, mock_env):
        req = Mock()
        req.args = {
            "ticker_interval": "invalid-interval",
            "enabled_1": "true",
            "frequency_1": "daily",
            "title_1": "Daily Report",
            "owner_1": "admin",
            "description_1": "Auto ticket",
            "component_1": "Reports",
            "priority_1": "Normal",
            "offset_1": "invalid-offset",
        }
        plugin._db_get_enabled = Mock(return_value=False)
        plugin._db_get_last_run = Mock(return_value=0)
        plugin._db_set_enabled = Mock()

        plugin._save_jobs_from_form(req)

        mock_env.config.set.assert_any_call("trac_cron_createticket", "ticker_interval", "60")
        mock_env.config.set.assert_any_call("trac_cron_createticket", "job1.offset", "0")

    def test_create_job_from_form_invalid_offset_uses_zero(self, plugin, mock_env):
        req = Mock()
        req.args = {
            "new_enabled": "true",
            "new_frequency": "daily",
            "new_title": "Create Daily Report",
            "new_owner": "admin",
            "new_description": "Auto ticket",
            "new_component": "Reports",
            "new_priority": "Normal",
            "new_offset": "invalid-offset",
        }
        plugin._db_get_enabled = Mock(return_value=False)
        plugin._db_get_last_run = Mock(return_value=0)
        plugin._db_set_enabled = Mock()

        plugin._create_job_from_form(req)

        mock_env.config.set.assert_any_call("trac_cron_createticket", "job1.offset", "0")


class TestScheduler:
    @patch("trac_cron_createticket.sleep")
    @patch("trac_cron_createticket.time", return_value=2000000)
    def test_run_scheduler_initializes_last_run_without_creating_ticket(self, _mock_time, mock_sleep, plugin, mock_env):
        plugin._jobs = [
            {
                "name": "job1",
                "cron": "0 0 * * *",
                "title": "Daily Report",
                "owner": "admin",
                "description": "",
                "component": "",
                "priority": "",
                "status": "new",
                "offset": 0,
                "last_run": 0,
            }
        ]
        plugin._load_jobs = Mock()
        plugin._create_ticket = Mock(return_value=True)
        plugin._db_init_job = Mock(return_value=True)

        def stop_after_sleep(_interval):
            plugin._stop_ticker = True

        mock_sleep.side_effect = stop_after_sleep
        plugin._stop_ticker = False
        plugin._run_scheduler()

        plugin._create_ticket.assert_not_called()
        plugin._db_init_job.assert_called_once_with("job1", 2000000)

    @patch("trac_cron_createticket.sleep")
    @patch("trac_cron_createticket.time", return_value=2000000)
    def test_run_scheduler_does_not_create_ticket_when_claim_fails(self, _mock_time, mock_sleep, plugin, mock_env):
        """Another process already claimed this job execution."""
        plugin._jobs = [
            {
                "name": "job1",
                "cron": "0 * * * *",
                "title": "Hourly Report",
                "owner": "admin",
                "description": "",
                "component": "",
                "priority": "",
                "status": "new",
                "offset": 0,
                "last_run": 1,
            }
        ]
        plugin._load_jobs = Mock()
        plugin._create_ticket = Mock(return_value=True)
        plugin._db_try_claim_job = Mock(return_value=False)

        def stop_after_sleep(_interval):
            plugin._stop_ticker = True

        mock_sleep.side_effect = stop_after_sleep
        plugin._stop_ticker = False
        plugin._run_scheduler()

        plugin._db_try_claim_job.assert_called_once()
        plugin._create_ticket.assert_not_called()

    @patch("trac_cron_createticket.sleep")
    @patch("trac_cron_createticket.time", return_value=2000000)
    def test_run_scheduler_creates_ticket_when_claim_succeeds(self, _mock_time, mock_sleep, plugin, mock_env):
        plugin._jobs = [
            {
                "name": "job1",
                "cron": "0 * * * *",
                "title": "Hourly Report",
                "owner": "admin",
                "description": "",
                "component": "",
                "priority": "",
                "status": "new",
                "offset": 0,
                "last_run": 1,
            }
        ]
        plugin._load_jobs = Mock()
        plugin._create_ticket = Mock(return_value=True)
        plugin._db_try_claim_job = Mock(return_value=True)

        def stop_after_sleep(_interval):
            plugin._stop_ticker = True

        mock_sleep.side_effect = stop_after_sleep
        plugin._stop_ticker = False
        plugin._run_scheduler()

        plugin._db_try_claim_job.assert_called_once()
        plugin._create_ticket.assert_called_once()

    @patch("trac_cron_createticket.sleep")
    @patch("trac_cron_createticket.time", return_value=2000000)
    def test_run_scheduler_reverts_claim_on_create_failure(self, _mock_time, mock_sleep, plugin, mock_env):
        plugin._jobs = [
            {
                "name": "job1",
                "cron": "0 * * * *",
                "title": "Hourly Report",
                "owner": "admin",
                "description": "",
                "component": "",
                "priority": "",
                "status": "new",
                "offset": 0,
                "last_run": 1,
            }
        ]
        plugin._load_jobs = Mock()
        plugin._create_ticket = Mock(return_value=False)
        plugin._db_try_claim_job = Mock(return_value=True)

        def stop_after_sleep(_interval):
            plugin._stop_ticker = True

        mock_sleep.side_effect = stop_after_sleep
        plugin._stop_ticker = False
        plugin._run_scheduler()

        plugin._create_ticket.assert_called_once()
        # First call: claim (old_last_run -> due_run)
        # Second call: revert (due_run -> old_last_run)
        assert plugin._db_try_claim_job.call_count == 2
        revert_call = plugin._db_try_claim_job.call_args_list[1]
        # Revert should swap back: (job_name, due_run, old_last_run)
        assert revert_call.args[0] == "job1"
        assert revert_call.args[2] == 1  # original last_run


class TestDbJobState:
    def test_db_try_claim_job_succeeds(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.rowcount = 1
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        result = plugin._db_try_claim_job("job1", 1000, 2000)
        assert result is True
        mock_cursor.execute.assert_called_once()

    def test_db_try_claim_job_fails_when_already_claimed(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.rowcount = 0
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        result = plugin._db_try_claim_job("job1", 1000, 2000)
        assert result is False

    def test_db_init_job_inserts_when_new(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=None)
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        result = plugin._db_init_job("job1", 2000000)
        assert result is True
        assert mock_cursor.execute.call_count == 2  # SELECT + INSERT

    def test_db_init_job_skips_when_exists(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=(1,))
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        result = plugin._db_init_job("job1", 2000000)
        assert result is False
        assert mock_cursor.execute.call_count == 1  # Only SELECT

    def test_db_delete_job(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_delete_job("job1")
        mock_cursor.execute.assert_called_once()
        assert "DELETE" in mock_cursor.execute.call_args.args[0]


class TestDbEnabledState:
    def test_db_get_enabled_returns_true(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db(query_rows=[(1,)])
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        assert plugin._db_get_enabled("job1") is True

    def test_db_get_enabled_returns_false_when_disabled(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db(query_rows=[(0,)])
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        assert plugin._db_get_enabled("job1") is False

    def test_db_get_enabled_returns_false_when_not_found(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        assert plugin._db_get_enabled("job1") is False

    def test_db_set_enabled_updates_existing(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=(1,))
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_set_enabled("job1", True)
        assert mock_cursor.execute.call_count == 2  # SELECT + UPDATE
        update_call = mock_cursor.execute.call_args_list[1]
        assert "UPDATE" in update_call.args[0]
        assert update_call.args[1] == (1, "job1")

    def test_db_set_enabled_inserts_new(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=None)
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_set_enabled("job1", False)
        assert mock_cursor.execute.call_count == 2  # SELECT + INSERT
        insert_call = mock_cursor.execute.call_args_list[1]
        assert "INSERT" in insert_call.args[0]
        assert insert_call.args[1] == ("job1", 0, 0)


class TestAdminPanelActions:
    def test_delete_job_cleans_db_state(self, plugin, mock_env):
        plugin._db_delete_job = Mock()
        plugin._db_get_enabled = Mock(return_value=False)
        plugin._db_get_last_run = Mock(return_value=0)

        plugin._delete_job(1)

        plugin._db_delete_job.assert_called_once_with("job1")
        mock_env.config.remove.assert_any_call("trac_cron_createticket", "job1.enabled")

    def test_render_admin_panel_ignores_invalid_delete_action(self, plugin):
        req = Mock()
        req.method = "POST"
        req.args = {"action": "delete_job_not-a-number"}
        req.href = Mock()
        req.href.admin = Mock(return_value="/admin/endpoint")
        req.redirect = Mock()

        plugin._ensure_ticker_state = Mock()
        plugin._delete_job = Mock()
        plugin._render_admin_page = Mock(return_value=("template.html", {}))

        result = plugin.render_admin_panel(req, "trac_cron_createticket", "cron_createticket", "")

        plugin._delete_job.assert_not_called()
        req.redirect.assert_called_once_with("/admin/endpoint")
        assert result == ("template.html", {})


class TestDbUpgrade:
    def test_environment_needs_upgrade_when_version_0(self, plugin, mock_env):
        mock_env.config.getint = Mock(return_value=0)
        assert plugin.environment_needs_upgrade() is True

    def test_environment_needs_upgrade_when_version_1(self, plugin, mock_env):
        mock_env.config.getint = Mock(return_value=1)
        assert plugin.environment_needs_upgrade() is True

    def test_environment_needs_upgrade_when_version_2(self, plugin, mock_env):
        mock_env.config.getint = Mock(return_value=2)
        assert plugin.environment_needs_upgrade() is True

    def test_environment_no_upgrade_when_current(self, plugin, mock_env):
        mock_env.config.getint = Mock(return_value=3)
        assert plugin.environment_needs_upgrade() is False
