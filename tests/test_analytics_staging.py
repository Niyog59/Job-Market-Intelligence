"""The dbt project builds over ingestion data, tests pass, and ingestion is never written."""

import pytest
from analytics_harness import Analytics
from gh_fakes import T0, FakeClock, board, gh_job, registry
from test_history import HOUR, History

from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db


@pytest.fixture
def a(schema_conn, isolated_schema, db_settings):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    analytics = Analytics(schema_conn, isolated_schema)
    analytics.history = History(schema_conn)
    yield analytics
    analytics.drop(db_settings)


def ingestion_counts(conn):
    tables = ("source_postings", "posting_versions", "posting_observations",
              "posting_availability_events", "board_scans")  # fmt: skip
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def test_staging_builds_and_passes_its_tests_without_touching_ingestion(a):
    a.history.collect(T0, [gh_job(1), gh_job(2, title="Staff Accountant")])
    a.history.collect(T0 + HOUR, [gh_job(1, content="<p>Edited.</p>")])
    before = ingestion_counts(a.conn)
    a.build("--select", "staging")
    assert ingestion_counts(a.conn) == before
    versions = a.rows("staging", "SELECT posting_id, version_no, detection FROM {t}."
                      "stg_posting_versions ORDER BY 1, 2")  # fmt: skip
    assert [r[1:] for r in versions] == [(1, "first_seen"), (2, "content_change"),
                                         (1, "first_seen")]  # fmt: skip


def test_a_rebuild_gives_the_same_rows(a):
    a.history.collect(T0, [gh_job(1)])
    a.build("--select", "staging")
    first = a.rows("staging", "SELECT * FROM {t}.stg_postings ORDER BY posting_id")
    a.build("--select", "staging")
    assert a.rows("staging", "SELECT * FROM {t}.stg_postings ORDER BY posting_id") == first


def test_building_into_the_ingestion_schema_is_refused(a):
    from jobmarket.analytics import run_dbt

    outcome = run_dbt(
        ["build", "--select", "staging"],
        target="owner",
        variables={"source_schema": "analytics_staging", "schema_prefix": ""},
        quiet=True,
    )
    assert not outcome.success
