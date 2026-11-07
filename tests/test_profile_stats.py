"""The public numbers file (jobmarket.profile_stats), its route, and when publication writes
it. Built from the reporting tables over the synthetic history in search_world."""

import contextlib
import json
import stat

import pytest

from jobmarket import profile_stats, reports
from jobmarket.profile_stats import FIELDS, ProfileStatsError, validate, write

GOOD = {
    "boards_enabled": 63, "open_jobs": 148, "employers_with_open_jobs": 29,
    "kept_jobs_total": 259, "extracted_jobs": 166, "extracted_total": 167,
    "junior_pct": 10, "senior_pct": 84, "genai_pct": 27, "last_published_at": "2026-11-05",
}  # fmt: skip


@pytest.mark.parametrize(
    "change",
    [
        {"kept_jobs_total": 100},  # below open_jobs
        {"extracted_jobs": 168},  # above extracted_total
        {"junior_pct": 101},
        {"genai_pct": -1},
        {"open_jobs": 1.5},
        {"boards_enabled": 0},
        {"employers_with_open_jobs": 149},
    ],
)
def test_impossible_numbers_are_refused(change):
    with pytest.raises(ProfileStatsError):
        validate({**GOOD, **change})


def test_a_missing_field_is_refused():
    with pytest.raises(ProfileStatsError, match="genai_pct"):
        validate({k: v for k, v in GOOD.items() if k != "genai_pct"})


def test_the_file_has_exactly_the_ten_fields_and_is_readable_by_others(tmp_path):
    path = write(GOOD, tmp_path / "public" / "profile-stats.json")
    assert list(json.loads(path.read_text())) == list(FIELDS)
    assert json.loads(path.read_text()) == GOOD
    assert path.stat().st_mode & stat.S_IROTH
    assert [p.name for p in path.parent.iterdir()] == ["profile-stats.json"]  # no temp left


def test_a_refused_write_leaves_the_previous_file_untouched(tmp_path):
    path = write(GOOD, tmp_path / "profile-stats.json")
    before = path.read_bytes()
    with pytest.raises(ProfileStatsError):
        write({**GOOD, "kept_jobs_total": 1}, path)
    assert path.read_bytes() == before


# --- built from the published reports ---------------------------------------------------


@pytest.fixture(scope="module")
def reporting(world):
    world.analytics.build("--select", "+path:models/reporting", "+srv_postings", "+srv_boards")
    return world.analytics.schema("reporting")


@pytest.mark.db
def test_the_numbers_match_the_reports_page(world, reporting):
    stats = profile_stats.build(world.conn, reporting)
    page = reports.load_reports(world.conn, "kept", reporting)
    assert stats["extracted_jobs"] == page.coverage["extracted"]
    assert stats["extracted_total"] == page.coverage["listed_postings"]
    assert stats["kept_jobs_total"] == sum(r["opportunities"] for r in page.inventory)
    assert stats["boards_enabled"] == len(page.boards)
    jobs = world.conn.execute(f'SELECT count(*), count(DISTINCT employer_name) FROM '
                              f'"{reporting}".rpt_jobs_current').fetchone()  # fmt: skip
    assert (stats["open_jobs"], stats["employers_with_open_jobs"]) == jobs
    levels = {r["value"]: r["postings"] for r in page.experience["seniority"]}
    junior = sum(levels.get(v, 0) for v in profile_stats.JUNIOR)
    stated = junior + sum(levels.get(v, 0) for v in profile_stats.SENIOR | profile_stats.MID)
    assert stats["junior_pct"] == profile_stats._percent(junior, stated)
    assert stats["last_published_at"] == str(page.build.built_at.astimezone(
        profile_stats.ZONE).date())  # fmt: skip


@pytest.mark.db
def test_an_unrecognized_seniority_value_stops_the_build(world, reporting):
    with contextlib.suppress(RuntimeError), world.conn.transaction():
        world.conn.execute(f'UPDATE "{reporting}".rpt_experience_requirements SET value = '
                           "'astronaut' WHERE measure = 'seniority'")  # fmt: skip
        with pytest.raises(ProfileStatsError, match="astronaut"):
            profile_stats.build(world.conn, reporting)
        raise RuntimeError("roll back the change")

