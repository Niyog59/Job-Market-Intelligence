"""Command-line entry point: ``jobmarket <command>``."""

import argparse
import sys
from collections.abc import Sequence

import psycopg

from jobmarket import db
from jobmarket.config import ConfigError, Settings, load_settings
from jobmarket.migrate import MigrationError, apply_migrations, migration_status

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2


def _describe(settings: Settings) -> str:
    return f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"


def _check_db(settings: Settings) -> int:
    try:
        info = db.check_connection(settings)
    except psycopg.Error as exc:
        print(f"Database check failed for {_describe(settings)}: {exc}".rstrip(), file=sys.stderr)
        return EXIT_FAILURE
    print(
        f"OK: connected to database {info.database!r} as {info.user!r} "
        f"(PostgreSQL {info.server_version})"
    )
    return EXIT_OK


def _migrate(settings: Settings, status_only: bool) -> int:
    try:
        with db.connect(settings, autocommit=True) as conn:
            if status_only:
                for item in migration_status(conn):
                    state = item.applied_at.isoformat() if item.applied_at else "pending"
                    print(f"{item.filename}  {state}")
                return EXIT_OK
            applied = apply_migrations(conn)
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except psycopg.Error as exc:
        print(f"Database error for {_describe(settings)}: {exc}".rstrip(), file=sys.stderr)
        return EXIT_FAILURE

    if not applied:
        print("Database schema is up to date.")
    for filename in applied:
        print(f"Applied {filename}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobmarket", description="Job Market Intelligence")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-db", help="connect to PostgreSQL and run SELECT 1")
    migrate = commands.add_parser("migrate", help="apply pending database migrations")
    migrate.add_argument(
        "--status", action="store_true", help="list migrations and whether they are applied"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.command == "check-db":
        return _check_db(settings)
    if args.command == "migrate":
        return _migrate(settings, status_only=args.status)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
