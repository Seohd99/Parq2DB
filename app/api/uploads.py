"""Stream a multipart upload to the upload store without buffering the whole body."""

from __future__ import annotations

from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.multipart import FormParserError, MultipartParseError, parse_options_header

from app.core.errors import PBError
from app.core.files import FileRegistry, SelectedFile, StreamingUpload

MULTIPART_OVERHEAD_BYTES = 64 * 1024


async def read_streaming_upload(request: Request, registry: FileRegistry, max_bytes: int) -> SelectedFile:
    """Parse the multipart body in chunks and write only the file part to the upload."""
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
