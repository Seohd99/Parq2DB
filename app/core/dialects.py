"""Database dialects supported by ParqBridge."""

SUPPORTED_DIALECTS: tuple[str, ...] = ("oracle", "mysql", "postgres")

DIALECT_LABELS: dict[str, str] = {
    "oracle": "Oracle",
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
}


def quote_identifier(name: str, dialect: str, quoted: bool = True) -> str:
    """Quote a table or column name for one dialect; unquoted names pass through unchanged."""
    if not quoted:
        return name
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'
