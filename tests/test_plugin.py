import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch

from trac.env import Environment
from trac_cron_createticket import CronCreateTicketPlugin


@pytest.fixture
def mock_env():
    env = MagicMock(spec=Environment)
    env.config = Mock()
    env.config.getbool = Mock(return_value=False)
    env.config.get = Mock(return_value="")
    env.config.getint = Mock(return_value=0)
    env.config.set = Mock()
    env.config.save = Mock()
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

        with patch("trac_cron_createticket.Ticket", return_value=mock_ticket):
            job = {
                "title": "Test Ticket",
                "owner": "test_user",
                "description": "Test description",
                "component": "",
                "priority": "",
                "offset": 0,
            }
            plugin._create_ticket(job)
            mock_env.log.info.assert_called_once()

    @patch("trac_cron_createticket.Ticket")
    def test_create_ticket_with_component_priority(self, mock_ticket_class, plugin, mock_env):
        mock_ticket = Mock()
        mock_ticket.id = 456
        mock_ticket.insert = Mock()
        mock_ticket_class.return_value = mock_ticket

        job = {
            "title": "Test Ticket [today]",
            "owner": "test_user",
            "description": "Test description",
            "component": "Testing",
            "priority": "High",
            "offset": 0,
        }
        plugin._create_ticket(job)
        mock_env.log.info.assert_called_once()


class TestJobLoading:
    def test_load_jobs_empty_config(self, plugin, mock_env):
        mock_env.config.getbool = Mock(return_value=False)
        plugin._load_jobs()
        assert plugin._jobs == []

    def test_load_jobs_with_enabled_job(self, plugin, mock_env):

        def mock_get_bool(section, option, default=False):
            if option == "job1.enabled":
                return True
            return default

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

        mock_env.config.getbool = Mock(side_effect=mock_get_bool)
        mock_env.config.get = Mock(side_effect=mock_get)
        mock_env.config.getint = Mock(return_value=0)

        plugin._load_jobs()
        assert len(plugin._jobs) == 1
        assert plugin._jobs[0]["name"] == "job1"
        assert plugin._jobs[0]["title"] == "Daily Report"
        assert plugin._jobs[0]["owner"] == "admin"

    def test_load_jobs_with_invalid_frequency(self, plugin, mock_env):

        def mock_get_bool(section, option, default=False):
            if option == "job1.enabled":
                return True
            return default

        def mock_get(section, option, default=""):
            if option == "job1.frequency":
                return "invalid"
            return default

        mock_env.config.getbool = Mock(side_effect=mock_get_bool)
        mock_env.config.get = Mock(side_effect=mock_get)

        plugin._load_jobs()
        assert len(plugin._jobs) == 0


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
