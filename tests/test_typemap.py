from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.model import StringLength
from app.core.typemap import (
    STRING_SAMPLE_BATCH_SIZE,
    STRING_SAMPLE_ROWS,
    map_arrow_type,
    measure_string_lengths,
    sample_row_positions,
)


def warning_codes(result: dict) -> set[str]:
    return {item["code"] for item in result["warnings"]}


def test_integer_float_decimal_and_boolean_mappings():
    expected = {
        pa.int8(): ("NUMBER(3)", "TINYINT"),
        pa.int16(): ("NUMBER(5)", "SMALLINT"),
        pa.int32(): ("NUMBER(10)", "INT"),
        pa.int64(): ("NUMBER(19)", "BIGINT"),
        pa.uint8(): ("NUMBER(3)", "TINYINT UNSIGNED"),
        pa.uint16(): ("NUMBER(5)", "SMALLINT UNSIGNED"),
        pa.uint32(): ("NUMBER(10)", "INT UNSIGNED"),
        pa.uint64(): ("NUMBER(20)", "BIGINT UNSIGNED"),
        pa.float32(): ("BINARY_FLOAT", "FLOAT"),
        pa.float64(): ("BINARY_DOUBLE", "DOUBLE"),
        pa.bool_(): ("NUMBER(1)", "TINYINT(1)"),
        pa.decimal128(12, 2): ("NUMBER(12,2)", "DECIMAL(12,2)"),
    }
    for arrow_type, (oracle, mysql) in expected.items():
        assert map_arrow_type(arrow_type, "oracle")["sql_type"] == oracle
        assert map_arrow_type(arrow_type, "mysql")["sql_type"] == mysql


def test_decimal_timestamp_date_binary_and_json_mappings():
    too_wide = pa.decimal256(76, 4)
    oracle = map_arrow_type(too_wide, "oracle")
    mysql = map_arrow_type(too_wide, "mysql")
    assert oracle["sql_type"].startswith("VARCHAR2(")
    assert "W_DECIMAL_PRECISION_WIDE" in warning_codes(oracle)
    assert mysql["sql_type"].startswith("VARCHAR(")
    assert "W_DECIMAL_PRECISION_WIDE" in warning_codes(mysql)

    for unit, precision in (("s", 0), ("ms", 3), ("us", 6), ("ns", 9)):
        assert map_arrow_type(pa.timestamp(unit), "oracle")["sql_type"] == f"TIMESTAMP({precision})"
    assert map_arrow_type(pa.timestamp("us", tz="Asia/Seoul"), "oracle")["sql_type"] == "TIMESTAMP(6) WITH TIME ZONE"
    mysql_tz = map_arrow_type(pa.timestamp("ns", tz="UTC"), "mysql")
    assert mysql_tz["sql_type"] == "DATETIME(6)"
    assert {"W_TIMESTAMP_TZ_UTC", "W_TIMESTAMP_PRECISION_LOSS"} <= warning_codes(mysql_tz)
    assert map_arrow_type(pa.date32(), "oracle")["sql_type"] == "DATE"
    assert map_arrow_type(pa.date32(), "mysql")["sql_type"] == "DATE"
    assert map_arrow_type(pa.binary(), "oracle", string_length=StringLength(bytes=2001))["sql_type"] == "BLOB"
    assert map_arrow_type(pa.binary(), "mysql", string_length=StringLength(bytes=65_536))["sql_type"] == "LONGBLOB"
    assert map_arrow_type(pa.list_(pa.int32()), "oracle")["sql_type"] == "CLOB"
    assert map_arrow_type(pa.map_(pa.string(), pa.int32()), "mysql")["sql_type"] == "JSON"


def test_string_length_strategy_and_korean_byte_limit():
    measured = StringLength(characters=4101, bytes=4101)
    assert map_arrow_type(pa.string(), "oracle", string_length=measured)["sql_type"] == "CLOB"
    assert map_arrow_type(pa.string(), "mysql", string_length=measured)["sql_type"] == "VARCHAR(4922)"
    korean = StringLength(characters=1001, bytes=3003)
    assert map_arrow_type(pa.string(), "oracle", string_length=korean)["sql_type"] == "VARCHAR2(1202 CHAR)"
    assert map_arrow_type(pa.string(), "oracle", string_strategy="max")["sql_type"] == "VARCHAR2(1000 CHAR)"
    assert map_arrow_type(pa.string(), "mysql", string_strategy="max")["sql_type"] == "VARCHAR(16383)"
    assert map_arrow_type(pa.string(), "oracle", string_strategy="lob")["sql_type"] == "CLOB"
    assert map_arrow_type(pa.string(), "mysql", string_strategy="lob")["sql_type"] == "LONGTEXT"


def test_oracle_max_is_not_narrower_than_measured_sample_and_large_text_is_safe():
    measured = StringLength(characters=1663, bytes=1663)
    sampled = map_arrow_type(
        pa.string(), "oracle", string_length=measured, string_strategy="sample", length_margin=1.0,
    )
    maximum = map_arrow_type(
        pa.string(), "oracle", string_length=measured, string_strategy="max", length_margin=1.0,
    )
    assert sampled["sql_type"] == "VARCHAR2(1663 CHAR)"
    assert maximum["sql_type"] == "CLOB"

    over_byte_limit = map_arrow_type(
        pa.string(), "oracle", string_length=StringLength(characters=1200, bytes=4801),
        string_strategy="max", length_margin=1.0,
    )
    assert over_byte_limit["sql_type"] == "CLOB"
    assert map_arrow_type(
        pa.string(), "mysql", string_length=StringLength(characters=70_000, bytes=70_000),
        string_strategy="max", length_margin=1.0,
    )["sql_type"] == "MEDIUMTEXT"
    assert map_arrow_type(
        pa.string(), "postgres", string_length=measured, string_strategy="max",
    )["sql_type"] == "TEXT"


def test_sample_positions_cover_the_full_file_and_late_row_group(tmp_path: Path):
    positions = sample_row_positions(1000, 20)
    assert len(positions) == 20
    assert positions[0] == 0
    assert positions[-1] == 999
    assert max(b - a for a, b in zip(positions, positions[1:])) <= 53

    path = tmp_path / "late-long-string.parquet"
    values = ["short"] * 990 + ["\ud55c\uae00" * 2000] * 8 + ["short"] * 2
    pq.write_table(pa.table({"comment": pa.array(values)}), path, row_group_size=10)
    lengths = measure_string_lengths(
        path, pq.ParquetFile(path).schema_arrow, length_scan="sample", sample_rows=20,
    )
    assert lengths["comment"].characters == len(values[990])
    assert lengths["comment"].bytes == len(values[990].encode("utf-8"))
    assert lengths.sampled_rows == 20
    assert lengths.total_rows == len(values)


def test_sample_windows_find_cluster_in_late_row_group_and_report_warning(tmp_path: Path):
    from app.core.flatten import plan_tables

    path = tmp_path / "late-window.parquet"
    values = ["short"] * 200_000
    long_value = "\ud55c\uae00" * 2000
    values[199_003:199_011] = [long_value] * 8
    pq.write_table(pa.table({"comment": pa.array(values)}), path, row_group_size=1000)

    schema = pq.ParquetFile(path).schema_arrow
    measured = measure_string_lengths(path, schema, length_scan="sample")
    assert measured.sampled_rows == STRING_SAMPLE_ROWS
    assert measured.total_rows == 200_000
    assert measured["comment"].characters == len(long_value)
    assert measured["comment"].bytes == len(long_value.encode("utf-8"))

    planned = plan_tables(
        schema, table_name="PB_TEST_SAMPLE", dialect="oracle", length_scan="sample", lengths=measured,
    )
    sampled_warning = next(item for item in planned[0].warnings if item["code"] == "W_STRING_LENGTH_SAMPLED")
    assert sampled_warning["params"] == {
        "sampled_rows": STRING_SAMPLE_ROWS,
        "total_rows": 200_000,
        "margin": 1.2,
    }


def test_small_file_samples_every_row_and_has_no_sample_warning(tmp_path: Path):
    from app.core.flatten import plan_tables

    path = tmp_path / "small-all-rows.parquet"
    values = ["a", "bbb", "c" * 900, None, "last"]
    pq.write_table(pa.table({"comment": pa.array(values)}), path, row_group_size=2)
    schema = pq.ParquetFile(path).schema_arrow

    measured = measure_string_lengths(path, schema, length_scan="sample")
    assert measured.sampled_rows == len(values)
    assert measured.total_rows == len(values)
    assert measured["comment"].characters == 900
    assert measured["comment"].bytes == 900
    planned = plan_tables(
        schema, table_name="PB_TEST_SMALL", dialect="oracle", length_scan="sample", lengths=measured,
    )
    assert not any(item["code"] == "W_STRING_LENGTH_SAMPLED" for item in planned[0].warnings)


def test_sampling_stops_after_last_window_batch(tmp_path: Path, monkeypatch):
    path = tmp_path / "one-large-row-group.parquet"
    pq.write_table(pa.table({"comment": pa.array(["x"] * 50_000)}), path, row_group_size=50_000)
    schema = pq.ParquetFile(path).schema_arrow
    real_parquet_file = pq.ParquetFile
    rows_read = 0

    class CountingParquetFile:
        def __init__(self, *args, **kwargs):
            self.inner = real_parquet_file(*args, **kwargs)
            self.metadata = self.inner.metadata

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def iter_batches(self, **kwargs):
            nonlocal rows_read
            for batch in self.inner.iter_batches(**kwargs):
                rows_read += batch.num_rows
                yield batch

    monkeypatch.setattr("app.core.typemap.pq.ParquetFile", CountingParquetFile)
    measured = measure_string_lengths(path, schema, length_scan="sample", sample_rows=1)
    assert measured.sampled_rows == 1
    assert rows_read < 50_000


def test_full_length_scan_finds_late_value_that_sample_misses_and_builds_wider_ddl(tmp_path: Path):
    from app.core.flatten import plan_tables

    path = tmp_path / "late-full-only.parquet"
    values = ["x"] * 110_000
    values[-1] = "z" * 2000
    pq.write_table(pa.table({"comment": pa.array(values)}), path, row_group_size=500)
    schema = pq.ParquetFile(path).schema_arrow

    full = measure_string_lengths(path, schema, length_scan="full")
    sampled = measure_string_lengths(path, schema, length_scan="sample")
    assert full.sampled_rows == full.total_rows == len(values)
    assert full.margin == 1.0
    assert full["comment"].characters == 2000
    assert sampled.sampled_rows == STRING_SAMPLE_ROWS
    assert sampled["comment"].characters == 1

    full_table = plan_tables(
        schema, table_name="PB_TEST_FULL", dialect="oracle", lengths=full, length_scan="full",
    )[0]
    sample_table = plan_tables(
        schema, table_name="PB_TEST_SAMPLE", dialect="oracle", lengths=sampled, length_scan="sample",
    )[0]
    assert full_table.columns[0].sql_type_oracle == "VARCHAR2(2000 CHAR)"
    assert sample_table.columns[0].sql_type_oracle == "VARCHAR2(2 CHAR)"
    assert not any(item["code"] == "W_STRING_LENGTH_SAMPLED" for item in full_table.warnings)
    assert any(item["code"] == "W_STRING_LENGTH_SAMPLED" for item in sample_table.warnings)

    max_lengths = measure_string_lengths(path, schema, strategy="max", length_scan="full")
    max_table = plan_tables(
        schema,
        table_name="PB_TEST_MAX",
        dialect="oracle",
        string_strategy="max",
        lengths=max_lengths,
        length_scan="full",
    )[0]
    assert max_table.columns[0].sql_type_oracle == "CLOB"


def test_full_measurement_uses_duckdb_projection_and_cache(tmp_path: Path, monkeypatch):
    path = tmp_path / "streamed-lengths.parquet"
    table = pa.table({"id": pa.array(range(5000), type=pa.int64()), "comment": ["x"] * 5000})
    pq.write_table(table, path, row_group_size=500)
    schema = table.schema
    assert pq.read_metadata(path).num_row_groups > 1

    queries: list[tuple[str, object]] = []

    class FakeConnection:
        def execute(self, sql, params=None):
            if sql.startswith("SELECT "):
                queries.append((sql, params))
            return self

        def fetchone(self):
            return (1, 1)

        def close(self):
            pass

    monkeypatch.setattr("app.core.duckdb_utils.connect_utc", FakeConnection)
    measured = measure_string_lengths(path, schema, length_scan="full")
    assert measured["comment"].characters == 1
    assert len(queries) == 1
    query, parameters = queries[0]
    # Columns are read by positional alias: "id" is __pb_src_0, "comment" is __pb_src_1.
    assert 'max(length("__pb_src_1"))' in query
    assert "max(length(\"__pb_src_0\"))" not in query and "octet_length(\"__pb_src_0\")" not in query
    assert "read_parquet(?)" in query
    assert parameters == [str(path)]
    cached = measure_string_lengths(path, schema, length_scan="full")
    assert cached == measured
    assert len(queries) == 1


def test_full_length_scan_recurses_through_nested_string_leaves(tmp_path: Path):
    struct_type = pa.struct([pa.field("display_name", pa.string())])
    item_type = pa.struct([pa.field("label", pa.string())])
    map_type = pa.map_(pa.string(), pa.string())
    schema = pa.schema([
        pa.field("profile", struct_type),
        pa.field("items", pa.list_(item_type)),
        pa.field("attributes", map_type),
    ])
    table = pa.Table.from_arrays([
        pa.array([{"display_name": "abc"}, {"display_name": "x"}], type=struct_type),
        pa.array([[{"label": "12345"}], []], type=pa.list_(item_type)),
        pa.array([[ ("key", "value-long") ], []], type=map_type),
    ], schema=schema)
    path = tmp_path / "nested-string-lengths.parquet"
    pq.write_table(table, path, row_group_size=1)

    from app.core.typemap import measure_string_lengths_pyarrow_reference

    lengths = measure_string_lengths(path, schema, length_scan="full")
    reference = measure_string_lengths_pyarrow_reference(path, schema)
    assert dict(lengths) == dict(reference)
    assert lengths["profile.display_name"].characters == 3
    assert lengths["items[].label"].characters == 5
    assert lengths["attributes.key"].characters == 3
    assert lengths["attributes.value"].characters == 10


def test_full_duckdb_scan_matches_reference_for_deep_maps_unicode_nulls_and_binary(tmp_path: Path):
    from app.core.typemap import measure_string_lengths_pyarrow_reference

    leaf_type = pa.struct([
        pa.field("label", pa.string()),
        pa.field("aliases", pa.list_(pa.string())),
    ])
    nested_type = pa.list_(pa.struct([
        pa.field("groups", pa.list_(leaf_type)),
    ]))
    map_type = pa.map_(pa.string(), pa.list_(leaf_type))
    schema = pa.schema([
        pa.field("plain", pa.string()),
        pa.field("nested", nested_type),
        pa.field("attributes", map_type),
        pa.field("payload", pa.binary()),
    ])
    wide = chr(0x00E9) + chr(0x1F600)
    table = pa.Table.from_arrays([
        pa.array([wide, None, "", chr(0x6F22)], type=pa.string()),
        pa.array([
            [{"groups": [{"label": wide, "aliases": [chr(0x6F22), ""]}]}],
            None,
            [],
            [{"groups": [{"label": "x" * 5, "aliases": None}]}],
        ], type=nested_type),
        pa.array([
            [("key", [{"label": chr(0x1F680), "aliases": ["v"]}])],
            None,
            [],
            [(wide, [{"label": "", "aliases": [wide]}])],
        ], type=map_type),
        pa.array([b"", None, b"\x00\xff", b"bytes"], type=pa.binary()),
    ], schema=schema)
    path = tmp_path / "deep-lengths.parquet"
    pq.write_table(table, path, row_group_size=1)
    assert pq.read_metadata(path).num_row_groups == 4

    lengths = measure_string_lengths(path, schema, length_scan="full")
    reference = measure_string_lengths_pyarrow_reference(path, schema)

    assert dict(lengths) == dict(reference)
    assert lengths["plain"].characters == 2
    assert lengths["plain"].bytes == 6
    assert lengths["nested[].groups[].label"].characters == 5
    assert lengths["attributes.key"].characters == 3
    assert lengths["attributes.key"].bytes == 6
    assert lengths["attributes.value[].label"].characters == 1
    assert lengths["attributes.value[].aliases[]"].characters == 2
    assert lengths["payload"].characters == 0
    assert lengths["payload"].bytes == 5
