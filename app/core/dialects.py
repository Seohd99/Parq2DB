"""Database dialects supported by ParqBridge."""

SUPPORTED_DIALECTS: tuple[str, ...] = ("oracle", "mysql", "postgres")

DIALECT_LABELS: dict[str, str] = {
    "oracle": "Oracle",
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
}
