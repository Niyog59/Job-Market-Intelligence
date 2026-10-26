"""Write a validated board response into the database.

For every valid job: keep its identity and discovery timestamps (source_postings) and the
current screening decision (posting_screenings). Postings that are, or have ever been,
retained also get full content history: a new posting_versions row only when the content
fingerprint differs from the latest version, and a posting_observations row per scan.
Rerunning the same data therefore adds observations but never duplicate postings or
invented versions.
"""

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from jobmarket.classify import CLASSIFIER_VERSION, screen
from jobmarket.greenhouse import JobRecord, ParsedResponse


@dataclass
class StoreCounts:
    retained: int = 0
    excluded: int = 0
    needing_review: int = 0
    new_postings: int = 0
    new_versions: int = 0


def store_scan_results(
    conn: psycopg.Connection,
    board_id: int,
    board_scan_id: int,
    raw_response_id: int,
    parsed: ParsedResponse,
    observed_at: datetime,
) -> StoreCounts:
    """Store jobs and rejected records. Runs inside the caller's transaction."""
    counts = StoreCounts()
    for job in parsed.jobs:
        _store_job(conn, board_id, board_scan_id, raw_response_id, job, observed_at, counts)

    for rejection in parsed.rejected:
        conn.execute(
            "INSERT INTO rejected_records (board_scan_id, raw_response_id, record_index, "
            "source_posting_id, reason, detail, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                board_scan_id,
                raw_response_id,
                rejection.record_index,
                rejection.source_id,
                rejection.reason,
                Jsonb(rejection.detail) if rejection.detail else None,
                observed_at,
            ),
        )

    if parsed.outcome == "complete":
        # Only a complete scan is evidence that a posting is missing.
        seen = [str(job.source_id) for job in parsed.jobs]
        conn.execute(
            "UPDATE source_postings SET complete_scan_absences = complete_scan_absences + 1, "
            "last_absent_scan_id = %s WHERE board_id = %s AND NOT (source_posting_id = ANY(%s))",
            (board_scan_id, board_id, seen),
        )
    return counts


def _store_job(
    conn: psycopg.Connection,
    board_id: int,
    board_scan_id: int,
    raw_response_id: int,
    job: JobRecord,
    observed_at: datetime,
    counts: StoreCounts,
) -> None:
    source_id = str(job.source_id)
    row = conn.execute(
        "SELECT posting_id FROM source_postings WHERE board_id = %s AND source_posting_id = %s "
        "FOR UPDATE",
        (board_id, source_id),
    ).fetchone()
    if row is None:
        posting_id = conn.execute(
            "INSERT INTO source_postings (board_id, source_posting_id, first_discovered_at, "
            "last_observed_at) VALUES (%s, %s, %s, %s) RETURNING posting_id",
            (board_id, source_id, observed_at, observed_at),
        ).fetchone()[0]
        counts.new_postings += 1
    else:
        posting_id = row[0]
        conn.execute(
            "UPDATE source_postings SET last_observed_at = GREATEST(last_observed_at, %s), "
            "complete_scan_absences = 0 WHERE posting_id = %s",
            (observed_at, posting_id),
        )

    content_hash = job.content_hash()
    screening_row = conn.execute(
        "SELECT content_hash, classifier_version, retained, needs_review, "
        "to_jsonb(posting_screenings) FROM posting_screenings WHERE posting_id = %s",
        (posting_id,),
    ).fetchone()
    if (
        screening_row is None
        or screening_row[0] != content_hash
        or screening_row[1] != CLASSIFIER_VERSION
    ):
        result = screen(job.title, job.description_text, job.location_name, job.offices)
        retained, needs_review, snapshot = result.retained, result.needs_review, result.as_dict()
        conn.execute(
            """
            INSERT INTO posting_screenings (
                posting_id, content_hash, classifier_version, title, location_text, retained,
                relevant, families, texas_eligibility, city_targeted_remote, seniority,
                min_years_required, early_career, sponsorship_status, sponsorship_evidence,
                needs_review, reasons, screened_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                screened_at = EXCLUDED.screened_at
            """,
            (
                posting_id,
                content_hash,
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
                observed_at,
            ),
        )
    else:
        retained, needs_review, snapshot = screening_row[2], screening_row[3], screening_row[4]

    if retained:
        counts.retained += 1
    else:
        counts.excluded += 1
    if needs_review:
        counts.needing_review += 1

    latest = conn.execute(
        "SELECT version_id, content_hash FROM posting_versions WHERE posting_id = %s "
        "ORDER BY version_no DESC LIMIT 1",
        (posting_id,),
    ).fetchone()
    if not retained and latest is None:
        return  # never retained: identity and screening only

    if latest is None or latest[1] != content_hash:
        version_id = conn.execute(
            """
            INSERT INTO posting_versions (
                posting_id, version_no, content_hash, title, location_text, description_text,
                first_seen_at, absolute_url, content_html, departments, offices,
                requisition_id, internal_job_id, source_metadata, screening)
            VALUES (%s, (SELECT coalesce(max(version_no), 0) + 1 FROM posting_versions
                         WHERE posting_id = %s),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING version_id
            """,
            (
                posting_id,
                posting_id,
                content_hash,
                job.title,
                job.location_name,
                job.description_text,
                observed_at,
                job.absolute_url,
                job.content_html,
                Jsonb(job.departments),
                Jsonb(job.offices),
                job.requisition_id,
                job.internal_job_id,
                Jsonb(job.metadata),
                Jsonb(snapshot),
            ),
        ).fetchone()[0]
        counts.new_versions += 1
    else:
        version_id = latest[0]

    conn.execute(
        "INSERT INTO posting_observations (board_scan_id, posting_id, version_id, "
        "raw_response_id, observed_at, source_published_at, source_updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (board_scan_id, posting_id) DO NOTHING",
        (
            board_scan_id,
            posting_id,
            version_id,
            raw_response_id,
            observed_at,
            job.first_published,
            job.updated_at,
        ),
    )


def flag_possible_duplicates(conn: psycopg.Connection, detected_at: datetime) -> int:
    """Flag retained postings that look like the same vacancy, without merging them.

    Same employer, same normalized title, and the same description text, across boards or
    across city-targeted postings. Each member is linked to the lowest posting id of its
    group, so a group of n postings creates n - 1 flags rather than every pair.
    """
    cur = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (v.posting_id) v.posting_id, b.employer_id,
                   lower(regexp_replace(
                       regexp_replace(v.title, '\\s+-\\s+[^-]+,\\s*[^-]+$', ''),
                       '\\s+', ' ', 'g')) AS norm_title,
                   md5(coalesce(v.description_text, '')) AS desc_hash
            FROM posting_versions v
            JOIN source_postings p USING (posting_id)
            JOIN source_boards b USING (board_id)
            JOIN posting_screenings ps USING (posting_id)
            WHERE ps.retained
            ORDER BY v.posting_id, v.version_no DESC
        ), grouped AS (
            SELECT posting_id,
                   min(posting_id) OVER (PARTITION BY employer_id, norm_title, desc_hash)
                       AS anchor,
                   count(*) OVER (PARTITION BY employer_id, norm_title, desc_hash) AS n
            FROM latest
        )
        INSERT INTO posting_duplicate_flags (posting_id, related_posting_id, reason, detected_at)
        SELECT anchor, posting_id, 'same employer, title, and description', %s
        FROM grouped WHERE n > 1 AND posting_id <> anchor
        ON CONFLICT DO NOTHING
        """,
        (detected_at,),
    )
    return cur.rowcount
