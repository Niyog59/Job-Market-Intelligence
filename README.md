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
- Incremental history: a new content version only for meaningful changes, versioned
  normalization rules, current state ordered by collection time, and safe replay of
  saved responses.
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
| `jobmarket replay --scan ID [--scan ID ...]` / `--available` | Reprocess saved raw responses under their original collection time |
| `jobmarket history POSTING_ID` / `--board TOKEN --job SOURCE_ID` | Content stages, change windows, and versions of one posting |
| `jobmarket refingerprint` | Evaluate stored versions under the current normalization rules; report rule effects |
| `jobmarket check-history` | Run the history integrity checks (exit `1` if any fail) |

Exit codes: `0` success, `1` failure (database, migration, a collection where every
board failed, or a replay that failed or found a missing or altered file), `2`
configuration or registry error, `3` another collection or replay is running.

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
    -> retained jobs: posting_observations (one per scan) and posting_versions (a new
       row only for a meaningful change; see "Posting history")
    -> collection_incidents: retry state; notifications: alert/recovery outbox
```

- The single documented API host serves EU-hosted boards too; the region only changes
  human-facing board links (`job-boards.eu.greenhouse.io`). No pagination is assumed.
- Application URLs (`absolute_url`) are stored exactly as returned.
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
deleted. A replay of a purged response reports `expired`.

## Posting history

**Identity.** A job is identified by board (source system and board token) and the
source's job id, enforced by a unique constraint; titles are never identifiers.
`first_discovered_at` is the earliest collection time at which the job was seen. The
source's own `first_published` and `updated_at` are kept per observation as evidence,
not treated as a posting date.

**Observations.** Every scan of a tracked job (one that is or was ever retained) adds one
observation. Observations are unique per (board scan, posting), so processing the same
saved response again adds nothing.

**Meaningful changes.** `jobmarket.normalize` builds a comparison form of each content:
title, description wording, locations (which carry remote restrictions), published
compensation, and employment type. HTML markup, whitespace, invisible characters,
typographic quote/dash variants, and timestamps are ignored; case, numbers, and wording
are kept. A new version is written only when the comparison form differs from the
chronologically neighbouring observation. Unchanged content reuses the existing version,
and returning to earlier content (A, B, A) is a new version, so the sequence is kept.
Each version keeps its original source content (`content_html`, offices, metadata, and
so on), its exact source fingerprint, and the rules version that detected it; its
comparison form under each rules version is in `posting_version_comparisons`.

**Rule changes are not employer edits.** Normalization rules are versioned
(`CURRENT_NORMALIZATION`) and old rule sets stay registered. Identical source content is
never a change, whatever the rules. When the neighbouring observation's own content was
not stored (it was a cosmetic variant of its version), the new content is compared under
the rules that observation was recorded with. `jobmarket refingerprint` evaluates stored
versions under the current rules and lists versions whose form differs only because of
the rules, without creating versions.

**Chronology.** Collection time (`observed_at`, the response's fetch time) orders
everything; `processed_at` is recorded separately. `source_postings.current_observation_id`
and `posting_screenings` only move forward in collection time, so an older saved response
processed later adds history (and can move first discovery earlier) but never replaces
newer current state. Exact collection-time ties are broken by board scan id. A change
happened at an unknown moment between two observations: `posting_version_stages` shows
each stage with `change_window_start` (last sight of the previous content) and
`change_window_end` (first sight of the new content). An older response can narrow a
window or add a stage between two existing ones; existing observations are never
rewritten.

**Safe writes.** Responses are fetched and validated before any write transaction. All
rows from one response are written in one transaction, so an interrupted write leaves
nothing behind. Postings are created with `INSERT ... ON CONFLICT` and locked in source id
order, so simultaneous processing of the same data serializes per posting without
deadlocks. A composite foreign key ties the current version to the current observation.
`posting_history_problems` (and `jobmarket check-history`) lists anything the constraints
cannot express, such as a current pointer that is not the newest observation.

**Replay.** `jobmarket replay` verifies the stored file's SHA-256, validates it again, and
stores it under its original board scan and collection time. It never changes the scan's
recorded status, retry incident, or alerts, and recomputes absence evidence only for the
jobs it touches. Every attempt is recorded in `replay_runs` with its outcome
(`processed`, `expired`, `file_missing`, `checksum_mismatch`, `no_response`, `unusable`,
`failed`) and counts.

**Evidence.** `python tests/history_scenarios.py` runs controlled fixtures (unchanged,
cosmetic, reworded, A-B-A, out-of-order replay, duplicate replay, a hypothetical rule
change, expired raw input) through the real collector and replay code in a temporary
schema and prints the SQL and results. Useful queries on your own data:

```sql
-- new / changed / unchanged per scan
SELECT board_scan_id, started_at, status, jobs_new, jobs_changed, jobs_unchanged
FROM board_scans ORDER BY started_at DESC LIMIT 25;

-- content stages and change windows of one posting
SELECT * FROM posting_version_stages WHERE posting_id = 42 ORDER BY stage_no;

-- replays and what they added
SELECT * FROM replay_runs ORDER BY replay_id DESC;
```

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
| `posting_versions`     | meaningfully distinct content stage of a posting, with its original content |
| `posting_version_comparisons` | comparison form and fingerprint of a version under one rules version |
| `posting_observations` | sighting of a posting in a board scan, with the source's stated dates |
| `posting_screenings`   | current screening decision for every valid posting seen |
| `rejected_records`     | record from a usable response that failed validation |
| `collection_incidents` | run of unsuccessful scans of a board, with retry state |
| `notifications`        | queued alert or recovery message (the outbox) |
| `posting_duplicate_flags` | possible duplicate postings, for review |
| `replay_runs`          | attempt to reprocess a saved raw response |

Views: `board_status` (one row per board for status displays), `retained_jobs`
(retained postings with their current content), `posting_version_stages` (content stages
in collection order with change windows), and `posting_history_problems` (integrity
checks; expected empty).

Design rules enforced by the schema:

- The source's publication time, our first discovery time, our last observation time,
  and content change times are separate columns. A missing source date is stored as
  `NULL`, never guessed.
- Only a `complete` board scan records a definite posting count; `partial` and `failed`
  scans are stored explicitly and never imply a job closed.
- Natural identities are unique (one posting per board and source ID, one observation per
  posting per scan, one rejected record per response record), so loading the same data
  twice cannot create duplicates.
- A posting's current version must be the version of its current observation.
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

`python tests/history_scenarios.py` replays the history scenarios with printed SQL
evidence (it also runs as part of `pytest`).

Database tests create a temporary schema, work only inside it, and drop it afterwards;
they do not modify existing tables in the configured database. The role needs permission
to create schemas in that database (the database owner has it).

## Continuous integration

`.github/workflows/ci.yml` runs on pushes and pull requests to `main`: it starts a
PostgreSQL 17 service container, installs the package, runs `ruff` and `pytest` with
`JOBMARKET_REQUIRE_DB=1`, runs `jobmarket check-db`, applies the migrations twice to
confirm the second run changes nothing, loads the board registry, runs the history
scenarios, and runs the history integrity checks. Collection tests use a scripted HTTP
transport; CI never contacts Greenhouse.
