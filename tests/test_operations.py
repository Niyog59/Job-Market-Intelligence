"""Pipeline step records and step alerts (jobmarket.operations): one alert per incident, one
recovery notice, and no duplicate messages when a step is re-run."""

from datetime import UTC, datetime, timedelta

import pytest

from jobmarket.migrate import apply_migrations
from jobmarket.operations import (
    last_step_run,
    open_incidents,
    queue_notification,
    record_step,
)

pytestmark = pytest.mark.db
T0 = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)


@pytest.fixture
def conn(schema_conn):
    apply_migrations(schema_conn)
    return schema_conn


def outbox(conn):
    return conn.execute(
        "SELECT kind, subject FROM notifications ORDER BY notification_id"
    ).fetchall()


def test_first_failure_alerts_once_and_success_recovers_once(conn):
    first = record_step(conn, "backup", T0, T0, "failed", error="S3 upload failed: denied")
    assert first.alert_notification_id is not None
    again = record_step(conn, "backup", T0 + timedelta(days=1), T0 + timedelta(days=1),
                        "failed", error="still denied")  # fmt: skip
    assert again.alert_notification_id is None
    assert [(s, f) for s, _, f, _ in open_incidents(conn)] == [("backup", 2)]

    ok = record_step(conn, "backup", T0 + timedelta(days=2), T0 + timedelta(days=2), "succeeded")
    assert ok.recovery_notification_id is not None
    later = record_step(conn, "backup", T0 + timedelta(days=3), T0 + timedelta(days=3), "succeeded")
    assert later.recovery_notification_id is None
    assert outbox(conn) == [("alert", "[Job Pipeline] Backup failed"),
                            ("recovery", "[Job Pipeline] Backup recovered")]  # fmt: skip
    assert open_incidents(conn) == []


def test_each_alerting_step_has_its_own_incident(conn):
    record_step(conn, "backup", T0, T0, "failed", error="disk full")
    record_step(conn, "report_publish", T0, T0, "failed", error="dbt test failed")
    assert {s for s, *_ in open_incidents(conn)} == {"backup", "report_publish"}
    assert len(outbox(conn)) == 2


def test_non_alerting_steps_and_skips_never_alert(conn):
    record_step(conn, "raw_sync", T0, T0, "failed", error="network")
    record_step(conn, "backup", T0, T0, "skipped")
    assert outbox(conn) == []
    assert last_step_run(conn, "raw_sync")[1] == "failed"
    assert last_step_run(conn, "raw_sync", "succeeded") is None


def test_dedup_key_survives_restarts(conn):
    first = queue_notification(conn, "summary", "s", "b", T0, dedup_key="summary:2026-11-01")
    second = queue_notification(conn, "summary", "s", "b", T0, dedup_key="summary:2026-11-01")
    assert first is not None and second is None
    assert len(outbox(conn)) == 1


def test_step_records_are_validated(conn):
    with pytest.raises(ValueError):
        record_step(conn, "backup", T0, T0, "failed")
    with pytest.raises(ValueError):
        record_step(conn, "deploy", T0, T0, "succeeded")
