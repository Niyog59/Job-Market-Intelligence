"""Greenhouse Job Board API: fetch a board's jobs and validate the response.

API contract (https://docs.greenhouse.io/job-board.html): public, unauthenticated
`GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` returns
`{"jobs": [...], "meta": {"total": N}}` in a single response; no pagination is documented,
so none is assumed. EU-hosted boards are served by the same API host; only their
human-facing `absolute_url` uses job-boards.eu.greenhouse.io. `content` is HTML-escaped.
"""

import hashlib
import html
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from jobmarket.connectors import (
    USER_AGENT,
    BoardFetch,
    InlineRetryBudget,
    Pacer,
    Page,
    ParsedResponse,
    Rejection,
    SavedPage,
    get_page,
    html_to_text,
    parse_time,
)
from jobmarket.normalize import SourceContent

__all__ = [
    "GREENHOUSE",
    "USER_AGENT",
    "JobRecord",
    "ParsedResponse",
    "Rejection",
    "fetch_jobs",
    "html_to_text",
    "jobs_url",
    "parse_jobs_response",
]

API_BASE = "https://boards-api.greenhouse.io/v1/boards"
BOARD_URL = {
    "us": "https://job-boards.greenhouse.io/{token}",
    "eu": "https://job-boards.eu.greenhouse.io/{token}",
}

# Kept for callers that think of one Greenhouse request as a fetch result.
FetchResult = Page


def jobs_url(token: str) -> str:
    return f"{API_BASE}/{token}/jobs?content=true"


def fetch_jobs(
    client: httpx.Client,
    token: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
    pacer: Pacer | None = None,
) -> Page:
    """Fetch one board: a single request. Transport problems are returned, never raised."""
    return get_page(
        client, jobs_url(token), 1, clock, pacer or Pacer(0, sleep), InlineRetryBudget(), sleep
    )


@dataclass(frozen=True)
class JobRecord:
    """One validated job from a board response."""

    source_id: int
    title: str
    absolute_url: str
    updated_at: datetime
    first_published: datetime | None
    location_name: str | None
    content_html: str
    description_text: str
    offices: list[dict[str, Any]]
    departments: list[dict[str, Any]]
    requisition_id: str | None
    internal_job_id: int | None
    metadata: Any
    company_name: str | None
    record_index: int
    page_no: int = 1
    # Greenhouse has no separate application URL, creation time, or extra structured fields.
    apply_url: str | None = None
    created_at: datetime | None = None
    source_fields: dict[str, Any] | None = None
    identity_basis: str = "source_id"

    def content_hash(self) -> str:
        """Exact source fingerprint: every field as the source sent it, except source
        timestamps such as updated_at, which change without the job changing. Equal
        fingerprints mean the source content is identical. Whether a difference is
        meaningful is decided separately by jobmarket.normalize."""
        canonical = {
            "title": self.title,
            "location": self.location_name,
            "content": self.content_html,
            "offices": sorted((str(o.get("name")), str(o.get("location"))) for o in self.offices),
            "departments": sorted(str(d.get("name")) for d in self.departments),
            "absolute_url": self.absolute_url,
            "requisition_id": self.requisition_id,
            "internal_job_id": self.internal_job_id,
            "metadata": self.metadata,
            "company_name": self.company_name,
        }
        encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def source_content(self) -> SourceContent:
        return SourceContent(
            title=self.title,
            location=self.location_name,
            content_html=self.content_html,
            offices=self.offices,
            departments=self.departments,
            metadata=self.metadata,
            description_text=self.description_text,
        )


_URL_ID = re.compile(r"/jobs/(\d+)(?:[/?#]|$)|[?&]gh_jid=(\d+)")


def id_from_url(url: Any) -> int | None:
    """The job id in a job-specific Greenhouse URL (/jobs/<id> or ?gh_jid=<id>), if any."""
    if not isinstance(url, str):
        return None
    match = _URL_ID.search(url)
    return int(match.group(1) or match.group(2)) if match else None


def _validate_job(raw: Any, index: int) -> JobRecord | Rejection:
    if not isinstance(raw, dict):
        return Rejection(index, None, "record is not an object")
    source_id = raw.get("id")
    sid = str(source_id) if source_id is not None else None
    identity_basis = "source_id"
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        # Without an id, a job-specific URL that carries one is a stable identity; the
        # posting is marked so its identity can be reviewed.
        source_id = id_from_url(raw.get("absolute_url"))
        identity_basis = "job_url"
        if source_id is None:
            return Rejection(
                index, sid, "missing or invalid id and no job-specific URL: identity unknown",
                {"title": raw.get("title"), "absolute_url": raw.get("absolute_url")},
            )  # fmt: skip
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return Rejection(index, sid, "missing title")
    url = raw.get("absolute_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return Rejection(index, sid, "missing or non-https absolute_url", {"absolute_url": url})
    updated_at = parse_time(raw.get("updated_at"))
    if updated_at is None:
        return Rejection(
            index, sid, "missing or unparseable updated_at", {"updated_at": raw.get("updated_at")}
        )
    content = raw.get("content")
    if not isinstance(content, str):
        return Rejection(index, sid, "missing content (requested with content=true)")
    first_published_raw = raw.get("first_published")
    first_published = parse_time(first_published_raw)
    if first_published_raw not in (None, "") and first_published is None:
        return Rejection(
            index, sid, "unparseable first_published", {"first_published": first_published_raw}
        )
    location = raw.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None
    offices = [o for o in raw.get("offices") or [] if isinstance(o, dict)]
    departments = [d for d in raw.get("departments") or [] if isinstance(d, dict)]
    internal = raw.get("internal_job_id")
    content_html = html.unescape(content)
    return JobRecord(
        source_id=source_id,
        title=title.strip(),
        absolute_url=url,
        updated_at=updated_at,
        first_published=first_published,
        location_name=location_name.strip() if isinstance(location_name, str) else None,
        content_html=content_html,
        description_text=html_to_text(content_html),
        offices=offices,
        departments=departments,
        requisition_id=str(raw["requisition_id"]) if raw.get("requisition_id") else None,
        internal_job_id=internal if isinstance(internal, int) else None,
        metadata=raw.get("metadata"),
        company_name=raw.get("company_name") if isinstance(raw.get("company_name"), str) else None,
        record_index=index,
        identity_basis=identity_basis,
    )


def parse_jobs_response(
    status_code: int | None, headers: dict[str, str], body: bytes | None
) -> ParsedResponse:
    """Decide whether a response is usable and validate every job in it.

    complete: HTTP 200, valid JSON, every record valid, and the record count matches
              meta.total (a valid empty board is complete with zero jobs).
    partial:  usable but some records were rejected or the count does not match.
    failed:   nothing usable (HTTP error, unreadable JSON, wrong shape, all records bad).
    """
    if status_code is None or body is None:
        return ParsedResponse("failed", error="no response received")
    if status_code != 200:
        return ParsedResponse("failed", error=f"HTTP {status_code}")
    content_type = headers.get("content-type", "")
    if "json" not in content_type.lower():
        return ParsedResponse("failed", error=f"unexpected content type {content_type!r}")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ParsedResponse("failed", error="response is not valid JSON")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return ParsedResponse("failed", error="response has no jobs list")

    raw_jobs = data["jobs"]
    meta = data.get("meta")
    total = meta.get("total") if isinstance(meta, dict) else None
    reported = total if isinstance(total, int) and not isinstance(total, bool) else None

    jobs: list[JobRecord] = []
    rejected: list[Rejection] = []
    seen: set[int] = set()
    for index, raw in enumerate(raw_jobs):
        result = _validate_job(raw, index)
        if isinstance(result, Rejection):
            rejected.append(result)
        elif result.source_id in seen:
            rejected.append(Rejection(index, str(result.source_id), "duplicate id in response"))
        else:
            seen.add(result.source_id)
            jobs.append(result)

    received = len(raw_jobs)
    parsed = ParsedResponse("complete", jobs, rejected, reported, received)
    problems = []
    if reported is None:
        problems.append("meta.total missing, completeness unknown")
    elif reported != received:
        problems.append(f"received {received} records but meta.total is {reported}")
    if rejected:
        problems.append(f"{len(rejected)} invalid record(s)")
    if received > 0 and not jobs:
        parsed.outcome = "failed"
        parsed.error = "no valid records: " + "; ".join(problems)
    elif problems:
        parsed.outcome = "partial"
        parsed.error = "; ".join(problems)
    return parsed


class GreenhouseConnector:
    """One request per board; completeness is checked against the API's own meta.total."""

    source_system = "greenhouse"
    regions = ("us", "eu")

    def board_url(self, token: str, region: str) -> str:
        return BOARD_URL[region].format(token=token)

    def scope_key(self, region: str) -> str:
        # The API host is the same for every region, so the region is not part of the scope.
        return f"greenhouse:{API_BASE}/{{token}}/jobs?content=true"

    def fetch(
        self,
        client: httpx.Client,
        token: str,
        region: str,
        clock: Callable[[], datetime],
        pacer: Pacer,
        sleep: Callable[[float], None],
    ) -> BoardFetch:
        # EU-hosted boards are served by the same API host (see the module docstring).
        return BoardFetch((fetch_jobs(client, token, clock, sleep, pacer),))

    def parse(self, pages: Sequence[SavedPage]) -> ParsedResponse:
        page = pages[0]
        if page.body is None:
            return ParsedResponse("failed", error=page.error or "no response received", pages=1)
        parsed = parse_jobs_response(page.status_code, page.headers, page.body)
        parsed.pages = 1
        return parsed


GREENHOUSE = GreenhouseConnector()
