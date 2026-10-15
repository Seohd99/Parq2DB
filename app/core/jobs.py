"""Background database load jobs with batch-boundary cancellation."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core import load_mysql, load_oracle, load_postgres
from app.core.config import DbProfile
from app.core.db import (
    BatchInsertError,
    close_quietly,
    connect,
    count_table,
    driver_code,
    table_columns,
    table_exists,
)
from app.core.duckdb_utils import connect_utc, literal_path
from app.core.ddl_mysql import generate_mysql_ddl
from app.core.ddl_oracle import generate_oracle_ddl
from app.core.ddl_postgres import generate_postgres_ddl
from app.core.errors import PBError
from app.core.flatten import build_expected_count_sql, build_select_sql
from app.core.model import ColumnModel, TableModel

LoadProgress = Callable[[str, int, int | None], None]
LoadPreparation = Callable[
    [threading.Event, Callable[[int], None]],
    tuple[Sequence[TableModel], int] | None,
]


def validate_load_request(
    tables: Sequence[TableModel],
    existing_policy: str,
    confirm_table_name: str | None,
) -> None:
    """Check destructive confirmation before queueing."""
    if existing_policy not in {"fail", "append", "truncate", "drop_create"}:
        raise PBError("E_BAD_REQUEST", fields=["existing_policy"])
    if not tables:
        raise PBError("E_BAD_REQUEST", fields=["tables"])
    if existing_policy in {"truncate", "drop_create"} and confirm_table_name != tables[0].name:
        raise PBError("E_CONFIRM_REQUIRED", table=tables[0].name)


def _identifier(name: str, dialect: str, quoted: bool) -> str:
    if not quoted:
        return name
    if dialect == "oracle":
        return '"' + name.replace('"', '""') + '"'
    if dialect == "postgres":
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def _create_statement(profile: DbProfile, table: TableModel) -> str:
    if profile.kind == "oracle":
        return generate_oracle_ddl((table,)).strip().removesuffix(";")
    if profile.kind == "postgres":
        return generate_postgres_ddl((table,)).strip().removesuffix(";")
    return generate_mysql_ddl((table,)).strip().removesuffix(";")


def _execute_statement(connection: Any, profile: DbProfile, sql: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
    except Exception as exc:
        raise PBError("E_DB_OPERATION_FAILED", driver_code=driver_code(profile.kind, exc)) from None
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _normalized_column_names(names: set[str] | list[str], table: TableModel) -> set[str]:
    if table.identifier_policy == "quoted":
        return set(names)
    return {name.upper() for name in names}


def _has_generated_parent_key(tables: Sequence[TableModel]) -> bool:
    root = tables[0]
    if root.mode != "child":
        return False
    keys = set(root.parent_key)
    return any(column.generated and column.name in keys for column in root.columns)


def _preflight(
    connection: Any,
    profile: DbProfile,
    tables: Sequence[TableModel],
    existing_policy: str,
) -> dict[str, bool]:
    exists = {
        table.name: table_exists(
            connection,
            profile,
            table.name,
            quoted=table.identifier_policy == "quoted",
        )
        for table in tables
    }
    if existing_policy == "fail":
        existing = [name for name, present in exists.items() if present]
        if existing:
            raise PBError("E_TABLE_EXISTS", table=existing[0], tables=existing)
    if existing_policy == "append" and _has_generated_parent_key(tables):
        # Generated keys restart at 0 for every file, so appending to tables that
        # already hold rows would give different parents the same key.
        for table in tables:
            if exists[table.name] and count_table(
                connection, profile, table.name, quoted=table.identifier_policy == "quoted",
            ):
                raise PBError("E_APPEND_GENERATED_KEY", table=table.name)
    if existing_policy == "append":
        for table in tables:
            if not exists[table.name]:
                continue
            actual = table_columns(
                connection,
                profile,
                table.name,
                quoted=table.identifier_policy == "quoted",
            )
            expected_names = {column.name for column in table.columns}
            expected = _normalized_column_names(expected_names, table)
            actual_normalized = _normalized_column_names(actual, table)
            if expected != actual_normalized:
                raise PBError(
                    "E_APPEND_COLUMNS_MISMATCH",
                    table=table.name,
                    missing=sorted(expected - actual_normalized),
                    extra=sorted(actual_normalized - expected),
                )
    return exists


def _prepare_tables(
    connection: Any,
    profile: DbProfile,
    tables: Sequence[TableModel],
    existing_policy: str,
    cancel_event: threading.Event,
) -> bool:
    exists = _preflight(connection, profile, tables, existing_policy)
    quoted = tables[0].identifier_policy == "quoted"
    changed = False
    if existing_policy == "truncate":
        for table in reversed(tables):
            if cancel_event.is_set():
                return changed
            if exists[table.name]:
                name = _identifier(table.name, profile.kind, quoted)
                _execute_statement(connection, profile, f"TRUNCATE TABLE {name}")
                changed = True
        for table in tables:
            if cancel_event.is_set():
                return changed
            if not exists[table.name]:
                _execute_statement(connection, profile, _create_statement(profile, table))
                changed = True
        return changed
    if existing_policy == "drop_create":
        for table in reversed(tables):
            if cancel_event.is_set():
                return changed
            if exists[table.name]:
                name = _identifier(table.name, profile.kind, quoted)
                suffix = " PURGE" if profile.kind == "oracle" else ""
                _execute_statement(connection, profile, f"DROP TABLE {name}{suffix}")
                changed = True
        for table in tables:
            if cancel_event.is_set():
                return changed
            _execute_statement(connection, profile, _create_statement(profile, table))
            changed = True
        return changed
    for table in tables:
        if cancel_event.is_set():
            return changed
        if existing_policy == "append" and exists[table.name]:
            continue
        _execute_statement(connection, profile, _create_statement(profile, table))
        changed = True
    return changed


def _expected_rows(path: Path, table: TableModel, source_rows: int) -> int:
    sql = build_expected_count_sql(table)
    if sql is None:
        return source_rows
    connection = connect_utc()
    try:
        row = connection.execute(sql, [literal_path(path)]).fetchone()
        return int(row[0] or 0)
    except Exception:
        raise PBError("E_PARQUET_INVALID") from None
    finally:
        connection.close()


def _as_datetime(value: Any, *, assume_utc: bool = False) -> Any:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return value
    if assume_utc and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class PostgresNulValueError(ValueError):
    """A PostgreSQL text or JSON value contains a forbidden NUL character."""

    def __init__(self, column: str, row_offset: int | None = None) -> None:
        super().__init__("PostgreSQL text values cannot contain NUL")
        self.column = column
        self.row_offset = row_offset


def _contains_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_nul(item) for item in value)
    return False


def _json_has_nul(value: str) -> bool:
    if "\x00" in value:
        return True
    try:
        return _contains_nul(json.loads(value))
    except (TypeError, ValueError):
        return False


def convert_value(value: Any, column: ColumnModel, dialect: str) -> Any:
    """Convert a DuckDB Arrow scalar to the target driver's bind value."""
    if value is None:
        return None
    if hasattr(value, "as_py"):
        value = value.as_py()
    logical = column.logical_type
    if logical == "bool":
        return bool(value) if dialect == "postgres" else (1 if bool(value) else 0)
    if logical == "decimal":
        return value if isinstance(value, Decimal) else Decimal(str(value))
    if logical == "timestamp_tz":
        value = _as_datetime(value, assume_utc=True)
        if dialect == "mysql" and isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        if dialect == "oracle" and isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            return value.replace(tzinfo=None)
        return value
    if logical == "timestamp":
        value = _as_datetime(value)
    if dialect == "postgres" and (column.is_json or logical == "json"):
        if isinstance(value, str):
            if _json_has_nul(value):
                raise PostgresNulValueError(column.name)
        elif _contains_nul(value):
            raise PostgresNulValueError(column.name)
    elif dialect == "postgres" and _contains_nul(value):
        raise PostgresNulValueError(column.name)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (dict, list)) and (column.is_json or logical == "json"):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if isinstance(value, (date, datetime, Decimal, bytes, str, int, float)):
        return value
    return value


def _record_batch_rows(batch: Any, table: TableModel, dialect: str) -> list[tuple[Any, ...]]:
    columns = [batch.column(index).to_pylist() for index in range(batch.num_columns)]
    rows: list[tuple[Any, ...]] = []
    for row_index in range(batch.num_rows):
        try:
            rows.append(tuple(
                convert_value(columns[column_index][row_index], column, dialect)
                for column_index, column in enumerate(table.columns)
            ))
        except PostgresNulValueError as exc:
            exc.row_offset = row_index
            raise
    return rows


def _stream_batches(path: Path, table: TableModel, dialect: str, batch_size: int):
    """Read plain tables in PyArrow batches and apply JSON transforms to each batch."""
    if table.mode == "none":
        batch_name = "__parqbridge_load_batch"
        # Batches hold every top-level column in file order, matching the plan's positional aliases.
        query = build_select_sql(table, dialect=dialect, source=f'"{batch_name}"')

        def transform_batch(batch: Any):
            connection = connect_utc()
            reader = None
            try:
                connection.register(batch_name, pa.Table.from_batches([batch]))
                reader = connection.sql(query).to_arrow_reader(batch_size)
                yield from reader
            finally:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                try:
                    connection.unregister(batch_name)
                except Exception:
                    pass
                close_quietly(connection)

        pending_batches: list[Any] = []
        pending_rows = 0

        def emit_pending():
            nonlocal pending_batches, pending_rows
            combined = pa.Table.from_batches(pending_batches).combine_chunks()
            while combined.num_rows >= batch_size:
                output = combined.slice(0, batch_size).to_batches(max_chunksize=batch_size)[0]
                yield from transform_batch(output)
                combined = combined.slice(batch_size)
            pending_batches = combined.to_batches(max_chunksize=batch_size) if combined.num_rows else []
            pending_rows = combined.num_rows

        try:
            with pq.ParquetFile(path) as metadata_file:
                row_group_count = metadata_file.metadata.num_row_groups
            memory_pool = pa.default_memory_pool()
            release_unused = getattr(memory_pool, "release_unused", None)
            for row_group_index in range(row_group_count):
                parquet_file = pq.ParquetFile(path)
                batch = None
                batches = None
                try:
                    batches = parquet_file.iter_batches(
                        batch_size=batch_size,
                        row_groups=[row_group_index],
                        use_threads=False,
                    )
                    for batch in batches:
                        if not pending_batches and batch.num_rows == batch_size:
                            yield from transform_batch(batch)
                        else:
                            pending_batches.append(batch)
                            pending_rows += batch.num_rows
                            if pending_rows >= batch_size:
                                yield from emit_pending()
                finally:
                    batch = None
                    batches = None
                    parquet_file.close()
                    if callable(release_unused):
                        try:
                            release_unused()
                        except Exception:
                            pass
            if pending_rows:
                pending = pa.Table.from_batches(pending_batches).combine_chunks()
                for final_batch in pending.to_batches(max_chunksize=batch_size):
                    yield from transform_batch(final_batch)
        except PBError:
            raise
        except Exception:
            raise PBError("E_PARQUET_INVALID") from None
        return

    connection = connect_utc()
    try:
        # Build a lazy relation before opening its Arrow reader. `execute()` first
        # materializes the query result and can retain the full table in DuckDB.
        relation = connection.sql(build_select_sql(table, dialect=dialect), params=[literal_path(path)])
        reader = relation.to_arrow_reader(batch_size)
        for batch in reader:
            yield batch
    except PBError:
        raise
    except Exception:
        raise PBError("E_PARQUET_INVALID") from None
    finally:
        connection.close()


def execute_load(
    *,
    path: Path,
    tables: Sequence[TableModel],
    profile: DbProfile,
    source_rows: int,
    batch_size: int,
    existing_policy: str,
    confirm_table_name: str | None = None,
    cancel_event: threading.Event,
    progress: LoadProgress,
) -> dict[str, Any]:
    """Create/prepare planned tables, stream inserts, and compare row counts."""
    validate_load_request(tables, existing_policy, confirm_table_name)
    connection = connect(profile)
    reports: list[dict[str, Any]] = []
    committed_batches = 0
    committed_rows = 0
    cancelled = False
    try:
        preparation_changes_applied = _prepare_tables(connection, profile, tables, existing_policy, cancel_event)
        if profile.kind == "postgres" and preparation_changes_applied:
            try:
                connection.commit()
            except Exception as exc:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise PBError(
                    "E_DB_OPERATION_FAILED",
                    driver_code=driver_code(profile.kind, exc),
                ) from None
        if cancel_event.is_set():
            cancelled = True
            return {
                "tables": reports,
                "committed_batches": committed_batches,
                "committed_rows": committed_rows,
                "partial_commits_kept": False,
                "preparation_changes_applied": preparation_changes_applied,
            }
        for table in tables:
            if cancel_event.is_set():
                cancelled = True
                break
            expected = _expected_rows(path, table, source_rows)
            progress(table.name, 0, expected)
            quoted = table.identifier_policy == "quoted"
            before_count = count_table(connection, profile, table.name, quoted=quoted)
            rows_done = 0
            table_batches = 0
            batch_index = 0
            insert = {
                "oracle": load_oracle.insert_batch,
                "mysql": load_mysql.insert_batch,
                "postgres": load_postgres.insert_batch,
            }[profile.kind]
            for record_batch in _stream_batches(path, table, profile.kind, batch_size):
                if cancel_event.is_set():
                    cancelled = True
                    break
                batch_index += 1
                try:
                    rows = _record_batch_rows(record_batch, table, profile.kind)
                except PostgresNulValueError as exc:
                    raise PBError(
                        "E_POSTGRES_NUL_CHARACTER",
                        table=table.name,
                        column=exc.column,
                        batch_index=batch_index,
                        row_index=rows_done + int(exc.row_offset or 0) + 1,
                        row_index_scope="table",
                        committed_batches=committed_batches,
                        committed_rows=committed_rows,
                    ) from None
                try:
                    insert(connection, table, rows)
                    try:
                        connection.commit()
                    except Exception as exc:
                        try:
                            connection.rollback()
                        except Exception:
                            pass
                        raise PBError(
                            "E_LOAD_FAILED",
                            table=table.name,
                            batch_index=batch_index,
                            row_index=rows_done + 1,
                            row_index_scope="table",
                            committed_batches=committed_batches,
                            committed_rows=committed_rows,
                            driver_code=driver_code(profile.kind, exc),
                        ) from None
                except BatchInsertError as exc:
                    raise PBError(
                        "E_LOAD_FAILED",
                        table=table.name,
                        batch_index=batch_index,
                        row_index=rows_done + min(exc.row_offset, max(len(rows) - 1, 0)) + 1,
                        row_index_scope="table",
                        committed_batches=committed_batches,
                        committed_rows=committed_rows,
                        driver_code=exc.driver_code,
                    ) from None
                except PBError:
                    raise
                except Exception as exc:
                    raise PBError(
                        "E_LOAD_FAILED",
                        table=table.name,
                        batch_index=batch_index,
                        row_index=rows_done + 1,
                        row_index_scope="table",
                        committed_batches=committed_batches,
                        committed_rows=committed_rows,
                        driver_code=driver_code(profile.kind, exc),
                    ) from None
                rows_done += len(rows)
                table_batches += 1
                committed_batches += 1
                committed_rows += len(rows)
                progress(table.name, rows_done, expected)

            after_count = count_table(connection, profile, table.name, quoted=quoted)
            inserted_rows = after_count - before_count
            reports.append({
                "table": table.name,
                "expected_rows": expected,
                "actual_rows": inserted_rows,
                "database_count": after_count,
                "rows_committed": rows_done,
                "batches_committed": table_batches,
                "matched": inserted_rows == expected,
                "cancelled": cancelled,
            })
            if cancelled:
                break
        return {
            "tables": reports,
            "committed_batches": committed_batches,
            "committed_rows": committed_rows,
            "partial_commits_kept": bool(committed_batches),
            "preparation_changes_applied": preparation_changes_applied,
            "cancelled": cancelled,
        }
    finally:
        close_quietly(connection)


class LoadJobManager:
    """Own queued/running jobs and prevent overlapping target-table loads."""

    def __init__(self, max_workers: int = 2) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="parqbridge-load")
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_tables: dict[tuple[str, str], str] = {}
        self._closed = False

    def submit(
        self,
        *,
        path: Path,
        tables: Sequence[TableModel],
        profile: DbProfile,
        source_rows: int,
        batch_size: int,
        existing_policy: str,
        confirm_table_name: str | None,
        prepare: LoadPreparation | None = None,
    ) -> dict[str, Any]:
        validate_load_request(tables, existing_policy, confirm_table_name)
        job_id = uuid.uuid4().hex
        keys = [(profile.kind, table.name.upper()) for table in tables]
        now = time.monotonic()
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "measurement_progress_percent": 0 if prepare is not None else None,
            "dialect": profile.kind,
            "target_tables": [table.name for table in tables],
            "progress": [{"table": table.name, "rows_done": 0, "rows_expected": None} for table in tables],
            "result": None,
            "error": None,
            "created": now,
            "started": None,
            "finished": None,
            "cancel_event": threading.Event(),
            "table_keys": keys,
            "confirm_table_name": confirm_table_name,
        }
        with self._lock:
            if self._closed:
                raise PBError("E_DB_OPERATION_FAILED", driver_code=None)
            conflicts = [self._active_tables[key] for key in keys if key in self._active_tables]
            if conflicts:
                raise PBError("E_JOB_CONFLICT", table=tables[0].name)
            self._jobs[job_id] = job
            for key in keys:
                self._active_tables[key] = job_id
            self._executor.submit(
                self._run,
                job_id,
                path,
                tuple(tables),
                profile,
                source_rows,
                batch_size,
                existing_policy,
                confirm_table_name,
                prepare,
            )
            return self._snapshot_locked(job)

    def _snapshot_locked(self, job: dict[str, Any]) -> dict[str, Any]:
        now = job["finished"] or time.monotonic()
        started = job["started"] or job["created"]
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "stage": job["stage"],
            "measurement_progress_percent": job["measurement_progress_percent"],
            "dialect": job["dialect"],
            "target_tables": list(job["target_tables"]),
            "progress": [dict(item) for item in job["progress"]],
            "elapsed_seconds": round(max(0.0, now - started), 3),
            "result": job["result"],
            "error": job["error"],
        }

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PBError("E_JOB_NOT_FOUND")
            return self._snapshot_locked(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PBError("E_JOB_NOT_FOUND")
            if job["status"] in {"queued", "running"}:
                job["cancel_event"].set()
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                    job["finished"] = time.monotonic()
                    self._release_locked(job)
            return self._snapshot_locked(job)

    def _release_locked(self, job: dict[str, Any]) -> None:
        for key in job["table_keys"]:
            if self._active_tables.get(key) == job["job_id"]:
                del self._active_tables[key]

    def _run(
        self,
        job_id: str,
        path: Path,
        tables: Sequence[TableModel],
        profile: DbProfile,
        source_rows: int,
        batch_size: int,
        existing_policy: str,
        confirm_table_name: str | None,
        prepare: LoadPreparation | None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job["status"] == "cancelled" or job["cancel_event"].is_set():
                self._release_locked(job)
                return
            job["status"] = "running"
            job["stage"] = "measuring" if prepare is not None else "loading"
            job["started"] = time.monotonic()

        if prepare is not None:
            def report_measurement(progress_percent: int) -> None:
                with self._lock:
                    job["measurement_progress_percent"] = max(0, min(100, progress_percent))

            try:
                prepared = prepare(job["cancel_event"], report_measurement)
                if prepared is None or job["cancel_event"].is_set():
                    with self._lock:
                        job["status"] = "cancelled"
                        job["finished"] = time.monotonic()
                        self._release_locked(job)
                    return
                prepared_tables, prepared_rows = prepared
                validate_load_request(prepared_tables, existing_policy, confirm_table_name)
                if [table.name for table in prepared_tables] != job["target_tables"]:
                    raise PBError("E_INTERNAL", type="InvalidLoadPlan")
                tables = tuple(prepared_tables)
                source_rows = prepared_rows
                with self._lock:
                    job["stage"] = "loading"
                    job["measurement_progress_percent"] = 100
                    job["progress"] = [
                        {"table": table.name, "rows_done": 0, "rows_expected": None}
                        for table in tables
                    ]
            except PBError as exc:
                with self._lock:
                    if job["cancel_event"].is_set():
                        job["status"] = "cancelled"
                    else:
                        job["error"] = {"code": exc.code, "params": exc.params}
                        job["status"] = "failed"
                    job["finished"] = time.monotonic()
                    self._release_locked(job)
                return
            except Exception as exc:
                with self._lock:
                    job["error"] = {"code": "E_INTERNAL", "params": {"type": type(exc).__name__}}
                    job["status"] = "failed"
                    job["finished"] = time.monotonic()
                    self._release_locked(job)
                return

        def report(table_name: str, rows_done: int, rows_expected: int | None) -> None:
            with self._lock:
                job["stage"] = "loading"
                item = next((row for row in job["progress"] if row["table"] == table_name), None)
                if item is not None:
                    item["rows_done"] = rows_done
                    item["rows_expected"] = rows_expected

        try:
            result = execute_load(
                path=path,
                tables=tables,
                profile=profile,
                source_rows=source_rows,
                batch_size=batch_size,
                existing_policy=existing_policy,
                confirm_table_name=confirm_table_name,
                cancel_event=job["cancel_event"],
                progress=report,
            )
            with self._lock:
                job["result"] = result
                job["status"] = "cancelled" if result.get("cancelled") else "succeeded"
        except PBError as exc:
            with self._lock:
                job["error"] = {"code": exc.code, "params": exc.params}
                job["status"] = "failed"
        except Exception as exc:
            # Driver exceptions are intentionally never stringified, logged, or returned.
            with self._lock:
                job["error"] = {"code": "E_INTERNAL", "params": {"type": type(exc).__name__}}
                job["status"] = "failed"
        finally:
            with self._lock:
                job["finished"] = time.monotonic()
                self._release_locked(job)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if job["status"] in {"queued", "running"}:
                    job["cancel_event"].set()
        self._executor.shutdown(wait=True, cancel_futures=False)
