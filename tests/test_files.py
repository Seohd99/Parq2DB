from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.main import app


def configured(tmp_path: Path, roots: tuple[Path, ...] = (), upload_max_mb: int = 2048):
    base = load_config(tmp_path / "missing.toml", tmp_path / "missing.local.toml", env={})
    server = replace(
        base.server,
        upload_dir=tmp_path / "uploads",
        allowed_roots=roots,
        upload_max_mb=upload_max_mb,
    )
    return replace(base, server=server)


def error_code(response) -> str:
    return response.json()["error"]["code"]


def test_upload_stream_saves_safe_name_and_delete_stays_in_upload_dir(tmp_path, parquet_path):
    app.state.config = configured(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/files/upload",
            files={"file": ("../../unsafe.parquet", parquet_path.read_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 200
        selected = response.json()["file"]
        assert selected["display_name"] == "unsafe.parquet"
        assert selected["source"] == "upload"
        assert len(selected["file_id"]) == 32
        files = list((tmp_path / "uploads").iterdir())
        assert len(files) == 1
        assert files[0].name == f"{selected['file_id']}.parquet"
        assert files[0].name != selected["display_name"]

        removed = client.delete(f"/api/files/{selected['file_id']}")
        assert removed.status_code == 200
        assert removed.json() == {"deleted": True}
        assert list((tmp_path / "uploads").iterdir()) == []


def test_upload_rejects_invalid_magic_and_removes_partial_file(tmp_path):
    app.state.config = configured(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/files/upload",
            files={"file": ("not-parquet.parquet", b"not a parquet file", "application/octet-stream")},
        )
        assert response.status_code == 422
        assert error_code(response) == "E_PARQUET_INVALID"
        assert list((tmp_path / "uploads").iterdir()) == []


def test_upload_rejects_size_over_limit_and_removes_partial_file(tmp_path):
    app.state.config = configured(tmp_path, upload_max_mb=1)
    payload = b"P" * (1024 * 1024 + 1)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/files/upload",
            files={"file": ("large.parquet", payload, "application/octet-stream")},
        )
        assert response.status_code == 413
        assert error_code(response) == "E_UPLOAD_TOO_LARGE"
        assert list((tmp_path / "uploads").iterdir()) == []


def test_upload_rejects_large_content_length_before_reading_body(tmp_path):
    app.state.config = configured(tmp_path, upload_max_mb=1)
    yielded = False

    def unread_body():
        nonlocal yielded
        yielded = True
        raise AssertionError("request body should not be consumed")
        yield b""

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/files/upload",
            content=unread_body(),
            headers={
                "content-type": "multipart/form-data; boundary=unused",
                "content-length": str(1024 * 1024 + 65537),
            },
        )
        assert response.status_code == 413
        assert error_code(response) == "E_UPLOAD_TOO_LARGE"
        assert yielded is False
        assert list((tmp_path / "uploads").iterdir()) == []


def test_upload_rejects_streamed_body_without_content_length_and_removes_partial(tmp_path):
    app.state.config = configured(tmp_path, upload_max_mb=1)
    boundary = "pb-streaming-test"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="large.parquet"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    file_size = 1024 * 1024 + 1
    file_data = b"PAR1" + b"x" * (file_size - 8) + b"PAR1"
    suffix = f"\r\n--{boundary}--\r\n".encode()

    def body_chunks():
        yield prefix
        yield file_data
        yield suffix

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/files/upload",
            content=body_chunks(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert response.status_code == 413
        assert error_code(response) == "E_UPLOAD_TOO_LARGE"
        assert list((tmp_path / "uploads").iterdir()) == []


def test_upload_requires_exactly_one_file_part_named_file(tmp_path, parquet_path):
    app.state.config = configured(tmp_path)
    payload = parquet_path.read_bytes()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        missing = client.post(
            "/api/files/upload",
            content=b"--pb-empty--\r\n",
            headers={"content-type": "multipart/form-data; boundary=pb-empty"},
        )
        assert missing.status_code == 400
        assert error_code(missing) == "E_BAD_REQUEST"

        wrong_field = client.post(
            "/api/files/upload",
            files={"payload": ("wrong-field.parquet", payload, "application/octet-stream")},
        )
        assert wrong_field.status_code == 400
        assert error_code(wrong_field) == "E_BAD_REQUEST"

        multiple = client.post(
            "/api/files/upload",
            files=[
                ("file", ("one.parquet", payload, "application/octet-stream")),
                ("file", ("two.parquet", payload, "application/octet-stream")),
            ],
        )
        assert multiple.status_code == 400
        assert error_code(multiple) == "E_BAD_REQUEST"
        assert list((tmp_path / "uploads").iterdir()) == []


def test_uploaded_file_can_be_inspected_previewed_and_deleted(tmp_path, parquet_path):
    app.state.config = configured(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        uploaded = client.post(
            "/api/files/upload",
            files={"file": ("inspection.parquet", parquet_path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        selected = uploaded.json()["file"]
        path = tmp_path / "uploads" / f"{selected['file_id']}.parquet"
        assert path.exists()

        inspected = client.get(f"/api/files/{selected['file_id']}/inspect")
        assert inspected.status_code == 200
        assert inspected.json()["metadata"]["num_rows"] == 5
        preview = client.get(f"/api/files/{selected['file_id']}/preview", params={"offset": 0, "limit": 2})
        assert preview.status_code == 200
        assert len(preview.json()["rows"]) == 2

        deleted = client.delete(f"/api/files/{selected['file_id']}")
        assert deleted.status_code == 200
        assert not path.exists()


def test_local_path_and_browse_reject_escape_and_allow_in_root_file(tmp_path, parquet_path):
    root = tmp_path / "allowed"
    root.mkdir()
    local_file = root / "local.parquet"
    local_file.write_bytes(parquet_path.read_bytes())
    nested_dir = root / "nested"
    nested_dir.mkdir()
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(parquet_path.read_bytes())
    app.state.config = configured(tmp_path, (root,))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        roots = client.get("/api/files/browse")
        assert roots.json()["roots"] == [str(root.resolve())]
        listing = client.get("/api/files/browse", params={"path": str(root)}).json()
        entries = {entry["name"]: entry for entry in listing["entries"]}
        assert set(entries) == {"local.parquet", "nested"}
        assert entries["local.parquet"]["kind"] == "file"
        assert entries["local.parquet"]["size"] == local_file.stat().st_size
        assert entries["nested"]["kind"] == "directory"

        selected = client.post("/api/files/local", json={"path": str(local_file)})
        assert selected.status_code == 200
        assert selected.json()["file"]["source"] == "local"

        traversal = client.post("/api/files/local", json={"path": str(root / ".." / "outside.parquet")})
        assert traversal.status_code == 403
        assert error_code(traversal) == "E_PATH_NOT_ALLOWED"
        absolute_escape = client.get("/api/files/browse", params={"path": str(tmp_path)})
        assert absolute_escape.status_code == 403
        assert error_code(absolute_escape) == "E_PATH_NOT_ALLOWED"


def test_symlink_escape_is_rejected_when_symlinks_are_available(tmp_path, parquet_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(parquet_path.read_bytes())
    link = root / "escape.parquet"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created in this environment")

    app.state.config = configured(tmp_path, (root,))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/files/local", json={"path": str(link)})
        assert response.status_code == 403
        assert error_code(response) == "E_PATH_NOT_ALLOWED"
        listing = client.get("/api/files/browse", params={"path": str(root)}).json()
        assert listing["entries"] == []


def test_local_inspection_and_preview_do_not_change_source(tmp_path, parquet_path):
    root = tmp_path / "allowed"
    root.mkdir()
    local_file = root / "source.parquet"
    local_file.write_bytes(parquet_path.read_bytes())
    before = local_file.stat()
    digest_before = hashlib.sha256(local_file.read_bytes()).hexdigest()
    app.state.config = configured(tmp_path, (root,))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        selected = client.post("/api/files/local", json={"path": str(local_file)}).json()["file"]
        inspect = client.get(f"/api/files/{selected['file_id']}/inspect")
        assert inspect.status_code == 200
        assert inspect.json()["metadata"]["num_rows"] == 5
        preview = client.get(f"/api/files/{selected['file_id']}/preview", params={"offset": 3, "limit": 2})
        assert preview.status_code == 200
        assert [row["id"] for row in preview.json()["rows"]] == [4, 5]
        forbidden_delete = client.delete(f"/api/files/{selected['file_id']}")
        assert forbidden_delete.status_code == 403
        assert error_code(forbidden_delete) == "E_PATH_NOT_ALLOWED"

    after = local_file.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size == before.st_size
    assert hashlib.sha256(local_file.read_bytes()).hexdigest() == digest_before

