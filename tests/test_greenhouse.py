import json

import httpx
from gh_fakes import T0, FakeClock, gh_body, gh_job

from jobmarket.greenhouse import fetch_jobs, jobs_url, parse_jobs_response

JSON = {"content-type": "application/json; charset=utf-8"}


def parse(body: bytes, status: int = 200, headers: dict | None = None):
    return parse_jobs_response(status, JSON if headers is None else headers, body)


def test_request_asks_for_full_content():
    assert jobs_url("acme") == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


def test_complete_response():
    parsed = parse(gh_body([gh_job(1), gh_job(2)]))
    assert parsed.outcome == "complete"
    assert [j.source_id for j in parsed.jobs] == [1, 2]
    assert parsed.reported_total == parsed.received == 2
    job = parsed.jobs[0]
    assert job.absolute_url == "https://job-boards.greenhouse.io/acme/jobs/1"
    assert job.content_html.startswith("<p>Build")  # HTML-escaping undone once
    assert "data pipelines" in job.description_text and "<p>" not in job.description_text
    assert job.first_published.isoformat() == "2026-10-19T09:00:00-05:00"


def test_valid_empty_board_is_complete():
    parsed = parse(gh_body([]))
    assert parsed.outcome == "complete"
    assert parsed.jobs == [] and parsed.received == 0 and parsed.reported_total == 0


def test_unreadable_response_is_failed_not_empty():
    assert parse(b"<html>maintenance</html>").outcome == "failed"
    assert parse(b'{"jobs": [').outcome == "failed"
    assert parse(b'{"meta": {"total": 0}}').error == "response has no jobs list"


def test_http_200_alone_is_not_enough():
    assert parse(gh_body([gh_job(1)]), headers={"content-type": "text/html"}).outcome == "failed"
    assert parse(gh_body([gh_job(1)]), status=500).error == "HTTP 500"


def test_invalid_records_make_a_partial_result():
    bad_url = gh_job(2)
    bad_url["absolute_url"] = "http://insecure.example/2"
    no_title = gh_job(3)
    no_title["title"] = "  "
    parsed = parse(gh_body([gh_job(1), bad_url, no_title, gh_job(1)]))
    assert parsed.outcome == "partial"
    assert [j.source_id for j in parsed.jobs] == [1]
    assert [r.reason for r in parsed.rejected] == [
        "missing or non-https absolute_url",
        "missing title",
        "duplicate id in response",
    ]


def test_count_mismatch_is_partial():
    parsed = parse(gh_body([gh_job(1)], total=5))
    assert parsed.outcome == "partial"
    assert "meta.total is 5" in parsed.error


def test_all_records_invalid_is_failed():
    broken = gh_job(1)
    del broken["updated_at"]
    assert parse(gh_body([broken])).outcome == "failed"


def test_content_hash_ignores_source_timestamps_but_not_content():
    a = parse(gh_body([gh_job(1)])).jobs[0]
    b = parse(gh_body([gh_job(1, updated_at="2026-10-25T08:00:00-05:00")])).jobs[0]
    c = parse(gh_body([gh_job(1, title="Senior Data Engineer")])).jobs[0]
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != c.content_hash()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_timeout_is_returned_not_raised():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result = fetch_jobs(_client(handler), "acme", FakeClock())
    assert result.status_code is None and result.error == "request timed out"


def test_short_retry_after_is_retried_once():
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, headers=JSON, content=gh_body([]))

    result = fetch_jobs(_client(handler), "acme", FakeClock(), sleep=slept.append)
    assert result.status_code == 200 and slept == [2.0] and len(calls) == 2


def test_long_retry_after_is_not_waited_for():
    slept = []

    def handler(request):
        return httpx.Response(429, headers={"retry-after": "900"}, content=b"slow down")

    result = fetch_jobs(_client(handler), "acme", FakeClock(), sleep=slept.append)
    assert result.status_code == 429 and result.retry_after_seconds == 900 and slept == []
    assert parse_jobs_response(429, result.headers, result.body).outcome == "failed"


def test_fetched_at_comes_from_the_clock():
    def handler(request):
        return httpx.Response(200, headers=JSON, content=json.dumps({"jobs": []}).encode())

    result = fetch_jobs(_client(handler), "acme", FakeClock())
    assert result.fetched_at > T0
