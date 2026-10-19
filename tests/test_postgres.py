from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from threading import Event

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.core import db, jobs
from app.core.config import DbProfile, load_config
from app.core.db import BatchInsertError, connect
from app.core.ddl_postgres import generate_postgres_ddl
from app.core.errors import PBError
from app.core.flatten import plan_tables, validate_table_name
from app.core.jobs import PostgresNulValueError, convert_value, execute_load, validate_load_request
from app.core.load_postgres import _first_failing_row, copy_sql, insert_batch, insert_sql
from app.core.model import StringLength
from app.core.typemap import measure_string_lengths, map_arrow_type
from app.main import app

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _table(schema: pa.Schema | None = None, name: str = "PB_TEST_POSTGRES"):
    schema = schema or pa.schema([
        pa.field("id", pa.int64(), nullable=False),
        pa.field("payload", pa.string()),
    ])
    return plan_tables(
        schema,
        table_name=name,
        dialect="postgres",
        string_strategy="max",
    )


def _profile(schema: str | None = None) -> DbProfile:
    return DbProfile(
        kind="postgres",
        host="db.invalid",
        port=5432,
        target="unit_test",
        user="test_user",
        schema=schema,
        password="synthetic test value",
    )


def _batch(ids: list[int], values: list[str | None]) -> pa.RecordBatch:
    return pa.record_batch([pa.array(ids, type=pa.int64()), pa.array(values)], names=["id", "payload"])


def test_postgres_type_mapping_and_string_strategies():
    cases = [
        (pa.int8(), "SMALLINT"),
        (pa.int16(), "SMALLINT"),
        (pa.int32(), "INTEGER"),
        (pa.int64(), "BIGINT"),
        (pa.uint8(), "SMALLINT"),
        (pa.uint16(), "INTEGER"),
        (pa.uint32(), "BIGINT"),
        (pa.uint64(), "NUMERIC(20)"),
        (pa.float32(), "REAL"),
        (pa.float64(), "DOUBLE PRECISION"),
        (pa.decimal128(20, 4), "NUMERIC(20,4)"),
        (pa.bool_(), "BOOLEAN"),
        (pa.binary(), "BYTEA"),
        (pa.date32(), "DATE"),
        (pa.timestamp("us"), "TIMESTAMP(6)"),
        (pa.timestamp("us", tz="UTC"), "TIMESTAMPTZ(6)"),
        (pa.time32("ms"), "TIME(3)"),
        (pa.time64("us"), "TIME(6)"),
        (pa.list_(pa.int32()), "JSONB"),
        (pa.struct([pa.field("x", pa.int32())]), "JSONB"),
        (pa.map_(pa.string(), pa.int32()), "JSONB"),
    ]
    for arrow_type, expected in cases:
        assert map_arrow_type(arrow_type, "postgres")["sql_type"] == expected

    sampled = map_arrow_type(pa.string(), "postgres", string_length=StringLength(characters=10, bytes=20))
    assert sampled["sql_type"] == "VARCHAR(12)"
    assert map_arrow_type(pa.string(), "postgres", string_strategy="max")["sql_type"] == "TEXT"
    assert map_arrow_type(pa.string(), "postgres", string_strategy="lob")["sql_type"] == "TEXT"
    ns_timestamp = map_arrow_type(pa.timestamp("ns", tz="UTC"), "postgres")
    assert ns_timestamp["sql_type"] == "TIMESTAMPTZ(6)"
    assert ns_timestamp["warnings"] == [{
        "code": "W_TIMESTAMP_PRECISION_LOSS",
        "params": {"precision": 9, "target_precision": 6, "dialect": "postgres"},
    }]
    ns_time = map_arrow_type(pa.time64("ns"), "postgres")
    assert ns_time["sql_type"] == "TIME(6)"
    assert ns_time["warnings"] == [{
        "code": "W_TIME_PRECISION_LOSS",
        "params": {"precision": 9, "target_precision": 6, "dialect": "postgres"},
    }]


def test_postgres_reserved_words_identifier_limits_and_column_limit():
    assert validate_table_name("A" * 63, "postgres") == "A" * 63
    with pytest.raises(PBError) as too_long:
        validate_table_name("A" * 64, "postgres")
    assert too_long.value.code == "E_TABLE_NAME_INVALID"

    models = _table(pa.schema([pa.field("select", pa.string())]))
    assert models[0].columns[0].name == "SELECT_2"
    assert any(item["code"] == "W_RESERVED_IDENTIFIER" for item in models[0].warnings)

    oversized_identifier = pa.schema([pa.field("가" * 22, pa.string())])
    with pytest.raises(PBError) as identifier_error:
        plan_tables(oversized_identifier, table_name="PB_TEST_LONG_COLUMN", dialect="postgres", column_policy="quoted")
    assert identifier_error.value.code == "E_IDENTIFIER_INVALID"

    too_many_columns = pa.schema([pa.field(f"c{index}", pa.int8()) for index in range(1601)])
    with pytest.raises(PBError) as column_error:
        plan_tables(too_many_columns, table_name="PB_TEST_WIDE", dialect="postgres")
    assert column_error.value.code == "E_POSTGRES_COLUMN_LIMIT"
    assert column_error.value.params["max_columns"] == 1600


def test_postgres_ddl_goldens_for_all_modes(parquet_path: Path):
    with pq.ParquetFile(parquet_path) as parquet:
        schema = parquet.schema_arrow
    lengths = measure_string_lengths(parquet_path, schema, length_scan="sample")
    for mode in ("none", "child", "explode"):
        models = plan_tables(
            schema,
            table_name="PB_TEST_DATA",
            mode=mode,
            dialect="postgres",
            length_scan="sample",
            parent_key="id" if mode == "child" else None,
            parent_key_unique=True if mode == "child" else None,
            explode_column="list_int" if mode == "explode" else None,
            lengths=lengths,
        )
        actual = generate_postgres_ddl(models)
        golden_path = GOLDEN_DIR / f"{mode}_postgres.sql"
        if os.environ.get("UPDATE_GOLDEN") == "1":
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            golden_path.write_text(actual, encoding="utf-8")
        assert golden_path.read_text(encoding="utf-8") == actual


def test_postgres_web_status_and_ddl_modes_are_available_offline(tmp_path: Path, parquet_path: Path):
    config = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    app.state.config = config
    with TestClient(app, base_url="http://127.0.0.1") as client:
        status = client.get("/api/status").json()
        assert status["db"]["postgres"]["online"] is False
        assert status["db"]["postgres"]["reason"]["code"] == "E_DB_NOT_CONFIGURED"
        uploaded = client.post(
            "/api/files/upload",
            files={"file": ("fixture.parquet", parquet_path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file"]["file_id"]
        try:
            for mode in ("none", "child", "explode"):
                response = client.post(
                    f"/api/files/{file_id}/ddl",
                    json={
                        "dialect": "postgres",
                        "mode": mode,
                        "table_name": "PB_TEST_WEB_PG",
                        "parent_key": "id" if mode == "child" else None,
                        "explode_column": "list_int" if mode == "explode" else None,
                    },
                )
                assert response.status_code == 200, response.text
                result = response.json()
                assert "JSONB" in result["ddl"] if mode == "none" else result["ddl"].startswith("CREATE TABLE")
                assert result["tables"][0]["sql_types"]["postgres"]
        finally:
            client.delete(f"/api/files/{file_id}")


def test_postgres_config_defaults_schema_and_public_secret_filtering(tmp_path: Path):
    config_path = tmp_path / "parq2db.toml"
    config_path.write_text(
        '[db.postgres]\nhost = "db.invalid"\ndatabase = "pb"\nschema = "analytics"\n'
        'user = "pb_user"\npassword_env = "PB_TEST_PG_PASSWORD"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path, tmp_path / "missing.local.toml", env={"PB_TEST_PG_PASSWORD": "test-only"})
    profile = cfg.db["postgres"]
    assert profile.online
    assert profile.port == 5432
    assert profile.schema == "analytics"
    assert profile.public() == {
        "kind": "postgres",
        "online": True,
        "target": "db.invalid:5432/pb",
        "user": "pb_user",
    }
    assert "test-only" not in repr(cfg) + repr(profile.public())


def test_postgres_connection_uses_psycopg_and_sets_schema(monkeypatch):
    calls = {}
    executed = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    connection = Connection()

    def fake_connect(**kwargs):
        calls.update(kwargs)
        return connection

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    assert connect(_profile('data"set')) is connection
    assert calls == {
        "host": "db.invalid",
        "port": 5432,
        "dbname": "unit_test",
        "user": "test_user",
        "password": "synthetic test value",
        "autocommit": False,
    }
    assert executed == [("SELECT set_config('search_path', %s, false)", ('"data""set"',))]


class _FakePgError(Exception):
    sqlstate = "22021"


class _FakeCopyUnsupported(Exception):
    sqlstate = "0A000"


class _FakeCopy:
    def __init__(self, cursor, sql):
        self.cursor = cursor
        self.sql = sql
        self.rows = []

    def __enter__(self):
        if self.cursor.copy_error is not None:
            raise self.cursor.copy_error("synthetic failure text")
        return self

    def write_row(self, row):
        self.rows.append(row)

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.cursor.calls.append(("copy", self.sql, tuple(self.rows)))
            if any(row[0] == "bad" for row in self.rows):
                raise _FakePgError("synthetic failure text")
            self.cursor.rowcount = len(self.rows)


class _FakePgCursor:
    def __init__(self, calls, copy_error=None):
        self.calls = calls
        self.copy_error = copy_error
        self.rowcount = -1

    def copy(self, sql):
        return _FakeCopy(self, sql)

    def executemany(self, sql, rows):
        rows = tuple(rows)
        self.calls.append(("executemany", sql, rows))
        if any(row[0] == "bad" for row in rows):
            raise _FakePgError("synthetic failure text")
        self.rowcount = len(rows)

    def execute(self, sql, params=None):
        if sql.startswith("INSERT") and params[0] == "bad":
            raise _FakePgError("synthetic failure text")

    def close(self):
        pass


class _FakePgConnection:
    def __init__(self, copy_error=None):
        self.calls = []
        self.copy_error = copy_error
        self.rollbacks = 0

    def cursor(self):
        return _FakePgCursor(self.calls, self.copy_error)

    def rollback(self):
        self.rollbacks += 1


class _SavepointPgCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        words = sql.split()
        if words[0] == "SAVEPOINT":
            self.connection.savepoints.append((words[1], self.connection.rows.copy()))
        elif words[:2] == ["ROLLBACK", "TO"]:
            self.connection.rollback_to(words[-1])
        elif words[0] == "RELEASE":
            self.connection.release(words[-1])
        elif sql.startswith("INSERT"):
            key = params[0]
            if key in self.connection.rows:
                raise _FakePgError("duplicate key")
            self.connection.rows.add(key)

    def close(self):
        pass


class _SavepointPgConnection:
    def __init__(self):
        self.rows = set()
        self.savepoints = []
        self.rollbacks = 0

    def cursor(self):
        return _SavepointPgCursor(self)

    def rollback_to(self, name):
        index = next(index for index, point in reversed(list(enumerate(self.savepoints))) if point[0] == name)
        self.rows = self.savepoints[index][1].copy()
        self.savepoints = self.savepoints[: index + 1]

    def release(self, name):
        index = next(index for index, point in reversed(list(enumerate(self.savepoints))) if point[0] == name)
        self.savepoints = self.savepoints[:index]

    def rollback(self):
        self.rows.clear()
        self.savepoints.clear()
        self.rollbacks += 1


def test_postgres_insert_batch_copies_rows_and_returns_driver_count():
    table = _table(pa.schema([pa.field("payload", pa.string())]))[0]
    connection = _FakePgConnection()
    assert copy_sql(table) == "COPY PB_TEST_POSTGRES (PAYLOAD) FROM STDIN"
    assert insert_batch(connection, table, [("one",), ("two",)]) == 2
    assert connection.calls == [("copy", copy_sql(table), (("one",), ("two",)))]
    assert connection.rollbacks == 0
    assert insert_batch(connection, table, []) == 0


def test_postgres_insert_batch_falls_back_to_insert_when_copy_is_refused():
    # PostgreSQL refuses COPY FROM on tables with row-level security (SQLSTATE 0A000).
    table = _table(pa.schema([pa.field("payload", pa.string())]))[0]
    connection = _FakePgConnection(copy_error=_FakeCopyUnsupported)
    assert insert_batch(connection, table, [("one",), ("two",)]) == 2
    assert connection.calls == [("executemany", insert_sql(table), (("one",), ("two",)))]
    assert connection.rollbacks == 1


def test_postgres_insert_batch_reports_bad_row_after_failed_copy():
    table = _table(pa.schema([pa.field("payload", pa.string())]))[0]
    connection = _FakePgConnection()
    assert insert_sql(table) == "INSERT INTO PB_TEST_POSTGRES (PAYLOAD) VALUES (%s)"
    with pytest.raises(BatchInsertError) as failure:
        insert_batch(connection, table, [("ok",), ("bad",)])
    assert failure.value.row_offset == 1
    assert failure.value.driver_code == "22021"
    assert connection.calls == [("copy", copy_sql(table), (("ok",), ("bad",)))]
    assert connection.rollbacks >= 2


def test_postgres_failure_diagnosis_keeps_earlier_rows_until_final_rollback():
    connection = _SavepointPgConnection()
    sql = "INSERT INTO PB_TEST_POSTGRES (ID) VALUES (%s)"

    assert _first_failing_row(connection, sql, [(1,), (2,), (3,), (2,)]) == 3
    assert connection.rows == set()
    assert connection.rollbacks == 1


def test_postgres_failure_diagnosis_returns_zero_when_rows_are_unique():
    connection = _SavepointPgConnection()
    sql = "INSERT INTO PB_TEST_POSTGRES (ID) VALUES (%s)"

    assert _first_failing_row(connection, sql, [(1,), (2,), (3,)]) == 0
    assert connection.rows == set()
    assert connection.rollbacks == 1


def test_postgres_copy_text_rows_escape_control_characters():
    # write_row encodes each value with psycopg's text dumpers and COPY text escaping.
    from psycopg import _copy_base
    from psycopg.adapt import Transformer

    out = bytearray()
    row = ("한글\\x", "a\tb\nc", None, Decimal("12.34"), True, '{"k":"v"}')
    _copy_base.format_row_text(row, Transformer(), out)
    assert bytes(out).decode("utf-8") == '한글\\\\x\ta\\tb\\nc\t\\N\t12.34\tt\t{"k":"v"}\n'


def _patch_fake_db(monkeypatch, *, exists=False, columns=()):
    state = {"exists": exists, "columns": set(columns), "count": 0, "statements": [], "inserts": []}

    class Connection:
        commits = 0
        rollbacks = 0
        closed = False

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(jobs, "connect", lambda _profile: connection)
    monkeypatch.setattr(jobs, "table_exists", lambda *_args, **_kwargs: state["exists"])
    monkeypatch.setattr(jobs, "table_columns", lambda *_args, **_kwargs: set(state["columns"]))
    monkeypatch.setattr(jobs, "count_table", lambda *_args, **_kwargs: state["count"])
    monkeypatch.setattr(jobs, "_execute_statement", lambda _conn, _profile, sql: state["statements"].append(sql))
    return connection, state


def test_postgres_load_policies_and_confirmation(monkeypatch):
    table = _table()[0]
    with pytest.raises(PBError) as confirmation:
        validate_load_request([table], "truncate", None)
    assert confirmation.value.code == "E_CONFIRM_REQUIRED"
    validate_load_request([table], "truncate", table.name)
    connection, state = _patch_fake_db(monkeypatch, exists=True)
    with pytest.raises(PBError) as exists:
        jobs._prepare_tables(connection, _profile(), [table], "fail", Event())
    assert exists.value.code == "E_TABLE_EXISTS"

    state["columns"] = {"id", "payload"}
    jobs._prepare_tables(connection, _profile(), [table], "append", Event())
    assert state["statements"] == []

    jobs._prepare_tables(connection, _profile(), [table], "truncate", Event())
    assert state["statements"] == ["TRUNCATE TABLE PB_TEST_POSTGRES"]

    state["statements"].clear()
    jobs._prepare_tables(connection, _profile(), [table], "drop_create", Event())
    assert state["statements"][0] == "DROP TABLE PB_TEST_POSTGRES"
    assert state["statements"][1].startswith("CREATE TABLE PB_TEST_POSTGRES (")
    assert "`" not in state["statements"][1]


def test_postgres_load_reports_failure_location_and_committed_counts(monkeypatch):
    table = _table()[0]
    connection, state = _patch_fake_db(monkeypatch)
    batches = [_batch([1, 2], ["one", "two"]), _batch([3, 4], ["three", "four"])]
    monkeypatch.setattr(jobs, "_expected_rows", lambda *_args: 4)
    monkeypatch.setattr(jobs, "_stream_batches", lambda *_args: iter(batches))

    def insert(_connection, _table, rows):
        if not state["inserts"]:
            state["inserts"].append(tuple(rows))
            state["count"] += len(rows)
            return len(rows)
        raise BatchInsertError(1, "22021")

    monkeypatch.setattr(jobs.load_postgres, "insert_batch", insert)
    with pytest.raises(PBError) as failure:
        execute_load(
            path=Path("unused.parquet"),
            tables=[table],
            profile=_profile(),
            source_rows=4,
            batch_size=2,
            existing_policy="fail",
            cancel_event=Event(),
            progress=lambda *_args: None,
        )
    assert failure.value.code == "E_LOAD_FAILED"
    assert failure.value.params == {
        "table": "PB_TEST_POSTGRES",
        "batch_index": 2,
        "row_index": 4,
        "row_index_scope": "table",
        "committed_batches": 1,
        "committed_rows": 2,
        "driver_code": "22021",
    }
    assert connection.commits == 2  # preparation DDL plus the first committed batch


def test_postgres_load_cancellation_keeps_completed_batch(monkeypatch):
    table = _table()[0]
    connection, state = _patch_fake_db(monkeypatch)
    batches = [_batch([1, 2], ["one", "two"]), _batch([3, 4], ["three", "four"])]
    monkeypatch.setattr(jobs, "_expected_rows", lambda *_args: 4)
    monkeypatch.setattr(jobs, "_stream_batches", lambda *_args: iter(batches))

    def insert(_connection, _table, rows):
        state["count"] += len(rows)
        return len(rows)

    monkeypatch.setattr(jobs.load_postgres, "insert_batch", insert)
    cancel_event = Event()

    def progress(_name, rows_done, _expected):
        if rows_done == 2:
            cancel_event.set()

    result = execute_load(
        path=Path("unused.parquet"),
        tables=[table],
        profile=_profile(),
        source_rows=4,
        batch_size=2,
        existing_policy="fail",
        cancel_event=cancel_event,
        progress=progress,
    )
    assert result["cancelled"] is True
    assert result["committed_batches"] == 1
    assert result["committed_rows"] == 2
    assert result["tables"][0]["rows_committed"] == 2
    assert connection.commits == 2  # preparation DDL plus the completed batch


def test_postgres_nul_is_rejected_with_exact_row_and_json_location(monkeypatch):
    table = _table()[0]
    connection, state = _patch_fake_db(monkeypatch)
    batch = _batch([1, 2], ["safe", "bad\x00value"])
    monkeypatch.setattr(jobs, "_expected_rows", lambda *_args: 2)
    monkeypatch.setattr(jobs, "_stream_batches", lambda *_args: iter([batch]))
    monkeypatch.setattr(
        jobs.load_postgres,
        "insert_batch",
        lambda *_args: pytest.fail("NUL value reached the PostgreSQL driver"),
    )

    with pytest.raises(PBError) as failure:
        execute_load(
            path=Path("unused.parquet"),
            tables=[table],
            profile=_profile(),
            source_rows=2,
            batch_size=2,
            existing_policy="fail",
            cancel_event=Event(),
            progress=lambda *_args: None,
        )
    assert failure.value.code == "E_POSTGRES_NUL_CHARACTER"
    assert failure.value.params["column"] == "PAYLOAD"
    assert failure.value.params["batch_index"] == 1
    assert failure.value.params["row_index"] == 2
    assert failure.value.params["committed_rows"] == 0
    assert state["count"] == 0
    assert connection.commits == 1  # the created table is committed before row conversion

    json_column = _table(pa.schema([pa.field("items", pa.list_(pa.string()))]))[0].columns[0]
    with pytest.raises(PostgresNulValueError):
        convert_value('{"item":"\\u0000"}', json_column, "postgres")


def test_postgres_connect_and_reserved_metadata_are_offline_without_profile():
    cfg = load_config(Path("missing.toml"), Path("missing.local.toml"), env={})
    assert not cfg.db["postgres"].online
    assert cfg.db["postgres"].offline_reason == "E_DB_NOT_CONFIGURED"
