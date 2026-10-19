"""Load, INSERT-into-existing, and job polling routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import get_config, get_db_profile, get_files, get_jobs, get_string_scans
from app.api.planning import build_models, oracle_reserved_provider
from app.api.schemas import InsertOptions, LoadOptions
from app.core.errors import PBError
from app.core.insert_target import inspect_insert_target
from app.core.model import StringLengthMeasurements

router = APIRouter()


@router.post("/api/files/{file_id}/load")
def queue_load(file_id: str, options: LoadOptions, request: Request) -> dict:
    """Queue CREATE+INSERT for the selected file and planned table family."""
    if options.existing_policy == "insert_only":
        # INSERT into an existing table goes through /insert, which matches stored columns.
        raise PBError("E_BAD_REQUEST", fields=["existing_policy"])
    profile = get_db_profile(request, options.dialect)
    selected = get_files(request).get(file_id)
    config = get_config(request)
    scans = get_string_scans(request)
    reserved_result = oracle_reserved_provider(request, options.dialect)
    tables, source_rows, reserved_warnings, _schema = build_models(
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
        prepared, prepared_rows, _warnings, _schema = build_models(
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
        batch_size=config.load.batch_size,
        existing_policy=options.existing_policy,
        confirm_table_name=options.confirm_table_name,
        prepare=prepare_load,
    )
    return {**job, "warnings": reserved_warnings}


@router.post("/api/files/{file_id}/insert/check")
def check_insert_target(file_id: str, options: InsertOptions, request: Request) -> dict:
    """Read an existing table and show how the file's columns match its columns."""
    profile = get_db_profile(request, options.dialect)
    selected = get_files(request).get(file_id)
    _model, report, _rows = inspect_insert_target(selected.path, profile, options.table_name, options.columns)
    return report


@router.post("/api/files/{file_id}/insert")
def queue_insert(file_id: str, options: InsertOptions, request: Request) -> dict:
    """Queue INSERT into an existing table without DDL or a string-length scan."""
    profile = get_db_profile(request, options.dialect)
    selected = get_files(request).get(file_id)
    model, report, source_rows = inspect_insert_target(selected.path, profile, options.table_name, options.columns)
    if model is None:
        raise PBError("E_INSERT_NO_MATCHING_COLUMNS")
    job = get_jobs(request).submit(
        path=selected.path,
        tables=[model],
        profile=profile,
        source_rows=source_rows,
        batch_size=get_config(request).load.batch_size,
        existing_policy="insert_only",
        confirm_table_name=None,
    )
    return {**job, "warnings": report["warnings"]}


@router.get("/api/jobs/{job_id}")
def get_load_job(job_id: str, request: Request) -> dict:
    """Poll one queued or completed load job."""
    return get_jobs(request).get(job_id)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_load_job(job_id: str, request: Request) -> dict:
    """Request cooperative cancellation at the next batch boundary."""
    return get_jobs(request).cancel(job_id)
