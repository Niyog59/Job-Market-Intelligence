"""The owner's private pages (profile, saved jobs, applications, visits) and what public
visitors can and cannot see. Synthetic history from search_world; fictional profile data."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from search_world import LAST_SCAN

pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")
pytest.importorskip("argon2", reason="owner accounts need argon2-cffi")

from jobmarket import owner  # noqa: E402
from jobmarket.web import create_app  # noqa: E402
from jobmarket.web.config import WebConfig  # noqa: E402

pytestmark = pytest.mark.db

PASSWORD = "fictional owner pass"
START = LAST_SCAN + timedelta(hours=1)
SECRET_NOTE = "Fictional private note 7f3a"


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
def site(world, db_settings):
    private = f"{world.source}_private"
    world.conn.execute(f'TRUNCATE "{private}".accounts, "{private}".login_attempts CASCADE')
    owner.create_owner(world.conn, private, "owner", PASSWORD, START)
    clock = Clock()
    config = WebConfig(db_settings, private_settings=db_settings, serving_schema=world.schema,
                       private_schema=private, source_schema=world.source,
                       cookie_secure=False, clock=clock)  # fmt: skip
    app = create_app(config)
    app.testing = True

    class Site:
        pass

    s = Site()
    s.app, s.clock, s.private, s.conn, s.ids = app, clock, private, world.conn, world.ids
    yield s
    world.conn.execute(f'TRUNCATE "{private}".accounts, "{private}".login_attempts CASCADE')


def text(response):
    return " ".join(response.get_data(as_text=True).split())


def signed_in(site):
    client = site.app.test_client()
    page = client.get("/signin").get_data(as_text=True)
    token = re.search(r'name="signin_token" value="([^"]+)"', page).group(1)
    assert client.post("/signin", data={"signin_token": token, "username": "owner",
                                        "password": PASSWORD}).status_code == 302  # fmt: skip
    client.csrf = re.search(r'name="csrf_token" value="([^"]+)"',
                            client.get("/profile").get_data(as_text=True)).group(1)  # fmt: skip
    return client


def post(client, url, **data):
    return client.post(url, data={"csrf_token": client.csrf, **data})


PROFILE = {
    "skills-count": "2", "skills-0-name": "Python", "skills-1-name": "SQL",
    "experiences-count": "1", "experiences-0-kind": "internship",
    "experiences-0-title": "Fictional data intern", "experiences-0-start": "2025-06",
    "experiences-0-end": "2025-08", "education-count": "1", "education-0-level": "bachelor",
    "education-0-status": "expected", "education-0-graduation": "2027-05",
    "place": "dfw", "confirm": "on",
}  # fmt: skip


def test_every_private_page_and_form_needs_a_session(site):
    client = site.app.test_client()
    for url in ("/profile", "/saved", "/applications", "/account"):
        assert client.get(url).status_code == 302
    j2 = site.ids[2]
    for url in (f"/jobs/{j2}/save", f"/jobs/{j2}/unsave", f"/jobs/{j2}/status", "/profile",
                "/saved/1/note"):  # fmt: skip
        assert client.post(url, data={"csrf_token": "guess"}).status_code == 403


def test_profile_saves_and_keeps_entered_values_on_errors(site):
    client = signed_in(site)
    bad = post(client, "/profile", **dict(PROFILE, **{"experiences-0-end": "2025-01",
                                                      "confirm": ""}))  # fmt: skip
    page = text(bad)
    assert bad.status_code == 400 and "The end is before the start." in page
    assert 'value="Fictional data intern"' in page and "Confirm that this describes" in page
    good = post(client, "/profile", **PROFILE)
    assert good.status_code == 302 and "n=profile_saved" in good.location
    page = text(client.get(good.location))
    assert "Profile saved." in page and "Saved version 1" in page
    more = post(client, "/profile", **dict(PROFILE, more="skills"))
    assert more.status_code == 200 and text(more).count('name="skills-') >= 3 * 5


def test_save_is_idempotent_and_survives_closure(site):
    client = signed_in(site)
    j7 = site.ids[7]  # closed at the source
    for _ in range(2):
        assert post(client, f"/jobs/{j7}/save").status_code == 302
    rows = site.conn.execute(f'SELECT count(*) FROM "{site.private}".saved_jobs').fetchone()[0]
    assert rows == 1
    saved = text(client.get("/saved"))
    assert "Data Engineer II" in saved and "Closed" in saved
    # Saving the other posting of J1's group reuses the one entry.
    post(client, f"/jobs/{site.ids[1]}/save")
    post(client, f"/jobs/{site.ids[5]}/save")
    assert site.conn.execute(f'SELECT count(*) FROM "{site.private}".saved_jobs').fetchone()[0] == 2
    assert "as another posting of this job" in text(client.get(f"/jobs/{site.ids[5]}"))


def test_tracking_is_manual_with_history_and_corrections(site):
    client = signed_in(site)
    j2 = site.ids[2]
    detail = text(client.get(f"/jobs/{j2}"))
    assert "Not tracked. Opening the Apply link does not change this" in detail
    post(client, f"/jobs/{j2}/status", status="applied", event_date="2026-10-27", note="")
    post(client, f"/jobs/{j2}/status", status="rejected", event_date="2026-10-28", note="")
    event = site.conn.execute(f'SELECT max(event_id) FROM "{site.private}".application_events'
                              ).fetchone()[0]  # fmt: skip
    fixed = post(client, f"/jobs/{j2}/status", status="interviewing", event_date="2026-10-28",
                 note="It was an interview invite", corrects=str(event))  # fmt: skip
    assert "n=status_corrected" in fixed.location
    page = text(client.get(f"/jobs/{j2}"))
    assert "Interviewing</strong> since Oct 28, 2026" in page and "(corrected)" in page
    future = post(client, f"/jobs/{j2}/status", status="offer", event_date="2031-01-01")
    assert "n=status_future" in future.location
    assert "Interviewing" in text(client.get("/applications"))


def test_guessed_ids_are_not_found(site):
    client = signed_in(site)
    assert post(client, "/saved/987654/note", note="x").status_code == 404
    assert post(client, "/jobs/987654321/save").status_code == 404
    assert post(client, "/jobs/987654321/status", status="applied",
                event_date="2026-10-28").status_code == 404  # fmt: skip


def test_private_data_never_reaches_public_or_signed_out_pages(site):
    client = signed_in(site)
    j2 = site.ids[2]
    post(client, "/profile", **PROFILE)
    post(client, f"/jobs/{j2}/save")
    saved_id = site.conn.execute(f'SELECT saved_id FROM "{site.private}".saved_jobs').fetchone()[0]
    post(client, f"/saved/{saved_id}/note", note=SECRET_NOTE, posting_id=str(j2))
    assert SECRET_NOTE in text(client.get(f"/jobs/{j2}"))
    post(client, "/signout", csrf_token=client.csrf)
    for url in (f"/jobs/{j2}", "/?level=any", "/saved", "/profile"):
        page = text(client.get(url, follow_redirects=True))
        assert SECRET_NOTE not in page and "Fictional data intern" not in page
        assert "Your workspace" not in page and ">Saved<" not in page
    stranger = site.app.test_client()
    page = text(stranger.get(f"/jobs/{j2}"))
    assert "to save this job or track your application" in page and SECRET_NOTE not in page


def test_results_mark_saved_jobs_only_for_the_owner(site):
    client = signed_in(site)
    post(client, f"/jobs/{site.ids[2]}/save")
    assert 'class="tag">Saved<' in client.get("/?level=any").get_data(as_text=True)
    assert 'class="tag">Saved<' not in site.app.test_client().get("/?level=any").get_data(
        as_text=True)  # fmt: skip


def test_new_since_last_visit_uses_a_fixed_cutoff(site):
    first = text(signed_in(site).get("/?level=any"))
    assert "This is your first visit, so nothing is marked New yet" in first
    # A previous visit that ended before J3 was discovered (2026-10-27 17:00 UTC).
    site.conn.execute(f'TRUNCATE "{site.private}".visits')
    ended = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    site.conn.execute(f'INSERT INTO "{site.private}".visits (account_id, started_at, '
                      'last_activity_at) VALUES (1, %s, %s)', (ended, ended))  # fmt: skip
    client = signed_in(site)  # signing in starts a new visit
    page = text(client.get("/?level=any&new=1"))
    assert "<strong>1</strong> job" in page and "Backend Engineer" in page
    assert "New since your last visit" in page
    site.clock.now = START + timedelta(minutes=40)  # still the same visit: same cutoff
    assert "<strong>1</strong> job" in text(client.get("/?level=any&new=1&page=1"))
