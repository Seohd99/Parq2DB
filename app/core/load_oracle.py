"""Oracle array-bound inserts for Parquet batches."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import oracledb

from app.core.db import BatchInsertError, driver_code, reported_rows
from app.core.dialects import quote_identifier
from app.core.model import TableModel


def insert_sql(table: TableModel) -> str:
    """Build one Oracle positional-bind INSERT for a planned table."""
    quoted = table.identifier_policy == "quoted"
    name = quote_identifier(table.name, "oracle", quoted)
    columns = ", ".join(quote_identifier(column.name, "oracle", quoted) for column in table.columns)
    binds = ", ".join(f":{index}" for index in range(1, len(table.columns) + 1))
    return f"INSERT INTO {name} ({columns}) VALUES ({binds})"


def _input_sizes(table: TableModel) -> list[Any]:
    values: list[Any] = []
    for column in table.columns:
        sql_type = (column.sql_type("oracle") or "").upper()
        if "BLOB" in sql_type:
            values.append(oracledb.DB_TYPE_BLOB)
        elif "CLOB" in sql_type or column.is_json:
            values.append(oracledb.DB_TYPE_CLOB)
        elif column.logical_type == "timestamp_tz":
            values.append(oracledb.DB_TYPE_TIMESTAMP_TZ)
        else:
            values.append(None)
    return values


def insert_batch(connection: Any, table: TableModel, rows: Sequence[tuple[Any, ...]]) -> int:
    """Insert one bounded batch with array binds; return the row count or report a batch-local offset."""
    if not rows:
        return 0
    cursor = connection.cursor()
    try:
        sizes = _input_sizes(table)
        if any(value is not None for value in sizes):
            cursor.setinputsizes(*sizes)
        cursor.executemany(insert_sql(table), rows, batcherrors=True)
        get_errors = getattr(cursor, "getbatcherrors", None)
        errors = get_errors() if get_errors is not None else ()
        if errors:
            first = min(errors, key=lambda item: int(getattr(item, "offset", 0)))
            code = getattr(first, "code", None)
            if isinstance(code, int):
                code = f"ORA-{code:05d}"
            connection.rollback()
            raise BatchInsertError(int(getattr(first, "offset", 0)), code)
        return reported_rows(cursor, rows)
    except BatchInsertError:
        raise
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        raise BatchInsertError(0, driver_code("oracle", exc)) from None
    finally:
        try:
            cursor.close()
        except Exception:
            pass
