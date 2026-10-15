"""Create test Parquet files in temporary locations at runtime."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _write_parquet(path: Path, table: pa.Table, row_group_size: int = 2) -> Path:
    pq.write_table(table, path, row_group_size=row_group_size, compression="snappy")
    return path


@pytest.fixture
def parquet_path(tmp_path: Path) -> Path:
    """Create a three-row-group file with flat, list/struct/map, and varied Arrow values."""
    list_struct_type = pa.list_(pa.struct([
        pa.field("name", pa.string()),
        pa.field("score", pa.int64()),
    ]))
    nested_list_type = pa.list_(pa.list_(pa.int32()))
    map_type = pa.map_(pa.string(), pa.int32())
    schema = pa.schema([
        pa.field("id", pa.int32(), nullable=False),
        pa.field("level", pa.string()),
        pa.field("date", pa.date32()),
        pa.field("comment", pa.string()),
        pa.field("list_int", pa.list_(pa.int64())),
        pa.field("list_double", pa.list_(pa.float64())),
        pa.field("list_struct", list_struct_type),
        pa.field("struct_value", pa.struct([
            pa.field("city", pa.string()),
            pa.field("active", pa.bool_()),
        ])),
        pa.field("map_value", map_type),
        pa.field("nested_list", nested_list_type),
        pa.field("event_ts", pa.timestamp("us", tz="Asia/Seoul")),
        pa.field("amount", pa.decimal128(12, 2)),
    ])
    values = [
        pa.array([1, 2, 3, 4, 5], type=pa.int32()),
        pa.array(["info", "debug", "warn", "error", "info"]),
        pa.array([date(2024, 1, 1), date(2024, 1, 2), None, date(2024, 1, 4), date(2024, 1, 5)], type=pa.date32()),
        pa.array(["한글 문자열", None, "x" * 4101, "four", "five"]),
        pa.array([[10, 20], None, [], [30], [40, 50]], type=pa.list_(pa.int64())),
        pa.array([[1.5, 2.25], None, [], [3.125], [4.5]], type=pa.list_(pa.float64())),
        pa.array([
            [{"name": "alpha", "score": 1}], None, [],
            [{"name": "한글", "score": 2}, {"name": "beta", "score": 3}],
            [{"name": "omega", "score": 5}],
        ], type=list_struct_type),
        pa.array([
            {"city": "Seoul", "active": True}, None,
            {"city": "Busan", "active": False}, {"city": "Incheon", "active": True},
            {"city": "Daejeon", "active": None},
        ], type=schema.field("struct_value").type),
        pa.array([
            [("a", 1)], None, [], [("b", 2)], [("c", 3), ("d", 4)],
        ], type=map_type),
        pa.array([[[1], [2, 3]], None, [], [[4]], [[5, 6]]], type=nested_list_type),
        pa.array([
            datetime(2024, 1, day, 12, 0, tzinfo=timezone.utc)
            for day in (1, 2, 3, 4, 5)
        ], type=pa.timestamp("us", tz="Asia/Seoul")),
        pa.array([Decimal("10.25"), Decimal("20.50"), None, Decimal("0.01"), Decimal("99.99")], type=pa.decimal128(12, 2)),
    ]
    table = pa.Table.from_arrays(values, schema=schema)
    return _write_parquet(tmp_path / "inspection.parquet", table, row_group_size=2)


@pytest.fixture
def flat_parquet_path(tmp_path: Path) -> Path:
    schema = pa.schema([
        pa.field("id", pa.int64(), nullable=False),
        pa.field("name", pa.string()),
        pa.field("active", pa.bool_()),
    ])
    table = pa.Table.from_arrays([
        pa.array([1, 2, 3], type=pa.int64()),
        pa.array(["한글", "two", "three"]),
        pa.array([True, False, True]),
    ], schema=schema)
    return _write_parquet(tmp_path / "flat.parquet", table, row_group_size=1)
