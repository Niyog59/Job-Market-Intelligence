# Databricks notebook source
# MAGIC %md
# MAGIC # Spark comparison on Databricks
# MAGIC
# MAGIC Runs the PySpark rebuild of `rpt_skill_demand`, `rpt_jobs_current`, and `rpt_weekly_jobs`
# MAGIC (`src/jobmarket/spark_models.py`, the same code as local mode) on serverless compute.
# MAGIC
# MAGIC Input: the directory `jobmarket spark export` writes (`inputs/` and `dbt/`), uploaded to
# MAGIC a Unity Catalog volume. Steps: `docs/spark/DATABRICKS.md`.
# MAGIC
# MAGIC Output: a parity check against dbt's results, then the same timings as local mode
# MAGIC (a scan of every input column; each model computed from Parquet and written back),
# MAGIC printed as JSON for `docs/spark/results/databricks.json`.

# COMMAND ----------

dbutils.widgets.text("data", "", "Volume directory of the export (/Volumes/...)")
dbutils.widgets.text("runs", "5", "Runs per measurement")

# COMMAND ----------

import json
import os
import sys
from datetime import UTC, datetime

# The notebook runs from docs/spark/ in the Git folder; the package is in src/.
sys.path.insert(0, os.path.abspath("../../src"))
from jobmarket import spark_models

spark.conf.set("spark.sql.session.timeZone", spark_models.TIMEZONE)
data = dbutils.widgets.get("data").rstrip("/")
runs = int(dbutils.widgets.get("runs"))
if not data.startswith("/Volumes/"):
    raise ValueError("Set the data widget to the volume directory, e.g. /Volumes/.../spark")

# COMMAND ----------

rebuilt = spark_models.build(spark_models.read_inputs(spark, data))
expected = spark_models.read_dbt(spark, data)
parity = {
    name: spark_models.differences(rebuilt[name], expected[name]) for name in spark_models.MODELS
}
for name, found in parity.items():
    print(f"{name}: {'match' if not found else found}")

# COMMAND ----------

timings = [spark_models.timed_run(spark, data, f"{data}/databricks_output") for _ in range(runs)]

# COMMAND ----------

result = {
    "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    "spark": spark.version,
    "compute": "serverless",
    "runs": runs,
    "rows": {
        name: spark.read.parquet(f"{data}/inputs/{name}.parquet").count()
        for name in spark_models.INPUTS
    },
    "parity": parity,
    "read": spark_models.stats([run["read"] for run in timings]),
    "models": {
        name: spark_models.stats([run["models"][name] for run in timings])
        for name in spark_models.MODELS
    },
    "models_total": spark_models.stats([sum(run["models"].values()) for run in timings]),
}
print(json.dumps(result, indent=2))
