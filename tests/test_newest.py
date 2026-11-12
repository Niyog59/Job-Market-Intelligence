"""Newest jobs (jobmarket.search.newest and the /newest page), and the age rule shared
with job search, on a small history of one Greenhouse board:

  2026-11-01 12:00  first complete scan (initial imports):
                      B1 Data Engineer, Dallas    published Oct 30
                      B2 Data Analyst, Houston    published Sep 1 (older than OLD_AFTER)
  2026-11-03 12:00  N1 Backend Engineer, Dallas   published Nov 3
                    N2 Sales Manager, Dallas      not kept by the screening rules
                    N3 Data Engineer II, Dallas   published Aug 1: new to us, but old
  2026-11-03 18:00  N5 Analytics Engineer, Houston published Nov 3
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from extraction_fakes import ScriptedModel, answer, reply
from gh_fakes import FakeClock, ScriptedTransport, board, gh_job, ok, registry

from jobmarket import db
from jobmarket.collector import Collector
from jobmarket.extraction import ExtractionConfig, run_extraction
from jobmarket.migrate import apply_migrations
from jobmarket.search import (
    NEWEST_WINDOW,
    OLD_AFTER,
    parse_newest,
    parse_params,
    search,
)
from jobmarket.search import newest as newest_jobs
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

DUTIES = "<p>Build data pipelines in Python and SQL with the team.</p>"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def posting(n, title, city="Dallas", published="2026-11-03T09:00:00+00:00", content=DUTIES):
    return gh_job(n, title=title, location=f"{city}, TX", content=content,
                  first_published=published)  # fmt: skip


B1 = posting(1, "Data Engineer", published="2026-10-30T09:00:00+00:00")
B2 = posting(2, "Data Analyst", "Houston", published="2026-09-01T09:00:00+00:00")
N1 = posting(3, "Backend Engineer", content="<p>Build backend APIs in Go.</p>")
N2 = posting(4, "Sales Manager", content="<p>Sell our product to new customers.</p>")
N3 = posting(5, "Data Engineer II", published="2026-08-01T09:00:00+00:00")
N5 = posting(6, "Analytics Engineer", "Houston")
NOW = at("2026-11-03T19:00:00")


@pytest.fixture(scope="module")
def world(db_settings, tmp_path_factory):
    from analytics_harness import Analytics

    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = db.connect(db_settings, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    conn = db.connect(db_settings, autocommit=True, options=f"-c search_path={schema}")
    apply_migrations(conn)
    sync_registry(conn, registry(board("acme")), FakeClock()())
    clock = FakeClock(at("2026-11-01T12:00:00"))
    transport = ScriptedTransport()
    raw = tmp_path_factory.mktemp("raw")

    class World:
        pass

    w = World()

    def collect(when, response):
        clock.now = at(when)
        transport.queues["acme"] = [response]
        Collector(conn, httpx.Client(transport=transport), raw, clock=clock,
                  sleep=lambda s: None, interval=0).collect(tokens=["acme"])  # fmt: skip

    collect("2026-11-01T12:00:00", ok([B1, B2]))
    # The serving views are live views: built once, they show later collections directly.
    analytics = Analytics(conn, schema)
    analytics.build("--select", "+srv_postings", "+srv_boards")
    collect("2026-11-03T12:00:00", ok([B1, B2, N1, N2, N3]))
    collect("2026-11-03T18:00:00", ok([B1, B2, N1, N2, N3, N5]))

    w.conn, w.schema, w.source, w.collect = conn, analytics.schema("serving"), schema, collect
    w.ids = {int(k): v for k, v in conn.execute(
        "SELECT source_posting_id, posting_id FROM source_postings")}  # fmt: skip
    yield w
    analytics.drop(db_settings)
    conn.close()
    admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    admin.execute(f'DROP SCHEMA IF EXISTS "{schema}_private" CASCADE')
    admin.close()


def newest(w, now=NOW, **args):
    params, problems = parse_newest(args)
    return newest_jobs(w.conn, params, now, problems, schema=w.schema)


def numbers(result, w):
    back = {v: k for k, v in w.ids.items()}
    return [back[row.posting_id] for row in result.rows]


# Source posting ids of the listings above.
B1_, B2_, N1_, N2_, N3_, N5_ = 1, 2, 3, 4, 5, 6


def test_newest_lists_unextracted_jobs_newest_first_with_their_application_link(world):
    result = newest(world)
    assert numbers(result, world) == [N5_, N1_]
    rows = {r.posting_id: r for r in result.rows}
    n1 = rows[world.ids[N1_]]
    assert n1.extraction_status in ("pending", "not_queued")
    assert n1.apply_url == "https://job-boards.greenhouse.io/acme/jobs/3"
    assert n1.discovered_at < rows[world.ids[N5_]].discovered_at
    assert n1.cohort == "discovered" and n1.unknown_reasons == []


def test_initial_imports_and_old_jobs_are_hidden_but_counted_and_available(world):
    result = newest(world)
    assert (result.hidden_imports, result.hidden_old) == (1, 1)  # B1; N3. B2 is both.
    assert numbers(newest(world, imports="include"), world) == [N5_, N1_, B1_]
    assert numbers(newest(world, old="include"), world) == [N5_, N3_, N1_]
    both = newest(world, imports="include", old="include")
    assert set(numbers(both, world)) == {N5_, N3_, N1_, B1_, B2_}
    assert (both.hidden_imports, both.hidden_old) == (0, 0)


def test_the_window_is_by_first_discovery(world):
    later = NOW + NEWEST_WINDOW - timedelta(hours=6)  # window from 13:00: N1 out, N5 in
    assert numbers(newest(world, now=later), world) == [N5_]


def test_source_based_filters_narrow_and_all_listings_broadens(world):
    assert numbers(newest(world, role="backend"), world) == [N1_]
    assert numbers(newest(world, area="houston"), world) == [N5_]
    assert N2_ in numbers(newest(world, population="all"), world)  # not kept, still listed
    params, problems = parse_newest({"role": "astronaut", "area": "moon"})
    assert params.families == () and params.areas == () and len(problems) == 2


def test_search_hides_old_jobs_unless_asked(world):
    def run(**args):
        params, problems = parse_params({"level": "any", **args}, choices=_choices(world))
        return search(world.conn, params, NOW, problems, schema=world.schema)

    default = run()
    assert N3_ not in numbers(default, world) and B2_ not in numbers(default, world)
    assert default.hidden_old == 2
    assert {N3_, B2_} <= set(numbers(run(old="include"), world))
    # B2 was published Sep 1: older than OLD_AFTER on Nov 3, whatever its discovery date.
    assert NOW - at("2026-09-01T09:00:00") > OLD_AFTER


def _choices(w):
    from jobmarket.search import load_choices

    return load_choices(w.conn, w.schema)


def test_failed_scan_keeps_jobs_listed_and_extraction_enriches_the_same_row(world):
    # A failed scan proves nothing about absence: the jobs stay observed and listed.
    world.collect("2026-11-03T18:30:00", httpx.Response(500, json={"error": "down"}))
    latest = world.conn.execute("SELECT status FROM board_scans ORDER BY board_scan_id DESC "
                                "LIMIT 1").fetchone()[0]  # fmt: skip
    assert latest != "complete"
    state = world.conn.execute(
        "SELECT availability_state, qualifying_absences FROM source_postings "
        "WHERE posting_id = %s", (world.ids[N1_],)).fetchone()  # fmt: skip
    assert state == ("observed", 0)
    assert numbers(newest(world), world) == [N5_, N1_]

    clock = FakeClock(at("2026-11-03T18:40:00"))
    model = ScriptedModel(reply(answer()))
    run_extraction(world.conn, model, ExtractionConfig(rpm=600, daily_limit=100, new_reserve=0),
                   clock, lambda s: clock.advance(timedelta(seconds=s)))  # fmt: skip
    after = newest(world)
    assert numbers(after, world) == [N5_, N1_]  # same listings, same order, no duplicates
    assert {r.extraction_status for r in after.rows} == {"extracted"}
    assert after.total == 2


def test_newest_page_is_public_and_links_to_apply_and_back(world, db_settings):
    pytest.importorskip("flask", reason="the website needs Flask (pip install -e '.[web]')")
    from jobmarket.web import create_app
    from jobmarket.web.config import WebConfig

    config = WebConfig(db_settings, serving_schema=world.schema, source_schema=world.source,
                       clock=lambda: NOW)  # fmt: skip
    app = create_app(config)
    app.testing = True
    client = app.test_client()

    response = client.get("/newest?role=backend")
    html = " ".join(response.get_data(as_text=True).split())
    assert response.status_code == 200
    assert "Backend Engineer" in html and "Analytics Engineer" not in html
    assert 'href="https://job-boards.greenhouse.io/acme/jobs/3" target="_blank"' in html
    assert "Board checked" in html
    assert "1 initial import from newly added boards hidden" not in html  # B1 is not backend

    detail = f"/jobs/{world.ids[N1_]}?list=newest&from=role%3Dbackend"
    page = " ".join(client.get(detail).get_data(as_text=True).split())
    assert 'href="/newest?role=backend" data-back>Back to newest jobs' in page
    assert "Apply on company site" in page

    home = " ".join(client.get("/").get_data(as_text=True).split())
    assert 'href="/newest"' in home
