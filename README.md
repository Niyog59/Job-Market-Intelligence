# Job Market Intelligence

Job discovery for data analyst, data engineer, and backend roles, built from permitted
public listings on official company career pages and applicant tracking systems.

## Status

Early development, focused on Dallas-Fort Worth and Greater Houston. What exists today:

- Registries of researched job boards on two applicant tracking systems:
  `config/greenhouse_boards.toml` (57 Greenhouse boards, 21 enabled) and
  `config/lever_boards.toml` (43 Lever boards, 30 enabled, plus one held). Only boards with
  a verified official employer connection and local hiring evidence are enabled; the rest
  stay disabled with the reason recorded. The registries come from a discovery pass and
  do not cover the whole market.
- One collector for both sources: a small connector per provider (HTTP, pagination,
  parsing) feeding shared validation, storage, history, retries, and status. It keeps
  complete, partial, and failed results apart per employer.
- Rule-based screening that keeps relevant data, analytics, BI, software, data science,
  and ML roles located in DFW/Houston or remote and open to Texas residents.
- Retry incidents (three retries over about three hours), an alert/recovery outbox, and
  four-day retention of raw responses.
- Incremental history: a new content version only for meaningful changes, versioned
  normalization rules, current state ordered by collection time, and safe replay of
  saved responses.
- Availability verification: postings move between observed, possibly unavailable,
  assumed inactive, and closed only on trustworthy evidence, with board holds that stop a
  suspicious mass disappearance from hiding jobs. Hidden jobs are never deleted.
- Requirement extraction with Gemini (free tier): a persistent queue sends one request per
  new or changed listing, Python validates the JSON answer and checks every quoted piece
  of evidence against the posting, and results are stored with their model and rule
  versions. Collection never waits for it. Rule-based screening still decides which jobs
  are retained; disagreements with the AI are listed for review.
- Content history for every listing, retained or not, and a display-safe listing form
  (sanitized description, original links, freshness, availability) for the job list and
  detail pages to come.
- Duplicate handling: records are grouped only on strong evidence (employer requisition
  ID, identical job-specific application URL); weaker similarity is flagged for review.
  Every source record keeps its own history and availability.
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
- **Measured extraction accuracy.** Extraction is checked for shape and quoted evidence,
  not for correctness. A manually reviewed evaluation set is being prepared; until it is
  reviewed, no accuracy figure exists.

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
| `jobmarket jobs [--all-states] [--board TOKEN] [--early-career] [--review] [--limit N]` | Active retained jobs (observed or possibly unavailable), newest discoveries first |
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
| `jobmarket extract run [--limit N]` | Send queued listings to the model, one at a time, within the configured limits |
| `jobmarket extract show POSTING_ID [--evidence]` | The current extraction of a listing, with warnings and quotes |
| `jobmarket extract reprocess --reason R [--limit N] [--retained-only]` | Explicitly queue current content under the active configuration |
| `jobmarket extract renormalize` | Re-apply skill and location clean-up to stored answers (no model calls) |
| `jobmarket extract disagreements [--limit N]` | Listings where the AI and the screening rules disagree |
| `jobmarket listing POSTING_ID [--html]` | One listing as a detail page would show it: safe description, links, freshness |
| `jobmarket refingerprint` | Evaluate stored versions under the current normalization rules; report rule effects |
| `jobmarket check-history` | Run the history integrity checks (exit `1` if any fail) |
| `jobmarket sources check-mapping [--all] [--source S] [--board TOKEN]` | Look for each board on its employer's official careers page; store the evidence |
| `jobmarket check-pages [--per-board N] [--source S] [--board TOKEN]` | GET a sample of retained postings' job and application pages; store the results |

Exit codes: `0` success, `1` failure (database, migration, a collection where every
board failed, or a replay that failed or found a missing or altered file), `2`
configuration or registry error, `3` another collection or replay is running.

## How collection works

```
config/greenhouse_boards.toml  \
config/lever_boards.toml        --sources sync--> employers, source_boards
                                                        |
jobmarket collect / retry-due                           v
  fetch (worker threads, one lane per provider, requests paced per provider)
    Greenhouse  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    Lever       GET https://api.lever.co/v0/postings/{token}?mode=json&skip=N&limit=100
                (repeated until a short page; api.eu.lever.co for EU-instance boards)
  then, per board in a fixed order, in the main thread:
    -> every page saved to data/raw/<source>/<token>/... (one raw_responses row per page:
       URL, status, sha256, size)
    -> the board's connector validates the pages:
         complete | partial (usable, but something is missing or wrong) | failed
    -> for each valid job: source_postings (identity, first discovered, last observed)
       and posting_screenings (relevance, Texas eligibility, seniority, sponsorship)
    -> every valid job: posting_observations (one per scan, pointing at its page) and
       posting_versions (a new row only for a meaningful change; see "Posting history")
    -> collection_incidents: retry state; notifications: alert/recovery outbox
```

### Sources

A connector (`jobmarket.connectors`) knows one provider: how to request a board and how to
turn the saved pages into validated records. Everything after that is shared, so both
sources get the same history, completeness, retry, and status guarantees.

- **Greenhouse** (`jobmarket.greenhouse`): one request per board. Complete means every
  record is valid and the count equals the API's `meta.total`. The single documented API
  host serves EU-hosted boards too; the region only changes human-facing links.
- **Lever** (`jobmarket.lever`): the API pages with `skip` and `limit` and reports no
  total. A scan is complete only when it reaches a page shorter than the limit (an empty
  page after an exactly full one counts). A failed or missing later page, a page that
  repeats earlier postings, a page larger than requested, or the 50-page cap make the scan
  partial: what arrived is kept, and nothing unseen is treated as gone. Each board's
  instance (`global` or `eu`) comes from the employer's official job link. `hostedUrl`,
  `applyUrl`, `allLocations`, `workplaceType`, `salaryRange`, and the other structured
  fields are stored. `createdAt` (not in Lever's documentation) is stored as the posting's
  creation time and never used as a publication date; Lever gives no update time.

Identity is source system + board + the source's job id, so the same title at two
employers or on two sources is never merged, and reprocessing the same record never
creates a second posting. If a record has no usable id but its job-specific URL carries one
(Greenhouse `/jobs/<id>` or `gh_jid=<id>`, Lever `jobs.lever.co/<site>/<id>`), that id is
used and the posting's `identity_basis` is `job_url` (shown as "identity from job URL" for
review); a later record with the id confirms it. A record with neither is kept in
`rejected_records` with its title and URL as evidence and makes the scan partial. Matching
different records that may be the same opening is separate (see "Duplicates").

Requests use a descriptive User-Agent, a 30-second timeout, at most
`JOBMARKET_CONCURRENCY` boards per provider at a time, and at least
`JOBMARKET_REQUEST_INTERVAL` seconds between requests to the same provider. No numeric
request quota is assumed for either API (Lever's documented limit applies to submitting
applications, which this project never does).

- Failed and partial scans never change existing records or advance absences; whether a
  missing job is hidden is decided by the availability rules below.
- Sponsorship is `not_stated` unless the posting says whether it is available; a
  question asking applicants about sponsorship is recorded as `asked_only`.

### Evidence

Three kinds of evidence are kept apart, because none of them proves the others:

- **Official mapping** (`board_mapping_checks`, `jobmarket sources check-mapping`): whether
  the employer's own careers page references the exact board (a job-board link, a postings
  API call, or an embed naming it). `refused` means the site declined automated requests;
  this is recorded and not worked around. `not_found` can mean the page loads jobs with
  scripts. Checks never change a board's verification status; the registry records that
  decision and its evidence.
- **Published listing** (`posting_observations`, `last_observed_at`): the job was in the
  source's listing at that collection time.
- **Page availability** (`posting_page_checks`, `jobmarket check-pages`): a sampled GET of
  the posting page and, for Lever, the application page. Nothing is ever submitted. A page
  that says applications are closed is recorded as `closed_notice` and closes the posting
  (see "Availability"). A page that otherwise loads shows only that it was published then; it does not prove funded headcount or
  active interviewing. `refused`, `rate_limited`, `server_error`, and `unreachable` are
  access or collection problems and never mean a job closed.

## Availability

Each source posting has one state, changed only on trustworthy evidence and never deleted:

| State | Meaning | In the active feed |
|---|---|---|
| `observed` | found in a valid source response | yes |
| `possibly_unavailable` | missing from one or two qualifying scans | yes, with a warning |
| `assumed_inactive` | missing from a third qualifying scan | no (`jobs --all-states` shows it) |
| `closed` | official evidence that applications are closed | no |

**Qualifying scans.** An absence counts only after a complete, fully processed scan of the
same board read under the same scope (source, API host, and request settings) as the
board's comparison reference, with no board hold and no mass disappearance. Failed and
partial scans pause counting: they neither advance nor reset it. Each scan is judged once
(`absence_checks`), so replaying a saved response never counts again; replays do not judge
scans. The first complete scan of a board becomes its reference without counting
anything. A scan under a different scope does not count either and becomes the new
reference, so counting resumes from the next scan.

**Reappearance.** A posting listed again by any usable scan (complete or partial) returns
to `observed` and its count resets; its first-discovery time and history are kept. A closed
posting listed again stays closed and the reappearance is recorded for review.

**Mass-disappearance protection.** Each complete scan is compared with the job IDs of the
board's reference scan, not just with its total. If every reference job is missing (even
on a small board), or at least 50% and at least 10 of them are, the board is put on hold
(`board_holds`): absences are not counted, while new jobs, reappearances, and explicit
closures are still recorded. A repeated suspicious result keeps the hold. A person
releases it after review:

```bash
jobmarket availability holds
jobmarket availability release 7 --reason collection-issue-resolved \
    --evidence "board token was mistyped in the registry; fixed and rescanned"
jobmarket availability release 8 --reason source-removals-verified \
    --evidence "employer careers page now lists 2 openings"
```

`collection-issue-resolved` keeps the board's last trusted reference.
`source-removals-verified` makes the latest complete scan the reference, so the removed
jobs are compared normally from then on. In both cases counting restarts with the next
qualifying scan, and scans taken during the hold are never counted. `jobmarket status`
shows active holds and each board's last trusted check.

**Closed.** `jobmarket availability close` records the official evidence (URL, the text,
and a reason) and hides the posting at once; it works during a hold. `jobmarket
check-pages` also closes a posting when the posting's own page (the final URL still names
the posting, so not a board front page or an error page) says in visible text that
applications are closed, for example "no longer accepting applications" or "this position
has been filled"; the page check and the matched sentence are stored as the evidence. A missing posting, a
404, or any request error is never treated as a closure or as evidence that someone was
hired. Every transition is in `posting_availability_events` with its reason, scan, and
evidence; `jobmarket availability show` prints them.

When availability tracking was introduced, every existing posting started as `observed`
with no qualifying absences; earlier Module 3 counts remain in `complete_scan_absences`.

## Duplicates

Repeated ingestion and duplicate matching are different problems. Reprocessing the same
source record can never create a second posting (identity is unique per board and source
id). Duplicate matching compares *different* source records, often on different boards or
sources, whose ids are not comparable. It runs after every collection, in one transaction,
and is safe to repeat. Rules are versioned (`duplicates.RULE_VERSION`), and every group and
flag stores its rule, version, and evidence. Confidence is a category, not a probability.

**Grouped automatically (strong evidence)** in `duplicate_groups`:

- `requisition_id`: the same employer and the same employer-issued requisition ID
  (compared case- and space-insensitively) with the same normalized title. A requisition
  used for different titles is flagged as `requisition_id_conflict` instead.
- `application_url`: the same job-specific application URL (scheme and host case, trailing
  slashes, and tracking parameters such as `utm_*` and `gh_src` ignored). A URL counts only
  if it names the posting's id or carries an id-like value; a shared careers front page is
  not evidence.

**Flagged for review only** in `posting_duplicate_flags` (`title_location_description`):
the same employer, the same normalized title (a trailing " - City, ST" is ignored), at least
one shared location (every location of a multi-location posting counts), and the same
description after normalization. Missing locations or descriptions never count as a match.
Publication and discovery dates are stored as evidence only; discovery date is not
publication date, and Lever gives no publication date. Conflicting evidence, such as two
different requisition IDs, is recorded on the flag. Similar titles in different places or
with different text are not flagged.

Every source record keeps its own identity, history, and availability: closing one record
never closes its counterparts. Review is reversible and remembered:

```bash
jobmarket duplicates list
jobmarket duplicates dismiss 12 --reason "separate teams, different hiring managers"
jobmarket duplicates confirm 13 --reason "same opening posted on both boards"
jobmarket duplicates dissolve 4 --reason "employer reuses requisition numbers per city"
```

A dismissed flag is not raised again, and a dissolved group is not recreated by its rule.
The Module 3 flags (same title and description, no location check) are kept with status
`superseded`; the current rule reopens any pair it also matches.

Duplicate matching covers postings that are or have been retained (the Module 6 scope),
even though every listing now has content history.

## Requirement extraction

A language model (Gemini, through the official `google-genai` SDK) reads each listing and
returns its requirements as JSON. Python decides what is sent, checks every answer, tidies
names, stores everything with its versions, and controls retries and quota. Availability,
duplicates, and retention stay rule-based.

**What is extracted** (one request per listing version, all fields together): role
categories (several may apply; titles are kept as published), seniority, employment type;
skills as required, preferred, mentioned, or ambiguous, with AND/OR/"at least N"
relationships kept as such ("Python or Java" keeps both names and its wording); overall and
skill-specific experience with ranges, education, and education/experience alternatives;
explicit graduation windows; sponsorship (supported, not supported, not stated, ambiguous),
kept apart from work authorization, citizenship, and clearance; and locations (original
text plus city, state, country), work arrangement, explicit remote restrictions, and
on-site frequency. "Not stated" is never turned into "no", "remote" never means
worldwide, and no location or coordinate is invented.

**Input and safety.** The model receives one document per version: title, location fields,
Lever workplace and commitment fields, and the description as display-safe text (headings
and lists kept). The posting is wrapped in `<posting>` tags and the instructions say its
contents are data, never instructions. Descriptions over 20,000 characters and empty ones
are skipped (`too_long`, `no_description`) and stay listed.

**Checks.** The API enforces the JSON schema; Python checks it again (types, allowed
values, required fields, bounds). A malformed answer is retried and never stored. Each
quote is then searched for in the exact document sent (case, spacing, and typographic
differences ignored), and its character offsets are stored. A quote that is not found, a
claim without a quote, a relationship naming an unlisted skill, or an inconsistent year
range is a warning: the answer is kept and marked `valid_with_warnings`. These checks show
an answer is well formed and quoted from the posting, not that it is correct.

**Clean-up.** Skill names are merged only through an exact alias table (`Postgres` becomes
`PostgreSQL`; `Java` and `JavaScript` stay apart; unknown names are kept as written).
States become two-letter codes, common country names one spelling, and Texas cities are
placed in the DFW or Houston area with the screening city lists.

**Queue.** `jobmarket collect` queues new content (it never calls the model);
`jobmarket extract run` processes the queue in priority order: new or edited retained
listings, retained backlog, new or edited other listings, other backlog. Requests are sent
one at a time, spaced to `JOBMARKET_AI_RPM` (and `JOBMARKET_AI_TPM` if set). A failed
request is retried three times after 10, 30, and 90 seconds, or longer if Google says so;
a job that still fails is retried in the next run, five runs at most, then waits for
`extract reprocess`. A daily-quota error pauses extraction until midnight Pacific time.
Runs stop at 90% of `JOBMARKET_AI_DAILY_LIMIT`. An interrupted run is recovered by the next
one. Collection is unaffected by any of this.

**Caching and versions.** Results are keyed by the posting version and the configuration
(model, thinking level, prompt version, schema version). Unchanged content is never sent
again; identical text in another version reuses the stored answer without a request; a
result is never attached to newer content (the `current_extractions` view joins on the
current version only), and older results are kept. A new model, prompt, or schema applies
to new content only: `extract reprocess` queues older content explicitly. Python clean-up
changes are applied with `extract renormalize`, without model calls.

**Free tier only.** Nothing enables billing. The code cannot tell whether a Google project
is billed, so the free tier is confirmed in Google AI Studio; limits are configured from
the same page. `extract status` reports requests and tokens per Pacific day and what they
would cost at paid rates, for planning only. Free-tier requests may be used by Google to
improve its products; job descriptions are public listings.

Tables: `extraction_inputs` (the exact text sent), `extraction_jobs` (queue),
`extraction_runs`, `extraction_attempts` (every request, its outcome and token usage),
`extraction_results` (answer, normalized form, warnings, evidence offsets, versions),
`extraction_pauses`. Views: `current_extractions`, `extraction_usage_daily`,
`screening_disagreements`.

## Listing data for display

`jobmarket.listings.listing_detail()` returns what a detail page needs for one listing: the
original title and locations, the source's posting page and separate application page,
availability state, first discovery, source publication and creation dates, when the
current content was first seen, the board's last trusted check, the description, and the
extraction status with a display-ready summary (`summary_lines()`) when there is one.

Descriptions are untrusted employer HTML and are never rendered as collected.
`safe_description_html()` rebuilds them from an allowlist: headings, paragraphs, line
breaks, lists, bold, italics, block quotes, and absolute `https` links (opened in a new tab
with `rel="nofollow noopener noreferrer"`). Every other attribute and link type is dropped;
scripts, styles, images, frames, and forms are removed with their content; text is escaped.
The text is what the source API supplied, which can differ from the employer's page, so
each description carries a note on where it came from. A description that was too long to
store is reported as `omitted_too_long`, and an empty one as `empty`, never as complete.
`jobmarket listing POSTING_ID` prints the same data in a terminal.

### Failures, retries, and alerts

- Each board's scan is `complete`, `partial`, or `failed`. One board's failure never
  stops the others, and an unexpected error while processing one board is recorded on
  that board's scan.
- A failed or partial scan opens an incident with retries due about 1, 2, and 3 hours
  after the first failure (later if the server sends a longer `Retry-After`). Retry state
  is stored in the database, so it survives restarts. `retry-due` scans only boards whose
  retry is due; successful boards are not rerun. A collection run while no retry is due
  neither uses nor resets the retry budget. Late retries never come in a burst: a retry
  run more than 5 minutes after its slot (there is no scheduler yet) moves the next one to
  a full hour after it, so the three retries always span about three hours of attempts.
- Within one attempt, a `429` or `503` with a `Retry-After` of 30 seconds or less is
  waited out once per board scan, however many pages the scan has; there are no other
  hidden HTTP retries, so pagination cannot multiply the three-retry budget.
- A PostgreSQL advisory lock lets only one collection, retry run, or replay work at a time,
  so two commands can never claim the same retries; scans left `running` by a stopped
  process are marked failed on the next run.
- `jobmarket status` (and the `board_status` view) shows, per board, the source, the last
  attempt and its kind, pages fetched, the last complete scan, and any open incident with
  retries used, attempts, and whether a retry is due. It reflects a failure as soon as the
  scan finishes. When the retries
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

**Observations.** Every scan of a valid job, retained or not, adds one observation (until
2026-10-27 only retained jobs were tracked; earlier excluded jobs get content history from
the next scan that lists them). Observations are unique per (board scan, posting), so processing the same
saved response again adds nothing.

**Meaningful changes.** `jobmarket.normalize` builds a comparison form of each content:
title, description wording, locations (which carry remote restrictions), published
compensation, and employment type. HTML markup, whitespace, invisible characters,
typographic quote/dash variants, and timestamps are ignored; case, numbers, and wording
are kept. A new version is written only when the comparison form differs from the
chronologically neighbouring observation. Unchanged content reuses the existing version,
and returning to earlier content (A, B, A) is a new version, so the sequence is kept.

**Long descriptions.** A description longer than 20,000 characters (the longest collected
so far is about 15,800) is not stored: the version keeps its fingerprints, title,
location, links, and `description_length`, with `description_omitted = 'too_long'`. Edits
are still detected by fingerprint, including after a rule change (the new content is
compared under the rules the old content was recorded with). Readers get the posting link
instead of the text.
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
| `raw_responses`        | preserved response page received during a board scan (bytes stored outside the database) |
| `source_postings`      | job as one board identifies it, with first discovery and last observation times |
| `posting_versions`     | meaningfully distinct content stage of a posting, with its original content |
| `posting_version_comparisons` | comparison form and fingerprint of a version under one rules version |
| `posting_observations` | sighting of a posting in a board scan, with the source's stated dates |
| `posting_screenings`   | current screening decision for every valid posting seen |
| `rejected_records`     | record from a usable response that failed validation |
| `collection_incidents` | run of unsuccessful scans of a board, with retry state |
| `notifications`        | queued alert or recovery message (the outbox) |
| `posting_duplicate_flags` | possible duplicate pair, with rule, evidence, and review status |
| `duplicate_groups`     | group of source records judged the same opening, with rule and evidence |
| `duplicate_group_members` | source record in a duplicate group |
| `replay_runs`          | attempt to reprocess a saved raw response |
| `board_mapping_checks` | check of a board's official careers page for a reference to that board |
| `posting_availability_events` | availability state change of a posting, with reason, scan, and evidence |
| `board_scan_listings`  | posting listed by a complete scan (the comparison reference for later scans) |
| `absence_checks`       | judgement of one scan for absence counting (qualifying, held, and so on) |
| `board_holds`          | board-level hold opened by a mass disappearance, with its review and release |
| `posting_page_checks`  | sampled check of a posting's job or application page |

Views: `board_status` (one row per board for status displays), `listings` (every listing
with stored content, retained or not, with links, freshness, and availability),
`retained_jobs`
(retained postings with their current content and availability), `active_jobs` (the
default feed: observed or possibly unavailable), `posting_version_stages` (content stages
in collection order with change windows), and `posting_history_problems` (integrity
checks; expected empty).

Design rules enforced by the schema:

- The source's publication time, its creation time (Lever), our first discovery time, our
  last observation time, and content change times are separate columns. A missing source
  date is stored as `NULL`, never guessed.
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

Extraction tests never call Gemini: a scripted fake model returns prepared answers and
errors (malformed JSON, schema violations, per-minute and daily quota errors, server and
network failures), so CI needs no API key.

Database tests create a temporary schema, work only inside it, and drop it afterwards;
they do not modify existing tables in the configured database. The role needs permission
to create schemas in that database (the database owner has it).

## Continuous integration

`.github/workflows/ci.yml` runs on pushes and pull requests to `main`: it starts a
PostgreSQL 17 service container, installs the package, runs `ruff` and `pytest` with
`JOBMARKET_REQUIRE_DB=1`, runs `jobmarket check-db`, applies the migrations twice to
confirm the second run changes nothing, loads the board registry, runs the history
scenarios, and runs the history integrity checks. Collection tests use a scripted HTTP
transport; CI never contacts Greenhouse or Lever.
