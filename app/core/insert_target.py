"""Match a Parquet file's top-level columns to an existing table for INSERT.

INSERT into an existing table needs no DDL and no string-length scan: the table
already fixes the column types. Columns match by name, exactly first and then
ignoring case, and the load uses the table's stored names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import DbProfile
from app.core.db import close_quietly, connect, describe_columns, find_table
from app.core.errors import PBError, warning
from app.core.flatten import plan_tables
from app.core.model import StringLengthMeasurements, TableModel
from app.core.typemap import container_kind

# plan_tables validates a table name; the stored name replaces this placeholder.
_PLACEHOLDER_TABLE = "PB_INSERT_TARGET"


def match_columns(schema: pa.Schema, db_columns: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pair source fields with insertable table columns by name.

    Returns ``columns`` (every table column in table order with its matched ``source``
    or ``None``) and ``file_only`` (source fields with no table column).
    """
    insertable = [column for column in db_columns if not column["generated"]]
    used: set[str] = set()
    source_for: dict[str, str] = {}
    unmatched: list[pa.Field] = []
    by_name = {column["name"]: column for column in insertable}
    for field in schema:
        column = by_name.get(field.name)
        if column is not None and column["name"] not in used:
            used.add(column["name"])
            source_for[column["name"]] = field.name
        else:
            unmatched.append(field)
    file_only: list[dict[str, str]] = []
    for field in unmatched:
        candidates = [
            column for column in insertable
            if column["name"] not in used and column["name"].casefold() == field.name.casefold()
        ]
        if len(candidates) == 1:
            used.add(candidates[0]["name"])
            source_for[candidates[0]["name"]] = field.name
        else:
            file_only.append({"source": field.name, "kind": container_kind(field.type) or "scalar"})
    kinds = {field.name: container_kind(field.type) or "scalar" for field in schema}
    columns = []
    for column in db_columns:
        source = source_for.get(column["name"])
        columns.append({
            **column,
            "source": source,
            "source_kind": kinds[source] if source is not None else None,
            "required": not column["nullable"] and not column["has_default"] and not column["generated"],
        })
    return {"columns": columns, "file_only": file_only}


def selected_matches(match: dict[str, Any], columns: Sequence[str] | None) -> list[dict[str, Any]]:
    """Return matched table columns limited to the chosen source columns; ``None`` keeps all."""
    matched = [column for column in match["columns"] if column["source"] is not None]
    if not matched:
        raise PBError("E_INSERT_NO_MATCHING_COLUMNS")
    if columns is None:
        return matched
    if not columns:
        raise PBError("E_COLUMNS_EMPTY")
    sources = {column["source"] for column in matched}
    for name in columns:
        if name not in sources:
            raise PBError("E_INSERT_COLUMN_NOT_MATCHED", column=name)
    wanted = set(columns)
    return [column for column in matched if column["source"] in wanted]


def insert_warnings(table: str, match: dict[str, Any], chosen: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Warn about file columns left out and required table columns that get no value."""
    warnings: list[dict[str, Any]] = []
    if match["file_only"]:
        warnings.append(warning(
            "W_INSERT_FILE_COLUMNS_SKIPPED",
            table=table,
            columns=[item["source"] for item in match["file_only"]],
        ))
    filled = {column["name"] for column in chosen}
    # A trigger may still fill these, so this is a warning rather than an error.
    required = [column["name"] for column in match["columns"] if column["required"] and column["name"] not in filled]
    if required:
        warnings.append(warning("W_INSERT_REQUIRED_COLUMNS", table=table, columns=required))
    return warnings


def plan_insert_table(
    schema: pa.Schema,
    *,
    table_name: str,
    dialect: str,
    chosen: Sequence[dict[str, Any]],
) -> TableModel:
    """Plan one plain table that reads the chosen sources and names the stored columns."""
    target_by_source = {column["source"]: column for column in chosen}
    (planned,) = plan_tables(
        schema,
        table_name=_PLACEHOLDER_TABLE,
        mode="none",
        dialect=dialect,
        column_policy="quoted",
        string_strategy="max",
        lengths=StringLengthMeasurements(),
        columns=list(target_by_source),
    )
    columns = []
    for column in planned.columns:
        target = target_by_source[column.original_name]
        # The stored type drives driver binds such as Oracle LOB input sizes.
        columns.append(replace(column, name=target["name"], **{f"sql_type_{dialect}": target["data_type"]}))
    return replace(planned, name=table_name, columns=tuple(columns), identifier_policy="quoted", warnings=())


def inspect_insert_target(
    path: Path,
    profile: DbProfile,
    table_name: str,
    columns: Sequence[str] | None = None,
) -> tuple[TableModel | None, dict[str, Any], int]:
    """Read the target table and return the insert plan, a public report, and source rows.

    The plan is ``None`` when no file column matches a table column, so the report can
    still show the table.
    """
    if not table_name or not table_name.strip():
        raise PBError("E_BAD_REQUEST", fields=["table_name"])
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
        source_rows = parquet.metadata.num_rows
    connection = connect(profile)
    try:
        stored = find_table(connection, profile, table_name.strip())
        db_columns = describe_columns(connection, profile, stored)
    finally:
        close_quietly(connection)
    match = match_columns(schema, db_columns)
    report = {
        "dialect": profile.kind,
        "table": stored,
        "columns": match["columns"],
        "file_only": match["file_only"],
        "source_rows": source_rows,
    }
    if not any(column["source"] is not None for column in match["columns"]):
        report["warnings"] = insert_warnings(stored, match, [])
        return None, report, source_rows
    chosen = selected_matches(match, columns)
    report["warnings"] = insert_warnings(stored, match, chosen)
    model = plan_insert_table(schema, table_name=stored, dialect=profile.kind, chosen=chosen)
    return model, report, source_rows
