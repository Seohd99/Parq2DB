from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.core.errors import PBError
from app.core.flatten import (
    build_select_sql,
    plan_tables,
    query_table_preview,
    sample_parent_key_duplicates,
    validate_table_name,
)
from app.core.reserved import reserved_words
from app.core.typemap import measure_string_lengths


def plan(path: Path, **options):
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
    lengths = measure_string_lengths(
        path,
        schema,
        strategy=options.get("string_strategy", "sample"),
        length_scan=options.get("length_scan", "full"),
    )
    return plan_tables(schema, table_name="PB_TEST_DATA", lengths=lengths, **options)


def test_none_mode_keeps_nested_columns_as_json(parquet_path: Path):
    models = plan(parquet_path, mode="none", dialect="oracle")
    assert len(models) == 1
    nested = {column.name: column for column in models[0].columns if column.is_json}
    assert {"LIST_INT", "LIST_STRUCT", "STRUCT_VALUE", "MAP_VALUE", "NESTED_LIST"} <= set(nested)
    result = query_table_preview(parquet_path, models[0], limit=20)
    assert result["actual_rows"] == 5
    assert result["expected_rows"] == 5
    assert isinstance(result["rows"][0]["LIST_INT"], list)


def test_child_mode_stream_plans_and_counts_all_child_rows(parquet_path: Path):
    assert not sample_parent_key_duplicates(parquet_path, "id")
    models = plan(parquet_path, mode="child", dialect="oracle", parent_key="id", parent_key_unique=True)
    assert [model.name for model in models] == [
        "PB_TEST_DATA", "PB_TEST_DATA_LIST_INT", "PB_TEST_DATA_LIST_DOUBLE",
        "PB_TEST_DATA_LIST_STRUCT", "PB_TEST_DATA_MAP_VALUE", "PB_TEST_DATA_NESTED_LIST",
        "PB_TEST_DATA_NESTED_LIST_ITEM",
    ]
    root = models[0]
    assert root.parent_key == ("ID",)
    assert "PB_TEST_DATA_LIST_INT" in root.child_tables
    for child in models[1:]:
        result = query_table_preview(parquet_path, child, limit=50)
        assert result["actual_rows"] == result["expected_rows"]
    nested = next(model for model in models if model.name.endswith("NESTED_LIST_ITEM"))
    assert nested.parent_table == "PB_TEST_DATA_NESTED_LIST"
    assert "PARENT_ORD" in [column.name for column in nested.columns]


def test_generated_parent_row_id_is_zero_based_and_streaming(parquet_path: Path):
    models = plan(parquet_path, mode="child", dialect="oracle")
    root = models[0]
    assert root.columns[0].name == "PB_ROW_ID"
    assert root.select_plan.row_id_strategy == "file_row_number"
    assert "file_row_number=true" in build_select_sql(root)
    assert "row_number() OVER ()" not in build_select_sql(root)
    preview = query_table_preview(parquet_path, root, limit=5)
    assert [row["PB_ROW_ID"] for row in preview["rows"]] == [0, 1, 2, 3, 4]


def test_generated_id_handles_physical_file_row_number_column(tmp_path: Path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "row-number-collision.parquet"
    schema = pa.schema([
        pa.field("file_row_number", pa.int32()),
        pa.field("items", pa.list_(pa.int32())),
    ])
    pq.write_table(pa.Table.from_arrays([
        pa.array([90, 91], type=pa.int32()),
        pa.array([[1], [2, 3]], type=schema.field("items").type),
    ], schema=schema), path)
    models = plan(path, mode="child", dialect="oracle")
    root = models[0]
    assert root.select_plan.row_id_strategy == "window"
    assert "row_number() OVER ()" in build_select_sql(root)
    preview = query_table_preview(path, root, limit=5)
    assert [row["PB_ROW_ID"] for row in preview["rows"]] == [0, 1]


def test_child_depth_limit_keeps_overflow_as_json(parquet_path: Path):
    models = plan(
        parquet_path,
        mode="child",
        dialect="oracle",
        parent_key="id",
        parent_key_unique=True,
        child_max_depth=1,
    )
    assert not any(model.name.endswith("NESTED_LIST_ITEM") for model in models)
    nested = next(model for model in models if model.name.endswith("NESTED_LIST"))
    value = next(column for column in nested.columns if column.name == "VALUE")
    assert value.is_json


@pytest.mark.parametrize(("preserve_empty", "expected"), [(True, 7), (False, 5)])
def test_explode_expected_formula_matches_duckdb(parquet_path: Path, preserve_empty: bool, expected: int):
    models = plan(
        parquet_path,
        mode="explode",
        dialect="mysql",
        explode_column="list_int",
        preserve_empty=preserve_empty,
    )
    result = query_table_preview(parquet_path, models[0], dialect="mysql", limit=50)
    assert result["expected_rows"] == expected
    assert result["actual_rows"] == expected
    if preserve_empty:
        assert sum(row["LIST_INT"] is None for row in result["rows"]) == 2


def test_identifiers_collisions_reserved_words_and_quoted_names():
    import pyarrow as pa

    schema = pa.schema([
        pa.field("level", pa.int32()), pa.field("date", pa.string()),
        pa.field("comment", pa.string()), pa.field("a-b", pa.int8()), pa.field("a b", pa.int8()),
    ])
    models = plan_tables(schema, table_name="GOOD_NAME", mode="none", dialect="oracle")
    names = [column.name for column in models[0].columns]
    assert names == ["LEVEL_2", "DATE_2", "COMMENT_2", "A_B", "A_B_2"]
    codes = {item["code"] for item in models[0].warnings}
    assert "W_RESERVED_IDENTIFIER" in codes
    assert "W_IDENTIFIER_COLLISION" in codes

    quoted = plan_tables(schema, table_name="GOOD_NAME", mode="none", dialect="oracle", column_policy="quoted")
    assert [column.name for column in quoted[0].columns][:3] == ["level", "date", "comment"]
    assert "ONLINE" in reserved_words("oracle", lambda _: ["ONLINE"])
    online_override = plan_tables(
        pa.schema([pa.field("custom_reserved", pa.int32())]),
        table_name="GOOD_NAME",
        mode="none",
        dialect="oracle",
        reserved_word_provider=lambda _: ["CUSTOM_RESERVED"],
    )
    assert online_override[0].columns[0].name == "CUSTOM_RESERVED_2"


def test_table_name_validation_returns_error_code():
    with pytest.raises(PBError) as error:
        validate_table_name("bad-name", "oracle")
    assert error.value.code == "E_TABLE_NAME_INVALID"
    with pytest.raises(PBError) as error:
        validate_table_name("A" * 65, "mysql")
    assert error.value.code == "E_TABLE_NAME_INVALID"


def test_child_preview_reads_only_the_selected_file_when_path_has_glob_characters(tmp_path: Path):
    import pyarrow as pa

    for folder, values in (("[ab]", [[1, 2]]), ("a", [[7, 8, 9]])):
        (tmp_path / folder).mkdir()
        pq.write_table(pa.table({"items": pa.array(values, type=pa.list_(pa.int64()))}), tmp_path / folder / "f.parquet")
    path = tmp_path / "[ab]" / "f.parquet"
    models = plan(path, mode="child", dialect="postgres")
    child = query_table_preview(path, models[1])
    assert [row["VALUE"] for row in child["rows"]] == [1, 2]


@pytest.mark.parametrize("mode", ["none", "child"])
def test_columns_differing_only_by_case_keep_their_own_values(tmp_path: Path, mode: str):
    import pyarrow as pa

    path = tmp_path / "case.parquet"
    pq.write_table(pa.table({
        "A": [1, 2],
        "a": [99, 98],
        "items": pa.array([[5], [6]], type=pa.list_(pa.int64())),
    }), path)
    models = plan(path, mode=mode, dialect="postgres")
    rows = query_table_preview(path, models[0])["rows"]
    assert [(row["A"], row["A_2"]) for row in rows] == [(1, 99), (2, 98)]


def test_struct_fields_differing_only_by_case_are_rejected(tmp_path: Path):
    import pyarrow as pa

    struct = pa.StructArray.from_arrays([pa.array([1]), pa.array([2])], names=["X", "x"])
    path = tmp_path / "case-struct.parquet"
    pq.write_table(pa.table({"s": struct}), path)
    with pytest.raises(PBError) as error:
        plan(path, mode="none", dialect="postgres")
    assert error.value.code == "E_SCHEMA_NAME_COLLISION"
    assert error.value.params == {"path": "s.x"}


def test_oracle_unquoted_identifiers_start_with_a_letter(tmp_path: Path):
    import pyarrow as pa

    path = tmp_path / "oracle-names.parquet"
    pq.write_table(pa.table({
        "_id": [1],
        "1st": [2],
        "items": pa.array([[5]], type=pa.list_(pa.int64())),
    }), path)
    models = plan(path, mode="child", dialect="oracle")
    names = [table.name for table in models] + [column.name for table in models for column in table.columns]
    assert all(name[0].isalpha() for name in names), names
    assert [column.name for column in models[0].columns] == ["PB_ROW_ID", "ID", "C_1ST"]
    quoted = plan(path, mode="child", dialect="oracle", column_policy="quoted")
    assert quoted[0].columns[0].name == "_PB_ROW_ID"
    with pytest.raises(PBError) as error:
        validate_table_name("_ORDERS", "oracle")
    assert error.value.code == "E_TABLE_NAME_INVALID"
    assert validate_table_name("_ORDERS", "oracle", "quoted") == "_ORDERS"
    assert validate_table_name("_ORDERS", "postgres") == "_ORDERS"


@pytest.mark.parametrize("policy", ["upper_unquoted", "quoted"])
def test_inherited_parent_key_name_is_reserved_in_child_tables(tmp_path: Path, policy: str):
    import pyarrow as pa

    path = tmp_path / "key-ord.parquet"
    pq.write_table(pa.table({
        "ORD": [10, 20],
        "items": pa.array([[1], [2, 3]], type=pa.list_(pa.int64())),
    }), path)
    models = plan(path, mode="child", dialect="postgres", parent_key="ORD", column_policy=policy)
    child = models[1]
    assert [column.name for column in child.columns] == ["ORD", "ORD_2", "VALUE"]
    rows = query_table_preview(path, child)["rows"]
    assert [(row["ORD"], row["ORD_2"], row["VALUE"]) for row in rows] == [(10, 0, 1), (20, 0, 2), (20, 1, 3)]


def test_child_table_name_never_equals_a_max_length_parent_name(tmp_path: Path):
    import pyarrow as pa

    path = tmp_path / "long.parquet"
    pq.write_table(pa.table({"items": pa.array([[1]], type=pa.list_(pa.int64()))}), path)
    parent = "P" * 63
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
    models = plan_tables(schema, table_name=parent, mode="child", dialect="postgres")
    names = [table.name for table in models]
    assert len(set(names)) == len(names)
    assert names[0] == parent
    assert len(names[1].encode("utf-8")) <= 63


def test_mysql_quoted_columns_cannot_differ_only_by_case():
    import pyarrow as pa

    schema = pa.schema([("A", pa.int64()), ("a", pa.int64())])
    with pytest.raises(PBError) as caught:
        plan_tables(schema, table_name="PB_TEST_CASE", dialect="mysql", column_policy="quoted")
    assert caught.value.code == "E_IDENTIFIER_INVALID"
    for dialect in ("postgres", "oracle"):
        tables = plan_tables(schema, table_name="PB_TEST_CASE", dialect=dialect, column_policy="quoted")
        assert [column.name for column in tables[0].columns] == ["A", "a"]


def _selection_file(tmp_path: Path) -> Path:
    import pyarrow as pa

    path = tmp_path / "selection.parquet"
    pq.write_table(pa.table({
        "id": [1, 2],
        "name": ["a", "b"],
        "score": [10, 20],
        "tags": pa.array([["x"], ["y", "z"]], type=pa.list_(pa.string())),
    }), path)
    return path


def test_column_selection_limits_ddl_columns_and_preview(tmp_path: Path):
    path = _selection_file(tmp_path)
    models = plan(path, mode="none", dialect="postgres", columns=["score", "id"])
    root = models[0]
    # Selected columns keep file order regardless of request order.
    assert [column.name for column in root.columns] == ["ID", "SCORE"]
    assert root.select_plan.read_columns == ("id", "score")
    assert root.select_plan.read_aliases == ("__pb_src_0", "__pb_src_2")
    rows = query_table_preview(path, root)["rows"]
    assert rows == [{"ID": 1, "SCORE": 10}, {"ID": 2, "SCORE": 20}]


def test_column_selection_without_a_list_column_drops_its_child_table(tmp_path: Path):
    path = _selection_file(tmp_path)
    assert [table.name for table in plan(path, mode="child", dialect="postgres")] == ["PB_TEST_DATA", "PB_TEST_DATA_TAGS"]
    models = plan(path, mode="child", dialect="postgres", columns=["id", "name"])
    assert [table.name for table in models] == ["PB_TEST_DATA"]
    assert [column.name for column in models[0].columns] == ["_PB_ROW_ID", "ID", "NAME"]


@pytest.mark.parametrize("options, code, params", [
    ({"columns": []}, "E_COLUMNS_EMPTY", {}),
    ({"columns": ["id", "missing"]}, "E_COLUMN_NOT_FOUND", {"column": "missing"}),
    ({"mode": "child", "parent_key": "id", "columns": ["name", "tags"]}, "E_COLUMN_NOT_SELECTED", {"column": "id"}),
    ({"mode": "explode", "explode_column": "tags", "columns": ["id"]}, "E_COLUMN_NOT_SELECTED", {"column": "tags"}),
])
def test_invalid_column_selection_is_rejected(tmp_path: Path, options, code, params):
    path = _selection_file(tmp_path)
    with pytest.raises(PBError) as error:
        plan(path, dialect="postgres", **{"mode": "none", **options})
    assert error.value.code == code
    assert error.value.params == params


@pytest.mark.parametrize("dialect", ["oracle", "postgres"])
def test_generated_name_suffix_fits_multibyte_names_within_byte_limit(dialect: str):
    from app.core.flatten import _NameAllocator

    allocator = _NameAllocator(dialect, "quoted")
    base = "가" * (allocator.max_length // 3) + "a" * (allocator.max_length % 3)
    assert allocator.allocate(base) == base
    renamed = allocator.allocate_generated(base)
    assert renamed != base and renamed.endswith("_2")
    assert len(renamed.encode("utf-8")) <= allocator.max_length
