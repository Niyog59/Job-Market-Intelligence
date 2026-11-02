"""The daily summary (jobmarket.summary): what it reports, once per day, quota pauses as
information only, and the heartbeat. Email delivery is mocked; nothing is sent."""

from datetime import timedelta

import httpx
import pytest
from gh_fakes import gh_job, ok

from jobmarket import summary
from jobmarket.notify import SmtpSettings, send_pending
from jobmarket.operations import record_step

pytestmark = pytest.mark.db
SERVER_ERROR = httpx.Response(503, content=b"unavailable")


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def test_summary_reports_jobs_problems_and_steps(env, monkeypatch):
    monkeypatch.setenv("JOBMARKET_AI_RPM", "10")
    monkeypatch.setenv("JOBMARKET_AI_DAILY_LIMIT", "100")
    env.transport.queue("acme", ok([gh_job(1, title="Data Engineer")]))
    env.transport.queue("beta", SERVER_ERROR)
    env.collector().collect("scheduled")
    now = env.clock.now + timedelta(minutes=30)
    record_step(env.conn, "backup", now, now, "succeeded",
                {"stamp": "20261102T030000Z", "bytes": 1234, "uploaded": True})  # fmt: skip
    record_step(env.conn, "report_publish", now, now, "failed", error="dbt test failed")

    result = summary.compose(env.conn, now + timedelta(minutes=1))
    body = result.body
    assert "1 scheduled partial" in body
    assert "New jobs:" in body  # first scan of a board is an initial import, not a new job
    assert "initial import" in body
    assert "Beta: failed scan, 0 of 3 retries used, next retry" in body
    assert "Pending retries: 1" in body
    assert "20261102T030000Z (1,234 bytes), confirmed in S3" in body
    assert "Latest rebuild FAILED" in body and "keeps the last good reports" in body
    assert "Waiting:" in body
    assert result.subject.endswith("(needs a look)")
    assert "report rebuild failed" in result.attention


def test_one_summary_per_day_even_when_rerun(env):
    env.transport.queue("acme", ok([]))
    env.transport.queue("beta", ok([]))
    env.collector().collect("scheduled")
    first, first_id = summary.queue_daily(env.conn, env.clock.now)
    again, again_id = summary.queue_daily(env.conn, env.clock.now + timedelta(minutes=5))
    assert first_id is not None and again_id is None
    assert env.scalar("SELECT count(*) FROM notifications WHERE kind = 'summary'") == 1

    tomorrow, tomorrow_id = summary.queue_daily(env.conn, env.clock.now + timedelta(days=1))
    assert tomorrow_id is not None and tomorrow.day != first.day
    # The next summary covers the time since the previous one.
    assert "Period:" in tomorrow.body


def test_quota_pause_is_information_not_an_alert(env, monkeypatch):
    monkeypatch.setenv("JOBMARKET_AI_RPM", "10")
    monkeypatch.setenv("JOBMARKET_AI_DAILY_LIMIT", "100")
    env.transport.queue("acme", ok([]))
    env.transport.queue("beta", ok([]))
    env.collector().collect("scheduled")
    from jobmarket.extraction import ExtractionConfig

    model = ExtractionConfig.from_env().model
    env.conn.execute(
        "INSERT INTO extraction_pauses (model, paused_until, reason, set_at) "
        "VALUES (%s, %s, 'daily quota exhausted (test)', %s)",
        (model, env.clock.now + timedelta(hours=5), env.clock.now),
    )
    result = summary.compose(env.conn, env.clock.now)
    assert "Paused by the daily free quota" in result.body
    assert "expected and resumes on its own" in result.body
    assert env.scalar("SELECT count(*) FROM notifications WHERE kind = 'alert'") == 0


def test_summary_is_delivered_through_the_outbox(env):
    env.transport.queue("acme", ok([]))
    env.transport.queue("beta", ok([]))
    env.collector().collect("scheduled")
    summary.queue_daily(env.conn, env.clock.now)
    FakeSMTP.sent.clear()
    settings = SmtpSettings("smtp.example", 587, "u", "p", "from@example.com",
                            "to@example.com", True)  # fmt: skip
    result = send_pending(env.conn, settings, env.clock.now, FakeSMTP)
    assert result.sent == 1
    assert FakeSMTP.sent[0]["Subject"].startswith("[Job Pipeline] Daily summary")
    assert env.scalar("SELECT status FROM notifications WHERE kind = 'summary'") == "sent"


def test_heartbeat():
    seen = []

    def client(**kwargs):
        def handle(request):
            seen.append(str(request.url))
            return httpx.Response(200)

        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    assert summary.ping_heartbeat({}, client) is None and seen == []
    url = "https://hc-ping.example/abc"
    assert summary.ping_heartbeat({summary.HEARTBEAT_ENV: url}, client) is None
    assert seen == [url]

    def failing(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                            **kwargs)  # fmt: skip

    problem = summary.ping_heartbeat({summary.HEARTBEAT_ENV: url}, failing)
    assert problem == "heartbeat failed: HTTPStatusError"
    assert url not in problem  # the URL is a secret
