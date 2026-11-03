"""The PySpark rebuild of rpt_skill_demand, rpt_jobs_current, and rpt_weekly_jobs equals dbt's
tables row for row, over the synthetic history in search_world (J1 to J8)."""

import json
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest

try:
    import pyspark  # noqa: F401
except ImportError:
    missing = "PySpark is not installed (pip install -e '.[spark]')"
else:
    java = shutil.which("java") or os.environ.get("JAVA_HOME")
    missing = None if java else "Spark needs Java 17 or 21"
if missing:
    # The CI job for these tests sets this, so a broken install cannot pass as skipped.
    if os.environ.get("JOBMARKET_REQUIRE_SPARK") == "1":
        pytest.fail(f"JOBMARKET_REQUIRE_SPARK=1 but {missing}", pytrace=False)
    pytest.skip(missing, allow_module_level=True)

import pyarrow.parquet as pq  # noqa: E402  (after the skips: both come with the spark extra)
from psycopg import sql  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from search_world import at  # noqa: E402

from jobmarket import cli, db, spark_export, spark_models  # noqa: E402


@pytest.fixture(scope="module")
def spark():
    session = spark_models.local_session()
    yield session
    session.stop()


@pytest.fixture(scope="module")
def exported(world, db_settings, tmp_path_factory):
    conn, source = world.conn, world.source
    conn.execute(
        f'UPDATE "{source}".posting_versions SET description_text = coalesce(description_text, '
        "'') || %s WHERE posting_id = ANY(%s)",
        (" We build agentic workflows on top of LLMs.", [world.ids[1], world.ids[5]]),
    )
    # Discovered late in the evening in Chicago, after midnight UTC: J2 on Sunday night, so
    # its week starts the Monday before; J3 on Monday night.
    for number, when in ((2, "2026-10-26T04:30:00"), (3, "2026-10-27T04:30:00")):
        conn.execute(
            f'UPDATE "{source}".source_postings SET first_discovered_at = %s WHERE posting_id = %s',
            (at(when), world.ids[number]),
        )
    world.analytics.build("--select", "+rpt_skill_demand", "+rpt_jobs_current", "+rpt_weekly_jobs")
    directory = tmp_path_factory.mktemp("spark")
    prefix = world.analytics.prefix
    reference = spark_export.reference_sql(
        "owner", {"source_schema": source, "schema_prefix": prefix}
    )
    with db.connect(db_settings) as export_conn:
        spark_export.export(export_conn, directory, reference, prefix)
    (directory / "built").mkdir()
    for model in spark_models.MODELS:
        query = sql.SQL("SELECT * FROM {}.{}").format(
            sql.Identifier(world.analytics.schema("reporting")), sql.Identifier(model)
        )
        with conn.transaction():
            spark_export.write_parquet(conn, query, directory / "built" / f"{model}.parquet")
    return directory


@pytest.fixture(scope="module")
def rebuilt(spark, exported):
    return spark_models.build(spark_models.read_inputs(spark, str(exported)))


@pytest.mark.db
@pytest.mark.parametrize("model", spark_models.MODELS)
def test_spark_matches_dbt_row_for_row(spark, exported, rebuilt, model):
    expected = spark_models.read_dbt(spark, str(exported))[model]
    assert expected.count() > 0
    assert spark_models.differences(rebuilt[model], expected) == []


@pytest.mark.db
@pytest.mark.parametrize("model", spark_models.MODELS)
def test_the_reference_is_the_table_dbt_built(spark, exported, model):
    built = spark.read.parquet(str(exported / "built" / f"{model}.parquet"))
    reference = spark_models.read_dbt(spark, str(exported))[model]
    assert spark_models.differences(reference, built) == []


@pytest.mark.db
def test_the_history_reaches_each_rule(exported):
    def rows(model):
        return pq.read_table(exported / "dbt" / f"{model}.parquet").to_pylist()

    skills = rows("rpt_skill_demand")
    assert {r["mention_kind"] for r in skills} == {"individual", "alternative"}
    assert {r["population"] for r in skills} == {"all", "retained"}
    jobs = rows("rpt_jobs_current")
    assert max(r["postings_in_group"] for r in jobs) == 2
    assert {r["genai_terms"] for r in jobs} >= {"LLM, AI agents", ""}
    days = {(r["discovery_day"], r["discovery_week"]) for r in jobs}
    assert (date(2026, 10, 25), date(2026, 10, 19)) in days  # J2, Sunday night
    assert (date(2026, 10, 26), date(2026, 10, 26)) in days  # J3, Monday night
    weekly = rows("rpt_weekly_jobs")
    assert {r["level_rule"] for r in weekly} == {"ai", "years"}
    assert sum(r["new_jobs"] for r in weekly) > 0 and sum(r["open_jobs_end"] for r in weekly) > 0
    assert {r["week_complete"] for r in weekly} == {True, False}


@pytest.mark.db
def test_the_comparison_reports_any_difference(spark, exported, rebuilt):
    expected = spark_models.read_dbt(spark, str(exported))["rpt_skill_demand"]
    actual = rebuilt["rpt_skill_demand"]
    first = actual.orderBy("skill", "requirement", "mention_kind", "population").first()
    changed = actual.withColumn(
        "postings",
        F.when(
            (F.col("skill") == first.skill)
            & (F.col("requirement") == first.requirement)
            & (F.col("mention_kind") == first.mention_kind)
            & (F.col("population") == first.population),
            F.col("postings") + 1,
        ).otherwise(F.col("postings")),
    )
    found = spark_models.differences(changed, expected)
    assert [line.split(",")[0] for line in found] == ["1 rows only in dbt", "1 rows only in spark"]
    reordered = actual.select(*reversed(actual.columns))
    assert spark_models.differences(reordered, expected)[0].startswith("columns differ")
    duplicated = actual.unionByName(actual.limit(1))
    assert spark_models.differences(duplicated, expected)[0].startswith("1 rows only in spark")


@pytest.mark.db
def test_the_export_reads_analytics_schemas_and_records_its_files(exported):
    assert {layer for layer, _ in spark_export.INPUTS.values()} <= {
        "analytics_staging",
        "analytics_intermediate",
    }
    summary = json.loads((exported / "export.json").read_text())
    for relative, info in summary["files"].items():
        assert pq.read_metadata(exported / relative).num_rows == info["rows"]


def test_build_refuses_another_session_time_zone(spark):
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    try:
        with pytest.raises(ValueError, match="America/Chicago"):
            spark_models.build({"int_current_postings": spark.range(1)})
    finally:
        spark.conf.set("spark.sql.session.timeZone", spark_models.TIMEZONE)


@pytest.mark.db
def test_parity_command(exported, capsys):
    assert cli.main(["spark", "parity", "--dir", str(exported)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    for model in spark_models.MODELS:
        assert model in out
    assert "All three models match dbt row for row." in out


def test_the_command_line_loads_without_spark():
    # The scheduled pipeline and the server image run jobmarket without the spark extra.
    check = "import sys, jobmarket.cli; print(sorted({'pyspark', 'pyarrow'} & set(sys.modules)))"
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
    assert result.stdout.strip() == "[]", result.stderr
