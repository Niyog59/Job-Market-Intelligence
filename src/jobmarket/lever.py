"""Lever Postings API: fetch every page of a board and validate it.

API contract (https://github.com/lever/postings-api): public, unauthenticated
`GET https://api.lever.co/v0/postings/{site}?mode=json&skip={offset}&limit={n}` returns a
JSON array of published postings. Accounts on Lever's EU instance use api.eu.lever.co (job
pages on jobs.eu.lever.co) instead; which instance a board uses is recorded per board from
the employer's own careers link, never guessed from where the employer is based.

Lever reports no total count. A board scan is therefore complete only when pagination
reaches a page shorter than the requested limit. Stopping anywhere else (a failed or
missing page, a repeated page, a page larger than requested, the page cap) is a partial
scan: the records seen are kept, and nothing unseen is treated as gone.

Fields used, as documented: id, text, categories (location, commitment, team, department,
allLocations), country, description (opening and body as HTML), lists, additional,
hostedUrl, applyUrl, workplaceType, salaryRange, salaryDescription. `createdAt` (epoch
milliseconds) is returned but not documented; it is kept as the source's creation time of
the posting and is never used as a publication or posting date. Lever exposes no update
time.
"""

import hashlib
import html
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from jobmarket.connectors import (
    BoardFetch,
    InlineRetryBudget,
    Pacer,
    ParsedResponse,
    Rejection,
    SavedPage,
    get_page,
    html_to_text,
)
from jobmarket.normalize import SourceContent

API_BASE = {
    "global": "https://api.lever.co/v0/postings",
    "eu": "https://api.eu.lever.co/v0/postings",
}
BOARD_URL = {
    "global": "https://jobs.lever.co/{token}",
    "eu": "https://jobs.eu.lever.co/{token}",
}
PAGE_SIZE = 100
# Safety cap (MAX_PAGES * PAGE_SIZE postings). A board that reaches it is scanned as
# partial, so a source that ignores `skip` can never make a run loop forever.
MAX_PAGES = 50

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{3,63}")


_URL_ID = re.compile(
    r"jobs(?:\.eu)?\.lever\.co/[^/?#]+/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def id_from_url(url: Any) -> str | None:
    """The posting id in a Lever job page URL (jobs.lever.co/<site>/<id>), if any."""
    if not isinstance(url, str):
        return None
    match = _URL_ID.search(url)
    return match.group(1).lower() if match else None


def postings_url(token: str, region: str, skip: int, limit: int = PAGE_SIZE) -> str:
    return f"{API_BASE[region]}/{token}?mode=json&skip={skip}&limit={limit}"


@dataclass(frozen=True)
class LeverJob:
    """One validated Lever posting, in the shape the shared storage uses."""

    source_id: str
    title: str
    absolute_url: str  # hostedUrl: Lever's page for the posting
    apply_url: str | None
    created_at: datetime | None
    location_name: str | None
    all_locations: tuple[str, ...]
    workplace_type: str | None
    country: str | None
    commitment: str | None
    team: str | None
    department: str | None
    description_html: str
    lists: tuple[tuple[str, str], ...]
    additional_html: str
    salary_range: dict[str, Any] | None
    salary_description_html: str | None
    record_index: int
    page_no: int
    # Lever has no update time, publication time, requisition id, or internal job id.
    updated_at: datetime | None = None
    first_published: datetime | None = None
    requisition_id: str | None = None
    internal_job_id: int | None = None
    identity_basis: str = "source_id"

    @cached_property
    def content_html(self) -> str:
        """The full posting as one HTML document: description, then each list under its
        heading, then the closing text and the salary description."""
        parts = [self.description_html]
        for heading, items in self.lists:
            parts.append(f"<h3>{html.escape(heading)}</h3><ul>{items}</ul>")
        if self.additional_html:
            parts.append(self.additional_html)
        if self.salary_description_html:
            parts.append(self.salary_description_html)
        return "\n".join(p for p in parts if p)

    @cached_property
    def description_text(self) -> str:
        return html_to_text(self.content_html)

    @property
    def offices(self) -> list[dict[str, Any]]:
        """Every posting location (allLocations includes the primary one), plus the
        workplace type, which carries the remote or hybrid restriction. Screening and
        meaningful-change detection both read these."""
        locations = list(self.all_locations) or ([self.location_name] if self.location_name else [])
        entries: list[dict[str, Any]] = [{"location": loc} for loc in locations]
        if self.workplace_type and self.workplace_type != "unspecified":
            entries.append({"name": f"Workplace type: {self.workplace_type}"})
        return entries

    @property
    def departments(self) -> list[dict[str, Any]]:
        return [
            {"name": value, "kind": kind}
            for kind, value in (("department", self.department), ("team", self.team))
            if value
        ]

    @property
    def metadata(self) -> list[dict[str, Any]]:
        """Structured fields as name/value pairs, the shape the comparison rules read for
        published compensation and employment type."""
        fields = [
            ("Employment type (commitment)", self.commitment),
            ("Workplace type", self.workplace_type),
            ("Country", self.country),
            ("Salary range", self.salary_range),
        ]
        return [{"name": n, "value": v} for n, v in fields if v not in (None, "", {})]

    @property
    def source_fields(self) -> dict[str, Any]:
        """The structured fields exactly as Lever named them, kept for provenance."""
        return {
            "categories": {
                "location": self.location_name,
                "allLocations": list(self.all_locations),
                "commitment": self.commitment,
                "team": self.team,
                "department": self.department,
            },
            "country": self.country,
            "workplaceType": self.workplace_type,
            "salaryRange": self.salary_range,
            "hostedUrl": self.absolute_url,
            "applyUrl": self.apply_url,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }

    def content_hash(self) -> str:
        """Exact source fingerprint: every field as Lever sent it except createdAt."""
        canonical = {
            "title": self.title,
            "location": self.location_name,
            "all_locations": sorted(self.all_locations),
            "workplace_type": self.workplace_type,
            "country": self.country,
            "commitment": self.commitment,
            "team": self.team,
            "department": self.department,
            "description": self.description_html,
            "lists": [list(item) for item in self.lists],
            "additional": self.additional_html,
            "salary_range": self.salary_range,
            "salary_description": self.salary_description_html,
            "hosted_url": self.absolute_url,
            "apply_url": self.apply_url,
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


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _https(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("https://")


def _validate(raw: Any, index: int, page_no: int) -> LeverJob | Rejection:
    def reject(source_id, reason, detail=None):
        return Rejection(index, source_id, reason, detail, page_no)

    if not isinstance(raw, dict):
        return reject(None, "record is not an object")
    source_id = raw.get("id")
    sid = source_id if isinstance(source_id, str) else None
    identity_basis = "source_id"
    if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
        # Without an id, the posting id in its job-specific hostedUrl is a stable identity;
        # the posting is marked so its identity can be reviewed.
        source_id = id_from_url(raw.get("hostedUrl"))
        identity_basis = "job_url"
        if source_id is None:
            return reject(
                str(raw.get("id")) if raw.get("id") is not None else None,
                "missing or invalid id and no job-specific URL: identity unknown",
                {"text": raw.get("text"), "hostedUrl": raw.get("hostedUrl")},
            )
        sid = source_id
    title = _text(raw.get("text"))
    if title is None:
        return reject(sid, "missing title (text)")
    if not _https(raw.get("hostedUrl")):
        return reject(sid, "missing or non-https hostedUrl", {"hostedUrl": raw.get("hostedUrl")})
    apply_url = raw.get("applyUrl")
    if apply_url is not None and not _https(apply_url):
        return reject(sid, "non-https applyUrl", {"applyUrl": apply_url})
    description = raw.get("description")
    if not isinstance(description, str):
        return reject(sid, "missing description")

    categories = raw.get("categories") if raw.get("categories") is not None else {}
    if not isinstance(categories, dict):
        return reject(sid, "categories is not an object")
    all_locations = categories.get("allLocations") or []
    if not isinstance(all_locations, list) or not all(isinstance(x, str) for x in all_locations):
        return reject(sid, "categories.allLocations is not a list of strings")

    lists = raw.get("lists") or []
    if not isinstance(lists, list) or not all(
        isinstance(i, dict) and isinstance(i.get("text"), str) and isinstance(i.get("content"), str)
        for i in lists
    ):
        return reject(sid, "lists is not a list of {text, content}")

    created_raw = raw.get("createdAt")
    created_at = None
    if created_raw is not None:
        if not isinstance(created_raw, int | float) or isinstance(created_raw, bool):
            return reject(sid, "unparseable createdAt", {"createdAt": created_raw})
        created_at = datetime.fromtimestamp(created_raw / 1000, tz=UTC)

    salary = raw.get("salaryRange")
    additional = raw.get("additional")
    salary_description = raw.get("salaryDescription")
    return LeverJob(
        source_id=source_id,
        title=title,
        absolute_url=raw["hostedUrl"],
        apply_url=apply_url,
        created_at=created_at,
        location_name=_text(categories.get("location")),
        all_locations=tuple(loc.strip() for loc in all_locations if loc.strip()),
        workplace_type=_text(raw.get("workplaceType")),
        country=_text(raw.get("country")),
        commitment=_text(categories.get("commitment")),
        team=_text(categories.get("team")),
        department=_text(categories.get("department")),
        description_html=description,
        lists=tuple((i["text"], i["content"]) for i in lists),
        additional_html=additional if isinstance(additional, str) else "",
        salary_range=salary if isinstance(salary, dict) and salary else None,
        salary_description_html=_text(salary_description),
        record_index=index,
        page_no=page_no,
        identity_basis=identity_basis,
    )


def _limit_of(url: str) -> int:
    values = parse_qs(urlsplit(url).query).get("limit")
    return int(values[0]) if values else PAGE_SIZE


def _decode(page: SavedPage) -> tuple[list | None, str | None]:
    """The page's JSON array, or (None, why the page is unusable)."""
    if page.body is None:
        return None, page.error or "no response received"
    if page.status_code != 200:
        if page.status_code == 404:
            return None, "HTTP 404 (board not found)"
        return None, f"HTTP {page.status_code}"
    content_type = page.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None, f"unexpected content type {content_type!r}"
    try:
        data = json.loads(page.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "response is not valid JSON"
    if not isinstance(data, list):
        return None, "response is not a list of postings"
    return data, None


def parse_pages(pages: Sequence[SavedPage]) -> ParsedResponse:
    """Validate every page of one board scan and judge its completeness."""
    if not pages:
        return ParsedResponse("failed", error="no page was requested", pages=0)
    jobs: list[LeverJob] = []
    rejected: list[Rejection] = []
    seen: set[str] = set()
    problems: list[str] = []
    received = 0
    reached_end = False

    for page in pages:
        n = page.sequence_no
        data, error = _decode(page)
        if data is None:
            if n == 1:
                return ParsedResponse("failed", error=error, pages=len(pages))
            problems.append(f"page {n} unusable ({error}); later postings were not seen")
            break
        limit = _limit_of(page.request_url)
        received += len(data)
        ids = [r.get("id") for r in data if isinstance(r, dict)]
        if data and ids and all(i in seen for i in ids):
            problems.append(
                f"page {n} repeated postings from earlier pages (the source may be ignoring "
                "skip); pagination stopped"
            )
            break
        for index, raw in enumerate(data):
            result = _validate(raw, index, n)
            if isinstance(result, Rejection):
                rejected.append(result)
            elif result.source_id in seen:
                rejected.append(
                    Rejection(index, result.source_id, "duplicate id in response", None, n)
                )
            else:
                seen.add(result.source_id)
                jobs.append(result)
        if len(data) > limit:
            problems.append(f"page {n} returned {len(data)} postings for limit {limit}")
            break
        if len(data) < limit:
            reached_end = True
            break

    if not reached_end and not problems:
        problems.append(
            f"pagination stopped after {len(pages)} page(s) without reaching a short final "
            "page, so the board may have more postings"
        )
    if rejected:
        problems.append(f"{len(rejected)} invalid record(s)")

    parsed = ParsedResponse("complete", list(jobs), rejected, None, received, pages=len(pages))
    if received > 0 and not jobs:
        parsed.outcome = "failed"
        parsed.error = "no valid records: " + "; ".join(problems)
    elif problems:
        parsed.outcome = "partial"
        parsed.error = "; ".join(problems)
    return parsed


class LeverConnector:
    source_system = "lever"
    regions = ("global", "eu")

    def board_url(self, token: str, region: str) -> str:
        return BOARD_URL[region].format(token=token)

    def scope_key(self, region: str) -> str:
        return f"lever:{API_BASE[region]}/{{token}}?mode=json&limit={PAGE_SIZE}"

    def fetch(
        self,
        client: httpx.Client,
        token: str,
        region: str,
        clock: Callable[[], datetime],
        pacer: Pacer,
        sleep: Callable[[float], None] = time.sleep,
        max_pages: int = MAX_PAGES,
    ) -> BoardFetch:
        """Request pages until a short page, an unusable page, a repeated page, or the cap.
        Each request goes through the provider's pacer; transport problems are recorded as
        a page without a body, never raised."""
        budget = InlineRetryBudget()
        pages = []
        seen: set[str] = set()
        for n in range(1, max_pages + 1):
            url = postings_url(token, region, (n - 1) * PAGE_SIZE)
            page = get_page(client, url, n, clock, pacer, budget, sleep)
            pages.append(page)
            data, _ = _decode(
                SavedPage(n, url, page.status_code, page.headers, page.body, page.error)
            )
            if data is None or len(data) != PAGE_SIZE:
                break
            ids = {r.get("id") for r in data if isinstance(r, dict)}
            if ids <= seen:
                break
            seen |= ids
        return BoardFetch(tuple(pages))

    def parse(self, pages: Sequence[SavedPage]) -> ParsedResponse:
        return parse_pages(pages)


LEVER = LeverConnector()
