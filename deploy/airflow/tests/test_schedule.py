"""The Airflow schedules in deploy/airflow/dags: times across daylight saving changes, one
catch-up run after downtime, and the evening/noon split. Runs only where Airflow is
installed (the CI "airflow" job); skipped elsewhere."""

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("airflow", reason="Airflow is not installed")

import airflow.timetables.trigger as trigger  # noqa: E402
import pendulum  # noqa: E402
from airflow.timetables.base import DataInterval, TimeRestriction  # noqa: E402

DAGS = Path(__file__).resolve().parents[1] / "dags" / "jobmarket.py"
CHICAGO = "America/Chicago"


@pytest.fixture(scope="module")
def module():
    spec = importlib.util.spec_from_file_location("jobmarket_dags", DAGS)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def local(*args):
    return pendulum.datetime(*args, tz=CHICAGO)


def next_run(timetable, monkeypatch, now, last_run=None, earliest=None):
    """The next run the scheduler would create at `now`, given the last scheduled run."""
    monkeypatch.setattr(trigger, "utcnow", lambda: now.in_tz("UTC"))
    last = DataInterval(last_run, last_run) if last_run is not None else None
    info = timetable.next_dagrun_info(
        last_automated_data_interval=last,
        restriction=TimeRestriction(earliest=earliest, latest=None, catchup=False),
    )
    return info.run_after.in_tz(CHICAGO)


def test_noon_and_nine_pm_local_time_across_daylight_saving(module, monkeypatch):
    timetable = module.collection.timetable
    # 2026-03-08: clocks spring forward at 2 AM. 2026-11-01: clocks fall back at 2 AM.
    for day, utc_offset in ((dt.date(2026, 3, 7), -6), (dt.date(2026, 3, 8), -5),
                            (dt.date(2026, 10, 31), -5), (dt.date(2026, 11, 1), -6)):  # fmt: skip
        morning = local(day.year, day.month, day.day, 6)
        yesterday_evening = local(day.year, day.month, day.day, 21).subtract(days=1)
        noon = next_run(timetable, monkeypatch, morning, last_run=yesterday_evening)
        assert (noon.hour, noon.minute) == (12, 0)
        assert noon.utcoffset() == dt.timedelta(hours=utc_offset)
        evening = next_run(timetable, monkeypatch, noon.add(minutes=5), last_run=noon)
        assert (evening.date(), evening.hour) == (day, 21)


def test_one_catch_up_run_after_downtime(module, monkeypatch):
    timetable = module.collection.timetable
    last = local(2026, 10, 30, 21)  # then the server was down for three days
    now = local(2026, 11, 2, 15, 7)
    catch_up = next_run(timetable, monkeypatch, now, last_run=last)
    assert catch_up == local(2026, 11, 2, 12)  # only the latest missed slot, not five runs
    # After that one run, the schedule continues normally: no other missed slot comes back.
    assert next_run(timetable, monkeypatch, now.add(minutes=10), last_run=catch_up) == local(
        2026, 11, 2, 21
    )


def test_no_extra_run_when_nothing_was_missed(module, monkeypatch):
    timetable = module.collection.timetable
    last = local(2026, 11, 2, 12)
    assert next_run(timetable, monkeypatch, local(2026, 11, 2, 12, 30), last) == local(
        2026, 11, 2, 21
    )


def test_first_start_runs_once_for_the_latest_slot(module, monkeypatch):
    timetable = module.collection.timetable
    first = next_run(timetable, monkeypatch, local(2026, 11, 20, 8), earliest=module.START)
    assert first == local(2026, 11, 19, 21)


def test_retry_check_every_15_minutes_and_ai_hourly_at_half_past(module, monkeypatch):
    retry = module.retries.timetable
    last = local(2026, 11, 2, 9, 15)
    assert next_run(retry, monkeypatch, last.add(minutes=1), last) == local(2026, 11, 2, 9, 30)
    # Down for a day: one retry check, not 96.
    assert next_run(retry, monkeypatch, local(2026, 11, 3, 9, 7), last) == local(2026, 11, 3, 9)
    ai = module.ai.timetable
    last = local(2026, 11, 2, 9, 30)
    assert next_run(ai, monkeypatch, last.add(minutes=1), last) == local(2026, 11, 2, 10, 30)


class Run:
    def __init__(self, run_after, logical_date=None):
        self.run_after, self.logical_date = run_after, logical_date


def test_evening_runs_are_recognized_in_local_time(module):
    assert module.is_evening(Run(local(2026, 11, 2, 21)))
    assert not module.is_evening(Run(local(2026, 11, 2, 12)))
    # 21:00 Chicago is 02:00 or 03:00 UTC the next day; the local hour decides.
    assert module.is_evening(Run(pendulum.datetime(2026, 7, 2, 2, tz="UTC")))
    assert module.is_evening(Run(pendulum.datetime(2026, 12, 2, 3, tz="UTC")))


def test_the_scheduled_slot_decides_not_the_start_time(module):
    # A scheduled or tested run of the 21:00 slot that starts after midnight.
    assert module.is_evening(Run(local(2026, 11, 3, 0, 37), logical_date=local(2026, 11, 2, 21)))
    assert not module.is_evening(Run(local(2026, 11, 2, 18), logical_date=local(2026, 11, 2, 12)))
    # A manual run without a logical date: the time it was started.
    assert module.is_evening(Run(local(2026, 11, 2, 19)))


def test_evening_only_commands_skip_at_noon(module):
    from jinja2 import Template

    command = module.evening_only("jobmarket backup create --upload")
    evening = Template(command).render(is_evening=module.is_evening,
                                       dag_run=Run(local(2026, 11, 2, 21)))  # fmt: skip
    noon = Template(command).render(is_evening=module.is_evening,
                                    dag_run=Run(local(2026, 11, 2, 12)))  # fmt: skip
    assert evening == "jobmarket backup create --upload"
    assert noon.endswith("exit 99")
