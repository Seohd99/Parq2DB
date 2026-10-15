"""Parquet 메타데이터, 통계, 스키마와 페이지 미리보기를 제공한다."""

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
from app.core.duckdb_utils import connect_utc

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def _container_kind(data_type: pa.DataType) -> str | None:
    if pa.types.is_map(data_type):
        return "map"
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type) or pa.types.is_fixed_size_list(data_type):
        return "list"
    if pa.types.is_struct(data_type):
        return "struct"
    return None


def _nested_depth(data_type: pa.DataType) -> int:
    """중첩 컨테이너를 한 단계로 세어 가장 깊은 경로를 반환한다."""
    kind = _container_kind(data_type)
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
    """스키마의 flat/nested 여부와 중첩 컬럼별 최대 컨테이너 깊이를 계산한다.

    P2에서 재사용할 수 있도록 파일이나 DB 상태를 읽지 않는 순수 함수다.
    """
    nested_columns = [
        {"name": field.name, "kind": _container_kind(field.type), "max_depth": _nested_depth(field.type)}
        for field in schema
        if _container_kind(field.type) is not None
    ]
    return {"kind": "nested" if nested_columns else "flat", "nested_columns": nested_columns}


def schema_tree(schema: pa.Schema) -> dict[str, Any]:
    """Arrow 스키마를 map kind를 보존한 재귀 트리로 변환한다."""
    return {"fields": [_field_tree(field) for field in schema]}


def _field_tree(field: pa.Field) -> dict[str, Any]:
    data_type = field.type
    kind = _container_kind(data_type) or "scalar"
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
    """Arrow/DB 값을 JSON 응답에 안전한 기본 타입으로 바꾼다."""
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
    """통계의 변환 accessor가 timezone DB를 요구하면 원시 epoch를 UTC로 바꾼다."""
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
    """푸터 기반 메타데이터, 통계, 스키마 트리와 유형 판별 결과를 반환한다."""
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
    """DuckDB read_parquet로 필요한 페이지 한 장만 조회한다."""
    if offset < 0 or limit < 1 or limit > MAX_PAGE_SIZE:
        raise PBError("E_BAD_REQUEST", fields=["offset" if offset < 0 else "limit"])
    with _parquet_file(path) as parquet:
        schema = parquet.schema_arrow
        total_rows = parquet.metadata.num_rows
    json_columns: set[str] = set()
    projections: list[str] = []
    for field in schema:
        quoted = '"' + field.name.replace('"', '""') + '"'
        if _container_kind(field.type) is not None:
            projections.append(f"CAST(to_json({quoted}) AS VARCHAR) AS {quoted}")
            json_columns.add(field.name)
        elif pa.types.is_timestamp(field.type) and field.type.tz is not None:
            projections.append(f"CAST({quoted} AS VARCHAR) AS {quoted}")
        else:
            projections.append(quoted)
    try:
        connection = connect_utc()
        try:
            cursor = connection.execute(
                f"SELECT {', '.join(projections)} FROM read_parquet(?) LIMIT ? OFFSET ?",
                [str(path), limit, offset],
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
