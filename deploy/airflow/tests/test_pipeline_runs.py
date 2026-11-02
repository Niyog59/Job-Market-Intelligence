"""Whole DAG runs inside Airflow (dag.test) with a stub `jobmarket` command that records
its calls and exits with chosen codes. Checks the order of steps, evening-only steps,
skips for "busy" and "not configured", and that a failure never stops later steps.
Nothing touches a database, S3, or email."""

import importlib.util
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("airflow", reason="Airflow is not installed")

import pendulum  # noqa: E402

DAGS = Path(__file__).resolve().parents[1] / "dags" / "jobmarket.py"
STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
key=$(echo "$1" | tr '-' '_')
eval "code=\\${STUB_EXIT_$key:-0}"
exit "$code"
"""


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("jobmarket", "airflow"):
        path = bin_dir / name
        path.write_text(STUB)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("JOBMARKET_WORKDIR", str(tmp_path))
    (tmp_path / "logs").mkdir()
    return log


def load():
    spec = importlib.util.spec_from_file_location("jobmarket_dags_run", DAGS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(dag, when):
    dag_run = dag.test(run_after=when, logical_date=when)
    return {ti.task_id: str(ti.state) for ti in dag_run.get_task_instances()}


def calls(log):
    return [line.split(" ", 1)[1] if line.startswith("jobmarket ") else line
            for line in log.read_text().splitlines()]  # fmt: skip


EVENING = pendulum.datetime(2026, 11, 2, 21, tz="America/Chicago")
NOON = pendulum.datetime(2026, 11, 2, 12, tz="America/Chicago")


def test_evening_run_does_every_step_in_order(stubs):
    states = run(load().collection, EVENING)
    assert set(states.values()) == {"success"}
    assert calls(stubs)[:8] == [
        "collect --scheduled",
        "backup create --upload",
        "backup prune --keep 3",
        "sync-raw",
        "cleanup-raw --require-s3",
        "analytics publish",
        "summary send",
        "notifications --send",
    ]
    assert calls(stubs)[8].startswith("airflow db clean --yes --skip-archive")


def test_noon_run_skips_the_evening_only_steps(stubs):
    states = run(load().collection, NOON)
    skipped = {task for task, state in states.items() if state == "skipped"}
    assert skipped == {"backup", "backup_prune", "daily_summary", "airflow_history_cleanup"}
    assert calls(stubs) == ["collect --scheduled", "sync-raw", "cleanup-raw --require-s3",
                            "analytics publish", "notifications --send"]  # fmt: skip


def test_a_failed_collection_still_backs_up_publishes_and_reports(stubs, monkeypatch):
    monkeypatch.setenv("STUB_EXIT_collect", "1")  # every board failed
    states = run(load().collection, EVENING)
    assert states["collect"] == "failed"
    for task in ("backup", "publish_reports", "daily_summary", "send_email"):
        assert states[task] == "success", task


def test_a_busy_collection_is_skipped_not_failed(stubs, monkeypatch):
    monkeypatch.setenv("STUB_EXIT_collect", "3")  # a manual collection holds the lock
    states = run(load().collection, EVENING)
    assert states["collect"] == "skipped"
    assert states["daily_summary"] == "success"


def test_a_failed_backup_does_not_stop_reports_or_the_summary(stubs, monkeypatch):
    monkeypatch.setenv("STUB_EXIT_backup", "1")
    states = run(load().collection, EVENING)
    assert states["backup"] == "failed" and states["backup_prune"] == "failed"
    assert states["publish_reports"] == "success" and states["daily_summary"] == "success"


def test_retry_check_then_email(stubs):
    states = run(load().retries, NOON)
    assert set(states.values()) == {"success"}
    assert calls(stubs) == ["retry-due", "notifications --send"]


def test_ai_batch_is_bounded_and_skips_until_configured(stubs, monkeypatch):
    assert set(run(load().ai, NOON).values()) == {"success"}
    assert calls(stubs) == ["extract run --limit 20"]
    monkeypatch.setenv("STUB_EXIT_extract", "2")  # no Gemini key or limits yet
    assert set(run(load().ai, NOON).values()) == {"skipped"}
