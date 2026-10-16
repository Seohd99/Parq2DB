"""PostgreSQL COPY batches with an INSERT fallback and bounded failure-row diagnosis."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.db import BatchInsertError, driver_code, reported_rows
from app.core.dialects import quote_identifier
from app.core.model import TableModel


# SQLSTATE feature_not_supported: PostgreSQL refuses COPY FROM on tables with
# row-level security (and on views), while INSERT into them is allowed.
COPY_NOT_SUPPORTED = "0A000"


def _target(table: TableModel) -> tuple[str, str]:
    quoted = table.identifier_policy == "quoted"
    name = quote_identifier(table.name, "postgres", quoted)
    columns = ", ".join(quote_identifier(column.name, "postgres", quoted) for column in table.columns)
    return name, columns


def insert_sql(table: TableModel) -> str:
    """Build a PostgreSQL positional-placeholder INSERT statement."""
    name, columns = _target(table)
    binds = ", ".join("%s" for _ in table.columns)
    return f"INSERT INTO {name} ({columns}) VALUES ({binds})"


def copy_sql(table: TableModel) -> str:
    """Build a text-format COPY ... FROM STDIN statement for one planned table."""
    name, columns = _target(table)
    return f"COPY {name} ({columns}) FROM STDIN"


def _first_failing_row(connection: Any, sql: str, rows: Sequence[tuple[Any, ...]]) -> int:
    """Probe a rolled-back failed batch and return its first bad row offset."""
    cursor = connection.cursor()
    try:
        for offset, row in enumerate(rows):
            savepoint = f"PB_DIAGNOSTIC_{offset}"
            cursor.execute(f"SAVEPOINT {savepoint}")
            try:
                cursor.execute(sql, row)
            except Exception:
                try:
                    cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                except Exception:
                    return offset
                return offset
            try:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                return offset
        return 0
    except Exception:
        return 0
    finally:
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            cursor.close()
        except Exception:
            pass


def insert_batch(connection: Any, table: TableModel, rows: Sequence[tuple[Any, ...]]) -> int:
    """Copy one bounded batch, return the driver's row count, and locate a failing row if needed."""
    if not rows:
        return 0
    cursor = connection.cursor()
    sql = insert_sql(table)
    try:
        try:
            with cursor.copy(copy_sql(table)) as copy:
                for row in rows:
                    copy.write_row(row)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != COPY_NOT_SUPPORTED:
                raise
            connection.rollback()
            cursor.executemany(sql, rows)
        return reported_rows(cursor, rows)
    except Exception as exc:
        code = driver_code("postgres", exc)
        try:
            connection.rollback()
        except Exception:
            pass
        offset = _first_failing_row(connection, sql, rows)
        raise BatchInsertError(offset, code) from None
    finally:
        try:
            cursor.close()
        except Exception:
            pass
