"""Reproducible posting-history scenarios with SQL evidence.

    python tests/history_scenarios.py

Runs controlled fixture responses through the real collector, store, and replay code in a
temporary PostgreSQL schema (created in the database configured by DB_* / .env and dropped
afterwards, so existing tables are never touched), then prints the SQL used and its
results. Collection times come from a controlled clock and HTTP responses from a scripted
transport, so no live employer has to edit a posting. Exits non-zero if any expectation
fails.
"""

import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from gh_fakes import DATA_DUTIES, FakeClock, ScriptedTransport, board, gh_job, ok, registry

import jobmarket.collector as collector_module
from jobmarket import db, normalize
from jobmarket.collector import Collector
from jobmarket.config import load_settings
from jobmarket.migrate import apply_migrations
from jobmarket.raw_store import cleanup_raw
from jobmarket.replay import replay_scans
from jobmarket.sources import sync_registry
from jobmarket.store import refingerprint_versions

HOUR = timedelta(hours=1)
CHICAGO = ZoneInfo("America/Chicago")
REWORDED = DATA_DUTIES.replace("Build and maintain", "Design and operate")
RESTYLED = (
    "<div><p>\n  Build and maintain data pipelines with <b>SQL</b>, Python, and Airflow.\n"
    "</p><p>Model data in Snowflake and publish dashboards in Tableau.</p></div>"
)
LATER_RULES = "2099-01-01.1"


def _show(value):
    if value is None:
        return "-"
    if hasattr(value, "astimezone"):
        return value.astimezone(CHICAGO).strftime("%m-%d %H:%M")
    if isinstance(value, list):
        return ",".join(map(str, value)) or "-"
    return str(value)


class Evidence:
    def __init__(self, conn):
        self.conn = conn
        self.failures = []

    def query(self, title, sql, params=()):
        print(f"\n-- {title}\n{sql.strip()}")
        cur = self.conn.execute(sql, params)
        names = [c.name for c in cur.description]
        rows = [[_show(v) for v in row] for row in cur.fetchall()]
        widths = [max([len(n)] + [len(r[i]) for r in rows]) for i, n in enumerate(names)]
        print("  " + "  ".join(n.ljust(w) for n, w in zip(names, widths, strict=True)))
        for r in rows:
            print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))
        return rows

    def scalar(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def expect(self, description, actual, expected):
        ok_ = actual == expected
        print(f"  [{'ok' if ok_ else 'FAILED'}] {description}: {actual!r}")
        if not ok_:
            self.failures.append(f"{description}: expected {expected!r}, got {actual!r}")

    def facts(self):
        return self.conn.execute(
            "SELECT (SELECT count(*) FROM source_postings), "
            "(SELECT count(*) FROM posting_versions), "
            "(SELECT count(*) FROM posting_observations), "
            "(SELECT count(*) FROM rejected_records)"
        ).fetchone()


def heading(text):
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def run(conn, raw_root):
    apply_migrations(conn)
    clock = FakeClock()
    sync_registry(conn, registry(board("acme")), clock())
    transport = ScriptedTransport()
    ev = Evidence(conn)

    def collect(*jobs):
        transport.queues["acme"] = [ok(list(jobs))]
        client = httpx.Client(transport=transport)
        report = Collector(conn, client, raw_root, clock=clock, sleep=lambda s: None).collect()
        clock.advance(HOUR)
        return report.outcomes[0]

    def counts_line(outcome):
        c = outcome.counts
        return (c.new_postings, c.changed, c.unchanged, c.cosmetic_only)

    data_job = gh_job(1)
    analyst = gh_job(2, title="Data Analyst", location="Houston, TX")
    accountant = gh_job(3, title="Staff Accountant")

    heading("1. First collection: three jobs, two relevant (all three get content history)")
    ev.expect(
        "(new, changed, unchanged, cosmetic)",
        counts_line(collect(data_job, analyst, accountant)),
        (3, 0, 0, 0),
    )

    heading("2. Unchanged rescan: observations, no versions")
    ev.expect(
        "(new, changed, unchanged, cosmetic)",
        counts_line(collect(data_job, analyst, accountant)),
        (0, 0, 3, 0),
    )

    heading("3. Cosmetic HTML/whitespace edit and a new source updated_at")
    cosmetic = gh_job(1, content=RESTYLED, updated_at="2026-10-27T09:00:00-05:00")
    ev.expect(
        "(new, changed, unchanged, cosmetic)",
        counts_line(collect(cosmetic, analyst, accountant)),
        (0, 0, 3, 1),
    )

    heading("4. Meaningful change: the description is reworded")
    ev.expect(
        "(new, changed, unchanged, cosmetic)",
        counts_line(collect(gh_job(1, content=REWORDED), analyst, accountant)),
        (0, 1, 2, 0),
    )

    heading("5. The wording is reverted (A -> B -> A)")
    ev.expect(
        "(new, changed, unchanged, cosmetic)",
        counts_line(collect(data_job, analyst, accountant)),
        (0, 1, 2, 0),
    )

    heading("6. Out of order: a response saved, not processed, and replayed after a newer scan")
    transport.queues["acme"] = [ok([gh_job(1, title="Data Engineer II"), analyst, accountant])]
    real_store = collector_module.store_scan_results

    def stop_after_saving(*args, **kwargs):
        raise RuntimeError("collector stopped after saving the response")

    collector_module.store_scan_results = stop_after_saving
    try:
        Collector(
            conn, httpx.Client(transport=transport), raw_root, clock=clock, sleep=lambda s: None
        ).collect()
    finally:
        collector_module.store_scan_results = real_store
    lost_scan = ev.scalar("SELECT max(board_scan_id) FROM board_scans")
    clock.advance(HOUR)
    collect(gh_job(1, title="Senior Data Engineer"), analyst, accountant)
    (replayed,) = replay_scans(conn, raw_root, [lost_scan], clock)
    ev.expect("replay outcome", replayed.outcome, "processed")
    ev.expect(
        "new version kept as history (changed, historical)",
        (replayed.counts.changed, replayed.counts.historical),
        (1, 3),  # every listing has content history, the excluded accountant too
    )
    ev.expect(
        "current title after the older replay",
        ev.scalar("SELECT title FROM retained_jobs WHERE source_posting_id = '1'"),
        "Senior Data Engineer",
    )

    heading("7. The same saved responses replayed again")
    before = ev.facts()
    every_scan = [
        r[0] for r in conn.execute("SELECT board_scan_id FROM raw_responses ORDER BY fetched_at")
    ]
    results = replay_scans(conn, raw_root, every_scan, clock)
    ev.expect("observations added", sum(r.counts.new_observations for r in results), 0)
    ev.expect("(postings, versions, observations, rejected) unchanged", ev.facts(), before)

    heading("8. A normalization rule change alone (hypothetical later rules)")
    previous = normalize.CURRENT_NORMALIZATION
    normalize.NORMALIZERS[LATER_RULES] = lambda c: {
        **normalize.NORMALIZERS[previous](c),
        "departments": sorted(str(d.get("name")) for d in c.departments),
    }
    normalize.CURRENT_NORMALIZATION = LATER_RULES
    try:
        versions_before = ev.scalar("SELECT count(*) FROM posting_versions")
        collect(gh_job(1, title="Senior Data Engineer"), analyst, accountant)
        result = refingerprint_versions(conn, clock())
        ev.expect(
            "versions created by the rule change",
            ev.scalar("SELECT count(*) FROM posting_versions") - versions_before,
            0,
        )
        ev.expect(
            "versions reported as rule effects, not edits (all of them: the rules add a field)",
            len(result.rule_effects),
            versions_before,
        )
        ev.query(
            "One stored version, two rule sets: differing fingerprints are a rule effect",
            "SELECT version_id, normalization_version, left(fingerprint, 12) AS fingerprint "
            "FROM posting_version_comparisons WHERE version_id = 1 ORDER BY normalization_version",
        )
    finally:
        normalize.CURRENT_NORMALIZATION = previous
        del normalize.NORMALIZERS[LATER_RULES]

    heading("9. Replay after the 4-day raw retention")
    cleanup_raw(conn, raw_root, clock.now + timedelta(days=5))
    (expired,) = replay_scans(conn, raw_root, [lost_scan], clock)
    ev.expect("replay outcome", expired.outcome, "expired")
    print(f"  detail: {expired.detail}")

    heading("SQL evidence")
    ev.query(
        "Inserted / changed / unchanged per scan (collection time order)",
        """
        SELECT s.board_scan_id AS scan, r.fetched_at AS collected, s.status, s.jobs_new AS new,
               s.jobs_changed AS changed, s.jobs_unchanged AS unchanged
        FROM board_scans s JOIN raw_responses r USING (board_scan_id)
        ORDER BY r.fetched_at
        """,
    )
    ev.query(
        "Version history of job 1, in recording order",
        """
        SELECT v.version_no, v.detection, v.normalization_version AS rules, v.first_seen_at,
               v.recorded_at, v.title, left(c.fingerprint, 12) AS fingerprint
        FROM posting_versions v
        JOIN source_postings p USING (posting_id)
        JOIN posting_version_comparisons c
          ON c.version_id = v.version_id AND c.normalization_version = v.normalization_version
        WHERE p.source_posting_id = '1'
        ORDER BY v.version_no
        """,
    )
    stages = ev.query(
        "Stages of job 1, in collection order, with change windows",
        """
        SELECT s.stage_no, s.version_no, s.first_observed_at, s.last_observed_at, s.observations,
               s.change_window_start, s.change_window_end, s.changed_fields
        FROM posting_version_stages s JOIN source_postings p USING (posting_id)
        WHERE p.source_posting_id = '1'
        ORDER BY s.stage_no
        """,
    )
    # A -> B -> A records the return to A as a new version (3); the replayed older response
    # (version 5) sits between versions 3 and 4 in collection order.
    ev.expect("stage sequence (version_no)", [int(r[1]) for r in stages], [1, 2, 3, 5, 4])
    ev.query(
        "Observations of job 1: collection time vs processing time",
        """
        SELECT o.board_scan_id AS scan, o.observed_at AS collected, o.processed_at AS processed,
               o.recorded_by, v.version_no, left(o.source_fingerprint, 12) AS source_fp
        FROM posting_observations o
        JOIN posting_versions v USING (version_id)
        JOIN source_postings p ON p.posting_id = o.posting_id
        WHERE p.source_posting_id = '1'
        ORDER BY o.observed_at, o.board_scan_id
        """,
    )
    ev.query(
        "Replay attempts",
        """
        SELECT replay_id, board_scan_id AS scan, collected_at, outcome, observations_new AS new,
               observations_existing AS existing, observations_historical AS historical
        FROM replay_runs ORDER BY replay_id
        """,
    )
    problems = ev.query(
        "Integrity checks (expected empty)", "SELECT * FROM posting_history_problems"
    )
    ev.expect("integrity problems", len(problems), 0)
    return ev.failures


def main() -> int:
    settings = load_settings()
    schema = f"history_demo_{uuid.uuid4().hex[:8]}"
    with db.connect(settings, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with (
            db.connect(settings, autocommit=True, options=f"-c search_path={schema}") as conn,
            tempfile.TemporaryDirectory() as raw,
        ):
            failures = run(conn, Path(raw))
    finally:
        with db.connect(settings, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}_private" CASCADE')
    print(f"\n{'All expectations met.' if not failures else 'FAILED:'}")
    for failure in failures:
        print(f"  {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
