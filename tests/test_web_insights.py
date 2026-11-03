"""The Market insights page (/insights) and the homepage prompt that leads to it, over the
synthetic history in search_world with the reporting models built on top of it."""

from dataclasses import replace
from datetime import timedelta

import pytest
from search_world import LAST_SCAN

pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")

from jobmarket.web import CONTENT_SECURITY_POLICY, create_app, insights  # noqa: E402
from jobmarket.web.config import WebConfig, load_web_config  # noqa: E402


def test_every_dashboard_has_a_question_an_objective_and_a_view():
    slugs = [d.slug for d in insights.DASHBOARDS]
    assert slugs[0] == "main" and len(set(slugs)) == len(slugs)
    for d in insights.DASHBOARDS:
        assert d.question.endswith("?") and d.objective and d.view.isalnum()


def test_embed_addresses_need_a_published_workbook():
    none = insights.load_tableau_config({})
    assert insights.embed_url(none, insights.BY_SLUG["main"]) is None
    config = insights.load_tableau_config({insights.WORKBOOK_ENV: " DFW Jobs/2026 "})
    url = insights.embed_url(config, insights.BY_SLUG["levels"])
    assert url.startswith("https://public.tableau.com/views/DFW%20Jobs%2F2026/JuniorVsSenior?")
    assert ":embed=y" in url and ":showVizHome=no" in url
    assert insights.view_url(config, insights.BY_SLUG["ai"]).endswith("/AIInTheJobMarket")


def test_the_profile_link_only_points_at_tableau_public():
    good = "https://public.tableau.com/app/profile/someone"
    assert insights.load_tableau_config({insights.PROFILE_ENV: good}).profile == good
    bad = insights.load_tableau_config({insights.PROFILE_ENV: "https://example.com/x"})
    assert bad.profile is None


def test_web_config_reads_the_tableau_settings():
    env = {"DB_NAME": "job_market", "DB_USER": "jobmarket", insights.WORKBOOK_ENV: "JobMarket"}
    config = load_web_config(env)
    assert config.tableau.workbook == "JobMarket"


def test_only_tableau_public_may_be_framed():
    assert "frame-src https://public.tableau.com" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY


@pytest.fixture(scope="module")
def reporting(world):
    # Rebuilding the shared upstream tables drops the serving views on them, so rebuild those too.
    world.analytics.build("--select", "+path:models/reporting", "+srv_postings", "+srv_boards")
    return world.analytics.schema("reporting")


def get(world, db_settings, url, reporting_schema, workbook=None):
    config = WebConfig(db_settings, serving_schema=world.schema, source_schema=world.source,
                       reporting_schema=reporting_schema,
                       clock=lambda: LAST_SCAN + timedelta(hours=1))  # fmt: skip
    if workbook:
        config = replace(config, tableau=insights.TableauConfig(workbook, None))
    app = create_app(config)
    app.testing = True
    response = app.test_client().get(url)
    return response, " ".join(response.get_data(as_text=True).split())


@pytest.mark.db
def test_headline_shares_count_only_jobs_with_a_clear_level(world, reporting):
    h = insights.headline(world.conn, reporting)
    query = f'SELECT level_ai, count(*) FROM "{reporting}".rpt_jobs_current GROUP BY 1'
    levels = dict(world.conn.execute(query).fetchall())
    known = sum(levels.get(k, 0) for k in ("Junior", "Mid", "Senior+"))
    assert h.jobs == sum(levels.values()) == 4
    assert h.junior_share == pytest.approx(levels.get("Junior", 0) / known)
    assert h.senior_share == pytest.approx(levels.get("Senior+", 0) / known)
    assert h.built_at is not None


@pytest.mark.db
def test_headline_is_absent_before_reports_are_published(world):
    assert insights.headline(world.conn, "no_such_schema") is None
    world.conn.execute("SELECT 1")  # the connection still works after the failed lookup


@pytest.mark.db
@pytest.mark.parametrize("schema", ["no_such_schema", None])
def test_homepage_prompt_leads_to_the_insights_page(world, db_settings, reporting, schema):
    response, html = get(world, db_settings, "/", schema or reporting)
    assert response.status_code == 200
    assert 'href="/insights">See the dashboards</a>' in html
    if schema is None:
        assert "% are junior" in html and "mention generative AI" in html
    else:  # no published reports: a plain prompt, and job search still works
        assert "junior vs senior hiring" in html and "% are junior" not in html


@pytest.mark.db
def test_insights_page_before_the_workbook_is_published(world, db_settings, reporting):
    response, html = get(world, db_settings, "/insights", reporting)
    assert response.status_code == 200
    assert "<iframe" not in html
    assert "The interactive dashboards are on their way" in html
    assert '4</span> <span class="kpi-label">open jobs' in html
    assert "Based on data collected through" in html
    assert html.count('class="dashboard-card"') == len(insights.DASHBOARDS) - 1


@pytest.mark.db
def test_insights_page_embeds_the_chosen_dashboard(world, db_settings, reporting):
    response, html = get(world, db_settings, "/insights?d=ai", reporting, workbook="JobMarket")
    assert response.status_code == 200
    assert 'src="https://public.tableau.com/views/JobMarket/AIInTheJobMarket?:embed=y' in html
    assert "Why it matters:</strong> See whether AI skills" in html
    assert "Based on data collected through" in html and "Open in Tableau Public" in html
    assert 'href="/insights">' in html and "Back to the main dashboard" in html


@pytest.mark.db
def test_an_unknown_dashboard_goes_back_to_the_main_one(world, db_settings, reporting):
    response, _ = get(world, db_settings, "/insights?d=nope", reporting)
    assert response.status_code == 302 and response.headers["Location"].endswith("/insights")
