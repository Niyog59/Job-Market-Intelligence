"""jobmarket spark benchmark over the synthetic history in search_world: every part is
measured, Spark and PostgreSQL agree at every scale, copies are separate jobs, and the
throwaway dbt schemas are removed."""

import pytest
from spark_harness import require_spark

require_spark()

from jobmarket import spark_benchmark, spark_export  # noqa: E402

RUNS = 2
SCALES = [1, 3]


@pytest.fixture(scope="module")
def measured(world, db_settings, tmp_path_factory):
    world.analytics.build("--select", "+rpt_skill_demand", "+rpt_jobs_current", "+rpt_weekly_jobs")
    return spark_benchmark.benchmark(
        db_settings,
        tmp_path_factory.mktemp("benchmark"),
        RUNS,
        SCALES,
        target="owner",
        source_schema=world.source,
        prefix=world.analytics.prefix,
        progress=lambda line: None,
    )


@pytest.mark.db
def test_spark_and_postgres_agree_at_every_scale(measured):
    for scale in measured["scales"].values():
        assert scale["parity"] == {name: [] for name in spark_export.MODELS}
        assert all(rows > 0 for rows in scale["output_rows"].values())


@pytest.mark.db
def test_copies_are_separate_jobs(measured):
    one, three = measured["scales"]["1"], measured["scales"]["3"]
    for name, rows in one["rows"].items():
        expected = rows if name == "int_reporting_days" else 3 * rows
        assert three["rows"][name] == expected, name
    # Three times the jobs, in the same skill and weekly groups.
    assert three["output_rows"]["rpt_jobs_current"] == 3 * one["output_rows"]["rpt_jobs_current"]
    for name in ("rpt_skill_demand", "rpt_weekly_jobs"):
        assert three["output_rows"][name] == one["output_rows"][name]
    assert three["parquet_bytes"] > one["parquet_bytes"]


@pytest.mark.db
def test_every_part_is_measured(measured):
    def timed(stats):
        assert len(stats["runs"]) == RUNS and stats["min"] <= stats["median"] <= stats["max"]

    real = measured["real_size"]
    timed(real["export_from_views"])
    timed(real["dbt_run"]["wall"])
    assert set(real["dbt_run"]["models"]) == set(spark_export.MODELS)
    for scale in measured["scales"].values():
        timed(scale["postgres"]["total"])
        timed(scale["export"])
        for part in ("startup", "read", "models_total", "with_export_and_startup"):
            timed(scale["spark"][part])
    assert measured["machine"]["pyspark"] and "version" in measured["machine"]["java"]
    assert measured["postgres_server"].startswith("PostgreSQL")
    table = spark_benchmark.summary(measured)
    assert "| 1x |" in table and "| 3x |" in table


@pytest.mark.db
def test_the_dbt_schemas_are_removed(world, measured):
    leftover = world.conn.execute(
        "SELECT count(*) FROM pg_namespace WHERE nspname LIKE %s",
        (f"{world.analytics.prefix}{spark_benchmark.DBT_PREFIX}%",),
    ).fetchone()[0]
    assert leftover == 0


def test_postgres_runs_only_on_the_temporary_tables():
    compiled = 'select * from "db"."analytics_intermediate"."int_current_postings" cp'
    assert spark_benchmark.local_sql(compiled) == 'select * from pg_temp."int_current_postings" cp'
    with pytest.raises(ValueError, match="no input int_opportunity_days"):
        spark_benchmark.local_sql('from "db"."analytics_intermediate"."int_opportunity_days"')
    with pytest.raises(ValueError, match="still reads"):
        spark_benchmark.local_sql('from "db"."analytics_reporting"."rpt_build_info"')
