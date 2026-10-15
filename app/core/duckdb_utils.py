"""DuckDB connection helpers shared by inspection, preview, and load paths."""

from __future__ import annotations

from typing import Any

import duckdb


def connect_utc(*, database: str = ":memory:") -> Any:
    """Create a DuckDB connection whose timestamp behavior is independent of host TZ."""
    connection = duckdb.connect(database=database)
    try:
        connection.execute("SET TimeZone='UTC'")
    except Exception:
        connection.close()
        raise
    return connection
