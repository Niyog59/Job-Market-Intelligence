"""The job-search website (jobmarket.web) on the synthetic history in search_world (listings
J1 to J8; see there). Pages are rendered through Flask's test client."""

import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from search_world import LAST_SCAN

pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")

from jobmarket.web import CONTENT_SECURITY_POLICY, create_app  # noqa: E402
from jobmarket.web.config import WebConfig, load_web_config  # noqa: E402

pytestmark = pytest.mark.db


def client(world, db_settings, now=LAST_SCAN + timedelta(hours=1), schema=None):
    config = WebConfig(db_settings, serving_schema=schema or world.schema,
                       source_schema=world.source, clock=lambda: now)  # fmt: skip
    app = create_app(config)
    app.testing = True
    return app


def get(world, db_settings, url, **kw):
    response = client(world, db_settings, **kw).test_client().get(url)
    # Whitespace collapsed, so sentences that templates wrap across lines still match.
    return response, " ".join(response.get_data(as_text=True).split())


def job(world, n):
    return f"/jobs/{world.ids[n]}"


def test_results_default_view_and_unknown_labels(world, db_settings):
    response, html = get(world, db_settings, "/")
    assert response.status_code == 200
    assert "<strong>3</strong> jobs" in html
    assert "Included with unknown information: Early career: early-career status unclear" in html
    assert "Early career: structured details pending" in html
    assert "Senior Data Engineer" not in html  # contradicts the early-career default
    assert "2 postings of one job" in html  # J1 and J5 shown once


def test_security_headers(world, db_settings):
    response, _ = get(world, db_settings, "/")
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_pages_use_a_read_only_session(world, db_settings):
    app = client(world, db_settings)
    with app.test_request_context("/"):
        conn = app.extensions["jobmarket_db"]()
        assert conn.read_only
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE web_must_not_write (i int)")


def test_detail_shows_requirements_with_natural_relationships_and_evidence(world, db_settings):
    response, html = get(world, db_settings, job(world, 2))
    assert response.status_code == 200
    assert "Python or Java" in html and "any one of these is enough" in html
    assert "Visa sponsorship offered" in html
    assert "View source evidence" not in html  # the scripted answers carry no quotes
    assert "Quoted from version" not in html


def test_descriptions_are_sanitized_and_links_are_safe(world, db_settings):
    _, html = get(world, db_settings, job(world, 2))
    description = html.split('class="description"', 1)[1].split("</section>", 1)[0]
    assert "Looker dashboards" in description
    assert "<script" not in description and "javascript:" not in html
    assert "Apply here" in description  # the link text stays, the link does not
    for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", html):
        assert 'rel="noopener noreferrer"' in tag or "noopener" in tag


def test_pending_extraction_is_not_shown_as_not_stated(world, db_settings):
    _, html = get(world, db_settings, job(world, 3))
    assert "Structured details pending" in html
    assert "req-table" not in html
    assert "Build things with the team." in html  # the description is still readable


def test_archived_jobs_stay_readable_without_a_misleading_apply_button(world, db_settings):
    _, closed = get(world, db_settings, job(world, 7))
    assert "This posting was confirmed closed" in closed
    assert "employer page says filled" in closed and "This position has been filled." in closed
    assert "Apply on company site" not in closed
    _, inactive = get(world, db_settings, job(world, 8))
    assert "Assumed inactive" in inactive and "Check on company site" in inactive
    assert "Apply on company site" not in inactive
    _, archived = get(world, db_settings, "/?view=archived&level=any")
    assert "Data Engineer II" in archived and "Analytics Engineer" in archived


def test_possibly_unavailable_is_active_with_a_warning(world, db_settings):
    _, html = get(world, db_settings, job(world, 4))
    assert "Possibly unavailable" in html
    assert "Missing from 1 of the 3 qualifying scans" in html
    assert "Apply on company site" in html


def test_duplicate_group_lists_every_posting(world, db_settings):
    _, html = get(world, db_settings, job(world, 1))
    assert "Postings of this job (2)" in html
    assert f'href="/jobs/{world.ids[5]}' in html  # the other copy, inspectable on its own


def test_stale_boards_are_flagged(world, db_settings):
    later = LAST_SCAN + timedelta(hours=40)
    _, results = get(world, db_settings, "/?level=any", now=later)
    assert "May be out of date" in results
    assert "had no complete collection in the last 36 hours" in results
    _, detail = get(world, db_settings, job(world, 2), now=later)
    assert "The status above may be out of date" in detail


def test_back_to_results_restores_the_search_and_cannot_leave_the_site(world, db_settings):
    _, results = get(world, db_settings, "/?level=any&sort=published&skill=Python")
    link = re.search(r'href="(/jobs/\d+\?from=[^"]+)"', results).group(1)
    origin = parse_qs(urlsplit(link.replace("&amp;", "&")).query)["from"][0]
    _, detail = get(world, db_settings, link.replace("&amp;", "&"))
    back = re.search(r'href="([^"]*)" data-back', detail).group(1).replace("&amp;", "&")
    assert back == "/?" + origin
    assert parse_qs(urlsplit(back).query) == {
        "level": ["any"], "skill": ["Python"], "sort": ["published"]}  # fmt: skip
    _, hostile = get(world, db_settings, job(world, 2) + "?from=%2F%2Fevil.example%2F")
    back = re.search(r'href="([^"]*)" data-back', hostile).group(1)
    assert back == "/"  # unknown parameters are dropped; the path is always the results page


def test_invalid_links_and_missing_data(world, db_settings):
    assert get(world, db_settings, "/jobs/987654321")[0].status_code == 404
    assert get(world, db_settings, "/jobs/not-a-number")[0].status_code == 404
    response, html = get(world, db_settings, "/", schema="no_such_schema")
    assert response.status_code == 503 and "jobmarket analytics build" in html


def test_empty_results_suggest_ways_to_widen(world, db_settings):
    _, html = get(world, db_settings, "/?q=nothing-matches-this&unknown=exclude")
    assert "No jobs" in html and "Include jobs with unknown information" in html


def test_the_website_logs_in_as_the_read_only_login():
    config = load_web_config(
        {
            "DB_NAME": "job_market",
            "DB_USER": "jobmarket",
            "DB_PASSWORD": "owner-secret",
            "ANALYTICS_DB_PASSWORD": "ro",
        }
    )
    assert config.settings.db_user == "jobmarket_analytics"
    assert config.settings.db_password == "ro"
