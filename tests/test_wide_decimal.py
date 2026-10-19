from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import DbProfile
from app.core.ddl_mysql import generate_mysql_ddl
from app.core.flatten import plan_tables, query_table_preview
from app.core.inspect import preview_parquet
from app.core.jobs import _expected_rows, _stream_batches, execute_load
from app.core.typemap import map_arrow_type
from app.api.planning import warning_for_dialect

WIDE_VALUE = Decimal("123456789012345678901234567890123456789012345678.12")
WIDE_TEXT = format(WIDE_VALUE, "f")


def write_wide_decimal_file(path: Path) -> Path:
    decimal_type = pa.decimal256(50, 2)
    schema = pa.schema([
        pa.field("amount", decimal_type),
        pa.field("items", pa.list_(decimal_type)),
    ])
    table = pa.Table.from_arrays([
        pa.array([WIDE_VALUE, Decimal("-3.05"), None], type=decimal_type),
        pa.array([[WIDE_VALUE, Decimal("-3.05")], [Decimal("-3.05")], None], type=schema.field("items").type),
    ], schema=schema)
    pq.write_table(table, path, row_group_size=1)
    return path


def write_wide_decimal_flatten_file(path: Path) -> Path:
    decimal_type = pa.decimal256(50, 2)
    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("amount", decimal_type),
        pa.field("items", pa.list_(decimal_type)),
        pa.field("tags", pa.list_(pa.string())),
    ])
    table = pa.Table.from_arrays([
        pa.array(range(5), type=pa.int64()),
        pa.array([WIDE_VALUE] * 5, type=decimal_type),
        pa.array([[WIDE_VALUE, Decimal("-3.05")]] * 5, type=schema.field("items").type),
        pa.array([["a", "b"], ["c"], [], ["d", "e"], ["f"]], type=schema.field("tags").type),
    ], schema=schema)
    pq.write_table(table, path, row_group_size=1)
    return path


def test_raw_and_flattened_previews_keep_decimal256_exact(tmp_path: Path):
    path = write_wide_decimal_file(tmp_path / "wide-decimal.parquet")

    raw = preview_parquet(path, limit=1)
    assert raw["rows"] == [{"amount": WIDE_TEXT, "items": [WIDE_TEXT, "-3.05"]}]
    assert preview_parquet(path, offset=1, limit=1)["rows"][0]["amount"] == "-3.05"

    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
    child_tables = plan_tables(
        schema, table_name="PB_TEST_WIDE", mode="child", dialect="postgres",
    )
    child_values = query_table_preview(path, child_tables[1], dialect="postgres", limit=10)
    assert [row["VALUE"] for row in child_values["rows"]] == [WIDE_TEXT, "-3.05", "-3.05"]
    assert [row["_PB_ROW_ID"] for row in child_values["rows"]] == [0, 0, 1]
    assert [row["ORD"] for row in child_values["rows"]] == [0, 1, 0]

    exploded = plan_tables(
        schema,
        table_name="PB_TEST_EXPLODED",
        mode="explode",
        dialect="postgres",
        explode_column="items",
    )[0]
    result = query_table_preview(path, exploded, dialect="postgres", limit=10)
    assert [row["ITEMS"] for row in result["rows"]] == [WIDE_TEXT, "-3.05", "-3.05", None]
    assert result["actual_rows"] == result["expected_rows"] == 4


class _CaptureCursor:
    rowcount = -1

    def __init__(self, connection):
        self.connection = connection

    def executemany(self, _sql, rows):
        self.connection.rows.extend(rows)

    def close(self):
        pass


class _CaptureConnection:
    def __init__(self):
        self.rows = []

    def cursor(self):
        return _CaptureCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_none_mode_execute_load_binds_wide_decimal_exactly(monkeypatch, tmp_path: Path):
    path = write_wide_decimal_file(tmp_path / "wide-load.parquet")
    with pq.ParquetFile(path) as parquet:
        tables = plan_tables(parquet.schema_arrow, table_name="PB_TEST_WIDE_LOAD", dialect="mysql")
    connection = _CaptureConnection()
    from app.core import jobs

    monkeypatch.setattr(jobs, "connect", lambda _profile: connection)
    monkeypatch.setattr(jobs, "_prepare_tables", lambda *_args: False)
    result = execute_load(
        path=path,
        tables=tables,
        profile=DbProfile(
            kind="mysql", host="db.invalid", port=3306, target="unit_test", user="test", password="test",
        ),
        source_rows=3,
        batch_size=2,
        existing_policy="fail",
        cancel_event=threading.Event(),
        progress=lambda *_args: None,
    )

    assert [row[0] for row in connection.rows] == [WIDE_VALUE, Decimal("-3.05"), None]
    assert result["committed_rows"] == 3


def test_child_and_explode_streams_keep_wide_decimals_and_expected_counts(tmp_path: Path):
    path = write_wide_decimal_flatten_file(tmp_path / "wide-flatten-load.parquet")
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow

    child_tables = plan_tables(schema, table_name="PB_TEST_WIDE_CHILD", mode="child", dialect="postgres")
    root = child_tables[0]
    root_batches = list(_stream_batches(path, root, "postgres", batch_size=2))
    root_rows = [row for batch in root_batches for row in batch.to_pylist()]
    root_key = next(column for column in root.columns if column.generated)
    amount = next(column for column in root.columns if column.original_name == "amount")
    assert [row[root_key.name] for row in root_rows] == [0, 1, 2, 3, 4]
    assert [row[amount.name] for row in root_rows] == [WIDE_TEXT] * 5
    assert _expected_rows(path, root, source_rows=5) == len(root_rows)

    child = next(table for table in child_tables if table.source_path == "items")
    child_batches = list(_stream_batches(path, child, "postgres", batch_size=2))
    child_rows = [row for batch in child_batches for row in batch.to_pylist()]
    child_key = next(column for column in child.columns if column.generated and column.original_name == "_PB_ROW_ID")
    value = next(column for column in child.columns if column.original_name == "VALUE")
    assert [row[child_key.name] for row in child_rows] == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    assert [row[value.name] for row in child_rows] == [WIDE_TEXT, "-3.05"] * 5
    assert _expected_rows(path, child, source_rows=5) == len(child_rows) == 10

    exploded = plan_tables(
        schema,
        table_name="PB_TEST_WIDE_EXPLODED_LOAD",
        mode="explode",
        dialect="postgres",
        explode_column="tags",
    )[0]
    explode_batches = list(_stream_batches(path, exploded, "postgres", batch_size=2))
    explode_rows = [row for batch in explode_batches for row in batch.to_pylist()]
    amount = next(column for column in exploded.columns if column.original_name == "amount")
    assert [row[amount.name] for row in explode_rows] == [WIDE_TEXT] * 7
    assert _expected_rows(path, exploded, source_rows=5) == len(explode_rows) == 7


def test_mysql_decimal_scale_over_30_uses_text_and_warns():
    result = map_arrow_type(pa.decimal128(38, 35), "mysql")
    assert result["sql_type"] == "VARCHAR(40)"
    assert {item["code"] for item in result["warnings"]} == {"W_DECIMAL_SCALE_WIDE"}

    tables = plan_tables(
        pa.schema([pa.field("amount", pa.decimal128(38, 35))]),
        table_name="PB_TEST_WIDE_SCALE",
        dialect="mysql",
    )
    ddl = generate_mysql_ddl(tables)
    assert "AMOUNT VARCHAR(40)" in ddl
    assert "W_DECIMAL_SCALE_WIDE" in {item["code"] for item in tables[0].warnings}
    warning_item = next(item for item in tables[0].warnings if item["code"] == "W_DECIMAL_SCALE_WIDE")
    assert not warning_for_dialect(warning_item, "oracle")
    assert warning_for_dialect(warning_item, "mysql")

