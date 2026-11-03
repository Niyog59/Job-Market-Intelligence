# Running the comparison on Databricks

`docs/spark/databricks_notebook.py` runs the same PySpark code as local mode
(`src/jobmarket/spark_models.py`) on Databricks Free Edition. It checks Spark's result
against dbt's, then times the read and the three models.

## Before you start

- A Databricks Free Edition account.
- An export from your own database: `jobmarket spark export` writes `data/spark/inputs/`
  (seven Parquet files) and `data/spark/dbt/` (three). Only public analytics data is in it:
  job listings, employers, and the AI-extracted requirements. Nothing from the private schema.
- Uploading the export copies that data into your Databricks workspace. Delete it there when
  you are done if you do not want to keep it.

## Limits that matter here

| Limit | Effect on this run |
|---|---|
| Serverless compute only; no cluster settings | The cluster size is chosen by Databricks and is not reported. Timings are for "Free Edition serverless", not a known machine. |
| DataFrame and SQL caching are not supported on serverless | The notebook caches nothing; local mode does the same, so the numbers compare. |
| Session time zone defaults to UTC | The notebook sets `spark.sql.session.timeZone` to America/Chicago; the code refuses to run otherwise. |
| Fair-use quota per day | If it is exceeded, compute stops for the rest of the day. One run of this notebook is small. |
| UI uploads to a volume: 5 GB per file | The export at today's size is far below that. |

Current details: [Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations),
[serverless limitations](https://docs.databricks.com/aws/en/compute/serverless/limitations),
[files in volumes](https://docs.databricks.com/aws/en/volumes/volume-files). Check them before
a run; they change.

## Steps

1. **Add the repository.** Workspace, Create, Git folder. URL:
   `https://github.com/Niyog59/Job-Market-Intelligence` (public, so no credentials). Pick the
   branch that has `docs/spark/databricks_notebook.py`.
2. **Create a volume.** Catalog, open the workspace's catalog and its `default` schema,
   Create, Volume, managed, named `jobmarket`. Its path is
   `/Volumes/<catalog>/default/jobmarket`; the volume's page shows it.
3. **Upload the export.** Open the volume, create a directory `spark` with two directories
   inside it, `inputs` and `dbt`. Upload the seven files of `data/spark/inputs/` into
   `spark/inputs` and the three of `data/spark/dbt/` into `spark/dbt` (Upload to this volume).
4. **Run the notebook.** Open `docs/spark/databricks_notebook` in the Git folder and connect it
   to Serverless. Set the widgets: `data` to `/Volumes/<catalog>/default/jobmarket/spark`,
   `runs` to `5`. Run all.
5. **Read the result.**
   - The parity cell prints `match` for each model, or the rows that differ.
   - The last cell prints the timings as JSON. Copy them into
     `docs/spark/results/databricks.json` in the repository.
   - Each cell also shows its own run time, and its Spark jobs link to the query profile,
     for a closer look at where the time went.
6. **Clean up (optional).** Delete `spark/` from the volume, or the volume itself.

## What the timings mean

| Field | What it measures |
|---|---|
| `read` | A scan of every column of every input file. |
| `models` | Each model computed from the Parquet files and written back to the volume (reading its inputs again, since nothing is cached). |
| `models_total` | The three models together, per run. |

There is no session startup time: the notebook attaches to compute that Databricks has already
started. All values are seconds; `median`, `min`, and `max` are over `runs`.
