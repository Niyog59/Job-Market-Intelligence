# Job Market Intelligence

Job discovery for data analyst, data engineer, and backend roles, built from permitted
public listings on official company career pages and applicant tracking systems.

## Status

Early development. What exists today:

- Environment-based configuration with validation (`DB_*` variables).
- `jobmarket check-db`: connects to PostgreSQL and runs `SELECT 1`.
- `jobmarket migrate`: a forward-only SQL migration runner and the core collection
  schema (employers, job boards, scan runs, raw responses, postings, content versions,
  observations).
- Unit tests, database and constraint tests, and a GitHub Actions workflow that runs
  them against PostgreSQL 17.

No collectors exist yet, so no job data is gathered.

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

Then check the connection and create the schema:

```bash
jobmarket check-db
jobmarket migrate
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

`.env` is ignored by Git. Never commit real credentials.

## Usage

| Command                     | What it does                                              |
|-----------------------------|-----------------------------------------------------------|
| `jobmarket check-db`        | Connect and run `SELECT 1`; print database, user, version |
| `jobmarket migrate`         | Apply pending migrations                                  |
| `jobmarket migrate --status`| List migrations and when each was applied                 |

Exit codes: `0` success, `1` database or migration failure, `2` configuration error.

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
`JOBMARKET_REQUIRE_DB=1`, runs `jobmarket check-db`, and applies the migrations twice to
confirm the second run changes nothing.
