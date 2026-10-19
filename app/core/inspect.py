"""Provide Parquet metadata, statistics, schema, and paged previews."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.errors import PBError
from app.core.duckdb_utils import (
    alias_list,
    connect_utc,
    duckdb_compatible_batch,
    has_wide_decimal,
    literal_path,
    quote_identifier,
    source_aliases,
    validate_source_schema,
)
from app.core.typemap import container_kind

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50
PREVIEW_ARROW_BATCH_SIZE = 10_000


def _nested_depth(data_type: pa.DataType) -> int:
    """Return the deepest path, counting each nested container as one level."""
    kind = container_kind(data_type)
    if kind is None:
        return 0
    if kind == "struct":
        child_depths = [_nested_depth(field.type) for field in data_type]
    elif kind == "map":
        child_depths = [_nested_depth(data_type.key_type), _nested_depth(data_type.item_type)]
    else:
        child_depths = [_nested_depth(data_type.value_type)]
    return 1 + max(child_depths, default=0)


def classify_schema(schema: pa.Schema) -> dict[str, Any]:
    """Classify the schema as flat or nested and find each nested column's maximum container depth.

    This is a pure function and does not read file contents or database state.
    """
    nested_columns = [
        {"name": field.name, "kind": container_kind(field.type), "max_depth": _nested_depth(field.type)}
        for field in schema
        if container_kind(field.type) is not None
    ]
    return {"kind": "nested" if nested_columns else "flat", "nested_columns": nested_columns}


def schema_tree(schema: pa.Schema) -> dict[str, Any]:
    """Convert an Arrow schema to a recursive tree that preserves map kinds."""
    return {"fields": [_field_tree(field) for field in schema]}


def _field_tree(field: pa.Field) -> dict[str, Any]:
    data_type = field.type
    kind = container_kind(data_type) or "scalar"
    node: dict[str, Any] = {
        "name": field.name,
        "type": str(data_type),
        "nullable": field.nullable,
        "kind": kind,
        "children": [],
    }
    if kind == "struct":
        node["children"] = [_field_tree(child) for child in data_type]
    elif kind == "map":
        node["children"] = [
            _field_tree(data_type.key_field),
            _field_tree(data_type.item_field),
        ]
    elif kind == "list":
        value_field = getattr(data_type, "value_field", pa.field("item", data_type.value_type))
        node["children"] = [_field_tree(value_field)]
    return node


def _json_safe(value: Any) -> Any:
    """Convert Arrow or database values to JSON-safe built-in types."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        rendered = value.isoformat()
        return rendered.replace("+00:00", "Z") if value.utcoffset() == timedelta(0) else rendered
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$binary_base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(_json_safe(key)): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "as_py"):
        return _json_safe(value.as_py())
    return str(value)


def _stat_value(stats: Any, name: str, data_type: pa.DataType | None) -> Any:
    """Convert the raw epoch to UTC if a statistics accessor requires a timezone database."""
    try:
        return getattr(stats, name)
    except Exception:
        raw = getattr(stats, f"{name}_raw")
        if data_type is not None and pa.types.is_timestamp(data_type) and isinstance(raw, int):
            unit_micros = {"s": 1_000_000, "ms": 1_000, "us": 1, "ns": 0}[data_type.unit]
            epoch_micros = raw // 1_000 if data_type.unit == "ns" else raw * unit_micros
            value = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=epoch_micros)
            return value.isoformat().replace("+00:00", "Z")
        return raw


def _parquet_file(path: Path) -> pq.ParquetFile:
    try:
        return pq.ParquetFile(path)
    except Exception as exc:
        raise PBError("E_PARQUET_INVALID") from exc


def inspect_parquet(path: Path) -> dict[str, Any]:
    """Return footer-based metadata, statistics, a schema tree, and type classifications."""
    with _parquet_file(path) as parquet:
        return _inspect_parquet_handle(path, parquet)


def _inspect_parquet_handle(path: Path, parquet: pq.ParquetFile) -> dict[str, Any]:
    metadata = parquet.metadata
    column_count = metadata.num_columns
    row_groups = metadata.num_row_groups
    statistics: list[dict[str, Any]] = []
    codecs: dict[str, set[str]] = {}

    for column_index in range(column_count):
        first_column = metadata.row_group(0).column(column_index) if row_groups else None
        name = first_column.path_in_schema if first_column is not None else metadata.schema.column(column_index).path
        top_level_field = next((field for field in parquet.schema_arrow if field.name == name), None)
        value_type = top_level_field.type if top_level_field is not None else None
        null_total = 0
        min_values: list[Any] = []
        max_values: list[Any] = []
        null_complete = row_groups > 0
        minmax_complete = row_groups > 0
        codec_set: set[str] = set()
        for group_index in range(row_groups):
            column = metadata.row_group(group_index).column(column_index)
            codec_set.add(str(column.compression).lower())
            stats = column.statistics
            if stats is None:
                null_complete = False
                minmax_complete = False
                continue
            if stats.null_count is None:
                null_complete = False
            else:
                null_total += int(stats.null_count)
            if not stats.has_min_max:
                minmax_complete = False
            else:
                min_values.append(_stat_value(stats, "min", value_type))
                max_values.append(_stat_value(stats, "max", value_type))
        codecs[name] = codec_set
        if minmax_complete and len(min_values) == row_groups and len(max_values) == row_groups:
            try:
                min_value = min(min_values)
                max_value = max(max_values)
            except TypeError:
                min_value = min_values[0] if all(value == min_values[0] for value in min_values) else None
                max_value = max_values[0] if all(value == max_values[0] for value in max_values) else None
        else:
            min_value = max_value = None
        null_value: int | None = null_total if null_complete else None
        statistics.append({
            "column": name,
            "null_count": null_value,
            "min": _json_safe(min_value),
            "max": _json_safe(max_value),
            "compression": sorted(codec_set),
        })

    all_codecs = sorted({codec for values in codecs.values() for codec in values})
    return {
        "metadata": {
            "num_rows": metadata.num_rows,
            "num_row_groups": row_groups,
            "file_size": path.stat().st_size,
            "compression": all_codecs,
            "created_by": metadata.created_by,
            "format_version": str(metadata.format_version),
            "columns": [
                {"column": name, "compression": sorted(values)}
                for name, values in codecs.items()
            ],
        },
        "statistics": statistics,
        "schema": schema_tree(parquet.schema_arrow),
        "classification": classify_schema(parquet.schema_arrow),
    }


def preview_parquet(path: Path, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """Use DuckDB read_parquet to fetch only the requested page."""
    if offset < 0 or limit < 1 or limit > MAX_PAGE_SIZE:
        raise PBError("E_BAD_REQUEST", fields=["offset" if offset < 0 else "limit"])
    with _parquet_file(path) as parquet:
        schema = parquet.schema_arrow
        total_rows = parquet.metadata.num_rows
    validate_source_schema(schema)
    if any(has_wide_decimal(field.type) for field in schema):
        return _preview_wide_decimals(path, schema, total_rows, offset, limit)
    json_columns: set[str] = set()
    projections: list[str] = []
    aliases = source_aliases(len(schema))
    for field, alias in zip(schema, aliases):
        # Read by positional alias: DuckDB matches names case-insensitively.
        source = f"src.{quote_identifier(alias)}"
        quoted = quote_identifier(field.name)
        if container_kind(field.type) is not None:
            projections.append(f"CAST(to_json({source}) AS VARCHAR) AS {quoted}")
            json_columns.add(field.name)
        elif pa.types.is_timestamp(field.type) and field.type.tz is not None:
            projections.append(f"CAST({source} AS VARCHAR) AS {quoted}")
        else:
            projections.append(f"{source} AS {quoted}")
    try:
        connection = connect_utc()
        try:
            cursor = connection.execute(
                f"SELECT {', '.join(projections)} FROM read_parquet(?) AS src{alias_list(aliases)} "
                "LIMIT ? OFFSET ?",
                [literal_path(path), limit, offset],
            )
            columns = [description[0] for description in cursor.description or ()]
            raw_rows = cursor.fetchall()
        finally:
            connection.close()
    except Exception as exc:
        raise PBError("E_PARQUET_INVALID") from exc
    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        item: dict[str, Any] = {}
        for name, value in zip(columns, raw_row):
            if name in json_columns and value is not None:
                try:
                    value = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    value = None
            item[name] = _json_safe(value)
        rows.append(item)
    return {
        "columns": columns,
        "rows": rows,
        "offset": offset,
        "page_size": limit,
        "total_rows": total_rows,
        "has_next": offset + len(rows) < total_rows,
    }


def _preview_wide_decimals(
    path: Path,
    schema: pa.Schema,
    total_rows: int,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Preview wide-decimal files through bounded, decimal256-safe Arrow batches."""
    json_columns: set[str] = set()
    projections: list[str] = []
    aliases = source_aliases(len(schema))
    for field, alias in zip(schema, aliases):
        source = f"src.{quote_identifier(alias)}"
        quoted = quote_identifier(field.name)
        if container_kind(field.type) is not None:
            projections.append(f"CAST(to_json({source}) AS VARCHAR) AS {quoted}")
            json_columns.add(field.name)
        elif pa.types.is_timestamp(field.type) and field.type.tz is not None:
            projections.append(f"CAST({source} AS VARCHAR) AS {quoted}")
        else:
            projections.append(f"{source} AS {quoted}")
    batch_name = "__parq2db_wide_decimal_inspect"
    query = f"SELECT {', '.join(projections)} FROM \"{batch_name}\" AS src{alias_list(aliases)}"
    rows: list[dict[str, Any]] = []
    remaining_skip = offset
    connection = connect_utc()
    try:
        with pq.ParquetFile(path) as parquet:
            for source_batch in parquet.iter_batches(
                batch_size=PREVIEW_ARROW_BATCH_SIZE,
                use_threads=False,
            ):
                try:
                    connection.register(
                        batch_name,
                        pa.Table.from_batches([duckdb_compatible_batch(source_batch)]),
                    )
                    cursor = connection.execute(query)
                    columns = [description[0] for description in cursor.description or ()]
                    for raw_row in cursor.fetchall():
                        if remaining_skip:
                            remaining_skip -= 1
                            continue
                        item: dict[str, Any] = {}
                        for name, value in zip(columns, raw_row):
                            if name in json_columns and value is not None:
                                try:
                                    value = json.loads(value)
                                except (TypeError, json.JSONDecodeError):
                                    value = None
                            item[name] = _json_safe(value)
                        rows.append(item)
                        if len(rows) >= limit:
                            break
                finally:
                    try:
                        connection.unregister(batch_name)
                    except Exception:
                        pass
                if len(rows) >= limit:
                    break
    except Exception as exc:
        raise PBError("E_PARQUET_INVALID") from exc
    finally:
        connection.close()
    return {
        "columns": list(schema.names),
        "rows": rows,
        "offset": offset,
        "page_size": limit,
        "total_rows": total_rows,
        "has_next": offset + len(rows) < total_rows,
    }
