"""The contract between the shared collector and the per-provider connectors.

A connector knows one provider's public job-board API: how to request one board (including
any pagination) and how to turn the saved response pages into validated records. Everything
else is shared and lives in the collector: scan rows, raw-response storage, the write
transaction, history, retries, incidents, and alerts. So every source gets the same
guarantees, and adding a source means adding a connector, not a new pipeline.

A connector's parse() works only from saved pages (status, headers, body, request URL). A
replay of stored raw files therefore reaches the same validation outcome as the original
collection did.
"""

import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Protocol

import httpx

from jobmarket.normalize import SourceContent

USER_AGENT = "job-market-intelligence/0.1 (+https://github.com/Niyog59/Job-Market-Intelligence)"
REQUEST_TIMEOUT_SECONDS = 30.0
# A rate-limited (429) or unavailable (503) response is waited out inside the same board
# scan only when the server asks for a short wait, and at most once per board scan, so
# pagination can never turn one attempt into a hidden series of retries. Longer waits end
# the scan; its retry is scheduled by the incident logic instead.
MAX_INLINE_RETRY_AFTER_SECONDS = 30.0

KEPT_HEADERS = ("content-type", "date", "x-request-id", "retry-after", "cache-control", "etag")


# --- Records -------------------------------------------------------------------------


class PostingRecord(Protocol):
    """What the shared storage needs from one validated job, whatever its source."""

    source_id: Any  # sortable; stored as text
    title: str
    absolute_url: str  # the posting page as the source gives it
    apply_url: str | None
    updated_at: datetime | None
    first_published: datetime | None
    created_at: datetime | None
    location_name: str | None
    content_html: str
    description_text: str
    offices: list[dict[str, Any]]
    departments: list[dict[str, Any]]
    requisition_id: str | None
    internal_job_id: int | None
    metadata: Any
    source_fields: dict[str, Any] | None
    record_index: int
    page_no: int

    def content_hash(self) -> str: ...

    def source_content(self) -> SourceContent: ...


@dataclass(frozen=True)
class Rejection:
    record_index: int | None
    source_id: str | None
    reason: str
    detail: dict[str, Any] | None = None
    page_no: int = 1


@dataclass
class ParsedResponse:
    """Validation result for one board scan. outcome is "complete", "partial", or "failed".

    complete: every page was read to the documented end, every record is valid, and any
              count the source reports matches.
    partial:  usable records, but something is missing or wrong (invalid records, a count
              mismatch, a page that failed or never arrived, a pagination anomaly). Unseen
              jobs are never treated as gone after a partial scan.
    failed:   nothing usable arrived.
    """

    outcome: str
    jobs: list[PostingRecord] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    reported_total: int | None = None
    received: int | None = None
    error: str | None = None
    pages: int | None = None


# --- Fetch results -------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One HTTP request of a board scan. body is None when no response arrived."""

    sequence_no: int
    request_url: str
    fetched_at: datetime
    status_code: int | None
    body: bytes | None
    headers: dict[str, str]
    error: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class BoardFetch:
    pages: tuple[Page, ...]

    @property
    def collected_at(self) -> datetime:
        """Collection time of the scan: when its first page was fetched. All records of one
        scan share it, so a scan is one point in a posting's history."""
        return self.pages[0].fetched_at

    @property
    def retry_after_seconds(self) -> float | None:
        values = [p.retry_after_seconds for p in self.pages if p.retry_after_seconds is not None]
        return max(values) if values else None

    @property
    def received_pages(self) -> list[Page]:
        return [p for p in self.pages if p.body is not None]


@dataclass(frozen=True)
class SavedPage:
    """A page as parse() sees it: from a live fetch or read back from raw storage."""

    sequence_no: int
    request_url: str
    status_code: int | None
    headers: dict[str, str]
    body: bytes | None
    error: str | None = None


def saved_pages(fetch: BoardFetch) -> list[SavedPage]:
    return [
        SavedPage(p.sequence_no, p.request_url, p.status_code, p.headers, p.body, p.error)
        for p in fetch.pages
    ]


# --- Polite request helpers ------------------------------------------------------------


class Pacer:
    """Minimum spacing between request starts to one provider, shared by every worker that
    talks to it. Thread-safe."""

    def __init__(
        self,
        interval: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = max(0.0, interval)
        self.sleep = sleep
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._next_start: float | None = None

    def wait(self) -> None:
        with self._lock:
            now = self.monotonic()
            if self._next_start is not None and self._next_start > now:
                self.sleep(self._next_start - now)
                now = self._next_start
            self._next_start = now + self.interval


class InlineRetryBudget:
    """At most one short Retry-After wait per board scan (see MAX_INLINE_RETRY_AFTER_SECONDS)."""

    def __init__(self) -> None:
        self.used = False

    def take(self, retry_after: float | None) -> bool:
        if self.used or retry_after is None or retry_after > MAX_INLINE_RETRY_AFTER_SECONDS:
            return False
        self.used = True
        return True


def retry_after_seconds(headers: httpx.Headers) -> float | None:
    """Retry-After in seconds. Only the delta-seconds form is understood; an HTTP-date form
    is treated as absent, which leads to the normal retry schedule."""
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def get_page(
    client: httpx.Client,
    url: str,
    sequence_no: int,
    clock: Callable[[], datetime],
    pacer: Pacer,
    budget: InlineRetryBudget,
    sleep: Callable[[float], None],
) -> Page:
    """One paced GET. Transport problems are returned as a Page without a body, never raised.
    A 429/503 with a short Retry-After is waited out once per board scan."""
    while True:
        pacer.wait()
        fetched_at = clock()
        try:
            response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.TimeoutException:
            return Page(sequence_no, url, fetched_at, None, None, {}, "request timed out")
        except httpx.HTTPError as exc:
            return Page(sequence_no, url, fetched_at, None, None, {}, f"request failed: {exc}")
        wait = retry_after_seconds(response.headers)
        if response.status_code in (429, 503) and budget.take(wait):
            sleep(wait)
            continue
        headers = {k: response.headers[k] for k in KEPT_HEADERS if k in response.headers}
        return Page(
            sequence_no,
            url,
            fetched_at,
            response.status_code,
            response.content,
            headers,
            retry_after_seconds=wait,
        )


# --- Shared parsing helpers ------------------------------------------------------------


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


def parse_time(value: Any) -> datetime | None:
    """An ISO 8601 timestamp with a time zone, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --- Connector interface ---------------------------------------------------------------


class Connector(Protocol):
    source_system: str
    regions: tuple[str, ...]

    def board_url(self, token: str, region: str) -> str: ...

    def scope_key(self, region: str) -> str:
        """How this connector reads a board: API host and request settings. Absence counts
        compare only scans with equal scope keys."""
        ...

    def fetch(
        self,
        client: httpx.Client,
        token: str,
        region: str,
        clock: Callable[[], datetime],
        pacer: Pacer,
        sleep: Callable[[float], None],
    ) -> BoardFetch: ...

    def parse(self, pages: Sequence[SavedPage]) -> ParsedResponse: ...


def get_connector(source_system: str) -> Connector:
    # Imported here so the provider modules can import this one.
    from jobmarket.greenhouse import GREENHOUSE
    from jobmarket.lever import LEVER

    connectors = {c.source_system: c for c in (GREENHOUSE, LEVER)}
    try:
        return connectors[source_system]
    except KeyError:
        raise ValueError(f"no connector for source system {source_system!r}") from None


SOURCE_SYSTEMS = ("greenhouse", "lever")
