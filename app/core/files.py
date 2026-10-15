"""Parquet 파일 선택, 허용 경로 검사, 업로드 저장을 담당한다."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import PBError


@dataclass(frozen=True)
class SelectedFile:
    """서버가 발급한 파일 핸들에 연결된 경로와 표시 정보."""

    file_id: str
    path: Path
    display_name: str
    source: str


class StreamingUpload:
    """multipart parser가 전달하는 조각을 업로드 파일에 바로 기록한다."""

    def __init__(self, registry: FileRegistry, filename: str | None, max_bytes: int) -> None:
        self.registry = registry
        self.file_id = uuid.uuid4().hex
        self.path = registry.upload_dir / f"{self.file_id}.parquet"
        self.display_name = _display_name(filename)
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self._output = self.path.open("xb")
        self._finished = False

    def write(self, chunk: bytes) -> None:
        """크기를 먼저 확인한 뒤 받은 조각만 디스크에 기록한다."""
        if self.total_bytes + len(chunk) > self.max_bytes:
            raise PBError("E_UPLOAD_TOO_LARGE", max_bytes=self.max_bytes)
        self._output.write(chunk)
        self.total_bytes += len(chunk)

    def finish(self) -> SelectedFile:
        """파일 핸들을 닫고 Parquet 매직을 확인한 뒤 file_id를 등록한다."""
        self._output.close()
        try:
            validate_parquet_magic(self.path)
        except BaseException:
            self.abort()
            raise
        selected = SelectedFile(self.file_id, self.path, self.display_name, "upload")
        self.registry._files[self.file_id] = selected
        self._finished = True
        return selected

    def abort(self) -> None:
        """미완성 업로드 핸들을 닫고 부분 파일을 제거한다."""
        if not self._output.closed:
            self._output.close()
        if not self._finished:
            self.path.unlink(missing_ok=True)


class FileRegistry:
    """클라이언트에 실제 경로 대신 file_id를 제공한다."""

    def __init__(self, upload_dir: Path, allowed_roots: tuple[Path, ...] = ()) -> None:
        requested = upload_dir.absolute()
        for candidate in (requested, *requested.parents):
            is_junction = getattr(candidate, "is_junction", lambda: False)
            if candidate.is_symlink() or is_junction():
                raise PBError("E_PATH_NOT_ALLOWED")
        requested.mkdir(parents=True, exist_ok=True)
        self.upload_dir = requested.resolve(strict=True)
        self.allowed_roots = allowed_roots
        self._files: dict[str, SelectedFile] = {}

    def get(self, file_id: str) -> SelectedFile:
        selected = self._files.get(file_id)
        if selected is None:
            raise PBError("E_FILE_NOT_FOUND")
        if selected.source == "local":
            current = resolve_allowed_path(selected.path, self.allowed_roots)
            if current != selected.path or not current.is_file():
                raise PBError("E_PATH_NOT_ALLOWED")
        else:
            try:
                resolved_parent = selected.path.parent.resolve(strict=True)
                resolved_path = selected.path.resolve(strict=True)
            except OSError as exc:
                raise PBError("E_FILE_NOT_FOUND") from exc
            if (
                resolved_parent != self.upload_dir
                or not resolved_path.is_relative_to(self.upload_dir)
                or selected.path.is_symlink()
            ):
                raise PBError("E_PATH_NOT_ALLOWED")
        return selected

    def select_local(self, raw_path: str, allowed_roots: tuple[Path, ...]) -> SelectedFile:
        path = resolve_allowed_path(raw_path, allowed_roots)
        if not path.is_file() or path.suffix.lower() != ".parquet":
            raise PBError("E_PARQUET_INVALID")
        validate_parquet_magic(path)
        selected = SelectedFile(uuid.uuid4().hex, path, path.name, "local")
        self._files[selected.file_id] = selected
        return selected

    def begin_upload(self, filename: str | None, max_bytes: int) -> StreamingUpload:
        """검증된 multipart 파일 파트를 위한 안전한 임시 업로드를 연다."""
        return StreamingUpload(self, filename, max_bytes)

    def delete_upload(self, file_id: str) -> None:
        selected = self.get(file_id)
        if selected.source != "upload":
            raise PBError("E_PATH_NOT_ALLOWED")
        try:
            resolved_parent = selected.path.parent.resolve(strict=True)
            resolved_path = selected.path.resolve(strict=True)
        except OSError as exc:
            raise PBError("E_FILE_NOT_FOUND") from exc
        if (
            resolved_parent != self.upload_dir
            or not resolved_path.is_relative_to(self.upload_dir)
            or selected.path.is_symlink()
        ):
            raise PBError("E_PATH_NOT_ALLOWED")
        selected.path.unlink()
        del self._files[file_id]


def resolve_allowed_path(raw_path: str | os.PathLike[str], allowed_roots: tuple[Path, ...]) -> Path:
    """strict resolve 후 허용 루트 안에 있는지 확인한다."""
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
        roots = tuple(root.resolve(strict=True) for root in allowed_roots)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise PBError("E_PATH_NOT_ALLOWED") from exc
    if not any(path.is_relative_to(root) for root in roots):
        raise PBError("E_PATH_NOT_ALLOWED")
    return path


def list_allowed_directory(raw_path: str, allowed_roots: tuple[Path, ...]) -> dict:
    """허용 루트 안의 하위 폴더와 Parquet 파일만 반환한다."""
    path = resolve_allowed_path(raw_path, allowed_roots)
    if not path.is_dir():
        raise PBError("E_PATH_NOT_ALLOWED")
    root = next(root.resolve(strict=True) for root in allowed_roots if path.is_relative_to(root.resolve(strict=True)))
    entries: list[dict] = []
    try:
        children = sorted(path.iterdir(), key=lambda child: child.name.casefold())
    except OSError as exc:
        raise PBError("E_PATH_NOT_ALLOWED") from exc
    for child in children:
        try:
            resolved = child.resolve(strict=True)
            if not resolved.is_relative_to(root):
                continue
            if resolved.is_dir():
                entries.append({"name": child.name, "path": str(resolved), "kind": "directory", "size": None})
            elif resolved.is_file() and child.suffix.lower() == ".parquet":
                entries.append({"name": child.name, "path": str(resolved), "kind": "file", "size": resolved.stat().st_size})
        except (OSError, RuntimeError):
            # 사라진 항목이나 루트 밖을 가리키는 링크는 목록에서 제외한다.
            continue
    parent = str(path.parent) if path != root and path.is_relative_to(root) else None
    return {"current": str(path), "parent": parent, "entries": entries}


def validate_parquet_magic(path: Path) -> None:
    """파일 시작과 푸터의 PAR1 매직을 확인한다."""
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            if size < 8:
                raise PBError("E_PARQUET_INVALID")
            source.seek(0)
            head = source.read(4)
            source.seek(-4, 2)
            tail = source.read(4)
    except OSError as exc:
        raise PBError("E_PARQUET_INVALID") from exc
    if head != b"PAR1" or tail != b"PAR1":
        raise PBError("E_PARQUET_INVALID")


def _display_name(filename: str | None) -> str:
    original = Path((filename or "parquet.parquet").replace("\\", "/")).name
    return original or "parquet.parquet"
