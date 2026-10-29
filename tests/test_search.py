"""Job search (jobmarket.search) over the serving views, on a synthetic history built with
the real collector, availability rules, duplicate rules, and extraction queue. Only HTTP
responses and model answers are scripted.

Board acme (Greenhouse) lists:
  J1  Junior Data Engineer, Dallas    early career; Python required, SQL preferred,
                                      Airflow only mentioned; no sponsorship; hybrid; 1 year
  J2  Data Analyst, Houston           early career unclear; Python or Java (required);
                                      sponsorship supported; on site; 3 years; "Looker"
  J3  Backend Engineer, Dallas        first seen after the extraction run: pending
  J4  Senior Data Engineer, Dallas    not early career; 6 years; missing from the last scan
                                      (possibly unavailable); no source publication date
  J6  Sales Manager, Dallas           not kept by the screening rules
  J7  Data Engineer II, Dallas        early career; closed on official evidence
  J8  Analytics Engineer, Dallas      early career; missing from three qualifying scans
Board acme2 (same employer) lists J5, Junior Data Engineer in Houston with J1's
requisition: a strong duplicate group with J1. acme2 is scanned last, so J5 is the most
recently observed copy.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from analytics_harness import Analytics
from extraction_fakes import answer, reply
from gh_fakes import FakeClock, ScriptedTransport, board, gh_job, ok, registry

from jobmarket import db
from jobmarket.availability import close_posting
from jobmarket.collector import Collector
from jobmarket.extraction import ExtractionConfig, run_extraction
from jobmarket.migrate import apply_migrations
from jobmarket.search import (
    PAGE_SIZE,
    STALE_AFTER,
    SearchParams,
    ServingUnavailable,
    load_choices,
    parse_params,
    search,
    site_status,
)
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def loc(city: str) -> list[dict]:
    return [{"original": f"{city}, TX", "city": city, "state": "TX", "country": None,
             "evidence": []}]  # fmt: skip


def arrangement(value: str) -> dict:
    return {"value": value, "onsite_days_per_week": None, "onsite_frequency_text": None,
            "evidence": []}  # fmt: skip


def years(n: int) -> list[dict]:
    return [{"scope": "overall", "subject": None, "min_years": n, "max_years": None,
             "status": "required", "alternative_group": None, "evidence": []}]  # fmt: skip


def early(status: str) -> dict:
    return {"status": status, "basis": [], "evidence": []}


def skill(name: str, status: str) -> dict:
    return {"name": name, "status": status, "evidence": []}


def role(category: str) -> list[dict]:
    return [{"category": category, "basis": "title", "evidence": []}]


def employment(value: str) -> dict:
    return {"value": value, "evidence": []}


JUNIOR = dict(
    role_categories=role("data_engineering"), early_career=early("yes"), experience=years(1),
    skills=[skill("Python", "required"), skill("SQL", "preferred"),
            skill("Airflow", "mentioned")],
    sponsorship={"status": "not_supported", "evidence": []},
    work_arrangement=arrangement("hybrid"), employment_type=employment("full_time"),
)  # fmt: skip
ANSWERS = {
    ("Junior Data Engineer", "Dallas"): answer(**JUNIOR, locations=loc("Dallas")),
    ("Junior Data Engineer", "Houston"): answer(**JUNIOR, locations=loc("Houston")),
    ("Data Analyst", "Houston"): answer(
        role_categories=role("data_analysis"), early_career=early("unclear"),
        experience=years(3), locations=loc("Houston"),
        skills=[skill("Python", "required"), skill("Java", "required")],
        skill_groups=[{"relation": "any_of", "min_count": None, "members": ["Python", "Java"],
                       "status": "required", "display_text": "Python or Java",
                       "evidence": []}],
        sponsorship={"status": "supported", "evidence": []},
        work_arrangement=arrangement("onsite"),
    ),
    ("Senior Data Engineer", "Dallas"): answer(
        role_categories=role("data_engineering"), early_career=early("no"),
        experience=years(6), locations=loc("Dallas"), skills=[skill("Python", "required")],
    ),
    ("Sales Manager", "Dallas"): answer(role_categories=role("other"), early_career=early("yes"),
                                        locations=loc("Dallas")),
    ("Data Engineer II", "Dallas"): answer(role_categories=role("data_engineering"),
                                           early_career=early("yes"), locations=loc("Dallas")),
    ("Analytics Engineer", "Dallas"): answer(role_categories=role("analytics_engineering"),
                                             early_career=early("yes"), locations=loc("Dallas")),
}  # fmt: skip


class ByPosting:
    """Answers by the posting's title and city, as read from the extraction document."""

    def __call__(self, instructions: str, text: str, schema: dict):
        for (title, city), value in ANSWERS.items():
            if f"Title: {title}\nLocation: {city}" in text:
                return reply(value)
        raise AssertionError(f"no scripted answer for {text[:80]!r}")


def job(n, title, city="Dallas", token="acme", req=None, published="2026-10-19T09:00:00-05:00",
        content="<p>Build things with the team.</p>"):  # fmt: skip
    item = gh_job(n, title=title, location=f"{city}, TX", token=token, content=content,
                  first_published=published)  # fmt: skip
    item["requisition_id"] = req or f"R{n}"
    return item


J1 = job(1, "Junior Data Engineer")
J2 = job(2, "Data Analyst", "Houston", published="2026-10-25T09:00:00-05:00",
         content="<p>Analyze retention with Looker dashboards.</p>")  # fmt: skip
J3 = job(3, "Backend Engineer")
J4 = job(4, "Senior Data Engineer", published=None)
J6 = job(6, "Sales Manager")
J7 = job(7, "Data Engineer II")
J8 = job(8, "Analytics Engineer")
J5 = job(5, "Junior Data Engineer", "Houston", token="acme2", req="R1")
LAST_SCAN = at("2026-10-28T19:00:00")


@pytest.fixture(scope="module")
def world(db_settings, tmp_path_factory):
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

    def collect(when, acme=None, acme2=None):
        clock.now = at(when)
        tokens = []
        for token, response in (("acme", acme), ("acme2", acme2)):
            if response is not None:
                transport.queues[token] = [response]
                tokens.append(token)
        Collector(conn, httpx.Client(transport=transport), raw, clock=clock,
                  sleep=lambda s: None, interval=0).collect(tokens=tokens)  # fmt: skip

    collect("2026-10-26T17:00:00", ok([J1, J2, J4, J6, J7, J8]), ok([J5]))  # baselines
    run_extraction(conn, ByPosting(), ExtractionConfig(rpm=60, daily_limit=100),
                   lambda: at("2026-10-26T18:00:00"), lambda s: None)  # fmt: skip
    collect("2026-10-27T17:00:00", ok([J1, J2, J3, J4, J6, J7]))  # J3 new; J8 missing (1)
    collect("2026-10-28T17:00:00", ok([J1, J2, J3, J4, J6, J7]))  # J8 missing (2)
    collect("2026-10-28T18:00:00", ok([J1, J2, J3, J6, J7]))  # J8 (3): inactive; J4 (1)
    collect("2026-10-28T19:00:00", acme2=ok([J5]))  # J5 is now the newest-seen copy
    j7 = conn.execute("SELECT posting_id FROM source_postings WHERE source_posting_id = '7'")
    close_posting(conn, j7.fetchone()[0], "employer page says filled",
                  "https://example.test/7", "This position has been filled.",
                  at("2026-10-28T19:30:00"))  # fmt: skip

    analytics = Analytics(conn, schema)
    analytics.build("--select", "+srv_postings", "+srv_boards")
    ids = dict(conn.execute("SELECT source_posting_id, posting_id FROM source_postings"))

    class World:
        pass

    w = World()
    w.conn, w.schema, w.ids = conn, analytics.schema("serving"), {int(k): v for k, v in ids.items()}
    w.choices = load_choices(conn, w.schema)
    yield w
    analytics.drop(db_settings)
    conn.close()
    admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.close()


def run(w, now=LAST_SCAN + timedelta(hours=1), page_size=PAGE_SIZE, **args):
    params, problems = parse_params(args, w.choices)
    return search(w.conn, params, now, problems, schema=w.schema, page_size=page_size)


def shown(result, w):
    """Results as source job numbers (the listing shown for each opportunity)."""
    back = {v: k for k, v in w.ids.items()}
    return [back[row.posting_id] for row in result.rows]


def test_default_is_kept_active_early_career_with_unknowns_labeled(world):
    result = run(world)
    rows = {r.posting_id: r for r in result.rows}
    # J1/J5 group (early career), J2 (unclear), J3 (pending). J4 is not early career, J6 is
    # not kept, J7 and J8 are archived.
    assert sorted(shown(result, world)) == [2, 3, 5]
    assert result.total == 3
    assert rows[world.ids[5]].unknown_reasons == []
    assert rows[world.ids[2]].unknown_reasons == ["Early career: early-career status unclear"]
    assert rows[world.ids[3]].unknown_reasons == ["Early career: structured details pending"]
    assert rows[world.ids[3]].extraction_status in ("pending", "not_queued")


def test_unknowns_can_be_excluded(world):
    assert shown(run(world, unknown="exclude"), world) == [5]


def test_a_confident_duplicate_group_is_one_result_shown_by_its_newest_copy(world):
    result = run(world, level="any")
    group = [r for r in result.rows if r.records == 2]
    assert len(group) == 1 and result.total == 4  # group, J2, J3, J4
    assert group[0].posting_id == world.ids[5]  # J5 was observed after J1
    assert group[0].locations == ["Dallas, TX", "Houston, TX"]
    # The group was discovered when its first member was: an initial import.
    assert group[0].cohort == "initial_import"


def test_possibly_unavailable_stays_active_and_archived_jobs_are_separate(world):
    active = {r.posting_id: r for r in run(world, level="any").rows}
    assert active[world.ids[4]].group_state == "possibly_unavailable"
    assert active[world.ids[4]].qualifying_absences == 1
    archived = run(world, level="any", view="archived")
    states = {r.posting_id: r.group_state for r in archived.rows}
    assert states == {world.ids[7]: "closed", world.ids[8]: "assumed_inactive"}
    assert archived.other_view_total == 4


def test_explicit_contradictions_never_pass_with_unknowns_allowed(world):
    result = run(world, level="any", sponsorship="offers")
    reasons = {r.posting_id: r.unknown_reasons for r in result.rows}
    # J2 supports sponsorship; J1/J5 explicitly do not (excluded); J3 is pending; J4 does not
    # state it.
    assert set(reasons) == {world.ids[2], world.ids[3], world.ids[4]}
    assert reasons[world.ids[2]] == []
    assert reasons[world.ids[4]] == ["Sponsorship: not stated"]
    assert reasons[world.ids[3]] == ["Sponsorship: structured details pending"]


def test_skills_count_alternatives_but_not_passing_mentions(world):
    java = run(world, level="any", skill="java", unknown="exclude")
    assert shown(java, world) == [2]
    assert java.rows[0].skill_notes == ["Java: required, one of Python or Java"]
    # Airflow is only mentioned by J1: not a request for it.
    airflow = run(world, level="any", skill="Airflow", unknown="exclude")
    assert airflow.total == 0
    assert airflow.problems == ["No listing asks for Airflow as a required or preferred skill."]
    both = run(world, level="any", skill=["Python", "SQL"], skill_mode="all", unknown="exclude")
    assert shown(both, world) == [5]
    either = run(world, level="any", skill=["Java", "SQL"], unknown="exclude")
    assert sorted(shown(either, world)) == [2, 5]


def test_experience_bands_use_the_required_overall_minimum(world):
    assert shown(run(world, level="any", experience="0-1", unknown="exclude"), world) == [5]
    assert shown(run(world, level="any", experience="2-4", unknown="exclude"), world) == [2]
    assert shown(run(world, level="any", experience="5+", unknown="exclude"), world) == [4]


def test_a_group_matches_through_any_member(world):
    # J1 is in Dallas, its duplicate J5 in Houston.
    result = run(world, level="any", place="houston", unknown="exclude")
    assert sorted(shown(result, world)) == [2, 5]
    assert sorted(shown(run(world, level="any", place="dfw", unknown="exclude"), world)) == [4, 5]


def test_text_search_covers_title_description_and_employer(world):
    assert shown(run(world, level="any", q="looker"), world) == [2]  # description
    assert shown(run(world, level="any", q="analysts"), world) == [2]  # stemmed title
    assert run(world, level="any", q="acme").total == 4  # employer name
    hostile = run(world, level="any", q="'; DROP TABLE source_postings; --")
    assert hostile.total == 0
    assert world.conn.execute("SELECT count(*) FROM source_postings").fetchone()[0] == 8


def test_newest_discovery_first_and_source_dates_with_missing_last(world):
    discovered = run(world, level="any")
    assert shown(discovered, world)[0] == 3  # the only job discovered after its baseline
    cohorts = {r.posting_id: r.cohort for r in discovered.rows}
    assert cohorts[world.ids[3]] == "discovered"
    assert cohorts[world.ids[2]] == "initial_import"
    published = run(world, level="any", sort="published")
    assert shown(published, world)[0] == 2  # published 2026-10-25
    assert shown(published, world)[-1] == 4  # no publication date
    assert published.rows[-1].source_date is None


def test_pages_are_bounded_and_a_page_past_the_end_shows_the_last(world):
    first = run(world, level="any", page_size=3)
    assert (first.total, first.pages, len(first.rows), first.first, first.last) == (4, 2, 3, 1, 3)
    late = run(world, level="any", page_size=3, page="9")
    assert (late.page, len(late.rows), late.first) == (2, 1, 4)
    everything = shown(first, world) + shown(late, world)
    assert len(set(everything)) == 4  # deterministic order: no row repeats or goes missing


def test_all_listings_includes_jobs_the_rules_did_not_keep(world):
    assert world.ids[6] not in {r.posting_id for r in run(world, level="any").rows}
    every = run(world, level="any", population="all")
    assert world.ids[6] in {r.posting_id for r in every.rows}


def test_stale_boards_are_flagged_after_36_hours(world):
    def stale(now):
        return {r.posting_id: r.stale for r in run(world, now=now, level="any").rows}

    fresh = stale(LAST_SCAN + timedelta(hours=1))
    assert not any(fresh.values())
    # acme's last complete scan was at 18:00, acme2's at 19:00.
    later = stale(at("2026-10-28T18:00:00") + STALE_AFTER + timedelta(minutes=1))
    assert later[world.ids[2]] and not later[world.ids[5]]
    status = site_status(world.conn, LAST_SCAN + timedelta(hours=1), schema=world.schema)
    assert (status.boards, status.stale_boards, status.held_boards) == (2, 0, 0)


def test_invalid_parameters_are_dropped_and_reported(world):
    params, problems = parse_params(
        {"view": "deleted", "role": ["bi", "nonsense"], "skill": "x" * 200, "page": "-3",
         "employer": "999999", "sort": "salary"},
        world.choices,
    )  # fmt: skip
    assert params.view == "active" and params.roles == ("bi",) and params.skills == ()
    assert params.page == 1 and params.employers == () and params.sort == "discovered"
    assert len(problems) >= 5


def test_search_state_round_trips_through_the_url(world):
    params = SearchParams(
        q="data",
        level="any",
        roles=("bi", "backend"),
        skills=("Python",),
        skill_mode="all",
        include_unknown=False,
        sort="published",
        page=2,
    )
    parsed, problems = parse_params(_args(params.query_pairs()), world.choices)
    assert parsed == params and problems == []
    assert SearchParams().query_string() == ""  # defaults are omitted


def _args(pairs):
    class Args(dict):
        def getlist(self, key):
            return [v for k, v in pairs if k == key]

    return Args(pairs)


def test_missing_serving_views_are_reported(world):
    with pytest.raises(ServingUnavailable):
        search(world.conn, SearchParams(), LAST_SCAN, schema="no_such_schema")
