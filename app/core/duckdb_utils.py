"""DuckDB connection helpers shared by inspection, preview, and load paths."""

from __future__ import annotations

from os import PathLike
from typing import Any

import duckdb
import pyarrow as pa

from app.core.errors import PBError

_GLOB_CHARACTERS = frozenset("*?[]")


def connect_utc(*, database: str = ":memory:") -> Any:
    """Create a DuckDB connection whose timestamp behavior is independent of host TZ."""
    connection = duckdb.connect(database=database)
    try:
        connection.execute("SET TimeZone='UTC'")
    except Exception:
        connection.close()
        raise
    return connection


def literal_path(path: str | PathLike[str]) -> str:
    """Return a read_parquet argument that matches exactly this one file.

    DuckDB expands glob syntax in file paths, so a validated path such as
    ``root/[ab]/f.parquet`` would otherwise read ``root/a/f.parquet``. Wrapping
    each glob character in a one-character class makes it match only itself.
    """
    return "".join(f"[{char}]" if char in _GLOB_CHARACTERS else char for char in str(path))


def quote_identifier(value: str) -> str:
    """Quote a DuckDB identifier."""
    return '"' + value.replace('"', '""') + '"'


def source_aliases(column_count: int) -> tuple[str, ...]:
    """Return positional internal names for a file's top-level columns.

    DuckDB resolves column names case-insensitively, so ``A`` and ``a`` in one
    file would both resolve to ``A``. Renaming every column by position with
    ``AS src(<aliases>)`` keeps each source column distinct.
    """
    return tuple(f"__pb_src_{index}" for index in range(column_count))


def alias_list(aliases: tuple[str, ...]) -> str:
    """Render a table-alias column list such as ``("__pb_src_0", "__pb_src_1")``."""
    return "(" + ", ".join(quote_identifier(alias) for alias in aliases) + ")" if aliases else ""


def validate_source_schema(schema: pa.Schema) -> None:
    """Reject field names that DuckDB cannot tell apart.

    Top-level columns are read by position, so only exact duplicates are a problem
    there. DuckDB matches struct fields case-insensitively and renames case-only
    duplicates (``x`` becomes ``x_1``), which would silently read or serialize the
    wrong field, so nested case-only duplicates are rejected.
    """
    seen: set[str] = set()
    for field in schema:
        if field.name in seen:
            raise PBError("E_SCHEMA_NAME_COLLISION", path=field.name)
        seen.add(field.name)

    def visit(data_type: pa.DataType, path: str) -> None:
        if pa.types.is_struct(data_type):
            folded: set[str] = set()
            for child in data_type:
                key = child.name.casefold()
                child_path = f"{path}.{child.name}"
                if key in folded:
                    raise PBError("E_SCHEMA_NAME_COLLISION", path=child_path)
                folded.add(key)
                visit(child.type, child_path)
        elif pa.types.is_list(data_type) or pa.types.is_large_list(data_type) or pa.types.is_fixed_size_list(data_type):
            visit(data_type.value_type, f"{path}[]")
        elif pa.types.is_map(data_type):
            visit(data_type.key_type, f"{path}.key")
            visit(data_type.item_type, f"{path}.value")

    for field in schema:
        visit(field.type, field.name)
