"""FastAPI app: middleware, error handlers, static files, and the API routers.

Run with:
  python -m app                 # Uses host and port from config (default 127.0.0.1:8765)
  uvicorn app.main:app          # Uses Uvicorn's default 127.0.0.1:8000

Routes live in app/api/: system (status and database checks), files (selection,
upload, inspection, preview), ddl (DDL preview and download), and loads (load jobs,
INSERT into existing tables, job polling).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import ddl, files, loads, system
from app.core.config import AppConfig, load_config
from app.core.errors import PBError
from app.core.files import FileRegistry
from app.core.hosts import allowed_host_names, request_rejection
from app.core.jobs import LoadJobManager
from app.core.string_scans import StringScanManager

log = logging.getLogger("parqbridge")

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
I18N_DIR = APP_DIR / "i18n"


def _config_from_env() -> AppConfig:
    # Set PARQBRIDGE_CONFIG to use a different config file, such as in tests.
    p = os.environ.get("PARQBRIDGE_CONFIG")
    config = load_config(Path(p) if p else None)
    bind_host = os.environ.get("PARQBRIDGE_BIND_HOST")
    if bind_host:
        config = replace(config, server=replace(config.server, host=bind_host))
    return config


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "config"):
        app.state.config = _config_from_env()
    app.state.allowed_hosts = allowed_host_names(app.state.config.server.host, app.state.config.server.allowed_hosts)
    app.state.files = FileRegistry(app.state.config.server.upload_dir, app.state.config.server.allowed_roots)
    app.state.jobs = LoadJobManager()
    app.state.string_scans = StringScanManager(max_workers=2)
    try:
        yield
    finally:
        app.state.jobs.shutdown()
        app.state.string_scans.shutdown()


app = FastAPI(title="ParqBridge", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def reject_foreign_hosts(request: Request, call_next):
    # Refuse DNS-rebinding and cross-site requests before any route or static file is served.
    code = request_rejection(
        request.method,
        request.headers.get("host"),
        request.headers.get("origin"),
        request.app.state.allowed_hosts,
        scheme=request.url.scheme,
    )
    if code is not None:
        return JSONResponse(status_code=403, content={"error": {"code": code, "params": {}}})
    return await call_next(request)


@app.middleware("http")
async def no_cache_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path in {"/static", "/i18n"} or path.startswith(("/static/", "/i18n/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/i18n", StaticFiles(directory=I18N_DIR), name="i18n")

for module in (system, files, ddl, loads):
    app.include_router(module.router)
