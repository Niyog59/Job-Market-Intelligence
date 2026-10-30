"""Signing in and out of the website, and the boundary around private pages. Runs on the
synthetic history in search_world; the owner account and password are fictional."""

import re
from datetime import timedelta

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


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
def site(world, db_settings):
    private = f"{world.source}_private"
    conn = world.conn
    conn.execute(f'TRUNCATE "{private}".accounts, "{private}".login_attempts CASCADE')
    owner.create_owner(conn, private, "owner", PASSWORD, START)
    clock = Clock()

    def make(secure=False, private_settings=db_settings):
        config = WebConfig(db_settings, private_settings=private_settings,
                           serving_schema=world.schema, private_schema=private,
                           source_schema=world.source, cookie_secure=secure,
                           clock=clock)  # fmt: skip
        app = create_app(config)
        app.testing = True
        return app

    class Site:
        pass

    s = Site()
    s.make, s.clock, s.private, s.conn = make, clock, private, conn
    yield s
    conn.execute(f'TRUNCATE "{private}".accounts, "{private}".login_attempts CASCADE')


def sign_in(client, password=PASSWORD, next_path="/account", username="owner"):
    page = client.get("/signin").get_data(as_text=True)
    token = re.search(r'name="signin_token" value="([^"]+)"', page).group(1)
    return client.post("/signin", data={"signin_token": token, "username": username,
                                        "password": password, "next": next_path})  # fmt: skip


def csrf(client):
    page = client.get("/account").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def test_private_pages_require_a_session(site):
    client = site.make().test_client()
    response = client.get("/account")
    assert response.status_code == 302 and "/signin?next=/account" in response.location
    assert client.post("/signout").status_code == 302  # nothing to end; just clears the cookie
    client.set_cookie("jm_session", "a-guessed-token")
    assert client.get("/account").status_code == 302


def test_sign_in_sets_a_strict_http_only_cookie_and_a_new_session(site):
    client = site.make(secure=True).test_client()
    response = sign_in(client)
    assert response.status_code == 302 and response.location == "/account"
    cookie = next(h for h in response.headers.getlist("Set-Cookie")
                  if h.startswith("__Host-jm_session="))  # fmt: skip
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
    assert "Path=/" in cookie and "Domain" not in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    hashed = site.conn.execute(f'SELECT token_hash FROM "{site.private}".sessions').fetchone()[0]
    assert hashed == owner.token_hash(token)


def test_signed_in_pages_are_never_cached(site):
    client = site.make().test_client()
    public = client.get("/")
    assert "no-store" not in public.headers.get("Cache-Control", "")
    sign_in(client)
    assert client.get("/account").headers["Cache-Control"] == "no-store"
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert "Cookie" in client.get("/").headers["Vary"]


def test_sign_out_ends_the_session_on_the_server(site):
    client = site.make().test_client()
    sign_in(client)
    token = csrf(client)
    assert client.post("/signout").status_code == 403  # no CSRF token
    response = client.post("/signout", data={"csrf_token": token})
    assert response.status_code == 302
    assert response.headers["Clear-Site-Data"] == '"cache", "cookies", "storage"'
    assert site.conn.execute(f'SELECT count(*) FROM "{site.private}".sessions').fetchone()[0] == 0
    assert client.get("/account").status_code == 302


def test_a_copied_cookie_stops_working_after_sign_out(site):
    client = site.make().test_client()
    response = sign_in(client)
    stolen = re.search(r"jm_session=([^;]+)", response.headers["Set-Cookie"]).group(1)
    client.post("/signout", data={"csrf_token": csrf(client)})
    other = site.make().test_client()
    other.set_cookie("jm_session", stolen)
    assert other.get("/account").status_code == 302


def test_forms_need_the_session_token_and_this_origin(site):
    client = site.make().test_client()
    sign_in(client)
    token = csrf(client)
    assert client.post("/signout", data={"csrf_token": "forged"}).status_code == 403
    hostile = client.post("/signout", data={"csrf_token": token},
                          headers={"Origin": "https://evil.example"})  # fmt: skip
    assert hostile.status_code == 403
    assert client.get("/account").status_code == 200  # still signed in


def test_sign_in_form_needs_its_own_token(site):
    client = site.make().test_client()
    client.get("/signin")
    response = client.post("/signin", data={"signin_token": "forged", "username": "owner",
                                            "password": PASSWORD})  # fmt: skip
    assert response.status_code == 401 and "expired" in response.get_data(as_text=True)


def test_wrong_passwords_pause_sign_in(site):
    client = site.make().test_client()
    for _ in range(3):
        page = sign_in(client, password="wrong").get_data(as_text=True)
        assert "not correct" in page
    page = sign_in(client).get_data(as_text=True)
    assert "Sign-in is paused; try again in 15 minutes" in page
    site.clock.now = START + timedelta(minutes=16)
    assert sign_in(client).status_code == 302


def test_idle_sessions_expire(site):
    client = site.make().test_client()
    sign_in(client)
    site.clock.now = START + timedelta(hours=2, minutes=1)
    assert client.get("/account").status_code == 302


def test_return_paths_never_leave_the_site(site):
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        assert sign_in(site.make().test_client(), next_path=target).location == "/"


def test_browsing_works_without_private_setup(site):
    app = site.make(private_settings=None)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert "PRIVATE_DB_PASSWORD is missing" in client.get("/signin").get_data(as_text=True)
    assert "Sign in" in client.get("/").get_data(as_text=True)


def test_browser_style_posts_with_this_origin_are_accepted(site):
    # Real browsers send Origin on form posts; the page's referrer policy keeps it real.
    client = site.make().test_client()
    assert client.get("/signin").headers["Referrer-Policy"] == "same-origin"
    page = client.get("/signin").get_data(as_text=True)
    token = re.search(r'name="signin_token" value="([^"]+)"', page).group(1)
    form = {"signin_token": token, "username": "owner", "password": PASSWORD}
    assert client.post("/signin", data=form, headers={"Origin": "null"}).status_code == 403
    ok = client.post("/signin", data=form, headers={"Origin": "http://localhost"})
    assert ok.status_code == 302


def test_the_account_page_changes_the_password(site):
    client = site.make().test_client()
    sign_in(client)
    other = site.make().test_client()
    sign_in(other)
    token = csrf(client)
    form = {
        "csrf_token": token,
        "current_password": PASSWORD,
        "new_password": "another fictional pass",
        "repeat_password": "different",
    }
    assert client.post("/account/password", data=form).status_code == 400
    assert (
        client.post("/account/password", data={**form, "csrf_token": "forged"}).status_code == 403
    )
    wrong = client.post(
        "/account/password",
        data={**form, "current_password": "nope", "repeat_password": "another fictional pass"},
    )
    assert wrong.status_code == 400 and "current password is not correct" in wrong.get_data(
        as_text=True
    )
    done = client.post("/account/password", data={**form, "repeat_password":
                                                   "another fictional pass"})  # fmt: skip
    assert done.status_code == 302 and "changed=1" in done.location
    assert client.get("/account").status_code == 200  # this browser stays signed in
    assert other.get("/account").status_code == 302  # the other one is signed out
    assert sign_in(site.make().test_client(), password="another fictional pass").status_code == 302
