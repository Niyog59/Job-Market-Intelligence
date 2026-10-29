"""Write a validated board response into the database.

Identity. Each valid job is one source_postings row per (board, source job id). It is
created race-safely (INSERT ... ON CONFLICT DO NOTHING) and then locked with SELECT ... FOR
UPDATE for the rest of the transaction, so two transactions processing the same job run one
after the other. Jobs are handled in source id order, so transactions lock postings in the
same order and cannot deadlock.

Chronology. Collection time (observed_at: when the response was fetched) orders everything,
with board_scan_id breaking exact ties. Processing time is recorded separately and never
decides what is current, so an older saved response processed later adds history without
replacing newer state.

Observations and versions. Every valid posting, retained or not, gets one
posting_observations row per board scan; the UNIQUE (board_scan_id, posting_id) key makes
reprocessing the same response add nothing. A new posting_versions row is written only when
the content differs meaningfully (jobmarket.normalize) from its chronological neighbours: the
newest observation before it and, for an older response processed late, the oldest one
after it. Identical source content never counts as a change, whatever the rules say, so a
change to the normalization rules cannot masquerade as an employer edit.

Long descriptions. A description longer than MAX_STORED_DESCRIPTION_CHARS is not stored
(description_omitted = 'too_long'): the version keeps its fingerprints, title, location, and
links, so edits are still detected, but not the text or HTML.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from jobmarket import normalize
from jobmarket.classify import CLASSIFIER_VERSION, screen
from jobmarket.connectors import ParsedResponse, PostingRecord
from jobmarket.normalize import Comparison, SourceContent, compare_form

RECORDED_BY = ("collection", "replay")

# Descriptions longer than this are not stored; readers get the posting link (decision
# 2026-10-27). The longest description collected so far is about 15,800 characters.
MAX_STORED_DESCRIPTION_CHARS = 20_000
OMITTED_DESCRIPTION = "[description not stored: longer than the storage limit]"


class ContentOmitted(Exception):
    """A version's description was not stored, so it cannot be re-normalized."""


@dataclass
class StoreCounts:
    retained: int = 0
    excluded: int = 0
    needing_review: int = 0
    new_postings: int = 0
    new_versions: int = 0
    new_observations: int = 0
    changed: int = 0  # tracked postings whose content changed meaningfully
    unchanged: int = 0  # tracked postings seen again with the same meaningful content
    cosmetic_only: int = 0  # unchanged, though the exact source content differed
    already_recorded: int = 0  # this response was processed before; nothing added
    historical: int = 0  # older than the posting's current observation; kept as history
    omitted_too_long: int = 0  # new versions whose description was too long to store
    touched_postings: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class _Neighbour:
    observation_id: int
    version_id: int
    observed_at: datetime
    board_scan_id: int
    source_fingerprint: str
    normalization_version: str | None
    comparison_fingerprint: str | None
    version_content_hash: str
    description_omitted: str | None


def store_scan_results(
    conn: psycopg.Connection,
    board_id: int,
    board_scan_id: int,
    raw_response_id: int | Mapping[int, int],
    parsed: ParsedResponse,
    observed_at: datetime,
    processed_at: datetime,
    recorded_by: str = "collection",
) -> StoreCounts:
    """Store jobs and rejected records from one board scan.

    Runs inside the caller's transaction, so either everything from the scan is written or
    nothing is. raw_response_id is the stored response, or for a paginated scan a mapping
    from page number to stored response, so every observation and rejected record points
    at the page it came from. observed_at is the scan's collection time and processed_at the
    time of this processing; they differ when a saved response is replayed.
    """
    if recorded_by not in RECORDED_BY:
        raise ValueError(f"recorded_by must be one of {RECORDED_BY}")
    raw_ids = {1: raw_response_id} if isinstance(raw_response_id, int) else raw_response_id
    counts = StoreCounts()
    for job in sorted(parsed.jobs, key=lambda j: j.source_id):
        _store_job(
            conn, board_id, board_scan_id, raw_ids[job.page_no], job, observed_at, processed_at,
            recorded_by, counts,
        )  # fmt: skip

    for rejection in parsed.rejected:
        conn.execute(
            "INSERT INTO rejected_records (board_scan_id, raw_response_id, record_index, "
            "source_posting_id, reason, detail, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (raw_response_id, record_index) DO NOTHING",
            (
                board_scan_id,
                raw_ids[rejection.page_no],
                rejection.record_index,
                rejection.source_id,
                rejection.reason,
                Jsonb(rejection.detail) if rejection.detail else None,
                processed_at,
            ),
        )

    if recorded_by == "collection":
        if parsed.outcome == "complete":
            # Only a complete scan is evidence that a posting is missing.
            seen = [str(job.source_id) for job in parsed.jobs]
            conn.execute(
                "UPDATE source_postings SET complete_scan_absences = complete_scan_absences + 1, "
                "last_absent_scan_id = %s "
                "WHERE board_id = %s AND NOT (source_posting_id = ANY(%s))",
                (board_scan_id, board_id, seen),
            )
    else:
        recompute_absences(conn, counts.touched_postings)
    return counts


def recompute_absences(conn: psycopg.Connection, posting_ids: list[int]) -> None:
    """Set absence counts from scan history: the complete scans of the posting's board
    collected after the posting was last seen. Equal to the running count kept during
    collection, and still correct after an older response adds an observation."""
    if not posting_ids:
        return
    conn.execute(
        """
        UPDATE source_postings p
        SET complete_scan_absences = a.absences,
            last_absent_scan_id = coalesce(a.last_scan_id, p.last_absent_scan_id)
        FROM (
            SELECT p2.posting_id, count(r.board_scan_id) AS absences,
                   max(r.board_scan_id) AS last_scan_id
            FROM source_postings p2
            LEFT JOIN board_scans s ON s.board_id = p2.board_id AND s.status = 'complete'
            LEFT JOIN raw_responses r ON r.board_scan_id = s.board_scan_id
                 AND r.sequence_no = 1 AND r.fetched_at > p2.last_observed_at
            WHERE p2.posting_id = ANY(%s)
            GROUP BY p2.posting_id
        ) a
        WHERE a.posting_id = p.posting_id
        """,
        (posting_ids,),
    )


def _store_job(
    conn: psycopg.Connection,
    board_id: int,
    board_scan_id: int,
    raw_response_id: int,
    job: PostingRecord,
    observed_at: datetime,
    processed_at: datetime,
    recorded_by: str,
    counts: StoreCounts,
) -> None:
    source_id = str(job.source_id)
    key = (observed_at, board_scan_id)

    inserted = conn.execute(
        "INSERT INTO source_postings (board_id, source_posting_id, first_discovered_at, "
        "last_observed_at, identity_basis) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (board_id, source_posting_id) DO NOTHING RETURNING posting_id",
        (board_id, source_id, observed_at, observed_at, job.identity_basis),
    ).fetchone()
    if inserted:
        counts.new_postings += 1
        conn.execute(
            "INSERT INTO posting_availability_events (posting_id, from_state, to_state, "
            "qualifying_absences, reason, board_scan_id, occurred_at, recorded_at) "
            "VALUES (%s, NULL, 'observed', 0, 'first observed', %s, %s, %s)",
            (inserted[0], board_scan_id, observed_at, processed_at),
        )
        conn.execute(
            "UPDATE source_postings SET availability_changed_at = %s WHERE posting_id = %s",
            (observed_at, inserted[0]),
        )
    # The lock is taken before anything else is read, so every later statement in this
    # transaction sees the committed results of any transaction that held it before.
    posting_id = conn.execute(
        "SELECT posting_id FROM source_postings WHERE board_id = %s AND source_posting_id = %s "
        "FOR UPDATE",
        (board_id, source_id),
    ).fetchone()[0]
    counts.touched_postings.append(posting_id)
    if not inserted:
        conn.execute(
            "UPDATE source_postings SET "
            "first_discovered_at = LEAST(first_discovered_at, %(t)s), "
            "last_observed_at = GREATEST(last_observed_at, %(t)s), "
            "complete_scan_absences = CASE WHEN %(t)s >= last_observed_at THEN 0 "
            "ELSE complete_scan_absences END, "
            # An id seen in the source record confirms an identity first taken from a URL.
            "identity_basis = CASE WHEN %(b)s = 'source_id' THEN 'source_id' "
            "ELSE identity_basis END "
            "WHERE posting_id = %(p)s",
            {"t": observed_at, "p": posting_id, "b": job.identity_basis},
        )

    current_key = conn.execute(
        "SELECT co.observed_at, co.board_scan_id FROM source_postings p "
        "JOIN posting_observations co ON co.observation_id = p.current_observation_id "
        "WHERE p.posting_id = %s",
        (posting_id,),
    ).fetchone()

    source_fingerprint = job.content_hash()
    retained, snapshot = _screen(
        conn, posting_id, job, source_fingerprint, key, processed_at, counts
    )
    if conn.execute(
        "SELECT 1 FROM posting_observations WHERE board_scan_id = %s AND posting_id = %s",
        (board_scan_id, posting_id),
    ).fetchone():
        counts.already_recorded += 1
        return

    content = job.source_content()
    comparison = compare_form(content)
    before = _neighbour(conn, posting_id, key, earlier=True)
    after = _neighbour(conn, posting_id, key, earlier=False)
    match = next(
        (
            n
            for n in (before, after)
            if n and _same_content(conn, n, source_fingerprint, content, comparison, processed_at)
        ),
        None,
    )
    if match is not None:
        version_id = match.version_id
        counts.unchanged += 1
        if match.source_fingerprint != source_fingerprint:
            counts.cosmetic_only += 1
    else:
        first = before is None and after is None
        version_id = _insert_version(
            conn, posting_id, job, source_fingerprint, comparison, snapshot, observed_at,
            processed_at, "first_seen" if first else "content_change", counts,
        )  # fmt: skip
        counts.new_versions += 1
        if not first:
            counts.changed += 1

    observation_id = conn.execute(
        "INSERT INTO posting_observations (board_scan_id, posting_id, version_id, "
        "raw_response_id, observed_at, source_published_at, source_updated_at, "
        "source_fingerprint, normalization_version, comparison_fingerprint, processed_at, "
        "recorded_by, source_created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING observation_id",
        (
            board_scan_id,
            posting_id,
            version_id,
            raw_response_id,
            observed_at,
            job.first_published,
            job.updated_at,
            source_fingerprint,
            comparison.normalization_version,
            comparison.fingerprint,
            processed_at,
            recorded_by,
            job.created_at,
        ),
    ).fetchone()[0]
    counts.new_observations += 1

    if current_key is None or key > tuple(current_key):
        conn.execute(
            "UPDATE source_postings SET current_observation_id = %s, current_version_id = %s "
            "WHERE posting_id = %s",
            (observation_id, version_id, posting_id),
        )
    else:
        counts.historical += 1


def _screen(
    conn: psycopg.Connection,
    posting_id: int,
    job: PostingRecord,
    source_fingerprint: str,
    key: tuple[datetime, int],
    processed_at: datetime,
    counts: StoreCounts,
) -> tuple[bool, dict]:
    """Screen this content and return (retained, screening snapshot). The stored screening
    is replaced only by content collected at or after the content it describes."""
    row = conn.execute(
        "SELECT content_hash, classifier_version, retained, needs_review, "
        "to_jsonb(posting_screenings), content_observed_at, content_scan_id "
        "FROM posting_screenings WHERE posting_id = %s",
        (posting_id,),
    ).fetchone()
    cached = row is not None and row[0] == source_fingerprint and row[1] == CLASSIFIER_VERSION
    newest = row is None or key >= (row[5], row[6] or 0)

    if cached:
        retained, needs_review, snapshot = row[2], row[3], row[4]
        if newest:
            conn.execute(
                "UPDATE posting_screenings SET content_observed_at = %s, content_scan_id = %s "
                "WHERE posting_id = %s",
                (key[0], key[1], posting_id),
            )
    else:
        result = screen(job.title, job.description_text, job.location_name, job.offices)
        retained, needs_review, snapshot = result.retained, result.needs_review, result.as_dict()
        if newest:
            _upsert_screening(conn, posting_id, job, source_fingerprint, result, key, processed_at)

    if retained:
        counts.retained += 1
    else:
        counts.excluded += 1
    if needs_review:
        counts.needing_review += 1
    return retained, snapshot


def _upsert_screening(conn, posting_id, job, source_fingerprint, result, key, processed_at):
    conn.execute(
        """
        INSERT INTO posting_screenings (
            posting_id, content_hash, classifier_version, title, location_text, retained,
            relevant, families, texas_eligibility, city_targeted_remote, seniority,
            min_years_required, early_career, sponsorship_status, sponsorship_evidence,
            needs_review, reasons, screened_at, content_observed_at, content_scan_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (posting_id) DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            classifier_version = EXCLUDED.classifier_version,
            title = EXCLUDED.title, location_text = EXCLUDED.location_text,
            retained = EXCLUDED.retained, relevant = EXCLUDED.relevant,
            families = EXCLUDED.families, texas_eligibility = EXCLUDED.texas_eligibility,
            city_targeted_remote = EXCLUDED.city_targeted_remote,
            seniority = EXCLUDED.seniority, min_years_required = EXCLUDED.min_years_required,
            early_career = EXCLUDED.early_career,
            sponsorship_status = EXCLUDED.sponsorship_status,
            sponsorship_evidence = EXCLUDED.sponsorship_evidence,
            needs_review = EXCLUDED.needs_review, reasons = EXCLUDED.reasons,
            screened_at = EXCLUDED.screened_at,
            content_observed_at = EXCLUDED.content_observed_at,
            content_scan_id = EXCLUDED.content_scan_id
        """,
        (
            posting_id,
            source_fingerprint,
            result.classifier_version,
            job.title,
            job.location_name,
            result.retained,
            result.relevant,
            result.families,
            result.texas_eligibility,
            result.city_targeted_remote,
            result.seniority,
            result.min_years_required,
            result.early_career,
            result.sponsorship_status,
            result.sponsorship_evidence,
            result.needs_review,
            Jsonb(result.reasons),
            processed_at,
            key[0],
            key[1],
        ),
    )


def _neighbour(
    conn: psycopg.Connection, posting_id: int, key: tuple[datetime, int], earlier: bool
) -> _Neighbour | None:
    """The newest observation collected before key, or the oldest one collected after it."""
    comparison, direction = ("<", "DESC") if earlier else (">", "ASC")
    row = conn.execute(
        "SELECT o.observation_id, o.version_id, o.observed_at, o.board_scan_id, "
        "o.source_fingerprint, o.normalization_version, o.comparison_fingerprint, "
        "v.content_hash, v.description_omitted FROM posting_observations o "
        "JOIN posting_versions v ON v.version_id = o.version_id "
        f"WHERE o.posting_id = %s AND (o.observed_at, o.board_scan_id) {comparison} (%s, %s) "
        f"ORDER BY o.observed_at {direction}, o.board_scan_id {direction} LIMIT 1",
        (posting_id, key[0], key[1]),
    ).fetchone()
    return _Neighbour(*row) if row else None


def _same_content(
    conn: psycopg.Connection,
    neighbour: _Neighbour,
    source_fingerprint: str,
    content: SourceContent,
    comparison: Comparison,
    now: datetime,
) -> bool:
    if neighbour.source_fingerprint == source_fingerprint:
        return True  # identical source content: nothing changed, whatever the rules say
    if neighbour.normalization_version == comparison.normalization_version:
        return neighbour.comparison_fingerprint == comparison.fingerprint
    if neighbour.description_omitted is None and (
        neighbour.source_fingerprint == neighbour.version_content_hash
        or neighbour.normalization_version is None
    ):
        # The neighbour's content is exactly its version's stored original (always true for
        # observations recorded before comparison rules existed), so it can be evaluated
        # under the current rules.
        rules = comparison.normalization_version
        stored = version_comparison(conn, neighbour.version_id, rules, now)
        return stored.fingerprint == comparison.fingerprint
    # Only a cosmetic variant of the neighbour's content is stored, or none at all (an omitted
    # long description). Judge under the rules the
    # neighbour was recorded with, so a difference the old rules ignored, and the new rules
    # would see, is not reported as a change the employer made now.
    return (
        compare_form(content, neighbour.normalization_version).fingerprint
        == neighbour.comparison_fingerprint
    )


def _insert_version(
    conn: psycopg.Connection,
    posting_id: int,
    job: PostingRecord,
    source_fingerprint: str,
    comparison: Comparison,
    snapshot: dict,
    observed_at: datetime,
    processed_at: datetime,
    detection: str,
    counts: StoreCounts,
) -> int:
    length = len(job.description_text or "")
    omitted = "too_long" if length > MAX_STORED_DESCRIPTION_CHARS else None
    if omitted:
        counts.omitted_too_long += 1
    version_id = conn.execute(
        """
        INSERT INTO posting_versions (
            posting_id, version_no, content_hash, title, location_text, description_text,
            first_seen_at, absolute_url, content_html, departments, offices,
            requisition_id, internal_job_id, source_metadata, screening,
            normalization_version, detection, recorded_at, apply_url, source_fields,
            description_length, description_omitted)
        VALUES (%s, (SELECT coalesce(max(version_no), 0) + 1 FROM posting_versions
                     WHERE posting_id = %s),
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING version_id
        """,
        (
            posting_id,
            posting_id,
            source_fingerprint,
            job.title,
            job.location_name,
            None if omitted else job.description_text,
            observed_at,
            job.absolute_url,
            None if omitted else job.content_html,
            Jsonb(job.departments),
            Jsonb(job.offices),
            job.requisition_id,
            job.internal_job_id,
            Jsonb(job.metadata),
            Jsonb(snapshot),
            comparison.normalization_version,
            detection,
            processed_at,
            job.apply_url,
            Jsonb(job.source_fields) if job.source_fields is not None else None,
            length,
            omitted,
        ),
    ).fetchone()[0]
    if omitted:
        # The fingerprint covers the full text; the stored comparison form does not keep it.
        normalized = {**comparison.normalized, "description": OMITTED_DESCRIPTION}
        comparison = Comparison(
            comparison.normalization_version, normalized, comparison.fingerprint
        )
    _save_comparison(conn, version_id, comparison, processed_at)
    return version_id


def _save_comparison(
    conn: psycopg.Connection, version_id: int, comparison: Comparison, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO posting_version_comparisons (version_id, normalization_version, "
        "fingerprint, normalized, computed_at) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (version_id, normalization_version) DO NOTHING",
        (
            version_id,
            comparison.normalization_version,
            comparison.fingerprint,
            Jsonb(comparison.normalized),
            now,
        ),
    )


def version_source_content(conn: psycopg.Connection, version_id: int) -> SourceContent:
    """The original content stored for a version, as normalization rules consume it.
    Raises ContentOmitted for a version whose long description was not stored."""
    title, location, html, offices, departments, metadata, text, omitted = conn.execute(
        "SELECT title, location_text, content_html, offices, departments, source_metadata, "
        "description_text, description_omitted FROM posting_versions WHERE version_id = %s",
        (version_id,),
    ).fetchone()
    if omitted:
        raise ContentOmitted(f"version {version_id}: description not stored ({omitted})")
    return SourceContent(
        title=title,
        location=location,
        content_html=html,
        offices=offices or [],
        departments=departments or [],
        metadata=metadata,
        description_text=text,
    )


def version_comparison(
    conn: psycopg.Connection, version_id: int, rules: str, now: datetime
) -> Comparison:
    """A version's comparison form under the given rules, computed from its stored original
    content the first time and kept in posting_version_comparisons."""
    row = conn.execute(
        "SELECT fingerprint, normalized FROM posting_version_comparisons "
        "WHERE version_id = %s AND normalization_version = %s",
        (version_id, rules),
    ).fetchone()
    if row:
        return Comparison(rules, row[1], row[0])
    comparison = compare_form(version_source_content(conn, version_id), rules)
    _save_comparison(conn, version_id, comparison, now)
    return comparison


@dataclass
class RefingerprintResult:
    rules: str
    versions: int
    computed: int
    omitted: int  # versions without stored text, which cannot be evaluated under new rules
    # Versions whose comparison form under the current rules differs from the form under
    # the rules they were recorded with. Same original content, so this is a rule effect.
    rule_effects: list[tuple[int, str]]


def refingerprint_versions(
    conn: psycopg.Connection, now: datetime, rules: str | None = None
) -> RefingerprintResult:
    """Evaluate every stored version under the given rules (the current ones by default).
    Writes comparison rows only; never creates, changes, or merges versions."""
    rules = rules or normalize.CURRENT_NORMALIZATION
    rows = conn.execute(
        "SELECT v.version_id, v.normalization_version, "
        "EXISTS (SELECT 1 FROM posting_version_comparisons c "
        "        WHERE c.version_id = v.version_id AND c.normalization_version = %s) "
        "FROM posting_versions v WHERE v.description_omitted IS NULL ORDER BY v.version_id",
        (rules,),
    ).fetchall()
    omitted = conn.execute(
        "SELECT count(*) FROM posting_versions WHERE description_omitted IS NOT NULL"
    ).fetchone()[0]
    computed, effects = 0, []
    for version_id, recorded_rules, present in rows:
        with conn.transaction():
            current = version_comparison(conn, version_id, rules, now)
            computed += not present
            if recorded_rules and recorded_rules != rules:
                earlier = version_comparison(conn, version_id, recorded_rules, now)
                if earlier.fingerprint != current.fingerprint:
                    effects.append((version_id, recorded_rules))
    return RefingerprintResult(rules, len(rows), computed, omitted, effects)
