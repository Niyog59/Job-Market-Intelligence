import socket

import pytest

from jobmarket import cli

DB_VARS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Run from an empty directory with no DB_* variables, so no real .env is read."""
    monkeypatch.chdir(tmp_path)
    for name in DB_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_check_db_reports_config_error(clean_env, capsys):
    exit_code = cli.main(["check-db"])
    assert exit_code == cli.EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "DB_NAME is required" in err


def test_check_db_fails_cleanly_when_server_unreachable(clean_env, capsys):
    secret = "s3cret-value"
    clean_env.setenv("DB_HOST", "127.0.0.1")
    clean_env.setenv("DB_PORT", str(_unused_local_port()))
    clean_env.setenv("DB_NAME", "job_market")
    clean_env.setenv("DB_USER", "app")
    clean_env.setenv("DB_PASSWORD", secret)

    exit_code = cli.main(["check-db"])

    assert exit_code == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert "Database check failed for app@127.0.0.1" in err
    assert secret not in out + err


def test_unknown_command_exits_with_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["no-such-command"])
    assert excinfo.value.code == 2
