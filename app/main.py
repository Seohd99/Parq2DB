"""FastAPI 앱: API 라우트와 정적 파일 서빙.

기동:
  python -m app                 # config 의 host/port 사용(기본 127.0.0.1:8765)
  uvicorn app.main:app          # uvicorn 기본값 127.0.0.1:8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from python_multipart import MultipartParser
from python_multipart.multipart import FormParserError, MultipartParseError, parse_options_header
import pyarrow as pa
import pyarrow.parquet as pq

from app import __version__
from app.core.config import AppConfig, load_config
from app.core.dialects import SUPPORTED_DIALECTS
from app.core.db import close_quietly, connect, require_online, test_connection
from app.core.errors import PBError, warning
from app.core.files import FileRegistry, SelectedFile, StreamingUpload, list_allowed_directory
from app.core.flatten import (
    build_expected_count_sql,
    build_select_sql,
    default_table_name,
    plan_tables,
    query_table_preview,
    sample_parent_key_duplicates,
)
from app.core.inspect import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, inspect_parquet, preview_parquet
from app.core.model import StringLengthMeasurements, TableModel
from app.core.typemap import container_kind, measure_string_lengths
from app.core.ddl_mysql import generate_mysql_ddl
from app.core.ddl_oracle import generate_oracle_ddl
from app.core.ddl_postgres import generate_postgres_ddl
from app.core.jobs import LoadJobManager
from app.core.reserved import fetch_oracle_reserved_words
from app.core.string_scans import StringScanManager

log = logging.getLogger("parqbridge")

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
I18N_DIR = APP_DIR / "i18n"
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _config_from_env() -> AppConfig:
    # 테스트나 다른 설정 파일로 띄울 때 PARQBRIDGE_CONFIG 로 경로를 바꿀 수 있다.
    p = os.environ.get("PARQBRIDGE_CONFIG")
    return load_config(Path(p) if p else None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "config"):
        app.state.config = _config_from_env()
    app.state.files = FileRegistry(app.state.config.server.upload_dir, app.state.config.server.allowed_roots)
    app.state.jobs = LoadJobManager()
    app.state.string_scans = StringScanManager(max_workers=2)
    try:
        yield
    finally:
        app.state.jobs.shutdown()
        app.state.string_scans.shutdown()


app = FastAPI(title="ParqBridge", version=__version__, lifespan=lifespan)


@app.exception_handler(PBError)
async def _pb_error(_: Request, exc: PBError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.to_dict()})


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [".".join(str(x) for x in e.get("loc", ())) for e in exc.errors()]
    return JSONResponse(status_code=400, content={"error": {"code": "E_BAD_REQUEST", "params": {"fields": fields}}})


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {
        400: "E_BAD_REQUEST",
        404: "E_ROUTE_NOT_FOUND",
        405: "E_METHOD_NOT_ALLOWED",
    }.get(exc.status_code, "E_HTTP_ERROR")
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "params": {}}})


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "E_INTERNAL", "params": {"type": type(exc).__name__}}},
    )


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_files(request: Request) -> FileRegistry:
    return request.app.state.files


def get_jobs(request: Request) -> LoadJobManager:
    return request.app.state.jobs


def get_string_scans(request: Request) -> StringScanManager:
    return request.app.state.string_scans


def _get_or_start_length_scan(file_id: str, selected: SelectedFile, request: Request) -> dict:
    scans = get_string_scans(request)
    try:
        return scans.state(file_id)
    except PBError as exc:
        if exc.code != "E_FILE_NOT_FOUND":
            raise
        return scans.start(file_id, selected.path)


def get_db_profile(request: Request, dialect: str):
    if dialect not in SUPPORTED_DIALECTS:
        raise PBError("E_DIALECT_INVALID", dialect=dialect)
    profile = get_config(request).db.get(dialect)
    if profile is None:
        raise PBError("E_DB_NOT_CONFIGURED")
    require_online(profile)
    return profile


async def _read_streaming_upload(request: Request, registry: FileRegistry, max_bytes: int) -> SelectedFile:
    """multipart 본문을 조각 단위로 파싱해 파일 파트만 업로드 파일에 기록한다."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise PBError("E_BAD_REQUEST") from exc
        if declared_length < 0:
            raise PBError("E_BAD_REQUEST")
        if declared_length > max_bytes + MULTIPART_OVERHEAD_BYTES:
            raise PBError("E_UPLOAD_TOO_LARGE", max_bytes=max_bytes)

    try:
        media_type, options = parse_options_header(request.headers.get("content-type"))
        boundary = options.get(b"boundary")
        if media_type.lower() != b"multipart/form-data" or not boundary:
            raise PBError("E_BAD_REQUEST")
    except (TypeError, ValueError) as exc:
        raise PBError("E_BAD_REQUEST") from exc

    headers: dict[bytes, bytes] = {}
    header_field = bytearray()
    header_value = bytearray()
    file_parts = 0
    current_is_file = False
    parser_ended = False
    upload: StreamingUpload | None = None

    def on_part_begin() -> None:
        nonlocal current_is_file
        headers.clear()
        header_field.clear()
        header_value.clear()
        current_is_file = False

    def on_header_begin() -> None:
        header_field.clear()
        header_value.clear()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header_field.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header_value.extend(data[start:end])

    def on_header_end() -> None:
        headers[bytes(header_field).lower()] = bytes(header_value).strip()
        header_field.clear()
        header_value.clear()

    def on_headers_finished() -> None:
        nonlocal current_is_file, file_parts, upload
        disposition_header = headers.get(b"content-disposition")
        if disposition_header is None:
            raise PBError("E_BAD_REQUEST")
        try:
            disposition, parameters = parse_options_header(disposition_header)
        except (FormParserError, TypeError, ValueError) as exc:
            raise PBError("E_BAD_REQUEST") from exc
        if disposition.lower() != b"form-data":
            raise PBError("E_BAD_REQUEST")

        has_filename = b"filename" in parameters or b"filename*" in parameters
        if not has_filename:
            return
        file_parts += 1
        if file_parts != 1 or parameters.get(b"name") != b"file":
            raise PBError("E_BAD_REQUEST")
        filename = parameters.get(b"filename", parameters.get(b"filename*", b""))
        upload = registry.begin_upload(filename.decode("utf-8", errors="replace"), max_bytes)
        current_is_file = True

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if current_is_file and upload is not None:
            upload.write(data[start:end])

    def on_part_end() -> None:
        nonlocal current_is_file
        current_is_file = False

    def on_end() -> None:
        nonlocal parser_ended
        parser_ended = True

    callbacks = {
        "on_part_begin": on_part_begin,
        "on_header_begin": on_header_begin,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
        "on_end": on_end,
    }
    try:
        try:
            parser = MultipartParser(
                boundary,
                callbacks,
                max_size=max_bytes + MULTIPART_OVERHEAD_BYTES,
            )
        except (FormParserError, TypeError, ValueError) as exc:
            raise PBError("E_BAD_REQUEST") from exc

        async for chunk in request.stream():
            try:
                consumed = parser.write(chunk)
            except PBError:
                raise
            except MultipartParseError as exc:
                raise PBError("E_BAD_REQUEST") from exc
            if consumed != len(chunk):
                raise PBError("E_UPLOAD_TOO_LARGE", max_bytes=max_bytes)

        if not parser_ended or file_parts != 1 or upload is None:
            raise PBError("E_BAD_REQUEST")
        return upload.finish()
    finally:
        if upload is not None:
            upload.abort()


class LocalFileSelection(BaseModel):
    path: str


class DdlOptions(BaseModel):
    """DDL 생성과 펼치기 미리보기 옵션."""

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
    preview_offset: int = 0
    preview_limit: int = 25


class LoadOptions(DdlOptions):
    """CREATE/INSERT 작업과 기존 테이블 정책."""

    existing_policy: str = "fail"
    confirm_table_name: str | None = None


@app.get("/api/status")
def status(request: Request) -> dict:
    """버전, 서버 제한, DB 프로필의 온라인/오프라인 상태(비밀번호 제외)."""
    return {"version": __version__, **get_config(request).public()}


@app.post("/api/db/{dialect}/test")
def test_db_connection(dialect: str, request: Request) -> dict:
    """설정된 프로필로 접속하고 서버 버전과 공개 대상 정보만 반환한다."""
    profile = get_db_profile(request, dialect)
    return {"connected": True, **test_connection(profile)}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/i18n", StaticFiles(directory=I18N_DIR), name="i18n")


@app.get("/api/files/browse")
def browse_files(request: Request, path: str | None = None) -> dict:
    """허용 루트의 폴더와 Parquet 파일만 나열한다."""
    roots = get_config(request).server.allowed_roots
    if path is None:
        return {"roots": [str(root) for root in roots], "current": None, "parent": None, "entries": []}
    result = list_allowed_directory(path, roots)
    return {"roots": [str(root) for root in roots], **result}


@app.post("/api/files/local")
def select_local_file(body: LocalFileSelection, request: Request) -> dict:
    """허용된 로컬 Parquet 경로를 읽기 전용 file_id로 등록한다."""
    selected = get_files(request).select_local(body.path, get_config(request).server.allowed_roots)
    scan = get_string_scans(request).start(selected.file_id, selected.path)
    return {
        "file": {
            "file_id": selected.file_id,
            "display_name": selected.display_name,
            "source": selected.source,
            "size": selected.path.stat().st_size,
            "length_scan": scan,
        }
    }


@app.post("/api/files/upload")
async def upload_file(request: Request) -> dict:
    """multipart 본문을 스트리밍하면서 파일 크기와 Parquet 매직을 검사한다."""
    selected = await _read_streaming_upload(
        request,
        get_files(request),
        get_config(request).server.upload_max_bytes,
    )
    scan = get_string_scans(request).start(selected.file_id, selected.path)
    return {
        "file": {
            "file_id": selected.file_id,
            "display_name": selected.display_name,
            "source": selected.source,
            "size": selected.path.stat().st_size,
            "length_scan": scan,
        }
    }


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str, request: Request) -> dict:
    """업로드 저장소에 등록된 파일만 삭제한다."""
    selected = get_files(request).get(file_id)
    if selected.source != "upload":
        raise PBError("E_PATH_NOT_ALLOWED")
    get_string_scans(request).release(file_id, wait=True)
    get_files(request).delete_upload(file_id)
    return {"deleted": True}


@app.post("/api/files/{file_id}/release")
def release_file_selection(file_id: str, request: Request) -> dict:
    """Cancel an unused selection's scan while leaving its source file untouched."""
    get_files(request).get(file_id)
    get_string_scans(request).release(file_id)
    return {"released": True}


@app.get("/api/files/{file_id}/length-scan")
def get_file_length_scan(file_id: str, request: Request) -> dict:
    """Return the current full string-length scan state for one file handle."""
    get_files(request).get(file_id)
    return get_string_scans(request).state(file_id)


@app.get("/api/files/{file_id}/inspect")
def inspect_file(file_id: str, request: Request) -> dict:
    selected = get_files(request).get(file_id)
    return inspect_parquet(selected.path)


@app.get("/api/files/{file_id}/preview")
def preview_file(
    file_id: str,
    request: Request,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """미리보기는 요청된 페이지 크기만 DuckDB에서 읽는다."""
    if limit > MAX_PAGE_SIZE:
        raise PBError("E_BAD_REQUEST", fields=["limit"])
    selected = get_files(request).get(file_id)
    return preview_parquet(selected.path, offset=offset, limit=limit)


def _warning_for_dialect(item: dict, dialect: str) -> bool:
    code = item.get("code")
    params = item.get("params") or {}
    if code in {"W_DECIMAL_PRECISION_WIDE", "W_UNSUPPORTED_TYPE"}:
        return params.get("dialect") == dialect
    if code == "W_TIMESTAMP_TZ_UTC":
        return dialect == "mysql"
    if code in {"W_TIMESTAMP_PRECISION_LOSS", "W_TIME_PRECISION_LOSS"}:
        return params.get("dialect", "mysql") == dialect
    return True


def _public_table(table: TableModel, counts: dict[str, dict], dialect: str) -> dict:
    count = counts.get(table.name, {})
    return {
        "name": table.name,
        "parent_table": table.parent_table,
        "child_tables": list(table.child_tables),
        "parent_key": list(table.parent_key),
        "parent_key_unique": table.parent_key_unique,
        "source_path": table.source_path,
        "actual_rows": count.get("actual_rows"),
        "expected_rows": count.get("expected_rows"),
        "columns": [
            {
                "original_name": column.original_name,
                "original_path": column.original_path,
                "name": column.name,
                "arrow_type": str(column.arrow_type),
                "logical_type": column.logical_type,
                "sql_type": column.sql_type(dialect),
                "nullable": column.nullable,
                "length": column.length,
                "byte_length": column.byte_length,
                "precision": column.precision,
                "scale": column.scale,
                "is_json": column.is_json,
                "generated": column.generated,
            }
            for column in table.columns
        ],
    }


def _oracle_reserved_provider(request: Request, dialect: str):
    """Use V$RESERVED_WORDS when the Oracle profile is online; otherwise use built-ins."""
    if dialect != "oracle":
        return None, []
    profile = get_config(request).db.get("oracle")
    if profile is None or not profile.online:
        return None, []
    connection = None
    try:
        connection = connect(profile)
        words = fetch_oracle_reserved_words(connection)
        return lambda _dialect: words, []
    except Exception:
        return None, [warning("W_RESERVED_WORDS_UNAVAILABLE")]
    finally:
        if connection is not None:
            close_quietly(connection)


def _build_models(
    selected: SelectedFile,
    options: DdlOptions,
    request: Request,
    *,
    lengths_override: StringLengthMeasurements | None = None,
    effective_length_scan: str | None = None,
    config: AppConfig | None = None,
    reserved_result: tuple[Any, list[dict]] | None = None,
) -> tuple[list[TableModel], int, list[dict], pa.Schema]:
    """Build the shared table plan once for DDL preview or a load job."""
    if options.dialect not in SUPPORTED_DIALECTS:
        raise PBError("E_DIALECT_INVALID", dialect=options.dialect)
    with pq.ParquetFile(selected.path) as parquet:
        schema = parquet.schema_arrow
        source_rows = parquet.metadata.num_rows

    if options.mode == "child" and options.parent_key:
        key_field = next((field for field in schema if field.name == options.parent_key), None)
        if key_field is None or container_kind(key_field.type) is not None:
            raise PBError("E_PARENT_KEY_INVALID", column=options.parent_key)

    scan_mode = effective_length_scan or options.length_scan
    lengths = lengths_override
    if lengths is None:
        lengths = measure_string_lengths(
            selected.path,
            schema,
            strategy=options.string_strategy,
            length_scan=scan_mode,
        )
    key_unique: bool | None = None
    if options.mode == "child" and options.parent_key:
        key_unique = not sample_parent_key_duplicates(selected.path, options.parent_key)

    table_name = options.table_name or default_table_name(selected.display_name, options.dialect)
    reserved_provider, reserved_warnings = reserved_result or _oracle_reserved_provider(request, options.dialect)
    if reserved_provider is not None and table_name.upper() in set(reserved_provider(options.dialect)):
        raise PBError("E_TABLE_NAME_INVALID", dialect=options.dialect, max_length=128)
    models = plan_tables(
        schema,
        table_name=table_name,
        mode=options.mode,
        dialect=options.dialect,
        column_policy=options.column_policy,
        string_strategy=options.string_strategy,
        length_scan=scan_mode,
        lengths=lengths,
        json_check=options.json_check,
        parent_key=options.parent_key if options.mode == "child" else None,
        parent_key_unique=key_unique,
        explode_column=options.explode_column,
        preserve_empty=options.preserve_empty,
        child_max_depth=(config or get_config(request)).load.child_max_depth,
        reserved_word_provider=reserved_provider,
    )
    return models, source_rows, reserved_warnings, schema


def _build_ddl_result(
    selected: SelectedFile,
    options: DdlOptions,
    request: Request,
    *,
    include_preview: bool,
) -> dict:
    if options.preview_offset < 0 or options.preview_limit < 1 or options.preview_limit > 200:
        raise PBError("E_BAD_REQUEST", fields=["preview_offset", "preview_limit"])
    models, source_rows, reserved_warnings, schema = _build_models(selected, options, request)

    ddl = {
        "oracle": lambda: generate_oracle_ddl(models, json_check=options.json_check),
        "mysql": lambda: generate_mysql_ddl(models),
        "postgres": lambda: generate_postgres_ddl(models),
    }[options.dialect]()
    previews: list[dict] = []
    counts: dict[str, dict] = {}
    if include_preview:
        for table in models:
            preview = query_table_preview(
                selected.path,
                table,
                dialect=options.dialect,
                offset=options.preview_offset,
                limit=options.preview_limit,
            )
            previews.append(preview)
            counts[table.name] = preview

    warnings = reserved_warnings + [
        item
        for table in models
        for item in table.warnings
        if _warning_for_dialect(item, options.dialect)
    ]
    if options.mode == "explode" and previews:
        expected = previews[0]["expected_rows"]
        if source_rows > 0 and expected > source_rows * 10:
            warnings.append(warning(
                "W_EXPLODE_ROW_MULTIPLIER",
                column=options.explode_column or "",
                expected_rows=expected,
                multiplier=round(expected / source_rows, 1),
            ))
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in warnings:
        key = (item["code"], repr(sorted((item.get("params") or {}).items())))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    mappings = [
        {
            "table": table.name,
            "original_name": column.original_name,
            "original_path": column.original_path,
            "name": column.name,
            "generated": column.generated,
        }
        for table in models
        for column in table.columns
    ]
    tables = []
    for table in models:
        public_table = _public_table(table, counts, options.dialect)
        public_table["sql_types"] = {
            "oracle": [column.sql_type("oracle") for column in table.columns],
            "mysql": [column.sql_type("mysql") for column in table.columns],
            "postgres": [column.sql_type("postgres") for column in table.columns],
        }
        tables.append(public_table)
    return {
        "dialect": options.dialect,
        "mode": options.mode,
            "table_name": models[0].name,
        "ddl": ddl,
        "tables": tables,
        "column_mappings": mappings,
        "warnings": deduped,
        "previews": previews,
        "select_sql": [
            {
                "table": table.name,
                "sql": build_select_sql(table, dialect=options.dialect),
                "expected_count_sql": build_expected_count_sql(table),
                "parameters": ["parquet_path"],
            }
            for table in models
        ],
    }


@app.post("/api/files/{file_id}/ddl")
def create_ddl_plan(file_id: str, options: DdlOptions, request: Request) -> dict:
    """DDL·이름 대응표·테이블별 펼치기 미리보기를 계산한다."""
    selected = get_files(request).get(file_id)
    scan = _get_or_start_length_scan(file_id, selected, request)
    is_exact = options.length_scan == "full" and scan["status"] == "ready"
    effective_options = options if is_exact or options.length_scan == "sample" else options.model_copy(update={"length_scan": "sample"})
    result = _build_ddl_result(selected, effective_options, request, include_preview=True)
    result["length_measurement"] = "exact" if is_exact else "estimated"
    result["length_scan_status"] = scan
    if options.length_scan == "full" and not is_exact:
        result["warnings"] = [
            item for item in result["warnings"]
            if item["code"] != "W_STRING_LENGTH_SAMPLED"
        ]
        result["warnings"].append(warning("W_STRING_LENGTH_ESTIMATED"))
    return result


@app.post("/api/files/{file_id}/ddl/download")
def download_ddl(file_id: str, options: DdlOptions, request: Request) -> Response:
    """선택한 옵션으로 생성한 SQL을 다운로드한다."""
    selected = get_files(request).get(file_id)
    scan = _get_or_start_length_scan(file_id, selected, request)
    is_exact = options.length_scan == "full" and scan["status"] == "ready"
    effective_options = options if is_exact or options.length_scan == "sample" else options.model_copy(update={"length_scan": "sample"})
    result = _build_ddl_result(selected, effective_options, request, include_preview=False)
    return Response(
        content=result["ddl"],
        media_type="application/sql; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{result["table_name"]}.sql"',
            "X-ParqBridge-Length-Measurement": "exact" if is_exact else "estimated",
            "X-ParqBridge-Length-Scan-Status": scan["status"],
        },
    )


@app.post("/api/files/{file_id}/load")
def queue_load(file_id: str, options: LoadOptions, request: Request) -> dict:
    """Queue CREATE+INSERT for the selected file and planned table family."""
    profile = get_db_profile(request, options.dialect)
    selected = get_files(request).get(file_id)
    config = get_config(request)
    scans = get_string_scans(request)
    reserved_result = _oracle_reserved_provider(request, options.dialect)
    tables, source_rows, reserved_warnings, _schema = _build_models(
        selected,
        options,
        request,
        lengths_override=StringLengthMeasurements(),
        effective_length_scan="full",
        config=config,
        reserved_result=reserved_result,
    )

    def prepare_load(cancel_event, progress):
        lengths = scans.wait(file_id, cancel_event, progress)
        if lengths is None:
            return None
        prepared, prepared_rows, _warnings, _schema = _build_models(
            selected,
            options,
            request,
            lengths_override=lengths,
            effective_length_scan="full",
            config=config,
            reserved_result=reserved_result,
        )
        return prepared, prepared_rows

    job = get_jobs(request).submit(
        path=selected.path,
        tables=tables,
        profile=profile,
        source_rows=source_rows,
        batch_size=get_config(request).load.batch_size,
        existing_policy=options.existing_policy,
        confirm_table_name=options.confirm_table_name,
        prepare=prepare_load,
    )
    return {**job, "warnings": reserved_warnings}


@app.get("/api/jobs/{job_id}")
def get_load_job(job_id: str, request: Request) -> dict:
    """Poll one queued or completed load job."""
    return get_jobs(request).get(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_load_job(job_id: str, request: Request) -> dict:
    """Request cooperative cancellation at the next batch boundary."""
    return get_jobs(request).cancel(job_id)
