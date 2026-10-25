import os

import pytest

from jobmarket.config import ConfigError, Settings, load_settings

REQUIRED = {"DB_NAME": "job_market", "DB_USER": "app"}


def test_defaults_when_only_required_values_set():
    settings = load_settings(REQUIRED)
    assert settings == Settings(
        db_host="localhost", db_port=5432, db_name="job_market", db_user="app", db_password=""
    )


def test_all_values_read_and_port_converted_to_int():
    settings = load_settings(
        {
            "DB_HOST": "db.internal",
            "DB_PORT": "6543",
            "DB_NAME": "jobs",
            "DB_USER": "reader",
            "DB_PASSWORD": "pw",
        }
    )
    assert settings.db_host == "db.internal"
    assert settings.db_port == 6543
    assert isinstance(settings.db_port, int)
    assert settings.db_name == "jobs"
    assert settings.db_user == "reader"
    assert settings.db_password == "pw"


def test_surrounding_whitespace_is_ignored_except_in_password():
    settings = load_settings(
        {
            "DB_HOST": " h ",
            "DB_PORT": " 5433 ",
            "DB_NAME": " n ",
            "DB_USER": " u ",
            "DB_PASSWORD": " pw ",
        }
    )
    assert (settings.db_host, settings.db_port, settings.db_name, settings.db_user) == (
        "h",
        5433,
        "n",
        "u",
    )
    assert settings.db_password == " pw "


def test_empty_optional_values_fall_back_to_defaults():
    settings = load_settings({**REQUIRED, "DB_HOST": "", "DB_PORT": "  "})
    assert settings.db_host == "localhost"
    assert settings.db_port == 5432


@pytest.mark.parametrize("missing", ["DB_NAME", "DB_USER"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_required_value_raises(missing, value):
    environ = dict(REQUIRED)
    if value is None:
        del environ[missing]
    else:
        environ[missing] = value
    with pytest.raises(ConfigError, match=f"{missing} is required"):
        load_settings(environ)


@pytest.mark.parametrize("port", ["abc", "54.32", "-1", "0", "65536", "+5432", "5_432", "٥٤٣٢"])
def test_invalid_port_raises(port):
    with pytest.raises(ConfigError, match="DB_PORT"):
        load_settings({**REQUIRED, "DB_PORT": port})


@pytest.mark.parametrize("port", ["1", "65535"])
def test_port_boundaries_accepted(port):
    assert load_settings({**REQUIRED, "DB_PORT": port}).db_port == int(port)


def test_all_problems_reported_together():
    with pytest.raises(ConfigError) as excinfo:
        load_settings({"DB_PORT": "abc"})
    message = str(excinfo.value)
    assert "DB_PORT" in message
    assert "DB_NAME is required" in message
    assert "DB_USER is required" in message


def test_password_never_appears_in_repr_or_errors():
    secret = "s3cret-value"
    settings = load_settings({**REQUIRED, "DB_PASSWORD": secret})
    assert secret not in repr(settings)
    assert secret not in str(settings)

    with pytest.raises(ConfigError) as excinfo:
        load_settings({"DB_PASSWORD": secret, "DB_PORT": "bad"})
    assert secret not in str(excinfo.value)


def test_environment_overrides_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DB_NAME=from_file\nDB_USER=file_user\nDB_PORT=1111\n")
    monkeypatch.chdir(tmp_path)
    # load_dotenv() writes into os.environ; give it a throwaway copy so nothing leaks
    # into later tests.
    environ = {k: v for k, v in os.environ.items() if not k.startswith("DB_")}
    environ["DB_NAME"] = "from_env"
    monkeypatch.setattr(os, "environ", environ)

    settings = load_settings()

    assert settings.db_name == "from_env"
    assert settings.db_user == "file_user"
    assert settings.db_port == 1111


def test_connection_kwargs_omits_empty_password():
    kwargs = load_settings(REQUIRED).connection_kwargs()
    assert kwargs == {"host": "localhost", "port": 5432, "dbname": "job_market", "user": "app"}

    with_password = load_settings({**REQUIRED, "DB_PASSWORD": "pw"}).connection_kwargs()
    assert with_password["password"] == "pw"
