"""The Google Sheet behind the Tableau Public dashboards (jobmarket.sheets). A fake Sheets API
records the requests; nothing contacts Google."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from jobmarket import sheets


class FakeApi:
    def __init__(self, tabs=None, fail_on_update=False):
        self.existing = dict(tabs or {"Sheet1": 0})
        self.batches = []
        self.fail_on_update = fail_on_update

    def tabs(self):
        return dict(self.existing)

    def batch_update(self, requests):
        if self.fail_on_update and any("updateCells" in r for r in requests):
            raise sheets.SheetsError("Google Sheets API 503: unavailable")
        self.batches.append(requests)
        for r in requests:
            if "addSheet" in r:
                self.existing[r["addSheet"]["properties"]["title"]] = len(self.existing) + 10
        return {}


def test_cells_keep_their_types_and_dates_become_sheet_dates():
    assert sheets.cell(None) == {}
    assert sheets.cell(True) == {"userEnteredValue": {"boolValue": True}}
    assert sheets.cell(Decimal("33.9")) == {"userEnteredValue": {"numberValue": 33.9}}
    assert sheets.cell("SQL") == {"userEnteredValue": {"stringValue": "SQL"}}
    day = sheets.cell(date(2026, 11, 2))
    assert day["userEnteredValue"]["numberValue"] == 46328  # days since 1899-12-30
    assert day["userEnteredFormat"]["numberFormat"]["type"] == "DATE"
    # 18:00 UTC on Nov 2, 2026 is noon in Chicago (CST): half a day after the date serial.
    noon = sheets.cell(datetime(2026, 11, 2, 18, 0, tzinfo=UTC))
    assert noon["userEnteredValue"]["numberValue"] == 46328.5


def test_each_tab_is_resized_then_rewritten_completely():
    table = sheets.Table("jobs", ["job_key", "title"], [("a", "Data Analyst"), ("b", "SWE")])
    requests = sheets.replace_requests([table], {"jobs": 7})
    resize, write = requests
    grid = resize["updateSheetProperties"]["properties"]["gridProperties"]
    assert (grid["rowCount"], grid["columnCount"], grid["frozenRowCount"]) == (3, 2, 1)
    rows = write["updateCells"]["rows"]
    assert rows[0]["values"][1] == {"userEnteredValue": {"stringValue": "title"}}
    assert len(rows) == 3 and write["updateCells"]["start"]["sheetId"] == 7


def test_only_published_reporting_tables_are_sent():
    assert all(table.startswith("rpt_") for table, _, _ in sheets.TABS.values())


def test_configuration_is_required(tmp_path):
    with pytest.raises(sheets.SheetsNotConfigured, match="JOBMARKET_SHEETS_ID"):
        sheets.load_sheets_config({})
    with pytest.raises(sheets.SheetsNotConfigured, match="does not exist"):
        missing = str(tmp_path / "missing.json")
        sheets.load_sheets_config({sheets.SHEETS_ID_ENV: "abc", sheets.KEY_FILE_ENV: missing})
    key = tmp_path / "key.json"
    key.write_text("{}")
    config = sheets.load_sheets_config({sheets.SHEETS_ID_ENV: " abc ",
                                        sheets.KEY_FILE_ENV: str(key)})  # fmt: skip
    assert config.spreadsheet_id == "abc"


@pytest.fixture(scope="module")
def reporting(world):
    world.analytics.build("--select", "+path:models/reporting", "+srv_postings", "+srv_boards")
    return world.analytics.schema("reporting")


@pytest.mark.db
def test_sync_writes_every_tab_in_one_request(world, reporting):
    api = FakeApi()
    result = sheets.sync(world.conn, api, reporting)
    assert set(result.tabs) == {"about", *sheets.TABS}
    assert result.tabs["jobs"] == 4 and result.tabs["about"] == 1
    adds, replace = api.batches  # tabs created first, then one request with every tab
    assert len(adds) == len(result.tabs)
    assert sum("updateCells" in r for r in replace) == len(result.tabs)
    about = next(r["updateCells"] for r in replace if "updateCells" in r
                 and r["updateCells"]["start"]["sheetId"] == api.existing["about"])  # fmt: skip
    caption = about["rows"][1]["values"][3]["userEnteredValue"]["stringValue"]
    assert caption.startswith("Data collected through ") and "reports built" in caption

    again = FakeApi(api.existing)
    sheets.sync(world.conn, again, reporting)
    assert len(again.batches) == 1  # tabs exist now: a single all-or-nothing request


@pytest.mark.db
def test_a_failed_update_raises_and_writes_nothing(world, reporting):
    api = FakeApi({name: i for i, name in enumerate(["about", *sheets.TABS])},
                  fail_on_update=True)  # fmt: skip
    with pytest.raises(sheets.SheetsError, match="503"):
        sheets.sync(world.conn, api, reporting)
    assert api.batches == []


@pytest.mark.db
def test_too_many_cells_are_refused(world, reporting, monkeypatch):
    monkeypatch.setattr(sheets, "MAX_CELLS", 10)
    with pytest.raises(sheets.SheetsError, match="more than this sync writes"):
        sheets.sync(world.conn, FakeApi(), reporting)
