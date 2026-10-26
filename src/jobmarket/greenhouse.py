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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

import httpx

API_BASE = "https://boards-api.greenhouse.io/v1/boards"
USER_AGENT = "job-market-intelligence/0.1 (+https://github.com/Niyog59/Job-Market-Intelligence)"
REQUEST_TIMEOUT_SECONDS = 30.0
# A rate-limited (429) or unavailable (503) response is retried once inside the same
# attempt only when the server asks for a short wait; longer waits become a failed scan
# whose retry is scheduled instead of blocking the worker.
MAX_INLINE_RETRY_AFTER_SECONDS = 30.0

KEPT_HEADERS = ("content-type", "date", "x-request-id", "retry-after", "cache-control")


def jobs_url(token: str) -> str:
    return f"{API_BASE}/{token}/jobs?content=true"


@dataclass(frozen=True)
class FetchResult:
    request_url: str
    fetched_at: datetime
    status_code: int | None
    body: bytes | None
    headers: dict[str, str]
    error: str | None = None
    retry_after_seconds: float | None = None


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def fetch_jobs(
    client: httpx.Client,
    token: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """Fetch one board. Transport problems are returned as a result, never raised."""
    url = jobs_url(token)
    for attempt in (1, 2):
        fetched_at = clock()
        try:
            response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.TimeoutException:
            return FetchResult(url, fetched_at, None, None, {}, "request timed out")
        except httpx.HTTPError as exc:
            return FetchResult(url, fetched_at, None, None, {}, f"request failed: {exc}")

        retry_after = _retry_after(response.headers)
        if (
            response.status_code in (429, 503)
            and attempt == 1
            and retry_after is not None
            and retry_after <= MAX_INLINE_RETRY_AFTER_SECONDS
        ):
            sleep(retry_after)
            continue
        headers = {k: response.headers[k] for k in KEPT_HEADERS if k in response.headers}
        return FetchResult(
            url,
            fetched_at,
            response.status_code,
            response.content,
            headers,
            retry_after_seconds=retry_after,
        )
    raise AssertionError("unreachable")


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


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

    def content_hash(self) -> str:
        """Fingerprint of the job content. Source timestamps such as updated_at are left
        out on purpose: they can change without the job changing, and such changes must
        not be recorded as new versions."""
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


@dataclass(frozen=True)
class Rejection:
    record_index: int | None
    source_id: str | None
    reason: str
    detail: dict[str, Any] | None = None


@dataclass
class ParsedResponse:
    """Validation result. outcome is "complete", "partial", or "failed"."""

    outcome: str
    jobs: list[JobRecord] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    reported_total: int | None = None
    received: int | None = None
    error: str | None = None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _validate_job(raw: Any, index: int) -> JobRecord | Rejection:
    if not isinstance(raw, dict):
        return Rejection(index, None, "record is not an object")
    source_id = raw.get("id")
    sid = str(source_id) if source_id is not None else None
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        return Rejection(index, sid, "missing or invalid id")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return Rejection(index, sid, "missing title")
    url = raw.get("absolute_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return Rejection(index, sid, "missing or non-https absolute_url", {"absolute_url": url})
    updated_at = _parse_time(raw.get("updated_at"))
    if updated_at is None:
        return Rejection(
            index, sid, "missing or unparseable updated_at", {"updated_at": raw.get("updated_at")}
        )
    content = raw.get("content")
    if not isinstance(content, str):
        return Rejection(index, sid, "missing content (requested with content=true)")
    first_published_raw = raw.get("first_published")
    first_published = _parse_time(first_published_raw)
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
