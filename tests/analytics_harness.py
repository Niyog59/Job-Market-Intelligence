"""Build the dbt analytics project over a throwaway ingestion schema.

The ingestion tables live in the test's isolated schema; dbt builds into
<schema>_analytics_staging, <schema>_analytics_intermediate, and <schema>_analytics_reporting,
which are dropped afterwards. dbt connects with the DB_* login (target "owner").
"""

import pytest

from jobmarket import db
from jobmarket.analytics import run_dbt

pytest.importorskip("dbt.cli.main", reason="dbt is not installed (pip install -e '.[analytics]')")

LAYERS = ("analytics_staging", "analytics_intermediate", "analytics_reporting")


class Analytics:
    def __init__(self, conn, source_schema: str):
        self.conn, self.source = conn, source_schema
        self.prefix = f"{source_schema}_"

    def schema(self, layer: str) -> str:
        return f"{self.prefix}analytics_{layer}"

    def build(self, *args: str):
        outcome = run_dbt(
            ["build", *args],
            target="owner",
            variables={"source_schema": self.source, "schema_prefix": self.prefix},
            quiet=True,
        )
        assert outcome.success, "\n".join(outcome.failures) or "dbt build failed"
        return outcome

    def rows(self, layer: str, sql: str, params=()):
        """Rows from `sql`, where {t} is replaced by the layer's schema."""
        return self.conn.execute(sql.format(t=self.schema(layer)), params).fetchall()

    def drop(self, db_settings) -> None:
        with db.connect(db_settings, autocommit=True) as admin:
            for layer in LAYERS:
                admin.execute(f'DROP SCHEMA IF EXISTS "{self.prefix}{layer}" CASCADE')
