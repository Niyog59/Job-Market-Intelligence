# Resource sizing for one t3.micro

Target: one EC2 t3.micro (2 vCPU burstable, 1 GiB memory) with a 20 GiB disk, running
PostgreSQL, the website, and Caddy in Docker, plus one command-line job at a time.

**Status: estimated from local measurements. Nothing has been measured on EC2 yet.**

## What was measured (2026-11-01, macOS, not Linux)

A copy of the real database was restored into a throwaway PostgreSQL 17 server configured
like `docker-compose.yml` (`shared_buffers=128MB`, `work_mem=4MB`,
`maintenance_work_mem=64MB`, `effective_cache_size=256MB`, `max_connections=20`). Peak
resident memory (RSS) per process:

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

Data sizes: database 107 MB after a fresh restore (311 MB on the development machine,
including bloat from rebuilds); ingestion backup 18.5 MB, private backup 31 kB; raw
responses about 460 MB for 4 days.

## Not measured

- Amazon Linux 2023 itself, the Docker daemon, and container overhead (estimated below).
- Caddy (its documentation and typical reports suggest tens of MB).
- A real collection over the network and a real extraction run.
- Linux memory accounting and page cache behavior, which differ from macOS RSS.

## Estimate

| Part | Estimate |
|---|---|
| OS and Docker | about 250 MB (estimate) |
| PostgreSQL | 190 to 250 MB |
| Website, 1 worker with 4 threads | 110 to 130 MB |
| Caddy | 30 to 50 MB |
| One command-line job | 150 MB |
| **Total** | **about 730 to 830 MB of 1,024 MB** |

That is tight but workable, with conditions:

1. **A 2 GiB swap file** (RUNBOOK step E1) so a short spike slows down instead of the
   kernel stopping a process. Constant swapping would make the site slow; check with
   `free -h` and `vmstat 5`.
2. **One command-line job at a time.** Collection, extraction, analytics builds, and
   backups each add 50 to 150 MB. Never start one while another is running.
3. **One website worker** (`GUNICORN_CMD_ARGS=--workers 1 --threads 4`). Each extra worker
   adds about 110 MB. Threads share one process; the pages are mostly database waits.
4. **Airflow does not fit.** Its documented minimum for the Docker Compose setup is 4 GB of
   memory. Scheduling is a later decision.

Disk (20 GiB): OS and Docker images about 4 to 5 GB, database under 1 GB, raw responses
about 0.5 GB (4 days kept), local backups about 19 MB each (not deleted automatically),
container logs capped at 30 MB per container. Check with `df -h` and `docker system df`.

## Settings that follow from this estimate

These values are recommendations from the measurements above. Change them in
`docker-compose.yml` or `.env` if the server shows otherwise.

| Setting | Value | Where |
|---|---|---|
| PostgreSQL `shared_buffers` / `work_mem` / `maintenance_work_mem` / `effective_cache_size` / `max_connections` | 128MB / 4MB / 64MB / 256MB / 20 | `docker-compose.yml` |
| Website processes | 1 worker, 4 threads | `GUNICORN_CMD_ARGS` in `.env` |
| Swap file | 2 GiB | RUNBOOK step E1 |
| Container log files | 10 MB, 3 files per container | `docker-compose.yml` |

## What to check after deploying

```bash
free -h                        # memory and swap in use
docker stats --no-stream       # memory per container
vmstat 5 5                     # "si"/"so" columns: swapping in/out (should stay near 0)
sudo dmesg | grep -i oom       # any process stopped for lack of memory
df -h /                        # disk
```

Record the results here, replacing the estimates.
