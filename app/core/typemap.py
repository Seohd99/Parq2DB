"""Arrow 타입을 Oracle 19c와 MySQL 8 타입으로 바꾼다."""

from __future__ import annotations

import math
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from app.core.errors import PBError, warning
from app.core.dialects import SUPPORTED_DIALECTS
from app.core.model import ColumnModel, StringLength, StringLengthMeasurements, TableModel

STRING_SAMPLE_ROWS = 512
STRING_SAMPLE_BATCH_SIZE = 1024
STRING_SAMPLE_READ_BATCH_SIZE = 128
STRING_SAMPLE_MAX_ROW_GROUPS = 4
STRING_SAMPLE_MARGIN = 1.20
STRING_SCAN_DUCKDB_THREADS = min(4, max(1, os.cpu_count() or 1))
STRING_SCAN_DUCKDB_MEMORY_LIMIT = "2GB"
STRING_SCAN_ARROW_BATCH_SIZE = 1024
ORACLE_VARCHAR_CHAR_LIMIT = 1000  # 4 KiB 바이트 한도에 AL32UTF8 최악 폭을 반영
MYSQL_VARCHAR_CHAR_LIMIT = 16_383  # utf8mb4 기준 65,532 바이트
POSTGRES_VARCHAR_CHAR_LIMIT = 10_485_760
POSTGRES_MAX_COLUMNS = 1_600
MYSQL_TEXT_BYTES = 65_535
MYSQL_MEDIUMTEXT_BYTES = 16_777_215
MYSQL_ROW_SIZE_LIMIT = 65_535
MYSQL_ROW_SIZE_SAFETY_MARGIN = 5_535
MYSQL_ROW_SIZE_BUDGET = MYSQL_ROW_SIZE_LIMIT - MYSQL_ROW_SIZE_SAFETY_MARGIN
MYSQL_ROW_FIXED_OVERHEAD = 32
STRING_LENGTH_CACHE_SIZE = 16
_STRING_LENGTH_CACHE: OrderedDict[tuple, StringLengthMeasurements] = OrderedDict()
_STRING_LENGTH_CACHE_LOCK = RLock()


def container_kind(data_type: pa.DataType) -> str | None:
    """Arrow의 복합 타입 종류를 반환한다."""
    if pa.types.is_map(data_type):
        return "map"
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type) or pa.types.is_fixed_size_list(data_type):
        return "list"
    if pa.types.is_struct(data_type):
        return "struct"
    return None


def logical_type(data_type: pa.DataType, is_json: bool = False) -> str:
    """DDL 표시에 쓸 간결한 Arrow 논리 타입 이름."""
    if is_json or container_kind(data_type) is not None:
        return "json"
    if pa.types.is_timestamp(data_type):
        return "timestamp_tz" if data_type.tz else "timestamp"
    if pa.types.is_decimal(data_type):
        return "decimal"
    if pa.types.is_boolean(data_type):
        return "bool"
    if pa.types.is_integer(data_type):
        return "uint" if pa.types.is_unsigned_integer(data_type) else "int"
    if pa.types.is_floating(data_type):
        return "float"
    if pa.types.is_date(data_type):
        return "date"
    if pa.types.is_time(data_type):
        return "time"
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type) or pa.types.is_fixed_size_binary(data_type):
        return "binary"
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        return "string"
    return str(data_type)


def _type_result(sql_type: str, logical: str, warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"sql_type": sql_type, "logical_type": logical, "warnings": warnings or []}


def _estimated_length(
    measured: StringLength | None,
    strategy: str,
    dialect: str,
    *,
    margin: float = STRING_SAMPLE_MARGIN,
) -> StringLength:
    if strategy == "lob":
        return StringLength()
    if strategy == "max":
        chars = {
            "oracle": ORACLE_VARCHAR_CHAR_LIMIT,
            "mysql": MYSQL_VARCHAR_CHAR_LIMIT,
            "postgres": POSTGRES_VARCHAR_CHAR_LIMIT,
        }[dialect]
        return StringLength(chars, chars * 4)
    if measured is None:
        measured = StringLength(255, 255)
    chars = max(1, math.ceil(measured.characters * margin))
    size_bytes = max(chars, math.ceil(measured.bytes * margin))
    return StringLength(chars, size_bytes)


def map_arrow_type(
    data_type: pa.DataType,
    dialect: str,
    *,
    string_length: StringLength | None = None,
    string_strategy: str = "sample",
    length_margin: float = STRING_SAMPLE_MARGIN,
    is_json: bool = False,
    json_check: bool = False,
) -> dict[str, Any]:
    """타입 변환을 계산한다. 파일 IO가 없는 순수 함수다."""
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(f"unknown SQL dialect: {dialect}")
    if string_strategy not in {"sample", "max", "lob"}:
        raise PBError("E_BAD_REQUEST", fields=["string_strategy"])

    kind = container_kind(data_type)
    if is_json or kind is not None:
        sql_type = {"oracle": "CLOB", "mysql": "JSON", "postgres": "JSONB"}[dialect]
        return _type_result(sql_type, "json")

    if pa.types.is_boolean(data_type):
        sql_type = {"oracle": "NUMBER(1)", "mysql": "TINYINT(1)", "postgres": "BOOLEAN"}[dialect]
        return _type_result(sql_type, "bool")

    if pa.types.is_signed_integer(data_type):
        bits = data_type.bit_width
        oracle = {8: "NUMBER(3)", 16: "NUMBER(5)", 32: "NUMBER(10)", 64: "NUMBER(19)"}[bits]
        mysql = {8: "TINYINT", 16: "SMALLINT", 32: "INT", 64: "BIGINT"}[bits]
        postgres = {8: "SMALLINT", 16: "SMALLINT", 32: "INTEGER", 64: "BIGINT"}[bits]
        return _type_result({"oracle": oracle, "mysql": mysql, "postgres": postgres}[dialect], "int")

    if pa.types.is_unsigned_integer(data_type):
        bits = data_type.bit_width
        oracle = {8: "NUMBER(3)", 16: "NUMBER(5)", 32: "NUMBER(10)", 64: "NUMBER(20)"}[bits]
        mysql = {8: "TINYINT UNSIGNED", 16: "SMALLINT UNSIGNED", 32: "INT UNSIGNED", 64: "BIGINT UNSIGNED"}[bits]
        postgres = {8: "SMALLINT", 16: "INTEGER", 32: "BIGINT", 64: "NUMERIC(20)"}[bits]
        return _type_result({"oracle": oracle, "mysql": mysql, "postgres": postgres}[dialect], "uint")

    if pa.types.is_float32(data_type):
        sql_type = {"oracle": "BINARY_FLOAT", "mysql": "FLOAT", "postgres": "REAL"}[dialect]
        return _type_result(sql_type, "float")
    if pa.types.is_float64(data_type):
        sql_type = {"oracle": "BINARY_DOUBLE", "mysql": "DOUBLE", "postgres": "DOUBLE PRECISION"}[dialect]
        return _type_result(sql_type, "float")

    if pa.types.is_decimal(data_type):
        precision, scale = data_type.precision, data_type.scale
        if dialect == "oracle" and precision > 38:
            return _type_result(
                f"VARCHAR2({min(4000, max(1, precision + 2))} CHAR)", "decimal",
                [warning("W_DECIMAL_PRECISION_WIDE", dialect=dialect, precision=precision)],
            )
        if dialect == "mysql" and precision > 65:
            return _type_result(
                f"VARCHAR({min(16383, max(1, precision + 2))})", "decimal",
                [warning("W_DECIMAL_PRECISION_WIDE", dialect=dialect, precision=precision)],
            )
        if dialect == "postgres" and precision > 1000:
            return _type_result(
                "TEXT", "decimal",
                [warning("W_DECIMAL_PRECISION_WIDE", dialect=dialect, precision=precision)],
            )
        sql_type = {
            "oracle": f"NUMBER({precision},{scale})",
            "mysql": f"DECIMAL({precision},{scale})",
            "postgres": f"NUMERIC({precision},{scale})",
        }[dialect]
        return _type_result(sql_type, "decimal")

    if pa.types.is_date32(data_type) or pa.types.is_date64(data_type):
        return _type_result("DATE", "date")

    if pa.types.is_time(data_type) and dialect == "postgres":
        precision = {"s": 0, "ms": 3, "us": 6, "ns": 9}[data_type.unit]
        warnings = []
        if precision > 6:
            warnings.append(warning(
                "W_TIME_PRECISION_LOSS", precision=precision, target_precision=6, dialect=dialect,
            ))
            precision = 6
        return _type_result(f"TIME({precision})", "time", warnings)

    if pa.types.is_timestamp(data_type):
        precision = {"s": 0, "ms": 3, "us": 6, "ns": 9}[data_type.unit]
        if dialect == "oracle":
            suffix = " WITH TIME ZONE" if data_type.tz else ""
            return _type_result(f"TIMESTAMP({precision}){suffix}", "timestamp_tz" if data_type.tz else "timestamp")
        if dialect == "postgres":
            warnings = []
            if precision > 6:
                warnings.append(warning(
                    "W_TIMESTAMP_PRECISION_LOSS", precision=precision, target_precision=6, dialect=dialect,
                ))
                precision = 6
            sql_type = f"TIMESTAMPTZ({precision})" if data_type.tz else f"TIMESTAMP({precision})"
            return _type_result(
                sql_type,
                "timestamp_tz" if data_type.tz else "timestamp",
                warnings,
            )
        warnings = []
        if data_type.tz:
            warnings.append(warning("W_TIMESTAMP_TZ_UTC", source_timezone=data_type.tz))
        if precision > 6:
            warnings.append(warning(
                "W_TIMESTAMP_PRECISION_LOSS", precision=precision, target_precision=6, dialect=dialect,
            ))
            precision = 6
        return _type_result(f"DATETIME({precision})", "timestamp_tz" if data_type.tz else "timestamp", warnings)

    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        if dialect == "postgres":
            if string_strategy in {"max", "lob"}:
                return _type_result("TEXT", "string")
            size = _estimated_length(string_length, string_strategy, dialect, margin=length_margin)
            if size.characters > POSTGRES_VARCHAR_CHAR_LIMIT:
                return _type_result("TEXT", "string")
            return _type_result(f"VARCHAR({size.characters})", "string")
        if string_strategy == "lob":
            return _type_result("CLOB" if dialect == "oracle" else "LONGTEXT", "string")
        if dialect == "oracle":
            if string_strategy == "max":
                # Keep max at least as wide as the corresponding measured sample.
                # STANDARD + AL32UTF8 has a conservative 1,000-character inline cap.
                if string_length is not None:
                    estimate = _estimated_length(
                        string_length, "sample", dialect, margin=length_margin,
                    )
                    if estimate.characters > ORACLE_VARCHAR_CHAR_LIMIT or estimate.bytes > 4000:
                        return _type_result("CLOB", "string")
                return _type_result(f"VARCHAR2({ORACLE_VARCHAR_CHAR_LIMIT} CHAR)", "string")
            size = _estimated_length(string_length, string_strategy, dialect, margin=length_margin)
            if size.bytes > 4000 or size.characters > 4000:
                return _type_result("CLOB", "string")
            return _type_result(f"VARCHAR2({min(4000, size.characters)} CHAR)", "string")
        if string_strategy == "max":
            if string_length is None:
                return _type_result(f"VARCHAR({MYSQL_VARCHAR_CHAR_LIMIT})", "string")
            estimate = _estimated_length(
                string_length, "sample", dialect, margin=length_margin,
            )
            if estimate.characters <= MYSQL_VARCHAR_CHAR_LIMIT and estimate.bytes <= MYSQL_VARCHAR_CHAR_LIMIT * 4:
                return _type_result(f"VARCHAR({MYSQL_VARCHAR_CHAR_LIMIT})", "string")
            if estimate.bytes <= MYSQL_TEXT_BYTES:
                return _type_result("TEXT", "string")
            if estimate.bytes <= MYSQL_MEDIUMTEXT_BYTES:
                return _type_result("MEDIUMTEXT", "string")
            return _type_result("LONGTEXT", "string")
        size = _estimated_length(string_length, string_strategy, dialect, margin=length_margin)
        if size.characters <= MYSQL_VARCHAR_CHAR_LIMIT and size.characters * 4 <= 65_532:
            return _type_result(f"VARCHAR({size.characters})", "string")
        if size.bytes <= MYSQL_TEXT_BYTES:
            return _type_result("TEXT", "string")
        if size.bytes <= MYSQL_MEDIUMTEXT_BYTES:
            return _type_result("MEDIUMTEXT", "string")
        return _type_result("LONGTEXT", "string")

    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type) or pa.types.is_fixed_size_binary(data_type):
        if dialect == "postgres":
            return _type_result("BYTEA", "binary")
        if string_strategy == "lob":
            return _type_result("BLOB" if dialect == "oracle" else "LONGBLOB", "binary")
        fixed_size = data_type.byte_width if pa.types.is_fixed_size_binary(data_type) else None
        if fixed_size:
            length = fixed_size
        elif string_strategy == "max":
            length = 2000 if dialect == "oracle" else 65_535
        else:
            length = string_length.bytes if string_length else 255
        if string_strategy == "sample":
            length = max(1, math.ceil(length * STRING_SAMPLE_MARGIN))
        if dialect == "oracle":
            return _type_result(f"RAW({length})" if length <= 2000 else "BLOB", "binary")
        return _type_result(f"VARBINARY({length})" if length <= 65_535 else "LONGBLOB", "binary")

    fallback = {"oracle": "CLOB", "mysql": "LONGTEXT", "postgres": "TEXT"}[dialect]
    return _type_result(
        fallback,
        "unsupported",
        [warning("W_UNSUPPORTED_TYPE", arrow_type=str(data_type), dialect=dialect)],
    )


def map_type_pair(
    data_type: pa.DataType,
    *,
    string_length: StringLength | None = None,
    string_strategy: str = "sample",
    length_margin: float = STRING_SAMPLE_MARGIN,
    is_json: bool = False,
    json_check: bool = False,
) -> dict[str, Any]:
    """Arrow 타입을 지원하는 각 DB로 매핑하고 경고를 합친다."""
    oracle = map_arrow_type(
        data_type, "oracle", string_length=string_length, string_strategy=string_strategy,
        length_margin=length_margin,
        is_json=is_json, json_check=json_check,
    )
    mysql = map_arrow_type(
        data_type, "mysql", string_length=string_length, string_strategy=string_strategy,
        length_margin=length_margin,
        is_json=is_json, json_check=json_check,
    )
    postgres = map_arrow_type(
        data_type, "postgres", string_length=string_length, string_strategy=string_strategy,
        length_margin=length_margin,
        is_json=is_json, json_check=json_check,
    )
    warnings = oracle["warnings"] + mysql["warnings"] + postgres["warnings"]
    return {
        "sql_type_oracle": oracle["sql_type"],
        "sql_type_mysql": mysql["sql_type"],
        "sql_type_postgres": postgres["sql_type"],
        "logical_type": oracle["logical_type"],
        "warnings": warnings,
    }


def sample_row_positions(total_rows: int, sample_size: int = STRING_SAMPLE_ROWS) -> tuple[int, ...]:
    """첫 구간에 치우치지 않도록 파일 전체 구간에서 행 위치를 고르게 고른다."""
    if total_rows <= 0 or sample_size <= 0:
        return ()
    count = min(total_rows, sample_size)
    if count == 1:
        return (0,)
    return tuple(sorted({round(index * (total_rows - 1) / (count - 1)) for index in range(count)}))


def _contains_variable_value(data_type: pa.DataType) -> bool:
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        return True
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
        return True
    kind = container_kind(data_type)
    if kind == "struct":
        return any(_contains_variable_value(field.type) for field in data_type)
    if kind == "list":
        return _contains_variable_value(data_type.value_type)
    if kind == "map":
        return _contains_variable_value(data_type.key_type) or _contains_variable_value(data_type.item_type)
    return False


def _evenly_spaced_indices(total: int, count: int) -> tuple[int, ...]:
    if total <= 0 or count <= 0:
        return ()
    if count == 1:
        return ((total - 1) // 2,)
    return tuple(sorted({round(i * (total - 1) / (count - 1)) for i in range(count)}))


def _allocate_group_rows(capacities: list[int], budget: int) -> list[int]:
    """선택한 row group마다 행 예산을 나누고 작은 그룹의 남은 분량을 재분배한다."""
    target = min(max(0, budget), sum(capacities))
    allocation = [0] * len(capacities)
    active = [index for index, capacity in enumerate(capacities) if capacity > 0]
    remaining = target
    while active and remaining:
        share, extra = divmod(remaining, len(active))
        saturated = [
            index for ordinal, index in enumerate(active)
            if capacities[index] <= share + (ordinal < extra)
        ]
        if not saturated:
            for ordinal, index in enumerate(active):
                allocation[index] = share + (ordinal < extra)
            break
        saturated_set = set(saturated)
        for index in saturated:
            allocation[index] = capacities[index]
            remaining -= capacities[index]
        active = [index for index in active if index not in saturated_set]
    return allocation


def _group_windows(row_count: int, sample_count: int) -> tuple[tuple[int, int], ...]:
    """row group 안에 고르게 분산된 연속 배치 구간을 만든다."""
    if row_count <= 0 or sample_count <= 0:
        return ()
    sample_count = min(row_count, sample_count)
    window_count = math.ceil(sample_count / STRING_SAMPLE_BATCH_SIZE)
    windows: list[tuple[int, int]] = []
    partitions = [
        (round(index * row_count / window_count), round((index + 1) * row_count / window_count))
        for index in range(window_count)
    ]
    lengths = _allocate_group_rows([end - start for start, end in partitions], sample_count)
    for (partition_start, partition_end), length in zip(partitions, lengths):
        if length <= 0:
            continue
        # A single sampled window per row group can start at its first rows and
        # stop decoding after one batch. Centering that window made a small
        # sample read roughly half of every large row group before measuring it.
        start = (
            partition_start
            if window_count == 1
            else partition_start + (partition_end - partition_start - length) // 2
        )
        windows.append((start, start + length))
    return tuple(windows)


def _sampling_windows(row_group_rows: list[int], sample_rows: int) -> dict[int, tuple[tuple[int, int], ...]]:
    nonempty = [index for index, count in enumerate(row_group_rows) if count > 0]
    chosen = _evenly_spaced_indices(
        len(nonempty),
        min(len(nonempty), STRING_SAMPLE_MAX_ROW_GROUPS, sample_rows),
    )
    group_indices = [nonempty[index] for index in chosen]
    allocations = _allocate_group_rows([row_group_rows[index] for index in group_indices], sample_rows)
    return {
        group_index: windows
        for group_index, count in zip(group_indices, allocations)
        if (windows := _group_windows(row_group_rows[group_index], count))
    }


def _maximum(array: pa.Array) -> int:
    if len(array) == 0:
        return 0
    value = pc.max(array)
    return int(value.as_py()) if value.is_valid else 0


def _merge_lengths(path: str, characters: int, byte_count: int, result: dict[str, StringLength]) -> None:
    previous = result.get(path, StringLength())
    result[path] = StringLength(max(previous.characters, characters), max(previous.bytes, byte_count))


def _record_arrow_lengths(
    array: pa.Array,
    data_type: pa.DataType,
    path: str,
    result: dict[str, StringLength],
) -> None:
    """Arrow compute로 배열 전체의 문자열·바이너리 최대 길이를 집계한다."""
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        characters = _maximum(pc.utf8_length(array))
        byte_count = _maximum(pc.binary_length(array))
        _merge_lengths(path, characters, byte_count, result)
        return
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type) or pa.types.is_fixed_size_binary(data_type):
        _merge_lengths(path, 0, _maximum(pc.binary_length(array)), result)
        return

    kind = container_kind(data_type)
    if kind == "struct":
        parent_valid = array.is_valid()
        for field in data_type:
            child = array.field(field.name)
            if array.null_count:
                child = pc.filter(child, parent_valid)
            _record_arrow_lengths(child, field.type, f"{path}.{field.name}", result)
    elif kind == "list":
        flattened = pc.list_flatten(array)
        _record_arrow_lengths(flattened, data_type.value_type, f"{path}[]", result)
    elif kind == "map":
        valid_maps = pc.filter(array, array.is_valid()) if array.null_count else array
        _record_arrow_lengths(valid_maps.keys, data_type.key_type, f"{path}.key", result)
        _record_arrow_lengths(valid_maps.items, data_type.item_type, f"{path}.value", result)


def measure_string_lengths(
    path: Path,
    schema: pa.Schema,
    *,
    strategy: str = "sample",
    length_scan: str = "full",
    sample_rows: int = STRING_SAMPLE_ROWS,
    progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> StringLengthMeasurements:
    """Measure only variable-length columns, reusing results while a file is unchanged."""
    if strategy not in {"sample", "max", "lob"}:
        raise PBError("E_BAD_REQUEST", fields=["string_strategy"])
    if length_scan not in {"full", "sample"}:
        raise PBError("E_BAD_REQUEST", fields=["length_scan"])
    if strategy == "lob":
        return StringLengthMeasurements()

    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except (OSError, RuntimeError):
        return _measure_string_lengths_uncached(
            path,
            schema,
            strategy=strategy,
            length_scan=length_scan,
            sample_rows=sample_rows,
            progress=progress,
            cancel_event=cancel_event,
        )
    key = _string_length_cache_key(resolved, schema, length_scan, sample_rows, stat=stat)
    with _STRING_LENGTH_CACHE_LOCK:
        cached = _STRING_LENGTH_CACHE.get(key)
        if cached is not None:
            _STRING_LENGTH_CACHE.move_to_end(key)
            result = StringLengthMeasurements(
                dict(cached),
                sampled_rows=cached.sampled_rows,
                total_rows=cached.total_rows,
                margin=cached.margin,
            )
            if progress is not None:
                progress(result.total_rows, result.total_rows)
            return result

    measured = _measure_string_lengths_uncached(
        resolved,
        schema,
        strategy=strategy,
        length_scan=length_scan,
        sample_rows=sample_rows,
        progress=progress,
        cancel_event=cancel_event,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise PBError("E_STRING_SCAN_CANCELLED")
    try:
        after = resolved.stat()
    except OSError:
        return measured
    if (stat.st_size, stat.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
        with _STRING_LENGTH_CACHE_LOCK:
            _STRING_LENGTH_CACHE[key] = StringLengthMeasurements(
                dict(measured),
                sampled_rows=measured.sampled_rows,
                total_rows=measured.total_rows,
                margin=measured.margin,
            )
            _STRING_LENGTH_CACHE.move_to_end(key)
            while len(_STRING_LENGTH_CACHE) > STRING_LENGTH_CACHE_SIZE:
                _STRING_LENGTH_CACHE.popitem(last=False)
    return measured


def _measure_string_lengths_uncached(
    path: Path,
    schema: pa.Schema,
    *,
    strategy: str = "sample",
    length_scan: str = "full",
    sample_rows: int = STRING_SAMPLE_ROWS,
    progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> StringLengthMeasurements:
    """필요한 문자열 리프의 길이를 읽어 측정 범위와 함께 반환한다."""
    if strategy not in {"sample", "max", "lob"}:
        raise PBError("E_BAD_REQUEST", fields=["string_strategy"])
    if length_scan not in {"full", "sample"}:
        raise PBError("E_BAD_REQUEST", fields=["length_scan"])
    if strategy == "lob":
        return StringLengthMeasurements()

    wanted_roots = [field.name for field in schema if _contains_variable_value(field.type)]
    if not wanted_roots:
        return StringLengthMeasurements()
    if length_scan == "full":
        return _measure_full_lengths_duckdb(
            path,
            schema,
            wanted_roots,
            progress=progress,
            cancel_event=cancel_event,
        )

    return _measure_sample_lengths_pyarrow(
        path,
        schema,
        wanted_roots,
        sample_rows=sample_rows,
        progress=progress,
        cancel_event=cancel_event,
    )


def measure_string_lengths_pyarrow_reference(
    path: Path,
    schema: pa.Schema,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> StringLengthMeasurements:
    """Full PyArrow scan retained as an exact reference implementation."""
    wanted_roots = [field.name for field in schema if _contains_variable_value(field.type)]
    if not wanted_roots:
        return StringLengthMeasurements()
    with pq.ParquetFile(path) as parquet:
        total_rows = parquet.metadata.num_rows
        field_types = {field.name: field.type for field in schema}
        measured: dict[str, StringLength] = {}
        scanned_rows = 0
        for batch in parquet.iter_batches(batch_size=STRING_SAMPLE_BATCH_SIZE, columns=wanted_roots):
            _raise_if_scan_cancelled(cancel_event)
            for column_index, name in enumerate(batch.schema.names):
                _record_arrow_lengths(batch.column(column_index), field_types[name], name, measured)
            scanned_rows += batch.num_rows
            if progress is not None:
                progress(scanned_rows, total_rows)
    return StringLengthMeasurements(measured, sampled_rows=total_rows, total_rows=total_rows, margin=1.0)


def _measure_sample_lengths_pyarrow(
    path: Path,
    schema: pa.Schema,
    wanted_roots: list[str],
    *,
    sample_rows: int,
    progress: Callable[[int, int], None] | None,
    cancel_event: Event | None,
) -> StringLengthMeasurements:
    lengths: dict[str, StringLength] = {}
    with pq.ParquetFile(path) as parquet:
        metadata = parquet.metadata
        total_rows = metadata.num_rows
        field_types = {field.name: field.type for field in schema}
        sample_budget = min(total_rows, max(0, sample_rows))
        row_group_rows = [metadata.row_group(index).num_rows for index in range(metadata.num_row_groups)]
        by_group = _sampling_windows(row_group_rows, sample_budget)
        sampled_rows = sum(end - start for windows in by_group.values() for start, end in windows)
        scanned_rows = 0
        for group_index, windows in by_group.items():
            _raise_if_scan_cancelled(cancel_event)
            batch_offset = 0
            next_window = 0
            for batch in parquet.iter_batches(
                batch_size=STRING_SAMPLE_READ_BATCH_SIZE,
                columns=wanted_roots,
                row_groups=[group_index],
            ):
                _raise_if_scan_cancelled(cancel_event)
                batch_end = batch_offset + batch.num_rows
                while next_window < len(windows):
                    window_start, window_end = windows[next_window]
                    if window_start >= batch_end:
                        break
                    if window_end <= batch_offset:
                        next_window += 1
                        continue
                    overlap_start = max(window_start, batch_offset)
                    overlap_end = min(window_end, batch_end)
                    if overlap_start < overlap_end:
                        fragment = batch.slice(overlap_start - batch_offset, overlap_end - overlap_start)
                        for column_index, name in enumerate(fragment.schema.names):
                            _record_arrow_lengths(fragment.column(column_index), field_types[name], name, lengths)
                        scanned_rows += fragment.num_rows
                        if progress is not None:
                            progress(scanned_rows, sampled_rows)
                    if window_end <= batch_end:
                        next_window += 1
                    else:
                        break
                batch_offset = batch_end
                if next_window == len(windows):
                    break
        margin = STRING_SAMPLE_MARGIN
    return StringLengthMeasurements(
        lengths,
        sampled_rows=sampled_rows,
        total_rows=total_rows,
        margin=margin,
    )


def _string_length_cache_key(
    path: Path,
    schema: pa.Schema,
    length_scan: str,
    sample_rows: int,
    *,
    stat: os.stat_result | None = None,
) -> tuple:
    """Cache exact values independently of the DDL strategy that consumes them."""
    current_stat = stat or path.stat()
    return (
        str(path),
        current_stat.st_size,
        current_stat.st_mtime_ns,
        str(schema),
        length_scan,
        sample_rows,
    )


def _raise_if_scan_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PBError("E_STRING_SCAN_CANCELLED")


def _quote_duckdb_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _measure_full_lengths_duckdb(
    path: Path,
    schema: pa.Schema,
    wanted_roots: list[str],
    *,
    progress: Callable[[int, int], None] | None,
    cancel_event: Event | None,
) -> StringLengthMeasurements:
    """Use parallel DuckDB aggregates for scalar leaves and parallel Arrow for nested leaves."""
    import duckdb
    from concurrent.futures import ThreadPoolExecutor

    from app.core.duckdb_utils import connect_utc

    metadata = pq.read_metadata(path)
    total_rows = metadata.num_rows
    fields = {field.name: field for field in schema}
    scalar_roots = [
        name for name in wanted_roots
        if container_kind(fields[name].type) is None
    ]
    nested_roots = [name for name in wanted_roots if name not in scalar_roots]
    result: dict[str, StringLength] = {}

    if scalar_roots:
        expressions: list[str] = []
        output_paths: list[tuple[str, str]] = []
        for name in scalar_roots:
            quoted = _quote_duckdb_identifier(name)
            data_type = fields[name].type
            if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
                expressions.extend((
                    f"coalesce(max(length({quoted})), 0)",
                    f"coalesce(max(strlen({quoted})), 0)",
                ))
                output_paths.extend(((name, "characters"), (name, "bytes")))
            else:
                expressions.extend(("0", f"coalesce(max(octet_length({quoted})), 0)"))
                output_paths.extend(((name, "characters"), (name, "bytes")))
        connection = connect_utc()
        try:
            connection.execute(f"SET threads={STRING_SCAN_DUCKDB_THREADS}")
            connection.execute(f"SET memory_limit='{STRING_SCAN_DUCKDB_MEMORY_LIMIT}'")
            connection.execute("SET preserve_insertion_order=false")
            _raise_if_scan_cancelled(cancel_event)
            row = connection.execute(
                f"SELECT {', '.join(expressions)} FROM read_parquet(?)",
                [str(path)],
            ).fetchone()
            _raise_if_scan_cancelled(cancel_event)
            if row is not None:
                for (name, metric), value in zip(output_paths, row):
                    previous = result.get(name, StringLength())
                    number = int(value or 0)
                    if metric == "characters":
                        result[name] = StringLength(number, previous.bytes)
                    else:
                        result[name] = StringLength(previous.characters, number)
        except duckdb.Error:
            if cancel_event is not None and cancel_event.is_set():
                raise PBError("E_STRING_SCAN_CANCELLED") from None
            # A DuckDB version without octet_length(BLOB) can still use Arrow for these roots.
            nested_roots.extend(scalar_roots)
            result.clear()
        finally:
            connection.close()

    if nested_roots:
        row_group_indices = list(range(metadata.num_row_groups))
        worker_count = min(STRING_SCAN_DUCKDB_THREADS, max(1, len(row_group_indices)))
        groups_by_worker = [row_group_indices[index::worker_count] for index in range(worker_count)]
        field_types = {field.name: field.type for field in schema}
        progress_lock = threading.Lock()
        progress_rows = 0

        def scan_groups(group_indices: list[int]) -> tuple[dict[str, StringLength], int]:
            nonlocal progress_rows
            local_lengths: dict[str, StringLength] = {}
            local_rows = 0
            with pq.ParquetFile(path) as parquet:
                for group_index in group_indices:
                    _raise_if_scan_cancelled(cancel_event)
                    for batch in parquet.iter_batches(
                        batch_size=STRING_SCAN_ARROW_BATCH_SIZE,
                        columns=nested_roots,
                        row_groups=[group_index],
                    ):
                        _raise_if_scan_cancelled(cancel_event)
                        for column_index, name in enumerate(batch.schema.names):
                            _record_arrow_lengths(
                                batch.column(column_index),
                                field_types[name],
                                name,
                                local_lengths,
                            )
                        local_rows += batch.num_rows
                        if progress is not None:
                            with progress_lock:
                                progress_rows += batch.num_rows
                                progress(progress_rows, total_rows)
            return local_lengths, local_rows

        if row_group_indices:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="parqbridge-lengths") as executor:
                parts = list(executor.map(scan_groups, groups_by_worker))
            for part, _ in parts:
                for name, item in part.items():
                    previous = result.get(name, StringLength())
                    result[name] = StringLength(
                        max(previous.characters, item.characters),
                        max(previous.bytes, item.bytes),
                    )
    if progress is not None and (not nested_roots or total_rows == 0):
        progress(total_rows, total_rows)
    return StringLengthMeasurements(result, sampled_rows=total_rows, total_rows=total_rows, margin=1.0)


def _decimal_storage_bytes(precision: int, scale: int) -> int:
    remainder_bytes = (0, 1, 1, 2, 2, 3, 3, 4, 4, 4)

    def digits_bytes(digits: int) -> int:
        full, remainder = divmod(digits, 9)
        return full * 4 + remainder_bytes[remainder]

    return digits_bytes(precision - scale) + digits_bytes(scale)


def _mysql_column_row_bytes(column: ColumnModel) -> int:
    """MySQL 컬럼의 최대 바이트와 길이 접두, 외부 저장 포인터를 추산한다."""
    sql_type = (column.sql_type_mysql or "").strip().upper()
    match = re.fullmatch(r"(VARCHAR|VARBINARY|CHAR|BINARY)\((\d+)\)", sql_type)
    if match:
        kind, raw_length = match.groups()
        length = int(raw_length)
        max_bytes = length * (4 if kind in {"VARCHAR", "CHAR"} else 1)
        return max_bytes + ((1 if max_bytes <= 255 else 2) if kind.startswith("VAR") else 0)
    match = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", sql_type)
    if match:
        return _decimal_storage_bytes(int(match.group(1)), int(match.group(2)))
    match = re.fullmatch(r"DATETIME\((\d+)\)", sql_type)
    if match:
        return 5 + math.ceil(int(match.group(1)) / 2)
    fixed = {
        "TINYINT": 1, "TINYINT(1)": 1, "TINYINT UNSIGNED": 1,
        "SMALLINT": 2, "SMALLINT UNSIGNED": 2,
        "MEDIUMINT": 3, "MEDIUMINT UNSIGNED": 3,
        "INT": 4, "INT UNSIGNED": 4, "INTEGER": 4,
        "BIGINT": 8, "BIGINT UNSIGNED": 8,
        "FLOAT": 4, "DOUBLE": 8, "DATE": 3, "TIME": 3, "DATETIME": 5,
        "YEAR": 1,
    }
    if sql_type in fixed:
        return fixed[sql_type]
    if sql_type.startswith(("TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", "BLOB", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB", "JSON")):
        return 12
    return 0


def mysql_row_size_bytes(columns: tuple[ColumnModel, ...] | list[ColumnModel]) -> int:
    """컬럼 메타데이터로 테이블 행 크기를 보수적으로 추산한다."""
    nullable_columns = sum(column.nullable for column in columns)
    null_bitmap = math.ceil(nullable_columns / 8)
    return MYSQL_ROW_FIXED_OVERHEAD + null_bitmap + sum(_mysql_column_row_bytes(column) for column in columns)


def _mysql_demoted_type(sql_type: str) -> str | None:
    if re.fullmatch(r"VARCHAR\(\d+\)", sql_type, flags=re.IGNORECASE):
        return "TEXT"
    if re.fullmatch(r"VARBINARY\(\d+\)", sql_type, flags=re.IGNORECASE):
        return "BLOB"
    return None


def enforce_mysql_row_size(table: TableModel) -> TableModel:
    """MySQL 행 예산이 넘으면 가장 큰 가변 길이 열부터 LOB로 낮춘다."""
    from dataclasses import replace

    columns = list(table.columns)
    estimate = mysql_row_size_bytes(columns)
    if estimate <= MYSQL_ROW_SIZE_BUDGET:
        return table
    candidates = sorted(
        (
            (index, _mysql_column_row_bytes(column), _mysql_demoted_type(column.sql_type_mysql or ""))
            for index, column in enumerate(columns)
        ),
        key=lambda item: (-item[1], item[0]),
    )
    demoted: list[str] = []
    for index, _size, target_type in candidates:
        if estimate <= MYSQL_ROW_SIZE_BUDGET:
            break
        if target_type is None:
            continue
        columns[index] = replace(columns[index], sql_type_mysql=target_type)
        estimate = mysql_row_size_bytes(columns)
        demoted.append(columns[index].name)
    if not demoted:
        return table
    message = warning(
        "W_MYSQL_ROW_SIZE_DEMOTED",
        table=table.name,
        columns=demoted,
        estimated_bytes=estimate,
        budget_bytes=MYSQL_ROW_SIZE_BUDGET,
    )
    warnings = tuple(item for item in table.warnings if item.get("code") != message["code"])
    return replace(table, columns=tuple(columns), warnings=warnings + (message,))
