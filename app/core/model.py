"""Pure data models shared by DDL generation and query planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnModel:
    """Store the mapping between a source column and its target database column."""

    original_name: str
    original_path: str
    name: str
    arrow_type: Any
    logical_type: str
    nullable: bool = True
    length: int | None = None
    byte_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_json: bool = False
    generated: bool = False
    check_json: bool = False
    sql_type_oracle: str | None = None
    sql_type_mysql: str | None = None
    sql_type_postgres: str | None = None
    source_path: str | None = None

    def sql_type(self, dialect: str) -> str:
        """Return the SQL type for the target database."""
        values = {
            "oracle": self.sql_type_oracle,
            "mysql": self.sql_type_mysql,
            "postgres": self.sql_type_postgres,
        }
        if dialect not in values:
            raise ValueError(f"unknown SQL dialect: {dialect}")
        value = values[dialect]
        if value is None:
            raise ValueError(f"missing {dialect} type for {self.name}")
        return value


@dataclass(frozen=True)
class SelectItem:
    """Represent one SELECT expression in a query plan."""

    expression: str
    name: str
    json_value: bool = False
    utc_timestamp: bool = False
    timestamp_text: bool = False


@dataclass(frozen=True)
class JoinSpec:
    """Represent one DuckDB UNNEST join."""

    expression: str
    alias: str
    join_type: str = "cross"  # cross or left
    map_entries: bool = False


@dataclass(frozen=True)
class SelectPlan:
    """Represent a DuckDB SELECT plan that accepts the file path as a parameter."""

    selections: tuple[SelectItem, ...]
    joins: tuple[JoinSpec, ...] = ()
    internal_row_id: str = "__parq2db_internal_row_id"
    needs_row_id: bool = False
    row_id_strategy: str = "file_row_number"
    expected_expression: str | None = None
    expected_joins: tuple[JoinSpec, ...] = ()
    order_by: tuple[str, ...] = ()
    explode_expression: str | None = None
    preserve_empty: bool = True
    # Positional aliases for the file's top-level columns; expressions reference these.
    source_columns: tuple[str, ...] = ()
    # Selected top-level source columns in file order and their positional aliases.
    # A source that holds only these columns, such as a projected Arrow batch, uses them.
    read_columns: tuple[str, ...] = ()
    read_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableModel:
    """Store table DDL, relationships, and a reusable SELECT plan."""

    name: str
    columns: tuple[ColumnModel, ...]
    parent_table: str | None = None
    child_tables: tuple[str, ...] = ()
    parent_key: tuple[str, ...] = ()
    parent_key_unique: bool | None = None
    source_path: str = ""
    mode: str = "none"
    identifier_policy: str = "upper_unquoted"
    warnings: tuple[dict[str, Any], ...] = ()
    select_plan: SelectPlan | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class StringLength:
    """Store a column string's character count and encoded byte count."""

    characters: int = 0
    bytes: int = 0


class StringLengthMeasurements(dict[str, StringLength]):
    """Store length measurements and full-file metadata for sampled measurements."""

    def __init__(
        self,
        values: dict[str, StringLength] | None = None,
        *,
        sampled_rows: int = 0,
        total_rows: int = 0,
        margin: float = 1.0,
    ) -> None:
        super().__init__(values or {})
        self.sampled_rows = sampled_rows
        self.total_rows = total_rows
        self.margin = margin

