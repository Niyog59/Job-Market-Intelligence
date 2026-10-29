"""Requirement extraction queue: priorities, caching, retries, quota pauses, recovery,
configuration changes, and idempotent storage. The model is always a scripted fake."""

from datetime import UTC, datetime, timedelta

import pytest
from extraction_fakes import ScriptedModel, answer, quota_error, reply
from gh_fakes import T0, FakeClock, board, gh_job, ok, registry
from test_history import HOUR, History

from jobmarket import cli, db, extraction
from jobmarket.extraction import ExtractionConfig, enqueue_new, reprocess, run_extraction
from jobmarket.gemini import ModelError
from jobmarket.listings import listing_detail
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

CONFIG = ExtractionConfig(rpm=60, daily_limit=100)


class X(History):
    def __init__(self, conn):
        super().__init__(conn)
        self.clock = FakeClock(T0 + timedelta(days=1))
        self.slept: list[float] = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))

    def run(self, model, config=CONFIG, limit=None):
        return run_extraction(self.conn, model, config, self.clock, self.sleep, limit)

    def jobs(self):
        return self.conn.execute(
            "SELECT p.source_posting_id, j.priority, j.reason, j.status FROM extraction_jobs j "
            "JOIN source_postings p USING (posting_id) ORDER BY j.job_id"
        ).fetchall()

    def track_from(self, at):
        self.conn.execute("UPDATE extraction_settings SET tracking_started_at = %s", (at,))


@pytest.fixture
def x(schema_conn):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    return X(schema_conn)


ACCOUNTANT = dict(title="Staff Accountant", content="<p>Close the books monthly.</p>")


def job(n, **kw):
    """A posting with its own text (identical text would reuse one answer)."""
    kw.setdefault("content", f"<p>Build and maintain data pipelines, team {n}.</p>")
    return gh_job(n, **kw)


def test_priorities_put_new_retained_content_first(x):
    x.collect(T0, [gh_job(1), gh_job(2, **ACCOUNTANT)])  # backlog: before tracking started
    x.track_from(T0 + HOUR / 2)
    x.collect(T0 + HOUR, [gh_job(1), gh_job(2, **ACCOUNTANT), gh_job(3), gh_job(4, **ACCOUNTANT)])
    assert enqueue_new(x.conn, CONFIG, x.clock()) == 4
    assert sorted(x.jobs(), key=lambda r: r[1]) == [
        ("3", 1, "new_content", "pending"), ("1", 2, "backlog", "pending"),
        ("4", 3, "new_content", "pending"), ("2", 4, "backlog", "pending")]  # fmt: skip
    model = ScriptedModel(reply())
    x.run(model, limit=1)
    assert "Title: Data Engineer" in model.calls[0]  # posting 3 went first
    assert enqueue_new(x.conn, CONFIG, x.clock()) == 0  # nothing queued twice


def test_a_result_is_stored_with_versions_evidence_and_usage(x):
    x.collect(T0, [gh_job(1)])
    report = x.run(ScriptedModel(reply()))
    assert (report.requests, report.succeeded, report.stop_reason) == (1, 1, "queue empty")
    row = x.conn.execute(
        "SELECT r.model, r.prompt_version, r.schema_version, r.normalization_version, "
        "r.validation_status, r.version_id = p.current_version_id, a.total_tokens "
        "FROM extraction_results r JOIN source_postings p USING (posting_id) "
        "JOIN extraction_attempts a USING (attempt_id)"
    ).fetchone()
    assert row[0] == "gemini-3.5-flash-lite" and row[4] == "valid" and row[5] is True
    assert row[6] == 1800
    found = x.scalar(
        "SELECT count(*) FROM extraction_results, jsonb_array_elements(evidence) e "
        "WHERE (e ->> 'found')::boolean"
    )
    assert found >= 4
    assert x.scalar("SELECT requests FROM extraction_usage_daily") == 1


def test_unchanged_scans_send_no_new_requests_and_edits_do(x):
    x.collect(T0, [gh_job(1)])
    model = ScriptedModel(reply())
    x.run(model)
    x.collect(T0 + HOUR, [gh_job(1)])  # unchanged content
    x.run(model)
    assert len(model.calls) == 1
    x.collect(T0 + 2 * HOUR, [gh_job(1, content="<p>Own the Snowflake models.</p>")])
    posting = x.scalar("SELECT posting_id FROM source_postings")
    # The stored result belongs to the old version and is never shown for the new one.
    assert listing_detail(x.conn, posting).extraction.status == "not_queued"
    x.run(model)
    assert len(model.calls) == 2
    assert "Own the Snowflake models." in model.calls[1]
    assert listing_detail(x.conn, posting).extraction.status == "extracted"
    assert x.scalar("SELECT count(*) FROM extraction_results") == 2  # history kept


def test_identical_input_reuses_the_earlier_answer(x):
    x.collect(T0, [gh_job(1)])
    model = ScriptedModel(reply())
    x.run(model)
    x.collect(T0 + HOUR, [gh_job(1, content="<p>Different.</p>")])
    x.run(model)
    x.collect(T0 + 2 * HOUR, [gh_job(1)])  # back to the first wording (A, B, A)
    report = x.run(model)
    assert (len(model.calls), report.reused, report.requests) == (2, 1, 0)
    assert x.scalar("SELECT count(*) FROM extraction_results WHERE reused_from_result_id "
                    "IS NOT NULL") == 1  # fmt: skip


def test_invalid_answers_are_retried_then_the_job_fails_and_gives_up_after_five_runs(x):
    x.collect(T0, [gh_job(1)])
    broken = ScriptedModel(reply(text='{"role_categories": "oops"}'))
    report = x.run(broken)
    assert (report.requests, report.failed, report.succeeded) == (4, 1, 0)
    assert x.slept[-3:] == [10.0, 30.0, 90.0]
    assert x.scalar("SELECT status FROM extraction_jobs") == "failed"
    assert x.scalar("SELECT count(*) FROM extraction_results") == 0  # nothing stored
    raw = x.scalar("SELECT raw_reply FROM extraction_attempts LIMIT 1")
    assert raw == '{"role_categories": "oops"}'  # kept for diagnosis
    for _ in range(4):
        x.run(broken)
    assert x.scalar("SELECT status, runs_failed FROM extraction_jobs") == "gave_up"
    assert x.run(broken).requests == 0  # gave up: not retried automatically
    result = reprocess(x.conn, CONFIG, x.clock(), "prompt fixed")
    assert result.reset == 1
    assert x.run(ScriptedModel(reply())).succeeded == 1


def test_not_json_is_an_invalid_answer(x):
    x.collect(T0, [gh_job(1)])
    report = x.run(ScriptedModel(reply(text="Sure! Here is the JSON:"), reply()))
    assert (report.requests, report.succeeded) == (2, 1)
    outcomes = [r[0] for r in x.conn.execute(
        "SELECT outcome FROM extraction_attempts ORDER BY attempt_id")]  # fmt: skip
    assert outcomes == ["invalid_output", "success"]


def test_rate_limits_wait_as_google_says(x):
    x.collect(T0, [gh_job(1)])
    report = x.run(ScriptedModel(quota_error(per_day=False, delay="45s"), reply()))
    assert report.succeeded == 1 and 45.0 in x.slept


def test_daily_quota_pauses_until_pacific_midnight_without_failing_the_job(x):
    x.collect(T0, [job(1), job(2)])
    report = x.run(ScriptedModel(quota_error(per_day=True)))
    assert report.stop_reason.startswith("daily quota exhausted")
    until = x.scalar("SELECT paused_until FROM extraction_pauses")
    assert until == datetime(2026, 10, 28, 7, 0, tzinfo=UTC)  # midnight PDT
    assert {r[3] for r in x.jobs()} == {"pending"}
    assert x.run(ScriptedModel(reply())).requests == 0  # still paused
    x.clock.now = until + timedelta(minutes=1)
    assert x.run(ScriptedModel(reply())).succeeded == 2


def test_server_errors_are_retried_with_waits(x):
    x.collect(T0, [gh_job(1)])
    errors = [ModelError("server_error", "503", 503), ModelError("network_error", "timeout")]
    report = x.run(ScriptedModel(*errors, reply()))
    assert (report.requests, report.succeeded) == (3, 1)
    assert x.slept[-2:] == [10.0, 30.0]


def test_a_rejected_request_stops_the_run(x):
    x.collect(T0, [gh_job(1), gh_job(2)])
    report = x.run(ScriptedModel(ModelError("client_error", "API key not valid", 400)))
    assert report.requests == 1 and report.stop_reason.startswith("request rejected (400)")


def test_the_safety_stop_leaves_ten_percent_of_the_daily_limit(x):
    x.collect(T0, [job(n) for n in range(1, 6)])
    report = x.run(ScriptedModel(reply()), config=ExtractionConfig(rpm=60, daily_limit=4))
    assert report.succeeded == 3  # floor(0.9 * 4)
    assert report.stop_reason.startswith("safety stop")


def test_requests_are_spaced_to_the_per_minute_limit(x):
    x.collect(T0, [job(1), job(2)])
    x.run(ScriptedModel(reply()), config=ExtractionConfig(rpm=4, daily_limit=100))
    assert any(14.0 < s <= 15.0 for s in x.slept)  # 60 s / 4 requests per minute


def test_limits_must_be_configured_before_any_request(x):
    with pytest.raises(extraction.ExtractionNotConfigured, match="JOBMARKET_AI_RPM"):
        run_extraction(x.conn, ScriptedModel(), ExtractionConfig(), x.clock, x.sleep)


def test_an_interrupted_run_is_recovered(x):
    x.collect(T0, [gh_job(1)])
    enqueue_new(x.conn, CONFIG, x.clock())
    x.conn.execute("UPDATE extraction_jobs SET status = 'running'")  # as if the process died
    assert x.run(ScriptedModel(reply())).succeeded == 1


def test_queued_content_that_was_replaced_is_superseded(x):
    x.collect(T0, [gh_job(1)])
    enqueue_new(x.conn, CONFIG, x.clock())
    x.collect(T0 + HOUR, [gh_job(1, content="<p>Rewritten.</p>")])
    model = ScriptedModel(reply())
    report = x.run(model)
    assert (report.superseded, report.succeeded, len(model.calls)) == (1, 1, 1)
    assert "Rewritten." in model.calls[0]


def test_long_and_empty_descriptions_are_skipped_not_sent(x):
    x.collect(T0, [gh_job(1, content=f"<p>{'data ' * 5000}</p>"), gh_job(2, content="")])
    model = ScriptedModel(reply())
    x.run(model)
    assert model.calls == []
    skips = dict(x.conn.execute(
        "SELECT p.source_posting_id, j.skip_reason FROM extraction_jobs j "
        "JOIN source_postings p USING (posting_id)"))  # fmt: skip
    assert skips == {"1": "too_long", "2": "no_description"}


def test_a_configuration_change_never_reruns_the_backlog_by_itself(x):
    x.collect(T0, [gh_job(1)])
    x.run(ScriptedModel(reply()))
    newer = ExtractionConfig(model="gemini-3.8-flash", thinking_level="low", rpm=60,
                             daily_limit=100)  # fmt: skip
    model = ScriptedModel(reply())
    assert x.run(model, config=newer).requests == 0  # already extracted under another config
    queued = reprocess(x.conn, newer, x.clock(), "compare models", limit=1)
    assert queued.queued == 1
    assert x.run(model, config=newer).succeeded == 1
    keys = {r[0] for r in x.conn.execute("SELECT config_key FROM extraction_results")}
    assert len(keys) == 2  # both results kept


def test_storing_a_result_twice_keeps_one_row(x):
    x.collect(T0, [gh_job(1)])
    x.run(ScriptedModel(reply()))
    job_id, version, posting, input_hash = x.conn.execute(
        "SELECT job_id, version_id, posting_id, input_hash FROM extraction_jobs"
    ).fetchone()
    from jobmarket.extraction_check import check_answer

    document = x.scalar("SELECT document FROM extraction_inputs")
    first = x.scalar("SELECT result_id FROM extraction_results")
    checked = check_answer(answer(), document)
    extraction._store_result(
        x.conn, job_id, version, posting, input_hash, CONFIG, checked, answer(), x.clock(),
        reused_from=first,
    )  # fmt: skip
    assert x.scalar("SELECT count(*) FROM extraction_results") == 1


def test_renormalize_needs_no_model_calls(x):
    x.collect(T0, [gh_job(1)])
    x.run(ScriptedModel(reply()))
    x.conn.execute("UPDATE extraction_results SET normalization_version = 'old'")
    assert extraction.renormalize(x.conn, x.clock()) == 1


def test_disagreements_with_the_rules_are_listed(x):
    x.collect(T0, [gh_job(1), gh_job(2, **ACCOUNTANT)])
    data = answer()
    model = ScriptedModel(reply(data), reply(data))
    x.run(model, limit=1)  # Data Engineer: both keep
    model.items = [reply(data)]  # the AI (wrongly) calls the accountant role data engineering
    x.run(model)
    rows = x.conn.execute(
        "SELECT title, rules_retained, ai_would_retain FROM screening_disagreements"
    ).fetchall()
    assert rows == [("Staff Accountant", False, True)]


def test_collection_continues_when_extraction_settings_are_broken(env, monkeypatch):
    monkeypatch.setenv(extraction.RPM_ENV, "lots")
    env.transport.queue("acme", ok([gh_job(1)]))
    env.transport.queue("beta", ok([]))
    report = env.collector().collect()
    assert report.status == "complete"
    assert "must be a number" in report.extraction_queue_error


def test_collection_queues_new_content_without_calling_the_model(env):
    env.transport.queue("acme", ok([gh_job(1)]))
    env.transport.queue("beta", ok([]))
    report = env.collector().collect()
    assert report.extraction_queued == 1
    assert env.scalar("SELECT count(*) FROM extraction_attempts") == 0


@pytest.fixture
def cli_x(x, monkeypatch, isolated_schema):
    real_connect = db.connect
    monkeypatch.setattr(
        db, "connect",
        lambda s, autocommit=False, options=None: real_connect(
            s, autocommit=autocommit, options=f"-c search_path={isolated_schema}"),
    )  # fmt: skip
    return x


def test_cli_status_show_and_run_without_limits(cli_x, monkeypatch, capsys):
    cli_x.collect(T0, [gh_job(1)])
    cli_x.run(ScriptedModel(reply()))
    posting = cli_x.scalar("SELECT posting_id FROM source_postings")
    # Set to empty rather than removed: the CLI loads .env, which never overrides a variable
    # that exists, so a developer's real limits and key are not picked up.
    for name in (extraction.RPM_ENV, extraction.DAILY_ENV, extraction.TPM_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    assert cli.main(["extract", "status"]) == cli.EXIT_OK
    assert cli.main(["extract", "show", str(posting), "--evidence"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Current results: 1 valid" in out and "Sponsorship not stated" in out
    assert "[found]" in out
    assert cli.main(["extract", "run"]) == cli.EXIT_CONFIG_ERROR
    assert "JOBMARKET_AI_RPM" in capsys.readouterr().err


def test_the_job_list_shows_fresher_friendly_jobs_by_default(cli_x, capsys):
    cli_x.collect(T0, [job(1, title="Data Engineer"), job(2, title="Data Analyst"),
                       job(3, title="Data Engineer Intern")])  # fmt: skip
    levels = {"yes": ["new_grad"], "internship": ["internship"], "no": ["senior_level"]}
    replies = [reply(answer(early_career={"status": s, "basis": b, "evidence": []}))
               for s, b in levels.items()]  # fmt: skip
    model = ScriptedModel(*replies)
    cli_x.run(model, limit=1)
    model.items = [replies[1]]
    cli_x.run(model, limit=1)
    model.items = [replies[2]]
    cli_x.run(model, limit=1)
    by_title = dict(cli_x.conn.execute(
        "SELECT v.title, c.derived ->> 'early_career' FROM current_extractions c "
        "JOIN posting_versions v USING (version_id)"))  # fmt: skip
    fresher = next(t for t, s in by_title.items() if s == "yes")
    assert cli.main(["jobs"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert fresher in out and "Showing 1 fresher-friendly job(s)" in out
    assert cli.main(["jobs", "--internships"]) == cli.EXIT_OK
    assert "Showing 1 internship job(s)" in capsys.readouterr().out
    assert cli.main(["jobs", "--all-levels"]) == cli.EXIT_OK
    assert "Showing 3 job(s)" in capsys.readouterr().out
