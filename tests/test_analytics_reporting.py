"""Reporting models on a deterministic, synthetic three-day history (America/Chicago days):

  2026-10-26  acme's first scan is partial (jobs 1, 2), its next scan is complete (1, 2, 3):
              all three are acme's initial import. acme2 fails both times (no baseline).
  2026-10-27  acme lists 1, 2 (edited), 3, and new job 4 (a discovery). acme2's first
              complete scan lists job 50 (same requisition and title as job 1: a strong
              duplicate group). At 23:59 CDT acme's scan misses job 3 (first absence).
  2026-10-28  acme fails (no absence counted), then misses job 3 again, then lists it again
              (a reactivation); job 4 is closed on official evidence. Two jobs are extracted
              by a scripted model on 2026-10-27.

The collector, availability rules, duplicate rules, and extraction queue are the real ones;
only HTTP responses and model answers are scripted.
"""

from datetime import UTC, datetime

import httpx
import pytest
from analytics_harness import Analytics
from extraction_fakes import ScriptedModel, answer, reply
from gh_fakes import FakeClock, ScriptedTransport, board, gh_job, ok, registry

from jobmarket.availability import close_posting
from jobmarket.collector import Collector
from jobmarket.extraction import ExtractionConfig, run_extraction
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

DAY1, DAY2, DAY3 = (datetime(2026, 10, 26).date(), datetime(2026, 10, 27).date(),
                    datetime(2026, 10, 28).date())  # fmt: skip


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def job(n, token="acme", req=None, **kw):
    kw.setdefault("content", f"<p>Build data pipelines for team {n}. Python or Java.</p>")
    item = gh_job(n, token=token, **kw)
    item["requisition_id"] = req or f"R{n}"
    return item


SKILLS = answer(
    skills=[{"name": "Python", "status": "required", "evidence": []},
            {"name": "Java", "status": "required", "evidence": []},
            {"name": "SQL", "status": "required", "evidence": []}],
    skill_groups=[{"relation": "any_of", "min_count": None, "members": ["Python", "Java"],
                   "status": "required", "display_text": "Python or Java", "evidence": []}],
    sponsorship={"status": "not_supported", "evidence": []},
)  # fmt: skip


@pytest.fixture(scope="module")
def built(request, db_settings, tmp_path_factory):
    """Build the history once in a module-level schema and run dbt over it."""
    import uuid

    from jobmarket import db

    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = db.connect(db_settings, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    conn = db.connect(db_settings, autocommit=True, options=f"-c search_path={schema}")
    apply_migrations(conn)
    sync_registry(conn, registry(board("acme"), board("acme2", employer="Acme", slug="acme")),
                  FakeClock()())  # fmt: skip
    clock = FakeClock(at("2026-10-26T17:00:00"))
    transport = ScriptedTransport()
    raw = tmp_path_factory.mktemp("raw")

    def collect(when, acme, acme2):
        clock.now = at(when)
        transport.queues["acme"] = [acme]
        transport.queues["acme2"] = [acme2]
        Collector(conn, httpx.Client(transport=transport), raw, clock=clock,
                  sleep=lambda s: None, interval=0).collect()  # fmt: skip

    fail = httpx.Response(500)
    j1, j2, j3, j4 = job(1), job(2), job(3), job(4)
    j2_edited = job(2, content="<p>Own the warehouse for team 2. Python or Java.</p>")
    j50 = job(50, token="acme2", req="R1")
    collect("2026-10-26T17:00:00", ok([j1, j2], total=3), fail)  # partial: baseline not set
    collect("2026-10-26T18:00:00", ok([j1, j2, j3]), fail)  # acme baseline
    collect("2026-10-27T17:00:00", ok([j1, j2_edited, j3, j4]), ok([j50]))  # acme2 baseline
    run_extraction(conn, ScriptedModel(reply(SKILLS)), ExtractionConfig(rpm=60, daily_limit=100),
                   lambda: at("2026-10-27T18:00:00"), lambda s: None, limit=2)  # fmt: skip
    collect("2026-10-28T04:59:00", ok([j1, j2_edited, j4]), ok([j50]))  # 23:59 CDT on 10-27
    collect("2026-10-28T13:00:00", fail, ok([j50]))  # acme failed: nothing counted
    collect("2026-10-28T14:00:00", ok([j1, j2_edited, j4]), ok([j50]))
    collect("2026-10-28T15:00:00", ok([j1, j2_edited, j3, j4]), ok([j50]))  # job 3 is back
    job4 = conn.execute("SELECT posting_id FROM source_postings WHERE source_posting_id = '4'")
    close_posting(conn, job4.fetchone()[0], "employer page says filled",
                  "https://example.test/4", "This position has been filled.",
                  at("2026-10-28T16:00:00"))  # fmt: skip

    analytics = Analytics(conn, schema)
    analytics.build()
    yield analytics
    analytics.drop(db_settings)
    conn.close()
    admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.close()


def activity(a, day, population="all"):
    row = a.rows("reporting", "SELECT initial_imports, discoveries, updates, "
                 "became_possibly_unavailable, assumed_inactive, explicit_closures, "
                 "reactivations, boards_baselined, boards_with_baseline FROM {t}."
                 "rpt_daily_activity WHERE report_day = %s AND population = %s",
                 (day, population))  # fmt: skip
    keys = ("imports", "discoveries", "updates", "possibly", "inactive", "closed", "back",
            "baselined", "boards")  # fmt: skip
    return dict(zip(keys, row[0], strict=True))


def test_initial_imports_are_kept_apart_from_discoveries(built):
    day1, day2 = activity(built, DAY1), activity(built, DAY2)
    # Job 1 and 2 were first seen in a partial scan: still part of acme's initial import.
    assert (day1["imports"], day1["discoveries"]) == (3, 0)
    # Job 50 is acme2's initial import (a new board, not new hiring); job 4 is a discovery.
    assert (day2["imports"], day2["discoveries"]) == (1, 1)
    assert (day1["boards"], day2["baselined"], day2["boards"]) == (1, 1, 2)


def test_updates_and_observations_are_not_new_jobs(built):
    assert activity(built, DAY2)["updates"] == 1  # job 2's edit
    totals = built.rows("reporting", "SELECT sum(initial_imports + discoveries) FROM "
                        "{t}.rpt_daily_activity WHERE population = 'all'")  # fmt: skip
    assert totals == [(5,)]  # five postings, however often they were seen


def test_day_boundaries_follow_chicago_time(built):
    # The 04:59 UTC scan on 10-28 is 23:59 CDT on 10-27.
    assert activity(built, DAY2)["possibly"] == 1
    day3 = activity(built, DAY3)
    assert (day3["possibly"], day3["back"], day3["closed"]) == (0, 1, 1)


def test_a_failed_scan_counts_no_absence(built):
    absences = built.rows(
        "staging", "SELECT count(*) FROM {t}.stg_availability_events WHERE event_kind = "
        "'absence_counted'"
    )  # fmt: skip
    assert absences == [(1,)]  # only the 14:00 qualifying scan; the 13:00 failure counted none


def inventory(a, day, population="all"):
    return {state: (records, opps) for state, records, opps in a.rows(
        "reporting", "SELECT availability_state, source_records, opportunities FROM "
        "{t}.rpt_inventory_daily WHERE report_day = %s AND population = %s",
        (day, population))}  # fmt: skip


def test_a_later_closure_does_not_rewrite_earlier_days(built):
    day2, day3 = inventory(built, DAY2), inventory(built, DAY3)
    # 10-27: jobs 1, 2, 4, 50 observed; job 3 possibly unavailable (missed at 23:59).
    assert day2["observed"][0] == 4 and day2["possibly_unavailable"] == (1, 1)
    # 10-28: job 4 closed, job 3 observed again.
    assert day3["closed"] == (1, 1) and day3["observed"][0] == 4


def test_a_duplicate_group_counts_as_one_opportunity(built):
    day2 = inventory(built, DAY2)
    # Jobs 1 and 50 share a requisition and title: 4 observed records, 3 opportunities.
    assert day2["observed"] == (4, 3)
    group = built.rows("intermediate", "SELECT opportunity_key, source_records, observed_members "
                       "FROM {t}.int_opportunity_days WHERE duplicate_group_id IS NOT NULL "
                       "AND report_day = %s", (DAY2,))  # fmt: skip
    assert [(r[1], r[2]) for r in group] == [(2, 2)]
    # On 10-26 the group did not exist yet: job 1 was its own opportunity.
    day1 = built.rows(
        "intermediate",
        "SELECT count(*) FROM {t}.int_opportunity_days "
        "WHERE report_day = %s AND duplicate_group_id IS NOT NULL",
        (DAY1,),
    )
    assert day1 == [(0,)]


def test_extractions_are_not_applied_before_they_existed(built):
    by_day = dict(built.rows("intermediate", "SELECT report_day, count(extraction_result_id) "
                             "FROM {t}.int_posting_days GROUP BY 1"))  # fmt: skip
    made = built.conn.execute("SELECT count(*) FROM extraction_results").fetchone()[0]
    assert made >= 1
    # Answers created on 10-27 appear from 10-27 on, never on 10-26.
    assert by_day[DAY1] == 0 and by_day[DAY2] == made


def test_skills_asked_as_alternatives_are_not_individual_requirements(built):
    rows = dict(((s, k), n) for s, k, n in built.rows(
        "reporting", "SELECT skill, mention_kind, postings FROM {t}.rpt_skill_demand "
        "WHERE population = 'all' AND requirement = 'required'"))  # fmt: skip
    assert ("Python", "alternative") in rows and ("Java", "alternative") in rows
    assert ("Python", "individual") not in rows and ("SQL", "individual") in rows


def test_eligibility_keeps_its_denominators_apart(built):
    rows = {status: r for status, *r in built.rows(
        "reporting", "SELECT status, postings, listed_postings, extracted_postings, "
        "stating_postings, share_of_listed, share_of_stating FROM {t}.rpt_eligibility "
        "WHERE concept = 'sponsorship' AND population = 'all'")}  # fmt: skip
    listed = rows["not_supported"][1]
    assert rows["not_supported"][2] <= listed  # extracted <= listed
    assert rows["not_supported"][5] == 100.0  # of postings stating a policy
    assert rows["not_extracted"][0] == listed - rows["not_supported"][2]
    assert rows["not_extracted"][5] is None  # unknown is never a share of "stating"


def test_coverage_and_health_disclose_the_gaps(built):
    coverage = dict((p, (listed, extracted)) for p, listed, extracted in built.rows(
        "reporting", "SELECT population, listed_postings, extracted FROM "
        "{t}.rpt_extraction_coverage"))  # fmt: skip
    assert coverage["all"][1] < coverage["all"][0]  # some listed postings are not extracted
    health = dict((d, (scans, failed, requests)) for d, scans, failed, requests in built.rows(
        "reporting", "SELECT report_day, scans, failed_scans, extraction_requests FROM "
        "{t}.rpt_pipeline_health_daily"))  # fmt: skip
    assert health[DAY1] == (4, 2, 0)  # two acme scans, two failed acme2 scans
    assert health[DAY2][2] >= 1  # requests recorded on the day they were sent


def test_a_rebuild_reproduces_the_reporting_tables(built):
    query = "SELECT * FROM {t}.rpt_daily_activity ORDER BY report_day, population"
    first = built.rows("reporting", query)
    built.build("--select", "reporting")
    assert built.rows("reporting", query) == first


def test_rebuilds_leave_ingestion_untouched(built):
    before = built.conn.execute(
        "SELECT (SELECT count(*) FROM source_postings), (SELECT count(*) FROM "
        "posting_availability_events), (SELECT count(*) FROM extraction_results)"
    ).fetchone()
    built.build()
    after = built.conn.execute(
        "SELECT (SELECT count(*) FROM source_postings), (SELECT count(*) FROM "
        "posting_availability_events), (SELECT count(*) FROM extraction_results)"
    ).fetchone()
    assert after == before
