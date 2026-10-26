# Job Market Intelligence

Job discovery for data analyst, data engineer, and backend roles, built from permitted
public listings on official company career pages and applicant tracking systems.

## Status

Early development, focused on Dallas-Fort Worth and Greater Houston. What exists today:

- A registry of 57 researched Greenhouse job boards (`config/greenhouse_boards.toml`):
  21 boards with a verified official employer connection are enabled; 36 candidates
  stay disabled until their connection is verified.
- A Greenhouse collector that validates every response, keeps complete, partial, and
  failed results apart per employer, and stores usable records with full history.
- Rule-based screening that keeps relevant data, analytics, BI, software, data science,
  and ML roles located in DFW/Houston or remote and open to Texas residents.
- Retry incidents (three retries over about three hours), an alert/recovery outbox, and
  four-day retention of raw responses.
- Unit, database, and end-to-end collection tests, and a GitHub Actions workflow that
  runs them against PostgreSQL 17.

Not yet in place:

- **Scheduling.** Nothing runs automatically. Collections and retries run only when
  `jobmarket collect` or `jobmarket retry-due` is executed. The intended schedule (noon
  and 9 PM America/Chicago, plus retry checks) will be wired to a scheduler later.
- **Alert email delivery.** Alerts and recovery notices are written to the
  `notifications` outbox. An SMTP sender exists but no provider is configured, so no
  email is sent.
- **A web interface.** Status is available through `jobmarket status` and the
  `board_status` database view.

## Requirements

- Python 3.11 or newer
- PostgreSQL (developed against 17)

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # then fill in your own values
```

Create a dedicated role and database for the application rather than connecting as a
superuser (example using `psql` as a superuser):

```sql
CREATE ROLE jobmarket LOGIN;
\password jobmarket
CREATE DATABASE job_market OWNER jobmarket;
```

`\password` prompts for the password, so it is not written to the psql history file.
Put the role name and password in `.env`.

Then check the connection, create the schema, and load the board registry:

```bash
jobmarket check-db
jobmarket migrate
jobmarket sources sync
```

### Configuration

Settings come from environment variables. A `.env` file in the working directory is
loaded if present; variables already set in the environment take precedence.

| Variable      | Required | Default     | Notes                          |
|---------------|----------|-------------|--------------------------------|
| `DB_HOST`     | no       | `localhost` |                                |
| `DB_PORT`     | no       | `5432`      | whole number from 1 to 65535   |
| `DB_NAME`     | yes      |             |                                |
| `DB_USER`     | yes      |             |                                |
| `DB_PASSWORD` | no       | empty       | empty falls back to `~/.pgpass` or local trust auth |

Optional settings:

| Variable                     | Default                         | Notes |
|------------------------------|---------------------------------|-------|
| `JOBMARKET_REGISTRY`         | `config/greenhouse_boards.toml` | board registry file |
| `JOBMARKET_RAW_DIR`          | `data/raw`                      | where raw responses are stored (ignored by Git) |
| `JOBMARKET_REQUEST_INTERVAL` | `1.0`                           | seconds between board requests |
| `JOBMARKET_SMTP_HOST`, `JOBMARKET_SMTP_PORT` (587), `JOBMARKET_SMTP_USER`, `JOBMARKET_SMTP_PASSWORD`, `JOBMARKET_SMTP_STARTTLS` (`true`), `JOBMARKET_ALERT_FROM`, `JOBMARKET_ALERT_TO` | unset | alert delivery; without host, sender, and recipient, notifications stay in the outbox |

`.env` is ignored by Git. Never commit real credentials.

## Usage

| Command | What it does |
|---------|--------------|
| `jobmarket check-db` | Connect and run `SELECT 1`; print database, user, version |
| `jobmarket migrate [--status]` | Apply pending migrations, or list them |
| `jobmarket sources sync` | Load the board registry into the database (boards are never deleted) |
| `jobmarket sources list [--all]` | Show enabled boards, or all boards, leads, and holds |
| `jobmarket collect [--board TOKEN ...] [--scheduled]` | Scan enabled boards now |
| `jobmarket retry-due` | Run only the retries that are due now |
| `jobmarket status [--all]` | Per-board last result, last complete scan, retained jobs, incidents |
| `jobmarket jobs [--board TOKEN] [--early-career] [--review] [--limit N]` | Retained jobs, newest discoveries first |
| `jobmarket notifications [--full] [--send]` | Show the alert outbox, or deliver it if SMTP is configured |
| `jobmarket cleanup-raw [--dry-run]` | Delete raw responses older than four days |

Exit codes: `0` success, `1` failure (database, migration, or a collection where every
board failed), `2` configuration or registry error, `3` another collection is running.

## How collection works

```
config/greenhouse_boards.toml --sources sync--> employers, source_boards
                                                      |
jobmarket collect / retry-due                         v
  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    -> raw response saved to data/raw/... (raw_responses row: URL, status, sha256, size)
    -> validate: HTTP 200, JSON content type, jobs list, required fields per record,
       record count equals meta.total
         complete | partial (usable, with rejected records) | failed (nothing usable)
    -> for each valid job: source_postings (identity, first discovered, last observed)
       and posting_screenings (relevance, Texas eligibility, seniority, sponsorship)
    -> retained jobs: posting_versions (new row only when content changes) and
       posting_observations (one per scan)
    -> collection_incidents: retry state; notifications: alert/recovery outbox
```

- The single documented API host serves EU-hosted boards too; the region only changes
  human-facing board links (`job-boards.eu.greenhouse.io`). No pagination is assumed.
- Application URLs (`absolute_url`) are stored exactly as returned.
- Content fingerprints exclude source timestamps such as `updated_at`, so a timestamp
  change alone never creates a new version.
- Only complete scans count a job as missing (`complete_scan_absences`). Failed and
  partial scans never change existing records. Nothing is closed yet.
- Possible duplicates (same employer, title, and description across boards or
  city-targeted postings) are recorded in `posting_duplicate_flags`, never merged.
- Sponsorship is `not_stated` unless the posting says whether it is available; a
  question asking applicants about sponsorship is recorded as `asked_only`.

### Failures, retries, and alerts

- Each board's scan is `complete`, `partial`, or `failed`. One board's failure never
  stops the others, and an unexpected error while processing one board is recorded on
  that board's scan.
- A failed or partial scan opens an incident with retries due about 1, 2, and 3 hours
  after the first failure (later if the server sends a longer `Retry-After`). Retry state
  is stored in the database, so it survives restarts. A collection run while no retry is
  due neither uses nor resets the retry budget. A PostgreSQL advisory lock prevents
  overlapping collections; scans left `running` by a stopped process are marked failed
  on the next run.
- `jobmarket status` reflects a failure as soon as the scan finishes. When the retries
  are exhausted, one alert is queued per incident (including whole-run failures); when an
  alerted incident is resolved, one recovery notice is queued. Messages list the affected
  employers, errors, retries used, last successful collection, and what remains usable.

### Raw response retention

Raw responses are kept for four days. `jobmarket cleanup-raw` deletes older files and
marks their rows `purged_at`; job records, history, and response metadata are never
deleted.

## Database schema

Relationships (`A ─< B` means one `A` has many `B`):

```
employers        ─< source_boards
source_boards    ─< board_scans, source_postings
collection_runs  ─< board_scans
board_scans      ─< raw_responses, posting_observations
source_postings  ─< posting_versions, posting_observations
posting_versions ─< posting_observations
raw_responses    ─< posting_observations
```

| Table                  | One row per                                                       |
|------------------------|-------------------------------------------------------------------|
| `employers`            | company                                                           |
| `source_boards`        | public job board of an employer in one source system              |
| `collection_runs`      | scheduled or manual scan                                          |
| `board_scans`          | board scanned during a run (`running`, `complete`, `partial`, `failed`) |
| `raw_responses`        | preserved response received during a board scan (bytes stored outside the database) |
| `source_postings`      | job as one board identifies it, with first discovery and last observation times |
| `posting_versions`     | distinct content of a posting                                     |
| `posting_observations` | sighting of a posting in a board scan, with the source's stated dates |
| `posting_screenings`   | current screening decision for every valid posting seen |
| `rejected_records`     | record from a usable response that failed validation |
| `collection_incidents` | run of unsuccessful scans of a board, with retry state |
| `notifications`        | queued alert or recovery message (the outbox) |
| `posting_duplicate_flags` | possible duplicate postings, for review |

Views: `board_status` (one row per board for status displays) and `retained_jobs`
(retained postings with their latest content).

Design rules enforced by the schema:

- The source's publication time, our first discovery time, our last observation time,
  and content change times are separate columns. A missing source date is stored as
  `NULL`, never guessed.
- Only a `complete` board scan records a definite posting count; `partial` and `failed`
  scans are stored explicitly and never imply a job closed.
- Natural identities are unique (one posting per board and source ID, one observation per
  posting per scan), so loading the same data twice cannot create duplicates.
- An observation can only reference a content version of the same posting and a raw
  response from the same scan.
- Rows with history cannot be deleted while other rows depend on them.

### Migrations

Migration files live in `src/jobmarket/migrations/` and are named
`NNNN_description.sql`. The runner:

- applies pending files in version order, each in its own transaction together with its
  record in `schema_migrations`, so a failing file leaves no partial changes;
- refuses to run if an applied file was edited or deleted, or if a new file is numbered
  before the latest applied one;
- holds a PostgreSQL advisory lock so two runners cannot migrate at the same time.

Migrations are forward-only: fix a mistake with a new migration, never by editing an
applied one. Files must not contain `BEGIN`/`COMMIT` or statements that cannot run in a
transaction (such as `CREATE INDEX CONCURRENTLY`).

## Tests

```bash
ruff check .
ruff format --check .
pytest
```

Tests marked `db` need a reachable database configured through the `DB_*` variables.
Without one they are skipped locally. Set `JOBMARKET_REQUIRE_DB=1` to make a missing
database a failure instead (CI does this).

Database tests create a temporary schema, work only inside it, and drop it afterwards;
they do not modify existing tables in the configured database. The role needs permission
to create schemas in that database (the database owner has it).

## Continuous integration

`.github/workflows/ci.yml` runs on pushes and pull requests to `main`: it starts a
PostgreSQL 17 service container, installs the package, runs `ruff` and `pytest` with
`JOBMARKET_REQUIRE_DB=1`, runs `jobmarket check-db`, applies the migrations twice to
confirm the second run changes nothing, and loads the board registry. Collection tests use
a scripted HTTP transport; CI never contacts Greenhouse.
