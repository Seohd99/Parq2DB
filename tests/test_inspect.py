from __future__ import annotations

from shutil import copyfile

import pyarrow.parquet as pq
import pytest

from app.core.errors import PBError
from app.core.inspect import MAX_PAGE_SIZE, classify_schema, inspect_parquet, preview_parquet


def test_metadata_statistics_and_json_safe_values(parquet_path):
    result = inspect_parquet(parquet_path)
    meta = result["metadata"]
    assert meta["num_rows"] == 5
    assert meta["num_row_groups"] == 3
    assert meta["file_size"] == parquet_path.stat().st_size
    assert meta["created_by"]
    assert meta["format_version"]
    assert meta["compression"] == ["snappy"]

    stats = {row["column"]: row for row in result["statistics"]}
    assert stats["id"]["null_count"] == 0
    assert stats["id"]["min"] == 1
    assert stats["id"]["max"] == 5
    assert stats["comment"]["null_count"] == 1
    # A row group with long strings may omit min/max statistics, so they appear as None.
    assert stats["comment"]["min"] is None
    assert stats["comment"]["max"] is None
    assert stats["level"]["min"] == "debug"
    assert stats["level"]["max"] == "warn"
    assert stats["event_ts"]["min"] == "2024-01-01T12:00:00Z"
    assert stats["event_ts"]["max"] == "2024-01-05T12:00:00Z"
    assert stats["id"]["compression"] == ["snappy"]


def test_schema_tree_marks_map_separately_and_reports_depth(parquet_path, flat_parquet_path):
    result = inspect_parquet(parquet_path)
    fields = {field["name"]: field for field in result["schema"]["fields"]}
    assert fields["map_value"]["kind"] == "map"
    assert len(fields["map_value"]["children"]) == 2
    assert fields["list_int"]["kind"] == "list"
    assert fields["struct_value"]["kind"] == "struct"

    nested = {column["name"]: column for column in result["classification"]["nested_columns"]}
    assert result["classification"]["kind"] == "nested"
    assert nested["list_int"]["max_depth"] == 1
    assert nested["list_struct"]["max_depth"] == 2
    assert nested["struct_value"]["max_depth"] == 1
    assert nested["map_value"]["kind"] == "map"
    assert nested["nested_list"]["max_depth"] == 2

    flat = classify_schema(pq.ParquetFile(flat_parquet_path).schema_arrow)
    assert flat == {"kind": "flat", "nested_columns": []}


def test_preview_paging_offsets_and_serialization(parquet_path):
    first = preview_parquet(parquet_path, offset=0, limit=2)
    second = preview_parquet(parquet_path, offset=2, limit=2)
    last = preview_parquet(parquet_path, offset=4, limit=2)
    beyond = preview_parquet(parquet_path, offset=5, limit=2)

    assert [row["id"] for row in first["rows"]] == [1, 2]
    assert first["has_next"] is True
    assert [row["id"] for row in second["rows"]] == [3, 4]
    assert [row["id"] for row in last["rows"]] == [5]
    assert last["has_next"] is False
    assert beyond["rows"] == []
    assert beyond["has_next"] is False
    assert first["rows"][0]["list_int"] == [10, 20]
    assert first["rows"][0]["list_double"] == [1.5, 2.25]
    assert all(isinstance(value, float) for value in first["rows"][0]["list_double"])
    assert isinstance(first["rows"][0]["event_ts"], str)
    assert first["rows"][0]["amount"] == "10.25"
    assert first["rows"][0]["map_value"] is not None


def test_preview_enforces_page_size_ceiling(parquet_path):
    with pytest.raises(PBError) as error:
        preview_parquet(parquet_path, limit=MAX_PAGE_SIZE + 1)
    assert error.value.code == "E_BAD_REQUEST"


def test_preview_binds_path_instead_of_interpolating_sql(parquet_path, tmp_path):
    unusual_path = tmp_path / "file'); SELECT 1 --.parquet"
    copyfile(parquet_path, unusual_path)
    result = preview_parquet(unusual_path, limit=1)
    assert result["rows"][0]["id"] == 1



def test_preview_reads_only_the_selected_file_when_path_has_glob_characters(tmp_path):
    import pyarrow as pa

    selected_dir = tmp_path / "[ab]"
    sibling_dir = tmp_path / "a"
    selected_dir.mkdir()
    sibling_dir.mkdir()
    pq.write_table(pa.table({"x": [1]}), selected_dir / "f.parquet")
    pq.write_table(pa.table({"x": [999]}), sibling_dir / "f.parquet")

    preview = preview_parquet(selected_dir / "f.parquet")
    assert preview["rows"] == [{"x": 1}]


def test_preview_keeps_columns_that_differ_only_by_case(tmp_path):
    import pyarrow as pa

    path = tmp_path / "case.parquet"
    pq.write_table(pa.table({"A": [1], "a": [99]}), path)
    preview = preview_parquet(path)
    assert preview["columns"] == ["A", "a"]
    assert preview["rows"] == [{"A": 1, "a": 99}]
