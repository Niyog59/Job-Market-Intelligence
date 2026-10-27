"""Evidence checks: official mapping pages and sampled job/application pages."""

import httpx
import pytest
from gh_fakes import FakeClock, ScriptedTransport, board, gh_job, ok, registry
from lever_fakes import lever_id, lever_job, lever_page

from jobmarket.collector import Collector
from jobmarket.connectors import Pacer
from jobmarket.evidence import (
    CheckTarget,
    check_mapping,
    check_mappings,
    check_pages,
    mapping_targets,
    page_targets,
)
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

PACER = Pacer(0)


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def page(html: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/html"}, text=html)


def target(source: str, token: str) -> CheckTarget:
    return CheckTarget(1, source, token, "Acme", f"https://{token}.example/careers")


@pytest.mark.parametrize(
    ("source", "token", "html", "outcome"),
    [
        ("lever", "lev", '<a href="https://jobs.lever.co/lev">Jobs</a>', "found"),
        ("lever", "lev", "fetch('https://api.lever.co/v0/postings/lev?mode=json')", "found"),
        ("lever", "lev", "window.leverJobsOptions = {accountName: 'lev'};", "found"),
        ("lever", "lev", '<a href="https://jobs.eu.lever.co/lev">Jobs</a>', "found"),
        # A longer token that starts the same way is a different board.
        ("lever", "lev", '<a href="https://jobs.lever.co/lev-2">Jobs</a>', "not_found"),
        ("lever", "lev", '<a href="https://jobs.lever.co/levitate">Jobs</a>', "not_found"),
        ("greenhouse", "acme", '<a href="https://job-boards.greenhouse.io/acme">', "found"),
        (
            "greenhouse",
            "acme",
            '<script src="https://boards.greenhouse.io/embed/job_board?for=acme">',
            "found",
        ),  # fmt: skip
        ("greenhouse", "acme", "<p>We are hiring!</p>", "not_found"),
    ],
)
def test_mapping_reference_must_name_the_exact_board(source, token, html, outcome):
    result = check_mapping(client(lambda r: page(html)), target(source, token), PACER)
    assert result.outcome == outcome


def test_case_only_reference_is_reported_not_accepted():
    html = "window.leverJobsOptions = {accountName: 'Zimperium'};"
    result = check_mapping(client(lambda r: page(html)), target("lever", "zimperium"), PACER)
    assert result.outcome == "not_found" and "differently cased" in result.detail


def test_refused_and_failed_pages_are_recorded_as_such():
    refused = check_mapping(client(lambda r: page("no", 403)), target("lever", "lev"), PACER)
    assert refused.outcome == "refused" and "not worked around" in refused.detail

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    failed = check_mapping(client(timeout), target("lever", "lev"), PACER)
    assert failed.outcome == "fetch_failed" and failed.detail == "request timed out"


@pytest.fixture
def collected(schema_conn, tmp_path):
    """One Greenhouse and one Lever board, collected once."""
    apply_migrations(schema_conn)
    sync_registry(
        schema_conn,
        registry(board("acme"), board("lev", employer="Lev Co", source="lever")),
        FakeClock()(),
    )
    transport = ScriptedTransport()
    transport.queue("acme", ok([gh_job(1), gh_job(2)]))
    transport.queue("lev", lever_page([lever_job(1), lever_job(2), lever_job(3)]))
    Collector(
        schema_conn, httpx.Client(transport=transport), tmp_path, clock=FakeClock(),
        sleep=lambda s: None, interval=0,
    ).collect()  # fmt: skip
    return schema_conn


@pytest.mark.db
def test_mapping_checks_are_stored_and_change_nothing_else(collected):
    targets = mapping_targets(collected)
    assert [(t.source_system, t.token) for t in targets] == [
        ("greenhouse", "acme"),
        ("lever", "lev"),
    ]
    html = {"acme.example": '<a href="https://job-boards.greenhouse.io/acme">', "lev.example": ""}
    results = check_mappings(
        collected, client(lambda r: page(html[r.url.host])), targets, FakeClock(), PACER
    )
    assert [r.outcome for r in results] == ["found", "not_found"]
    rows = collected.execute(
        "SELECT outcome, matched FROM board_mapping_checks ORDER BY mapping_check_id"
    ).fetchall()
    assert rows == [("found", "job-boards.greenhouse.io/acme"), ("not_found", None)]
    assert collected.execute(
        "SELECT count(*) FROM source_boards WHERE verification_status = 'verified'"
    ).fetchone()[0] == 2  # fmt: skip


@pytest.mark.db
def test_page_checks_sample_retained_postings_with_get_only(collected):
    targets = page_targets(collected, per_board=2)
    kinds = [(t.source_system, t.url_kind) for t in targets]
    # Greenhouse gives one page per posting; Lever a posting page and an application page.
    assert kinds.count(("greenhouse", "posting")) == 2
    assert kinds.count(("lever", "posting")) == 2 and kinds.count(("lever", "application")) == 2

    seen: list[httpx.Request] = []
    answers = {
        lever_id(1): page("<h1>Data Engineer</h1>"),
        lever_id(2): page("gone", 404),
        lever_id(3): page("slow down", 429),
    }

    def handler(request):
        seen.append(request)
        if request.url.host == "jobs.lever.co":
            return answers[request.url.path.split("/")[2]]
        if request.url.path.endswith("/1"):
            return httpx.Response(
                302, headers={"location": "https://job-boards.greenhouse.io/acme"}
            )
        if request.url.path == "/acme":
            return page("<h1>All jobs</h1>")
        raise httpx.ConnectError("no route", request=request)

    results = check_pages(collected, client(handler), targets, FakeClock(), PACER)

    assert {r.method for r in seen} == {"GET"}  # nothing is ever submitted
    outcomes = {(r.target.source_system, r.target.source_posting_id, r.target.url_kind): r.outcome
                for r in results}  # fmt: skip
    assert outcomes[("greenhouse", "1", "posting")] == "redirected"
    assert outcomes[("greenhouse", "2", "posting")] == "unreachable"
    lever_outcomes = {k[1]: v for k, v in outcomes.items() if k[0] == "lever"}
    assert set(lever_outcomes.values()) <= {"available", "not_found", "rate_limited"}
    stored = collected.execute("SELECT count(*), count(DISTINCT outcome) FROM posting_page_checks")
    assert stored.fetchone()[0] == len(targets)
    # Checks are evidence only: no posting history or screening changed.
    assert collected.execute(
        "SELECT count(*) FROM posting_observations"
    ).fetchone()[0] == 5  # fmt: skip
