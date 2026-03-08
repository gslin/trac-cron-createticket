# TracCronCreateTicket Plugin ![GitHub Actions CI](https://img.shields.io/github/actions/workflow/status/USERNAME/trac-cron-createticket/ci.yml?label=CI&logo=github-actions)

## Introduction

`TracCronCreateTicket` is a plugin for **Trac 1.6+** that provides **automated, scheduled ticket creation**. Users can configure schedules via `trac.ini` or through the admin web interface. Supported schedule types:

- **Presets**: `hourly`, `daily`, `weekly`, `monthly`, `quarterly`, `yearly`
- **Cron expressions** (using `croniter`), e.g. `* 0/2 * * * ? *`

The generated tickets can have the following fields (all support template variables):

- **Title** (required)
- **Owner** (required)
- **Description**
- **Component**
- **Priority**

### Template Variables

You can use the following placeholders in title, owner, description, and component fields:

- `[now]` – current datetime
- `[now_unix]` – current Unix timestamp
- `[today]` – today's date (YYYY‑MM‑DD)
- `[tomorrow]` – tomorrow's date (YYYY‑MM‑DD)
- `[yesterday]` – yesterday's date (YYYY‑MM‑DD)
- `[offset:N]` – date N seconds from now (e.g. `[offset:86400]` for tomorrow)

## Installation

1. **Install `uv`** (if you don't have it already)
   ```bash
   pip install uv
   ```

2. **Install dependencies with `uv`** (run in the project root)
   ```bash
   uv sync   # reads pyproject.toml and installs Trac, croniter, etc.
   ```

3. **Install the plugin in editable mode** (so changes take effect immediately)
   ```bash
   uv pip install -e .
   ```

4. **Enable the plugin in Trac's `trac.ini`**
   ```ini
   [components]
   trac_cron_createticket = enabled
   ```

5. **Restart Trac** (or reload plugins via `trac-admin`)
   ```bash
   trac-admin /path/to/trac_env plugin reload
   ```

## Configuration

The plugin can be configured in two ways:

### 1. Via the Admin Web Interface (recommended)
- Navigate to *Admin → Cron Create Ticket* in Trac.
- Set **Scheduler Settings** (enable/disable and check interval).
- Add up to 10 jobs in **Scheduled Jobs**, specifying frequency, title, owner, etc.
- Click **Save Configuration**.
- Use **Test Ticket Creation** to immediately verify a job.

### 2. Directly in `trac.ini`
```ini
[trac_cron_createticket]
# Global settings
ticker_enabled = true
ticker_interval = 60   ; seconds between checks

# Example job (job1)
job1.enabled = true
job1.frequency = daily   ; preset or cron expression
job1.title = Daily report [today]
job1.owner = admin
job1.description = This ticket is generated automatically each day.
job1.component = System
job1.priority = normal
job1.offset = 0
```

## Permissions

Two custom permissions are defined:

- `TRAC_CRON_CREATE_TICKET_ADMIN` – manage schedules (requires `TRAC_ADMIN`).
- `TRAC_CRON_CREATE_TICKET_VIEW` – view schedules (granted to `TRAC_ADMIN` or any user with `TICKET_VIEW`).

Assign these permissions in *Admin → Permissions*.

## Testing

```bash
# Verify the plugin can be imported without errors
uv run python -c "import trac_cron_createticket"

# Use the web UI test form to create a test ticket (reporter will be 'cron_create_ticket')
```

## Supported Databases

The plugin uses Trac’s built‑in database API, so it works with any database Trac supports, including **SQLite** and **MySQL/MariaDB**. Just ensure Trac itself is correctly configured for your database.

---

**License:** BSD License
**Author:** Your Name (<your@email.com>)

If you encounter any issues or have feature requests, feel free to open an issue on the project’s GitHub repository. Happy ticketing!
