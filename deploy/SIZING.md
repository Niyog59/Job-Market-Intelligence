# Resource sizing

The server today is one EC2 t3.micro (2 vCPU burstable, 1 GiB memory, 20 GiB disk) in
us-east-2. The full stack is PostgreSQL, the website, Caddy, and Airflow (scheduler, DAG
processor, API server) running the pipeline one task at a time.

**Status: nothing here has been measured on EC2.** There are three kinds of evidence below,
labeled every time: measured on macOS with the real data, measured on Linux in CI with
almost no data, and estimates. No instance size has been confirmed, and none has been
changed.

## 1. Full stack with the real pipeline, macOS (2026-11-01)

A local run of the actual DAGs under a real Airflow 3.3.2 scheduler (LocalExecutor,
parallelism 1, one API worker), against a restored copy of the real database on a
PostgreSQL 17 server with the settings in `docker-compose.yml`:
- **Real work:** collection over the network from all 51 enabled boards (twice: the
  first-start catch-up run and the 9 PM run), one real Gemini batch of 20 listings, report
  publication, and a backup.
- **Website:** answering about two requests a second the whole time; all 318 requests
  succeeded.
- **Mocked:** S3 was a local moto server (backup and 77 raw files uploaded and confirmed),
  and email was not configured.
- **Method:** values are physical footprint, which includes memory macOS compressed,
  sampled every 5 seconds over the 6.8 minutes of pipeline work.

| Component | Peak | Median | Notes |
|---|---|---|---|
| Airflow scheduler (with the running task) | 640 MB | 544 MB | 373 MB between tasks |
| PostgreSQL (all processes) | 298 MB | 188 MB | peak during the backup |
| Airflow API server, 1 worker | 256 MB | 256 MB | |
| Airflow DAG processor | 155 MB | 155 MB | |
| Website | 51 MB | 50 MB | development server, under light load |
| **Total at one moment** | **1,362 MB** | **1,168 MB** | lowest 914 MB |

Peak total while each task ran:

| Task | Duration | Peak total |
|---|---|---|
| Collection (network, 51 boards) | 89 to 101 s | 1,325 to 1,362 MB |
| Report publication | 8 to 9 s | 1,345 MB |
| Backup (20.4 MB of dumps, upload confirmed) | 6 s | 1,285 MB |
| AI batch (20 real Gemini requests) | 90 s | 1,236 MB |
| Raw upload and cleanup, retry check, email | 1 to 2 s each | 1,038 to 1,135 MB |

CPU over the run averaged 5.5% of one core.

## 2. Full stack in the container images, Linux (CI, every push)

The CI `deploy` job builds the real image, starts the Compose stack, and runs the
collection, retry, and extraction DAGs through Airflow (`airflow dags test`) while the
website serves requests. It records `docker stats` (sampled about every 2 seconds) in the
job summary. Run of 2026-11-02 (GitHub runner with 16 GB, so no memory pressure):

| Container | Peak MiB | Median MiB |
|---|---|---|
| airflow-scheduler | 526 | 410 |
| airflow-api | 200 | 200 |
| airflow-dag-processor | 150 | 150 |
| db | 63 | 58 |
| web (gunicorn, 1 worker) | 54 | 54 |
| caddy | 23 | 19 |
| moto (fake S3, CI only) | 62 | 61 |
| **Total at one moment** | **1,072** (about 1,010 without moto) | |

This is Linux and the real containers, but the database held almost nothing (no job boards
are loaded in CI, so collection had no work), and only about 8 samples were taken.

## What these measurements do not show

- **EC2 and Amazon Linux 2023:** the OS, the Docker daemon, and the kernel's own memory.
- **Linux with the real data:** the Linux run had an empty database; the real-data run was
  on macOS, where memory is counted differently.
- **Memory pressure:** both machines had plenty of free memory; behavior near a 2 GiB limit
  (page cache eviction, swapping) has not been observed.
- **Long running:** days of schedules, memory growth over time, a full AI quota day.
- **Email and real S3:** delivery through Gmail and uploads to a real bucket.

## Estimate for EC2 with the full stack

| Part | Estimate |
|---|---|
| Amazon Linux 2023 and Docker | about 250 MB (estimate, not measured) |
| PostgreSQL with the real data | 190 to 300 MB (macOS peak 298) |
| Airflow API server and DAG processor | 350 to 410 MB (Linux 350, macOS 411) |
| Airflow scheduler between tasks | 370 to 410 MB (macOS 373; Linux median 410 includes tasks) |
| Website | 55 to 130 MB (one worker) |
| Caddy | about 25 MB (Linux 23) |
| **Between tasks** | **about 1.24 to 1.53 GB** |
| A running task (Airflow task process plus the job) | +150 to 270 MB (macOS scheduler peak 640 vs 373) |
| **Peak** | **about 1.4 to 1.8 GB** |

## What this means

- **t3.micro (1 GiB) cannot run the full stack.** The stack measured 914 MB at its lowest
  on macOS, about 1 GB on Linux with an empty database, and has to share the 1 GiB with the
  OS and Docker. Swap would be in constant use. Without Airflow (jobs started by hand) the
  earlier estimate of 730 to 830 MB still applies.
- **t3.small (2 GiB) is a candidate, not a confirmed fit.** By the estimate it keeps roughly
  250 to 650 MB free at peak, with the 2 GiB swap file as a safety net. That margin rests on
  macOS numbers and a Linux run without data; it has to be measured on the instance itself
  (below) before it is relied on.
- **t3.medium (4 GiB)** is the step up if the 2 GiB measurement shows too little headroom or
  regular swapping.

## Candidate sizes and monthly cost before credits (us-east-2)

From the AWS public price list (published 2026-10-29), on-demand Linux, 730 hours a
month. Every option also pays for the 20 GiB gp3 disk ($0.08 per GB-month: $1.60), one
public IPv4 address ($0.005 per hour: $3.65), and S3 (about $0.15: roughly 3.5 GB of raw
responses kept 30 days, backups, and requests). A gp2 disk would be $2.00 instead of $1.60.

| Instance | Memory | EC2 | Total | Notes |
|---|---|---|---|---|
| t3.micro (current) | 1 GiB | $7.59 | **$13.00** | too small for the full stack |
| t3.small | 2 GiB | $15.18 | **$20.58** | same x86 image; the existing instance can be resized in place |
| t4g.small | 2 GiB | $12.26 | **$17.66** | ARM: needs a new instance and an arm64 image, not built or tested |
| t3.medium | 4 GiB | $30.37 | **$35.77** | comfortable headroom |

t3 instances default to "unlimited" CPU credits: sustained CPU above the baseline would add
charges. Measured CPU (4.6% of one core idle, 5.5% during the pipeline run) is far below a
t3.small's baseline, but a full day on EC2 has not been measured. Instance types available
under an AWS free-plan account are limited; check eligibility in the console before
choosing.

No instance has been resized and no spending has changed.

## What would confirm a size

On the candidate instance, with the full stack running its normal schedule (RUNBOOK
section G):

1. Watch at least one 9 PM run (collection, backup, publication, summary) and several hourly
   AI batches, with the website in use.
2. Record peak and idle memory (`free -h`, `docker stats --no-stream`), swapping (`vmstat 5`:
   the `si`/`so` columns should stay near 0), processes stopped for lack of memory
   (`sudo dmesg | grep -i oom`), and the CPU credit balance in CloudWatch.
3. Repeat over several days before calling the size a fit, then record the numbers here.

## Settings that follow from the current evidence

| Setting | Value | Where |
|---|---|---|
| PostgreSQL `shared_buffers` / `work_mem` / `maintenance_work_mem` / `effective_cache_size` | 128MB / 4MB / 64MB / 256MB | `docker-compose.yml` |
| PostgreSQL `max_connections` | 40 (Airflow holds a few per component) | `docker-compose.yml` |
| Airflow pools per component | 2, up to 3 more on demand | `docker-compose.yml` |
| Airflow tasks at once | 1 (`PARALLELISM=1`) | `docker-compose.yml` |
| Airflow API server workers | 1 | `docker-compose.yml` |
| Website processes | 1 worker, 4 threads | `GUNICORN_CMD_ARGS` in `.env` |
| Swap file | 2 GiB | RUNBOOK step E1 |
| Container log files | 10 MB, 3 files per container | `docker-compose.yml` |

## Checks on the server

```bash
free -h                        # memory and swap in use
docker stats --no-stream       # memory per container
vmstat 5 5                     # "si"/"so" columns: swapping in/out (should stay near 0)
sudo dmesg | grep -i oom       # any process stopped for lack of memory
df -h /                        # disk
```
