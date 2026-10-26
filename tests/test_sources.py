from pathlib import Path

import pytest
from gh_fakes import T0, board, registry

from jobmarket.migrate import apply_migrations
from jobmarket.sources import DEFAULT_REGISTRY_PATH, RegistryError, load_registry, sync_registry

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
