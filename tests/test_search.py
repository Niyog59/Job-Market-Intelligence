"""Job search (jobmarket.search) over the serving views, on the synthetic history in
search_world (see there for the listings J1 to J8)."""

from datetime import timedelta

import pytest
from search_world import LAST_SCAN, at

from jobmarket.search import (
    PAGE_SIZE,
    STALE_AFTER,
    SearchParams,
    ServingUnavailable,
    parse_params,
    search,
    site_status,
)

pytestmark = pytest.mark.db


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
