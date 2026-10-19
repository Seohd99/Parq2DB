"""Request bodies accepted by the API and reused by the CLI."""

from __future__ import annotations

from pydantic import BaseModel


class LocalFileSelection(BaseModel):
    path: str


class DdlOptions(BaseModel):
    """Options for DDL generation and flattening previews."""

    dialect: str = "oracle"
    mode: str = "none"
    table_name: str | None = None
    column_policy: str = "upper_unquoted"
    string_strategy: str = "sample"
    length_scan: str = "full"
    explode_column: str | None = None
    preserve_empty: bool = True
    parent_key: str | None = None
    json_check: bool = False
    # Top-level source columns to include in DDL, CREATE, and INSERT; None keeps all.
    columns: list[str] | None = None
    preview_offset: int = 0
    preview_limit: int = 25


class LoadOptions(DdlOptions):
    """Options for CREATE+INSERT loads and their existing-table policy."""

    existing_policy: str = "fail"
    confirm_table_name: str | None = None


class InsertOptions(BaseModel):
    """Options for INSERT into one existing table; columns match by name."""

    dialect: str = "oracle"
    table_name: str
    # Matched top-level source columns to insert; None inserts every matched column.
    columns: list[str] | None = None
