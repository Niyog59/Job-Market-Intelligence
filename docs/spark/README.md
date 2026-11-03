# PySpark and Databricks against PostgreSQL and dbt

> **Status: waiting for measurements.** The code, the parity tests, and the method are done.
> Every value marked *not measured yet* comes from `docs/spark/results/benchmark.json` (local
> run) and `docs/spark/results/databricks.json` (Databricks run) once those exist. No number in
> this page is estimated.

## The question

The pipeline's reporting step runs as dbt SQL models in PostgreSQL, on one EC2 server. Would
Spark, on the same machine (local mode) or on Databricks, do that step better for this data?

To answer it with evidence, three of the reporting models were rebuilt in PySpark, checked
row for row against dbt, and timed against PostgreSQL on the same inputs.

## Setup

| | Value |
|---|---|
| Machine (local run) | *not measured yet* (`machine` in benchmark.json) |
| PostgreSQL | *not measured yet* (`postgres_server`) |
| Python, PySpark, Java | *not measured yet* (PySpark pinned to 4.2.0) |
| dbt | 1.11.16 with dbt-postgres 1.11.0 |
| Databricks | Free Edition, serverless compute, Spark *not measured yet* |
| Data at today's size | *not measured yet*: input rows, Parquet MB, PostgreSQL MB |

## What was rebuilt

| Model | What it exercises |
|---|---|
| `rpt_skill_demand` | Unpacking the JSON skill arrays of every current extraction (`int_skill_mentions` is rebuilt too), then distinct counts per skill, level, and population. |
| `rpt_jobs_current` | One row per duplicate group (a window over postings), joined to attributes, generative-AI terms, and URLs. |
| `rpt_weekly_jobs` | Monday-start weeks in America/Chicago, two level rules, distinct counts with filters. |

Spark starts from the dbt models these three read (`int_current_postings`, `int_posting_days`,
and five others), exported to Parquet by `jobmarket spark export`. The code is
`src/jobmarket/spark_models.py`. It follows the SQL rule for rule; the differences that had to
be handled are commented there:

- PostgreSQL sorts NULL last ascending and first descending; Spark does the opposite.
- `array_to_string(NULL)` is NULL; Spark's `concat_ws(NULL)` is an empty string.
- A Python float literal would make `share_of_extracted` a double; the decimal literal keeps it
  exact.
- Dates depend on the session time zone, which must be America/Chicago.

## Parity

The rule: the same columns in the same order, and the same rows with the same counts. Values
must be equal exactly; the only rounding is the model's own (`share_of_extracted`, one decimal
place).

| Where | Result |
|---|---|
| Tests on the synthetic history (CI job `spark`), including a discovery late on a Sunday evening in Chicago (already Monday in UTC) | identical, all three models |
| Real data, local mode (`jobmarket spark parity`) | *not measured yet* |
| Real data, every benchmark scale (Spark against PostgreSQL) | *not measured yet* (`parity` per scale) |
| Real data, Databricks | *not measured yet* |

## How it was measured

`jobmarket spark benchmark` (the default is 5 runs of each measurement, reported as the median
with the fastest and slowest):

- The inputs are read once, in one snapshot, into temporary tables. Every measurement below
  starts from them, so PostgreSQL and Spark work on identical data.
- **PostgreSQL:** dbt's compiled SQL of the three models, as `CREATE TABLE AS` (what dbt runs).
  The temporary tables are analyzed first, as autovacuum would analyze real ones.
- **Export:** those tables written to Parquet, a cost only the Spark path has.
- **Spark local mode:** a new process per run, so JVM startup is counted. Session startup, then
  a scan of every input column, then the three models computed from Parquet and written to
  Parquet. Spark's defaults are kept, except the time zone and the web UI (off). Nothing is
  cached.
- **Scale:** the inputs copied 10 and 100 times, every id shifted per copy, so each copy is a
  separate set of jobs. This asks where Spark would start to pay off.
- **At today's size only:** the export as `jobmarket spark export` does it (reading the dbt
  views, so it includes computing them), and `dbt run` of the three models in a new process,
  with dbt's own startup, parsing, and connections.

## Timings

Seconds, medians of 5 runs. *Not measured yet.*

| Part | 1x | 10x | 100x |
|---|---|---|---|
| Input rows | | | |
| PostgreSQL: three models | | | |
| Export to Parquet | | | |
| Spark: session startup | | | |
| Spark: read every input column | | | |
| Spark: three models | | | |
| Spark: export + startup + three models | | | |
| Same result as PostgreSQL | | | |

At today's size: `dbt run` of the three models *not measured yet* (dbt's own model timings
*not measured yet*); `jobmarket spark export` *not measured yet*.

Databricks Free Edition (today's size): read *not measured yet*, three models *not measured
yet*. Serverless compute is already running when the notebook starts, so it has no startup
time to report, and its machine size is not shown.

## Decision

*To be written from the measurements above.*

## What would change the decision

*To be written from the measurements above:* the scale at which Spark's time drops below
PostgreSQL's, if any of the measured scales reaches it.

Moving engines would not mean rewriting the models in PySpark: dbt has adapters for
Databricks (`dbt-databricks`) and Snowflake (`dbt-snowflake`), so the models could stay SQL in
dbt. Their PostgreSQL-specific parts (`jsonb` functions, `DISTINCT ON`, `array_agg(...)[1]`)
would need translating to the other dialect.

## Reproduce

On the machine with the database (`.env` with `ANALYTICS_DB_PASSWORD`), with Java 17 or 21:

```bash
pip install -e ".[analytics,spark]"
jobmarket spark export
jobmarket spark parity
jobmarket spark benchmark
```

The benchmark takes several minutes at 100x and writes `docs/spark/results/benchmark.json`.
For Databricks, see [DATABRICKS.md](DATABRICKS.md).
