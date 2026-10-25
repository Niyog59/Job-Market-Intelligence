import pytest

from jobmarket import cli, db

pytestmark = pytest.mark.db


def test_check_connection_returns_server_details(db_settings):
    info = db.check_connection(db_settings)
    assert info.database == db_settings.db_name
    assert info.user == db_settings.db_user
    assert info.server_version


def test_cli_check_db_succeeds(db_settings, capsys):
    assert cli.main(["check-db"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("OK: connected to database")
    if db_settings.db_password:
        assert db_settings.db_password not in out


def test_cli_migrate_applies_then_reports_up_to_date(isolated_schema, monkeypatch, capsys):
    # libpq reads PGOPTIONS, so the command works inside the temporary schema only.
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={isolated_schema}")

    assert cli.main(["migrate", "--status"]) == cli.EXIT_OK
    assert "0001_create_core_tables.sql  pending" in capsys.readouterr().out

    assert cli.main(["migrate"]) == cli.EXIT_OK
    assert "Applied 0001_create_core_tables.sql" in capsys.readouterr().out

    assert cli.main(["migrate"]) == cli.EXIT_OK
    assert "Database schema is up to date." in capsys.readouterr().out

    assert cli.main(["migrate", "--status"]) == cli.EXIT_OK
    assert "pending" not in capsys.readouterr().out
