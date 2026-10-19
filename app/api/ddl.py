"""DDL preview and download routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.api.deps import get_files, get_or_start_length_scan
from app.api.planning import build_ddl_result
from app.api.schemas import DdlOptions
from app.core.errors import warning

router = APIRouter()


@router.post("/api/files/{file_id}/ddl")
def create_ddl_plan(file_id: str, options: DdlOptions, request: Request) -> dict:
    """Build DDL, a name-mapping table, and per-table flattening previews."""
    selected = get_files(request).get(file_id)
    scan = get_or_start_length_scan(file_id, selected, request)
    is_exact = options.length_scan == "full" and scan["status"] == "ready"
    effective_options = options if is_exact or options.length_scan == "sample" else options.model_copy(update={"length_scan": "sample"})
    result = build_ddl_result(selected, effective_options, request, include_preview=True)
    result["length_measurement"] = "exact" if is_exact else "estimated"
    result["length_scan_status"] = scan
    if options.length_scan == "full" and not is_exact:
        result["warnings"] = [
            item for item in result["warnings"]
            if item["code"] != "W_STRING_LENGTH_SAMPLED"
        ]
        result["warnings"].append(warning("W_STRING_LENGTH_ESTIMATED"))
    return result


@router.post("/api/files/{file_id}/ddl/download")
def download_ddl(file_id: str, options: DdlOptions, request: Request) -> Response:
    """Download SQL generated with the selected options."""
    selected = get_files(request).get(file_id)
    scan = get_or_start_length_scan(file_id, selected, request)
    is_exact = options.length_scan == "full" and scan["status"] == "ready"
    effective_options = options if is_exact or options.length_scan == "sample" else options.model_copy(update={"length_scan": "sample"})
    result = build_ddl_result(selected, effective_options, request, include_preview=False)
    return Response(
        content=result["ddl"],
        media_type="application/sql; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{result["table_name"]}.sql"',
            "X-ParqBridge-Length-Measurement": "exact" if is_exact else "estimated",
            "X-ParqBridge-Length-Scan-Status": scan["status"],
        },
    )
