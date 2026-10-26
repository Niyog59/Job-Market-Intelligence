from pathlib import Path

import pytest
from gh_fakes import T0, board, registry

from jobmarket.migrate import apply_migrations
from jobmarket.sources import (
    DEFAULT_REGISTRY_PATH,
    RegistryError,
    load_registries,
    load_registry,
    sync_registry,
)

REPO_REGISTRY = Path(__file__).parents[1] / DEFAULT_REGISTRY_PATH


def test_project_registry_matches_research():
    reg = load_registry(REPO_REGISTRY)
    assert len(reg.boards) == 57
    assert len(reg.enabled) == 21
    assert len({b.employer_slug for b in reg.boards}) == 56  # BGE has two boards
    assert all(b.verification == "verified" for b in reg.enabled)
    assert {b.token for b in reg.boards if b.employer_slug == "bge"} == {"bgeinc", "bgeinccampus"}
    cognite = next(b for b in reg.boards if b.token == "cognite")
    assert cognite.region == "eu"
    assert cognite.board_url == "https://job-boards.eu.greenhouse.io/cognite"
    assert {lead.employer for lead in reg.leads if lead.status == "hold"} == {"HP"}


def write(tmp_path, text):
    path = tmp_path / "boards.toml"
    path.write_text(text)
    return path


BASE = """
[[boards]]
employer = "Acme"
employer_slug = "acme"
token = "acme"
region = "us"
enabled = {enabled}
verification = "{verification}"
metros = ["dfw"]
official_source = "https://acme.example/careers"
"""


def test_pending_board_cannot_be_enabled(tmp_path):
    path = write(tmp_path, BASE.format(enabled="true", verification="pending"))
    with pytest.raises(RegistryError, match="only verified boards can be enabled"):
        load_registry(path)


def test_duplicate_token_and_bad_region_rejected(tmp_path):
    text = BASE.format(enabled="false", verification="pending") * 2
    text = text.replace('region = "us"', 'region = "asia"', 1)
    with pytest.raises(RegistryError) as excinfo:
        load_registry(write(tmp_path, text))
    assert "appears more than once" in str(excinfo.value)
    assert "region must be" in str(excinfo.value)


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "missing.toml")


@pytest.mark.db
def test_sync_is_idempotent_and_never_deletes(schema_conn):
    apply_migrations(schema_conn)
    reg = registry(
        board("acme"),
        board("beta", employer="Beta"),
        board("beta-campus", employer="Beta", slug="beta"),
        board("gamma", enabled=False),
    )
    first = sync_registry(schema_conn, reg, T0)
    second = sync_registry(schema_conn, reg, T0)
    assert (first.employers_upserted, first.boards_upserted, first.boards_enabled) == (3, 4, 3)
    assert second == first

    def counts():
        return schema_conn.execute(
            "SELECT (SELECT count(*) FROM employers), (SELECT count(*) FROM source_boards), "
            "(SELECT count(*) FROM source_boards WHERE is_active)"
        ).fetchone()

    assert counts() == (3, 4, 3)

    smaller = registry(board("acme"))
    result = sync_registry(schema_conn, smaller, T0)
    assert set(result.boards_not_in_registry) == {"beta", "beta-campus"}
    assert counts() == (3, 4, 1)  # disabled, not deleted


@pytest.mark.db
def test_project_registry_syncs(schema_conn):
    apply_migrations(schema_conn)
    result = sync_registry(schema_conn, load_registry(REPO_REGISTRY), T0)
    assert (result.employers_upserted, result.boards_upserted, result.boards_enabled) == (
        56,
        57,
        21,
    )
    region, url = schema_conn.execute(
        "SELECT hosting_region, board_url FROM source_boards WHERE board_key = 'indiciumai'"
    ).fetchone()
    assert (region, url) == ("eu", "https://job-boards.eu.greenhouse.io/indiciumai")


LEVER_REGISTRY = Path(__file__).parents[1] / "config/lever_boards.toml"


def test_project_lever_registry_matches_the_recheck():
    reg = load_registry(LEVER_REGISTRY)
    assert {b.source_system for b in reg.boards} == {"lever"}
    assert len(reg.boards) == 43 and len(reg.enabled) == 30
    assert len(reg.leads) == 1 and reg.leads[0].status == "hold"
    assert reg.leads[0].tokens == ("usasurveyjob",)  # Toward Jobs stays held
    assert all(b.region == "global" for b in reg.boards)
    assert all(b.verification == "verified" and b.metros for b in reg.enabled)
    shared = next(b for b in reg.boards if b.token == "boxlunch")
    assert shared.brands == ("BoxLunch", "Hot Topic") and not shared.enabled
    assert next(b for b in reg.boards if b.token == "cgsfederal").board_url == (
        "https://jobs.lever.co/cgsfederal"
    )
    # Unresolved or conflicting mappings stay disabled.
    for token in ("zimperium", "worldwidemissioncritical", "dmautoleasing", "field-ai", "tsmg"):
        assert not next(b for b in reg.boards if b.token == token).enabled


LEVER_BASE = """
source = "lever"

[[boards]]
employer = "Lev"
employer_slug = "lev"
token = "acme"
region = "{region}"
enabled = false
verification = "pending"
metros = []
"""


def test_lever_regions_are_global_or_eu(tmp_path):
    with pytest.raises(RegistryError, match="region must be one of"):
        load_registry(write(tmp_path, LEVER_BASE.format(region="us")))
    assert load_registry(write(tmp_path, LEVER_BASE.format(region="eu"))).boards[0].board_url == (
        "https://jobs.eu.lever.co/acme"
    )


def test_one_token_may_exist_on_two_sources_but_not_twice_on_one(tmp_path):
    gh = write(tmp_path, BASE.format(enabled="false", verification="pending"))
    lv = tmp_path / "lever.toml"
    lv.write_text(LEVER_BASE.format(region="global").replace('"Lev"', '"Lev"'))
    reg = load_registries((gh, lv))
    assert sorted((b.source_system, b.token) for b in reg.boards) == [
        ("greenhouse", "acme"),
        ("lever", "acme"),
    ]
    with pytest.raises(RegistryError, match="lever token 'acme' appears more than once"):
        load_registries((gh, lv, lv))


def test_enabled_boards_need_a_metro(tmp_path):
    text = LEVER_BASE.format(region="global").replace("enabled = false", "enabled = true")
    text = text.replace('"pending"', '"verified"') + 'official_source = "https://lev.example"\n'
    with pytest.raises(RegistryError, match="enabled boards need at least one metro"):
        load_registry(write(tmp_path, text))


@pytest.mark.db
def test_both_project_registries_sync(schema_conn):
    apply_migrations(schema_conn)
    reg = load_registries((REPO_REGISTRY, LEVER_REGISTRY))
    result = sync_registry(schema_conn, reg, T0)
    assert (result.boards_upserted, result.boards_enabled) == (100, 51)
    counts = dict(
        schema_conn.execute(
            "SELECT source_system, count(*) FILTER (WHERE is_active) FROM source_boards "
            "GROUP BY source_system"
        ).fetchall()
    )
    assert counts == {"greenhouse": 21, "lever": 30}
    region, brands = schema_conn.execute(
        "SELECT hosting_region, hiring_brands FROM source_boards WHERE board_key = 'boxlunch'"
    ).fetchone()
    assert (region, brands) == ("global", ["BoxLunch", "Hot Topic"])
    # Syncing only the Greenhouse file leaves the Lever boards alone.
    sync_registry(schema_conn, load_registry(REPO_REGISTRY), T0)
    assert (
        schema_conn.execute(
            "SELECT count(*) FROM source_boards WHERE source_system = 'lever' AND is_active"
        ).fetchone()[0]
        == 30
    )
