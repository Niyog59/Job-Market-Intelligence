"""Content history for every listing and the long-description rule."""

import pytest
from gh_fakes import DATA_DUTIES, T0, FakeClock, board, gh_job, registry
from test_history import HOUR, History

from jobmarket import normalize, store
from jobmarket.greenhouse import html_to_text
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry
from jobmarket.store import MAX_STORED_DESCRIPTION_CHARS, refingerprint_versions

pytestmark = pytest.mark.db


@pytest.fixture
def h(schema_conn):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    return History(schema_conn)


def test_excluded_listings_get_the_same_history_as_retained_ones(h):
    h.collect(T0, [gh_job(1), gh_job(2, title="Staff Accountant")])
    h.collect(T0 + HOUR, [gh_job(1), gh_job(2, title="Staff Accountant", content="<p>New</p>")])
    rows = h.conn.execute(
        "SELECT l.source_posting_id, l.retained, l.version_no, l.description_length "
        "FROM listings l ORDER BY 1"
    ).fetchall()
    expected = len(html_to_text(DATA_DUTIES))
    assert rows == [("1", True, 1, expected), ("2", False, 2, 3)]
    assert h.scalar("SELECT count(*) FROM retained_jobs") == 1  # retained views unchanged
    h.assert_consistent()


def long_job(job_id, word="data", **kw):
    paragraph = f"<p>{(word + ' ') * 5000}</p>"  # about 25,000 characters of text
    return gh_job(job_id, content=paragraph, **kw)


def test_long_descriptions_keep_fingerprints_but_not_text(h):
    h.collect(T0, [long_job(1)])
    text, html_, length, omitted = h.conn.execute(
        "SELECT description_text, content_html, description_length, description_omitted "
        "FROM posting_versions"
    ).fetchone()
    assert (text, html_, omitted) == (None, None, "too_long")
    assert length > MAX_STORED_DESCRIPTION_CHARS
    stored = h.scalar("SELECT normalized ->> 'description' FROM posting_version_comparisons")
    assert stored == store.OMITTED_DESCRIPTION

    _, same = h.collect(T0 + HOUR, [long_job(1)])
    assert (same.new_versions, same.unchanged) == (0, 1)  # unchanged text: no new version
    _, edited = h.collect(T0 + 2 * HOUR, [long_job(1, word="info")])
    assert (edited.new_versions, edited.changed, edited.omitted_too_long) == (1, 1, 1)
    h.assert_consistent()


def test_long_descriptions_are_compared_by_fingerprint_after_a_rule_change(h, monkeypatch):
    h.collect(T0, [long_job(1)])
    later = "2099-01-01.1"
    monkeypatch.setitem(normalize.NORMALIZERS, later, normalize.NORMALIZERS["2026-10-26.1"])
    monkeypatch.setattr(normalize, "CURRENT_NORMALIZATION", later)
    _, again = h.collect(T0 + HOUR, [long_job(1)])
    assert (again.new_versions, again.unchanged) == (0, 1)
    result = refingerprint_versions(h.conn, T0 + 2 * HOUR)
    assert (result.versions, result.omitted) == (0, 1)


def test_duplicate_matching_keeps_its_retained_scope(h):
    from jobmarket.duplicates import load_records

    h.collect(T0, [gh_job(1), gh_job(2, title="Staff Accountant")])
    assert [r.source_posting_id for r in load_records(h.conn, T0)] == ["1"]
