"""Skip a Spark test module when PySpark or Java is missing. The CI job for these tests sets
JOBMARKET_REQUIRE_SPARK=1, so a broken install fails there instead of passing as skipped."""

import os
import shutil

import pytest


def require_spark() -> None:
    try:
        import pyspark  # noqa: F401
    except ImportError:
        missing = "PySpark is not installed (pip install -e '.[spark]')"
    else:
        java = shutil.which("java") or os.environ.get("JAVA_HOME")
        missing = None if java else "Spark needs Java 17 or 21"
    if missing:
        if os.environ.get("JOBMARKET_REQUIRE_SPARK") == "1":
            pytest.fail(f"JOBMARKET_REQUIRE_SPARK=1 but {missing}", pytrace=False)
        pytest.skip(missing, allow_module_level=True)
