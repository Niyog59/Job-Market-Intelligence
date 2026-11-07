"""Rules the Compose file must keep for the scheduled pipeline (read as YAML; nothing runs)."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def env(service) -> dict:
    return service.get("environment") or {}


def test_one_task_at_a_time(services):
    for name in ("airflow-scheduler", "airflow-dag-processor", "airflow-api"):
        assert env(services[name])["AIRFLOW__CORE__PARALLELISM"] == "1", name
        assert env(services[name])["AIRFLOW__CORE__EXECUTOR"] == "LocalExecutor", name


def test_airflow_ui_is_loopback_only_and_nothing_else_is_published(services):
    assert services["airflow-api"]["ports"] == ["127.0.0.1:8080:8080"]
    published = {name for name, s in services.items() if s.get("ports")}
    assert published == {"caddy", "airflow-api"}


def test_airflow_has_its_own_database_login(services):
    conn = env(services["airflow-scheduler"])["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"]
    assert conn.startswith("postgresql+psycopg2://airflow:") and conn.endswith("@db:5432/airflow")


def test_only_the_scheduler_gets_the_application_settings(services):
    assert services["airflow-scheduler"].get("env_file") == ".env"
    for name in ("airflow-dag-processor", "airflow-api", "web"):
        assert "env_file" not in services[name], name
    assert "AIRFLOW__CORE__FERNET_KEY" not in env(services["web"])


def test_database_allows_airflow_connections(services):
    command = services["db"]["command"]
    assert "max_connections=40" in command


def test_everything_restarts_after_a_reboot(services):
    for name in ("db", "web", "caddy", "airflow-scheduler", "airflow-dag-processor",
                 "airflow-api"):  # fmt: skip
        assert services[name]["restart"] == "unless-stopped", name


def test_the_sheet_key_is_mounted_read_only_where_tasks_run(services):
    for name in ("airflow-scheduler", "jobs"):
        assert "./secrets:/app/secrets:ro" in services[name]["volumes"], name
    for name in ("web", "airflow-api", "airflow-dag-processor", "caddy", "db"):
        assert "./secrets:/app/secrets:ro" not in (services[name].get("volumes") or []), name


def test_the_public_numbers_file_is_shared_read_only_with_the_website(services):
    # Publication (scheduler, or a manual jobs run) writes profile-stats.json; the website
    # only reads it and sees nothing else of the application's data.
    assert services["web"]["volumes"] == ["publicdata:/app/public:ro"]
    for name in ("airflow-scheduler", "jobs"):
        assert "publicdata:/app/public" in services[name]["volumes"], name
    for name in ("web", "airflow-scheduler", "jobs"):
        assert env(services[name])["JOBMARKET_PUBLIC_DIR"] == "/app/public", name
