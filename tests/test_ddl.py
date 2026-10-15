from __future__ import annotations

import os
from pathlib import Path

import pyarrow.parquet as pq

from app.core.ddl_mysql import generate_mysql_ddl
from app.core.ddl_oracle import generate_oracle_ddl
from app.core.flatten import plan_tables
from app.core.model import StringLength
from app.core.typemap import MYSQL_ROW_SIZE_BUDGET, measure_string_lengths, mysql_row_size_bytes

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def build_models(parquet_path: Path, mode: str, dialect: str):
    with pq.ParquetFile(parquet_path) as parquet:
        schema = parquet.schema_arrow
    lengths = measure_string_lengths(parquet_path, schema, length_scan="sample")
    return plan_tables(
        schema,
        table_name="PB_TEST_DATA",
        mode=mode,
        dialect=dialect,
        length_scan="sample",
        parent_key="id" if mode == "child" else None,
        parent_key_unique=True if mode == "child" else None,
        explode_column="list_int" if mode == "explode" else None,
        lengths=lengths,
        json_check=True,
    )


def test_oracle_and_mysql_ddl_goldens_for_all_modes(parquet_path: Path):
    for mode in ("none", "child", "explode"):
        for dialect in ("oracle", "mysql"):
            models = build_models(parquet_path, mode, dialect)
            actual = (
                generate_oracle_ddl(models, json_check=True)
                if dialect == "oracle"
                else generate_mysql_ddl(models)
            )
            golden_path = GOLDEN_DIR / f"{mode}_{dialect}.sql"
            if os.environ.get("UPDATE_GOLDEN") == "1":
                GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
                golden_path.write_text(actual, encoding="utf-8")
            assert golden_path.read_text(encoding="utf-8") == actual


def test_quoted_identifier_escaping():
    import pyarrow as pa

    schema = pa.schema([pa.field("odd\"name", pa.string())])
    oracle_models = plan_tables(
        schema, table_name="PB_TEST_QUOTED", mode="none", dialect="oracle", column_policy="quoted",
    )
    mysql_models = plan_tables(
        schema, table_name="PB_TEST_QUOTED", mode="none", dialect="mysql", column_policy="quoted",
    )
    assert '"odd""name"' in generate_oracle_ddl(oracle_models)
    assert '`odd"name`' in generate_mysql_ddl(mysql_models)


def test_mysql_table_row_size_demotes_largest_varchars():
    import pyarrow as pa

    schema = pa.schema([pa.field(f"body_{index}", pa.string()) for index in range(4)])
    lengths = {field.name: StringLength(characters=10_000, bytes=10_000) for field in schema}
    models = plan_tables(
        schema,
        table_name="PB_TEST_ROW_SIZE",
        dialect="mysql",
        lengths=lengths,
    )
    table = models[0]

    assert mysql_row_size_bytes(table.columns) <= MYSQL_ROW_SIZE_BUDGET
    warning = next(item for item in table.warnings if item["code"] == "W_MYSQL_ROW_SIZE_DEMOTED")
    assert len(warning["params"]["columns"]) == 3
    assert sum(column.sql_type_mysql == "TEXT" for column in table.columns) == 3
    assert "W_MYSQL_ROW_SIZE_DEMOTED" in {
        item["code"] for item in table.warnings
    }
    assert generate_mysql_ddl(models).count(" TEXT") == 3
