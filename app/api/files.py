"""File selection, upload, inspection, and preview routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import get_config, get_files, get_string_scans
from app.api.schemas import LocalFileSelection
from app.api.uploads import read_streaming_upload
from app.core.dialects import SUPPORTED_DIALECTS
from app.core.errors import PBError
from app.core.files import SelectedFile, list_allowed_directory
from app.core.flatten import default_table_name
from app.core.inspect import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, inspect_parquet, preview_parquet

router = APIRouter()


def _selected_file_response(selected: SelectedFile, scan: dict) -> dict:
    return {
        "file": {
            "file_id": selected.file_id,
            "display_name": selected.display_name,
            "default_table_names": {
                dialect: default_table_name(selected.display_name, dialect) for dialect in SUPPORTED_DIALECTS
            },
            "source": selected.source,
            "size": selected.path.stat().st_size,
            "length_scan": scan,
        }
    }


@router.get("/api/files/browse")
def browse_files(request: Request, path: str | None = None) -> dict:
    """List only folders and Parquet files under allowed roots."""
    roots = get_config(request).server.allowed_roots
    if path is None:
        return {"roots": [str(root) for root in roots], "current": None, "parent": None, "entries": []}
    result = list_allowed_directory(path, roots)
    return {"roots": [str(root) for root in roots], **result}


@router.post("/api/files/local")
def select_local_file(body: LocalFileSelection, request: Request) -> dict:
    """Register an allowed local Parquet path as a read-only file_id."""
    selected = get_files(request).select_local(body.path, get_config(request).server.allowed_roots)
    scan = get_string_scans(request).start(selected.file_id, selected.path)
    return _selected_file_response(selected, scan)


@router.post("/api/files/upload")
async def upload_file(request: Request) -> dict:
    """Stream the multipart body while checking the file size and Parquet magic bytes."""
    selected = await read_streaming_upload(
        request,
        get_files(request),
        get_config(request).server.upload_max_bytes,
    )
    scan = get_string_scans(request).start(selected.file_id, selected.path)
    return _selected_file_response(selected, scan)


@router.delete("/api/files/{file_id}")
def delete_file(file_id: str, request: Request) -> dict:
    """Delete only files registered in the upload store."""
    selected = get_files(request).get(file_id)
    if selected.source != "upload":
        raise PBError("E_PATH_NOT_ALLOWED")
    get_string_scans(request).release(file_id, wait=True)
    get_files(request).delete_upload(file_id)
    return {"deleted": True}


@router.post("/api/files/{file_id}/release")
def release_file_selection(file_id: str, request: Request) -> dict:
    """Cancel an unused selection's scan while leaving its source file untouched."""
    get_files(request).get(file_id)
    get_string_scans(request).release(file_id)
    return {"released": True}


@router.get("/api/files/{file_id}/length-scan")
def get_file_length_scan(file_id: str, request: Request) -> dict:
    """Return the current full string-length scan state for one file handle."""
    get_files(request).get(file_id)
    return get_string_scans(request).state(file_id)


@router.get("/api/files/{file_id}/inspect")
def inspect_file(file_id: str, request: Request) -> dict:
    selected = get_files(request).get(file_id)
    return inspect_parquet(selected.path)


@router.get("/api/files/{file_id}/preview")
def preview_file(
    file_id: str,
    request: Request,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """Read only the requested page from DuckDB for the preview."""
    if limit > MAX_PAGE_SIZE:
        raise PBError("E_BAD_REQUEST", fields=["limit"])
    selected = get_files(request).get(file_id)
    return preview_parquet(selected.path, offset=offset, limit=limit)
