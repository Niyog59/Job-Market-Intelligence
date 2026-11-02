"""The production entry point (jobmarket.web.wsgi) and the health check route."""

import importlib
from datetime import timedelta

import pytest
from search_world import LAST_SCAN

pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")

from flask import request  # noqa: E402

from jobmarket.web import create_app  # noqa: E402
from jobmarket.web.config import WebConfig  # noqa: E402

FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "jobs.example.test",
             "X-Forwarded-For": "203.0.113.7"}  # fmt: skip


def wsgi_app(monkeypatch, trust: str | None):
    if trust is None:
        monkeypatch.delenv("WEB_TRUST_PROXY", raising=False)
    else:
        monkeypatch.setenv("WEB_TRUST_PROXY", trust)
    # The module builds its app at import; a fresh import per test sees the environment.
    from jobmarket.web import wsgi

    app = importlib.reload(wsgi).app

    @app.get("/_probe")
    def probe():
        return f"{request.host_url} {request.remote_addr}"

    return app


def test_behind_the_proxy_the_app_sees_the_public_https_site(monkeypatch):
    app = wsgi_app(monkeypatch, "1")
    body = app.test_client().get("/_probe", headers=FORWARDED).get_data(as_text=True)
    assert body == "https://jobs.example.test/ 203.0.113.7"


def test_forwarded_headers_are_ignored_unless_trusted(monkeypatch):
    for trust in (None, "0", "yes"):
        app = wsgi_app(monkeypatch, trust)
        body = app.test_client().get("/_probe", headers=FORWARDED).get_data(as_text=True)
        assert body == "http://localhost/ 127.0.0.1"


@pytest.mark.db
def test_health_check(world, db_settings):
    config = WebConfig(db_settings, serving_schema=world.schema, source_schema=world.source,
                       clock=lambda: LAST_SCAN + timedelta(hours=1))  # fmt: skip
    response = create_app(config).test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok\n"
    assert response.headers["Cache-Control"] == "no-store"
