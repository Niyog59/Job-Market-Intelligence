"""Test doubles for the Lever Postings API: posting objects and paged responses."""

import json
import uuid

import httpx
from gh_fakes import DATA_DUTIES

LEVER_CREATED_MS = 1_793_000_000_000  # 2026-10-25 UTC, as epoch milliseconds


def lever_id(n: int) -> str:
    """A stable UUID-shaped posting id for test number n."""
    return str(uuid.UUID(int=n))


def lever_job(
    n: int,
    title: str = "Data Engineer",
    location: str = "Dallas, TX",
    all_locations: list[str] | None = None,
    description: str = DATA_DUTIES,
    workplace: str = "onsite",
    commitment: str = "Full-time",
    token: str = "lev",
    lists: list[dict] | None = None,
    created_at: int | None = LEVER_CREATED_MS,
    salary: dict | None = None,
) -> dict:
    pid = lever_id(n)
    job = {
        "id": pid,
        "text": title,
        "categories": {
            "commitment": commitment,
            "location": location,
            "team": "Data",
            "allLocations": all_locations if all_locations is not None else [location],
        },
        "country": "US",
        "description": description,
        "descriptionPlain": "(plain text not used)",
        "lists": lists
        if lists is not None
        else [{"text": "Requirements", "content": "<li>3 years of SQL</li><li>Python</li>"}],
        "additional": "<div>We are an equal opportunity employer.</div>",
        "hostedUrl": f"https://jobs.lever.co/{token}/{pid}",
        "applyUrl": f"https://jobs.lever.co/{token}/{pid}/apply",
        "workplaceType": workplace,
    }
    if created_at is not None:
        job["createdAt"] = created_at
    if salary is not None:
        job["salaryRange"] = salary
    return job


def lever_page(jobs: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "application/json"}, content=json.dumps(jobs).encode()
    )


def lever_pages(jobs: list[dict], size: int = 100) -> list[httpx.Response]:
    """The responses a board with these jobs gives to successive pages of `size`, ending with
    the short (possibly empty) page that marks the end."""
    pages = [lever_page(jobs[i : i + size]) for i in range(0, len(jobs), size)]
    if not pages or len(jobs) % size == 0:
        pages.append(lever_page([]))
    return pages
