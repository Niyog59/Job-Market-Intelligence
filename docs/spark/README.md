# PySpark and Databricks against PostgreSQL and dbt

> **Status:** measured locally on 2026-11-03 (`docs/spark/results/benchmark.json`). The
> Databricks run is still to do; its values are marked *not measured yet* and will come from
> `docs/spark/results/databricks.json`. No number in this page is estimated.

## The question

The pipeline's reporting step runs as dbt SQL models in PostgreSQL, on one EC2 server. Would
Spark, on the same machine (local mode) or on Databricks, do that step better for this data?

To answer it with evidence, three of the reporting models were rebuilt in PySpark, checked
row for row against dbt, and timed against PostgreSQL on the same inputs.

## Setup

| | Value |
|---|---|
| Machine (local run) | MacBook with Apple M1, 8 cores, 8 GB of memory, macOS 15.3 |
| PostgreSQL | 17.2, an x86_64 build, so it runs under Rosetta on this ARM Mac |
| Python, PySpark, Java | 3.11.5, PySpark 4.2.0 (PyArrow 26.0.1), OpenJDK 21.0.13 |
| dbt | 1.11.16 with dbt-postgres 1.11.0 |
| Databricks | Free Edition, serverless compute, Spark *not measured yet* |
| Data at today's size | 66,367 input rows: 0.96 MB as Parquet, 8.53 MB in PostgreSQL |

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
| Real data, local mode (`jobmarket spark parity`) | identical: 1,673, 152, and 148 rows |
| Real data, every benchmark scale (Spark against PostgreSQL) | identical at 1x, 10x, and 100x |
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

Seconds, medians of 5 runs. The fastest and slowest runs are in `benchmark.json`.

| Part | 1x | 10x | 100x |
|---|---|---|---|
| Input rows | 66,367 | 663,580 | 6,635,710 |
| Size in PostgreSQL | 8.5 MB | 73.7 MB | 734.6 MB |
| PostgreSQL: three models | **0.040** | **0.351** | **5.180** |
| Export to Parquet | 0.212 | 2.359 | 20.954 |
| Spark: session startup | 3.780 | 3.244 | 3.368 |
| Spark: read every input column | 3.592 | 3.433 | 5.207 |
| Spark: three models | 4.619 | 4.902 | 12.400 |
| Spark: export + startup + three models | 8.443 | 10.710 | 37.178 |
| Same result as PostgreSQL | yes | yes | yes |

At today's size, `dbt run` of the three models takes 11.6 s and `jobmarket spark export`
7.7 s. Both are much slower than the 0.04 s above because they start from the live dbt views,
not from ready inputs. Inside that dbt run, the two models that read `int_genai_mentions` (keyword
matching over every description) take 8.9 s and 8.1 s, against 0.18 s for `rpt_skill_demand`,
which does not read it. The cost is upstream of the reporting step, and the export pays it
too, before Spark starts.

Databricks Free Edition (today's size): read *not measured yet*, three models *not measured
yet*. Serverless compute is already running when the notebook starts, so it has no startup
time to report, and its machine size is not shown.

## Decision

Stay with PostgreSQL and dbt.

- At today's size PostgreSQL computes the three models in 0.04 s. Spark needs 3.8 s just to
  start its JVM and session, then 4.6 s for the models alone, more than a hundred times
  PostgreSQL's time. At this size nearly all of Spark's time is fixed overhead.
- PostgreSQL stays faster at every measured scale, including 100 times today's data (6.6
  million input rows): 5.2 s against Spark's 12.4 s for the models alone, or 37.2 s once the
  export and startup that the Spark path needs are counted.
- Spark gives the same answer, row for row, so the choice is about cost and simplicity, not
  correctness. Keeping one engine also means one database to run, back up, and secure.

## What would change the decision

No measured scale reached a crossover. The trend points at one further out: from 1x to 100x
PostgreSQL's time grew from 0.04 s to 5.2 s, while Spark's model time grew from 4.6 s to
12.4 s. Somewhere above 100 times today's data Spark might win on this machine, but that was
not measured, and the export to Parquet (21 s at 100x) would still sit in front of it unless
the data lived in a Spark-native store to begin with. Not tested either: running a JVM next to
PostgreSQL and Airflow on the planned 2 GiB server (`deploy/SIZING.md`).

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
