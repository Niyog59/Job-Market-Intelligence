# Setup, commands, and operations

Local setup and configuration, every command, scheduling, alerts, backups, deployment, tests, and CI. Back to the [README](../README.md).

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
| `JOBMARKET_REGISTRY`         | `config/greenhouse_boards.toml,config/lever_boards.toml` | comma-separated board registry files |
| `JOBMARKET_RAW_DIR`          | `data/raw`                      | where raw responses are stored (ignored by Git) |
| `JOBMARKET_REQUEST_INTERVAL` | `1.0`                           | minimum seconds between two requests to the same provider |
| `JOBMARKET_CONCURRENCY`      | `1`                             | boards of one provider fetched at the same time (1 to 4) |
| `JOBMARKET_HOLD_ALL_MISSING` | `true`                          | hold a board when every reference job is missing |
| `JOBMARKET_HOLD_MIN_SHARE`, `JOBMARKET_HOLD_MIN_MISSING` | `0.5`, `10`        | hold a board when at least this share and this many reference jobs are missing |
| `GEMINI_API_KEY`             | unset                           | Gemini API key for requirement extraction; secret, never committed or printed |
| `JOBMARKET_AI_MODEL`, `JOBMARKET_AI_THINKING` | `gemini-3.5-flash-lite`, `minimal` | extraction model and thinking level |
| `JOBMARKET_AI_RPM`, `JOBMARKET_AI_DAILY_LIMIT` | unset (required to run extraction) | the account's free-tier requests per minute and per day for that model, as shown in Google AI Studio |
| `JOBMARKET_AI_TPM`           | unset                           | the account's tokens-per-minute limit, if any |
| `JOBMARKET_AI_TIMEOUT_SECONDS` | `120`                         | timeout of one extraction request |
| `JOBMARKET_EVAL_DIR`         | `data/eval`                     | evaluation case files (ignored by Git) |
| `ANALYTICS_DB_USER`, `ANALYTICS_DB_PASSWORD` | `jobmarket_analytics`, empty | the login dbt builds with (it reads ingestion tables and writes only its own analytics schemas); the website uses it in read-only sessions |
| `PRIVATE_DB_USER`, `PRIVATE_DB_PASSWORD` | `jobmarket_private`, empty | the login for private pages; without a password, private pages are off and browsing still works |
| `WEB_COOKIE_SECURE` | `true` | `false` only for a browser that refuses Secure cookies on http://127.0.0.1 |
| `JOBMARKET_SMTP_HOST`, `JOBMARKET_SMTP_PORT` (587), `JOBMARKET_SMTP_USER`, `JOBMARKET_SMTP_PASSWORD`, `JOBMARKET_SMTP_STARTTLS` (`true`), `JOBMARKET_ALERT_FROM`, `JOBMARKET_ALERT_TO` | unset | alert delivery; without host, sender, and recipient, notifications stay in the outbox |

`.env` is ignored by Git. Never commit real credentials.

## Usage

| Command | What it does |
|---------|--------------|
| `jobmarket check-db` | Connect and run `SELECT 1`; print database, user, version |
| `jobmarket migrate [--status]` | Apply pending migrations, or list them |
| `jobmarket sources sync` | Load the board registries into the database (boards are never deleted) |
| `jobmarket sources list [--all]` | Show enabled boards, or all boards, leads, and holds |
| `jobmarket collect [--board [SOURCE:]TOKEN ...] [--scheduled]` | Scan enabled Greenhouse and Lever boards now |
| `jobmarket retry-due` | Run only the retries that are due now |
| `jobmarket status [--all]` | Per-board last result, last complete scan, retained jobs, incidents |
| `jobmarket jobs [--internships \| --all-levels] [--all-states] [--board TOKEN] [--early-career] [--review] [--limit N]` | Active retained jobs, newest discoveries first: by default only fresher-friendly ones (see [Early career](EXTRACTION.md#requirement-extraction)) |
| `jobmarket availability show POSTING_ID` | A posting's availability state and every transition with its reason and evidence |
| `jobmarket availability close POSTING_ID --reason R --evidence-url URL --evidence-text T` | Mark a posting closed on official evidence |
| `jobmarket availability holds [--all]` | Board holds against mass disappearances |
| `jobmarket duplicates list [--all]` | Duplicate groups and open possible duplicates |
| `jobmarket duplicates confirm FLAG_ID --reason R` / `dismiss FLAG_ID --reason R` | Review a possible duplicate |
| `jobmarket duplicates dissolve GROUP_ID --reason R` | Undo a duplicate group |
| `jobmarket availability release HOLD_ID --reason collection-issue-resolved\|source-removals-verified --evidence T` | Release a hold after review |
| `jobmarket notifications [--full] [--send]` | Show the alert outbox, or deliver it if SMTP is configured |
| `jobmarket cleanup-raw [--dry-run]` | Delete raw responses older than four days |
| `jobmarket replay --scan ID [--scan ID ...]` / `--available` | Reprocess saved raw responses under their original collection time |
| `jobmarket history POSTING_ID` / `--board TOKEN --job SOURCE_ID` | Content stages, change windows, and versions of one posting |
| `jobmarket extract status` | Extraction queue, today's requests and tokens, pause, safety stop, results |
| `jobmarket extract run [--limit N] [--skip-if-worker]` | Send queued listings to the model, one at a time, within the configured limits; with `--skip-if-worker`, do nothing (exit 3) while the worker runs |
| `jobmarket extract worker` | Extract continuously until stopped: the extraction worker service ([Extraction](EXTRACTION.md#extraction-worker)) |
| `jobmarket extract show POSTING_ID [--evidence]` | The current extraction of a listing, with warnings and quotes |
| `jobmarket extract reprocess --reason R [--limit N] [--retained-only]` | Explicitly queue current content under the active configuration |
| `jobmarket extract renormalize` | Re-apply skill and location clean-up to stored answers (no model calls) |
| `jobmarket extract disagreements [--limit N]` | Listings where the AI and the screening rules disagree |
| `jobmarket eval prepare --selection FILE` / `eval run --split S [--final]` / `eval report --split S [--final] [--cases]` | Evaluate extraction against labelled cases |
| `jobmarket analytics build [--select S]` / `test` / `docs` | Build and test the dbt analytics models; generate their documentation |
| `jobmarket analytics publish` | Build and test in shadow schemas, then replace the live analytics schemas in one step |
| `jobmarket analytics grant` / `check-access` | Give the analytics login read-only access; verify it |
| `jobmarket owner create --username U` / `reset-password` / `status` / `sign-out-all` | The single owner account (passwords at a hidden prompt) |
| `jobmarket private grant` / `check-access` | Give the private login the private schema only; verify which logins reach private data |
| `jobmarket backup create [--dir D] [--upload]` / `verify DIR` / `restore DIR --database NAME` | Back up the public and private schemas; check a backup; restore into an empty database |
| `jobmarket backup upload DIR` / `list` / `fetch STAMP` | Copy a backup to S3 and confirm it; list the backups in S3; download and verify one |
| `jobmarket backup prune --keep N` | Keep the newest N verified local backups; delete older ones only if confirmed in S3 |
| `jobmarket sync-raw` | Upload raw responses that are not in S3 yet |
| `jobmarket tableau sync [--dry-run]` | Copy the published dashboard tables to the Google Sheet behind Tableau Public |
| `jobmarket spark export [--dir D]` | Write three reporting models' inputs, and dbt's results over them, to Parquet from one database snapshot |
| `jobmarket spark parity [--dir D]` | Rebuild those models in PySpark and compare them with dbt's results row for row |
| `jobmarket spark benchmark [--runs N] [--scales 1,10,100]` | Time PostgreSQL, dbt, and Spark local mode on the same inputs; write `docs/spark/results/benchmark.json` |
| `jobmarket web [--host H] [--port P]` | Run the job-search website on this computer (default http://127.0.0.1:8000/) |
| `jobmarket listing POSTING_ID [--html]` | One listing as a detail page would show it: safe description, links, freshness |
| `jobmarket refingerprint` | Evaluate stored versions under the current normalization rules; report rule effects |
| `jobmarket check-history` | Run the history integrity checks (exit `1` if any fail) |
| `jobmarket sources check-mapping [--all] [--source S] [--board TOKEN]` | Look for each board on its employer's official careers page; store the evidence |
| `jobmarket check-pages [--per-board N] [--source S] [--board TOKEN]` | GET a sample of retained postings' job and application pages; store the results |

Exit codes: `0` success, `1` failure (database, migration, a collection where every
board failed, or a replay that failed or found a missing or altered file), `2`
configuration or registry error, `3` another collection or replay is running.

## Scheduling

Apache Airflow 3.3.2 runs the pipeline on the server (`deploy/airflow/dags/jobmarket.py`).
Every task runs an existing `jobmarket` command, so each step can also be run by hand.

| DAG | When (America/Chicago) | Tasks |
|---|---|---|
| `jobmarket_collection` | 12:00 and 21:00 | `collect`, then (21:00 only) `backup create --upload` and `backup prune --keep 3`, then `sync-raw`, `cleanup-raw --require-s3`, `analytics publish`, `tableau sync` (only after a successful publication; skipped until the Sheet is configured), (21:00 only) `summary send`, then `notifications --send` and (21:00 only) Airflow history cleanup (30 days) |
| `jobmarket_retries` | every 15 minutes | `retry-due`, then `notifications --send` |
| `jobmarket_extraction` | every hour at :30 | `extract run --limit 20 --skip-if-worker` (skipped while the extraction worker runs) |

- **Time zone:** the cron expressions are read in America/Chicago, so the local times hold
  across daylight saving changes.
- **After downtime:** with `catchup=False`, Airflow makes one run for the latest missed
  time, never one per missed time. Collection always scans the boards as they are now.
- **One task at a time** across all DAGs (`PARALLELISM=1`), one run at a time per DAG,
  and the commands' own advisory locks stop a manual run from overlapping a scheduled one
  (exit code 3, shown as skipped).
- **Retries never wait inside a task:** each failed board's next retry time is in the
  database, and the 15-minute check runs only those that are due. The three retries are
  planned about 1, 2, and 3 hours after a failure; a check up to 15 minutes late moves the
  next one a full hour after it, so they span about 3 to 3.75 hours.
- **A failure never stops later steps** (trigger rule `all_done`): reports, the backup,
  and the summary run even when collection failed. The backup runs right after the evening
  collection, whatever its result and before any AI work.
- **AI work is separate from Airflow when the worker runs:** the extraction worker (Compose
  service `extract-worker`, profile `worker`) extracts new listings as collection queues
  them, and the hourly batch skips itself. Without the worker, the hourly batch handles at
  most 20 listings. Either way the free limits apply, a quota pause shows in the summary,
  not as an alert, and without a Gemini key nothing is sent.
- **Time limits:** collection 45 minutes, retry check 30, AI batch 20, report publication
  20, backup and raw uploads 20, Sheet sync 10, summary and email 10.
- **No triggerer:** none of the tasks are deferrable, so Airflow's optional triggerer is
  not run.
- **Run history:** kept in its own `airflow` database (login `airflow`) and cleaned after
  30 days by the evening run; Airflow 3 blocks its database inside tasks, so that one
  command gets the address as `JOBMARKET_AIRFLOW_DB_URL`.
- **Administration stays private:** the UI listens on the server's loopback address only
  (RUNBOOK G2).

Tests: `deploy/airflow/tests` (run by the CI `airflow` job with Airflow installed) check
the local times across both 2026 daylight saving changes, the single catch-up run, the
evening/noon split, and whole runs with a stub `jobmarket` command; the CI `deploy` job runs
the real DAGs in the containers against a moto S3 server and checks the recorded steps, the
confirmed backup, the queued summary, and a restore of that backup.

## Pipeline steps, alerts, and the daily summary

Scheduled steps other than collection record each run in `pipeline_step_runs`:
`report_publish`, `backup`, `backup_prune`, `raw_sync`, `raw_cleanup`, and
`daily_summary`. A failed report publication or backup opens an incident (`ops_incidents`)
and queues one alert; later failures of the same step only update it; the next success
resolves it and queues one recovery notice. AI quota pauses never alert: they appear in the
daily summary. Every queued message has a unique `dedup_key` in the outbox, so re-runs and
restarts cannot queue it twice.

`jobmarket summary send` queues one summary per America/Chicago day (a second call the same
day queues nothing), delivers pending email, and requests `JOBMARKET_HEARTBEAT_URL` if set.
The summary covers the time since the previous one: collection runs, new kept jobs (initial
imports of newly added boards counted apart), content and availability changes, board
health and every open collection incident with its next retry, the AI queue and today's
requests (and any quota pause), the published report build and any failed rebuild,
the last good backup and whether S3 confirmed it, local backup copies, raw uploads, and
undelivered email. Problems are listed first, and the subject then ends with "(needs a
look)". `--print` also prints it. `jobmarket notifications --send-test` queues one test
message and sends pending email, to check delivery on a new server.

**Email delivery** uses SMTP (`jobmarket.notify`), which works on any server. The intended
setup is a Gmail account with 2-Step Verification and an app password (Google Account >
Security > App passwords): `JOBMARKET_SMTP_HOST=smtp.gmail.com`, `JOBMARKET_SMTP_PORT=587`
(STARTTLS; EC2 limits outbound port 25, not 587), `JOBMARKET_SMTP_USER` and
`JOBMARKET_ALERT_FROM` = that Gmail address, `JOBMARKET_SMTP_PASSWORD` = the app password,
`JOBMARKET_ALERT_TO` = the recipient. The app password can be revoked at any time without
changing the account password. Without these settings, messages stay in the outbox
(`jobmarket notifications --full`). Tests use a fake SMTP class and never send email.

## Backups and restore

`jobmarket backup create` runs `pg_dump` (custom format) as the owner login and writes a
directory named by its UTC time under the ignored `data/backups/`:

| File | Contents |
|---|---|
| `ingestion.dump` | the `public` schema: boards, scans, postings, history, extractions, `schema_migrations` |
| `private.dump` | the `private` schema: owner account (password hash only), sessions, profile, saved jobs, applications, visits |
| `manifest.json` | sizes and SHA-256 checksums, server and `pg_dump` versions, migrations applied |

The analytics schemas are not backed up; `jobmarket analytics build` recreates them. The
directory is written as `<stamp>.partial` and renamed when complete. Dumps are made without
owners or privileges, and database roles (with their passwords) are never in a backup, so a
backup restores onto a fresh server whose roles were created separately. The password
reaches `pg_dump` and `pg_restore` through `PGPASSWORD` in their environment, never on the
command line. The client tools must be PostgreSQL 17 (`pg_dump` refuses a newer server);
`JOBMARKET_PG_BIN` names their directory when they are not on the `PATH`.

**Restore** (`jobmarket backup restore DIR --database NAME`) refuses any database that
already contains a table, function, or schema other than an empty `public`, so it cannot
overwrite data. It verifies every checksum first, then restores each part in one
transaction (all or nothing), ingestion before private. Afterwards, with `DB_NAME` set to the
restored database:

```bash
jobmarket migrate                 # apply migrations newer than the backup, if any
jobmarket analytics grant         # the analytics login: read ingestion, own analytics schemas
jobmarket private grant           # private schema for the private login
jobmarket private check-access    # confirm who can reach private data
jobmarket analytics build         # recreate the reporting tables and serving views
```

The logins `jobmarket_analytics` and `jobmarket_private` must exist first (see [Analytics](ANALYTICS.md#analytics-dbt)
and [Private workspace](WEBSITE.md#private-workspace) for how they are created).

**S3.** With `pip install -e ".[aws]"` (boto3) and `JOBMARKET_S3_BUCKET` set, `backup create
--upload` (or `backup upload DIR`) copies a verified backup to a private bucket, and
`jobmarket sync-raw` copies raw responses before `cleanup-raw` removes them locally. Every
object is written with S3-managed encryption (SSE-S3). Keys are separated by purpose so
access and lifecycle rules can differ:

| Prefix | Contents |
|---|---|
| `raw/<source>/<board>/` | raw responses, same paths as under `data/raw/` |
| `backups/ingestion/<stamp>/` | `ingestion.dump` and the manifest |
| `backups/private/<stamp>/` | `private.dump` and the manifest |

Credentials come from the AWS SDK's default chain (on EC2, the instance role), so no access
keys are configured. Nothing in the code deletes S3 objects: retention is a bucket lifecycle
rule (30 days for backups and for raw responses). The instance role has no `s3:DeleteObject`,
but that does not make the backups immutable: the role can overwrite an object by writing
the same key (versioning is off), and an account administrator can delete objects. S3
Versioning or Object Lock would be needed for that guarantee; neither is set up. A dump's manifest is uploaded after the
dump, so `backup list` shows only finished uploads; `backup fetch STAMP` downloads both parts
and verifies them before a restore. After an upload, every object is checked in S3 (it
must exist with the local size) and only then is an `UPLOADED` file written into the
backup directory.

**Local copies.** `jobmarket backup prune --keep N` keeps the newest N verified backups
(the scheduled run uses 3) and deletes an older one only if its `UPLOADED` mark shows S3
confirmed it. Damaged backups are reported and never deleted; unfinished
`<stamp>.partial` directories are removed. So the newest verified copy always stays.

**Raw files.** `jobmarket cleanup-raw` deletes a raw response only when S3 has the same
file with the same size, whenever `JOBMARKET_S3_BUCKET` is set (`--require-s3` makes a
missing bucket an error instead of a plain local cleanup). Files not yet confirmed stay and
are retried by the next cleanup. Locally, without a bucket, cleanup works as before.

## Deployment

`deploy/` holds everything for one server (EC2, Amazon Linux 2023) and no secrets:

| File | Purpose |
|---|---|
| `Dockerfile` | one image for the website, the command-line jobs, and Airflow (Python 3.11, PostgreSQL 17 client tools, Airflow 3.3.2 in its own virtual environment) |
| `docker-compose.yml` | `db` (PostgreSQL 17, no published port, data in a volume), `web` (gunicorn, read-only filesystem, analytics and private logins only), `caddy` (ports 80 and 443, Let's Encrypt), Airflow (`airflow-scheduler`, `airflow-dag-processor`, `airflow-api` on 127.0.0.1 only; `airflow-init` for its tables), and `jobs` for running commands by hand |
| `airflow/dags/jobmarket.py` | the schedules (see Scheduling) |
| `airflow/tests/` | DAG tests run by the CI `airflow` job |
| `ci/` | the CI-only Compose layer (a moto S3 server) and the memory report script |
| `Caddyfile` | HTTPS for `SITE_DOMAIN`, HTTP redirected to HTTPS |
| `postgres-init/10-jobmarket.sh` | on the first start: the three logins and the database owned by the non-superuser owner login |
| `env.example` | the settings, with placeholders only; copied to `deploy/.env` on the server |
| `aws/` | IAM role policy (S3 prefixes only, no `s3:DeleteObject`), EC2 trust policy, bucket policy (HTTPS only), lifecycle rules (30 days) |
| `RUNBOOK.md` | ordered steps from an empty AWS account to the running site, updates, rollback, recovery |
| `SIZING.md` | memory measurements (including Airflow), estimates, their limits, and candidate instance sizes with costs |

All containers restart unless stopped, and Docker starts at boot. Visitors reach only
Caddy, and the database is not exposed. Administration uses AWS Systems Manager Session
Manager. The runbook keeps SSH as a fallback until Session Manager has worked, including
after a reboot, and only then removes the rule; on the production server that happened on
2026-11-04. Public pages read through
the analytics login in read-only sessions; nothing a visitor does starts a collection, an AI call, or a change to
job data (signing in records sign-in attempts and sessions in the private schema).

## Tests

```bash
pip install -e ".[dev,analytics,web,aws]"
ruff check .
ruff format --check .
pytest
```

Analytics and website tests are skipped when dbt or Flask is not installed. The Spark tests
(`tests/test_spark_*.py`) need the `spark` extra and Java, and are skipped without them
unless `JOBMARKET_REQUIRE_SPARK=1` (CI's `spark` job). Tests marked `db` need a reachable database configured through the `DB_*` variables.
Without one they are skipped locally. Set `JOBMARKET_REQUIRE_DB=1` to make a missing
database a failure instead (CI does this).

`python tests/history_scenarios.py` replays the history scenarios with printed SQL
evidence (it also runs as part of `pytest`).

Search and website tests run a synthetic job history (clearly test data, `tests/search_world.py`)
through the real collector, availability rules, duplicate rules, and extraction queue, build
the serving views, and render pages through Flask's test client.

Extraction tests never call Gemini: a scripted fake model returns prepared answers and
errors (malformed JSON, schema violations, per-minute and daily quota errors, server and
network failures), so CI needs no API key.

Database tests create a temporary schema, work only inside it, and drop it afterwards;
they do not modify existing tables in the configured database. The role needs permission
to create schemas in that database (the database owner has it).

S3 tests use moto's in-memory S3 (no network, no AWS account). The backup round trip
(`pg_dump`, then `pg_restore` into a second database) creates and drops two temporary
databases, so it runs only when the login may create databases and the PostgreSQL 17 client
tools are available; otherwise it is skipped, unless `JOBMARKET_REQUIRE_PG_TOOLS=1` (CI).

## Continuous integration

`.github/workflows/ci.yml` runs on pushes and pull requests to `main`: it starts a
PostgreSQL 17 service container, installs the package, runs `ruff` and `pytest` with
`JOBMARKET_REQUIRE_DB=1`, runs `jobmarket check-db`, applies the migrations twice to
confirm the second run changes nothing, loads the board registry, runs the history
scenarios, runs the history integrity checks, and builds the analytics models with their
data tests. It installs the PostgreSQL 17 client tools so the backup round trip runs. Collection tests use a scripted HTTP
transport; CI never contacts Greenhouse or Lever.

The `spark` job installs Java 21 and the `spark` extra and runs the Spark parity and benchmark tests against
a PostgreSQL 17 service. It runs beside the `test` job, so the main tests do not wait for
the PySpark download.

The `airflow` job installs Airflow 3.3.2 with its official constraints and runs the DAG
tests in `deploy/airflow/tests`.

The `deploy` job checks the deployment in `deploy/`:
- It builds the Docker image and starts the Compose stack with throwaway passwords plus a
  moto S3 server, then runs the first-start commands.
- It requests pages over HTTPS from Caddy (with its local certificate for `localhost`),
  starts Airflow, and runs the collection, retry, and extraction DAGs in the containers.
- It checks the recorded pipeline steps, the S3-confirmed backup, and the queued summary,
  restores that backup into an empty database, and checks that only Caddy publishes public
  ports (the Airflow UI only on 127.0.0.1).
- It records container memory with `docker stats` in the job summary.
- It never contacts AWS, a job board (none are loaded), Gemini, or an email server.
