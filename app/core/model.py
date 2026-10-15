"""DDL과 조회 계획이 공유하는 순수 데이터 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ColumnModel:
    """원본 컬럼과 대상 DB 컬럼의 대응 정보를 보관한다."""

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
        """대상 DB의 SQL 타입을 반환한다."""
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
    """조회 계획의 한 SELECT 표현식."""

    expression: str
    name: str
    json_value: bool = False
    utc_timestamp: bool = False
    timestamp_text: bool = False


@dataclass(frozen=True)
class JoinSpec:
    """DuckDB UNNEST 조인 하나."""

    expression: str
    alias: str
    join_type: str = "cross"  # cross 또는 left
    map_entries: bool = False


@dataclass(frozen=True)
class SelectPlan:
    """파일 경로를 파라미터로 받는 DuckDB SELECT 계획."""

    selections: tuple[SelectItem, ...]
    joins: tuple[JoinSpec, ...] = ()
    internal_row_id: str = "__parqbridge_internal_row_id"
    needs_row_id: bool = False
    row_id_strategy: str = "file_row_number"
    expected_expression: str | None = None
    expected_joins: tuple[JoinSpec, ...] = ()
    order_by: tuple[str, ...] = ()
    explode_expression: str | None = None
    preserve_empty: bool = True


@dataclass(frozen=True)
class TableModel:
    """테이블 DDL, 관계와 재사용 SELECT 계획."""

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
    """컬럼 문자열의 문자 수와 인코딩 바이트 수."""

    characters: int = 0
    bytes: int = 0


class StringLengthMeasurements(dict[str, StringLength]):
    """길이 측정값과 표본 측정 시 전체 파일 기준 정보를 함께 보관한다."""

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

