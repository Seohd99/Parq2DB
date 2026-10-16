"""Generate MySQL 8 CREATE TABLE statements as a pure function."""

from __future__ import annotations

from collections.abc import Iterable

from app.core.dialects import quote_identifier
from app.core.model import TableModel
from app.core.typemap import enforce_mysql_row_size


def generate_mysql_ddl(tables: Iterable[TableModel]) -> str:
    """Generate InnoDB/utf8mb4 tables in parent-to-child order."""
    statements: list[str] = []
    for planned_table in tables:
        table = enforce_mysql_row_size(planned_table)
        table_name = quote_identifier(table.name, "mysql", table.identifier_policy == "quoted")
        columns: list[str] = []
        for column in table.columns:
            name = quote_identifier(column.name, "mysql", table.identifier_policy == "quoted")
            definition = f"  {name} {column.sql_type('mysql')}"
            if not column.nullable:
                definition += " NOT NULL"
            columns.append(definition)
        statements.append(
            f"CREATE TABLE {table_name} (\n" + ",\n".join(columns)
            + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        )
    return "\n\n".join(statements) + ("\n" if statements else "")
