"""Evidence checks kept apart from collection: official mappings and job/application pages.

Mapping check: fetch a board's official careers page (the registry's official_source) and
look for a reference to that exact board: a link to its job board or postings API, or an
embed configuration naming it. Many careers pages load jobs with scripts, so "not found"
does not disprove a mapping, and a site that refuses automated requests is recorded as
refused rather than worked around. The registry stays the decision record: checks never
change a board's verification status.

Page check: for a sample of currently retained postings, GET the posting page and, if the
source gives one, the application page. Applications are never submitted. If the posting's
own page explicitly says applications are closed, the posting is marked closed with that
text as evidence. A page that
loads shows only that it was published at check time; it does not prove funded headcount
or active interviewing. Refusals, rate limits, server errors, and timeouts are access or
collection problems, never evidence that a job closed.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
import psycopg

from jobmarket.availability import close_posting
from jobmarket.connectors import REQUEST_TIMEOUT_SECONDS, USER_AGENT, Pacer
from jobmarket.normalize import visible_text

MAX_REDIRECTS = 5


def make_page_client() -> httpx.Client:
    """A client for human-facing pages: follows a few redirects, asks for HTML."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _after(token: str) -> str:
    # The token must end there: "lev" must not match inside "lever" or "lev-2".
    return re.escape(token) + r"(?![A-Za-z0-9_-])"


def mapping_patterns(source_system: str, token: str) -> list[re.Pattern]:
    t = _after(token)
    if source_system == "lever":
        return [
            re.compile(r"jobs(?:\.eu)?\.lever\.co/" + t),
            re.compile(r"api(?:\.eu)?\.lever\.co/v0/postings/" + t),
            re.compile(r"accountName['\"]?\s*[:=]\s*['\"]" + t + "['\"]"),
        ]
    return [
        re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?" + t),
        re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + t),
        re.compile(r"[?&]for=" + t),
    ]


@dataclass(frozen=True)
class CheckTarget:
    board_id: int
    source_system: str
    token: str
    employer: str
    url: str


@dataclass(frozen=True)
class MappingResult:
    target: CheckTarget
    outcome: str
    http_status: int | None
    final_url: str | None
    matched: str | None
    detail: str | None


def _request(client: httpx.Client, url: str, pacer: Pacer) -> tuple[httpx.Response | None, str]:
    pacer.wait()
    try:
        return client.get(url), ""
    except httpx.TimeoutException:
        return None, "request timed out"
    except httpx.TooManyRedirects:
        return None, f"more than {MAX_REDIRECTS} redirects"
    except httpx.HTTPError as exc:
        return None, f"request failed: {exc}"


def check_mapping(client: httpx.Client, target: CheckTarget, pacer: Pacer) -> MappingResult:
    response, error = _request(client, target.url, pacer)
    if response is None:
        return MappingResult(target, "fetch_failed", None, None, None, error)
    final = str(response.url)
    status = response.status_code
    if status in (401, 403, 429):
        return MappingResult(
            target, "refused", status, final, None,
            f"HTTP {status}: the site declined this automated request; not worked around",
        )  # fmt: skip
    if status != 200:
        return MappingResult(target, "fetch_failed", status, final, None, f"HTTP {status}")
    for pattern in mapping_patterns(target.source_system, target.token):
        if match := pattern.search(response.text):
            return MappingResult(target, "found", status, final, match.group(0), None)
    case_only = re.search(_after(target.token), response.text, re.IGNORECASE)
    detail = (
        f"only a differently cased reference ({case_only.group(0)!r}) was found"
        if case_only
        else "no reference to this board; the page may load its jobs with scripts"
    )
    return MappingResult(target, "not_found", status, final, None, detail)


def mapping_targets(
    conn: psycopg.Connection,
    include_disabled: bool = False,
    source: str | None = None,
    tokens: list[str] | None = None,
) -> list[CheckTarget]:
    rows = conn.execute(
        "SELECT b.board_id, b.source_system, b.board_key, e.name, b.official_source_url "
        "FROM source_boards b JOIN employers e USING (employer_id) "
        "WHERE b.official_source_url IS NOT NULL AND (b.is_active OR %s) "
        "AND (%s::text IS NULL OR b.source_system = %s) "
        "AND (%s::text[] IS NULL OR b.board_key = ANY(%s)) "
        "ORDER BY b.source_system, e.name, b.board_key",
        (include_disabled, source, source, tokens, tokens),
    ).fetchall()
    return [CheckTarget(*r) for r in rows]


def record_mapping(conn: psycopg.Connection, result: MappingResult, checked_at: datetime) -> None:
    conn.execute(
        "INSERT INTO board_mapping_checks (board_id, checked_at, url, http_status, final_url, "
        "outcome, matched, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            result.target.board_id,
            checked_at,
            result.target.url,
            result.http_status,
            result.final_url,
            result.outcome,
            result.matched,
            result.detail,
        ),
    )


def check_mappings(
    conn: psycopg.Connection,
    client: httpx.Client,
    targets: list[CheckTarget],
    clock: Callable[[], datetime],
    pacer: Pacer,
) -> list[MappingResult]:
    results = []
    for target in targets:
        result = check_mapping(client, target, pacer)
        record_mapping(conn, result, clock())
        results.append(result)
    return results


# --- Page checks -------------------------------------------------------------------------


@dataclass(frozen=True)
class PageTarget:
    posting_id: int
    version_id: int
    employer: str
    source_system: str
    token: str
    source_posting_id: str
    title: str
    url_kind: str
    url: str


@dataclass(frozen=True)
class PageResult:
    target: PageTarget
    outcome: str
    http_status: int | None
    final_url: str | None
    detail: str | None


def page_targets(
    conn: psycopg.Connection,
    per_board: int,
    source: str | None = None,
    tokens: list[str] | None = None,
) -> list[PageTarget]:
    """Up to per_board retained postings per board, most recently observed first, with
    their posting page and, when there is one, their application page."""
    rows = conn.execute(
        """
        SELECT posting_id, version_id, employer, source_system, token, source_posting_id,
               title, absolute_url, apply_url
        FROM (
            SELECT p.posting_id, p.current_version_id AS version_id, e.name AS employer,
                   b.source_system, b.board_key AS token, p.source_posting_id, v.title,
                   v.absolute_url, v.apply_url,
                   row_number() OVER (PARTITION BY b.board_id
                                      ORDER BY p.last_observed_at DESC, p.posting_id) AS n
            FROM source_postings p
            JOIN posting_screenings ps USING (posting_id)
            JOIN posting_versions v ON v.version_id = p.current_version_id
            JOIN source_boards b ON b.board_id = p.board_id
            JOIN employers e ON e.employer_id = b.employer_id
            WHERE ps.retained AND b.is_active
              AND p.availability_state IN ('observed', 'possibly_unavailable')
              AND (%s::text IS NULL OR b.source_system = %s)
              AND (%s::text[] IS NULL OR b.board_key = ANY(%s))
        ) ranked
        WHERE n <= %s
        ORDER BY source_system, employer, token, posting_id
        """,
        (source, source, tokens, tokens, per_board),
    ).fetchall()
    targets = []
    for pid, vid, employer, src, token, sid, title, url, apply_url in rows:
        for kind, page_url in (("posting", url), ("application", apply_url)):
            if page_url:
                targets.append(
                    PageTarget(pid, vid, employer, src, token, sid, title, kind, page_url)
                )
    return targets


# Wording that explicitly says the posting no longer takes applications. Only matched on the
# posting's own page (the final URL still names the posting), never on a board front page
# or an error page, and a missing or failing page is never treated as a closure.
CLOSURE_PHRASES = re.compile(
    r"no longer (accepting|taking) applications|not (currently )?accepting applications"
    r"|applications? (are|is|have been|has been) closed|this (job|position|posting|role|"
    r"opening|vacancy) (is|has been) (closed|filled)|position has been filled",
    re.IGNORECASE,
)


def closure_notice(markup: str) -> str | None:
    """The sentence of a page that explicitly says applications are closed, if any. Only
    visible text counts: scripts and styles often carry interface strings for other states."""
    text = " ".join(visible_text(markup).split())
    match = CLOSURE_PHRASES.search(text)
    if match is None:
        return None
    start = max(0, match.start() - 80)
    return " ".join(text[start : match.end() + 80].split())


def check_page(client: httpx.Client, target: PageTarget, pacer: Pacer) -> PageResult:
    response, error = _request(client, target.url, pacer)
    if response is None:
        return PageResult(target, "unreachable", None, None, error)
    status, final = response.status_code, str(response.url)
    if status in (401, 403):
        return PageResult(target, "refused", status, final, "access refused; not worked around")
    if status == 429:
        return PageResult(
            target, "rate_limited", status, final, response.headers.get("retry-after")
        )
    if status in (404, 410):
        return PageResult(target, "not_found", status, final, None)
    if status >= 500:
        return PageResult(target, "server_error", status, final, None)
    if status != 200:
        return PageResult(target, "unreachable", status, final, f"HTTP {status}")
    if target.source_posting_id not in final:
        # The page loaded, but somewhere else (for example the board's front page). The
        # posting may be gone or moved; this check alone does not decide which.
        return PageResult(
            target, "redirected", status, final, "landed on a page without the posting id"
        )
    notice = closure_notice(response.text)
    if notice:
        return PageResult(target, "closed_notice", status, final, notice)
    return PageResult(target, "available", status, final, None)


def check_pages(
    conn: psycopg.Connection,
    client: httpx.Client,
    targets: list[PageTarget],
    clock: Callable[[], datetime],
    pacer: Pacer,
) -> list[PageResult]:
    results = []
    for target in targets:
        result = check_page(client, target, pacer)
        checked_at = clock()
        check_id = conn.execute(
            "INSERT INTO posting_page_checks (posting_id, version_id, url_kind, url, checked_at, "
            "http_status, final_url, outcome, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING page_check_id",
            (
                target.posting_id,
                target.version_id,
                target.url_kind,
                target.url,
                checked_at,
                result.http_status,
                result.final_url,
                result.outcome,
                result.detail,
            ),
        ).fetchone()[0]
        if result.outcome == "closed_notice":
            close_posting(
                conn,
                target.posting_id,
                "the employer's job page says applications are closed",
                result.final_url,
                result.detail,
                checked_at,
                source="page check",
                page_check_id=check_id,
            )
        results.append(result)
    return results
