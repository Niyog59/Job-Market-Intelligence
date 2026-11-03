"""rpt_skill_demand, rpt_jobs_current, and rpt_weekly_jobs rebuilt in PySpark from the inputs
jobmarket.spark_export writes, and an exact comparison with dbt's tables.

The rules are the dbt models' (analytics/models/reporting/); comments here cover only where
Spark and PostgreSQL behave differently. rpt_skill_demand also rebuilds int_skill_mentions,
so the JSON skill arrays are unpacked here rather than exported already unpacked.

This module imports nothing but PySpark, so a Databricks notebook can import it as it is.
"""

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

TIMEZONE = "America/Chicago"
INPUTS = (
    "int_current_postings",
    "int_job_attributes",
    "int_genai_mentions",
    "int_posting_days",
    "int_reporting_days",
    "stg_boards",
    "stg_posting_versions",
)
MODELS = ("rpt_skill_demand", "rpt_jobs_current", "rpt_weekly_jobs")
LISTED = ("observed", "possibly_unavailable")

# Only the fields read; from_json ignores the rest of the extraction document.
SKILLS_JSON = (
    "skills ARRAY<STRUCT<canonical: STRING, status: STRING>>, "
    "skill_groups ARRAY<STRUCT<relation: STRING, status: STRING, "
    "members: ARRAY<STRUCT<canonical: STRING>>>>"
)


def local_session() -> SparkSession:
    spark = (
        SparkSession.builder.master("local[*]")
        .appName("jobmarket")
        .config("spark.sql.session.timeZone", TIMEZONE)
        .config("spark.ui.enabled", "false")
        .config("spark.ui.showConsoleProgress", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    return spark


def read_inputs(spark: SparkSession, directory: str) -> dict[str, DataFrame]:
    return {name: spark.read.parquet(f"{directory}/inputs/{name}.parquet") for name in INPUTS}


def read_dbt(spark: SparkSession, directory: str) -> dict[str, DataFrame]:
    return {name: spark.read.parquet(f"{directory}/dbt/{name}.parquet") for name in MODELS}


def build(t: dict[str, DataFrame]) -> dict[str, DataFrame]:
    spark = t["int_current_postings"].sparkSession
    zone = spark.conf.get("spark.sql.session.timeZone")
    if zone != TIMEZONE:
        # to_date() uses the session time zone, so reporting days would shift silently.
        raise ValueError(f"spark.sql.session.timeZone is {zone}, expected {TIMEZONE}")
    return {
        "rpt_skill_demand": skill_demand(t),
        "rpt_jobs_current": jobs_current(t),
        "rpt_weekly_jobs": weekly_jobs(t),
    }


def first_per(df: DataFrame, keys: list, *order: Column) -> DataFrame:
    """One row per key, the first in the given order: PostgreSQL's DISTINCT ON, and
    (array_agg(... ORDER BY ...))[1]."""
    rank = F.row_number().over(Window.partitionBy(*keys).orderBy(*order))
    return df.withColumn("_rank", rank).where("_rank = 1").drop("_rank")


# PostgreSQL sorts NULL last ascending and first descending; Spark does the opposite. Every
# ordering states it, so a null timestamp would pick the same row in both.
def newest_first() -> tuple[Column, Column]:
    return F.col("last_observed_at").desc_nulls_first(), F.col("posting_id").desc_nulls_first()


def earliest_first() -> tuple[Column, Column]:
    return F.col("first_discovered_at").asc_nulls_last(), F.col("posting_id").asc_nulls_last()


def week_start(day: Column) -> Column:
    return F.trunc(day, "week")  # Monday, as PostgreSQL's date_trunc('week', ...)


def opportunity_key(df: DataFrame) -> Column:
    return F.coalesce(
        F.concat(F.lit("group:"), df["duplicate_group_id"].cast("string")),
        F.concat(F.lit("posting:"), df["posting_id"].cast("string")),
    )


def skill_mentions(t: dict[str, DataFrame]) -> DataFrame:
    extracted = (
        t["int_current_postings"]
        .where("extracted")
        .select("posting_id", F.from_json("extraction", SKILLS_JSON).alias("x"))
    )
    skills = extracted.select("posting_id", F.explode("x.skills").alias("s")).select(
        "posting_id", F.col("s.canonical").alias("skill"), F.col("s.status").alias("requirement")
    )
    alternatives = (
        extracted.select("posting_id", F.explode("x.skill_groups").alias("g"))
        .where(F.col("g.relation").isin("any_of", "at_least"))
        .select(
            "posting_id", F.col("g.status").alias("requirement"), F.explode("g.members").alias("m")
        )
        .select("posting_id", F.col("m.canonical").alias("skill"), "requirement")
        .distinct()
        .withColumn("alternative", F.lit(True))
    )
    return (
        skills.join(alternatives, ["posting_id", "skill", "requirement"], "left")
        .select(
            "posting_id",
            "skill",
            "requirement",
            F.when(F.col("alternative"), "alternative")
            .otherwise("individual")
            .alias("mention_kind"),
        )
        .distinct()
    )


def skill_demand(t: dict[str, DataFrame]) -> DataFrame:
    current = t["int_current_postings"].where(
        F.col("extracted") & F.col("availability_state").isin(*LISTED)
    )
    listed = current.select("posting_id", "opportunity_key", F.lit("all").alias("population"))
    listed = listed.unionByName(
        current.where("retained").select(
            "posting_id", "opportunity_key", F.lit("retained").alias("population")
        )
    )
    denominators = listed.groupBy("population").agg(F.count("*").alias("extracted_postings"))
    counts = (
        skill_mentions(t)
        .join(listed, "posting_id")
        .join(denominators, "population")
        .groupBy("skill", "requirement", "mention_kind", "population", "extracted_postings")
        .agg(
            F.countDistinct("posting_id").alias("postings"),
            F.countDistinct("opportunity_key").alias("opportunities"),
        )
    )
    return counts.select(
        "skill",
        "requirement",
        "mention_kind",
        "population",
        "postings",
        "opportunities",
        "extracted_postings",
        # The SQL literal 100.0 is a decimal, as in PostgreSQL; F.lit(100.0) would be a double
        # and could round differently. Spark keeps 14 decimal places in this division
        # (PostgreSQL at least 16), so rounding to one place gives the same digits.
        F.expr("round(100.0 * postings / nullif(extracted_postings, 0), 1)").alias(
            "share_of_extracted"
        ),
    )


def jobs_current(t: dict[str, DataFrame]) -> DataFrame:
    listed = t["int_current_postings"].where(
        F.col("retained") & F.col("availability_state").isin(*LISTED)
    )
    representative = first_per(listed, ["opportunity_key"], *newest_first())
    groups = (
        listed.groupBy("opportunity_key")
        .agg(
            F.count("*").alias("postings_in_group"),
            F.min("first_discovered_at").alias("group_first_discovered_at"),
        )
        .join(
            first_per(listed, ["opportunity_key"], *earliest_first()).select(
                "opportunity_key", F.col("cohort").alias("group_cohort")
            ),
            "opportunity_key",
        )
    )
    r = representative.alias("r")
    a = t["int_job_attributes"].alias("a")
    g = t["int_genai_mentions"].alias("g")
    v = t["stg_posting_versions"].alias("v")
    discovery_day = F.to_date("gr.group_first_discovered_at")
    return (
        r.join(groups.alias("gr"), F.col("gr.opportunity_key") == F.col("r.opportunity_key"))
        .join(a, F.col("a.result_id") == F.col("r.extraction_result_id"), "left")
        .join(g, F.col("g.version_id") == F.col("r.version_id"), "left")
        .join(v, F.col("v.version_id") == F.col("r.version_id"), "left")
        .select(
            F.col("r.opportunity_key").alias("job_key"),
            "r.posting_id",
            "r.employer_name",
            "r.title",
            F.coalesce("a.metro", F.lit("Not extracted")).alias("metro"),
            "a.city",
            F.coalesce("r.work_arrangement", F.lit("not_extracted")).alias("work_arrangement"),
            F.coalesce("r.employment_type", F.lit("not_extracted")).alias("employment_type"),
            F.coalesce("a.role_group", F.lit("Not extracted")).alias("role_group"),
            # A missing list gives '' in the SQL as well (array() of no elements).
            F.concat_ws(", ", F.from_json("r.role_categories", "ARRAY<STRING>")).alias(
                "role_categories"
            ),
            F.coalesce("a.level_ai", F.lit("Not extracted")).alias("level_ai"),
            F.coalesce("a.level_years", F.lit("Not extracted")).alias("level_years"),
            "a.overall_min_years",
            F.coalesce("a.minimum_degree", F.lit("Not extracted")).alias("minimum_degree"),
            "r.early_career",
            "r.seniority",
            "r.sponsorship",
            "r.work_authorization",
            "r.citizenship",
            "r.clearance",
            F.coalesce("g.genai_mention", F.lit(False)).alias("genai_mention"),
            # array_to_string(NULL) is NULL, but concat_ws(NULL) is ''.
            F.when(F.col("g.genai_terms").isNotNull(), F.concat_ws(", ", "g.genai_terms")).alias(
                "genai_terms"
            ),
            "r.extracted",
            F.col("gr.group_first_discovered_at").alias("first_discovered_at"),
            discovery_day.alias("discovery_day"),
            week_start(discovery_day).alias("discovery_week"),
            F.col("gr.group_cohort").alias("cohort"),
            "gr.postings_in_group",
            "v.posting_url",
            "r.as_of",
        )
    )


def classified(
    df: DataFrame, t: dict[str, DataFrame], result_id: str, version_id: str
) -> DataFrame:
    """Attach role group, metro, both levels, and the generative-AI flag, as the SQL's left
    joins on int_job_attributes and int_genai_mentions do."""
    attributes = t["int_job_attributes"].select(
        "result_id", "role_group", "metro", "level_ai", "level_years"
    )
    genai = t["int_genai_mentions"].select(
        F.col("version_id").alias("genai_version_id"), "genai_mention"
    )
    return (
        df.join(attributes, attributes["result_id"] == df[result_id], "left")
        .join(genai, genai["genai_version_id"] == df[version_id], "left")
        .withColumn("role_group", F.coalesce("role_group", F.lit("Not extracted")))
        .withColumn("metro", F.coalesce("metro", F.lit("Not extracted")))
        .withColumn("level_ai", F.coalesce("level_ai", F.lit("Not extracted")))
        .withColumn("level_years", F.coalesce("level_years", F.lit("Not extracted")))
        .withColumn("genai_mention", F.coalesce("genai_mention", F.lit(False)))
    )


def weekly_jobs(t: dict[str, DataFrame]) -> DataFrame:
    kept = t["int_current_postings"].where("retained")
    jobs = (
        kept.groupBy("opportunity_key")
        .agg(F.min("first_discovered_at").alias("first_discovered_at"))
        .join(
            first_per(kept, ["opportunity_key"], *earliest_first()).select(
                "opportunity_key", "cohort"
            ),
            "opportunity_key",
        )
        .join(
            first_per(kept, ["opportunity_key"], *newest_first()).select(
                "opportunity_key", "employer_id", "extraction_result_id", "version_id"
            ),
            "opportunity_key",
        )
    )
    new_jobs = classified(
        jobs.where(F.col("cohort") == "discovered"), t, "extraction_result_id", "version_id"
    ).select(
        week_start(F.to_date("first_discovered_at")).alias("week_start"),
        "opportunity_key",
        "employer_id",
        "role_group",
        "metro",
        "level_ai",
        "level_years",
        "genai_mention",
    )

    days = t["int_posting_days"]
    last_day = days.agg(F.max("report_day").alias("last_day"))
    week_ends = (
        t["int_reporting_days"]
        .crossJoin(last_day)
        .where(F.col("report_day") <= F.col("last_day"))
        .groupBy(week_start(F.col("report_day")).alias("week_start"))
        .agg(
            F.max("report_day").alias("report_day"),
            F.bool_and("is_complete").alias("week_complete"),
        )
    )
    listed_days = (
        days.where(F.col("retained") & F.col("availability_state").isin(*LISTED))
        .withColumn("opportunity_key", opportunity_key(days))
        .join(week_ends.select("week_start", "report_day"), "report_day")
        .join(t["stg_boards"].select("board_id", "employer_id"), "board_id")
    )
    open_jobs = classified(
        first_per(listed_days, ["week_start", "opportunity_key"], *newest_first()),
        t,
        "extraction_result_id",
        "version_id",
    ).select(
        "week_start",
        "opportunity_key",
        "employer_id",
        "role_group",
        "metro",
        "level_ai",
        "level_years",
        "genai_mention",
    )

    def rule(df: DataFrame, level_rule: str, kind: str) -> DataFrame:
        level = "level_ai" if level_rule == "ai" else "level_years"
        return df.select(
            "week_start",
            F.lit(level_rule).alias("level_rule"),
            F.col(level).alias("level"),
            "role_group",
            "metro",
            "opportunity_key",
            "employer_id",
            "genai_mention",
            F.lit(kind).alias("kind"),
        )

    by_rule = (
        rule(new_jobs, "ai", "new")
        .unionByName(rule(new_jobs, "years", "new"))
        .unionByName(rule(open_jobs, "ai", "open"))
        .unionByName(rule(open_jobs, "years", "open"))
    )
    new, opened = F.col("kind") == "new", F.col("kind") == "open"
    genai = F.col("genai_mention")
    # COUNT(DISTINCT x) FILTER (WHERE c) is countDistinct(when(c, x)): rows outside the filter
    # become NULL, which countDistinct ignores.
    return (
        by_rule.join(week_ends.select("week_start", "week_complete"), "week_start")
        .groupBy("week_start", "week_complete", "level_rule", "level", "role_group", "metro")
        .agg(
            F.countDistinct(F.when(new, F.col("opportunity_key"))).alias("new_jobs"),
            F.countDistinct(F.when(new, F.col("employer_id"))).alias("new_job_employers"),
            F.countDistinct(F.when(new & genai, F.col("opportunity_key"))).alias("genai_new_jobs"),
            F.countDistinct(F.when(opened, F.col("opportunity_key"))).alias("open_jobs_end"),
            F.countDistinct(F.when(opened & genai, F.col("opportunity_key"))).alias(
                "genai_open_jobs_end"
            ),
        )
    )


def differences(actual: DataFrame, expected: DataFrame, sample: int = 5) -> list[str]:
    """What separates two tables, compared as bags of rows (an empty list means identical).

    Values must be equal: numbers by value (dbt's numeric 33.3 equals Spark's decimal 33.3),
    timestamps as instants. The only rounding is the models' own (share_of_extracted to one
    decimal place, on both sides).
    """
    if actual.columns != expected.columns:
        return [f"columns differ: spark {actual.columns}, dbt {expected.columns}"]
    found = []
    for label, left, right in (
        ("only in dbt", expected, actual),
        ("only in spark", actual, expected),
    ):
        rows = left.exceptAll(right)
        count = rows.count()
        if count:
            shown = "; ".join(str(tuple(row)) for row in rows.limit(sample).collect())
            found.append(f"{count} rows {label}, e.g. {shown}")
    return found
