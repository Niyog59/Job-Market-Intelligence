"""Posting history: safe reprocessing, meaningful versions, chronology, and concurrency.

Responses are stored directly with store_scan_results, the same routine the collector and
replay use, so each test controls the collection time and the processing order exactly.
"""

import itertools
import threading
import time
from datetime import timedelta

import psycopg
import pytest
from gh_fakes import DATA_DUTIES, T0, FakeClock, board, gh_body, gh_job, registry

from jobmarket import db, normalize, store
from jobmarket.greenhouse import parse_jobs_response
from jobmarket.migrate import apply_migrations, discover
from jobmarket.sources import sync_registry
from jobmarket.store import refingerprint_versions, store_scan_results

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
V1 = "2026-10-26.1"
V2 = "2099-01-01.1"  # a hypothetical later rule set, registered only inside tests
REWORDED = DATA_DUTIES.replace("Build and maintain", "Design and operate")
RESTYLED = (
    "<div><p>\n  Build and maintain data pipelines with <b>SQL</b>, Python, and Airflow.\n"
    "</p><p>Model data in Snowflake and publish dashboards in Tableau.</p></div>"
)
_uri = itertools.count(1)


class History:
    """Saves responses as if collected at a given time and processes them on demand."""

    def __init__(self, conn):
        self.conn = conn
        self.board_id = conn.execute("SELECT board_id FROM source_boards").fetchone()[0]

    def save(self, at, jobs, total=None):
        """A response collected at `at` and saved, not yet processed."""
        run = self.scalar(
            "INSERT INTO collection_runs (trigger, started_at, finished_at, status) "
            "VALUES ('manual', %s, %s, 'complete') RETURNING run_id",
            (at, at),
        )
        scan = self.scalar(
            "INSERT INTO board_scans (run_id, board_id, started_at, finished_at, status, "
            "postings_seen) VALUES (%s, %s, %s, %s, 'complete', %s) RETURNING board_scan_id",
            (run, self.board_id, at, at, len(jobs)),
        )
        raw = self.scalar(
            "INSERT INTO raw_responses (board_scan_id, sequence_no, request_url, http_status, "
            "fetched_at, storage_uri, content_sha256, byte_size) "
            "VALUES (%s, 1, 'https://example.test', 200, %s, %s, %s, 1) RETURNING raw_response_id",
            (scan, at, f"test/{next(_uri)}.json", "0" * 64),
        )
        parsed = parse_jobs_response(
            200, {"content-type": "application/json"}, gh_body(jobs, total)
        )
        return Saved(scan, raw, at, parsed)

    def process(self, saved, recorded_by="collection", conn=None, processed_at=None):
        conn = conn or self.conn
        with conn.transaction():
            return store_scan_results(
                conn,
                self.board_id,
                saved.scan,
                saved.raw,
                saved.parsed,
                observed_at=saved.at,
                processed_at=processed_at or saved.at + timedelta(minutes=1),
                recorded_by=recorded_by,
            )

    def collect(self, at, jobs):
        saved = self.save(at, jobs)
        return saved, self.process(saved)

    def scalar(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def facts(self):
        """Row counts of every table that holds facts about postings."""
        tables = (
            "source_postings", "posting_versions", "posting_observations",
            "posting_version_comparisons", "posting_screenings", "rejected_records",
        )  # fmt: skip
        return {t: self.scalar(f"SELECT count(*) FROM {t}") for t in tables}

    def stages(self, source_id="1"):
        return self.conn.execute(
            "SELECT s.stage_no, s.version_no, s.first_observed_at, s.last_observed_at, "
            "s.change_window_start, s.change_window_end, s.changed_fields "
            "FROM posting_version_stages s JOIN source_postings p USING (posting_id) "
            "WHERE p.source_posting_id = %s ORDER BY s.stage_no",
            (source_id,),
        ).fetchall()

    def current(self, source_id="1"):
        """(current version_no, collection time of the current observation)."""
        return self.conn.execute(
            "SELECT v.version_no, o.observed_at FROM source_postings p "
            "JOIN posting_versions v ON v.version_id = p.current_version_id "
            "JOIN posting_observations o ON o.observation_id = p.current_observation_id "
            "WHERE p.source_posting_id = %s",
            (source_id,),
        ).fetchone()

    def assert_consistent(self):
        assert self.conn.execute("SELECT * FROM posting_history_problems").fetchall() == []


class Saved:
    def __init__(self, scan, raw, at, parsed):
        self.scan, self.raw, self.at, self.parsed = scan, raw, at, parsed


@pytest.fixture
def h(schema_conn):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    return History(schema_conn)


def other_connection(db_settings, schema):
    return db.connect(db_settings, autocommit=True, options=f"-c search_path={schema}")


# --- reprocessing and unchanged content ------------------------------------------------------


def test_same_saved_input_processed_twice_adds_no_facts(h):
    invalid = gh_job(9)
    invalid["absolute_url"] = None
    saved = h.save(T0, [gh_job(1), gh_job(2, title="Staff Accountant"), invalid])
    first = h.process(saved)
    before = h.facts()

    again = h.process(saved, recorded_by="replay", processed_at=T0 + 5 * HOUR)

    assert h.facts() == before
    assert before["rejected_records"] == 1
    assert (first.new_postings, first.new_versions, first.new_observations) == (2, 1, 1)
    assert (again.new_postings, again.new_versions, again.new_observations) == (0, 0, 0)
    assert again.already_recorded == 1
    h.assert_consistent()


def test_new_scan_of_unchanged_content_adds_an_observation_not_a_version(h):
    h.collect(T0, [gh_job(1)])
    _, counts = h.collect(T0 + HOUR, [gh_job(1, updated_at="2026-10-26T08:00:00-05:00")])

    assert (counts.unchanged, counts.changed, counts.new_versions) == (1, 0, 0)
    assert h.scalar("SELECT count(*) FROM posting_versions") == 1
    assert h.scalar("SELECT count(*) FROM posting_observations") == 2
    assert h.current() == (1, T0 + HOUR)
    first, last = h.conn.execute(
        "SELECT first_discovered_at, last_observed_at FROM source_postings"
    ).fetchone()
    assert (first, last) == (T0, T0 + HOUR)  # first-seen time preserved
    h.assert_consistent()


def test_cosmetic_differences_do_not_create_versions(h):
    h.collect(T0, [gh_job(1)])
    restyled = gh_job(1, content=RESTYLED, title=" Data  Engineer")
    _, counts = h.collect(T0 + HOUR, [restyled])

    assert (counts.unchanged, counts.cosmetic_only, counts.new_versions) == (1, 1, 0)
    assert h.scalar("SELECT count(*) FROM posting_versions") == 1
    # The exact source content did differ, and that is recorded on the observation.
    assert h.scalar("SELECT count(DISTINCT source_fingerprint) FROM posting_observations") == 2
    h.assert_consistent()


def test_source_fields_outside_the_comparison_do_not_create_versions(h):
    h.collect(T0, [gh_job(1)])
    moved = gh_job(1)
    moved["absolute_url"] = "https://job-boards.greenhouse.io/acme/jobs/1?gh_src=new"
    moved["departments"] = [{"id": 5, "name": "Analytics"}]
    moved["requisition_id"] = "R-NEW"
    h.collect(T0 + HOUR, [moved])
    assert h.scalar("SELECT count(*) FROM posting_versions") == 1


# --- meaningful changes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changed_job", "field"),
    [
        (gh_job(1, content=REWORDED), "description"),
        (gh_job(1, title="Senior Data Engineer"), "title"),
        (gh_job(1, location="Remote - US", offices=[]), "locations"),
        (gh_job(1, content=DATA_DUTIES + "<p>Salary: $120,000 - $140,000</p>"), "compensation"),
    ],
)
def test_meaningful_change_keeps_old_and_new_versions(h, changed_job, field):
    h.collect(T0, [gh_job(1)])
    _, counts = h.collect(T0 + HOUR, [changed_job])

    assert (counts.changed, counts.new_versions) == (1, 1)
    rows = h.conn.execute(
        "SELECT version_no, detection, normalization_version, content_html, first_seen_at "
        "FROM posting_versions ORDER BY version_no"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, "first_seen", V1), (2, "content_change", V1)]
    assert rows[0][3] == DATA_DUTIES  # the original content is kept unchanged
    stages = h.stages()
    assert len(stages) == 2
    # The change happened after the last sight of the old content and by the first sight of
    # the new one; the exact moment is unknown.
    assert stages[1][4:6] == (T0, T0 + HOUR)
    assert field in stages[1][6]
    assert h.current() == (2, T0 + HOUR)
    h.assert_consistent()


def test_a_b_a_is_preserved_as_three_stages(h):
    h.collect(T0, [gh_job(1)])
    h.collect(T0 + HOUR, [gh_job(1, content=REWORDED)])
    h.collect(T0 + 2 * HOUR, [gh_job(1)])

    fingerprints = [
        f
        for (f,) in h.conn.execute(
            "SELECT c.fingerprint FROM posting_versions v "
            "JOIN posting_version_comparisons c USING (version_id) ORDER BY v.version_no"
        )
    ]
    assert len(fingerprints) == 3
    assert fingerprints[0] == fingerprints[2] != fingerprints[1]  # repeated fingerprint allowed
    assert [s[1] for s in h.stages()] == [1, 2, 3]
    assert h.current() == (3, T0 + 2 * HOUR)
    h.assert_consistent()


# --- chronology ---------------------------------------------------------------------------------


def test_older_input_processed_later_does_not_regress_current_state(h):
    h.collect(T0, [gh_job(1)])
    late = h.save(T0 + HOUR, [gh_job(1)])  # collected at 1h, processed last
    h.collect(T0 + 2 * HOUR, [gh_job(1, title="Senior Data Engineer")])

    counts = h.process(late, recorded_by="replay", processed_at=T0 + 5 * HOUR)

    assert (counts.historical, counts.unchanged, counts.new_versions) == (1, 1, 0)
    assert h.current() == (2, T0 + 2 * HOUR)
    assert h.scalar("SELECT title FROM retained_jobs") == "Senior Data Engineer"
    assert h.scalar("SELECT title FROM posting_screenings") == "Senior Data Engineer"
    # The older evidence narrows when the change happened: after 1h, by 2h.
    stages = h.stages()
    assert [(s[1], s[2], s[3]) for s in stages] == [
        (1, T0, T0 + HOUR),
        (2, T0 + 2 * HOUR, T0 + 2 * HOUR),
    ]
    assert stages[1][4:6] == (T0 + HOUR, T0 + 2 * HOUR)
    h.assert_consistent()


def test_older_input_with_different_content_becomes_a_middle_stage(h):
    h.collect(T0, [gh_job(1)])
    late = h.save(T0 + HOUR, [gh_job(1, content=REWORDED)])
    h.collect(T0 + 2 * HOUR, [gh_job(1, title="Senior Data Engineer")])

    counts = h.process(late, recorded_by="replay")

    assert (counts.changed, counts.historical) == (1, 1)
    assert [s[1] for s in h.stages()] == [1, 3, 2]  # recording order differs from chronology
    assert h.current() == (2, T0 + 2 * HOUR)
    h.assert_consistent()


def test_older_input_between_identical_observations_splits_the_stage(h):
    h.collect(T0, [gh_job(1)])
    late = h.save(T0 + HOUR, [gh_job(1, content=REWORDED)])
    h.collect(T0 + 2 * HOUR, [gh_job(1)])
    existing = h.conn.execute(
        "SELECT observation_id, version_id FROM posting_observations ORDER BY observation_id"
    ).fetchall()

    h.process(late, recorded_by="replay")

    # A -> B -> A, recorded without rewriting the observations that already existed.
    assert [s[1] for s in h.stages()] == [1, 2, 1]
    assert (
        h.conn.execute(
            "SELECT observation_id, version_id FROM posting_observations "
            "WHERE observation_id = ANY(%s) ORDER BY observation_id",
            ([e[0] for e in existing],),
        ).fetchall()
        == existing
    )
    assert h.current() == (1, T0 + 2 * HOUR)
    h.assert_consistent()


def test_older_input_moves_first_discovery_earlier_but_never_later(h):
    early = h.save(T0, [gh_job(1)])
    h.collect(T0 + HOUR, [gh_job(1)])
    h.process(early, recorded_by="replay")
    first, last = h.conn.execute(
        "SELECT first_discovered_at, last_observed_at FROM source_postings"
    ).fetchone()
    assert (first, last) == (T0, T0 + HOUR)
    assert h.current() == (1, T0 + HOUR)
    h.assert_consistent()


def test_older_input_does_not_regress_screening_of_excluded_jobs(h):
    late = h.save(T0, [gh_job(2, title="Staff Accountant")])
    h.collect(T0 + HOUR, [gh_job(2, title="Senior Staff Accountant")])
    h.process(late, recorded_by="replay")
    assert h.scalar("SELECT title FROM posting_screenings") == "Senior Staff Accountant"
    assert h.scalar("SELECT count(*) FROM posting_versions") == 0


def test_older_input_recomputes_absence_evidence(h):
    late = h.save(T0, [gh_job(1), gh_job(2)])  # job 2 is seen only in this older response
    h.collect(T0 + HOUR, [gh_job(1)])
    h.process(late, recorded_by="replay")
    absences = dict(
        h.conn.execute("SELECT source_posting_id, complete_scan_absences FROM source_postings")
    )
    # Job 2 is kept, with the evidence that one later complete scan did not list it.
    assert absences == {"1": 0, "2": 1}


def test_collection_time_ties_are_broken_by_scan_id(h):
    first = h.save(T0, [gh_job(1)])
    second = h.save(T0, [gh_job(1, title="Senior Data Engineer")])  # same instant, later scan
    h.process(second)
    h.process(first, recorded_by="replay")
    assert h.current() == (1, T0)
    assert h.scalar("SELECT title FROM retained_jobs") == "Senior Data Engineer"
    h.assert_consistent()


# --- concurrency and interrupted writes ---------------------------------------------------


def _wait_until_blocked(watch_conn, pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = watch_conn.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if waiting and waiting[0] == "Lock":
            return
        time.sleep(0.02)
    raise AssertionError("second transaction never waited for the first")


def _run_overlapping(h, db_settings, schema, saved_a, saved_b):
    """Process two inputs in two transactions that genuinely overlap: the first stays open
    until the second is blocked waiting for it."""
    results, errors = {}, []
    release, a_written = threading.Event(), threading.Event()
    conn_a, conn_b = other_connection(db_settings, schema), other_connection(db_settings, schema)

    def first():
        try:
            with conn_a.transaction():
                results["a"] = store_scan_results(
                    conn_a, h.board_id, saved_a.scan, saved_a.raw, saved_a.parsed,
                    saved_a.at, saved_a.at, "collection",
                )  # fmt: skip
                a_written.set()
                release.wait(10)
        except Exception as exc:  # noqa: BLE001 - reported by the main thread
            errors.append(exc)
            a_written.set()

    def second():
        try:
            results["b"] = h.process(saved_b, recorded_by="replay", conn=conn_b)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with conn_a, conn_b:
        ta = threading.Thread(target=first)
        ta.start()
        a_written.wait(10)
        tb = threading.Thread(target=second)
        tb.start()
        try:
            _wait_until_blocked(h.conn, conn_b.info.backend_pid)
        finally:
            release.set()
            ta.join(10)
            tb.join(10)
    assert errors == []
    return results


def test_simultaneous_processing_of_the_same_input_stays_consistent(
    h, db_settings, isolated_schema
):
    saved = h.save(T0, [gh_job(1), gh_job(2), gh_job(3)])
    results = _run_overlapping(h, db_settings, isolated_schema, saved, saved)

    assert results["a"].new_observations == 3
    assert (results["b"].new_observations, results["b"].already_recorded) == (0, 3)
    assert h.facts()["source_postings"] == 3
    assert h.facts()["posting_versions"] == 3
    assert h.facts()["posting_observations"] == 3
    h.assert_consistent()


def test_simultaneous_processing_of_two_scans_keeps_the_newer_current(
    h, db_settings, isolated_schema
):
    older = h.save(T0, [gh_job(1)])
    newer = h.save(T0 + HOUR, [gh_job(1, title="Senior Data Engineer")])
    # The newer scan commits first; the older one, processed concurrently, becomes history.
    _run_overlapping(h, db_settings, isolated_schema, newer, older)

    assert h.facts()["source_postings"] == 1
    assert h.scalar("SELECT count(*) FROM posting_observations") == 2
    assert h.current() == (1, T0 + HOUR)
    assert h.scalar("SELECT title FROM retained_jobs") == "Senior Data Engineer"
    h.assert_consistent()


def test_interrupted_transaction_leaves_no_half_written_state(h, monkeypatch):
    h.collect(T0, [gh_job(1)])
    before = h.facts()
    saved = h.save(T0 + HOUR, [gh_job(1, content=REWORDED), gh_job(2), gh_job(3)])
    calls = itertools.count()
    real_screen = store.screen

    def failing_screen(*args, **kwargs):
        if next(calls) == 1:  # after job 1 has been fully written
            raise RuntimeError("process stopped")
        return real_screen(*args, **kwargs)

    monkeypatch.setattr(store, "screen", failing_screen)
    with pytest.raises(RuntimeError):
        h.process(saved)

    assert h.facts() == before
    assert h.current() == (1, T0)
    h.assert_consistent()


def test_lost_connection_mid_transaction_leaves_nothing(
    h, db_settings, isolated_schema, monkeypatch
):
    saved = h.save(T0, [gh_job(1), gh_job(2), gh_job(3)])
    victim = other_connection(db_settings, isolated_schema)
    calls = itertools.count()
    real_screen = store.screen

    def kill_connection_midway(*args, **kwargs):
        if next(calls) == 2:
            h.conn.execute("SELECT pg_terminate_backend(%s)", (victim.info.backend_pid,))
        return real_screen(*args, **kwargs)

    monkeypatch.setattr(store, "screen", kill_connection_midway)
    with pytest.raises(psycopg.OperationalError):
        h.process(saved, conn=victim)

    assert h.facts()["source_postings"] == 0
    assert h.facts()["posting_observations"] == 0


# --- normalization rule changes -------------------------------------------------------------


def _v2_with_departments(content):
    """Hypothetical later rules that also treat the department as meaningful."""
    form = normalize.NORMALIZERS[V1](content)
    form["departments"] = sorted(str(d.get("name")) for d in content.departments)
    return form


@pytest.fixture
def switch_to_v2(monkeypatch):
    def switch():
        monkeypatch.setitem(normalize.NORMALIZERS, V2, _v2_with_departments)
        monkeypatch.setattr(normalize, "CURRENT_NORMALIZATION", V2)

    return switch


def in_department(job, name):
    job["departments"] = [{"id": 1, "name": name}]
    return job


def test_rule_change_alone_creates_no_version(h, switch_to_v2):
    h.collect(T0, [gh_job(1)])
    switch_to_v2()
    _, counts = h.collect(T0 + HOUR, [gh_job(1)])

    assert (counts.unchanged, counts.new_versions) == (1, 0)
    result = refingerprint_versions(h.conn, T0 + 2 * HOUR)
    # The same stored original has a different form under the new rules: a rule effect,
    # recorded as such, with no version created or changed.
    # Identical source content was never re-evaluated during collection, so the new
    # rules' form of the version is computed now.
    assert (result.rules, result.versions, result.computed) == (V2, 1, 1)
    assert len(result.rule_effects) == 1 and result.rule_effects[0][1] == V1
    assert h.scalar("SELECT count(*) FROM posting_versions") == 1
    assert h.scalar("SELECT count(DISTINCT fingerprint) FROM posting_version_comparisons") == 2


def test_rule_change_is_not_reported_as_an_employer_edit(h, switch_to_v2):
    h.collect(T0, [in_department(gh_job(1), "Data")])
    # The department changes; the current rules ignore departments, so no version.
    h.collect(T0 + HOUR, [in_department(gh_job(1), "Analytics")])
    switch_to_v2()
    # A cosmetic edit after the rule change. Against the stored original ("Data") the new
    # rules would see a department difference that is really older and that the old rules
    # deliberately ignored.
    _, counts = h.collect(T0 + 2 * HOUR, [in_department(gh_job(1, content=RESTYLED), "Analytics")])
    assert (counts.unchanged, counts.new_versions) == (1, 0)

    # From here on the new rules apply in full: a real department change is a version.
    _, counts = h.collect(T0 + 3 * HOUR, [in_department(gh_job(1, content=RESTYLED), "Research")])
    assert (counts.changed, counts.new_versions) == (1, 1)
    detection, rules = h.conn.execute(
        "SELECT detection, normalization_version FROM posting_versions WHERE version_no = 2"
    ).fetchone()
    assert (detection, rules) == ("content_change", V2)
    h.assert_consistent()


def test_stored_original_reproduces_the_recorded_comparison(h):
    h.collect(T0, [gh_job(1, content=RESTYLED)])
    version_id, recorded = h.conn.execute(
        "SELECT version_id, fingerprint FROM posting_version_comparisons"
    ).fetchone()
    content = store.version_source_content(h.conn, version_id)
    assert normalize.compare_form(content, V1).fingerprint == recorded


def test_observations_from_before_comparison_rules_are_compared_exactly(h):
    """Rows written before comparison rules existed have no comparison fingerprint; their
    content equals their version's stored original, which is re-evaluated."""
    h.collect(T0, [gh_job(1)])
    h.conn.execute(
        "UPDATE posting_observations "
        "SET normalization_version = NULL, comparison_fingerprint = NULL"
    )
    h.conn.execute("DELETE FROM posting_version_comparisons")
    _, counts = h.collect(T0 + HOUR, [gh_job(1, content=RESTYLED)])
    assert (counts.unchanged, counts.cosmetic_only, counts.new_versions) == (1, 1, 0)
    _, counts = h.collect(T0 + 2 * HOUR, [gh_job(1, content=REWORDED)])
    assert counts.changed == 1


# --- migration of existing history --------------------------------------------------------


def test_migration_backfills_existing_history(schema_conn):
    migrations = discover()
    apply_migrations(schema_conn, [m for m in migrations if m.version <= 2])
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    c = schema_conn
    board_id = c.execute("SELECT board_id FROM source_boards").fetchone()[0]
    one = lambda sql, p=(): c.execute(sql, p).fetchone()[0]  # noqa: E731
    observed = [T0, T0 + HOUR]
    scans = []
    for at in observed:
        run = one(
            "INSERT INTO collection_runs (trigger, started_at, finished_at, status) "
            "VALUES ('manual', %s, %s, 'complete') RETURNING run_id",
            (at, at),
        )
        scan = one(
            "INSERT INTO board_scans (run_id, board_id, started_at, finished_at, status, "
            "postings_seen) VALUES (%s, %s, %s, %s, 'complete', 1) RETURNING board_scan_id",
            (run, board_id, at, at),
        )
        raw = one(
            "INSERT INTO raw_responses (board_scan_id, sequence_no, request_url, http_status, "
            "fetched_at, storage_uri, content_sha256, byte_size) VALUES "
            "(%s, 1, 'https://example.test', 200, %s, %s, %s, 1) RETURNING raw_response_id",
            (scan, at, f"legacy/{scan}.json", "0" * 64),
        )
        scans.append((scan, raw))
    posting = one(
        "INSERT INTO source_postings (board_id, source_posting_id, first_discovered_at, "
        "last_observed_at) VALUES (%s, '1', %s, %s) RETURNING posting_id",
        (board_id, T0, T0 + HOUR),
    )
    version = one(
        "INSERT INTO posting_versions (posting_id, version_no, content_hash, title, first_seen_at,"
        " content_html) VALUES (%s, 1, %s, 'Data Engineer', %s, %s) RETURNING version_id",
        (posting, "a" * 64, T0, DATA_DUTIES),
    )
    for (scan, raw), at in zip(scans, observed, strict=True):
        c.execute(
            "INSERT INTO posting_observations (board_scan_id, posting_id, version_id, "
            "raw_response_id, observed_at) VALUES (%s, %s, %s, %s, %s)",
            (scan, posting, version, raw, at),
        )
    c.execute(
        "INSERT INTO posting_screenings (posting_id, content_hash, classifier_version, title, "
        "retained, relevant, texas_eligibility, seniority, early_career, sponsorship_status, "
        "screened_at) VALUES (%s, %s, 'x', 'Data Engineer', true, true, 'local_dfw', 'mid', "
        "false, 'not_stated', %s)",
        (posting, "a" * 64, T0),
    )

    apply_migrations(c, migrations)

    current_obs, current_version, observed_at = c.execute(
        "SELECT p.current_observation_id, p.current_version_id, o.observed_at "
        "FROM source_postings p JOIN posting_observations o "
        "ON o.observation_id = p.current_observation_id"
    ).fetchone()
    assert (current_version, observed_at) == (version, T0 + HOUR)
    assert one("SELECT detection FROM posting_versions") == "first_seen"
    assert (
        one("SELECT count(*) FROM posting_observations WHERE source_fingerprint = %s", ("a" * 64,))
        == 2
    )
    assert one("SELECT content_observed_at FROM posting_screenings") == T0 + HOUR
    assert one("SELECT content_scan_id FROM posting_screenings") == scans[1][0]
    assert c.execute("SELECT * FROM posting_history_problems").fetchall() == []
    assert one("SELECT count(*) FROM retained_jobs") == 1
