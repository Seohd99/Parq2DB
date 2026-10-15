"""PostgreSQL CREATE TABLE statements from shared table plans."""

from __future__ import annotations

from collections.abc import Iterable

from app.core.errors import PBError
from app.core.model import TableModel
from app.core.typemap import POSTGRES_MAX_COLUMNS


def _identifier(name: str, policy: str) -> str:
    if policy == "quoted":
        return '"' + name.replace('"', '""') + '"'
    return name


def generate_postgres_ddl(tables: Iterable[TableModel]) -> str:
    """Generate parent-first PostgreSQL CREATE TABLE statements without I/O."""
    statements: list[str] = []
    for table in tables:
        if len(table.columns) > POSTGRES_MAX_COLUMNS:
            raise PBError(
                "E_POSTGRES_COLUMN_LIMIT",
                table=table.name,
                max_columns=POSTGRES_MAX_COLUMNS,
            )
        table_name = _identifier(table.name, table.identifier_policy)
        columns: list[str] = []
        for column in table.columns:
            name = _identifier(column.name, table.identifier_policy)
            definition = f"  {name} {column.sql_type('postgres')}"
            if not column.nullable:
                definition += " NOT NULL"
            columns.append(definition)
        statements.append(f"CREATE TABLE {table_name} (\n" + ",\n".join(columns) + "\n);")
    return "\n\n".join(statements) + ("\n" if statements else "")
