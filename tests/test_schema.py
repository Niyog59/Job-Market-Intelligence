"""Constraint tests for the core schema: the database itself must reject invalid data."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import errors

from jobmarket.migrate import apply_migrations

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 26, 17, 0, tzinfo=UTC)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Rows:
    """Small helpers that insert valid rows and return their generated ids."""

    def __init__(self, conn):
        self.conn = conn
        self._counter = 0

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def employer(self):
        n = self._next()
        return self.one(
            "INSERT INTO employers (name, slug) VALUES (%s, %s) RETURNING employer_id",
            (f"Employer {n}", f"employer-{n}"),
        )

    def board(self, employer_id=None):
        employer_id = employer_id or self.employer()
        n = self._next()
        return self.one(
            "INSERT INTO source_boards (employer_id, source_system, board_key, board_url) "
            "VALUES (%s, 'greenhouse', %s, %s) RETURNING board_id",
            (employer_id, f"board{n}", f"https://boards.example.com/board{n}"),
        )

    def run(self, status="complete"):
        finished = None if status == "running" else T0 + timedelta(minutes=5)
        return self.one(
            "INSERT INTO collection_runs (trigger, started_at, finished_at, status) "
            "VALUES ('scheduled', %s, %s, %s) RETURNING run_id",
            (T0, finished, status),
        )

    def scan(self, board_id, run_id=None, status="complete", postings_seen=1):
        run_id = run_id or self.run()
        finished = None if status == "running" else T0 + timedelta(minutes=1)
        return self.one(
            "INSERT INTO board_scans (run_id, board_id, started_at, finished_at, status, "
            "postings_seen) VALUES (%s, %s, %s, %s, %s, %s) RETURNING board_scan_id",
            (run_id, board_id, T0, finished, status, postings_seen),
        )

    def raw(self, scan_id, sequence_no=1):
        n = self._next()
        return self.one(
            "INSERT INTO raw_responses (board_scan_id, sequence_no, request_url, http_status, "
            "fetched_at, storage_uri, content_sha256, byte_size) "
            "VALUES (%s, %s, 'https://boards.example.com/api', 200, %s, %s, %s, 10) "
            "RETURNING raw_response_id",
            (scan_id, sequence_no, T0, f"raw/test/{n}/page-{sequence_no:03d}.json", sha(str(n))),
        )

    def posting(self, board_id, source_posting_id="gh-4471"):
        return self.one(
            "INSERT INTO source_postings (board_id, source_posting_id, first_discovered_at, "
            "last_observed_at) VALUES (%s, %s, %s, %s) RETURNING posting_id",
            (board_id, source_posting_id, T0, T0),
        )

    def version(self, posting_id, version_no=1, content="A"):
        return self.one(
            "INSERT INTO posting_versions (posting_id, version_no, content_hash, title, "
            "first_seen_at, detection, recorded_at) "
            "VALUES (%s, %s, %s, 'Data Engineer', %s, 'first_seen', %s) RETURNING version_id",
            (posting_id, version_no, sha(content), T0, T0),
        )

    def observation(self, scan_id, posting_id, version_id, raw_id, published_at=None):
        return self.one(
            "INSERT INTO posting_observations (board_scan_id, posting_id, version_id, "
            "raw_response_id, observed_at, source_published_at, source_fingerprint, "
            "processed_at, recorded_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'collection') RETURNING observation_id",
            (scan_id, posting_id, version_id, raw_id, T0, published_at, sha("A"), T0),
        )

    def chain(self):
        """A complete, valid set of rows from employer to observation."""
        board = self.board()
        scan = self.scan(board)
        raw = self.raw(scan)
        posting = self.posting(board)
        version = self.version(posting)
        observation = self.observation(scan, posting, version, raw)
        return Chain(board, scan, raw, posting, version, observation)


@dataclass(frozen=True)
class Chain:
    board: int
    scan: int
    raw: int
    posting: int
    version: int
    observation: int


@pytest.fixture
def rows(schema_conn):
    apply_migrations(schema_conn)
    return Rows(schema_conn)


def test_valid_chain_is_accepted_with_unknown_publication_time(rows):
    chain = rows.chain()
    published = rows.one(
        "SELECT source_published_at FROM posting_observations WHERE observation_id = %s",
        (chain.observation,),
    )
    assert published is None


def test_same_posting_on_same_board_is_rejected(rows):
    board = rows.board()
    rows.posting(board, "gh-1")
    with pytest.raises(errors.UniqueViolation, match="source_postings_identity_key"):
        rows.posting(board, "gh-1")


def test_same_posting_id_on_different_boards_is_allowed(rows):
    rows.posting(rows.board(), "gh-1")
    rows.posting(rows.board(), "gh-1")


def test_duplicate_board_identity_is_rejected(rows):
    employer = rows.employer()
    sql = (
        "INSERT INTO source_boards (employer_id, source_system, board_key, board_url) "
        "VALUES (%s, 'lever', 'acme', 'https://jobs.example.com/acme')"
    )
    rows.conn.execute(sql, (employer,))
    with pytest.raises(errors.UniqueViolation, match="source_boards_identity_key"):
        rows.conn.execute(sql, (employer,))


def test_unknown_source_system_is_rejected(rows):
    with pytest.raises(errors.CheckViolation):
        rows.conn.execute(
            "INSERT INTO source_boards (employer_id, source_system, board_key, board_url) "
            "VALUES (%s, 'scraped_somewhere', 'x', 'https://x.example.com')",
            (rows.employer(),),
        )


@pytest.mark.parametrize("slug", ["Acme", "acme analytics", "-acme", "acme--x", ""])
def test_invalid_employer_slug_is_rejected(rows, slug):
    with pytest.raises(errors.CheckViolation):
        rows.conn.execute("INSERT INTO employers (name, slug) VALUES ('Acme', %s)", (slug,))


@pytest.mark.parametrize("status", ["done", "closed", ""])
def test_unknown_scan_status_is_rejected(rows, status):
    with pytest.raises(errors.CheckViolation):
        rows.scan(rows.board(), status=status)


def test_running_scan_with_finish_time_is_rejected(rows):
    with pytest.raises(errors.CheckViolation, match="board_scans_finished_iff_not_running"):
        rows.conn.execute(
            "INSERT INTO board_scans (run_id, board_id, started_at, finished_at, status) "
            "VALUES (%s, %s, %s, %s, 'running')",
            (rows.run(), rows.board(), T0, T0 + timedelta(minutes=1)),
        )


def test_finished_scan_without_finish_time_is_rejected(rows):
    with pytest.raises(errors.CheckViolation, match="board_scans_finished_iff_not_running"):
        rows.conn.execute(
            "INSERT INTO board_scans (run_id, board_id, started_at, status, postings_seen) "
            "VALUES (%s, %s, %s, 'complete', 3)",
            (rows.run(), rows.board(), T0),
        )


def test_run_finishing_before_it_started_is_rejected(rows):
    with pytest.raises(errors.CheckViolation, match="collection_runs_finished_after_started"):
        rows.conn.execute(
            "INSERT INTO collection_runs (trigger, started_at, finished_at, status) "
            "VALUES ('manual', %s, %s, 'complete')",
            (T0, T0 - timedelta(seconds=1)),
        )


def test_complete_scan_must_know_its_posting_count(rows):
    with pytest.raises(errors.CheckViolation, match="board_scans_complete_has_count"):
        rows.scan(rows.board(), status="complete", postings_seen=None)


def test_failed_and_partial_scans_may_have_unknown_count(rows):
    board = rows.board()
    rows.scan(board, status="failed", postings_seen=None)
    rows.scan(board, status="partial", postings_seen=None)


def test_board_scanned_twice_in_one_run_is_rejected(rows):
    board, run = rows.board(), rows.run()
    rows.scan(board, run_id=run)
    with pytest.raises(errors.UniqueViolation, match="board_scans_run_board_key"):
        rows.scan(board, run_id=run)


def test_posting_observed_twice_in_one_scan_is_rejected(rows):
    chain = rows.chain()
    with pytest.raises(errors.UniqueViolation, match="posting_observations_scan_posting_key"):
        rows.observation(chain.scan, chain.posting, chain.version, chain.raw)


def test_observation_with_version_of_another_posting_is_rejected(rows):
    chain = rows.chain()
    other_posting = rows.posting(chain.board, "gh-other")
    other_version = rows.version(other_posting)
    second_scan = rows.scan(chain.board)
    second_raw = rows.raw(second_scan)
    with pytest.raises(errors.ForeignKeyViolation, match="posting_observations_version_fkey"):
        rows.observation(second_scan, chain.posting, other_version, second_raw)


def test_observation_with_raw_response_from_another_scan_is_rejected(rows):
    chain = rows.chain()
    second_scan = rows.scan(chain.board)
    with pytest.raises(errors.ForeignKeyViolation, match="posting_observations_raw_response_fkey"):
        rows.observation(second_scan, chain.posting, chain.version, chain.raw)


def test_content_can_return_to_an_earlier_version(rows):
    posting = rows.posting(rows.board())
    rows.version(posting, 1, "A")
    rows.version(posting, 2, "B")
    rows.version(posting, 3, "A")
    hashes = [
        h
        for (h,) in rows.conn.execute(
            "SELECT content_hash FROM posting_versions WHERE posting_id = %s ORDER BY version_no",
            (posting,),
        )
    ]
    assert hashes == [sha("A"), sha("B"), sha("A")]


def test_duplicate_version_number_is_rejected(rows):
    posting = rows.posting(rows.board())
    rows.version(posting, 1, "A")
    with pytest.raises(errors.UniqueViolation, match="posting_versions_posting_version_key"):
        rows.version(posting, 1, "B")


def test_last_observed_before_first_discovered_is_rejected(rows):
    with pytest.raises(errors.CheckViolation, match="source_postings_observed_after_discovered"):
        rows.conn.execute(
            "INSERT INTO source_postings (board_id, source_posting_id, first_discovered_at, "
            "last_observed_at) VALUES (%s, 'gh-1', %s, %s)",
            (rows.board(), T0, T0 - timedelta(hours=1)),
        )


def test_raw_response_requires_valid_fingerprint(rows):
    scan = rows.scan(rows.board())
    with pytest.raises(errors.CheckViolation):
        rows.conn.execute(
            "INSERT INTO raw_responses (board_scan_id, sequence_no, request_url, http_status, "
            "fetched_at, storage_uri, content_sha256, byte_size) "
            "VALUES (%s, 1, 'https://x.example.com', 200, %s, 'raw/x.json', 'not-a-hash', 1)",
            (scan, T0),
        )


def test_raw_response_storage_location_is_unique(rows):
    scan = rows.scan(rows.board())
    sql = (
        "INSERT INTO raw_responses (board_scan_id, sequence_no, request_url, http_status, "
        "fetched_at, storage_uri, content_sha256, byte_size) "
        "VALUES (%s, %s, 'https://x.example.com', 200, %s, 'raw/same.json', %s, 1)"
    )
    rows.conn.execute(sql, (scan, 1, T0, sha("1")))
    with pytest.raises(errors.UniqueViolation, match="raw_responses_storage_uri_key"):
        rows.conn.execute(sql, (scan, 2, T0, sha("2")))


def test_deleting_an_employer_with_history_is_refused(rows):
    chain = rows.chain()
    employer = rows.one("SELECT employer_id FROM source_boards WHERE board_id = %s", (chain.board,))
    with pytest.raises(errors.ForeignKeyViolation):
        rows.conn.execute("DELETE FROM employers WHERE employer_id = %s", (employer,))


def test_deleting_a_posting_with_observations_is_refused(rows):
    chain = rows.chain()
    with pytest.raises(errors.ForeignKeyViolation):
        rows.conn.execute("DELETE FROM source_postings WHERE posting_id = %s", (chain.posting,))


def test_identity_keys_cannot_be_set_by_hand(rows):
    with pytest.raises(errors.GeneratedAlways):
        rows.conn.execute("INSERT INTO employers (employer_id, name, slug) VALUES (999, 'X', 'x')")
