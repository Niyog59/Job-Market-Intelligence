"""Copy the published dashboard tables to a Google Sheet that Tableau Public reads.

Tableau Public cannot connect to the database, but it can keep a workbook in sync with a
Google Sheet (it re-reads the Sheet about once a day). After each report publication the
pipeline runs `jobmarket tableau sync`, which writes one tab per table below.

Only published reporting tables (analytics_reporting) are read, through the read-only
analytics login, so nothing from the private schema can reach the Sheet. All tabs are
replaced in a single batchUpdate request, which Google applies all or nothing: the Sheet
always holds one complete publication, the previous one if the request fails.

Settings: JOBMARKET_SHEETS_ID (the spreadsheet id from its URL) and
JOBMARKET_SHEETS_KEY_FILE (a Google service account key, JSON). The service account needs
editor access to that one spreadsheet and nothing else.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from jobmarket.reports import REPORTING_SCHEMA

SHEETS_ID_ENV = "JOBMARKET_SHEETS_ID"
KEY_FILE_ENV = "JOBMARKET_SHEETS_KEY_FILE"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
CHICAGO = ZoneInfo("America/Chicago")
# Google allows 10 million cells per spreadsheet; refuse well before that.
MAX_CELLS = 5_000_000
REQUEST_TIMEOUT_SECONDS = 120
SHEETS_EPOCH = date(1899, 12, 30)  # day 0 of Google Sheets date serial numbers

# Tab name -> (table, filter, order). Every table is a published reporting table.
TABS: dict[str, tuple[str, str, str]] = {
    "jobs": ("rpt_jobs_current", "", "employer_name, title, job_key"),
    "job_skills": ("rpt_job_skills", "", "job_key, skill, requirement"),
    "skill_pairs": ("rpt_skill_pairs", "", "skill_a, skill_b, role_group"),
    "weekly_jobs": ("rpt_weekly_jobs", "", "week_start, level_rule, level, role_group, metro"),
    "weekly_skills": ("rpt_weekly_skills", "", "week_start, skill, role_group"),
    "daily_activity": ("rpt_daily_activity", "population = 'retained'", "report_day"),
    "eligibility": ("rpt_eligibility", "population = 'retained'", "concept, status"),
    "pipeline_health": ("rpt_pipeline_health_daily", "", "report_day"),
}


class SheetsNotConfigured(Exception):
    """JOBMARKET_SHEETS_ID or the service account key is missing."""


class SheetsError(Exception):
    """The Google Sheets API refused or failed a request."""


@dataclass(frozen=True)
class SheetsConfig:
    spreadsheet_id: str
    key_file: str


def load_sheets_config(environ: Mapping[str, str] | None = None) -> SheetsConfig:
    env = os.environ if environ is None else environ
    spreadsheet_id = env.get(SHEETS_ID_ENV, "").strip()
    key_file = env.get(KEY_FILE_ENV, "").strip()
    missing = [name for name, value in ((SHEETS_ID_ENV, spreadsheet_id),
                                        (KEY_FILE_ENV, key_file)) if not value]  # fmt: skip
    if missing:
        raise SheetsNotConfigured(f"set {' and '.join(missing)} to sync the dashboard Sheet")
    if not os.path.isfile(key_file):
        raise SheetsNotConfigured(f"the service account key file {key_file} does not exist")
    return SheetsConfig(spreadsheet_id, key_file)


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[tuple]


def _when(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    t = value.astimezone(CHICAGO)
    return f"{t:%b} {t.day}, {t.year}, {t.hour % 12 or 12}:{t:%M} {t:%p} {t:%Z}"


def read_tables(conn: psycopg.Connection, schema: str = REPORTING_SCHEMA) -> list[Table]:
    """The "about" tab (build time and the data-pull caption) and every tab in TABS."""
    built = conn.execute(sql.SQL(
        "SELECT built_at, latest_complete_collection, latest_extraction FROM {}.rpt_build_info"
    ).format(sql.Identifier(schema))).fetchone()  # fmt: skip
    totals = conn.execute(sql.SQL(
        "SELECT count(*), count(DISTINCT employer_name), "
        "count(*) FILTER (WHERE extracted) FROM {}.rpt_jobs_current"
    ).format(sql.Identifier(schema))).fetchone()  # fmt: skip
    about = Table(
        "about",
        ["built_at", "latest_complete_collection", "latest_extraction", "data_pull_caption",
         "current_jobs", "employers", "jobs_with_ai_details"],
        [(*built, f"Data collected through {_when(built[1])} · reports built "
                  f"{_when(built[0])}", *totals)],
    )  # fmt: skip
    tables = [about]
    for name, (table, where, order) in TABS.items():
        query = sql.SQL("SELECT * FROM {}.{} {} ORDER BY {}").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.SQL(f"WHERE {where}") if where else sql.SQL(""), sql.SQL(order),
        )  # fmt: skip
        cur = conn.execute(query)
        tables.append(Table(name, [c.name for c in cur.description], cur.fetchall()))
    return tables


def _dated(serial: float, kind: str, pattern: str) -> dict:
    return {
        "userEnteredValue": {"numberValue": serial},
        "userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}},
    }


def cell(value: Any) -> dict:
    """One cell for updateCells: numbers, booleans, and text as they are; dates and times as
    Sheets date serial numbers with a date format, so Tableau reads them as dates."""
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, datetime):
        local = value.astimezone(CHICAGO).replace(tzinfo=None)
        days = (local.date() - SHEETS_EPOCH).days
        seconds = local.hour * 3600 + local.minute * 60 + local.second
        return _dated(days + seconds / 86400, "DATE_TIME", "yyyy-mm-dd hh:mm:ss")
    if isinstance(value, date):
        return _dated((value - SHEETS_EPOCH).days, "DATE", "yyyy-mm-dd")
    if isinstance(value, int | float | Decimal):
        return {"userEnteredValue": {"numberValue": float(value)}}
    if isinstance(value, list | tuple):
        return {"userEnteredValue": {"stringValue": ", ".join(str(v) for v in value)}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def replace_requests(tables: list[Table], sheet_ids: dict[str, int]) -> list[dict]:
    """batchUpdate requests that make each tab exactly its table: resize the grid to the
    table (which deletes leftover rows and columns), then write the header and every cell."""
    requests = []
    for table in tables:
        sheet_id = sheet_ids[table.name]
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {
                "rowCount": max(len(table.rows) + 1, 2),
                "columnCount": max(len(table.columns), 1),
                "frozenRowCount": 1}},
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount)"}})  # fmt: skip
        rows = [{"values": [cell(c) for c in table.columns]}]
        rows += [{"values": [cell(v) for v in row]} for row in table.rows]
        requests.append({"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
            "rows": rows,
            "fields": "userEnteredValue,userEnteredFormat.numberFormat"}})  # fmt: skip
    return requests


class SheetsApi:
    """The two Sheets API calls the sync needs, authenticated as the service account."""

    def __init__(self, config: SheetsConfig, session=None):
        self.spreadsheet_id = config.spreadsheet_id
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                config.key_file, scopes=[SCOPE]
            )
            session = AuthorizedSession(credentials)
        self.session = session

    def _call(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS,
                                            **kwargs)  # fmt: skip
        except Exception as exc:  # network errors and token refresh failures
            raise SheetsError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise SheetsError(f"Google Sheets API {response.status_code}: "
                              f"{response.text[:300]}")  # fmt: skip
        return response.json()

    def tabs(self) -> dict[str, int]:
        data = self._call("GET", f"{API}/{self.spreadsheet_id}",
                          params={"fields": "sheets.properties(sheetId,title)"})  # fmt: skip
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in data["sheets"]}

    def batch_update(self, requests: list[dict]) -> dict:
        return self._call("POST", f"{API}/{self.spreadsheet_id}:batchUpdate",
                          json={"requests": requests})  # fmt: skip


@dataclass(frozen=True)
class SyncResult:
    tabs: dict[str, int]  # tab -> data rows written
    cells: int


def sync(conn: psycopg.Connection, api: SheetsApi, schema: str = REPORTING_SCHEMA) -> SyncResult:
    tables = read_tables(conn, schema)
    cells = sum((len(t.rows) + 1) * max(len(t.columns), 1) for t in tables)
    if cells > MAX_CELLS:
        raise SheetsError(f"{cells:,} cells is more than this sync writes ({MAX_CELLS:,})")
    existing = api.tabs()
    missing = [t.name for t in tables if t.name not in existing]
    if missing:
        # New tabs are created first; a failure later leaves them empty, never half-written.
        api.batch_update([{"addSheet": {"properties": {"title": name}}} for name in missing])
        existing = api.tabs()
    api.batch_update(replace_requests(tables, existing))
    return SyncResult({t.name: len(t.rows) for t in tables}, cells)
