"""Export the inputs of three reporting models to Parquet for the PySpark rebuild
(jobmarket.spark_models), together with dbt's result over the same inputs.

The inputs are the dbt models that rpt_skill_demand, rpt_jobs_current, and rpt_weekly_jobs
read, from analytics_staging and analytics_intermediate, through the read-only analytics login.
The private schema is never read.

The reference Spark must match is dbt's compiled SQL of the three models, run inside the same
snapshot as the export. The published tables would not do: they were built at the last
publication, while the views they read move on with every collection, and int_reporting_days
with the clock (a week becomes complete at midnight). Reading the inputs at one moment and the
published tables from another would report differences that have nothing to do with Spark.

Directory layout:
  inputs/<model>.parquet   what Spark reads
  dbt/<model>.parquet      dbt's result over the same snapshot
  export.json              when, and rows, bytes, and seconds per file
"""

import json
import time
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql

from jobmarket.analytics import run_dbt

MODELS = ("rpt_skill_demand", "rpt_jobs_current", "rpt_weekly_jobs")

# Only the columns the three models use. stg_posting_versions, for one, also computes a
# full-text search vector for every row.
INPUTS = {
    "int_current_postings": (
        "analytics_intermediate",
        """posting_id opportunity_key employer_id employer_name title cohort first_discovered_at
        last_observed_at version_id retained availability_state extracted extraction_result_id
        early_career sponsorship work_arrangement work_authorization citizenship clearance
        seniority employment_type role_categories extraction as_of""".split(),
    ),
    "int_job_attributes": (
        "analytics_intermediate",
        """result_id role_group level_ai overall_min_years level_years minimum_degree metro
        city""".split(),
    ),
    "int_genai_mentions": (
        "analytics_intermediate",
        ["version_id", "genai_mention", "genai_terms"],
    ),
    "int_posting_days": (
        "analytics_intermediate",
        """posting_id report_day board_id duplicate_group_id version_id extraction_result_id
        retained availability_state last_observed_at""".split(),
    ),
    "int_reporting_days": ("analytics_intermediate", ["report_day", "is_complete"]),
    "stg_boards": ("analytics_staging", ["board_id", "employer_id"]),
    "stg_posting_versions": ("analytics_staging", ["version_id", "posting_url"]),
}

# Parquet has no JSON type. These travel as text and Spark parses them (from_json), which is
# part of the work the rebuild is measured on.
JSON_COLUMNS = {"role_categories", "extraction"}

PARQUET_TYPES = {
    "bool": pa.bool_(),
    "int4": pa.int32(),
    "int8": pa.int64(),
    "text": pa.string(),
    "varchar": pa.string(),
    "date": pa.date32(),
    "timestamptz": pa.timestamp("us", tz="UTC"),
    # numeric has no fixed scale. Ten places hold every value these models produce, and
    # pyarrow refuses a value that would lose digits instead of rounding it.
    "numeric": pa.decimal128(38, 10),
    "_text": pa.list_(pa.string()),
}

BATCH_ROWS = 50_000


class ExportError(Exception):
    pass


def reference_sql(
    target: str = "analytics", variables: dict | None = None, models: tuple = MODELS
) -> dict[str, str]:
    """dbt's compiled SQL of the models (what `dbt run` would create the tables from)."""
    # Without --log-level none, compile prints one model's SQL to the terminal.
    outcome = run_dbt(
        ["compile", "--select", *models, "--log-level", "none"],
        target=target,
        variables=variables,
        quiet=True,
    )
    missing = [name for name in models if name not in outcome.compiled]
    if not outcome.success or missing:
        raise ExportError("; ".join(outcome.failures) or f"dbt did not compile {missing}")
    return {name: outcome.compiled[name] for name in models}


def columns(name: str, json_as_text: bool = True) -> sql.Composed:
    """The select list of one input."""
    return sql.SQL(", ").join(
        sql.SQL("{}::text AS {}").format(sql.Identifier(c), sql.Identifier(c))
        if json_as_text and c in JSON_COLUMNS
        else sql.Identifier(c)
        for c in INPUTS[name][1]
    )


def input_query(name: str, prefix: str = "") -> sql.Composed:
    layer = INPUTS[name][0]
    return sql.SQL("SELECT {} FROM {}.{}").format(
        columns(name), sql.Identifier(f"{prefix}{layer}"), sql.Identifier(name)
    )


def write_parquet(conn: psycopg.Connection, query, path: Path) -> int:
    """Stream a query's rows into one Parquet file; return the row count."""
    # A server-side cursor, so a large table never sits in memory whole.
    with conn.cursor(name="spark_export") as cur:
        cur.execute(query)
        fields = []
        for column in cur.description:
            info = conn.adapters.types.get(column.type_code)
            # The registry files an array type under its element type.
            type_name = f"_{info.name}" if column.type_code == info.array_oid else info.name
            if type_name not in PARQUET_TYPES:
                raise ExportError(f"{path.stem}.{column.name}: no Parquet type for {type_name}")
            fields.append(pa.field(column.name, PARQUET_TYPES[type_name]))
        schema = pa.schema(fields)
        rows = 0
        with pq.ParquetWriter(path, schema) as writer:
            while batch := cur.fetchmany(BATCH_ROWS):
                columns = list(zip(*batch, strict=True))
                writer.write_batch(
                    pa.record_batch(
                        [
                            pa.array(values, f.type)
                            for values, f in zip(columns, fields, strict=True)
                        ],
                        schema=schema,
                    )
                )
                rows += len(batch)
    return rows


def export(
    conn: psycopg.Connection, directory: Path, reference: dict[str, str], prefix: str = ""
) -> dict:
    """Write the inputs and dbt's results from one snapshot. conn must be idle (no open
    transaction); prefix is for test builds (<prefix>analytics_intermediate, ...)."""
    for folder in ("inputs", "dbt"):
        (directory / folder).mkdir(parents=True, exist_ok=True)
    jobs = [(f"inputs/{name}.parquet", input_query(name, prefix)) for name in INPUTS]
    jobs += [(f"dbt/{name}.parquet", query) for name, query in reference.items()]
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    files = {}
    with conn.transaction():
        snapshot = conn.execute("SELECT now()").fetchone()[0]
        for relative, query in jobs:
            started = time.perf_counter()
            rows = write_parquet(conn, query, directory / relative)
            files[relative] = {
                "rows": rows,
                "bytes": (directory / relative).stat().st_size,
                "seconds": round(time.perf_counter() - started, 3),
            }
    summary = {"snapshot": snapshot.isoformat(), "files": files}
    (directory / "export.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
