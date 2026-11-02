"""The dashboard tables (rpt_jobs_current, rpt_job_skills, rpt_skill_pairs, rpt_weekly_*) and
the generative-AI keyword rule, over the synthetic history in search_world (J1 to J8)."""

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def reporting(world):
    conn, source = world.conn, world.source
    # Generative-AI wording, and an employer whose own name contains a keyword.
    conn.execute(f'UPDATE "{source}".employers SET name = %s', ("Acme GenAI",))
    # J1 and J5 are the same job: its details come from the posting seen most recently.
    conn.execute(
        f'UPDATE "{source}".posting_versions SET description_text = coalesce(description_text, '
        "'') || %s WHERE posting_id = ANY(%s)",
        (" We build agentic workflows on top of LLMs.", [world.ids[1], world.ids[5]]),
    )
    conn.execute(
        f'UPDATE "{source}".posting_versions SET description_text = coalesce(description_text, '
        "'') || %s WHERE posting_id = %s",
        (" Acme GenAI is growing fast. Experience with AI is a plus.", world.ids[2]),
    )
    world.analytics.build("--select", "+path:models/reporting", "+srv_postings", "+srv_boards")
    return world.analytics.schema("reporting")


def rows(world, sql, *params):
    return world.conn.execute(sql, params).fetchall()


def job(world, reporting, posting_number):
    """The rpt_jobs_current row whose job contains the given synthetic posting."""
    result = world.conn.execute(
        f'SELECT * FROM "{reporting}".rpt_jobs_current WHERE job_key IN '
        f'(SELECT job_key FROM "{reporting}".rpt_jobs_current WHERE posting_id = %s) '
        "OR job_key = (SELECT 'group:' || duplicate_group_id FROM "
        f'"{world.source}_analytics_intermediate".int_current_postings WHERE posting_id = %s)',
        (world.ids[posting_number], world.ids[posting_number]),
    )
    names = [c.name for c in result.description]
    found = result.fetchall()
    return dict(zip(names, found[0], strict=True)) if found else None


def test_one_row_per_current_kept_job_with_duplicates_merged(world, reporting):
    keys = rows(world, f'SELECT job_key, postings_in_group FROM "{reporting}".rpt_jobs_current')
    # J1 and J5 are one job; J2, J3, J4 are listed; J7 closed, J8 inactive, J6 not kept.
    assert len(keys) == 4
    merged = job(world, reporting, 1)
    assert merged["postings_in_group"] == 2
    assert job(world, reporting, 7) is None and job(world, reporting, 6) is None


def test_levels_by_both_rules_and_role_groups(world, reporting):
    junior = job(world, reporting, 1)
    assert (junior["level_ai"], junior["level_years"]) == ("Junior", "0-2 years")
    assert junior["role_group"] == "Data"
    analyst = job(world, reporting, 2)
    assert (analyst["level_ai"], analyst["level_years"]) == ("Unclear", "3-4 years")
    senior = job(world, reporting, 4)
    assert senior["level_years"] == "5+ years" and senior["overall_min_years"] == 6
    pending = job(world, reporting, 3)  # first seen after the extraction run
    assert (pending["level_ai"], pending["role_group"]) == ("Not extracted", "Not extracted")


def test_genai_mentions_ignore_the_employers_own_name(world, reporting):
    assert job(world, reporting, 1)["genai_mention"] is True
    assert job(world, reporting, 1)["genai_terms"] == "LLM, AI agents"
    # "Acme GenAI" is the employer's name and a bare "AI" is not a term: no mention.
    assert job(world, reporting, 2)["genai_mention"] is False


def test_skills_and_pairs(world, reporting):
    table = f'"{reporting}".rpt_job_skills'
    analyst = job(world, reporting, 2)["job_key"]
    skills = rows(world, f"SELECT skill, requirement, mention_kind FROM {table} "
                  "WHERE job_key = %s ORDER BY 1, 2", analyst)  # fmt: skip
    assert skills == [("Java", "required", "alternative"), ("Python", "required", "alternative")]
    pairs = rows(world, f'SELECT skill_a, skill_b, jobs FROM "{reporting}".rpt_skill_pairs '
                 "WHERE role_group = 'Data' ORDER BY 1, 2")  # fmt: skip
    assert ("Java", "Python", 1) in pairs
    assert ("Python", "SQL", 1) in pairs  # J1: Python required, SQL preferred


def test_weekly_tables_count_each_job_once_per_rule(world, reporting):
    weekly = rows(world, f'SELECT level_rule, sum(open_jobs_end)::int FROM "{reporting}".'
                  "rpt_weekly_jobs WHERE week_start = (SELECT max(week_start) FROM "
                  f'"{reporting}".rpt_weekly_jobs) GROUP BY 1 ORDER BY 1')  # fmt: skip
    # The same open jobs, split by each level rule: both rules add up to the same total.
    assert weekly[0][1] == weekly[1][1] == 4
    new_by_rule = rows(world, f'SELECT level_rule, sum(new_jobs)::int FROM "{reporting}".'
                       "rpt_weekly_jobs GROUP BY 1 ORDER BY 1")  # fmt: skip
    assert new_by_rule[0][1] == new_by_rule[1][1]
    assert rows(world, f'SELECT count(*) FROM "{reporting}".rpt_weekly_skills')[0][0] >= 0
