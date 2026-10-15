"""Build Parquet flattening plans and DuckDB query SQL."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.duckdb_utils import alias_list, connect_utc, literal_path, source_aliases, validate_source_schema
from app.core.dialects import SUPPORTED_DIALECTS
from app.core.errors import PBError, warning
from app.core.inspect import _json_safe
from app.core.model import ColumnModel, JoinSpec, SelectItem, SelectPlan, StringLength, TableModel
from app.core.reserved import ReservedWordProvider, reserved_words
from app.core.typemap import (
    STRING_SAMPLE_BATCH_SIZE,
    STRING_SAMPLE_MARGIN,
    container_kind,
    enforce_mysql_row_size,
    map_type_pair,
    POSTGRES_MAX_COLUMNS,
    sample_row_positions,
)

MAX_IDENTIFIER_BYTES = 128
MAX_IDENTIFIER_CHARS = 64
POSTGRES_IDENTIFIER_BYTES = 63
EXPLODE_WARNING_MULTIPLIER = 10
PARENT_KEY_SAMPLE_ROWS = 256


def _max_identifier_length(dialect: str) -> int:
    return {
        "oracle": MAX_IDENTIFIER_BYTES,
        "mysql": MAX_IDENTIFIER_CHARS,
        "postgres": POSTGRES_IDENTIFIER_BYTES,
    }[dialect]


def validate_table_name(name: str, dialect: str, policy: str = "upper_unquoted") -> str:
    """Validate allowed table-name characters and database-specific length limits."""
    if dialect not in SUPPORTED_DIALECTS:
        raise PBError("E_DIALECT_INVALID", dialect=dialect)
    max_length = _max_identifier_length(dialect)
    size = len(name.encode("utf-8")) if dialect in {"oracle", "postgres"} else len(name)
    # Oracle nonquoted identifiers must begin with a letter.
    first = r"[A-Za-z]" if dialect == "oracle" and policy != "quoted" else r"[A-Za-z_]"
    if (
        not name
        or size > max_length
        or not re.fullmatch(first + r"[A-Za-z0-9_]*", name, flags=re.ASCII)
        or name.upper() in reserved_words(dialect)
    ):
        raise PBError("E_TABLE_NAME_INVALID", dialect=dialect, max_length=max_length)
    return name


def default_table_name(filename: str, dialect: str = "oracle") -> str:
    """Create a safe default table name from a file name."""
    stem = Path(filename).stem
    result = _normalize_upper(stem, dialect, oracle_prefix="T_")
    result = result[:_max_identifier_length(dialect)] or "PARQUET_TABLE"
    try:
        return validate_table_name(result, dialect)
    except PBError:
        return "PARQUET_TABLE"


def _normalize_upper(value: str, dialect: str | None = None, *, oracle_prefix: str = "C_") -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", value, flags=re.ASCII).upper()
    if dialect == "oracle":
        # Oracle nonquoted identifiers must begin with a letter, so drop leading
        # underscores and prefix anything that still does not start with one.
        value = value.lstrip("_")
        if not value or not value[0].isalpha():
            value = oracle_prefix + value
        return value
    if value and value[0].isdigit():
        value = "_" + value
    return value or "_"


class _NameAllocator:
    """Apply naming policy, length, reserved-word, and collision rules to columns and child tables."""

    def __init__(
        self,
        dialect: str,
        policy: str,
        *,
        table: bool = False,
        reserved_word_provider: ReservedWordProvider | None = None,
    ) -> None:
        if dialect not in SUPPORTED_DIALECTS:
            raise PBError("E_DIALECT_INVALID", dialect=dialect)
        if policy not in {"upper_unquoted", "quoted"}:
            raise PBError("E_BAD_REQUEST", fields=["column_policy"])
        self.dialect = dialect
        self.policy = policy
        self.max_length = _max_identifier_length(dialect)
        self.used: set[str] = set()
        self.reserved = reserved_words(dialect, reserved_word_provider)
        self.table = table

    def _size(self, value: str) -> int:
        return len(value.encode("utf-8")) if self.dialect in {"oracle", "postgres"} else len(value)

    def allocate(self, original: str, warnings: list[dict[str, Any]] | None = None) -> str:
        warnings = warnings if warnings is not None else []
        if self.policy == "quoted":
            candidate = original
            if not candidate or self._size(candidate) > self.max_length:
                raise PBError("E_IDENTIFIER_INVALID", name=original)
            if candidate in self.used:
                raise PBError("E_IDENTIFIER_INVALID", name=original)
        else:
            candidate = _normalize_upper(
                original, self.dialect, oracle_prefix="T_" if self.table else "C_",
            )
            candidate = self._fit(candidate, "")
            if candidate.upper() in self.reserved:
                renamed = self._unique(self._fit(candidate, "_2"))
                warnings.append(warning(
                    "W_RESERVED_IDENTIFIER", original=original, name=renamed, dialect=self.dialect,
                ))
                candidate = renamed
            else:
                unique = self._unique(candidate)
                if unique != candidate:
                    warnings.append(warning(
                        "W_IDENTIFIER_COLLISION", original=original, name=unique,
                        dialect=self.dialect,
                    ))
                candidate = unique
        self.used.add(candidate)
        return candidate

    def allocate_generated(self, base: str, warnings: list[dict[str, Any]] | None = None) -> str:
        """Allocate an app-generated name, renaming it on collision even under the quoted policy."""
        if self.policy != "quoted":
            return self.allocate(base, warnings)
        warnings = warnings if warnings is not None else []
        unique = self._unique(base)
        if unique != base:
            warnings.append(warning(
                "W_IDENTIFIER_COLLISION", original=base, name=unique, dialect=self.dialect,
            ))
        self.used.add(unique)
        return unique

    def reserve(self, name: str) -> None:
        """Mark a name that is already taken in this namespace, such as an inherited key."""
        self.used.add(name if self.policy == "quoted" else name.upper())

    def _fit(self, name: str, suffix: str) -> str:
        room = self.max_length - len(suffix)
        base = name[:max(1, room)]
        return base + suffix

    def _unique(self, base: str) -> str:
        if base not in self.used:
            return base
        index = 2
        while True:
            suffix = f"_{index}"
            candidate = self._fit(base, suffix)
            if candidate not in self.used:
                return candidate
            index += 1


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_field(parent: str, name: str) -> str:
    return f"{parent}.{_quote(name)}"


def _json_expression(expression: str) -> str:
    return f"CAST(to_json({expression}) AS VARCHAR)"


def _column(
    *,
    original_path: str,
    original_name: str,
    name: str,
    data_type: pa.DataType,
    nullable: bool = True,
    source_path: str | None = None,
    lengths: Mapping[str, StringLength],
    string_strategy: str,
    is_json: bool = False,
    generated: bool = False,
    json_check: bool = False,
    extra_warnings: list[dict[str, Any]] | None = None,
) -> ColumnModel:
    mapping = map_type_pair(
        data_type,
        string_length=lengths.get(original_path),
        string_strategy=string_strategy,
        length_margin=getattr(lengths, "margin", STRING_SAMPLE_MARGIN),
        is_json=is_json,
        json_check=json_check,
    )
    if extra_warnings is not None:
        extra_warnings.extend(mapping["warnings"])
    precision = data_type.precision if pa.types.is_decimal(data_type) else None
    scale = data_type.scale if pa.types.is_decimal(data_type) else None
    measured = lengths.get(original_path)
    return ColumnModel(
        original_name=original_name,
        original_path=original_path,
        name=name,
        arrow_type=data_type,
        logical_type=mapping["logical_type"],
        nullable=nullable,
        length=measured.characters if measured else None,
        byte_length=measured.bytes if measured else None,
        precision=precision,
        scale=scale,
        is_json=is_json,
        generated=generated,
        check_json=json_check and is_json,
        sql_type_oracle=mapping["sql_type_oracle"],
        sql_type_mysql=mapping["sql_type_mysql"],
        sql_type_postgres=mapping["sql_type_postgres"],
        source_path=source_path,
    )


def _count_formula(expression: str, preserve_empty: bool = False) -> str:
    if preserve_empty:
        return f"CASE WHEN {expression} IS NULL OR len({expression}) = 0 THEN 1 ELSE len({expression}) END"
    return f"COALESCE(len({expression}), 0)"


def _source_parts(plan: SelectPlan, source: str = "read_parquet(?)") -> tuple[str, str]:
    """Return the WITH clause and FROM clause that expose the source as ``src``."""
    aliases = alias_list(plan.source_columns)
    if not plan.needs_row_id:
        return "", f"FROM {source} AS src{aliases}"
    if plan.row_id_strategy == "file_row_number" and source == "read_parquet(?)":
        row_id = "file_row_number"
        source = "read_parquet(?, file_row_number=true)"
    else:
        row_id = "row_number() OVER () - 1"
    cte = (
        f"WITH src AS (SELECT *, {row_id} AS {_quote(plan.internal_row_id)} "
        f"FROM {source} AS raw{aliases}) "
    )
    return cte, "FROM src"


def _select_sql_from_parts(
    *,
    plan: SelectPlan,
    dialect: str,
    limit: int | None = None,
    source: str = "read_parquet(?)",
) -> str:
    selections = plan.selections
    joins = plan.joins
    order_by = plan.order_by
    cte, from_clause = _source_parts(plan, source)

    rendered: list[str] = []
    for item in selections:
        expression = _json_expression(item.expression) if item.json_value else item.expression
        if item.timestamp_text and not item.json_value:
            if dialect == "mysql" and item.utc_timestamp:
                expression = f"CAST(timezone('UTC', {expression}) AS VARCHAR)"
            else:
                expression = f"CAST({expression} AS VARCHAR)"
        rendered.append(f"{expression} AS {_quote(item.name)}")
    for join in joins:
        expression = f"map_entries({join.expression})" if join.map_entries else join.expression
        prefix = "LEFT JOIN" if join.join_type == "left" else "CROSS JOIN"
        join_sql = f" {prefix} UNNEST({expression}) WITH ORDINALITY AS {_quote(join.alias)}({_quote('value')}, {_quote('ordinality')})"
        if join.join_type == "left":
            join_sql += " ON TRUE"
        from_clause += join_sql
    sql = cte + f"SELECT {', '.join(rendered)} {from_clause}"
    ordering = tuple(order_by)
    if ordering:
        sql += " ORDER BY " + ", ".join(ordering)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
    return sql


def build_select_sql(
    table: TableModel,
    *,
    dialect: str = "oracle",
    limit: int | None = None,
    offset: int = 0,
    source: str = "read_parquet(?)",
) -> str:
    """Return the SELECT SQL for a table, with path parameters to bind.

    ``source`` replaces ``read_parquet(?)``, for example with a registered Arrow batch.
    """
    if table.select_plan is None:
        raise ValueError(f"table {table.name} has no select plan")
    if offset < 0 or (limit is not None and limit < 1):
        raise PBError("E_BAD_REQUEST", fields=["offset", "limit"])
    return _select_sql_from_parts(
        plan=table.select_plan,
        dialect=dialect,
        limit=limit,
        source=source,
    )


def build_count_sql(table: TableModel) -> tuple[str, list[str]]:
    """Return SQL to count actual rows in DuckDB and the number of path parameters to bind."""
    select_sql = build_select_sql(table)
    return f"SELECT COUNT(*) FROM ({select_sql}) AS q", ["path"]


def build_expected_count_sql(table: TableModel) -> str | None:
    """Return SQL to count expected rows from array lengths or the explode formula."""
    plan = table.select_plan
    if plan is None or plan.expected_expression is None:
        return None
    cte, from_clause = _source_parts(plan)
    for join in plan.expected_joins:
        expression = f"map_entries({join.expression})" if join.map_entries else join.expression
        from_clause += (
            f" CROSS JOIN UNNEST({expression}) WITH ORDINALITY AS {_quote(join.alias)}"
            f"({_quote('value')}, {_quote('ordinality')})"
        )
    formula = _count_formula(plan.expected_expression, preserve_empty=plan.preserve_empty and plan.explode_expression is not None)
    return cte + f"SELECT COALESCE(SUM({formula}), 0) {from_clause}"


def query_table_preview(
    path: Path,
    table: TableModel,
    *,
    dialect: str = "oracle",
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """Read one page from DuckDB and return its row count."""
    if limit < 1 or limit > 200 or offset < 0:
        raise PBError("E_BAD_REQUEST", fields=["offset", "limit"])
    sql = build_select_sql(table, dialect=dialect, limit=limit, offset=offset)
    params: list[Any] = [literal_path(path), limit, offset]
    count_sql, _ = build_count_sql(table)
    try:
        connection = connect_utc()
        try:
            actual_rows = int(connection.execute(count_sql, [literal_path(path)]).fetchone()[0])
            cursor = connection.execute(sql, params)
            columns = [item[0] for item in cursor.description or ()]
            rows = cursor.fetchall()
            expected_sql = build_expected_count_sql(table)
            expected_rows = (
                int(connection.execute(expected_sql, [literal_path(path)]).fetchone()[0]) if expected_sql else actual_rows
            )
        finally:
            connection.close()
    except PBError:
        raise
    except Exception as exc:
        raise PBError("E_PARQUET_INVALID") from exc
    json_names = {column.name for column in table.columns if column.is_json}
    values: list[dict[str, Any]] = []
    for row in rows:
        item = dict(zip(columns, row))
        for name in json_names:
            value = item.get(name)
            if isinstance(value, str):
                try:
                    item[name] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        values.append({name: _json_safe(value) for name, value in item.items()})
    return {
        "name": table.name,
        "columns": columns,
        "rows": values,
        "offset": offset,
        "page_size": limit,
        "actual_rows": actual_rows,
        "expected_rows": expected_rows,
        "has_next": offset + len(values) < actual_rows,
    }


def sample_parent_key_duplicates(path: Path, column: str, *, sample_rows: int = PARENT_KEY_SAMPLE_ROWS) -> bool:
    """Sample the selected key column evenly across the file to check for duplicates."""
    with pq.ParquetFile(path) as parquet:
        metadata = parquet.metadata
        positions = set(sample_row_positions(metadata.num_rows, sample_rows))
        if not positions:
            return False
        start_by_group: list[int] = []
        cumulative = 0
        for index in range(metadata.num_row_groups):
            start_by_group.append(cumulative)
            cumulative += metadata.row_group(index).num_rows
        sampled: list[int] = []
        for index, start in enumerate(start_by_group):
            end = start + metadata.row_group(index).num_rows
            if any(start <= position < end for position in positions):
                batch_offset = 0
                for batch in parquet.iter_batches(batch_size=STRING_SAMPLE_BATCH_SIZE, columns=[column], row_groups=[index]):
                    batch_end = batch_offset + batch.num_rows
                    for position in positions:
                        local = position - start
                        if batch_offset <= local < batch_end:
                            sampled.append(batch.column(0)[local - batch_offset].as_py())
                    batch_offset = batch_end
        values = sampled
        try:
            return any(value is None for value in values) or len(values) != len(set(values))
        except TypeError:
            return True


def _row_id_column(name: str = "_PB_ROW_ID", string_strategy: str = "sample") -> ColumnModel:
    return _column(
        original_path="_PB_ROW_ID",
        original_name="_PB_ROW_ID",
        name=name,
        data_type=pa.int64(),
        nullable=False,
        source_path=None,
        lengths={},
        string_strategy=string_strategy,
        generated=True,
    )


def _check_unique_names(tables: list[TableModel], policy: str, dialect: str) -> None:
    """Fail instead of emitting DDL with duplicate table or column names."""

    def key(name: str) -> str:
        return name if policy == "quoted" else name.upper()

    table_keys: set[str] = set()
    for table in tables:
        if key(table.name) in table_keys:
            raise PBError("E_IDENTIFIER_INVALID", name=table.name)
        table_keys.add(key(table.name))
        column_keys: set[str] = set()
        for column in table.columns:
            column_key = column.name.casefold() if dialect == "mysql" else key(column.name)
            if column_key in column_keys:
                raise PBError("E_IDENTIFIER_INVALID", name=column.name)
            column_keys.add(column_key)


def plan_tables(
    schema: pa.Schema,
    *,
    table_name: str,
    mode: str = "none",
    dialect: str = "oracle",
    column_policy: str = "upper_unquoted",
    string_strategy: str = "sample",
    length_scan: str = "full",
    lengths: Mapping[str, StringLength] | None = None,
    json_check: bool = False,
    parent_key: str | None = None,
    parent_key_unique: bool | None = None,
    explode_column: str | None = None,
    preserve_empty: bool = True,
    child_max_depth: int = 3,
    reserved_word_provider: ReservedWordProvider | None = None,
) -> list[TableModel]:
    """Build a parent/child table plan from the schema without reading Parquet data."""
    validate_table_name(table_name, dialect, column_policy)
    if mode not in {"none", "child", "explode"}:
        raise PBError("E_FLATTEN_MODE_INVALID", mode=mode)
    if string_strategy not in {"sample", "max", "lob"}:
        raise PBError("E_BAD_REQUEST", fields=["string_strategy"])
    if length_scan not in {"full", "sample"}:
        raise PBError("E_BAD_REQUEST", fields=["length_scan"])
    if child_max_depth < 1:
        raise PBError("E_BAD_REQUEST", fields=["child_max_depth"])
    if lengths is None:
        lengths = {}
    validate_source_schema(schema)
    field_by_name = {field.name: field for field in schema}
    # Expressions read top-level columns by positional alias, because DuckDB
    # resolves names case-insensitively and would map both A and a to A.
    source_columns = source_aliases(len(schema))
    alias_by_name = {field.name: alias for field, alias in zip(schema, source_columns)}

    def source_ref(name: str) -> str:
        return f"src.{_quote(alias_by_name[name])}"

    if parent_key is not None:
        key_field = field_by_name.get(parent_key)
        if key_field is None or container_kind(key_field.type) is not None:
            raise PBError("E_PARENT_KEY_INVALID", column=parent_key)
    else:
        key_field = None

    if mode == "explode":
        selected = field_by_name.get(explode_column or "")
        if selected is None or container_kind(selected.type) != "list":
            raise PBError("E_EXPLODE_COLUMN_INVALID", column=explode_column or "")
    else:
        selected = None

    column_names = _NameAllocator(dialect, column_policy, reserved_word_provider=reserved_word_provider)
    table_names = _NameAllocator(
        dialect, column_policy, table=True, reserved_word_provider=reserved_word_provider,
    )
    table_names.reserve(table_name)
    tables: list[TableModel] = []
    root_warnings: list[dict[str, Any]] = []
    sampled_rows = getattr(lengths, "sampled_rows", None)
    total_rows = getattr(lengths, "total_rows", None)
    if (
        length_scan == "sample"
        and sampled_rows is not None
        and total_rows is not None
        and sampled_rows < total_rows
    ):
        root_warnings.append(warning(
            "W_STRING_LENGTH_SAMPLED",
            sampled_rows=sampled_rows,
            total_rows=total_rows,
            margin=getattr(lengths, "margin", 1.0),
        ))
    internal_row_id = "__parqbridge_internal_row_id"
    while internal_row_id.casefold() in {name.casefold() for name in schema.names}:
        internal_row_id += "_"
    row_id_strategy = (
        "window" if any(name.casefold() == "file_row_number" for name in schema.names) else "file_row_number"
    )
    if mode == "child" and key_field is not None and parent_key_unique is False:
        root_warnings.append(warning("W_PARENT_KEY_DUPLICATE", column=parent_key or key_field.name))

    def add_column(
        output: list[ColumnModel],
        selections: list[SelectItem],
        *,
        data_type: pa.DataType,
        original_path: str,
        original_name: str,
        expression: str,
        nullable: bool = True,
        is_json: bool = False,
        generated: bool = False,
        target_name: str | None = None,
        warnings: list[dict[str, Any]] | None = None,
        allocator: _NameAllocator | None = None,
    ) -> ColumnModel:
        allocator = allocator or column_names
        identifier_source = original_path.replace("[]", "_ITEM").replace(".", "_")
        final_name = target_name or allocator.allocate(identifier_source, warnings)
        column = _column(
            original_path=original_path,
            original_name=original_name,
            name=final_name,
            data_type=data_type,
            nullable=nullable,
            source_path=original_path if not generated else None,
            lengths=lengths,
            string_strategy=string_strategy,
            is_json=is_json,
            generated=generated,
            json_check=json_check,
            extra_warnings=warnings,
        )
        output.append(column)
        selections.append(SelectItem(
            expression,
            final_name,
            json_value=is_json,
            utc_timestamp=column.logical_type == "timestamp_tz",
            timestamp_text=column.logical_type == "timestamp_tz",
        ))
        return column

    # Populate parent-table columns and recursively build list/map plans in parent-first order.
    PendingChild = tuple[
        pa.DataType, str, str, str, str, list[tuple[ColumnModel, str]], tuple[JoinSpec, ...], int
    ]

    def _next_parent_ordinal(parent_keys: list[tuple[ColumnModel, str]], expression: str) -> tuple[ColumnModel, str]:
        used = {column.name for column, _ in parent_keys}
        index = 1
        candidate = "PARENT_ORD"
        while candidate in used:
            index += 1
            candidate = f"PARENT_ORD_{index}"
        column = _column(
            original_path=f"{candidate}:generated",
            original_name=candidate,
            name=candidate,
            data_type=pa.int64(),
            nullable=False,
            source_path=None,
            lengths={},
            string_strategy=string_strategy,
            generated=True,
        )
        return column, expression

    def build_child(
        *,
        data_type: pa.DataType,
        expression: str,
        original_path: str,
        original_name: str,
        parent_table: str,
        parent_keys: list[tuple[ColumnModel, str]],
        outer_joins: tuple[JoinSpec, ...],
        container_depth: int,
    ) -> None:
        kind = container_kind(data_type)
        if kind not in {"list", "map"}:
            return
        alias = f"u{len(outer_joins)}"
        join = JoinSpec(expression=expression, alias=alias, join_type="cross", map_entries=(kind == "map"))
        joins = outer_joins + (join,)
        child_name = table_names.allocate(f"{parent_table}_{original_name}", root_warnings)
        child_allocator = _NameAllocator(
            dialect, column_policy, reserved_word_provider=reserved_word_provider,
        )
        child_columns: list[ColumnModel] = []
        selections: list[SelectItem] = []
        current_table_warnings: list[dict[str, Any]] = []
        pending: list[PendingChild] = []

        for key_column, _ in parent_keys:
            child_allocator.reserve(key_column.name)
        for key_column, key_expression in parent_keys:
            child_columns.append(key_column)
            selections.append(SelectItem(
                key_expression,
                key_column.name,
                utc_timestamp=key_column.logical_type == "timestamp_tz",
                timestamp_text=key_column.logical_type == "timestamp_tz",
            ))

        ordinal_column: ColumnModel | None = None
        if kind == "list":
            ordinal_name = child_allocator.allocate_generated("ORD", current_table_warnings)
            ordinal_column = _column(
                original_path=f"{original_path}[]:ord",
                original_name="ORD",
                name=ordinal_name,
                data_type=pa.int64(),
                nullable=False,
                source_path=None,
                lengths={},
                string_strategy=string_strategy,
                generated=True,
            )
            child_columns.append(ordinal_column)
            selections.append(SelectItem(f"{_quote(alias)}.{_quote('ordinality')} - 1", ordinal_name))

        item_expression = f"{_quote(alias)}.{_quote('value')}"
        if kind == "list":
            value_type = data_type.value_type
            item_kind = container_kind(value_type)
            if item_kind is None:
                value_name = child_allocator.allocate_generated("VALUE", current_table_warnings)
                add_column(
                    child_columns, selections, data_type=value_type, original_path=f"{original_path}[]",
                    original_name="VALUE", expression=item_expression, target_name=value_name,
                    warnings=current_table_warnings, allocator=child_allocator,
                )
            elif item_kind == "struct":
                nested_parent_keys = list(parent_keys)
                if ordinal_column is not None:
                    nested_parent_keys.append(_next_parent_ordinal(
                        parent_keys, f"{_quote(alias)}.{_quote('ordinality')} - 1",
                    ))
                for field in value_type:
                    visit_nested(
                        field.type,
                        _source_field(item_expression, field.name),
                        f"{original_path}[].{field.name}",
                        field.name,
                        child_name,
                        child_columns,
                        selections,
                        nested_parent_keys,
                        joins,
                        container_depth,
                        current_table_warnings,
                        child_allocator,
                        pending,
                    )
                if len(value_type) == 0:
                    value_name = child_allocator.allocate_generated("VALUE", current_table_warnings)
                    add_column(
                        child_columns, selections, data_type=value_type, original_path=f"{original_path}[]",
                        original_name="VALUE", expression=item_expression, is_json=True,
                        target_name=value_name, warnings=current_table_warnings, allocator=child_allocator,
                    )
            elif container_depth >= child_max_depth:
                value_name = child_allocator.allocate_generated("VALUE", current_table_warnings)
                add_column(
                    child_columns, selections, data_type=value_type, original_path=f"{original_path}[]",
                    original_name="VALUE", expression=item_expression, is_json=True,
                    target_name=value_name, warnings=current_table_warnings, allocator=child_allocator,
                )
            else:
                next_parent_keys = list(parent_keys)
                if ordinal_column is not None:
                    next_parent_keys.append(_next_parent_ordinal(
                        parent_keys, f"{_quote(alias)}.{_quote('ordinality')} - 1",
                    ))
                pending.append((
                    value_type, item_expression, f"{original_path}[]", "ITEM", child_name,
                    next_parent_keys, joins, container_depth + 1,
                ))
        else:
            key_type = data_type.key_type
            value_type = data_type.item_type
            key_name = child_allocator.allocate_generated("MAP_KEY", current_table_warnings)
            value_name = child_allocator.allocate_generated("MAP_VALUE", current_table_warnings)
            add_column(
                child_columns, selections, data_type=key_type, original_path=f"{original_path}.key",
                original_name="MAP_KEY", expression=_source_field(item_expression, "key"),
                target_name=key_name, warnings=current_table_warnings, allocator=child_allocator,
            )
            add_column(
                child_columns, selections, data_type=value_type, original_path=f"{original_path}.value",
                original_name="MAP_VALUE", expression=_source_field(item_expression, "value"),
                target_name=value_name, is_json=container_kind(value_type) is not None,
                warnings=current_table_warnings, allocator=child_allocator,
            )

        if not child_columns:
            value_name = child_allocator.allocate_generated("VALUE", current_table_warnings)
            add_column(
                child_columns, selections, data_type=pa.string(), original_path=f"{original_path}[]",
                original_name="VALUE", expression="CAST(NULL AS VARCHAR)", target_name=value_name,
                warnings=current_table_warnings, allocator=child_allocator,
            )

        child_model = TableModel(
            name=child_name,
            columns=tuple(child_columns),
            parent_table=parent_table,
            parent_key=tuple(column.name for column, _ in parent_keys),
            parent_key_unique=parent_key_unique,
            source_path=original_path,
            mode=mode,
            identifier_policy=column_policy,
            warnings=tuple(current_table_warnings),
            select_plan=SelectPlan(
                selections=tuple(selections),
                joins=joins,
                internal_row_id=internal_row_id,
                needs_row_id=True,
                row_id_strategy=row_id_strategy,
                expected_expression=f"map_entries({expression})" if kind == "map" else expression,
                expected_joins=outer_joins,
                source_columns=source_columns,
                order_by=(
                    f"src.{_quote(internal_row_id)}",
                    *(expression for _, expression in parent_keys),
                    f"{_quote(alias)}.{_quote('ordinality')}",
                ),
            ),
        )
        tables.append(child_model)
        for nested_type, nested_expression, nested_path, nested_name, nested_parent, nested_keys, nested_joins, nested_depth in pending:
            build_child(
                data_type=nested_type,
                expression=nested_expression,
                original_path=nested_path,
                original_name=nested_name,
                parent_table=nested_parent,
                parent_keys=nested_keys,
                outer_joins=nested_joins,
                container_depth=nested_depth,
            )

    def visit_nested(
        data_type: pa.DataType,
        expression: str,
        original_path: str,
        original_name: str,
        current_table_name: str,
        columns: list[ColumnModel],
        selections: list[SelectItem],
        parent_keys: list[tuple[ColumnModel, str]],
        joins: tuple[JoinSpec, ...],
        depth: int,
        local_warnings: list[dict[str, Any]],
        allocator: _NameAllocator,
        pending_children: list[PendingChild] | None = None,
        collections_as_json: bool = False,
    ) -> None:
        kind = container_kind(data_type)
        if kind == "struct":
            for child in data_type:
                visit_nested(
                    child.type,
                    _source_field(expression, child.name),
                    f"{original_path}.{child.name}",
                    child.name,
                    current_table_name,
                    columns,
                    selections,
                    parent_keys,
                    joins,
                    depth,
                    local_warnings,
                    allocator,
                    pending_children,
                    collections_as_json,
                )
        elif kind in {"list", "map"}:
            if collections_as_json or depth >= child_max_depth:
                add_column(
                    columns, selections, data_type=data_type, original_path=original_path,
                    original_name=original_name, expression=expression, is_json=True,
                    warnings=local_warnings, allocator=allocator,
                )
            elif pending_children is not None:
                pending_children.append((
                    data_type, expression, original_path, original_name, current_table_name,
                    parent_keys, joins, depth + 1,
                ))
            else:
                build_child(
                    data_type=data_type,
                    expression=expression,
                    original_path=original_path,
                    original_name=original_name,
                    parent_table=current_table_name,
                    parent_keys=parent_keys,
                    outer_joins=joins,
                    container_depth=depth + 1,
                )
        else:
            add_column(
                columns, selections, data_type=data_type, original_path=original_path,
                original_name=original_name, expression=expression,
                warnings=local_warnings, allocator=allocator,
            )

    # In child mode, choose the parent key first. A user key is one scalar column; otherwise, use the file row ID.
    root_key: ColumnModel
    root_key_expression: str
    if mode == "child" and key_field is not None:
        key_target_name = column_names.allocate(key_field.name, root_warnings)
        root_key = _column(
            original_path=key_field.name,
            original_name=key_field.name,
            name=key_target_name,
            data_type=key_field.type,
            nullable=key_field.nullable,
            source_path=key_field.name,
            lengths=lengths,
            string_strategy=string_strategy,
            extra_warnings=root_warnings,
        )
        root_key_expression = source_ref(key_field.name)
    elif mode == "child":
        generated_name = column_names.allocate_generated("_PB_ROW_ID", root_warnings)
        root_key = _row_id_column(name=generated_name, string_strategy=string_strategy)
        root_key_expression = f"src.{_quote(internal_row_id)}"
    else:
        root_key = _row_id_column(string_strategy=string_strategy)
        root_key_expression = f"src.{_quote(internal_row_id)}"

    root_columns: list[ColumnModel] = []
    root_selections: list[SelectItem] = []
    root_child_names: list[str] = []

    if mode == "none":
        for field in schema:
            expr = source_ref(field.name)
            is_json = container_kind(field.type) is not None
            add_column(
                root_columns, root_selections, data_type=field.type,
                original_path=field.name, original_name=field.name, expression=expr,
                nullable=field.nullable, is_json=is_json, warnings=root_warnings,
            )
    elif mode == "child":
        root_key_added = False
        for field in schema:
            expr = source_ref(field.name)
            kind = container_kind(field.type)
            if key_field is not None and field.name == key_field.name and not root_key_added:
                root_columns.append(root_key)
                root_selections.append(SelectItem(
                    root_key_expression,
                    root_key.name,
                    utc_timestamp=root_key.logical_type == "timestamp_tz",
                    timestamp_text=root_key.logical_type == "timestamp_tz",
                ))
                root_key_added = True
                continue
            if kind in {"list", "map"}:
                build_child(
                    data_type=field.type,
                    expression=expr,
                    original_path=field.name,
                    original_name=field.name,
                    parent_table=table_name,
                    parent_keys=[(root_key, root_key_expression)],
                    outer_joins=(),
                    container_depth=1,
                )
            else:
                visit_nested(
                    field.type, expr, field.name, field.name, table_name,
                    root_columns, root_selections,
                    [(root_key, root_key_expression)], (), 0,
                    root_warnings, column_names,
                )
        if key_field is None:
            root_columns.insert(0, root_key)
            root_selections.insert(0, SelectItem(
                root_key_expression,
                root_key.name,
                utc_timestamp=root_key.logical_type == "timestamp_tz",
                timestamp_text=root_key.logical_type == "timestamp_tz",
            ))
        # Normalize the relationship list to remove ordering differences and duplicates.
        root_child_names = [t.name for t in tables if t.parent_table == table_name]
    else:  # explode
        assert selected is not None
        exploded_alias = "u0"
        selected_expression = source_ref(selected.name)
        exploded_join = JoinSpec(
            expression=selected_expression,
            alias=exploded_alias,
            join_type="left" if preserve_empty else "cross",
        )
        for field in schema:
            expr = source_ref(field.name)
            if field.name == selected.name:
                element_expr = f"{_quote(exploded_alias)}.{_quote('value')}"
                value_type = selected.type.value_type
                if container_kind(value_type) == "struct":
                    for child in value_type:
                        visit_nested(
                            child.type, _source_field(element_expr, child.name),
                            f"{field.name}[].{child.name}", child.name, table_name,
                            root_columns, root_selections, [], (), 0, root_warnings,
                            column_names, collections_as_json=True,
                        )
                else:
                    is_json = container_kind(value_type) is not None
                    add_column(
                        root_columns, root_selections, data_type=value_type,
                        original_path=f"{field.name}[]", original_name=field.name,
                        expression=element_expr, is_json=is_json,
                        target_name=column_names.allocate(field.name, root_warnings),
                        warnings=root_warnings,
                    )
            elif container_kind(field.type) == "struct":
                visit_nested(
                    field.type, expr, field.name, field.name, table_name,
                    root_columns, root_selections, [], (), 0, root_warnings,
                    column_names, collections_as_json=True,
                )
            else:
                is_json = container_kind(field.type) is not None
                add_column(
                    root_columns, root_selections, data_type=field.type,
                    original_path=field.name, original_name=field.name,
                    expression=expr, nullable=field.nullable, is_json=is_json,
                    warnings=root_warnings,
                )

    if not root_columns:
        raise PBError("E_PARQUET_INVALID")
    plan_joins = (exploded_join,) if mode == "explode" else ()
    expected_expression = source_ref(selected.name) if selected is not None else None
    root = TableModel(
        name=table_name,
        columns=tuple(root_columns),
        child_tables=tuple(root_child_names),
        parent_key=tuple([root_key.name]) if mode == "child" else (),
        parent_key_unique=parent_key_unique,
        source_path="",
        mode=mode,
        identifier_policy=column_policy,
        warnings=tuple(root_warnings),
        select_plan=SelectPlan(
            selections=tuple(root_selections),
            joins=plan_joins,
            internal_row_id=internal_row_id,
            needs_row_id=mode in {"child", "explode"},
            row_id_strategy=row_id_strategy,
            expected_expression=expected_expression,
            explode_expression=expected_expression,
            preserve_empty=preserve_empty,
            source_columns=source_columns,
            order_by=(f"src.{_quote(internal_row_id)}", f"{_quote('u0')}.{_quote('ordinality')}") if mode == "explode" else (),
        ),
    )
    tables.insert(0, root)
    children_by_parent: dict[str, list[str]] = {}
    for table in tables:
        if table.parent_table:
            children_by_parent.setdefault(table.parent_table, []).append(table.name)
    planned = [replace(table, child_tables=tuple(children_by_parent.get(table.name, ()))) for table in tables]
    _check_unique_names(planned, column_policy, dialect)
    if dialect == "mysql":
        planned = [enforce_mysql_row_size(table) for table in planned]
    if dialect == "postgres":
        for table in planned:
            if len(table.columns) > POSTGRES_MAX_COLUMNS:
                raise PBError(
                    "E_POSTGRES_COLUMN_LIMIT",
                    table=table.name,
                    max_columns=POSTGRES_MAX_COLUMNS,
                )
    return planned
