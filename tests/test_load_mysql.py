import pytest

from app.core.db import BatchInsertError
from app.core.errors import PBError
from app.core.load_mysql import _first_failing_row, insert_batch, insert_sql
from app.core.model import ColumnModel, TableModel


def id_table(*, quoted=False):
    return TableModel(
        name="PB_TEST_MYSQL",
        identifier_policy="quoted" if quoted else "upper_unquoted",
        columns=(ColumnModel(
            original_name="id",
            original_path="id",
            name="ID",
            arrow_type=None,
            logical_type="int",
            sql_type_mysql="BIGINT",
        ),),
    )


class FakeCursor:
    def __init__(self):
        self.sql = None
        self.rows = None
        self.closed = False

    def executemany(self, sql, rows):
        self.sql = sql
        self.rows = list(rows)
        if any(row[0] == 2 for row in self.rows):
            raise Exception(1062, "FICTIONAL secret-dsn-text")

    def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO") and params and params[0] == 2:
            raise Exception(1062, "FICTIONAL secret-dsn-text")

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.cursors = []
        self.rollbacks = 0

    def cursor(self):
        cursor = FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def rollback(self):
        self.rollbacks += 1


class SavepointCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        words = sql.split()
        if words[0] == "SAVEPOINT":
            self.connection.savepoints.append((words[-1], self.connection.keys.copy()))
        elif words[:2] == ["ROLLBACK", "TO"]:
            self.connection.rollback_to(words[-1])
        elif words[0] == "RELEASE":
            self.connection.release(words[-1])
        elif sql.startswith("INSERT INTO"):
            key = params[0]
            if key in self.connection.keys:
                raise Exception(1062, "duplicate key")
            self.connection.keys.add(key)

    def close(self):
        pass


class SavepointConnection:
    def __init__(self):
        self.keys = set()
        self.savepoints = []
        self.rollbacks = 0

    def cursor(self):
        return SavepointCursor(self)

    def rollback_to(self, name):
        index = next(index for index, point in reversed(list(enumerate(self.savepoints))) if point[0] == name)
        self.keys = self.savepoints[index][1].copy()
        self.savepoints = self.savepoints[: index + 1]

    def release(self, name):
        index = next(index for index, point in reversed(list(enumerate(self.savepoints))) if point[0] == name)
        self.savepoints = self.savepoints[:index]

    def rollback(self):
        self.keys.clear()
        self.savepoints.clear()
        self.rollbacks += 1


def test_mysql_uses_multi_values_executemany_and_escaped_names():
    connection = FakeConnection()
    planned = id_table(quoted=True)
    insert_batch(connection, planned, [(1,), (3,)])

    cursor = connection.cursors[0]
    assert cursor.sql == "INSERT INTO `PB_TEST_MYSQL` (`ID`) VALUES (%s)"
    assert cursor.rows == [(1,), (3,)]
    assert cursor.closed is True
    assert insert_sql(planned).endswith("VALUES (%s)")


def test_mysql_failed_executemany_finds_table_row_offset_without_driver_text():
    connection = FakeConnection()

    with pytest.raises(BatchInsertError) as caught:
        insert_batch(connection, id_table(), [(1,), (2,), (3,)])

    assert caught.value.row_offset == 1
    assert caught.value.driver_code == 1062
    assert "secret-dsn-text" not in str(caught.value)
    assert connection.rollbacks >= 2


def test_mysql_failure_diagnosis_keeps_earlier_rows_until_final_rollback():
    connection = SavepointConnection()

    assert _first_failing_row(connection, insert_sql(id_table()), [(1,), (2,), (3,), (2,)]) == 3
    assert connection.keys == set()
    assert connection.rollbacks == 1


def test_mysql_failure_diagnosis_returns_zero_when_rows_are_unique():
    connection = SavepointConnection()

    assert _first_failing_row(connection, insert_sql(id_table()), [(1,), (2,), (3,)]) == 0
    assert connection.keys == set()
    assert connection.rollbacks == 1


def test_local_infile_path_is_opt_in_and_off_by_default():
    connection = FakeConnection()
    with pytest.raises(PBError) as caught:
        insert_batch(connection, id_table(), [(1,)], use_local_infile=True)
    assert caught.value.code == "E_LOCAL_INFILE_UNAVAILABLE"
