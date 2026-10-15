"""PostgreSQL executemany batches with bounded failure-row diagnosis."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.db import BatchInsertError, driver_code
from app.core.model import TableModel


def _identifier(name: str, quoted: bool) -> str:
    return '"' + name.replace('"', '""') + '"' if quoted else name


def insert_sql(table: TableModel) -> str:
    """Build a PostgreSQL positional-placeholder INSERT statement."""
    quoted = table.identifier_policy == "quoted"
    name = _identifier(table.name, quoted)
    columns = ", ".join(_identifier(column.name, quoted) for column in table.columns)
    binds = ", ".join("%s" for _ in table.columns)
    return f"INSERT INTO {name} ({columns}) VALUES ({binds})"


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


def insert_batch(connection: Any, table: TableModel, rows: Sequence[tuple[Any, ...]]) -> None:
    """Insert a single bounded batch and locate a failing row if needed."""
    if not rows:
        return
    cursor = connection.cursor()
    sql = insert_sql(table)
    try:
        cursor.executemany(sql, rows)
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
