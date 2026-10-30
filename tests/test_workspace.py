"""The private workspace (jobmarket.workspace): profile validation and storage, saved jobs,
application tracking, and visits. Runs on the synthetic history in search_world; every
profile value is fictional."""

from datetime import UTC, date, datetime, timedelta

import pytest
from werkzeug.datastructures import MultiDict

pytest.importorskip("argon2", reason="owner accounts need argon2-cffi")

from jobmarket import owner, workspace  # noqa: E402

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)


@pytest.fixture
def ws(world):
    private = f"{world.source}_private"
    world.conn.execute(f'TRUNCATE "{private}".accounts, "{private}".login_attempts CASCADE')
    owner.create_owner(world.conn, private, "owner", "fictional pass", NOW)

    class W:
        conn, schema, ids = world.conn, private, world.ids

    yield W()
    world.conn.execute(f'TRUNCATE "{private}".accounts, "{private}".login_attempts CASCADE')


def form(**fields):
    data = MultiDict()
    for key, value in fields.items():
        key = key.replace("__", "-")
        for v in value if isinstance(value, list) else [value]:
            data.add(key, v)
    return data


VALID = dict(
    skills__count="3", skills__0__name="Python", skills__1__name="postgres",
    skills__1__link="https://example.test/project", skills__2__name="",
    experiences__count="2",
    experiences__0__kind="employment", experiences__0__title="Data intern turned analyst",
    experiences__0__start="2024-06", experiences__0__end="2025-08",
    experiences__1__kind="project", experiences__1__title="Pipeline project",
    experiences__1__start="2025-01", experiences__1__current="on",
    education__count="1", education__0__level="bachelor", education__0__status="expected",
    education__0__graduation="2027-05", education__0__field="Computer Science",
    role=["data_engineering", "backend"], place=["dfw"], confirm="on",
)  # fmt: skip


def test_a_valid_profile_is_saved_exactly_as_entered(ws):
    parsed = workspace.parse_profile_form(form(**VALID), NOW)
    assert parsed.ok, parsed.errors
    assert [s.name for s in parsed.profile.skills] == ["Python", "postgres"]  # blank row ignored
    assert workspace.save_profile(ws.conn, ws.schema, 1, parsed.profile, NOW) == 1
    loaded = workspace.load_profile(ws.conn, ws.schema, 1)
    assert loaded.version == 1 and loaded.preferred_roles == ["data_engineering", "backend"]
    assert loaded.experiences[1].current and loaded.experiences[1].end is None
    assert loaded.education[0].graduation == date(2027, 5, 1)
    assert workspace.save_profile(ws.conn, ws.schema, 1, parsed.profile, NOW) == 2


def test_problems_are_reported_by_field_and_nothing_is_guessed(ws):
    bad = dict(VALID, skills__2__name="PostgreSQL", skills__0__link="http://plain.test",
               experiences__0__end="2023-01", experiences__1__start="2031-01",
               education__0__graduation="", confirm="")  # fmt: skip
    parsed = workspace.parse_profile_form(form(**bad), NOW)
    assert set(parsed.errors) == {
        "skills-2-name",
        "skills-0-link",
        "experiences-0-end",
        "experiences-1-start",
        "education-0-graduation",
        "confirm",
    }
    assert "already listed" in parsed.errors["skills-2-name"]  # postgres = PostgreSQL
    assert parsed.raw["skills"][0]["link"] == "http://plain.test"  # kept for redisplay
    assert workspace.load_profile(ws.conn, ws.schema, 1).version == 0  # nothing saved


def test_saving_is_idempotent_and_one_per_confident_group(ws):
    j1, j5, j2 = ws.ids[1], ws.ids[5], ws.ids[2]
    first = workspace.save_job(ws.conn, ws.schema, 1, j1, [j1, j5], NOW)
    again = workspace.save_job(ws.conn, ws.schema, 1, j1, [j1, j5], NOW + timedelta(hours=1))
    other_copy = workspace.save_job(ws.conn, ws.schema, 1, j5, [j1, j5], NOW)
    assert first == again == other_copy
    workspace.save_job(ws.conn, ws.schema, 1, j2, [j2], NOW)
    assert {s.posting_id for s in workspace.list_saved(ws.conn, ws.schema, 1)} == {j1, j2}


def test_unsaving_keeps_the_note_and_saving_again_restores_it(ws):
    j2 = ws.ids[2]
    saved = workspace.save_job(ws.conn, ws.schema, 1, j2, [j2], NOW)
    assert workspace.set_saved_note(ws.conn, ws.schema, 1, saved.saved_id, "Ask about the team")
    assert workspace.unsave_job(ws.conn, ws.schema, 1, [j2], NOW) == 1
    assert workspace.list_saved(ws.conn, ws.schema, 1) == []
    back = workspace.save_job(ws.conn, ws.schema, 1, j2, [j2], NOW + timedelta(days=1))
    assert back.saved_id == saved.saved_id and back.note == "Ask about the team"


def test_entries_made_before_a_group_formed_are_not_merged(ws):
    j1, j5 = ws.ids[1], ws.ids[5]
    # As if the two postings were saved before the duplicate group existed.
    workspace.save_job(ws.conn, ws.schema, 1, j1, [j1], NOW)
    workspace.save_job(ws.conn, ws.schema, 1, j5, [j5], NOW)
    assert len(workspace.saved_among(ws.conn, ws.schema, 1, [j1, j5])) == 2


def test_guessed_ids_find_nothing(ws):
    assert not workspace.set_saved_note(ws.conn, ws.schema, 1, 987654, "x")
    j2 = ws.ids[2]
    saved = workspace.save_job(ws.conn, ws.schema, 1, j2, [j2], NOW)
    assert not workspace.set_saved_note(ws.conn, ws.schema, 2, saved.saved_id, "other account")
    assert workspace.saved_among(ws.conn, ws.schema, 2, [j2]) == []


def test_status_history_allows_real_transitions_and_corrections(ws):
    j2 = ws.ids[2]

    def record(status, day, **kw):
        return workspace.record_status(ws.conn, ws.schema, 1, j2, [j2], status,
                                       date(2026, 10, day), "", NOW, **kw)  # fmt: skip

    record("applied", 20)
    record("rejected", 24)
    app = record("interviewing", 27)  # a "rejection" was followed by an interview
    assert [e.status for e in app.events] == ["applied", "rejected", "interviewing"]
    assert app.current.status == "interviewing"
    wrong = app.events[-1]
    fixed = record("offer", 27, corrects_event_id=wrong.event_id)
    statuses = {e.event_id: (e.status, e.corrected_by) for e in fixed.events}
    assert statuses[wrong.event_id][1] is not None  # kept, marked corrected
    assert fixed.current.status == "offer" and len(fixed.events) == 4
    with pytest.raises(workspace.TrackingError, match="already corrected"):
        record("withdrawn", 28, corrects_event_id=wrong.event_id)
    with pytest.raises(workspace.TrackingError, match="future"):
        record("applied", 31)
    with pytest.raises(workspace.TrackingError, match="does not belong"):
        record("applied", 28, corrects_event_id=999999)


def test_a_group_shares_one_application_and_closure_changes_nothing(ws):
    j1, j5, j7 = ws.ids[1], ws.ids[5], ws.ids[7]
    workspace.record_status(ws.conn, ws.schema, 1, j1, [j1, j5], "applied", date(2026, 10, 20),
                            "", NOW)  # fmt: skip
    via_copy = workspace.application_among(ws.conn, ws.schema, 1, [j5, j1])
    assert via_copy.posting_id == j1 and via_copy.current.status == "applied"
    # J7 is closed at the source; its tracked status is the owner's alone.
    workspace.record_status(ws.conn, ws.schema, 1, j7, [j7], "interviewing", date(2026, 10, 28),
                            "Panel on Monday", NOW)  # fmt: skip
    closed = workspace.application_among(ws.conn, ws.schema, 1, [j7])
    assert closed.current.status == "interviewing" and closed.current.note == "Panel on Monday"


def test_visits_keep_their_cutoff_and_start_at_sign_in_or_after_an_hour(ws):
    first = workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW)
    assert first.cutoff is None  # the first visit is a baseline
    same = workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW + timedelta(minutes=50))
    assert same.visit_id == first.visit_id
    # 50 minutes after the last activity: still the same visit, same (absent) cutoff.
    later = workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW + timedelta(minutes=100))
    assert later.visit_id == first.visit_id and later.cutoff is None


def test_a_new_visit_uses_the_previous_visits_last_activity(ws):
    workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW)
    workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW + timedelta(minutes=30))
    idle = workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW + timedelta(hours=2))
    assert idle.cutoff == NOW + timedelta(minutes=30)
    stable = workspace.touch_visit(ws.conn, ws.schema, 1, 10, NOW + timedelta(hours=2, minutes=5))
    assert stable.visit_id == idle.visit_id and stable.cutoff == idle.cutoff
    signed_in_again = workspace.touch_visit(ws.conn, ws.schema, 1, 11,
                                            NOW + timedelta(hours=2, minutes=10))  # fmt: skip
    assert signed_in_again.cutoff == NOW + timedelta(hours=2, minutes=5)
