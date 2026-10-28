"""Requirement extraction queue: which posting versions to send to the model, when, and
what to keep.

Flow. Collection stores versions and never calls the model. enqueue_new() adds a job for
every current version that has never been queued, under the active configuration (model,
thinking level, prompt and schema versions). run_extraction() works through the queue one
request at a time:

  priority  1 new or edited retained listing    2 retained backlog
            3 new or edited other listing       4 other backlog
  (backlog = content recorded before extraction tracking started, decision 2026-10-27)

For each job it builds the input document, reuses an earlier answer when the same document
was already extracted under the same configuration (no request), otherwise sends one
request, checks the answer (jobmarket.extraction_check), and stores the result together
with its versions and evidence in one transaction. A job whose version is no longer the
posting's current content is superseded instead of extracted, so a result is never attached
to newer content.

Limits and failures (decisions 2026-10-27). Requests are spaced to the configured
requests-per-minute and, if set, tokens-per-minute limits. A failed request is retried up
to three times, waiting 10, 30, then 90 seconds, or longer if Google says so. A job whose
requests all fail is retried in the next run, at most five runs, and then gives up until
`jobmarket extract reprocess`. A daily-quota error pauses extraction until midnight Pacific
time (the provider's reset). Runs stop before 90% of the configured daily request limit, to
leave room for manual tests. None of this touches collection.

Configuration changes are never applied to old results automatically: a new model, prompt,
or schema version applies to new content only, until `jobmarket extract reprocess` queues
older content explicitly. Python-side normalization changes are applied by
`jobmarket extract renormalize`, which needs no model calls.

Free tier only. Nothing here enables billing; the free tier is confirmed in Google AI
Studio. Estimated paid-equivalent cost is reported for planning only.
"""

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from jobmarket.clock import Clock
from jobmarket.extraction_check import NORMALIZATION_VERSION, check_answer, derive, normalize_answer
from jobmarket.extraction_schema import (
    INSTRUCTIONS,
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SCHEMA_VERSION,
    build_document,
    request_text,
)
from jobmarket.gemini import ModelError, Reply
from jobmarket.store import MAX_STORED_DESCRIPTION_CHARS

EXTRACTION_LOCK_KEY = 4_815_162_344
PACIFIC = ZoneInfo("America/Los_Angeles")

MODEL_ENV = "JOBMARKET_AI_MODEL"
THINKING_ENV = "JOBMARKET_AI_THINKING"
RPM_ENV = "JOBMARKET_AI_RPM"
TPM_ENV = "JOBMARKET_AI_TPM"
DAILY_ENV = "JOBMARKET_AI_DAILY_LIMIT"
TIMEOUT_ENV = "JOBMARKET_AI_TIMEOUT_SECONDS"

DEFAULT_MODEL = "gemini-3.5-flash-lite"  # decision 2026-10-27
DEFAULT_THINKING = "minimal"
DEFAULT_TIMEOUT_SECONDS = 120.0

RETRY_WAITS = (10.0, 30.0, 90.0)  # seconds before retries 1, 2, 3
MAX_FAILED_RUNS = 5
SAFETY_SHARE = 0.9  # stop at 90% of the daily request limit
MAX_INPUT_DESCRIPTION_CHARS = MAX_STORED_DESCRIPTION_CHARS  # longer: link only, not sent

# For planning only (Gemini 3.5 Flash-Lite paid rates, USD per 1M tokens, read 2026-10-27).
# Thinking tokens are billed as output.
PAID_RATE_PER_MILLION = {"gemini-3.5-flash-lite": (0.30, 2.50)}


class ExtractionBusy(Exception):
    """Another extraction run holds the lock."""


class ExtractionNotConfigured(Exception):
    """A required setting (such as the account's limits) is missing."""


@dataclass(frozen=True)
class ExtractionConfig:
    model: str = DEFAULT_MODEL
    thinking_level: str = DEFAULT_THINKING
    rpm: int | None = None  # requests per minute allowed for this account and model
    tpm: int | None = None  # tokens per minute, if the account has such a limit
    daily_limit: int | None = None  # requests per day
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ExtractionConfig":
        env = os.environ if environ is None else environ

        def number(name, kind):
            raw = env.get(name, "").strip()
            if not raw:
                return None
            try:
                value = kind(raw)
            except ValueError:
                raise ExtractionNotConfigured(f"{name} must be a number, got {raw!r}") from None
            if value <= 0:
                raise ExtractionNotConfigured(f"{name} must be positive")
            return value

        return cls(
            model=env.get(MODEL_ENV, "").strip() or DEFAULT_MODEL,
            thinking_level=env.get(THINKING_ENV, "").strip().lower() or DEFAULT_THINKING,
            rpm=number(RPM_ENV, int),
            tpm=number(TPM_ENV, int),
            daily_limit=number(DAILY_ENV, int),
            timeout_seconds=number(TIMEOUT_ENV, float) or DEFAULT_TIMEOUT_SECONDS,
        )

    @property
    def key(self) -> str:
        return (
            f"{self.model}|thinking={self.thinking_level}|prompt={PROMPT_VERSION}"
            f"|schema={SCHEMA_VERSION}"
        )

    def require_limits(self) -> None:
        missing = [n for n, v in ((RPM_ENV, self.rpm), (DAILY_ENV, self.daily_limit)) if not v]
        if missing:
            raise ExtractionNotConfigured(
                f"set {' and '.join(missing)} to this account's free-tier limits for "
                f"{self.model} (shown in Google AI Studio) before sending requests"
            )


Generator = Callable[[str, str, dict], Reply]  # (instructions, text, schema) -> Reply


# --- queue -----------------------------------------------------------------------------------


def enqueue_new(conn: psycopg.Connection, config: ExtractionConfig, now: datetime) -> int:
    """Queue every current version never queued under any configuration. Content already
    extracted (or queued) under an earlier configuration is left alone: that needs an
    explicit `extract reprocess`."""
    rows = conn.execute(
        """
        INSERT INTO extraction_jobs (version_id, posting_id, config_key, priority, reason,
                                     status, skip_reason, created_at, updated_at)
        SELECT v.version_id, p.posting_id, %(key)s,
               CASE WHEN ps.retained THEN (CASE WHEN n.is_new THEN 1 ELSE 2 END)
                    ELSE (CASE WHEN n.is_new THEN 3 ELSE 4 END) END,
               CASE WHEN n.is_new THEN 'new_content' ELSE 'backlog' END,
               CASE WHEN s.skip IS NULL THEN 'pending' ELSE 'skipped' END,
               s.skip, %(now)s, %(now)s
        FROM source_postings p
        JOIN posting_versions v ON v.version_id = p.current_version_id
        JOIN posting_screenings ps ON ps.posting_id = p.posting_id
        CROSS JOIN extraction_settings es
        CROSS JOIN LATERAL (SELECT
            p.first_discovered_at >= es.tracking_started_at
            OR (v.version_no > 1 AND v.first_seen_at >= es.tracking_started_at) AS is_new) n
        CROSS JOIN LATERAL (SELECT CASE
            WHEN v.description_omitted IS NOT NULL OR v.description_length > %(max)s
                THEN 'too_long'
            WHEN coalesce(btrim(v.description_text), '') = '' THEN 'no_description'
            END AS skip) s
        WHERE NOT EXISTS (SELECT 1 FROM extraction_jobs j WHERE j.version_id = v.version_id)
        ON CONFLICT (version_id, config_key) DO NOTHING
        RETURNING job_id
        """,
        {"key": config.key, "now": now, "max": MAX_INPUT_DESCRIPTION_CHARS},
    ).fetchall()
    return len(rows)


@dataclass
class ReprocessResult:
    queued: int = 0
    reset: int = 0
    superseded: int = 0


def reprocess(
    conn: psycopg.Connection,
    config: ExtractionConfig,
    now: datetime,
    reason: str,
    limit: int | None = None,
    retained_only: bool = False,
) -> ReprocessResult:
    """Explicitly queue current content under the active configuration: content whose result
    or job is under another configuration, and jobs that gave up. Pending jobs under other
    configurations are superseded. limit caps how many listings are queued."""
    if not reason.strip():
        raise ValueError("a reason is required")
    result = ReprocessResult()
    with conn.transaction():
        result.superseded = conn.execute(
            "UPDATE extraction_jobs SET status = 'superseded', updated_at = %s "
            "WHERE status IN ('pending', 'failed') AND config_key <> %s",
            (now, config.key),
        ).rowcount
        retained = "AND ps.retained" if retained_only else ""
        cap = "LIMIT %(limit)s" if limit else ""
        result.reset = conn.execute(
            f"""
            UPDATE extraction_jobs j SET status = 'pending', runs_failed = 0, note = %(reason)s,
                   updated_at = %(now)s
            FROM source_postings p JOIN posting_screenings ps USING (posting_id)
            WHERE j.posting_id = p.posting_id AND j.version_id = p.current_version_id
              AND j.config_key = %(key)s AND j.status = 'gave_up' {retained}
            """,
            {"reason": reason, "now": now, "key": config.key},
        ).rowcount
        result.queued = len(
            conn.execute(
                f"""
                INSERT INTO extraction_jobs (version_id, posting_id, config_key, priority,
                                             reason, status, note, created_at, updated_at)
                SELECT v.version_id, p.posting_id, %(key)s,
                       CASE WHEN ps.retained THEN 2 ELSE 4 END, 'reprocess', 'pending',
                       %(reason)s, %(now)s, %(now)s
                FROM source_postings p
                JOIN posting_versions v ON v.version_id = p.current_version_id
                JOIN posting_screenings ps ON ps.posting_id = p.posting_id
                WHERE v.description_omitted IS NULL
                  AND coalesce(btrim(v.description_text), '') <> ''
                  AND v.description_length <= %(max)s {retained}
                  AND NOT EXISTS (SELECT 1 FROM extraction_jobs j
                                  WHERE j.version_id = v.version_id AND j.config_key = %(key)s)
                ORDER BY ps.retained DESC, p.posting_id
                {cap}
                ON CONFLICT (version_id, config_key) DO NOTHING
                RETURNING job_id
                """,
                {
                    "key": config.key,
                    "reason": reason,
                    "now": now,
                    "limit": limit,
                    "max": MAX_INPUT_DESCRIPTION_CHARS,
                },  # fmt: skip
            ).fetchall()
        )
    return result


# --- documents ---------------------------------------------------------------------------------


def version_document(conn: psycopg.Connection, version_id: int) -> str:
    title, location, offices, fields, html, text = conn.execute(
        "SELECT title, location_text, offices, source_fields, content_html, description_text "
        "FROM posting_versions WHERE version_id = %s",
        (version_id,),
    ).fetchone()
    return build_document(title, location, offices, fields, html, text)


def store_input(conn: psycopg.Connection, document: str, now: datetime) -> str:
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO extraction_inputs (input_hash, prompt_version, document, char_count, "
        "created_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (input_hash) DO NOTHING",
        (digest, PROMPT_VERSION, document, len(document), now),
    )
    return digest


# --- running ------------------------------------------------------------------------------------


@dataclass
class RunReport:
    run_id: int | None = None
    queued_now: int = 0
    requests: int = 0
    succeeded: int = 0
    reused: int = 0
    failed: int = 0
    gave_up: int = 0
    superseded: int = 0
    warnings: int = 0
    stop_reason: str = "queue empty"
    errors: list[str] = field(default_factory=list)


def next_pacific_midnight(now: datetime) -> datetime:
    local = now.astimezone(PACIFIC)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0, 0), tzinfo=PACIFIC)


def requests_today(conn: psycopg.Connection, model: str, now: datetime) -> int:
    start = datetime.combine(now.astimezone(PACIFIC).date(), time(0, 0), tzinfo=PACIFIC)
    return conn.execute(
        "SELECT count(*) FROM extraction_attempts WHERE model = %s AND started_at >= %s",
        (model, start),
    ).fetchone()[0]


def safety_stop_at(config: ExtractionConfig) -> int:
    return math.floor(config.daily_limit * SAFETY_SHARE)


def _paused_until(conn, model: str, now: datetime) -> datetime | None:
    row = conn.execute(
        "SELECT paused_until FROM extraction_pauses WHERE model = %s AND paused_until > %s",
        (model, now),
    ).fetchone()
    return row[0] if row else None


def estimated_tokens(document: str) -> int:
    """A rough pre-request estimate (about four characters per token, plus room for the
    answer), used only to stay under a tokens-per-minute limit."""
    return (len(INSTRUCTIONS) + len(document)) // 4 + 2000


class _Pacer:
    def __init__(self, conn, config: ExtractionConfig, clock: Clock, sleep):
        self.conn, self.config, self.clock, self.sleep = conn, config, clock, sleep

    def wait(self, estimate: int) -> None:
        last = self.conn.execute(
            "SELECT max(started_at) FROM extraction_attempts WHERE model = %s",
            (self.config.model,),
        ).fetchone()[0]
        if last is not None:
            gap = 60.0 / self.config.rpm - (self.clock() - last).total_seconds()
            if gap > 0:
                self.sleep(gap)
        if self.config.tpm:
            while True:
                now = self.clock()
                rows = self.conn.execute(
                    "SELECT started_at, coalesce(total_tokens, 0) FROM extraction_attempts "
                    "WHERE model = %s AND started_at > %s ORDER BY started_at",
                    (self.config.model, now - timedelta(seconds=60)),
                ).fetchall()
                used = sum(t for _, t in rows)
                if used + estimate <= self.config.tpm or not rows:
                    return
                self.sleep(max(1.0, 60.0 - (now - rows[0][0]).total_seconds()))


def _pick(conn, config: ExtractionConfig, tried: list[int]):
    return conn.execute(
        """
        SELECT j.job_id, j.version_id, j.posting_id, (p.current_version_id = j.version_id)
        FROM extraction_jobs j JOIN source_postings p ON p.posting_id = j.posting_id
        WHERE j.config_key = %s AND NOT (j.job_id = ANY(%s))
          AND (j.status = 'pending' OR (j.status = 'failed' AND j.runs_failed < %s))
        ORDER BY j.priority, j.created_at, j.job_id
        LIMIT 1
        """,
        (config.key, tried, MAX_FAILED_RUNS),
    ).fetchone()


def _record_attempt(
    conn,
    job_id,
    run_id,
    n,
    config,
    started,
    finished,
    outcome,
    *,
    reply=None,
    error: ModelError | None = None,
    raw=None,
    detail=None,
) -> int:
    usage = reply.usage if reply else (error.usage if error and error.usage else None)
    return conn.execute(
        """
        INSERT INTO extraction_attempts (job_id, run_id, attempt_no, model, started_at,
            finished_at, outcome, http_status, quota_id, retry_after_seconds, prompt_tokens,
            output_tokens, thought_tokens, total_tokens, traffic_type, model_version,
            raw_reply, error_detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING attempt_id
        """,
        (
            job_id, run_id, n, config.model, started, finished, outcome,
            error.http_status if error else None,
            error.quota_id if error else None,
            error.retry_after if error else None,
            usage.prompt_tokens if usage else None,
            usage.output_tokens if usage else None,
            usage.thought_tokens if usage else None,
            usage.total_tokens if usage else None,
            usage.traffic_type if usage else None,
            reply.model_version if reply else None,
            raw, detail or (error.detail if error else None),
        ),
    ).fetchone()[0]  # fmt: skip


def _store_result(
    conn,
    job_id,
    version_id,
    posting_id,
    input_hash,
    config,
    checked,
    answer,
    now,
    *,
    attempt_id=None,
    model_version=None,
    reused_from=None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO extraction_results (job_id, version_id, posting_id, input_hash, config_key,
            model, model_version, thinking_level, prompt_version, schema_version,
            normalization_version, raw_output, normalized, derived, validation_status,
            validation_issues, evidence, reused_from_result_id, attempt_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (version_id, config_key) DO NOTHING
        RETURNING result_id
        """,
        (
            job_id, version_id, posting_id, input_hash, config.key, config.model,
            model_version, config.thinking_level, PROMPT_VERSION, SCHEMA_VERSION,
            NORMALIZATION_VERSION, Jsonb(answer), Jsonb(checked.normalized),
            Jsonb(checked.derived), checked.status, Jsonb(checked.warnings),
            Jsonb(checked.evidence), reused_from, attempt_id, now,
        ),
    ).fetchone()  # fmt: skip
    result_id = (
        row[0]
        if row
        else conn.execute(
            "SELECT result_id FROM extraction_results WHERE version_id = %s AND config_key = %s",
            (version_id, config.key),
        ).fetchone()[0]
    )
    conn.execute(
        "UPDATE extraction_jobs SET status = 'succeeded', result_id = %s, last_error = NULL, "
        "updated_at = %s WHERE job_id = %s",
        (result_id, now, job_id),
    )
    return result_id


def _reuse(conn, job_id, version_id, posting_id, input_hash, config, now) -> bool:
    earlier = conn.execute(
        "SELECT result_id, raw_output, input_hash FROM extraction_results "
        "WHERE input_hash = %s AND config_key = %s AND reused_from_result_id IS NULL "
        "ORDER BY result_id LIMIT 1",
        (input_hash, config.key),
    ).fetchone()
    if earlier is None:
        return False
    result_id, answer, _ = earlier
    document = conn.execute(
        "SELECT document FROM extraction_inputs WHERE input_hash = %s", (input_hash,)
    ).fetchone()[0]
    checked = check_answer(answer, document)
    with conn.transaction():
        _store_result(conn, job_id, version_id, posting_id, input_hash, config, checked,
                      answer, now, reused_from=result_id)  # fmt: skip
    return True


def _fail_job(conn, job_id, message: str, now: datetime) -> bool:
    """Count a failed run for the job; True when it has now given up."""
    row = conn.execute(
        "UPDATE extraction_jobs SET runs_failed = runs_failed + 1, last_error = %s, "
        "status = CASE WHEN runs_failed + 1 >= %s THEN 'gave_up' ELSE 'failed' END, "
        "updated_at = %s WHERE job_id = %s RETURNING status",
        (message[:1000], MAX_FAILED_RUNS, now, job_id),
    ).fetchone()
    return row[0] == "gave_up"


def _release(conn, job_id, message: str | None, now: datetime) -> None:
    """Put a job back as pending without counting a failure (the cause was not the job)."""
    conn.execute(
        "UPDATE extraction_jobs SET status = 'pending', last_error = %s, updated_at = %s "
        "WHERE job_id = %s",
        (message, now, job_id),
    )


class _StopRun(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def run_extraction(
    conn: psycopg.Connection,
    generate: Generator,
    config: ExtractionConfig,
    clock: Clock,
    sleep: Callable[[float], None],
    limit: int | None = None,
) -> RunReport:
    """Work through the queue (see the module docstring). conn must be in autocommit mode;
    each step commits on its own so usage and results survive an interrupted run. limit caps
    how many jobs this run handles."""
    config.require_limits()
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (EXTRACTION_LOCK_KEY,)).fetchone()[0]:
        raise ExtractionBusy("another extraction run is in progress")
    try:
        return _run(conn, generate, config, clock, sleep, limit)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (EXTRACTION_LOCK_KEY,))


def _run(conn, generate, config, clock, sleep, limit) -> RunReport:
    report = RunReport()
    now = clock()
    # Holding the lock, no other run is active: anything left "running" was interrupted.
    conn.execute(
        "UPDATE extraction_jobs SET status = 'pending', updated_at = %s WHERE status = 'running'",
        (now,),
    )
    with conn.transaction():
        report.queued_now = enqueue_new(conn, config, now)
    report.run_id = conn.execute(
        "INSERT INTO extraction_runs (started_at, config_key) VALUES (%s, %s) RETURNING run_id",
        (now, config.key),
    ).fetchone()[0]
    pacer = _Pacer(conn, config, clock, sleep)
    tried: list[int] = []
    try:
        while limit is None or len(tried) < limit:
            now = clock()
            until = _paused_until(conn, config.model, now)
            if until is not None:
                raise _StopRun(f"paused until {until.isoformat(timespec='minutes')} "
                               "(daily quota)")  # fmt: skip
            if requests_today(conn, config.model, now) >= safety_stop_at(config):
                raise _StopRun(
                    f"safety stop: {safety_stop_at(config)} requests today is "
                    f"{int(SAFETY_SHARE * 100)}% of the daily limit {config.daily_limit}"
                )
            picked = _pick(conn, config, tried)
            if picked is None:
                break
            job_id, version_id, posting_id, current = picked
            tried.append(job_id)
            if not current:
                conn.execute(
                    "UPDATE extraction_jobs SET status = 'superseded', updated_at = %s "
                    "WHERE job_id = %s",
                    (now, job_id),
                )
                report.superseded += 1
                continue
            document = version_document(conn, version_id)
            with conn.transaction():
                input_hash = store_input(conn, document, now)
                conn.execute(
                    "UPDATE extraction_jobs SET input_hash = %s, updated_at = %s WHERE job_id = %s",
                    (input_hash, now, job_id),
                )
            if _reuse(conn, job_id, version_id, posting_id, input_hash, config, now):
                report.reused += 1
                continue
            conn.execute(
                "UPDATE extraction_jobs SET status = 'running', updated_at = %s WHERE job_id = %s",
                (now, job_id),
            )
            _process(conn, generate, config, clock, sleep, pacer, report,
                     job_id, version_id, posting_id, input_hash, document)  # fmt: skip
    except _StopRun as stop:
        report.stop_reason = stop.reason
    else:
        if limit is not None and len(tried) >= limit:
            report.stop_reason = f"limit of {limit} job(s) reached"
    conn.execute(
        "UPDATE extraction_runs SET finished_at = %s, requests = %s, succeeded = %s, "
        "reused = %s, failed = %s, stop_reason = %s WHERE run_id = %s",
        (clock(), report.requests, report.succeeded, report.reused, report.failed,
         report.stop_reason, report.run_id),
    )  # fmt: skip
    return report


def _process(
    conn,
    generate,
    config,
    clock,
    sleep,
    pacer,
    report,
    job_id,
    version_id,
    posting_id,
    input_hash,
    document,
) -> None:
    text = request_text(document)
    last_problem = "no request sent"
    for n in range(1, len(RETRY_WAITS) + 2):
        if n > 1 and requests_today(conn, config.model, clock()) >= safety_stop_at(config):
            _release(conn, job_id, last_problem, clock())
            raise _StopRun(f"safety stop reached during retries of job {job_id}")
        pacer.wait(estimated_tokens(document))
        started = clock()
        try:
            reply = generate(INSTRUCTIONS, text, RESPONSE_SCHEMA)
        except ModelError as error:
            finished = clock()
            report.requests += 1
            _record_attempt(conn, job_id, report.run_id, n, config, started, finished,
                            error.kind, error=error)  # fmt: skip
            last_problem = f"{error.kind}: {error.detail}"
            if error.kind == "quota_exhausted":
                until = next_pacific_midnight(finished)
                conn.execute(
                    "INSERT INTO extraction_pauses (model, paused_until, reason, set_at) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (model) DO UPDATE SET "
                    "paused_until = EXCLUDED.paused_until, reason = EXCLUDED.reason, "
                    "set_at = EXCLUDED.set_at",
                    (config.model, until, f"daily quota exhausted ({error.quota_id})", finished),
                )
                _release(conn, job_id, last_problem, finished)
                raise _StopRun(f"daily quota exhausted; paused until "
                               f"{until.isoformat(timespec='minutes')}") from None  # fmt: skip
            if error.kind == "client_error":
                report.failed += 1
                report.gave_up += _fail_job(conn, job_id, last_problem, finished)
                report.errors.append(f"job {job_id}: {last_problem}")
                raise _StopRun(
                    f"request rejected ({error.http_status}): {error.detail[:200]}"
                ) from None
            if n <= len(RETRY_WAITS):
                sleep(max(RETRY_WAITS[n - 1], error.retry_after or 0))
            continue
        finished = clock()
        report.requests += 1
        try:
            answer = json.loads(reply.text)
        except json.JSONDecodeError as exc:
            answer, checked = None, None
            problem = f"reply is not JSON: {exc}"
        else:
            checked = check_answer(answer, document)
            problem = None if checked.valid else "; ".join(checked.errors[:5])
        if problem is not None:
            _record_attempt(conn, job_id, report.run_id, n, config, started, finished,
                            "invalid_output", reply=reply, raw=reply.text[:200_000],
                            detail=problem)  # fmt: skip
            last_problem = f"invalid_output: {problem}"
            if n <= len(RETRY_WAITS):
                sleep(RETRY_WAITS[n - 1])
            continue
        with conn.transaction():
            attempt_id = _record_attempt(conn, job_id, report.run_id, n, config, started,
                                         finished, "success", reply=reply)  # fmt: skip
            _store_result(conn, job_id, version_id, posting_id, input_hash, config, checked,
                          answer, finished, attempt_id=attempt_id,
                          model_version=reply.model_version)  # fmt: skip
        report.succeeded += 1
        report.warnings += bool(checked.warnings)
        return
    report.failed += 1
    report.gave_up += _fail_job(conn, job_id, last_problem, clock())
    report.errors.append(f"job {job_id}: {last_problem}")


# --- re-normalization, status, and display ---------------------------------------------------


def renormalize(conn: psycopg.Connection, now: datetime) -> int:
    """Apply the current Python normalization to stored answers. No model calls."""
    rows = conn.execute(
        "SELECT result_id, raw_output FROM extraction_results "
        "WHERE normalization_version <> %s ORDER BY result_id",
        (NORMALIZATION_VERSION,),
    ).fetchall()
    for result_id, answer in rows:
        normalized = normalize_answer(answer)
        conn.execute(
            "UPDATE extraction_results SET normalized = %s, derived = %s, "
            "normalization_version = %s, renormalized_at = %s WHERE result_id = %s",
            (Jsonb(normalized), Jsonb(derive(normalized)), NORMALIZATION_VERSION, now, result_id),
        )
    return len(rows)


def paid_equivalent_usd(model: str, prompt_tokens: int, output_tokens: int) -> float | None:
    rates = PAID_RATE_PER_MILLION.get(model)
    if rates is None:
        return None
    return (prompt_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def status(conn: psycopg.Connection, config: ExtractionConfig, now: datetime) -> dict[str, Any]:
    queue = conn.execute(
        "SELECT status, priority, count(*) FROM extraction_jobs WHERE config_key = %s "
        "GROUP BY 1, 2 ORDER BY 1, 2",
        (config.key,),
    ).fetchall()
    other = conn.execute(
        "SELECT count(*) FROM extraction_jobs WHERE config_key <> %s "
        "AND status IN ('pending', 'failed')",
        (config.key,),
    ).fetchone()[0]
    day = now.astimezone(PACIFIC).date()
    usage = conn.execute(
        "SELECT requests, successful, prompt_tokens, output_tokens, thought_tokens, "
        "total_tokens FROM extraction_usage_daily WHERE pacific_day = %s AND model = %s",
        (day, config.model),
    ).fetchone() or (0, 0, 0, 0, 0, 0)
    results = dict(
        conn.execute(
            "SELECT validation_status, count(*) FROM current_extractions GROUP BY 1"
        ).fetchall()
    )
    pending_new = conn.execute(
        "SELECT count(*) FROM source_postings p WHERE NOT EXISTS "
        "(SELECT 1 FROM extraction_jobs j WHERE j.version_id = p.current_version_id) "
        "AND p.current_version_id IS NOT NULL"
    ).fetchone()[0]
    return {
        "config_key": config.key,
        "queue": queue,
        "other_config_open": other,
        "not_yet_queued": pending_new,
        "pacific_day": day,
        "usage_today": usage,
        "paid_equivalent_today": paid_equivalent_usd(config.model, usage[2], usage[3] + usage[4]),
        "paused_until": _paused_until(conn, config.model, now),
        "safety_stop_at": safety_stop_at(config) if config.daily_limit else None,
        "current_results": results,
    }
