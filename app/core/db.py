"""DB driver connections and metadata helpers.

The only input to this module is the validated ``DbProfile`` produced by
``config.load_config``.  Driver exception text is never returned or logged.
"""

from __future__ import annotations

import re
from typing import Any

import oracledb
import pymysql
import psycopg

from app.core.config import DbProfile
from app.core.errors import PBError


class BatchInsertError(Exception):
    """Sanitized failed-array metadata. ``row_offset`` is zero-based in its batch."""

    def __init__(self, row_offset: int, code: str | int | None) -> None:
        super().__init__("batch insert failed")
        self.row_offset = max(0, int(row_offset))
        self.driver_code = code


def require_online(profile: DbProfile) -> None:
    """Reject an offline profile with its configured reason and HTTP 409."""
    if profile.online:
        return
    raise PBError(profile.offline_reason or "E_DB_NOT_CONFIGURED", **profile.offline_params)


def driver_code(kind: str, exc: BaseException) -> str | int | None:
    """Extract only a driver code; never expose driver text."""
    args = getattr(exc, "args", ())
    first = args[0] if args else None
    if kind == "mysql":
        value = getattr(exc, "errno", None)
        if value is None and isinstance(first, int) and not isinstance(first, bool):
            value = first
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    if kind == "postgres":
        value = getattr(exc, "sqlstate", None)
        return value if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{5}", value) else None

    value = getattr(first, "code", None) or getattr(exc, "code", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return f"ORA-{value:05d}"
    if isinstance(value, str) and re.fullmatch(r"ORA-\d{5}", value):
        return value
    full_code = getattr(first, "full_code", None) or getattr(exc, "full_code", None)
    if isinstance(full_code, str):
        match = re.search(r"ORA-\d{5}", full_code)
        if match:
            return match.group(0)
    # Driver messages may contain a DSN or credentials, so inspect internally
    # only and return the matched Oracle code rather than any surrounding text.
    try:
        message = str(exc)
    except Exception:
        message = ""
    match = re.search(r"ORA-\d{5}", message)
    if match:
        return match.group(0)
    return None


def connect(profile: DbProfile):
    """Open an Oracle thin, MySQL, or PostgreSQL connection from a validated profile."""
    require_online(profile)
    try:
        if profile.kind == "oracle":
            dsn = oracledb.makedsn(profile.host, profile.port, service_name=profile.target)
            # Do not call init_oracle_client: python-oracledb stays in thin mode.
            connection = oracledb.connect(user=profile.user, password=profile.password, dsn=dsn)
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute("ALTER SESSION SET TIME_ZONE = '+00:00'")
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
            except Exception:
                close_quietly(connection)
                raise
            return connection
        if profile.kind == "mysql":
            return pymysql.connect(
                host=profile.host,
                port=profile.port,
                user=profile.user,
                password=profile.password,
                database=profile.target,
                charset="utf8mb4",
                autocommit=False,
            )
        if profile.kind == "postgres":
            connection = psycopg.connect(
                host=profile.host,
                port=profile.port,
                dbname=profile.target,
                user=profile.user,
                password=profile.password,
                autocommit=False,
            )
            if profile.schema:
                schema_identifier = '"' + profile.schema.replace('"', '""') + '"'
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT set_config('search_path', %s, false)", (schema_identifier,))
                except Exception:
                    close_quietly(connection)
                    raise
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
            return connection
    except Exception as exc:
        raise PBError("E_DB_CONNECT", driver_code=driver_code(profile.kind, exc)) from None
    raise PBError("E_DIALECT_INVALID", dialect=profile.kind)


def close_quietly(connection: Any) -> None:
    """Close a driver connection without allowing close text to escape."""
    try:
        connection.close()
    except Exception:
        pass


def _identifier(name: str, profile: DbProfile, quoted: bool) -> str:
    if not quoted:
        return name
    if profile.kind == "oracle":
        return '"' + name.replace('"', '""') + '"'
    if profile.kind == "postgres":
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def _lookup_name(name: str, profile: DbProfile, quoted: bool) -> str:
    return name if quoted or profile.kind != "postgres" else name.lower()


def table_exists(connection: Any, profile: DbProfile, table_name: str, *, quoted: bool = False) -> bool:
    """Check the current Oracle schema or MySQL database for one table."""
    cursor = connection.cursor()
    try:
        if profile.kind == "oracle":
            value = table_name if quoted else table_name.upper()
            cursor.execute("SELECT 1 FROM USER_TABLES WHERE TABLE_NAME = :name", {"name": value})
        elif profile.kind == "mysql":
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                (table_name,),
            )
        else:
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = COALESCE(%s, current_schema()) AND table_name = %s",
                (profile.schema, _lookup_name(table_name, profile, quoted)),
            )
        return cursor.fetchone() is not None
    except Exception as exc:
        raise PBError("E_DB_OPERATION_FAILED", driver_code=driver_code(profile.kind, exc)) from None
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def table_columns(connection: Any, profile: DbProfile, table_name: str, *, quoted: bool = False) -> set[str]:
    """Return the existing column names for append compatibility checks."""
    cursor = connection.cursor()
    try:
        if profile.kind == "oracle":
            value = table_name if quoted else table_name.upper()
            cursor.execute(
                "SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME = :name",
                {"name": value},
            )
        elif profile.kind == "mysql":
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                (table_name,),
            )
        else:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = COALESCE(%s, current_schema()) AND table_name = %s",
                (profile.schema, _lookup_name(table_name, profile, quoted)),
            )
        return {str(row[0]) for row in cursor.fetchall()}
    except Exception as exc:
        raise PBError("E_DB_OPERATION_FAILED", driver_code=driver_code(profile.kind, exc)) from None
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def count_table(connection: Any, profile: DbProfile, table_name: str, *, quoted: bool = False) -> int:
    """Return COUNT(*) from a planned table name."""
    cursor = connection.cursor()
    name = _identifier(table_name, profile, quoted)
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {name}")
        row = cursor.fetchone()
        return int(row[0])
    except Exception as exc:
        raise PBError("E_DB_OPERATION_FAILED", driver_code=driver_code(profile.kind, exc)) from None
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def test_connection(profile: DbProfile) -> dict[str, Any]:
    """Connect and return only the server version and public profile details."""
    connection = connect(profile)
    try:
        if profile.kind == "oracle":
            version = getattr(connection, "version", None)
            if not version:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT BANNER FROM V$VERSION WHERE BANNER LIKE 'Oracle Database%'")
                    row = cursor.fetchone()
                    version = row[0] if row else "unknown"
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
        elif profile.kind == "mysql":
            version = getattr(connection, "get_server_info", lambda: None)()
            if not version:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT VERSION()")
                    row = cursor.fetchone()
                    version = row[0] if row else "unknown"
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
        else:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT current_setting('server_version')")
                row = cursor.fetchone()
                version = row[0] if row else "unknown"
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass
        return {"profile": profile.public(), "server_version": str(version)}
    except PBError:
        raise
    except Exception as exc:
        raise PBError("E_DB_OPERATION_FAILED", driver_code=driver_code(profile.kind, exc)) from None
    finally:
        close_quietly(connection)
