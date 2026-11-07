# One application image for the website (gunicorn), the command-line jobs
# (docker compose run --rm jobs jobmarket ...), and the Airflow scheduler that runs those
# jobs on a schedule. Build from the repository root:
#   docker compose -f deploy/docker-compose.yml build
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JOBMARKET_PG_BIN=/usr/lib/postgresql/17/bin

# pg_dump must have the server's major version (17); Debian bookworm ships 15, so the client
# comes from the PostgreSQL project's apt repository, checked against its signing key.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
WORKDIR /app

# Editable install: the analytics command finds the dbt project next to src/ (/app/analytics).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[analytics,web,aws]"

# Apache Airflow in its own virtual environment, installed with the project's official
# constraints file: its pinned dependencies must not change the app's (dbt pins many of the
# same libraries). Its commands are reached through PATH (after the app's, so `python` and
# `jobmarket` stay the app's).
ARG AIRFLOW_VERSION=3.3.2
RUN python -m venv /opt/airflow \
 && /opt/airflow/bin/pip install "apache-airflow[postgres]==${AIRFLOW_VERSION}" \
      --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-3.11.txt"
ENV PATH="${PATH}:/opt/airflow/bin" \
    AIRFLOW_HOME=/app/airflow

COPY analytics ./analytics
COPY config ./config
COPY deploy/airflow/dags ./deploy/airflow/dags
# The app user writes only raw responses and backups (data/), dbt's own output (analytics/),
# Airflow's logs and generated files (airflow/), and the public numbers file (public/; a new
# named volume takes this directory's owner).
RUN mkdir -p data/raw data/backups airflow public \
 && chown -R app:app data analytics airflow public

USER app
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "jobmarket.web.wsgi:app"]
