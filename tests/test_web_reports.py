"""The public Reports page (/reports) over the synthetic history in search_world, with the
reporting models built on top of it."""

import re
from datetime import timedelta

import pytest
from search_world import LAST_SCAN

pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")

from jobmarket import reports  # noqa: E402
from jobmarket.web import create_app  # noqa: E402
from jobmarket.web.config import WebConfig  # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def reporting(world):
    # Rebuilding the shared upstream tables drops the serving views on them, so rebuild those too.
    world.analytics.build("--select", "+path:models/reporting", "+srv_postings", "+srv_boards")
    return world.analytics.schema("reporting")


def page(world, db_settings, reporting_schema, url="/reports"):
    config = WebConfig(db_settings, serving_schema=world.schema, source_schema=world.source,
                       reporting_schema=reporting_schema,
                       clock=lambda: LAST_SCAN + timedelta(hours=1))  # fmt: skip
    app = create_app(config)
    app.testing = True
    response = app.test_client().get(url)
    return response, " ".join(response.get_data(as_text=True).split())


def row(html, label):
    """The cells of the table row headed `label`, as text."""
    start = html.index(f'<th scope="row">{label}')
    cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", html[start : html.index("</tr>", start)])
    return [" ".join(re.sub(r"<[^>]+>", " ", c).split()) for c in cells]


def test_reports_say_when_they_were_built_and_label_incomplete_processing(
    world, db_settings, reporting
):
    response, html = page(world, db_settings, reporting)
    assert response.status_code == 200
    assert "Reports built" in html
    assert "Reports change only when the analytics are rebuilt" in html
    # Kept jobs: J1, J2, J3, J4, J5 are listed; J3 arrived after the extraction run.
    assert "4 of 5 listed postings (80.0%) have structured details" in html
    assert "Processing is incomplete: 1 waiting in the queue" in html
    # The collection in job search is the one the build saw: no "newer collection" note.
    assert "is already in job search" not in html


def test_skill_columns_keep_alternatives_apart(world, db_settings, reporting):
    _, html = page(world, db_settings, reporting)
    assert row(html, "Python") == ["Python", "3", "1", "0"]  # J1, J4, J5 alone; J2 a choice
    assert row(html, "Java") == ["Java", "0", "1", "0"]
    assert row(html, "SQL") == ["SQL", "0", "0", "2"]
    assert "Out of 4 postings with structured details" in html


def test_eligibility_shows_all_three_denominators(world, db_settings, reporting):
    _, html = page(world, db_settings, reporting)
    # 2 of 5 listed, 2 of 4 with details, 2 of the 3 that state a sponsorship policy.
    assert row(html, "No sponsorship") == ["No sponsorship", "2", "40.0%", "50.0%", "66.7%"]
    assert row(html, "Sponsorship offered")[1:] == ["1", "20.0%", "25.0%", "33.3%"]
    # Not-yet-extracted postings are shown, never folded into "not stated".
    assert "No structured details yet" in html


def test_experience_buckets_in_natural_order(world, db_settings, reporting):
    data = reports.load_reports(world.conn, "kept", reporting)
    values = [r["value"] for r in data.experience["overall_min_years"]]
    assert values == ["1", "3-4", "5-7", "not_extracted"]
    _, html = page(world, db_settings, reporting)
    assert row(html, "1 year") == ["1 year", "2", "50.0%"]


def test_population_choice(world, db_settings, reporting):
    _, all_jobs = page(world, db_settings, reporting, "/reports?population=all")
    assert "5 of 6 listed postings" in all_jobs  # J6 is listed but not kept
    assert "Every listing collected from the tracked boards" in all_jobs
    _, unknown = page(world, db_settings, reporting, "/reports?population=everything")
    assert "4 of 5 listed postings" in unknown


def test_activity_inventory_health_and_boards(world, db_settings, reporting):
    _, html = page(world, db_settings, reporting)
    assert row(html, "Oct 26, 2026")[1] == "6"  # initial imports, kept jobs
    assert row(html, "Closed")[1] == "1"  # J7, closed on official evidence
    assert "so far today" in html  # the current day is provisional
    assert "2 enabled boards" in html
    assert "AI requests" in html


def test_a_newer_collection_than_the_build_is_announced(world, db_settings, reporting):
    table = f'"{reporting}".rpt_build_info'
    (saved,) = world.conn.execute(f"SELECT latest_complete_collection FROM {table}").fetchone()
    world.conn.execute(f"UPDATE {table} SET latest_complete_collection = %s",
                       (saved - timedelta(days=1),))  # fmt: skip
    try:
        _, html = page(world, db_settings, reporting)
    finally:
        world.conn.execute(f"UPDATE {table} SET latest_complete_collection = %s", (saved,))
    assert "is already in job search. These reports will include it after the next" in html


def test_before_the_first_build(world, db_settings):
    response, html = page(world, db_settings, "no_such_reporting_schema")
    assert response.status_code == 200
    assert "The reports have not been built yet" in html
    assert 'href="/"' in html  # job search still works


def test_reports_are_linked_and_public(world, db_settings, reporting):
    _, results = page(world, db_settings, reporting, "/")
    assert 'href="/reports"' in results
    _, html = page(world, db_settings, reporting)
    assert "Sign in" in html and "Sign out" not in html
