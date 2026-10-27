"""Lever connector: field parsing, pagination, completeness, and polite fetching."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from gh_fakes import FakeClock
from lever_fakes import LEVER_CREATED_MS, lever_id, lever_job, lever_page

from jobmarket import lever
from jobmarket.connectors import InlineRetryBudget, Pacer, SavedPage, saved_pages
from jobmarket.normalize import compare_form

JSON = {"content-type": "application/json"}


def page(n: int, jobs, status: int = 200, headers=None, limit: int = 100) -> SavedPage:
    body = jobs if isinstance(jobs, bytes) else json.dumps(jobs).encode()
    url = lever.postings_url("lev", "global", (n - 1) * limit, limit)
    return SavedPage(n, url, status, JSON if headers is None else headers, body)


def jobs(start: int, count: int) -> list[dict]:
    return [lever_job(i) for i in range(start, start + count)]


# --- fields ----------------------------------------------------------------------------


def test_posting_fields_are_kept_and_mapped():
    raw = lever_job(
        1,
        location="Dallas, TX",
        all_locations=["Dallas, TX", "Houston, TX"],
        workplace="hybrid",
        salary={"currency": "USD", "interval": "per-year-salary", "min": 90000, "max": 120000},
    )
    parsed = lever.parse_pages([page(1, [raw])])
    assert parsed.outcome == "complete" and parsed.pages == 1
    (job,) = parsed.jobs
    assert job.source_id == lever_id(1)
    assert job.absolute_url == raw["hostedUrl"] and job.apply_url == raw["applyUrl"]
    assert job.created_at == datetime.fromtimestamp(LEVER_CREATED_MS / 1000, tz=UTC)
    # Lever has no publication or update time: nothing is invented from createdAt.
    assert job.first_published is None and job.updated_at is None
    assert job.offices == [
        {"location": "Dallas, TX"},
        {"location": "Houston, TX"},
        {"name": "Workplace type: hybrid"},
    ]
    assert "<h3>Requirements</h3><ul><li>3 years of SQL</li>" in job.content_html
    assert "equal opportunity employer" in job.description_text
    assert {"name": "Employment type (commitment)", "value": "Full-time"} in job.metadata
    assert job.source_fields["categories"]["allLocations"] == ["Dallas, TX", "Houston, TX"]
    form = compare_form(job.source_content()).normalized
    assert form["locations"] == ["Dallas, TX", "Houston, TX", "Workplace type: hybrid"]
    assert form["employment_type"] == ["full_time"]
    assert "salary range" in " ".join(form["compensation"]).lower()


def test_fingerprint_ignores_created_at_but_not_content():
    a = lever.parse_pages([page(1, [lever_job(1)])]).jobs[0]
    b = lever.parse_pages([page(1, [lever_job(1, created_at=LEVER_CREATED_MS + 5)])]).jobs[0]
    c = lever.parse_pages([page(1, [lever_job(1, workplace="remote")])]).jobs[0]
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != c.content_hash()


def test_cosmetic_and_meaningful_lever_differences():
    def form(**kw):
        job = lever.parse_pages([page(1, [lever_job(1, **kw)])]).jobs[0]
        return compare_form(job.source_content()).fingerprint

    base = form()
    restyled = form(description="<div>\n  <p>Build and maintain data pipelines with SQL, "
                    "Python, and Airflow. Model data in Snowflake and publish dashboards in "
                    "Tableau.</p></div>")  # fmt: skip
    assert restyled == base
    assert form(workplace="remote") != base  # a remote restriction changed
    assert form(commitment="Contract") != base  # employment type changed
    assert form(all_locations=["Dallas, TX", "Austin, TX"]) != base


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda j: j.pop("id"), "missing or invalid id"),
        (lambda j: j.update(id="a/b"), "missing or invalid id"),
        (lambda j: j.update(text="  "), "missing title (text)"),
        (lambda j: j.update(hostedUrl="http://jobs.lever.co/x"), "missing or non-https hostedUrl"),
        (lambda j: j.update(applyUrl="ftp://x"), "non-https applyUrl"),
        (lambda j: j.pop("description"), "missing description"),
        (lambda j: j["categories"].update(allLocations="Dallas"), "not a list of strings"),
        (lambda j: j.update(lists=[{"text": "x"}]), "lists is not a list"),
        (lambda j: j.update(createdAt="yesterday"), "unparseable createdAt"),
    ],
)
def test_invalid_records_are_rejected_with_a_reason(change, reason):
    bad = lever_job(2)
    change(bad)
    parsed = lever.parse_pages([page(1, [lever_job(1), bad])])
    assert parsed.outcome == "partial"
    assert len(parsed.jobs) == 1
    (rejection,) = parsed.rejected
    assert reason in rejection.reason and rejection.page_no == 1 and rejection.record_index == 1


def test_all_records_invalid_is_a_failed_scan():
    bad = lever_job(1)
    bad.pop("hostedUrl")
    parsed = lever.parse_pages([page(1, [bad])])
    assert parsed.outcome == "failed" and "no valid records" in parsed.error


# --- completeness ----------------------------------------------------------------------


def test_short_final_page_completes_a_multi_page_scan():
    parsed = lever.parse_pages([page(1, jobs(1, 100)), page(2, jobs(101, 30))])
    assert parsed.outcome == "complete"
    assert (len(parsed.jobs), parsed.received, parsed.pages) == (130, 130, 2)
    assert parsed.reported_total is None  # Lever reports no total
    assert {j.page_no for j in parsed.jobs} == {1, 2}


def test_exactly_full_board_ends_with_an_empty_page():
    parsed = lever.parse_pages([page(1, jobs(1, 100)), page(2, [])])
    assert parsed.outcome == "complete" and len(parsed.jobs) == 100


def test_empty_board_is_complete():
    parsed = lever.parse_pages([page(1, [])])
    assert parsed.outcome == "complete" and parsed.jobs == []


def test_first_full_page_alone_is_never_a_complete_scan():
    parsed = lever.parse_pages([page(1, jobs(1, 100))])
    assert parsed.outcome == "partial"
    assert "without reaching a short final page" in parsed.error
    assert len(parsed.jobs) == 100  # usable records are kept


def test_failed_later_page_gives_a_partial_scan_with_earlier_records():
    failed = page(2, b"unavailable", status=503, headers={"content-type": "text/plain"})
    parsed = lever.parse_pages([page(1, jobs(1, 100)), failed])
    assert parsed.outcome == "partial" and "page 2 unusable (HTTP 503)" in parsed.error
    assert len(parsed.jobs) == 100


def test_missing_later_page_gives_a_partial_scan():
    missing = SavedPage(2, page(2, []).request_url, None, {}, None, "request timed out")
    parsed = lever.parse_pages([page(1, jobs(1, 100)), missing])
    assert parsed.outcome == "partial" and "request timed out" in parsed.error


def test_repeated_page_is_a_pagination_anomaly():
    first = jobs(1, 100)
    parsed = lever.parse_pages([page(1, first), page(2, first)])
    assert parsed.outcome == "partial" and "repeated postings" in parsed.error
    assert len(parsed.jobs) == 100 and parsed.rejected == []


def test_duplicate_posting_across_pages_is_rejected():
    parsed = lever.parse_pages([page(1, jobs(1, 100)), page(2, [lever_job(5), lever_job(200)])])
    assert parsed.outcome == "partial"
    assert len(parsed.jobs) == 101
    assert [(r.reason, r.page_no) for r in parsed.rejected] == [("duplicate id in response", 2)]


def test_page_larger_than_the_limit_is_an_anomaly():
    parsed = lever.parse_pages([page(1, jobs(1, 12), limit=10)])
    assert parsed.outcome == "partial" and "returned 12 postings for limit 10" in parsed.error


@pytest.mark.parametrize(
    ("first", "error"),
    [
        (page(1, b'{"ok":false,"error":"Document not found"}', status=404), "board not found"),
        (page(1, b"[oops"), "not valid JSON"),
        (page(1, b'{"postings": []}'), "not a list of postings"),
        (page(1, b"<html></html>", headers={"content-type": "text/html"}), "content type"),
        (SavedPage(1, "u", None, {}, None, "request failed: refused"), "request failed"),
    ],
)
def test_unusable_first_page_fails_the_scan(first, error):
    parsed = lever.parse_pages([first])
    assert parsed.outcome == "failed" and error in parsed.error


# --- fetching --------------------------------------------------------------------------


class Board:
    """A fake Lever board answering skip/limit requests, with optional faults."""

    def __init__(self, count: int, faults: dict | None = None, ignore_skip: bool = False):
        self.all = jobs(1, count)
        self.faults = faults or {}
        self.ignore_skip = ignore_skip
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        n = len(self.requests)
        fault = self.faults.get(n)
        if isinstance(fault, Exception):
            raise fault
        if fault is not None:
            return fault
        skip = 0 if self.ignore_skip else int(request.url.params["skip"])
        limit = int(request.url.params["limit"])
        return lever_page(self.all[skip : skip + limit])


def fetch(board: Board, slept=None, **kw):
    client = httpx.Client(transport=httpx.MockTransport(board))
    sleep = slept.append if slept is not None else lambda s: None
    return lever.LEVER.fetch(client, "lev", "global", FakeClock(), Pacer(0, sleep), sleep, **kw)


def test_fetch_pages_until_a_short_page():
    board = Board(230)
    result = fetch(board)
    assert [r.url.params["skip"] for r in board.requests] == ["0", "100", "200"]
    assert {r.url.params["mode"] for r in board.requests} == {"json"}
    assert board.requests[0].url.host == "api.lever.co"
    assert lever.LEVER.parse(saved_pages(result)).outcome == "complete"
    assert result.collected_at == result.pages[0].fetched_at


def test_eu_boards_use_the_eu_api_host():
    board = Board(1)
    client = httpx.Client(transport=httpx.MockTransport(board))
    lever.LEVER.fetch(client, "lev", "eu", FakeClock(), Pacer(0), lambda s: None)
    assert board.requests[0].url.host == "api.eu.lever.co"


def test_fetch_stops_at_a_timeout_and_keeps_earlier_pages():
    board = Board(250, faults={2: httpx.ReadTimeout("slow")})
    result = fetch(board)
    assert len(result.pages) == 2 and result.pages[1].error == "request timed out"
    parsed = lever.LEVER.parse(saved_pages(result))
    assert parsed.outcome == "partial" and len(parsed.jobs) == 100


def test_fetch_stops_when_the_source_ignores_skip():
    board = Board(250, ignore_skip=True)
    result = fetch(board)
    assert len(board.requests) == 2  # the repeat is detected on the second page
    assert "repeated postings" in lever.LEVER.parse(saved_pages(result)).error


def test_fetch_page_cap_is_a_partial_scan():
    board = Board(1000)
    result = fetch(board, max_pages=3)
    assert len(board.requests) == 3
    parsed = lever.LEVER.parse(saved_pages(result))
    assert parsed.outcome == "partial" and len(parsed.jobs) == 300


def test_short_retry_after_is_waited_out_once_per_scan():
    limited = httpx.Response(429, headers={"retry-after": "2"}, content=b"slow down")
    board = Board(250, faults={1: limited, 3: limited})
    slept: list[float] = []
    result = fetch(board, slept)
    # The first 429 is waited out; the second one ends the scan instead of retrying again.
    assert slept == [2.0]
    assert [p.status_code for p in result.pages] == [200, 429]
    assert result.retry_after_seconds == 2.0
    parsed = lever.LEVER.parse(saved_pages(result))
    assert parsed.outcome == "partial" and "HTTP 429" in parsed.error


def test_long_retry_after_is_not_waited_for():
    board = Board(5, faults={1: httpx.Response(429, headers={"retry-after": "900"})})
    slept: list[float] = []
    result = fetch(board, slept)
    assert slept == [] and result.retry_after_seconds == 900
    assert lever.LEVER.parse(saved_pages(result)).outcome == "failed"


def test_pacer_spaces_request_starts():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    pacer = Pacer(1.0, sleep, monotonic=lambda: now[0])
    pacer.wait()  # first request: no wait
    now[0] += 0.25
    pacer.wait()  # 0.25 s later: waits the remaining 0.75 s
    now[0] += 5
    pacer.wait()  # long after: no wait
    assert slept == [0.75]


def test_inline_retry_budget_allows_one_short_wait():
    budget = InlineRetryBudget()
    assert not budget.take(None) and not budget.take(31)
    assert budget.take(2) and not budget.take(2)
