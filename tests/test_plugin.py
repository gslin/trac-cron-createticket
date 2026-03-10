from datetime import datetime, timedelta, timezone
from threading import Lock
from unittest.mock import Mock, MagicMock, patch

import pytest

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
        assert result != template

    def test_expand_now_unix_placeholder(self, plugin):
        template = "Timestamp: [now_unix]"
        result = plugin._expand_template(template)
        assert "Timestamp:" in result

    def test_expand_today_placeholder(self, plugin):
        template = "Due: [today]"
        result = plugin._expand_template(template)
        expected = (datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_tomorrow_placeholder(self, plugin):
        template = "Due: [tomorrow]"
        result = plugin._expand_template(template)
        expected = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_yesterday_placeholder(self, plugin):
        template = "Started: [yesterday]"
        result = plugin._expand_template(template)
        expected = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        assert expected in result

    def test_expand_offset_placeholder(self, plugin):
        template = "Schedule: [offset:86400]"
        result = plugin._expand_template(template)
        expected = (datetime.now(timezone.utc) + timedelta(seconds=86400)).strftime("%Y-%m-%d")
        assert expected in result


class TestTicketCreation:
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
        }
        assert plugin._create_ticket(job) is False
        mock_env.log.error.assert_called_once()


class TestJobLoading:
    def test_load_jobs_no_enabled_jobs(self, plugin):
        plugin._db_get_all_jobs = Mock(return_value=[
            {"name": "job1", "last_run": 0, "enabled": False, "frequency": "daily",
             "title": "Report", "owner": "admin", "description": "",
             "component": "", "priority": "", "status": "new"},
        ])
        plugin._load_jobs()
        assert plugin._jobs == []

    def test_load_jobs_with_enabled_job(self, plugin):
        plugin._db_get_all_jobs = Mock(return_value=[
            {"name": "job1", "last_run": 1000000, "enabled": True, "frequency": "daily",
             "title": "Daily Report", "owner": "admin", "description": "Automated report",
             "component": "Reports", "priority": "Normal", "status": "new"},
        ])

        plugin._load_jobs()
        assert len(plugin._jobs) == 1
        assert plugin._jobs[0]["name"] == "job1"
        assert plugin._jobs[0]["title"] == "Daily Report"
        assert plugin._jobs[0]["owner"] == "admin"
        assert plugin._jobs[0]["last_run"] == 1000000

    def test_load_jobs_with_invalid_frequency(self, plugin):
        plugin._db_get_all_jobs = Mock(return_value=[
            {"name": "job1", "last_run": 0, "enabled": True, "frequency": "invalid",
             "title": "Report", "owner": "admin", "description": "",
             "component": "", "priority": "", "status": "new"},
        ])

        plugin._load_jobs()
        assert len(plugin._jobs) == 0


class TestComponentsAndPriorities:
    def test_get_components(self, plugin, mock_env):
        mock_db = MagicMock()
        mock_cursor = Mock()
        mock_cursor.fetchall = Mock(return_value=[("Component1",), ("Component2",)])
        mock_db.cursor = Mock(return_value=mock_cursor)
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        result = plugin._get_components()
        assert result == ["Component1", "Component2"]

    def test_get_priorities(self, plugin, mock_env):
        mock_db = MagicMock()
        mock_cursor = Mock()
        mock_cursor.fetchall = Mock(return_value=[("Low",), ("Normal",), ("High",)])
        mock_db.cursor = Mock(return_value=mock_cursor)
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        result = plugin._get_priorities()
        assert result == ["Low", "Normal", "High"]


class TestFormHandling:
    def test_save_jobs_from_form_persists_scheduler_settings(self, plugin, mock_env):
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
        }
        plugin._db_set_enabled = Mock()
        plugin._db_save_job = Mock()
        plugin._db_get_all_jobs = Mock(return_value=[])

        plugin._save_jobs_from_form(req)

        mock_env.config.set.assert_any_call("trac_cron_createticket", "ticker_enabled", "false")
        mock_env.config.set.assert_any_call("trac_cron_createticket", "ticker_interval", "30")
        plugin._db_set_enabled.assert_any_call("job1", False)
        plugin._db_save_job.assert_any_call("job1", {
            "frequency": "daily",
            "title": "Daily Report",
            "owner": "admin",
            "description": "Auto ticket",
            "component": "Reports",
            "priority": "Normal",
            "status": "new",
        })
        mock_env.config.save.assert_called_once()

    def test_create_job_from_form_saves_to_db(self, plugin, mock_env):
        req = Mock()
        req.args = {
            "new_enabled": "false",
            "new_frequency": "daily",
            "new_title": "Create Daily Report",
            "new_owner": "admin",
            "new_description": "Auto ticket",
            "new_component": "Reports",
            "new_priority": "Normal",
        }
        plugin._db_get_all_jobs = Mock(return_value=[])
        plugin._db_save_job = Mock()
        plugin._db_set_enabled = Mock()

        plugin._create_job_from_form(req)

        plugin._db_save_job.assert_called_once_with("job1", {
            "frequency": "daily",
            "title": "Create Daily Report",
            "owner": "admin",
            "description": "Auto ticket",
            "component": "Reports",
            "priority": "Normal",
            "status": "new",
        })
        plugin._db_set_enabled.assert_called_once_with("job1", False)

    def test_create_job_from_form_finds_next_available_slot(self, plugin, mock_env):
        req = Mock()
        req.args = {
            "new_enabled": "on",
            "new_frequency": "weekly",
            "new_title": "Weekly Report",
            "new_owner": "admin",
            "new_description": "",
            "new_component": "",
            "new_priority": "",
        }
        # job1 is occupied, job2 should be used
        plugin._db_get_all_jobs = Mock(return_value=[
            {"name": "job1", "last_run": 0, "enabled": True, "frequency": "daily",
             "title": "Existing Job", "owner": "admin", "description": "",
             "component": "", "priority": "", "status": "new"},
        ])
        plugin._db_save_job = Mock()
        plugin._db_set_enabled = Mock()

        plugin._create_job_from_form(req)

        plugin._db_save_job.assert_called_once()
        assert plugin._db_save_job.call_args.args[0] == "job2"

    def test_save_jobs_from_form_invalid_interval_uses_default(self, plugin, mock_env):
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
        }
        plugin._db_set_enabled = Mock()
        plugin._db_save_job = Mock()
        plugin._db_get_all_jobs = Mock(return_value=[])

        plugin._save_jobs_from_form(req)

        mock_env.config.set.assert_any_call("trac_cron_createticket", "ticker_interval", "60")


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
        assert plugin._db_try_claim_job.call_count == 2
        revert_call = plugin._db_try_claim_job.call_args_list[1]
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

    def test_db_delete_job(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_delete_job("job1")
        mock_cursor.execute.assert_called_once()
        assert "DELETE" in mock_cursor.execute.call_args.args[0]

    def test_db_save_job_inserts_new(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=None)
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_save_job("job1", {
            "frequency": "daily",
            "title": "Report",
            "owner": "admin",
            "description": "desc",
            "component": "Sys",
            "priority": "Normal",
            "status": "new",
        })
        assert mock_cursor.execute.call_count == 2  # SELECT + INSERT
        insert_call = mock_cursor.execute.call_args_list[1]
        assert "INSERT" in insert_call.args[0]

    def test_db_save_job_updates_existing(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=(1,))
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_save_job("job1", {
            "frequency": "daily",
            "title": "Report",
            "owner": "admin",
            "description": "desc",
            "component": "Sys",
            "priority": "Normal",
            "status": "new",
        })
        assert mock_cursor.execute.call_count == 2  # SELECT + UPDATE
        update_call = mock_cursor.execute.call_args_list[1]
        assert "UPDATE" in update_call.args[0]

    def test_db_get_all_jobs(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchall = Mock(return_value=[
            ("job1", 1000, 1, "daily", "Report", "admin", "desc", "Sys", "Normal", "new"),
            ("job2", 0, 0, "", "", "", "", "", "", "new"),
        ])
        mock_env.db_query.__enter__ = Mock(return_value=mock_db)
        mock_env.db_query.__exit__ = Mock(return_value=False)

        result = plugin._db_get_all_jobs()
        assert len(result) == 2
        assert result[0]["name"] == "job1"
        assert result[0]["enabled"] is True
        assert result[0]["title"] == "Report"
        assert result[1]["name"] == "job2"
        assert result[1]["enabled"] is False


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
        assert mock_cursor.execute.call_count == 2
        update_call = mock_cursor.execute.call_args_list[1]
        assert "UPDATE" in update_call.args[0]
        assert update_call.args[1] == (1, "job1")

    def test_db_set_enabled_inserts_new(self, plugin, mock_env):
        mock_db, mock_cursor = _make_mock_db()
        mock_cursor.fetchone = Mock(return_value=None)
        mock_env.db_transaction.__enter__ = Mock(return_value=mock_db)
        mock_env.db_transaction.__exit__ = Mock(return_value=False)

        plugin._db_set_enabled("job1", False)
        assert mock_cursor.execute.call_count == 2
        insert_call = mock_cursor.execute.call_args_list[1]
        assert "INSERT" in insert_call.args[0]


class TestAdminPanelActions:
    def test_delete_job_removes_from_db(self, plugin, mock_env):
        plugin._db_delete_job = Mock()
        plugin._db_get_all_jobs = Mock(return_value=[])

        plugin._delete_job(1)

        plugin._db_delete_job.assert_called_once_with("job1")

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

    def test_environment_needs_upgrade_when_version_3(self, plugin, mock_env):
        mock_env.config.getint = Mock(return_value=3)
        assert plugin.environment_needs_upgrade() is True

    def test_environment_no_upgrade_when_current(self, plugin, mock_env):
        mock_env.config.getint = Mock(return_value=4)
        assert plugin.environment_needs_upgrade() is False
