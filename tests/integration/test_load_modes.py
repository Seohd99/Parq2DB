"""Run the transformation and batch guarantees against each real database."""

import json
import threading
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.db import close_quietly, connect, table_exists
from app.core.errors import PBError
from app.core.flatten import plan_tables
from app.core.jobs import execute_load
from app.core.typemap import measure_string_lengths


@pytest.fixture
def plans(db_profile):
    created = []

    def make(path, **options):
        with pq.ParquetFile(path) as parquet:
            schema = parquet.schema_arrow
        tables = plan_tables(
            schema,
            table_name=f"PB_TEST_{uuid.uuid4().hex[:10].upper()}",
            dialect=db_profile.kind,
            lengths=measure_string_lengths(path, schema, length_scan="full"),
            **options,
        )
        created.extend(tables)
        return tables

    yield make

    connection = connect(db_profile)
    try:
        cursor = connection.cursor()
        try:
            for table in reversed(created):
                assert table.name.upper().startswith("PB_TEST_")
                quoted = table.identifier_policy == "quoted"
                if table_exists(connection, db_profile, table.name, quoted=quoted):
                    name = identifier(table.name, table, db_profile.kind)
                    suffix = " PURGE" if db_profile.kind == "oracle" else ""
                    cursor.execute(f"DROP TABLE {name}{suffix}")
            connection.commit()
        finally:
            cursor.close()
    finally:
        close_quietly(connection)


def identifier(name, table, dialect):
    if dialect == "postgres" and table.identifier_policy != "quoted":
        name = name.lower()
    quote = "`" if dialect == "mysql" else '"'
    return quote + name.replace(quote, quote * 2) + quote


def load(path, tables, profile, *, policy="fail", cancel=None, progress=lambda *_: None):
    with pq.ParquetFile(path) as parquet:
        source_rows = parquet.metadata.num_rows
    return execute_load(
        path=path, tables=tables, profile=profile, source_rows=source_rows,
        batch_size=2, existing_policy=policy,
        cancel_event=cancel or threading.Event(), progress=progress,
    )


def read_row(profile, table, paths, *, row_id=1):
    names = {column.original_path: column.name for column in table.columns}
    columns = ", ".join(identifier(names[path], table, profile.kind) for path in paths)
    name = identifier(table.name, table, profile.kind)
    key = identifier(names["id"], table, profile.kind)
    connection = connect(profile)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT {columns} FROM {name} WHERE {key} = {int(row_id)}")
            return tuple(value.read() if hasattr(value, "read") else value for value in cursor.fetchone())
        finally:
            cursor.close()
    finally:
        close_quietly(connection)


@pytest.mark.parametrize("mode", ["none", "child", "explode"])
def test_nested_modes_preserve_values_and_match_counts(db_profile, plans, parquet_path, mode):
    options = {"parent_key": "id"} if mode == "child" else {}
    if mode == "explode":
        options["explode_column"] = "list_int"
    tables = plans(parquet_path, mode=mode, **options)
    result = load(parquet_path, tables, db_profile)
    assert all(report["matched"] for report in result["tables"])
    assert result["tables"][0]["database_count"] == (7 if mode == "explode" else 5)
    assert read_row(db_profile, tables[0], ["comment"])[0] == "한글 문자열"
    assert len(read_row(db_profile, tables[0], ["comment"], row_id=3)[0]) == 4101
    if mode == "none":
        values = read_row(db_profile, tables[0], ["list_int", "struct_value", "map_value"])
        decoded = [json.loads(value) if isinstance(value, str) else value for value in values]
        assert decoded == [[10, 20], {"city": "Seoul", "active": True}, {"a": 1}]
    elif mode == "child":
        assert len(result["tables"]) == 7
        assert [report["database_count"] for report in result["tables"]] == [5, 5, 4, 4, 4, 4, 6]


@pytest.mark.parametrize("generated", [False, True])
def test_append_respects_parent_key_strategy(db_profile, plans, parquet_path, generated):
    tables = plans(parquet_path, mode="child", parent_key=None if generated else "id")
    first = load(parquet_path, tables, db_profile)
    assert all(report["matched"] for report in first["tables"])
    if generated:
        with pytest.raises(PBError) as caught:
            load(parquet_path, tables, db_profile, policy="append")
        assert caught.value.code == "E_APPEND_GENERATED_KEY"
    else:
        second = load(parquet_path, tables, db_profile, policy="append")
        assert all(report["matched"] and report["database_count"] == 2 * report["expected_rows"]
                   for report in second["tables"])


def test_cancel_keeps_only_the_committed_batch(db_profile, plans, parquet_path):
    tables = plans(parquet_path)
    cancel = threading.Event()

    def after_batch(table, done, expected):
        if done:
            cancel.set()

    result = load(parquet_path, tables, db_profile, cancel=cancel, progress=after_batch)
    assert result["cancelled"] is True
    assert result["committed_batches"] == 1
    assert result["tables"][0]["database_count"] == 2


def test_case_distinct_columns_keep_separate_values(db_profile, plans, tmp_path):
    path = tmp_path / "case.parquet"
    pq.write_table(pa.table({"id": [1], "A": [1], "a": [99]}), path)
    policy = "upper_unquoted" if db_profile.kind == "mysql" else "quoted"
    tables = plans(path, column_policy=policy)
    result = load(path, tables, db_profile)
    assert result["tables"][0]["matched"] is True
    assert read_row(db_profile, tables[0], ["A", "a"]) == (1, 99)
