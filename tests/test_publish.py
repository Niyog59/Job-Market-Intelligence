"""Building reports in shadow schemas and publishing them atomically
(jobmarket.analytics.build_and_publish), over the synthetic history in search_world."""

import pytest

from jobmarket import analytics, db
from jobmarket.search import site_status

pytestmark = pytest.mark.db


@pytest.fixture
def owner(world, db_settings):
    with db.connect(db_settings, autocommit=True) as conn:
        yield conn


def built_at(conn, world):
    return conn.execute(
        f'SELECT built_at FROM "{world.source}_analytics_reporting".rpt_build_info'
    ).fetchone()[0]


def schemas(conn, world, kind):
    rows = conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s ORDER BY 1",
        (f"{world.source}_{kind}%",),
    ).fetchall()
    return [name for (name,) in rows]


def publish(owner, world):
    return analytics.build_and_publish(owner, target="owner", source_schema=world.source,
                                       prefix=f"{world.source}_")  # fmt: skip


def test_publish_replaces_every_layer_and_leaves_no_shadow(owner, world):
    first = publish(owner, world)
    assert first.published and first.failures == []
    second = publish(owner, world)
    assert second.published and second.built_at > first.built_at
    assert built_at(owner, world) == second.built_at
    assert schemas(owner, world, "next__") == [] and schemas(owner, world, "old__") == []
    # Job search reads the swapped-in views, which read the swapped-in tables.
    status = site_status(owner, second.built_at, world.schema)
    assert status.listings > 0


def test_a_failed_build_keeps_the_live_reports_and_job_search(owner, world, monkeypatch):
    assert publish(owner, world).published
    before = built_at(owner, world)
    real = analytics.run_dbt

    def failing(*args, **kwargs):
        real(*args, **kwargs)  # the shadow schemas really get built
        return analytics.DbtOutcome(False, [], ["rpt_daily_activity: fail 1 row"])

    monkeypatch.setattr(analytics, "run_dbt", failing)
    result = publish(owner, world)
    assert not result.published
    assert result.failures == ["rpt_daily_activity: fail 1 row"]
    assert built_at(owner, world) == before
    assert schemas(owner, world, "next__") == []
    assert site_status(owner, before, world.schema).listings > 0


def test_a_failed_validation_publishes_nothing(owner, world, monkeypatch):
    assert publish(owner, world).published
    before = built_at(owner, world)
    monkeypatch.setattr(analytics, "validate_build", lambda conn, prefix: ["srv_postings broken"])
    result = publish(owner, world)
    assert not result.published and result.failures == ["srv_postings broken"]
    assert built_at(owner, world) == before


def test_validation_catches_a_missing_build_record(owner, world, monkeypatch):
    real = analytics.run_dbt

    def build_then_break(*args, **kwargs):
        outcome = real(*args, **kwargs)
        owner.execute(f'DELETE FROM "{world.source}_next__analytics_reporting".rpt_build_info')
        return outcome

    monkeypatch.setattr(analytics, "run_dbt", build_then_break)
    result = publish(owner, world)
    assert result.failures == ["rpt_build_info has 0 rows, expected 1"]


def test_leftovers_of_an_interrupted_attempt_are_cleared(owner, world):
    owner.execute(f'CREATE SCHEMA IF NOT EXISTS "{world.source}_next__analytics_reporting"')
    owner.execute(f'CREATE SCHEMA IF NOT EXISTS "{world.source}_old__analytics_serving"')
    assert publish(owner, world).published
    assert schemas(owner, world, "next__") == [] and schemas(owner, world, "old__") == []


def test_only_one_publish_at_a_time(owner, world, db_settings):
    with db.connect(db_settings, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (analytics.PUBLISH_LOCK_KEY,))
        with pytest.raises(analytics.PublishBusy):
            publish(owner, world)
        other.execute("SELECT pg_advisory_unlock(%s)", (analytics.PUBLISH_LOCK_KEY,))


def test_a_reader_never_blocks_the_swap(owner, world, db_settings):
    """A reader with an open transaction on the old build: the swap still happens at once,
    the old schemas are left for the next run, and nobody waits."""
    with db.connect(db_settings) as reader:  # not autocommit: keeps its locks until it ends
        reader.execute(f'SELECT count(*) FROM "{world.schema}".srv_postings').fetchone()
        result = analytics.build_and_publish(
            owner, target="owner", source_schema=world.source, prefix=f"{world.source}_",
            lock_timeout="1s",
        )  # fmt: skip
        assert result.published
        assert f"{world.source}_old__analytics_serving" in result.retired_left
        # A new reader sees the new build immediately.
        with db.connect(db_settings, autocommit=True) as fresh:
            assert built_at(fresh, world) == result.built_at
    # Once the reader is gone, the next run removes the leftovers.
    assert publish(owner, world).published
    assert schemas(owner, world, "old__") == []
