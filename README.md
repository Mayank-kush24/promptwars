# Prompt Wars (Flask only)

Single **Python / Flask** application: dashboard, admin CSV import, and JSON APIs.

**Development** (`FLASK_DEBUG=1`, default): Flask’s built-in server with the debugger.

```bash
python app.py
```

**Production** (hundreds of concurrent users on one Windows host): set `FLASK_DEBUG=0` and `FLASK_USE_RELOADER=0`, then start the app — it serves with **Waitress** (multi-threaded). Tune `WAITRESS_THREADS` (e.g. 32–64). Use `python -m waitress --listen=127.0.0.1:5000 wsgi:app` if you prefer the CLI.

Then open **http://127.0.0.1:5000** (or your configured `FLASK_HOST` / `FLASK_PORT`).

**Caching topology:** read-through TTL caches live **in-process** (see `services/cache.py`). They are correct for **one Waitress process**. If you scale to multiple processes or hosts, replace with Redis or another shared cache and invalidate on writes accordingly.

**Chart / ECharts:** place `chart.umd.min.js` and `echarts.min.js` under `static/vendor/` to serve them locally; otherwise the UI falls back to jsDelivr (see `pw_vendor_chart_js` in templates).

### Web UI

| Path | Purpose |
|------|---------|
| `/` | Main overview (aggregated in-person + virtual stats) |
| `/in-person` | In-person Main Data Center analytics (map + charts) |
| `/virtual` | Virtual leaderboard + score distribution (polling) |

Optional query params on all three: `inPersonEventId`, `virtualEventId`, `challengeId` (defaults from `.env`).

## Prerequisites

- **PostgreSQL** (local)
- **Python 3.11+** (recommended)

## Database setup

```bash
psql "postgresql://postgres:postgres@127.0.0.1:5432/postgres" -c "CREATE DATABASE prompt_wars;"
psql "$DATABASE_URL" -f database/init.sql
```

On Windows PowerShell:

```powershell
psql $env:DATABASE_URL -f database/init.sql
```

## Configuration

Copy [.env.example](.env.example) to `.env` in the repository root (next to `app.py`).

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Postgres connection string |
| `FLASK_HOST` / `FLASK_PORT` | Bind address (default `127.0.0.1:5000`) |
| `SESSION_SECRET` | Flask session signing secret |
| `ADMIN_PASSWORD` | Optional; if set, `/admin` requires login |
| `DEFAULT_IN_PERSON_EVENT_ID` | Main Data Center import scope + in-person page query default |
| `DEFAULT_VIRTUAL_EVENT_ID` | Reference for operators |
| `DEFAULT_CHALLENGE_ID` | Dashboard leaderboard + distribution |

Additional knobs are documented in [.env.example](.env.example) (`DB_POOL_*`, `PW_CACHE_*`, `WAITRESS_*`, `AUDIT_SQL_SELECTS`, `AUDIT_QUEUE_DROP_OLDEST`, `PW_SLOW_REQUEST_LOG`, etc.).

Optional DB indexes for heavy MDC date filters: [database/migrate_perf_indexes.sql](database/migrate_perf_indexes.sql) (`CREATE INDEX CONCURRENTLY`).

## Install and run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## CSV import (Admin)

From **http://127.0.0.1:5000/admin**, upload two CSV files.

Required columns (case-insensitive):

- `user_id`, `city_id`

Optional: `display_name`, `rsvped_at` (RSVPs), `submitted_at` (Submissions).

`city_id` values must exist in `cities` for the selected in-person `event_id`.

Imports still run **synchronously** in the request handler (correct for Flask request context and multipart bodies). A small `ThreadPoolExecutor` is created at import time (`PW_IMPORT_THREAD_POOL`, default 2 workers) as a **reserved hook** for a future async path; it is not used yet. For large files, use a higher `WAITRESS_CHANNEL_TIMEOUT` or run imports off-peak.

## JSON API (same process)

| Method | Path | Notes |
|--------|------|------|
| GET | `/api/health` | DB ping + optional `db_pool_*` and `audit_queue_depth` |
| GET | `/api/funnel?event_id=` | City conversion aggregates |
| GET | `/api/stats/city/<city_id>` | Includes `missing_in_action` |
| GET | `/api/leaderboard?event_id=` or `?challenge_id=` | Exactly one scope param |
| GET | `/api/distribution?event_id=` or `?challenge_id=` | Histogram bins |
| GET | `/api/import/latest` | Latest `import_jobs` row |
| POST | `/api/import/in-person` | Same as admin import; requires admin session if `ADMIN_PASSWORD` is set |
| POST | `/api/credits/grant` | JSON body; requires admin session if `ADMIN_PASSWORD` is set |

## Tests

```powershell
$env:PYTHONPATH="."
pytest tests -q
```

(POSIX: `PYTHONPATH=. pytest tests -q`.)
