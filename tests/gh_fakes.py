"""Test doubles: fake Greenhouse responses, a scripted HTTP transport, and a clock."""

import html
import json
import threading
from datetime import UTC, datetime, timedelta

import httpx

from jobmarket.sources import BoardEntry, Registry

T0 = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)  # noon in Chicago (CDT)

DATA_DUTIES = (
    "<p>Build and maintain data pipelines with SQL, Python, and Airflow. "
    "Model data in Snowflake and publish dashboards in Tableau.</p>"
)


def gh_job(
    job_id: int,
    title: str = "Data Engineer",
    location: str = "Dallas, TX",
    content: str = DATA_DUTIES,
    updated_at: str = "2026-10-20T10:00:00-05:00",
    first_published: str | None = "2026-10-19T09:00:00-05:00",
    token: str = "acme",
    offices: list | None = None,
) -> dict:
    return {
        "id": job_id,
        "internal_job_id": job_id + 1000,
        "title": title,
        "updated_at": updated_at,
        "first_published": first_published,
        "requisition_id": f"R{job_id}",
        "location": {"name": location},
        "absolute_url": f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
        "language": "en",
        "metadata": None,
        "content": html.escape(content),  # the API returns HTML-escaped content
        "departments": [{"id": 1, "name": "Data"}],
        "offices": offices
        if offices is not None
        else [{"id": 2, "name": location, "location": location}],
        "company_name": "Acme",
    }


def gh_body(jobs: list[dict], total: int | None = None) -> bytes:
    return json.dumps(
        {"jobs": jobs, "meta": {"total": len(jobs) if total is None else total}}
    ).encode()


def ok(jobs: list[dict], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "application/json"}, content=gh_body(jobs, total)
    )


class ScriptedTransport(httpx.MockTransport):
    """Serves queued responses per board token. The last queued response repeats.
    A queued exception instance is raised instead (for example httpx.ReadTimeout)."""

    def __init__(self) -> None:
        self.queues: dict[str, list] = {}
        self.requests: list[httpx.Request] = []
        super().__init__(self._handle)

    def queue(self, token: str, *responses) -> None:
        self.queues.setdefault(token, []).extend(responses)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.url.path.split("/")[3]
        queue = self.queues.get(token)
        if not queue:
            return httpx.Response(404, json={"error": "not found"})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start
        self._lock = threading.Lock()  # fetch workers read the clock from several threads

    def __call__(self) -> datetime:
        with self._lock:
            self.now += timedelta(milliseconds=1)  # strictly increasing, like a real clock
            return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def board(
    token: str,
    employer: str | None = None,
    slug: str | None = None,
    enabled: bool = True,
    source: str = "greenhouse",
) -> BoardEntry:
    name = employer or token.title()
    return BoardEntry(
        employer=name,
        employer_slug=slug or token,
        token=token,
        region="us" if source == "greenhouse" else "global",
        enabled=enabled,
        verification="verified" if enabled else "pending",
        metros=("dfw",),
        local_evidence="Dallas",
        official_source=f"https://{token}.example/careers",
        evidence="test",
        checks="",
        group=None,
        source_system=source,
    )


def registry(*boards: BoardEntry) -> Registry:
    return Registry(boards=tuple(boards), leads=())
