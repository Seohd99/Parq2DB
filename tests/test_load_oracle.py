from datetime import datetime
from types import SimpleNamespace

import oracledb
import pytest

from app.core.db import BatchInsertError
from app.core.load_oracle import insert_batch, insert_sql
from app.core.model import ColumnModel, TableModel


def column(name, logical, sql_type, *, is_json=False):
    return ColumnModel(
        original_name=name,
        original_path=name,
        name=name,
        arrow_type=None,
        logical_type=logical,
        is_json=is_json,
        sql_type_oracle=sql_type,
    )


def table():
    return TableModel(
        name="PB_TEST_ORACLE",
        columns=(
            column("ID", "int", "NUMBER(19)"),
            column("DOC", "json", "CLOB", is_json=True),
            column("PAYLOAD", "binary", "BLOB"),
        ),
    )


class FakeCursor:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.sizes = None
        self.sql = None
        self.rows = None
        self.batcherrors = False
        self.closed = False

    def setinputsizes(self, *sizes):
        self.sizes = sizes

    def executemany(self, sql, rows, *, batcherrors=False):
        self.sql = sql
        self.rows = list(rows)
        self.batcherrors = batcherrors

    def getbatcherrors(self):
        return self.errors

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, errors=()):
        self.fake_cursor = FakeCursor(errors)
        self.rollbacks = 0

    def cursor(self):
        return self.fake_cursor

    def rollback(self):
        self.rollbacks += 1


def test_oracle_uses_array_binds_and_sets_lob_input_sizes():
    connection = FakeConnection()
    insert_batch(connection, table(), [(1, '{"name":"한글"}', b"blob")])

    cursor = connection.fake_cursor
    assert cursor.sql == 'INSERT INTO PB_TEST_ORACLE (ID, DOC, PAYLOAD) VALUES (:1, :2, :3)'
    assert cursor.batcherrors is True
    assert cursor.sizes == (None, oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_BLOB)
    assert cursor.rows == [(1, '{"name":"한글"}', b"blob")]
    assert cursor.closed is True
    assert connection.rollbacks == 0


def test_oracle_timestamp_tz_uses_explicit_bind_type():
    planned = TableModel(
        name="PB_TEST_ORACLE_TZ",
        columns=(column("EVENT_TS", "timestamp_tz", "TIMESTAMP(6) WITH TIME ZONE"),),
    )
    connection = FakeConnection()
    insert_batch(connection, planned, [(datetime(2024, 1, 1, 12),)])

    assert connection.fake_cursor.sizes == (oracledb.DB_TYPE_TIMESTAMP_TZ,)
    assert connection.fake_cursor.rows == [(datetime(2024, 1, 1, 12),)]


def test_oracle_batcherrors_reports_zero_based_offset():
    connection = FakeConnection([SimpleNamespace(offset=1, code=12899)])

    with pytest.raises(BatchInsertError) as caught:
        insert_batch(connection, table(), [(1, "{}", b"a"), (2, "{}", b"b")])

    assert caught.value.row_offset == 1
    assert caught.value.driver_code == "ORA-12899"
    assert connection.rollbacks == 1


def test_oracle_identifier_quotes_are_escaped_for_quoted_policy():
    planned = TableModel(
        name="PB_TEST_ORACLE",
        identifier_policy="quoted",
        columns=(column('A"B', "string", "VARCHAR2(20 CHAR)"),),
    )
    assert insert_sql(planned) == 'INSERT INTO "PB_TEST_ORACLE" ("A""B") VALUES (:1)'
