"""A synthetic job history for the search and website tests, built with the real
collector, availability rules, duplicate rules, and extraction queue. Only HTTP responses and
model answers are scripted.

Board acme (Greenhouse) lists:
  J1  Junior Data Engineer, Dallas    early career; Python required, SQL preferred,
                                      Airflow only mentioned; no sponsorship; hybrid; 1 year
  J2  Data Analyst, Houston           early career unclear; Python or Java (required);
                                      sponsorship supported; on site; 3 years; "Looker";
                                      its description also carries a script and a
                                      javascript: link, which must never be rendered
  J3  Backend Engineer, Dallas        first seen after the extraction run: pending
  J4  Senior Data Engineer, Dallas    not early career; 6 years; missing from the last scan
                                      (possibly unavailable); no source publication date
  J6  Sales Manager, Dallas           not kept by the screening rules
  J7  Data Engineer II, Dallas        early career; closed on official evidence
  J8  Analytics Engineer, Dallas      early career; missing from three qualifying scans
Board acme2 (same employer) lists J5, Junior Data Engineer in Houston with J1's
requisition: a strong duplicate group with J1. acme2 is scanned last, so J5 is the most
recently observed copy.
"""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from extraction_fakes import answer, reply
from gh_fakes import FakeClock, ScriptedTransport, board, gh_job, ok, registry

from jobmarket import db
from jobmarket.availability import close_posting
from jobmarket.collector import Collector
from jobmarket.extraction import ExtractionConfig, run_extraction
from jobmarket.migrate import apply_migrations
from jobmarket.search import load_choices
from jobmarket.sources import sync_registry


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def loc(city: str) -> list[dict]:
    return [{"original": f"{city}, TX", "city": city, "state": "TX", "country": None,
             "evidence": []}]  # fmt: skip


def arrangement(value: str) -> dict:
    return {"value": value, "onsite_days_per_week": None, "onsite_frequency_text": None,
            "evidence": []}  # fmt: skip


def years(n: int) -> list[dict]:
    return [{"scope": "overall", "subject": None, "min_years": n, "max_years": None,
             "status": "required", "alternative_group": None, "evidence": []}]  # fmt: skip


def early(status: str) -> dict:
    return {"status": status, "basis": [], "evidence": []}


def skill(name: str, status: str) -> dict:
    return {"name": name, "status": status, "evidence": []}


def role(category: str) -> list[dict]:
    return [{"category": category, "basis": "title", "evidence": []}]


def employment(value: str) -> dict:
    return {"value": value, "evidence": []}


JUNIOR = dict(
    role_categories=role("data_engineering"), early_career=early("yes"), experience=years(1),
    skills=[skill("Python", "required"), skill("SQL", "preferred"),
            skill("Airflow", "mentioned")],
    sponsorship={"status": "not_supported", "evidence": []},
    work_arrangement=arrangement("hybrid"), employment_type=employment("full_time"),
)  # fmt: skip
ANSWERS = {
    ("Junior Data Engineer", "Dallas"): answer(**JUNIOR, locations=loc("Dallas")),
    ("Junior Data Engineer", "Houston"): answer(**JUNIOR, locations=loc("Houston")),
    ("Data Analyst", "Houston"): answer(
        role_categories=role("data_analysis"), early_career=early("unclear"),
        experience=years(3), locations=loc("Houston"),
        skills=[skill("Python", "required"), skill("Java", "required")],
        skill_groups=[{"relation": "any_of", "min_count": None, "members": ["Python", "Java"],
                       "status": "required", "display_text": "Python or Java",
                       "evidence": []}],
        sponsorship={"status": "supported", "evidence": []},
        work_arrangement=arrangement("onsite"),
    ),
    ("Senior Data Engineer", "Dallas"): answer(
        role_categories=role("data_engineering"), early_career=early("no"),
        experience=years(6), locations=loc("Dallas"), skills=[skill("Python", "required")],
    ),
    ("Sales Manager", "Dallas"): answer(role_categories=role("other"), early_career=early("yes"),
                                        locations=loc("Dallas")),
    ("Data Engineer II", "Dallas"): answer(role_categories=role("data_engineering"),
                                           early_career=early("yes"), locations=loc("Dallas")),
    ("Analytics Engineer", "Dallas"): answer(role_categories=role("analytics_engineering"),
                                             early_career=early("yes"), locations=loc("Dallas")),
}  # fmt: skip


class ByPosting:
    """Answers by the posting's title and city, as read from the extraction document."""

    def __call__(self, instructions: str, text: str, schema: dict):
        for (title, city), value in ANSWERS.items():
            if f"Title: {title}\nLocation: {city}" in text:
                return reply(value)
        raise AssertionError(f"no scripted answer for {text[:80]!r}")


def job(n, title, city="Dallas", token="acme", req=None, published="2026-10-19T09:00:00-05:00",
        content="<p>Build things with the team.</p>"):  # fmt: skip
    item = gh_job(n, title=title, location=f"{city}, TX", token=token, content=content,
                  first_published=published)  # fmt: skip
    item["requisition_id"] = req or f"R{n}"
    return item


J1 = job(1, "Junior Data Engineer")
J2 = job(2, "Data Analyst", "Houston", published="2026-10-25T09:00:00-05:00",
         content='<p>Analyze retention with Looker dashboards.</p><script>alert("x")</script>'
         '<p><a href="javascript:alert(1)">Apply here</a></p>')  # fmt: skip
J3 = job(3, "Backend Engineer")
J4 = job(4, "Senior Data Engineer", published=None)
J6 = job(6, "Sales Manager")
J7 = job(7, "Data Engineer II")
J8 = job(8, "Analytics Engineer")
J5 = job(5, "Junior Data Engineer", "Houston", token="acme2", req="R1")
LAST_SCAN = at("2026-10-28T19:00:00")


@pytest.fixture(scope="module")
def world(db_settings, tmp_path_factory):
    # Imported here: the harness skips when dbt is not installed, and this module is
    # loaded by conftest for every test.
    from analytics_harness import Analytics

    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = db.connect(db_settings, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    conn = db.connect(db_settings, autocommit=True, options=f"-c search_path={schema}")
    apply_migrations(conn)
    sync_registry(conn, registry(board("acme"), board("acme2", employer="Acme", slug="acme")),
                  FakeClock()())  # fmt: skip
    clock = FakeClock(at("2026-10-26T17:00:00"))
    transport = ScriptedTransport()
    raw = tmp_path_factory.mktemp("raw")

    def collect(when, acme=None, acme2=None):
        clock.now = at(when)
        tokens = []
        for token, response in (("acme", acme), ("acme2", acme2)):
            if response is not None:
                transport.queues[token] = [response]
                tokens.append(token)
        Collector(conn, httpx.Client(transport=transport), raw, clock=clock,
                  sleep=lambda s: None, interval=0).collect(tokens=tokens)  # fmt: skip

    collect("2026-10-26T17:00:00", ok([J1, J2, J4, J6, J7, J8]), ok([J5]))  # baselines
    run_extraction(conn, ByPosting(), ExtractionConfig(rpm=60, daily_limit=100),
                   lambda: at("2026-10-26T18:00:00"), lambda s: None)  # fmt: skip
    collect("2026-10-27T17:00:00", ok([J1, J2, J3, J4, J6, J7]))  # J3 new; J8 missing (1)
    collect("2026-10-28T17:00:00", ok([J1, J2, J3, J4, J6, J7]))  # J8 missing (2)
    collect("2026-10-28T18:00:00", ok([J1, J2, J3, J6, J7]))  # J8 (3): inactive; J4 (1)
    collect("2026-10-28T19:00:00", acme2=ok([J5]))  # J5 is now the newest-seen copy
    j7 = conn.execute("SELECT posting_id FROM source_postings WHERE source_posting_id = '7'")
    close_posting(conn, j7.fetchone()[0], "employer page says filled",
                  "https://example.test/7", "This position has been filled.",
                  at("2026-10-28T19:30:00"))  # fmt: skip

    analytics = Analytics(conn, schema)
    analytics.build("--select", "+srv_postings", "+srv_boards")
    ids = dict(conn.execute("SELECT source_posting_id, posting_id FROM source_postings"))

    class World:
        pass

    w = World()
    w.conn, w.schema, w.source = conn, analytics.schema("serving"), schema
    w.ids = {int(k): v for k, v in ids.items()}
    w.choices = load_choices(conn, w.schema)
    yield w
    analytics.drop(db_settings)
    conn.close()
    admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.execute(f'DROP SCHEMA IF EXISTS "{schema}_private" CASCADE')
    admin.close()
