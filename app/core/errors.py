"""에러 코드 정의.

API는 사람이 읽는 문구 대신 에러 코드와 파라미터를 돌려준다. 문구는 프런트엔드가
app/i18n/<lang>.json 의 "error.<CODE>" 키로 번역한다. 경고(W_*)도 같은 형식을 쓴다.
"""

from __future__ import annotations

from typing import Any

# 코드 -> 기본 HTTP 상태. 새 코드를 추가하면 ko.json/en.json 에도 문구를 추가한다.
ERROR_STATUS: dict[str, int] = {
    "E_INTERNAL": 500,
    "E_BAD_REQUEST": 400,
    "E_CONFIG_INVALID": 500,
    "E_DB_NOT_CONFIGURED": 409,
    "E_DB_PASSWORD_MISSING": 409,
    "E_DB_CONFIG_INVALID": 409,
    "E_UPLOAD_TOO_LARGE": 413,
    "E_PARQUET_INVALID": 422,
    "E_PATH_NOT_ALLOWED": 403,
    "E_FILE_NOT_FOUND": 404,
    "E_TABLE_NAME_INVALID": 400,
    "E_IDENTIFIER_INVALID": 400,
    "E_EXPLODE_COLUMN_INVALID": 400,
    "E_PARENT_KEY_INVALID": 400,
    "E_FLATTEN_MODE_INVALID": 400,
    "E_DIALECT_INVALID": 400,
    "E_CONFIRM_REQUIRED": 409,
    "E_DDL_OUTPUT_PROTECTED": 400,
    "E_DDL_OUTPUT_EXISTS": 409,
    "E_TABLE_EXISTS": 409,
    "E_APPEND_COLUMNS_MISMATCH": 409,
    "E_DB_CONNECT": 502,
    "E_DB_OPERATION_FAILED": 502,
    "E_LOAD_FAILED": 500,
    "E_JOB_NOT_FOUND": 404,
    "E_JOB_CONFLICT": 409,
    "E_STRING_SCAN_FAILED": 500,
    "E_STRING_SCAN_CANCELLED": 409,
    "E_LOCAL_INFILE_UNAVAILABLE": 501,
    "E_POSTGRES_NUL_CHARACTER": 400,
    "E_POSTGRES_COLUMN_LIMIT": 400,
    "E_ROUTE_NOT_FOUND": 404,
    "E_METHOD_NOT_ALLOWED": 405,
    "E_HTTP_ERROR": 500,
}

# 경고 코드(응답의 warnings 배열에 실린다).
WARNING_CODES: frozenset[str] = frozenset(
    {
        "W_BIND_ALL_INTERFACES",
        "W_PASSWORD_IN_MAIN_CONFIG",
        "W_ALLOWED_ROOT_MISSING",
        "W_DECIMAL_PRECISION_WIDE",
        "W_TIMESTAMP_TZ_UTC",
        "W_TIMESTAMP_PRECISION_LOSS",
        "W_TIME_PRECISION_LOSS",
        "W_UNSUPPORTED_TYPE",
        "W_RESERVED_IDENTIFIER",
        "W_PARENT_KEY_DUPLICATE",
        "W_EXPLODE_ROW_MULTIPLIER",
        "W_STRING_LENGTH_SAMPLED",
        "W_STRING_LENGTH_ESTIMATED",
        "W_MYSQL_ROW_SIZE_DEMOTED",
        "W_IDENTIFIER_COLLISION",
        "W_RESERVED_WORDS_UNAVAILABLE",
    }
)


class PBError(Exception):
    """ParqBridge 도메인 에러. code 는 ERROR_STATUS 의 키다."""

    def __init__(self, code: str, status: int | None = None, **params: Any) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(code)
        self.code = code
        self.status = status or ERROR_STATUS[code]
        self.params = params

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": self.params}

    def __str__(self) -> str:
        return f"{self.code} {self.params}" if self.params else self.code


def warning(code: str, **params: Any) -> dict[str, Any]:
    """경고 항목 하나를 만든다."""
    if code not in WARNING_CODES:
        raise ValueError(f"unknown warning code: {code}")
    return {"code": code, "params": params}
