"""Build table plans and DDL results; shared by the web API and the CLI.

The ``request`` argument only needs ``request.app.state.config``, so the CLI passes a
lightweight stand-in instead of a FastAPI request.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.api.deps import get_config
from app.api.schemas import DdlOptions
from app.core.config import AppConfig
from app.core.db import close_quietly, connect
from app.core.ddl_mysql import generate_mysql_ddl
from app.core.ddl_oracle import generate_oracle_ddl
from app.core.ddl_postgres import generate_postgres_ddl
from app.core.dialects import SUPPORTED_DIALECTS
from app.core.errors import PBError, warning
from app.core.files import SelectedFile
from app.core.flatten import (
    build_expected_count_sql,
    build_select_sql,
    default_table_name,
    plan_tables,
    query_table_preview,
    sample_parent_key_duplicates,
)
from app.core.model import StringLengthMeasurements, TableModel
from app.core.reserved import fetch_oracle_reserved_words
from app.core.typemap import container_kind, measure_string_lengths


def warning_for_dialect(item: dict, dialect: str) -> bool:
    code = item.get("code")
    params = item.get("params") or {}
    if code in {"W_DECIMAL_PRECISION_WIDE", "W_UNSUPPORTED_TYPE"}:
        return params.get("dialect") == dialect
    if code == "W_TIMESTAMP_TZ_UTC":
        return dialect == "mysql"
    if code in {"W_TIMESTAMP_PRECISION_LOSS", "W_TIME_PRECISION_LOSS"}:
        return params.get("dialect", "mysql") == dialect
    return True


def public_table(table: TableModel, counts: dict[str, dict], dialect: str) -> dict:
    count = counts.get(table.name, {})
    return {
        "name": table.name,
        "parent_table": table.parent_table,
        "child_tables": list(table.child_tables),
        "parent_key": list(table.parent_key),
        "parent_key_unique": table.parent_key_unique,
        "source_path": table.source_path,
        "actual_rows": count.get("actual_rows"),
        "expected_rows": count.get("expected_rows"),
        "columns": [
            {
                "original_name": column.original_name,
                "original_path": column.original_path,
                "name": column.name,
                "arrow_type": str(column.arrow_type),
                "logical_type": column.logical_type,
                "sql_type": column.sql_type(dialect),
                "nullable": column.nullable,
                "length": column.length,
                "byte_length": column.byte_length,
                "precision": column.precision,
                "scale": column.scale,
                "is_json": column.is_json,
                "generated": column.generated,
            }
            for column in table.columns
        ],
    }


def oracle_reserved_provider(request: Any, dialect: str):
    """Use V$RESERVED_WORDS when the Oracle profile is online; otherwise use built-ins."""
    if dialect != "oracle":
        return None, []
    profile = get_config(request).db.get("oracle")
    if profile is None or not profile.online:
        return None, []
    connection = None
    try:
        connection = connect(profile)
        words = fetch_oracle_reserved_words(connection)
        return lambda _dialect: words, []
    except Exception:
        return None, [warning("W_RESERVED_WORDS_UNAVAILABLE")]
    finally:
        if connection is not None:
            close_quietly(connection)


def build_models(
    selected: SelectedFile,
    options: DdlOptions,
    request: Any,
    *,
    lengths_override: StringLengthMeasurements | None = None,
    effective_length_scan: str | None = None,
    config: AppConfig | None = None,
    reserved_result: tuple[Any, list[dict]] | None = None,
) -> tuple[list[TableModel], int, list[dict], pa.Schema]:
    """Build the shared table plan once for DDL preview or a load job."""
    if options.dialect not in SUPPORTED_DIALECTS:
        raise PBError("E_DIALECT_INVALID", dialect=options.dialect)
    with pq.ParquetFile(selected.path) as parquet:
        schema = parquet.schema_arrow
        source_rows = parquet.metadata.num_rows

    if options.mode == "child" and options.parent_key:
        key_field = next((field for field in schema if field.name == options.parent_key), None)
        if key_field is None or container_kind(key_field.type) is not None:
            raise PBError("E_PARENT_KEY_INVALID", column=options.parent_key)

    scan_mode = effective_length_scan or options.length_scan
    lengths = lengths_override
    if lengths is None:
        lengths = measure_string_lengths(
            selected.path,
            schema,
            strategy=options.string_strategy,
            length_scan=scan_mode,
        )
    key_unique: bool | None = None
    if options.mode == "child" and options.parent_key:
        key_unique = not sample_parent_key_duplicates(selected.path, options.parent_key)

    table_name = options.table_name or default_table_name(selected.display_name, options.dialect)
    reserved_provider, reserved_warnings = reserved_result or oracle_reserved_provider(request, options.dialect)
    if reserved_provider is not None and table_name.upper() in set(reserved_provider(options.dialect)):
        raise PBError("E_TABLE_NAME_INVALID", dialect=options.dialect, max_length=128)
    models = plan_tables(
        schema,
        table_name=table_name,
        mode=options.mode,
        dialect=options.dialect,
        column_policy=options.column_policy,
        string_strategy=options.string_strategy,
        length_scan=scan_mode,
        lengths=lengths,
        json_check=options.json_check,
        parent_key=options.parent_key if options.mode == "child" else None,
        parent_key_unique=key_unique,
        explode_column=options.explode_column,
        preserve_empty=options.preserve_empty,
        child_max_depth=(config or get_config(request)).load.child_max_depth,
        reserved_word_provider=reserved_provider,
        columns=options.columns,
    )
    return models, source_rows, reserved_warnings, schema


def build_ddl_result(
    selected: SelectedFile,
    options: DdlOptions,
    request: Any,
    *,
    include_preview: bool,
) -> dict:
    if options.preview_offset < 0 or options.preview_limit < 1 or options.preview_limit > 200:
        raise PBError("E_BAD_REQUEST", fields=["preview_offset", "preview_limit"])
    models, source_rows, reserved_warnings, schema = build_models(selected, options, request)

    ddl = {
        "oracle": lambda: generate_oracle_ddl(models, json_check=options.json_check),
        "mysql": lambda: generate_mysql_ddl(models),
        "postgres": lambda: generate_postgres_ddl(models),
    }[options.dialect]()
    previews: list[dict] = []
    counts: dict[str, dict] = {}
    if include_preview:
        for table in models:
            preview = query_table_preview(
                selected.path,
                table,
                dialect=options.dialect,
                offset=options.preview_offset,
                limit=options.preview_limit,
            )
            previews.append(preview)
            counts[table.name] = preview

    warnings = reserved_warnings + [
        item
        for table in models
        for item in table.warnings
        if warning_for_dialect(item, options.dialect)
    ]
    if options.mode == "explode" and previews:
        expected = previews[0]["expected_rows"]
        if source_rows > 0 and expected > source_rows * 10:
            warnings.append(warning(
                "W_EXPLODE_ROW_MULTIPLIER",
                column=options.explode_column or "",
                expected_rows=expected,
                multiplier=round(expected / source_rows, 1),
            ))
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in warnings:
        key = (item["code"], repr(sorted((item.get("params") or {}).items())))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    mappings = [
        {
            "table": table.name,
            "original_name": column.original_name,
            "original_path": column.original_path,
            "name": column.name,
            "generated": column.generated,
        }
        for table in models
        for column in table.columns
    ]
    tables = []
    for table in models:
        table_view = public_table(table, counts, options.dialect)
        table_view["sql_types"] = {
            "oracle": [column.sql_type("oracle") for column in table.columns],
            "mysql": [column.sql_type("mysql") for column in table.columns],
            "postgres": [column.sql_type("postgres") for column in table.columns],
        }
        tables.append(table_view)
    return {
        "dialect": options.dialect,
        "mode": options.mode,
        "table_name": models[0].name,
        "ddl": ddl,
        "tables": tables,
        "column_mappings": mappings,
        "warnings": deduped,
        "previews": previews,
        "select_sql": [
            {
                "table": table.name,
                "sql": build_select_sql(table, dialect=options.dialect),
                "expected_count_sql": build_expected_count_sql(table),
                "parameters": ["parquet_path"],
            }
            for table in models
        ],
    }
