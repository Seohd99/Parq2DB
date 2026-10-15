import re
import json
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.core.config import DbProfile
from app.core.db import BatchInsertError, connect
from app.core.errors import PBError
from app.core.flatten import plan_tables, query_table_preview
from app.core.jobs import (
    LoadJobManager,
    _prepare_tables,
    _record_batch_rows,
    _stream_batches,
    convert_value,
    execute_load,
    validate_load_request,
)
from app.core.model import ColumnModel, TableModel
from app.core.reserved import fetch_oracle_reserved_words, reserved_words


def online_profile(kind="mysql"):
    return DbProfile(
        kind=kind,
        host="db.invalid",
        port=3306 if kind == "mysql" else 1521,
        target="unit_test",
        user="unit_test_user",
        password="UNIT_TEST_ONLY_SECRET",
    )


def planned_table(path, name="PB_TEST_JOB", dialect="mysql"):
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
    return plan_tables(schema, table_name=name, dialect=dialect, string_strategy="max")


class FakeDriverError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql = ""
        self.params = None
        self.result = None
        self.closed = False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        upper = sql.upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            table = params[0]
            self.result = (1,) if table in self.connection.existing else None
        elif "INFORMATION_SCHEMA.COLUMNS" in upper:
            table = params[0]
            self.result = [(name,) for name in sorted(self.connection.columns.get(table, set()))]
        elif upper.startswith("SELECT COUNT(*) FROM"):
            table = upper.split("FROM", 1)[1].strip().strip("`")
            self.result = (self.connection.row_counts.get(table, 0),)
        elif upper.startswith("CREATE TABLE"):
            table = re.match(r"CREATE TABLE\s+([^\s(]+)", sql, re.I).group(1).strip("`")
            self.connection.existing.add(table)
            column_block = sql.split("(", 1)[1].rsplit(")", 1)[0]
            self.connection.columns[table] = {
                line.strip().split()[0].strip("`,\" ")
                for line in column_block.splitlines()
                if line.strip()
            }
            self.connection.statements.append(sql)
        elif upper.startswith("TRUNCATE TABLE"):
            table = sql.split()[-1].strip("`")
            self.connection.row_counts[table] = 0
            self.connection.statements.append(sql)
        elif upper.startswith("DROP TABLE"):
            table = sql.split()[2].strip("`")
            self.connection.existing.discard(table)
            self.connection.row_counts.pop(table, None)
            self.connection.statements.append(sql)
        elif upper.startswith("SAVEPOINT") or upper.startswith("ROLLBACK TO"):
            pass
        elif upper.startswith("INSERT INTO") and params is not None:
            if self.connection.fail_value is not None and params[0] == self.connection.fail_value:
                raise FakeDriverError(1062, "UNIT_TEST_ONLY_SECRET DSN")

    def executemany(self, sql, rows):
        self.sql = sql
        rows = list(rows)
        self.connection.batch_sizes.append(len(rows))
        if self.connection.fail_value is not None and any(row[0] == self.connection.fail_value for row in rows):
            raise FakeDriverError(1062, "UNIT_TEST_ONLY_SECRET DSN")
        table = re.match(r"INSERT INTO\s+([^\s(]+)", sql, re.I).group(1).strip("`")
        self.connection.pending.setdefault(table, []).extend(rows)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result or []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, *, existing=(), columns=None, fail_value=None):
        self.existing = set(existing)
        self.columns = dict(columns or {})
        self.fail_value = fail_value
        self.row_counts = {name: 0 for name in self.existing}
        self.pending = {}
        self.batch_sizes = []
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        for table, rows in self.pending.items():
            self.row_counts[table] = self.row_counts.get(table, 0) + len(rows)
        self.pending.clear()
        self.commits += 1

    def rollback(self):
        self.pending.clear()
        self.rollbacks += 1

    def close(self):
        pass


def test_streaming_batches_commit_each_batch_and_stay_within_batch_size(monkeypatch, flat_parquet_path):
    connection = FakeConnection()
    tables = planned_table(flat_parquet_path)
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    observed_batch_rows = []
    from app.core import jobs

    original = jobs._record_batch_rows

    def inspect_batch(batch, table, dialect):
        observed_batch_rows.append(batch.num_rows)
        return original(batch, table, dialect)

    monkeypatch.setattr(jobs, "_record_batch_rows", inspect_batch)
    result = execute_load(
        path=flat_parquet_path,
        tables=tables,
        profile=online_profile(),
        source_rows=3,
        batch_size=2,
        existing_policy="fail",
        cancel_event=threading.Event(),
        progress=lambda *_: None,
    )

    assert observed_batch_rows == [2, 1]
    assert connection.batch_sizes == [2, 1]
    assert connection.commits == 2
    assert result["committed_batches"] == 2
    assert result["committed_rows"] == 3
    assert result["tables"][0]["expected_rows"] == result["tables"][0]["actual_rows"] == 3
    assert result["tables"][0]["database_count"] == 3
    assert result["tables"][0]["matched"] is True


def test_none_mode_arrow_stream_matches_nested_preview_json(parquet_path):
    tables = planned_table(parquet_path)
    table = tables[0]
    expected = query_table_preview(parquet_path, table, dialect="mysql", limit=5)["rows"]
    stream = _stream_batches(parquet_path, table, "mysql", batch_size=2)
    try:
        actual = [
            row
            for batch in stream
            for row in _record_batch_rows(batch, table, "mysql")
        ]
    finally:
        stream.close()

    assert len(actual) == len(expected) == 5
    for row_index, row in enumerate(actual):
        for column_index, column in enumerate(table.columns):
            if column.is_json and row[column_index] is not None:
                assert json.loads(row[column_index]) == expected[row_index][column.name]


def test_mysql_failure_reports_table_batch_row_and_committed_counts(monkeypatch, flat_parquet_path):
    connection = FakeConnection(fail_value=3)
    tables = planned_table(flat_parquet_path)
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)

    with pytest.raises(PBError) as caught:
        execute_load(
            path=flat_parquet_path,
            tables=tables,
            profile=online_profile(),
            source_rows=3,
            batch_size=2,
            existing_policy="fail",
            cancel_event=threading.Event(),
            progress=lambda *_: None,
        )

    assert caught.value.code == "E_LOAD_FAILED"
    assert caught.value.params == {
        "table": "PB_TEST_JOB",
        "batch_index": 2,
        "row_index": 3,
        "row_index_scope": "table",
        "committed_batches": 1,
        "committed_rows": 2,
        "driver_code": 1062,
    }
    assert connection.row_counts["PB_TEST_JOB"] == 2


def test_cancellation_keeps_committed_batches_and_returns_partial_counts(monkeypatch, flat_parquet_path):
    connection = FakeConnection()
    tables = planned_table(flat_parquet_path)
    cancel_event = threading.Event()
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    from app.core import jobs

    original = jobs.load_mysql.insert_batch

    def insert_then_cancel(conn, table, rows):
        original(conn, table, rows)
        cancel_event.set()

    monkeypatch.setattr(jobs.load_mysql, "insert_batch", insert_then_cancel)
    result = execute_load(
        path=flat_parquet_path,
        tables=tables,
        profile=online_profile(),
        source_rows=3,
        batch_size=2,
        existing_policy="fail",
        cancel_event=cancel_event,
        progress=lambda *_: None,
    )

    assert result["cancelled"] is True
    assert result["partial_commits_kept"] is True
    assert result["committed_batches"] == 1
    assert result["committed_rows"] == 2
    assert result["tables"][0]["actual_rows"] == 2
    assert result["tables"][0]["matched"] is False


def test_existing_table_policies_and_append_column_check(flat_parquet_path):
    (table,) = planned_table(flat_parquet_path)
    matching = {column.name for column in table.columns}

    fail_connection = FakeConnection(existing={table.name})
    with pytest.raises(PBError) as caught:
        _prepare_tables(fail_connection, online_profile(), [table], "fail", threading.Event())
    assert caught.value.code == "E_TABLE_EXISTS"

    append_connection = FakeConnection(existing={table.name}, columns={table.name: matching})
    _prepare_tables(append_connection, online_profile(), [table], "append", threading.Event())
    assert not append_connection.statements

    mismatch_connection = FakeConnection(existing={table.name}, columns={table.name: {"OTHER"}})
    with pytest.raises(PBError) as caught:
        _prepare_tables(mismatch_connection, online_profile(), [table], "append", threading.Event())
    assert caught.value.code == "E_APPEND_COLUMNS_MISMATCH"
    assert caught.value.params["missing"]
    assert caught.value.params["extra"] == ["OTHER"]

    truncate_connection = FakeConnection(existing={table.name})
    _prepare_tables(truncate_connection, online_profile(), [table], "truncate", threading.Event())
    assert truncate_connection.statements == [f"TRUNCATE TABLE {table.name}"]

    recreate_connection = FakeConnection(existing={table.name})
    _prepare_tables(recreate_connection, online_profile(), [table], "drop_create", threading.Event())
    assert recreate_connection.statements[0] == f"DROP TABLE {table.name}"
    assert recreate_connection.statements[1].startswith(f"CREATE TABLE {table.name}")


def test_table_names_use_the_same_destructive_confirmation(flat_parquet_path):
    (table,) = planned_table(flat_parquet_path)
    ordinary_table = TableModel(name="OPS_EVENTS", columns=table.columns)
    validate_load_request([ordinary_table], "fail", None)

    for policy in ("truncate", "drop_create"):
        with pytest.raises(PBError) as caught:
            validate_load_request([ordinary_table], policy, "wrong-name")
        assert caught.value.code == "E_CONFIRM_REQUIRED"
        validate_load_request([ordinary_table], policy, ordinary_table.name)

    child = TableModel(name="PB_TEST_JOB_CHILD", columns=table.columns, parent_table=table.name)
    validate_load_request([table, child], "drop_create", table.name)


def test_value_conversions_preserve_arrow_scalar_semantics():
    def col(logical):
        return ColumnModel("x", "x", "X", None, logical)

    assert convert_value(True, col("bool"), "oracle") == 1
    assert convert_value(False, col("bool"), "mysql") == 0
    assert convert_value(Decimal("2.50"), col("decimal"), "oracle") == Decimal("2.50")
    assert convert_value(date(2024, 2, 3), col("date"), "mysql") == date(2024, 2, 3)
    assert convert_value(memoryview(b"\x00\xff"), col("binary"), "oracle") == b"\x00\xff"
    zoned = datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=9)))
    assert convert_value(zoned, col("timestamp_tz"), "mysql") == datetime(2024, 1, 1, 3)
    assert convert_value(zoned, col("timestamp_tz"), "oracle") == datetime(2024, 1, 1, 3)
    json_column = ColumnModel("x", "x", "X", None, "json", is_json=True)
    assert convert_value({"text": "한글"}, json_column, "mysql") == '{"text":"한글"}'


def test_connection_factory_uses_oracle_thin_profile_and_mysql_utf8mb4(monkeypatch):
    from app.core import db

    oracle_calls = {}
    oracle_session_statements = []

    class OracleCursor:
        def execute(self, sql):
            oracle_session_statements.append(sql)

        def close(self):
            pass

    class OracleConnection:
        def cursor(self):
            return OracleCursor()

        def close(self):
            pass

    oracle_connection = OracleConnection()
    monkeypatch.setattr(db.oracledb, "makedsn", lambda host, port, service_name: f"{host}:{port}/{service_name}")

    def oracle_connect(**kwargs):
        oracle_calls.update(kwargs)
        return oracle_connection

    monkeypatch.setattr(db.oracledb, "connect", oracle_connect)
    assert connect(online_profile("oracle")) is oracle_connection
    assert oracle_calls["user"] == "unit_test_user"
    assert oracle_calls["password"] == "UNIT_TEST_ONLY_SECRET"
    assert oracle_calls["dsn"] == "db.invalid:1521/unit_test"
    assert oracle_session_statements == ["ALTER SESSION SET TIME_ZONE = '+00:00'"]

    mysql_connection = object()
    mysql_calls = {}

    def mysql_connect(**kwargs):
        mysql_calls.update(kwargs)
        return mysql_connection

    monkeypatch.setattr(db.pymysql, "connect", mysql_connect)
    assert connect(online_profile("mysql")) is mysql_connection
    assert mysql_calls["charset"] == "utf8mb4"
    assert mysql_calls["autocommit"] is False
    assert mysql_calls["database"] == "unit_test"


def test_online_oracle_reserved_word_query_and_builtin_union():
    class Cursor:
        sql = ""

        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [("newword",), ("level",)]

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    online = fetch_oracle_reserved_words(Connection())
    combined = reserved_words("oracle", lambda _dialect: online)
    assert "NEWWORD" in online
    assert "LEVEL" in combined
    assert "NEWWORD" in combined


def test_job_manager_prevents_overlapping_table_jobs(monkeypatch, tmp_path):
    table = TableModel(name="PB_TEST_BUSY", columns=(ColumnModel("x", "x", "X", None, "int"),))
    started = threading.Event()
    release = threading.Event()
    from app.core import jobs

    def hold_job(**_kwargs):
        started.set()
        release.wait(3)
        return {"tables": [], "committed_batches": 0, "committed_rows": 0}

    monkeypatch.setattr(jobs, "execute_load", hold_job)
    manager = LoadJobManager(max_workers=1)
    try:
        manager.submit(
            path=tmp_path / "unused.parquet",
            tables=[table],
            profile=online_profile(),
            source_rows=0,
            batch_size=10,
            existing_policy="fail",
            confirm_table_name=None,
        )
        assert started.wait(1)
        with pytest.raises(PBError) as caught:
            manager.submit(
                path=tmp_path / "unused.parquet",
                tables=[table],
                profile=online_profile(),
                source_rows=0,
                batch_size=10,
                existing_policy="fail",
                confirm_table_name=None,
            )
        assert caught.value.code == "E_JOB_CONFLICT"
    finally:
        release.set()
        manager.shutdown()
