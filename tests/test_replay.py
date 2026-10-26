"""Replaying saved raw responses end to end: real stored files, collector, and CLI."""

from datetime import timedelta

import httpx
import pytest
from gh_fakes import DATA_DUTIES, gh_job, ok

from jobmarket import cli, db
from jobmarket.collector import COLLECTION_LOCK_KEY, CollectionBusy
from jobmarket.raw_store import cleanup_raw
from jobmarket.replay import UnknownScan, available_scan_ids, replay_scans

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
REWORDED = DATA_DUTIES.replace("Build and maintain", "Design and operate")


def facts(env):
    return env.one(
        "SELECT (SELECT count(*) FROM source_postings), (SELECT count(*) FROM posting_versions), "
        "(SELECT count(*) FROM posting_observations), (SELECT count(*) FROM rejected_records), "
        "(SELECT count(*) FROM posting_screenings)"
    )


def replay(env, *scan_ids):
    return replay_scans(env.conn, env.raw_root, list(scan_ids), env.clock)


def acme_scans(env):
    return [
        r[0]
        for r in env.conn.execute(
            "SELECT s.board_scan_id FROM board_scans s JOIN source_boards b USING (board_id) "
            "WHERE b.board_key = 'acme' ORDER BY s.board_scan_id"
        )
    ]


def collect_without_processing(env, monkeypatch):
    """A collection that saved its raw response and then failed while processing it."""
    import jobmarket.collector as collector_module

    def fail(*args, **kwargs):
        raise RuntimeError("processing stopped")

    with monkeypatch.context() as m:
        m.setattr(collector_module, "store_scan_results", fail)
        env.collector().collect(tokens=["acme"])
    return acme_scans(env)[-1]


def test_replaying_a_processed_scan_adds_nothing(env):
    broken = gh_job(9)
    broken["absolute_url"] = None
    env.transport.queue("acme", ok([gh_job(1), gh_job(2, title="Staff Accountant"), broken]))
    env.collector().collect(tokens=["acme"])
    before = facts(env)
    (scan,) = acme_scans(env)

    (result,) = replay(env, scan)

    assert result.outcome == "processed" and result.validation == "partial"
    assert (result.counts.new_observations, result.counts.already_recorded) == (0, 1)
    assert facts(env) == before
    row = env.one(
        "SELECT outcome, observations_new, observations_existing, collected_at, started_at "
        "FROM replay_runs"
    )
    assert row[:3] == ("processed", 0, 1)
    assert row[4] > row[3]  # processed later than collected; both kept


def test_replay_recovers_a_scan_whose_processing_failed_as_history(env, monkeypatch):
    env.transport.queue(
        "acme",
        ok([gh_job(1)]),
        ok([gh_job(1, content=REWORDED)]),
        ok([gh_job(1, title="Senior Data Engineer")]),
    )
    env.collector().collect(tokens=["acme"])
    env.clock.advance(HOUR)
    lost = collect_without_processing(env, monkeypatch)
    env.clock.advance(HOUR)
    env.collector().collect(tokens=["acme"])
    assert (
        env.scalar("SELECT status FROM board_scans WHERE board_scan_id = %s", (lost,)) == "failed"
    )

    (result,) = replay(env, lost)

    assert (result.counts.changed, result.counts.historical) == (1, 1)
    assert env.scalar("SELECT title FROM retained_jobs") == "Senior Data Engineer"
    assert (
        env.scalar("SELECT status FROM board_scans WHERE board_scan_id = %s", (lost,)) == "failed"
    )
    assert [
        r[0]
        for r in env.conn.execute("SELECT version_no FROM posting_version_stages ORDER BY stage_no")
    ] == [1, 3, 2]
    assert (
        env.scalar("SELECT recorded_by FROM posting_observations WHERE board_scan_id = %s", (lost,))
        == "replay"
    )
    assert env.conn.execute("SELECT * FROM posting_history_problems").fetchall() == []


def test_expired_raw_response_is_reported_and_history_kept(env):
    env.transport.queue("acme", ok([gh_job(1)]))
    env.collector().collect(tokens=["acme"])
    (scan,) = acme_scans(env)
    cleanup_raw(env.conn, env.raw_root, env.clock.now + timedelta(days=5))
    before = facts(env)

    (result,) = replay(env, scan)

    assert result.outcome == "expired"
    assert "4-day raw retention" in result.detail and "cannot be replayed" in result.detail
    assert facts(env) == before
    assert scan not in available_scan_ids(env.conn)
    assert env.scalar("SELECT outcome FROM replay_runs") == "expired"


def test_missing_or_altered_raw_file_is_refused(env):
    env.transport.queue("acme", ok([gh_job(1)]), ok([gh_job(2)]))
    env.collector().collect(tokens=["acme"])
    env.collector().collect(tokens=["acme"])
    first, second = acme_scans(env)
    paths = dict(
        env.conn.execute("SELECT board_scan_id, storage_uri FROM raw_responses").fetchall()
    )
    (env.raw_root / paths[first]).unlink()
    (env.raw_root / paths[second]).write_bytes(b'{"jobs": [], "meta": {"total": 0}}')
    before = facts(env)

    results = replay(env, first, second)

    assert [r.outcome for r in results] == ["file_missing", "checksum_mismatch"]
    assert facts(env) == before


def test_scan_without_a_response_has_nothing_to_replay(env):
    env.transport.queue("acme", httpx.ConnectError("network down"))
    env.collector().collect(tokens=["acme"])
    (result,) = replay(env, acme_scans(env)[0])
    assert result.outcome == "no_response"


def test_unknown_scan_is_refused(env):
    with pytest.raises(UnknownScan):
        replay(env, 999_999)


def test_replay_does_not_overlap_a_collection(env, db_settings):
    with db.connect(db_settings, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (COLLECTION_LOCK_KEY,))
        with pytest.raises(CollectionBusy):
            replay(env)
        other.execute("SELECT pg_advisory_unlock(%s)", (COLLECTION_LOCK_KEY,))


def test_available_replays_everything_still_stored_and_stays_duplicate_free(env):
    env.transport.queue("acme", ok([gh_job(1)]), ok([gh_job(1, content=REWORDED)]))
    env.transport.queue("beta", ok([gh_job(7, token="beta")]))
    env.collector().collect()
    env.clock.advance(HOUR)
    env.collector().collect()
    before = facts(env)

    for _ in range(2):
        results = replay(env, *available_scan_ids(env.conn))
        assert len(results) == 4
        assert all(r.outcome == "processed" for r in results)
        assert sum(r.counts.new_observations for r in results) == 0
    assert facts(env) == before


# --- command line ------------------------------------------------------------------------


@pytest.fixture
def cli_env(env, monkeypatch, isolated_schema, db_settings):
    """Run CLI commands against the test schema and raw directory."""
    monkeypatch.setenv("JOBMARKET_RAW_DIR", str(env.raw_root))
    real_connect = db.connect

    def connect(settings, autocommit=False, options=None):
        return real_connect(
            settings, autocommit=autocommit, options=f"-c search_path={isolated_schema}"
        )

    monkeypatch.setattr(db, "connect", connect)
    return env


def test_cli_replay_history_and_checks(cli_env, capsys):
    env = cli_env
    env.transport.queue("acme", ok([gh_job(1)]), ok([gh_job(1, title="Senior Data Engineer")]))
    env.collector().collect(tokens=["acme"])
    env.clock.advance(HOUR)
    env.collector().collect(tokens=["acme"])
    first = acme_scans(env)[0]

    assert cli.main(["replay", "--scan", str(first)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"Scan {first} (acme" in out and "processed" in out and "already recorded 1" in out

    assert cli.main(["history", "--board", "acme", "--job", "1"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "current: v2 'Senior Data Engineer'" in out
    assert "changed after" in out and "title" in out

    assert cli.main(["check-history"]) == cli.EXIT_OK
    assert "0 problem(s)" in capsys.readouterr().out

    assert cli.main(["refingerprint"]) == cli.EXIT_OK
    assert "No versions were created or changed." in capsys.readouterr().out

    cleanup_raw(env.conn, env.raw_root, env.clock.now + timedelta(days=5))
    assert cli.main(["replay", "--scan", str(first)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert ": expired" in out and "cannot be replayed" in out
