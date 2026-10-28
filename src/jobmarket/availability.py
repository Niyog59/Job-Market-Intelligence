"""Availability verification: is each source posting still listed?

A posting's state changes only on trustworthy evidence:

  observed              found in a valid source response
  possibly_unavailable  missing from one or two qualifying scans (still shown, with a warning)
  assumed_inactive      missing from a third qualifying scan (hidden from the active feed)
  closed                official evidence that applications are closed (hidden at once)

A qualifying absence needs a complete scan of the same board under the same scope (how the
board was read) as the board's comparison reference, with no active board hold and no mass
disappearance. Failed and partial scans pause counting; they never advance or reset it. Each
scan is judged once (absence_checks has one row per scan), so a replay can never count again,
and replays do not judge scans at all.

Mass-disappearance protection compares the IDs listed by the reference scan with the IDs in
the current complete scan. If every reference job is missing, or at least HOLD_MIN_SHARE of
them and at least HOLD_MIN_MISSING jobs are, the board is put on hold: no absence counting
until a person releases the hold with a reason and evidence. Positive observations (new jobs,
reappearances) and explicit closures still apply during a hold. A repeated suspicious result
keeps the hold; it is never released automatically, and held scans are never counted later.

A missing posting or a request error is never treated as proof that a job closed or that
anyone was hired.
"""

import os
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

STATES = ("observed", "possibly_unavailable", "assumed_inactive", "closed")
ACTIVE_STATES = ("observed", "possibly_unavailable")
ABSENCES_TO_INACTIVE = 3

HOLD_ALL_MISSING_ENV = "JOBMARKET_HOLD_ALL_MISSING"
HOLD_MIN_SHARE_ENV = "JOBMARKET_HOLD_MIN_SHARE"
HOLD_MIN_MISSING_ENV = "JOBMARKET_HOLD_MIN_MISSING"

RELEASE_REASONS = ("collection_issue_resolved", "source_removals_verified")


@dataclass(frozen=True)
class HoldThresholds:
    """When a complete scan is treated as a mass disappearance (values from the brief)."""

    all_missing: bool = True  # every reference job missing, whatever the board size
    min_share: float = 0.5  # and/or at least this share of reference jobs missing ...
    min_missing: int = 10  # ... and at least this many

    @classmethod
    def from_env(cls) -> "HoldThresholds":
        return cls(
            all_missing=os.environ.get(HOLD_ALL_MISSING_ENV, "true").lower() != "false",
            min_share=float(os.environ.get(HOLD_MIN_SHARE_ENV, cls.min_share)),
            min_missing=int(os.environ.get(HOLD_MIN_MISSING_ENV, cls.min_missing)),
        )

    def trigger(self, reference_count: int, missing_count: int) -> str | None:
        if reference_count == 0 or missing_count == 0:
            return None
        if self.all_missing and missing_count == reference_count:
            return "all_missing"
        if missing_count / reference_count >= self.min_share and missing_count >= self.min_missing:
            return "share_missing"
        return None

    def as_dict(self) -> dict:
        return {
            "all_missing": self.all_missing,
            "min_share": self.min_share,
            "min_missing": self.min_missing,
        }


@dataclass
class ScanJudgement:
    outcome: str
    reference_count: int | None = None
    present_count: int | None = None
    missing_count: int | None = None
    hold_id: int | None = None
    newly_possibly_unavailable: int = 0
    newly_inactive: int = 0
    reappeared: int = 0
    detail: str | None = None


def record_event(
    conn: psycopg.Connection,
    posting_id: int,
    from_state: str | None,
    to_state: str,
    absences: int,
    reason: str,
    occurred_at: datetime,
    recorded_at: datetime,
    scan_id: int | None = None,
    evidence: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO posting_availability_events (posting_id, from_state, to_state, "
        "qualifying_absences, reason, board_scan_id, evidence, occurred_at, recorded_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            posting_id,
            from_state,
            to_state,
            absences,
            reason,
            scan_id,
            Jsonb(evidence) if evidence else None,
            occurred_at,
            recorded_at,
        ),
    )
    conn.execute(
        "UPDATE source_postings SET availability_state = %s, qualifying_absences = %s, "
        "availability_changed_at = %s WHERE posting_id = %s",
        (to_state, absences, occurred_at, posting_id),
    )


def _apply_reappearances(
    conn: psycopg.Connection,
    present: list[int],
    scan_id: int,
    collected_at: datetime,
    now: datetime,
) -> int:
    """Postings listed again return to observed; their history and first-seen time stay.
    A closed posting listed again stays closed (official closure evidence outranks a
    listing) and the reappearance is recorded for review."""
    if not present:
        return 0
    rows = conn.execute(
        "SELECT posting_id, availability_state, qualifying_absences FROM source_postings "
        "WHERE posting_id = ANY(%s) AND (availability_state <> 'observed' "
        "OR qualifying_absences > 0) ORDER BY posting_id",
        (present,),
    ).fetchall()
    restored = 0
    for posting_id, state, absences in rows:
        if state == "closed":
            conn.execute(
                "INSERT INTO posting_availability_events (posting_id, from_state, to_state, "
                "qualifying_absences, reason, board_scan_id, occurred_at, recorded_at) "
                "VALUES (%s, 'closed', 'closed', %s, %s, %s, %s, %s)",
                (
                    posting_id,
                    absences,
                    "listed again after an official closure; kept closed, needs review",
                    scan_id,
                    collected_at,
                    now,
                ),
            )
            continue
        record_event(
            conn, posting_id, state, "observed", 0,
            f"listed again after {absences} qualifying absence(s)", collected_at, now, scan_id,
        )  # fmt: skip
        restored += 1
    return restored


def judge_scan(
    conn: psycopg.Connection,
    board_id: int,
    scan_id: int,
    outcome: str,
    present: list[int],
    collected_at: datetime | None,
    scope_key: str,
    now: datetime,
    thresholds: HoldThresholds | None = None,
) -> ScanJudgement:
    """Judge one collected scan for availability. Runs inside the caller's transaction, so
    the judgement commits with the scan's data or not at all.

    outcome is the scan's validation result; present lists every posting the scan contained
    (retained or not)."""
    thresholds = thresholds or HoldThresholds.from_env()
    board = conn.execute(
        "SELECT absence_reference_scan_id, absence_scope_key FROM source_boards "
        "WHERE board_id = %s FOR UPDATE",
        (board_id,),
    ).fetchone()
    reference_id, reference_scope = board
    hold = conn.execute(
        "SELECT hold_id FROM board_holds WHERE board_id = %s AND status = 'active'",
        (board_id,),
    ).fetchone()
    judgement = ScanJudgement("not_complete", present_count=len(present))
    if outcome != "failed":
        judgement.reappeared = _apply_reappearances(conn, present, scan_id, collected_at, now)

    if outcome != "complete":
        judgement.detail = f"{outcome} scan: absences paused"
        judgement.present_count = len(present) if outcome != "failed" else None
        return _save(conn, board_id, scan_id, collected_at, scope_key, reference_id, judgement, now)

    conn.execute(
        "INSERT INTO board_scan_listings (board_scan_id, posting_id) "
        "SELECT %s, unnest(%s::bigint[]) ON CONFLICT DO NOTHING",
        (scan_id, present),
    )

    if reference_id is None or reference_scope != scope_key:
        judgement.outcome = "baseline" if reference_id is None else "scope_changed"
        judgement.detail = (
            "first comparable scan: becomes the reference; nothing counted"
            if reference_id is None
            else f"scope changed from {reference_scope!r} to {scope_key!r}: becomes the new "
            "reference; nothing counted"
        )
        _set_reference(conn, board_id, scan_id, scope_key)
        return _save(conn, board_id, scan_id, collected_at, scope_key, reference_id, judgement, now)

    reference = {
        r[0]
        for r in conn.execute(
            "SELECT posting_id FROM board_scan_listings WHERE board_scan_id = %s",
            (reference_id,),
        )
    }
    missing = reference - set(present)
    judgement.reference_count = len(reference)
    judgement.missing_count = len(missing)

    if hold is not None:
        judgement.outcome = "held"
        judgement.hold_id = hold[0]
        judgement.detail = "board on hold: absences not counted until the hold is released"
        return _save(conn, board_id, scan_id, collected_at, scope_key, reference_id, judgement, now)

    trigger = thresholds.trigger(len(reference), len(missing))
    if trigger is not None:
        judgement.outcome = "mass_disappearance"
        judgement.hold_id = conn.execute(
            "INSERT INTO board_holds (board_id, opened_at, opened_by_scan_id, reference_scan_id, "
            "trigger, reference_count, missing_count, thresholds) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING hold_id",
            (
                board_id,
                now,
                scan_id,
                reference_id,
                trigger,
                len(reference),
                len(missing),
                Jsonb(thresholds.as_dict()),
            ),
        ).fetchone()[0]
        judgement.detail = (
            f"{len(missing)} of {len(reference)} reference jobs missing ({trigger}): board "
            "put on hold; absences not counted"
        )
        return _save(conn, board_id, scan_id, collected_at, scope_key, reference_id, judgement, now)

    judgement.outcome = "qualifying"
    _count_absences(conn, board_id, scan_id, present, collected_at, now, judgement)
    _set_reference(conn, board_id, scan_id, scope_key)
    return _save(conn, board_id, scan_id, collected_at, scope_key, reference_id, judgement, now)


def _count_absences(conn, board_id, scan_id, present, collected_at, now, judgement) -> None:
    rows = conn.execute(
        "SELECT posting_id, availability_state, qualifying_absences FROM source_postings "
        "WHERE board_id = %s AND availability_state = ANY(%s) AND NOT (posting_id = ANY(%s)) "
        "ORDER BY posting_id FOR UPDATE",
        (board_id, list(ACTIVE_STATES), present),
    ).fetchall()
    for posting_id, state, absences in rows:
        absences += 1
        new_state = (
            "assumed_inactive" if absences >= ABSENCES_TO_INACTIVE else "possibly_unavailable"
        )
        evidence = {"qualifying_absences": absences, "board_scan_id": scan_id}
        if new_state != state:
            record_event(
                conn, posting_id, state, new_state, absences,
                f"missing from qualifying scan {absences} of {ABSENCES_TO_INACTIVE}",
                collected_at, now, scan_id, evidence,
            )  # fmt: skip
            if new_state == "assumed_inactive":
                judgement.newly_inactive += 1
            else:
                judgement.newly_possibly_unavailable += 1
        else:
            # Still possibly unavailable (second absence): the count moves, the state does not.
            record_event(
                conn, posting_id, state, state, absences,
                f"missing from qualifying scan {absences} of {ABSENCES_TO_INACTIVE}",
                collected_at, now, scan_id, evidence,
            )  # fmt: skip


def _set_reference(conn, board_id: int, scan_id: int, scope_key: str) -> None:
    conn.execute(
        "UPDATE source_boards SET absence_reference_scan_id = %s, absence_scope_key = %s "
        "WHERE board_id = %s",
        (scan_id, scope_key, board_id),
    )


def _save(conn, board_id, scan_id, collected_at, scope_key, reference_id, j, now):
    conn.execute(
        "INSERT INTO absence_checks (board_scan_id, board_id, collected_at, evaluated_at, "
        "scope_key, outcome, reference_scan_id, reference_count, present_count, missing_count, "
        "hold_id, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            scan_id,
            board_id,
            collected_at,
            now,
            scope_key,
            j.outcome,
            reference_id,
            j.reference_count,
            j.present_count,
            j.missing_count,
            j.hold_id,
            j.detail,
        ),
    )
    return j


# --- Manual review ---------------------------------------------------------------------


class AvailabilityError(ValueError):
    """A review command was given something it cannot act on."""


def close_posting(
    conn: psycopg.Connection,
    posting_id: int,
    reason: str,
    evidence_url: str,
    evidence_text: str,
    now: datetime,
    source: str = "manual review",
    page_check_id: int | None = None,
) -> str:
    """Mark a posting closed on official evidence. Works during a board hold too. Returns
    the previous state."""
    if not reason.strip() or not evidence_url.strip() or not evidence_text.strip():
        raise AvailabilityError("closing needs a reason, an evidence URL, and evidence text")
    with conn.transaction():
        row = conn.execute(
            "SELECT availability_state, qualifying_absences FROM source_postings "
            "WHERE posting_id = %s FOR UPDATE",
            (posting_id,),
        ).fetchone()
        if row is None:
            raise AvailabilityError(f"there is no posting {posting_id}")
        state, absences = row
        if state == "closed":
            return state
        record_event(
            conn, posting_id, state, "closed", absences, reason, now, now,
            evidence={
                "source": source,
                "url": evidence_url,
                "text": evidence_text,
                "page_check_id": page_check_id,
            },
        )  # fmt: skip
    return state


def release_hold(
    conn: psycopg.Connection, hold_id: int, reason: str, evidence: str, now: datetime
) -> int | None:
    """Release a board hold after review. Returns the board's new reference scan id.

    collection_issue_resolved: the collection problem was fixed; the board keeps its
        pre-hold reference, so the next complete scan is compared with the last trusted list.
    source_removals_verified: the jobs really were removed at the source; the latest
        complete scan becomes the reference, so the removed jobs are not compared again as a
        mass disappearance, and they start counting absences from the next qualifying scan.

    Either way absence counting restarts from the next qualifying scan: held scans are
    never counted afterwards."""
    if reason not in RELEASE_REASONS:
        raise AvailabilityError(f"reason must be one of {RELEASE_REASONS}")
    if not evidence.strip():
        raise AvailabilityError("releasing a hold needs evidence text")
    with conn.transaction():
        row = conn.execute(
            "SELECT board_id, status FROM board_holds WHERE hold_id = %s FOR UPDATE",
            (hold_id,),
        ).fetchone()
        if row is None:
            raise AvailabilityError(f"there is no hold {hold_id}")
        board_id, status = row
        if status != "active":
            raise AvailabilityError(f"hold {hold_id} is already released")
        new_reference = None
        if reason == "source_removals_verified":
            latest = conn.execute(
                "SELECT max(c.board_scan_id) FROM absence_checks c "
                "WHERE c.board_id = %s AND c.outcome IN ('held', 'mass_disappearance') "
                "AND c.hold_id = %s",
                (board_id, hold_id),
            ).fetchone()[0]
            new_reference = latest
            scope = conn.execute(
                "SELECT scope_key FROM absence_checks WHERE board_scan_id = %s", (latest,)
            ).fetchone()[0]
            _set_reference(conn, board_id, latest, scope)
        conn.execute(
            "UPDATE board_holds SET status = 'released', released_at = %s, release_reason = %s, "
            "release_evidence = %s, new_reference_scan_id = %s WHERE hold_id = %s",
            (now, reason, evidence, new_reference, hold_id),
        )
    return new_reference
