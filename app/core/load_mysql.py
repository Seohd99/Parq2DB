"""MySQL multi-row INSERT batches using PyMySQL executemany."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.db import BatchInsertError, driver_code
from app.core.errors import PBError
from app.core.model import TableModel


def _identifier(name: str, quoted: bool) -> str:
    return "`" + name.replace("`", "``") + "`" if quoted else name


def insert_sql(table: TableModel) -> str:
    """Build a MySQL multi-VALUES template for one planned table."""
    quoted = table.identifier_policy == "quoted"
    name = _identifier(table.name, quoted)
    columns = ", ".join(_identifier(column.name, quoted) for column in table.columns)
    binds = ", ".join("%s" for _ in table.columns)
    return f"INSERT INTO {name} ({columns}) VALUES ({binds})"


def _first_failing_row(connection: Any, sql: str, rows: Sequence[tuple[Any, ...]]) -> int:
    """Probe a rolled-back failed batch with savepoints to locate its first bad row."""
    cursor = connection.cursor()
    try:
        for offset, row in enumerate(rows):
            cursor.execute("SAVEPOINT PB_DIAGNOSTIC")
            try:
                cursor.execute(sql, row)
            except Exception:
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT PB_DIAGNOSTIC")
                except Exception:
                    return offset
                return offset
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT PB_DIAGNOSTIC")
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


def insert_batch(
    connection: Any,
    table: TableModel,
    rows: Sequence[tuple[Any, ...]],
    *,
    use_local_infile: bool = False,
) -> None:
    """Insert one bounded batch; diagnose a failed multi-VALUES statement."""
    if use_local_infile:
        raise PBError("E_LOCAL_INFILE_UNAVAILABLE")
    if not rows:
        return
    cursor = connection.cursor()
    sql = insert_sql(table)
    try:
        cursor.executemany(sql, rows)
    except Exception as exc:
        code = driver_code("mysql", exc)
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
