"""Time rpt_skill_demand, rpt_jobs_current, and rpt_weekly_jobs in PostgreSQL, dbt, and Spark
local mode on the same inputs (jobmarket spark benchmark; the write-up is docs/spark/).

The inputs are read once, in one snapshot, into temporary tables of the analytics login's
session. Every measurement then starts from them:
  postgres  dbt's compiled SQL of the three models over those tables (CREATE TABLE AS, as dbt
            runs it). int_skill_mentions is a temporary view, so the JSON unpacking is timed
            here too, as it is in Spark.
  export    those tables written to Parquet, which Spark needs and PostgreSQL does not.
  spark     a new Python process per run: session startup (the JVM), a scan of every input
            column, and the three models computed from Parquet and written back to Parquet.
At scale N the inputs are copied N times with every id shifted per copy, so each copy is a
separate set of jobs; the calendar (int_reporting_days) is shared. Spark's result is compared
with PostgreSQL's at every scale.

At the real size only, two more: the real export (reading the dbt views, so it includes
computing them), and `dbt run` of the three models in a new process each time, with dbt's
own start-up, parsing, and connections. dbt builds into spark_bench__analytics_* schemas,
dropped at the end; the published schemas are never touched.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql

from jobmarket import db, spark_export
from jobmarket.analytics import LAYERS, run_dbt
from jobmarket.config import Settings
from jobmarket.spark_models import stats

DBT_PREFIX = "spark_bench__"
OFFSET = 10**12  # added to every id once per copy; ids are bigint
# The id columns shifted per copy. int_reporting_days is the calendar and is not copied.
COPIED_IDS = {
    "int_current_postings": ("posting_id", "employer_id", "version_id", "extraction_result_id"),
    "int_job_attributes": ("result_id",),
    "int_genai_mentions": ("version_id",),
    "int_posting_days": (
        "posting_id",
        "board_id",
        "duplicate_group_id",
        "version_id",
        "extraction_result_id",
    ),
    "stg_boards": ("board_id", "employer_id"),
    "stg_posting_versions": ("version_id",),
}
COMPILED = ("int_skill_mentions", *spark_export.MODELS)


def copy_query(name: str, scale: int) -> sql.Composed:
    _, columns = spark_export.INPUTS[name]
    base = sql.Identifier(f"base_{name}")
    if name not in COPIED_IDS:
        return sql.SQL("SELECT * FROM pg_temp.{}").format(base)
    select = []
    for column in columns:
        target = sql.Identifier(column)
        if column in COPIED_IDS[name]:
            select.append(sql.SQL("{} + copy_number * {} AS {}").format(target, OFFSET, target))
        elif column == "opportunity_key":  # 'group:12' or 'posting:34'
            select.append(
                sql.SQL(
                    "split_part({0}, ':', 1) || ':' "
                    "|| (split_part({0}, ':', 2)::bigint + copy_number * {1}) AS {0}"
                ).format(target, OFFSET)
            )
        else:
            select.append(target)
    return sql.SQL(
        "SELECT {} FROM pg_temp.{} CROSS JOIN generate_series(0, {}) AS copy_number"
    ).format(sql.SQL(", ").join(select), base, scale - 1)


def local_sql(compiled: str) -> str:
    """dbt's SQL with every analytics relation it reads replaced by the session's
    temporary table of the same name."""
    names = set(spark_export.INPUTS) | {"int_skill_mentions"}
    pattern = r'"[^"]+"\."[^"]*analytics_(?:staging|intermediate)"\."(\w+)"'

    def replace(match):
        if match.group(1) not in names:
            raise ValueError(f"the benchmark has no input {match.group(1)}")
        return f'pg_temp."{match.group(1)}"'

    local = re.sub(pattern, replace, compiled)
    if re.search(r"analytics_\w+\"\.", local):
        raise ValueError("compiled SQL still reads an analytics schema")
    return local


def child(*args: str) -> dict:
    """Run one measurement in a new Python process (python -m jobmarket.spark_benchmark)."""
    done = subprocess.run(
        [sys.executable, "-m", "jobmarket.spark_benchmark", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} run failed: {done.stderr.strip()[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _spark_child(directory: str, output: str) -> dict:
    started = time.perf_counter()
    from jobmarket import spark_models

    spark = spark_models.local_session()
    startup = time.perf_counter() - started
    return {"startup": startup, **spark_models.timed_run(spark, directory, output)}


def _parity_child(spark_output: str, postgres_output: str) -> dict:
    from jobmarket import spark_models

    spark = spark_models.local_session()
    return {
        name: spark_models.differences(
            spark.read.parquet(f"{spark_output}/{name}.parquet"),
            spark.read.parquet(f"{postgres_output}/{name}.parquet"),
        )
        for name in spark_models.MODELS
    }


def _dbt_child(arguments: str) -> dict:
    options = json.loads(arguments)
    outcome = run_dbt(
        ["run", "--select", *spark_export.MODELS],
        target=options["target"],
        variables=options["variables"],
        quiet=True,
    )
    if not outcome.success:
        raise RuntimeError("; ".join(outcome.failures) or "dbt run failed")
    return {name: outcome.timings[name] for name in spark_export.MODELS}


def machine() -> dict:
    info = {
        "system": platform.platform(),
        "processor": platform.machine(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
    }
    if sys.platform == "darwin":
        for key, name in (("cpu", "machdep.cpu.brand_string"), ("memory_bytes", "hw.memsize")):
            done = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
            info[key] = done.stdout.strip()
    elif Path("/proc/meminfo").exists():
        info["memory_bytes"] = int(Path("/proc/meminfo").read_text().split()[1]) * 1024
    executable = shutil.which("java") or str(Path(os.environ.get("JAVA_HOME", ""), "bin", "java"))
    java = subprocess.run([executable, "-version"], capture_output=True, text=True)
    info["java"] = next(
        (line for line in java.stderr.splitlines() if "version" in line), java.stderr.strip()
    )
    import dbt.version
    import pyarrow
    import pyspark

    info.update(
        pyspark=pyspark.__version__, pyarrow=pyarrow.__version__, dbt=dbt.version.__version__
    )
    return info


def _dbt_runs(conn, runs: int, target: str, variables: dict) -> dict:
    schema_prefix = variables["schema_prefix"]
    setup = run_dbt(
        ["run", "--select", *(f"+{m}" for m in spark_export.MODELS)],
        target=target,
        variables=variables,
        quiet=True,
    )
    try:
        if not setup.success:
            raise RuntimeError("; ".join(setup.failures) or "dbt run failed")
        walls, models = [], {name: [] for name in spark_export.MODELS}
        for _ in range(runs):
            started = time.perf_counter()
            timings = child("dbt", json.dumps({"target": target, "variables": variables}))
            walls.append(time.perf_counter() - started)
            for name, seconds in timings.items():
                models[name].append(seconds)
        totals = [sum(run) for run in zip(*models.values(), strict=True)]
        return {
            "wall": stats(walls),
            "models": {name: stats(values) for name, values in models.items()},
            "models_total": stats(totals),
        }
    finally:
        for layer in LAYERS:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(f"{schema_prefix}{layer}")
                )
            )


def _scale(conn, directory: Path, scale: int, runs: int, queries: dict[str, str]) -> dict:
    for name in spark_export.INPUTS:
        conn.execute(
            sql.SQL("DROP TABLE IF EXISTS pg_temp.{} CASCADE").format(sql.Identifier(name))
        )
        conn.execute(
            sql.SQL("CREATE TEMP TABLE {} AS {}").format(
                sql.Identifier(name), copy_query(name, scale)
            )
        )
        # Autovacuum never analyzes temporary tables. The tables behind dbt's views have
        # statistics, so these get them too.
        conn.execute(sql.SQL("ANALYZE pg_temp.{}").format(sql.Identifier(name)))
    conn.execute(f"CREATE TEMP VIEW int_skill_mentions AS {queries['int_skill_mentions']}")
    result = {
        "rows": {
            name: conn.execute(
                sql.SQL("SELECT count(*) FROM pg_temp.{}").format(sql.Identifier(name))
            ).fetchone()[0]
            for name in spark_export.INPUTS
        },
        "postgres_bytes": sum(
            conn.execute(
                "SELECT pg_total_relation_size(%s::regclass)", (f"pg_temp.{name}",)
            ).fetchone()[0]
            for name in spark_export.INPUTS
        ),
    }

    postgres = {name: [] for name in spark_export.MODELS}
    for _ in range(runs):
        for name in spark_export.MODELS:
            table = sql.Identifier(f"bench_{name}")
            conn.execute(sql.SQL("DROP TABLE IF EXISTS pg_temp.{}").format(table))
            started = time.perf_counter()
            conn.execute(
                sql.SQL("CREATE TEMP TABLE {} AS ").format(table).as_string(conn) + queries[name]
            )
            postgres[name].append(time.perf_counter() - started)

    with tempfile.TemporaryDirectory(dir=directory) as work:
        work = Path(work)
        (work / "inputs").mkdir()
        exports = []
        for _ in range(runs):
            started = time.perf_counter()
            with conn.transaction():
                for name in spark_export.INPUTS:
                    query = sql.SQL("SELECT {} FROM pg_temp.{}").format(
                        spark_export.columns(name), sql.Identifier(name)
                    )
                    spark_export.write_parquet(conn, query, work / "inputs" / f"{name}.parquet")
            exports.append(time.perf_counter() - started)
        result["parquet_bytes"] = sum(f.stat().st_size for f in (work / "inputs").iterdir())

        spark = [child("spark", str(work), str(work / "spark")) for _ in range(runs)]
        (work / "postgres").mkdir()
        result["output_rows"] = {}
        with conn.transaction():
            for name in spark_export.MODELS:
                table = sql.Identifier(f"bench_{name}")
                result["output_rows"][name] = spark_export.write_parquet(
                    conn,
                    sql.SQL("SELECT * FROM pg_temp.{}").format(table),
                    work / "postgres" / f"{name}.parquet",
                )
        result["parity"] = child("parity", str(work / "spark"), str(work / "postgres"))

    postgres_totals = [sum(run) for run in zip(*postgres.values(), strict=True)]
    spark_models_totals = [sum(run["models"].values()) for run in spark]
    result.update(
        postgres={
            "models": {name: stats(values) for name, values in postgres.items()},
            "total": stats(postgres_totals),
        },
        export=stats(exports),
        spark={
            "startup": stats([run["startup"] for run in spark]),
            "read": stats([run["read"] for run in spark]),
            "models": {
                name: stats([run["models"][name] for run in spark]) for name in spark_export.MODELS
            },
            "models_total": stats(spark_models_totals),
            # what Spark needs end to end, from the PostgreSQL tables to its written results
            "with_export_and_startup": stats(
                [
                    export + run["startup"] + total
                    for export, run, total in zip(exports, spark, spark_models_totals, strict=True)
                ]
            ),
        },
    )
    return result


def benchmark(
    settings: Settings,
    directory: Path,
    runs: int,
    scales: list[int],
    target: str = "analytics",
    source_schema: str | None = None,
    prefix: str = "",
    progress=print,
) -> dict:
    """Run every measurement; settings is the login (the read-only analytics login on a
    normal install). prefix and source_schema are for test builds."""
    directory.mkdir(parents=True, exist_ok=True)
    variables = {"schema_prefix": prefix}
    if source_schema:
        variables["source_schema"] = source_schema
    progress("Compiling the dbt models.")
    queries = {
        name: local_sql(query)
        for name, query in spark_export.reference_sql(target, variables, COMPILED).items()
    }
    result = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs": runs,
        "machine": machine(),
    }

    progress(f"Real size: the export from the dbt views and dbt run, {runs} times each.")
    exports = []
    for _ in range(runs):
        with tempfile.TemporaryDirectory(dir=directory) as work, db.connect(settings) as conn:
            started = time.perf_counter()
            spark_export.export(conn, Path(work), {}, prefix)
            exports.append(time.perf_counter() - started)
    with db.connect(settings, autocommit=True) as conn:
        result["postgres_server"] = conn.execute("SELECT version()").fetchone()[0]
        dbt_variables = {**variables, "schema_prefix": f"{prefix}{DBT_PREFIX}"}
        result["real_size"] = {
            "export_from_views": stats(exports),
            "dbt_run": _dbt_runs(conn, runs, target, dbt_variables),
        }

        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with conn.transaction():
            for name, (layer, _) in spark_export.INPUTS.items():
                conn.execute(
                    sql.SQL("CREATE TEMP TABLE {} AS SELECT {} FROM {}.{}").format(
                        sql.Identifier(f"base_{name}"),
                        spark_export.columns(name, json_as_text=False),
                        sql.Identifier(f"{prefix}{layer}"),
                        sql.Identifier(name),
                    )
                )
        result["scales"] = {}
        for scale in scales:
            progress(f"Scale {scale}x: PostgreSQL, export, and Spark, {runs} times each.")
            result["scales"][str(scale)] = _scale(conn, directory, scale, runs, queries)
    return result


def summary(result: dict) -> str:
    """The medians as a Markdown table, in seconds."""
    real = result["real_size"]
    lines = [
        f"Medians of {result['runs']} runs, seconds.",
        "",
        f"Real size: export from the dbt views {real['export_from_views']['median']}; "
        f"dbt run of the three models {real['dbt_run']['wall']['median']} "
        f"(dbt's own model timings add up to {real['dbt_run']['models_total']['median']}).",
        "",
        "| Scale | Input rows | Parquet MB | PostgreSQL MB | PostgreSQL models | Export "
        "| Spark startup | Spark read | Spark models | Spark with export and startup "
        "| Same result |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for scale, s in result["scales"].items():
        spark = s["spark"]
        same = "yes" if not any(s["parity"].values()) else "no"
        lines.append(
            f"| {scale}x | {sum(s['rows'].values()):,} | {s['parquet_bytes'] / 1e6:.2f} "
            f"| {s['postgres_bytes'] / 1e6:.2f} | {s['postgres']['total']['median']} "
            f"| {s['export']['median']} | {spark['startup']['median']} "
            f"| {spark['read']['median']} | {spark['models_total']['median']} "
            f"| {spark['with_export_and_startup']['median']} | {same} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    measurements = {"spark": _spark_child, "parity": _parity_child, "dbt": _dbt_child}
    print(json.dumps(measurements[sys.argv[1]](*sys.argv[2:])))
