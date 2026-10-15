"""Generate Oracle 19c CREATE TABLE statements as a pure function."""

from __future__ import annotations

from collections.abc import Iterable

from app.core.model import TableModel


def _identifier(name: str, policy: str) -> str:
    if policy == "quoted":
        return '"' + name.replace('"', '""') + '"'
    return name


def generate_oracle_ddl(tables: Iterable[TableModel], *, json_check: bool = False) -> str:
    """Generate Oracle CREATE TABLE statements in parent-to-child order."""
    statements: list[str] = []
    for table in tables:
        table_name = _identifier(table.name, table.identifier_policy)
        columns = []
        for column in table.columns:
            name = _identifier(column.name, table.identifier_policy)
            definition = f"  {name} {column.sql_type('oracle')}"
            if not column.nullable:
                definition += " NOT NULL"
            if column.is_json and (json_check or column.check_json):
                definition += f" CHECK ({name} IS JSON)"
            columns.append(definition)
        statements.append(f"CREATE TABLE {table_name} (\n" + ",\n".join(columns) + "\n);")
    return "\n\n".join(statements) + ("\n" if statements else "")
