# TracCronCreateTicket Plugin ![CI](https://github.com/gslin/trac-cron-createticket/actions/workflows/ci.yml/badge.svg)

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

You can use the following placeholders in title, description, and priority fields (all times are in UTC):

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

5. **Run the Trac upgrade** (creates the required database table)
   ```bash
   trac-admin /path/to/trac_env upgrade
   ```

6. **Restart Trac** to load the plugin. The method depends on your deployment:
   - **tracd**: restart the `tracd` process
   - **Apache + mod_wsgi**: `systemctl restart apache2` (or `apachectl restart`)
   - **FastCGI / uWSGI**: restart the corresponding application process

## Upgrading

If you are upgrading from a previous version, run the database upgrade after installing the new version:

```bash
trac-admin /path/to/trac_env upgrade
```

This creates the `cron_createticket_jobs` table and migrates job state from `trac.ini` to the database. The migration is required for cross-process locking to prevent duplicate ticket creation in multi-process environments (e.g. FastCGI).

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

**License:** MIT License
**Author:** Gea-Suan Lin

If you encounter any issues or have feature requests, feel free to open an issue on the project’s GitHub repository. Happy ticketing!
