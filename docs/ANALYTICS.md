# Analytics with dbt, and the dashboard data

The dbt models, their counting rules, the serving views behind job search, the PySpark comparison, and the Google Sheet behind the Tableau dashboards. Back to the [README](../README.md).

## Analytics (dbt)

SQL models in `analytics/` (dbt Core with the PostgreSQL adapter) turn the stored history
into reporting tables for Tableau and live views for the job-search website. Python collects, Gemini
interprets, and dbt only transforms what is already stored: it never calls a source or the
model, and it never writes ingestion tables.

**Layers and schemas.**

| Schema | Built as | Holds |
|---|---|---|
| `analytics_staging` | views | one model per ingestion table: consistent names and types, no new meaning |
| `analytics_intermediate` | views and tables | shared rules (cohorts, grouping, as-of history) |
| `analytics_reporting` | tables | the reporting models |
| `analytics_serving` | views | live, search-ready listings and board freshness for the website |

Every model documents its grain (what one row is) in its `.yml` file.

**Rebuild safety.** A build drops and recreates only tables and views in the four
analytics schemas. The project refuses to build if an analytics schema would be the
ingestion schema, and on a normal install it runs as a separate login,
`jobmarket_analytics`, that can only read ingestion tables.

**Setup (once).**

1. Install the extra: `pip install -e ".[analytics]"`.
2. As a PostgreSQL superuser, create the login:
   `CREATE ROLE jobmarket_analytics LOGIN PASSWORD '...';`
3. Put the password in `.env` as `ANALYTICS_DB_PASSWORD` (optional `ANALYTICS_DB_USER`).
4. As the database owner: `jobmarket analytics grant`, then
   `jobmarket analytics check-access` (every write right must say "no").

**Build.** `jobmarket analytics build` runs `dbt build` (models and all data tests);
`--select reporting` limits it. `jobmarket analytics test` runs only the tests, and
`jobmarket analytics docs` generates dbt documentation with the dependency graph (`cd
analytics && dbt docs serve --profiles-dir .`). Running dbt directly works too:
`cd analytics && dbt build --profiles-dir .`. `analytics build` rebuilds the live schemas in
place, so during a build (or after a failed one) job search and reports can be missing:
use it for development.

**Publish (servers and scheduled runs).** `jobmarket analytics publish` builds every model
into shadow schemas (`next__analytics_*`) and runs all data tests there, then checks that
each schema exists, `rpt_build_info` has exactly one row, and the job-search views can be
read. Only then does it rename the live schemas away and the shadow schemas into place, in
one transaction. Visitors therefore see either the previous build or the new one, never a
mix. The previous build is dropped afterwards; a reader still using it is not waited on
for more than 30 seconds, and leftovers are removed by the next run. A failed build or
check publishes nothing: the website keeps the last good reports with their build time,
the Reports page says the latest rebuild failed, newly collected data stays in the
ingestion tables, and the failure is recorded (one alert, one recovery notice later; see
[Pipeline steps, alerts, and the daily summary](OPERATIONS.md#pipeline-steps-alerts-and-the-daily-summary)). Before the first publish, the Reports page says "Reports not ready
yet". Only one publish runs at a time.

### Counting rules

- **Grains.** A *source record* is one posting on one board. An *opportunity* is a duplicate
  group with strong or reviewed evidence counted once, or an ungrouped posting; possible
  duplicates (flags) stay separate. Reports give both counts.
- **Discoveries versus initial imports.** A board's first complete collection is its
  baseline. Postings first seen at or before it (including in an earlier partial scan) are
  its *initial import*: searchable and in inventory, never counted as hiring. Later first
  sightings are *discoveries*: first observed by this pipeline, not necessarily newly
  published (the source's own dates are kept separately). `boards_baselined` and
  `boards_with_baseline` sit next to every trend so new boards are not read as market
  growth.
- **Updates** are meaningful content changes (new versions), including employer-wide
  template edits. Repeated observations never count.
- **Availability** keeps the four states apart. A group is observed if any member is,
  possibly unavailable if any member is, closed only if every member is closed, otherwise
  assumed inactive; member counts per state are kept.
- **Extraction coverage.** Postings without an extraction count in every inventory; only
  metrics that need extracted fields leave them out, and each such metric reports its
  denominator (`extracted_postings`) and the coverage (`rpt_extraction_coverage`).
- **Overlap.** Role categories, rule families, and skills can overlap; their totals cannot
  be summed (`categories_overlap`). "Python or Java" gives each skill an *alternative*
  mention, never an individual requirement.
- **Eligibility.** Sponsorship, work authorization, citizenship, and clearance are separate
  concepts. Each has three shares: of all listed postings, of extracted postings, and of
  postings that state a policy. `not_stated` and `ambiguous` are never folded into yes or
  no.

### History and reporting days

Reporting days are America/Chicago calendar days. Each day's rows describe what was known at
the end of that day (`int_posting_days`), rebuilt from timestamped evidence: the latest
sighting, the screening stored with that content version, the latest availability event,
an extraction only if it existed by then, and a duplicate group only if it was active then.
A later closure, extraction, or grouping never changes an earlier day. Today is provisional
(`is_complete = false`). `seen_that_day` and `carried_forward` separate postings re-verified
that day from ones carried from an earlier sighting.

Coverage limits: availability is tracked from the earliest availability event (on the
current installation 2026-10-27, when tracking started); earlier days are reported as
`before_availability_tracking`, not reconstructed. Before 2026-10-28 only retained postings
kept content history, so earlier days know other postings by identity only. Extraction
answers later re-normalized in Python show their current clean-up
(`extraction_cleaned_up_later`).

| Model | Grain |
|---|---|
| `rpt_daily_activity` | day x population (`all`, `retained`): initial imports, discoveries, updates, transitions, board coverage |
| `rpt_inventory_daily` | day x population x availability state: source records, opportunities, re-verified vs carried forward |
| `rpt_inventory_current` | dimension (employer, source, rule family, AI role category, place, work arrangement, early career) x value x population x state |
| `rpt_skill_demand` | skill x requirement x mention kind (individual / alternative) x population |
| `rpt_experience_requirements` | measure (early career, overall minimum years, graduation, seniority) x value x population |
| `rpt_eligibility` | concept x status x population, with three denominators |
| `rpt_pipeline_health_daily` | day: scans by outcome, holds, AI requests and tokens, extraction coverage |
| `rpt_board_freshness` | board: latest scan, last complete collection and its age, baseline, hold |
| `rpt_extraction_coverage` | population: extracted, queued, failed, skipped |
| `rpt_build_info` | one row: when the build ran, and the newest complete collection and extraction it saw |
| `rpt_jobs_current` | distinct current job (duplicates merged), kept and listed: employer, title, metro, role group, level by both rules, minimum degree, eligibility statements, generative-AI mention, discovery week |
| `rpt_job_skills` | current job x skill x requirement level, with the job's filter fields |
| `rpt_job_genai_terms` | current job x generative-AI term it mentions, with the job's filter fields |
| `rpt_skill_pairs` | skill pair x role group: current jobs that require or prefer both |
| `rpt_weekly_jobs` | week x level rule x level x role group x metro: new jobs (initial imports excluded), jobs open at the week's end, generative-AI mentions |
| `rpt_weekly_skills` | week x skill x role group: new jobs requiring the skill |

**Dashboard definitions** (`int_job_attributes`, `int_genai_mentions`):
- **Role group:** one per job, by priority Data (data analysis, BI, data and analytics
  engineering) > AI/ML (ML, data science) > Software (software, backend, full stack) > Other.
- **Level, two rules:** `level_ai` is Junior (early career, entry, internship), Mid, Senior+
  (senior and above), or Unclear, from the AI's seniority and early-career answers.
  `level_years` uses the smallest required overall minimum: 0-2, 3-4, 5+ years, or Not stated.
- **Minimum degree:** the lowest degree listed as required, or "Degree preferred only" or
  "Not stated".
- **Weeks:** Monday to Sunday, America/Chicago. **New jobs** exclude initial imports of newly
  added boards.
- **Generative-AI mention:** the title or description matches a term in
  `analytics/macros/genai_terms.sql` (version 1), with the employer's own name removed first.
  A bare "AI" is not a term, because it appears in company names and boilerplate. Changing
  the list changes every week's numbers from the next build on.

Populations default to `retained` for headline numbers; `all` covers every collected
listing. Rebuilds are idempotent: the same stored data gives the same rows.

### Serving views (job search)

`analytics_serving` holds views, not tables, so they always show the current collection and
extraction without a rebuild; a build is needed only after the model definitions change.

| Model | Grain |
|---|---|
| `srv_postings` | listing with stored content: current version, links, availability state (as stored by the availability rules), the rule-based screening (kept, role families, Texas eligibility), cohort, duplicate group, the current version's extraction (role categories, early career, place, arrangement, sponsorship, employment type, smallest required overall minimum years, skills asked for as required or preferred), Lever's structured salary range, and board freshness |
| `srv_boards` | board: last complete collection, last scan counted by the availability rules, active hold |

Text search uses `posting_search_vector(title, description_text)`, an indexed function in
the ingestion schema (migration 0011); `srv_postings.search_vector` calls it, so searches
through the view use the index. Selecting that column for every listing computes every
vector, so search queries name the columns they need.

### PySpark comparison

Three reporting models are also written in PySpark (`src/jobmarket/spark_models.py`), to
measure Spark against PostgreSQL and dbt on this data: `rpt_skill_demand` (including the
unpacking of the JSON skill arrays), `rpt_jobs_current`, and `rpt_weekly_jobs`. dbt stays the
pipeline: nothing scheduled, and nothing in the server image, uses Spark.

1. Install `pip install -e ".[analytics,spark]"` and Java 17 or 21 (`java -version`).
2. `jobmarket spark export` reads the models' inputs from `analytics_staging` and
   `analytics_intermediate` through the analytics login and writes them to
   `data/spark/inputs/` as Parquet. In the same database snapshot it runs dbt's compiled SQL
   of the three models and writes the results to `data/spark/dbt/`. `data/` is never
   committed.
3. `jobmarket spark parity` rebuilds the models with Spark in local mode from `inputs/` and
   compares each with `dbt/`: the same columns in the same order and the same rows, each
   row as many times. Numbers must be equal exactly; the only rounding is the model's own
   (`share_of_extracted`, one decimal place). Any difference exits with `1`.
4. `jobmarket spark benchmark` times PostgreSQL, dbt, and Spark on the same inputs, at today's
   size and copied 10 and 100 times. For Databricks, see [DATABRICKS.md](spark/DATABRICKS.md).

The reference is dbt's SQL run in the export's snapshot, not the published tables. Those were
built at the last publication, while the views they read change with every collection (and
a week becomes complete at midnight), so comparing the two would show differences that
have nothing to do with Spark.

Result, measured on an M1 Mac: PostgreSQL was faster at every size tried, from today's data
(0.04 s against 4.6 s for Spark's models, plus 3.8 s of Spark startup) to 100 times today's
data (5.2 s against 12.4 s). On Databricks Free Edition the same models took 10.9 s at
today's size. Spark's results matched dbt's row for row throughout. The pipeline stays on
PostgreSQL and dbt. The method, every measurement, and what would change
the decision: [docs/spark/README.md](spark/README.md).

## Dashboard data (Tableau Public)

The Tableau Public dashboards read a Google Sheet, because Tableau Public cannot connect to the
database. `jobmarket tableau sync` copies the published dashboard tables to that Sheet, one tab
each: `about` (build time and the "data collected through ..." caption every chart shows),
`jobs`, `job_skills`, `genai_terms`, `skill_pairs`, `weekly_jobs`, `weekly_skills`,
`daily_activity`, `eligibility`, and `pipeline_health`.

- Only published reporting tables are read, through the analytics login, so
  nothing from the private workspace can reach the Sheet.
- All tabs are replaced in one Google `batchUpdate` request, which applies all or nothing:
  the Sheet holds one complete publication, the previous one if an update fails.
- Dates are sent as Sheets dates, so Tableau reads them as dates.
- The scheduled run updates the Sheet after every successful publication (Airflow task
  `sheets_sync`, 10-minute limit, skipped until `JOBMARKET_SHEETS_ID` and
  `JOBMARKET_SHEETS_KEY_FILE` are set); a failed publication never reaches the Sheet. The
  daily summary says when the Sheet was last updated.
- `--dry-run` shows the tabs and their sizes without contacting Google. Tests use a fake
  Sheets API.
- Setup (Google project, service account, Sheet, key file): [RUNBOOK](../deploy/RUNBOOK.md) section T.

**Market insights page.** `/insights` embeds the published dashboards: the main one, "What
are employers asking for?", first, and the others under "See more dashboards"
(`src/jobmarket/web/insights.py` lists each dashboard's question and business objective).
Each dashboard is framed from its Tableau Public address,
`https://public.tableau.com/views/<workbook>/<view>`, so the workbook's dashboard names must
match the view names there ([BUILD_GUIDE.md](tableau/BUILD_GUIDE.md)). Set
`JOBMARKET_TABLEAU_WORKBOOK` (and optionally `JOBMARKET_TABLEAU_PROFILE`) once the workbook
is published; until then the page shows the headline numbers and says the dashboards are on
their way. Under each dashboard the page states the collection and build time of the
published reports.

The job-search page links to it with a short summary from the published `rpt_jobs_current`:
the junior and senior shares among jobs with a clear level, and the share mentioning
generative AI. Before the first publication, or when the published reports predate these
tables, the link appears without numbers and job search is unaffected.

Dashboard designs, the data dictionary, and the build steps: [docs/tableau/](tableau/).
