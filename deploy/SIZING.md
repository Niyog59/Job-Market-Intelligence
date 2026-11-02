# Resource sizing

The server today is one EC2 t3.micro (2 vCPU burstable, 1 GiB memory, 20 GiB disk) in
us-east-2. This report covers two stacks:

1. **The stack in this repository:** PostgreSQL, the website, and Caddy in Docker, with
   collection, extraction, analytics builds, and backups started by hand, one at a time.
2. **The planned stack with scheduling:** the same plus Apache Airflow running those jobs on
   a schedule. Airflow is not implemented yet.

**Status: nothing here has been measured on EC2 or on Linux.** All measurements were taken
on macOS against a restored copy of the real data; everything else is an estimate. No
instance size has been confirmed.

## Measurements (2026-11-01, macOS)

### Application (from a restored copy of the data)

PostgreSQL ran with the settings in `docker-compose.yml` (`shared_buffers=128MB`,
`work_mem=4MB`, `maintenance_work_mem=64MB`, `effective_cache_size=256MB`,
`max_connections=20`). Values are peak resident memory (max RSS) while working.

| Process | Peak memory | Notes |
|---|---|---|
| PostgreSQL (all processes) | 189 MB | during an analytics build and website load at the same time |
| Website (one process) | 113 MB | 180 requests, 97 ms average |
| gunicorn loading the app | 109 MB | one worker |
| `jobmarket analytics build` (dbt) | 141 MB | 6.8 s, 138 models and tests |
| `jobmarket replay` of 318 saved scans | 150 MB | 70 s; stands in for a collection run |
| Extraction code loaded | 61 MB | no AI calls were made |
| `jobmarket migrate` | 42 MB | |
| `jobmarket backup restore` | 46 MB | 6.5 s for 18.5 MB of dumps |

Data sizes: database 107 MB after a fresh restore; ingestion backup 18.5 MB, private
backup 31 kB; raw responses about 460 MB for 4 days (about 110 files a day).

### Apache Airflow (throwaway installation, removed afterwards)

Airflow 3.3.2 with the LocalExecutor, `parallelism=1`, one API server worker, its metadata
in a separate PostgreSQL 17 database, and one test DAG whose only task runs `sleep 45`.
Values are physical footprint, which includes memory macOS has compressed. (macOS
compresses idle memory, so `ps` badly undercounts idle services: the scheduler showed 52 MB
in `ps` and 144 MB in footprint at the same moment.)

| Component | Memory | Notes |
|---|---|---|
| Scheduler | 368 MB | idle, after one task had run (264 MB before any task) |
| DAG processor | 153 MB | idle |
| API server (UI and task API), 1 worker | 255 MB | idle |
| Triggerer | 311 MB | only needed for deferrable tasks |
| A running task, extra | about 110 MB | scheduler tree peaked at 478 MB during the task |
| CPU, all components idle | 4.6% of one core | over 60 s |

Without the triggerer, idle Airflow alone measured 776 MB; with it, 1,087 MB.

## What these measurements do not show

- **Linux and Amazon Linux 2023.** Memory accounting, shared libraries, and the page cache
  differ from macOS. The OS and Docker overhead below is an estimate.
- **Our real pipeline under Airflow.** The Airflow task only waited. A real task would run
  `jobmarket collect`, `extract run`, `analytics build`, or `backup create` inside Airflow's
  task process; those costs were measured separately, never together.
- **Real collection and extraction.** Collection was measured as a replay of saved
  responses, without network requests; extraction only loaded its code and made no Gemini
  calls.
- **PostgreSQL serving Airflow too.** Airflow opens its own database connections; the
  current `max_connections=20` has not been tested with it and is likely too low.
- **Caddy, sustained traffic, and long-running behavior** (memory growth over days).

## Estimates

| Part | Stack in this repo | With Airflow (no triggerer) |
|---|---|---|
| OS and Docker | 250 MB (estimate) | 250 MB (estimate) |
| PostgreSQL | 190 to 250 MB | 240 to 350 MB (extra for Airflow, estimate) |
| Website, 1 worker with 4 threads | 110 to 130 MB | 110 to 130 MB |
| Caddy | 30 to 50 MB (estimate) | 30 to 50 MB (estimate) |
| Airflow, idle | none | 776 MB |
| **Idle total** | **about 580 to 680 MB** | **about 1.41 to 1.56 GB** |
| One job running | +150 MB | +150 MB job, +110 MB Airflow task |
| **Peak total** | **about 730 to 830 MB** | **about 1.67 to 1.82 GB** |

With the triggerer, add about 310 MB to the Airflow column.

## What this means

- **Stack in this repo on t3.micro (1 GiB):** tight but expected to work with a 2 GiB swap
  file, one website worker, and one job at a time. Unconfirmed until measured on the server.
- **With Airflow, t3.micro does not fit:** idle Airflow alone (776 MB) uses most of 1 GiB.
- **t3.small (2 GiB) is a candidate, not a confirmed fit.** By the estimate above it leaves
  roughly 100 to 380 MB of its 2,048 MB at peak (less after the kernel's own share), only without the triggerer, with one task at a time, and
  with swap as a safety net. The estimate rests on macOS numbers and a task that only
  waited, so it does not prove that the real pipeline fits reliably. It has to be tested
  with the full stack before it is relied on (see below).
- **t3.medium (4 GiB)** is the next step up if a 2 GiB test shows too little headroom.

## Candidate sizes and monthly cost before credits (us-east-2)

From the AWS public price list (published 2026-10-29), on-demand Linux, 730 hours a
month. Every option also pays for the 20 GiB gp3 disk ($0.08 per GB-month: $1.60), one
public IPv4 address ($0.005 per hour: $3.65), and S3 (about $0.15: roughly 3.5 GB of raw
responses kept 30 days, backups, and requests). A gp2 disk would be $2.00 instead of $1.60.

| Instance | Memory | EC2 | Total | Notes |
|---|---|---|---|---|
| t3.micro (current) | 1 GiB | $7.59 | **$13.00** | not enough for Airflow |
| t3.small | 2 GiB | $15.18 | **$20.58** | same x86 image; the existing instance can be resized in place |
| t4g.small | 2 GiB | $12.26 | **$17.66** | ARM: needs a new instance and an arm64 image, not built or tested |
| t3.medium | 4 GiB | $30.37 | **$35.77** | comfortable headroom |

t3 instances default to "unlimited" CPU credits: sustained CPU above the baseline would add
charges. The idle measurement (4.6% of one core) is far below the baseline, but long
extraction or build runs have not been measured on EC2. Instance types available under an
AWS free-plan account are limited; check eligibility in the console before choosing.

No instance has been resized and no spending has changed.

## What would confirm a size

Run the full stack on Linux with the target memory, for example on the candidate instance
or in containers limited to that memory:

1. Airflow (as it will actually be configured) running the real tasks: a collection, an
   extraction batch, `analytics build`, and a backup, one after another.
2. The website under light traffic at the same time.
3. Record peak and idle memory (`free -h`, `docker stats`), swap activity (`vmstat 5`, the
   `si`/`so` columns should stay near 0), any processes stopped for lack of memory
   (`sudo dmesg | grep -i oom`), and the CPU credit balance in CloudWatch.
4. Repeat over several days of the schedule before calling the size a fit.

## Settings that follow from the current estimate

| Setting | Value | Where |
|---|---|---|
| PostgreSQL `shared_buffers` / `work_mem` / `maintenance_work_mem` / `effective_cache_size` / `max_connections` | 128MB / 4MB / 64MB / 256MB / 20 | `docker-compose.yml` |
| Website processes | 1 worker, 4 threads | `GUNICORN_CMD_ARGS` in `.env` |
| Swap file | 2 GiB | RUNBOOK step E1 |
| Container log files | 10 MB, 3 files per container | `docker-compose.yml` |

These were chosen for the stack in this repository. Adding Airflow will need at least a
higher `max_connections` and a new measurement.

## Checks on the server

```bash
free -h                        # memory and swap in use
docker stats --no-stream       # memory per container
vmstat 5 5                     # "si"/"so" columns: swapping in/out (should stay near 0)
sudo dmesg | grep -i oom       # any process stopped for lack of memory
df -h /                        # disk
```

Record the results here, replacing the estimates.
