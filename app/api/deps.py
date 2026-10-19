"""Accessors for the per-app state that route handlers share."""

from __future__ import annotations

from fastapi import Request

from app.core.config import AppConfig, DbProfile
from app.core.db import require_online
from app.core.dialects import SUPPORTED_DIALECTS
from app.core.errors import PBError
from app.core.files import FileRegistry, SelectedFile
from app.core.jobs import LoadJobManager
from app.core.string_scans import StringScanManager
from app.core.updates import UpdateChecker


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_files(request: Request) -> FileRegistry:
    return request.app.state.files


def get_jobs(request: Request) -> LoadJobManager:
    return request.app.state.jobs


def get_string_scans(request: Request) -> StringScanManager:
    return request.app.state.string_scans


def get_updates(request: Request) -> UpdateChecker:
    return request.app.state.updates


def get_or_start_length_scan(file_id: str, selected: SelectedFile, request: Request) -> dict:
    scans = get_string_scans(request)
    try:
        return scans.state(file_id)
    except PBError as exc:
        if exc.code != "E_FILE_NOT_FOUND":
            raise
        return scans.start(file_id, selected.path)


def get_db_profile(request: Request, dialect: str) -> DbProfile:
    if dialect not in SUPPORTED_DIALECTS:
        raise PBError("E_DIALECT_INVALID", dialect=dialect)
    profile = get_config(request).db.get(dialect)
    if profile is None:
        raise PBError("E_DB_NOT_CONFIGURED")
    require_online(profile)
    return profile
