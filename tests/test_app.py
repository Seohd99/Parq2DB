import threading
import time

from fastapi.testclient import TestClient
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.config import AppConfig, DbProfile, ServerConfig, load_config
from app.core.errors import PBError
from app.core.languages import supported_languages
from app.core.model import StringLength, StringLengthMeasurements
from app.core.string_scans import StringScanManager
from app.main import DdlOptions, LoadOptions, app


def client_with(cfg) -> TestClient:
    app.state.config = cfg
    return TestClient(app)


def test_index_and_static(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        r = c.get("/")
        app_js = c.get("/static/app.js")
        en_catalog = c.get("/i18n/en.json")
        assert r.status_code == 200
        assert "ParqBridge" in r.text
        assert 'id="ddl-length-scan"' in r.text
        assert app_js.status_code == 200
        assert c.get("/i18n/ko.json").status_code == 200
        assert en_catalog.status_code == 200
        for response in (r, app_js, en_catalog):
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers.get("etag")


def test_status_offline(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        body = c.get("/api/status").json()
    assert body["db"]["oracle"] == {
        "kind": "oracle", "online": False, "reason": {"code": "E_DB_NOT_CONFIGURED", "params": {}},
    }
    assert body["db"]["mysql"]["online"] is False
    assert body["languages"] == supported_languages()


def test_length_scan_defaults_to_full_and_is_shared_by_load_options():
    assert DdlOptions().length_scan == "full"
    assert LoadOptions(length_scan="sample").length_scan == "sample"


def test_string_scan_manager_joins_duplicate_files_and_discards_cancelled_result(monkeypatch, parquet_path):
    started = threading.Event()
    cancelled = threading.Event()
    cancel_event_seen = []

    def blocked_scan(_path, _schema, **kwargs):
        cancel_event_seen.append(kwargs["cancel_event"])
        started.set()
        while not kwargs["cancel_event"].wait(0.01):
            pass
        cancelled.set()
        raise PBError("E_STRING_SCAN_CANCELLED")

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", blocked_scan)
    manager = StringScanManager(max_workers=1)
    try:
        first = manager.start("first-handle", parquet_path)
        assert first["status"] in {"pending", "running"}
        assert started.wait(1)
        second = manager.start("second-handle", parquet_path)
        assert manager.state("first-handle")["status"] == "running"
        assert manager.state("second-handle")["status"] == "running"
        assert len(cancel_event_seen) == 1

        manager.release("first-handle")
        assert not cancel_event_seen[0].is_set()
        manager.release("second-handle", wait=True)
        assert cancelled.wait(1)
        with pytest.raises(PBError) as caught:
            manager.state("second-handle")
        assert caught.value.code == "E_FILE_NOT_FOUND"
    finally:
        manager.shutdown()


def test_released_selection_status_does_not_restart_scan(tmp_path, parquet_path, monkeypatch):
    started = threading.Event()
    cancelled = threading.Event()
    calls = []

    def blocked_scan(_path, _schema, **kwargs):
        calls.append(kwargs["cancel_event"])
        started.set()
        kwargs["cancel_event"].wait(2)
        cancelled.set()
        raise PBError("E_STRING_SCAN_CANCELLED")

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", blocked_scan)
    cfg = AppConfig(server=ServerConfig(upload_dir=tmp_path / "uploads"))
    with client_with(cfg) as c:
        uploaded = c.post(
            "/api/files/upload",
            files={"file": ("released.parquet", parquet_path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file"]["file_id"]
        assert started.wait(1)
        released = c.post(f"/api/files/{file_id}/release")
        assert released.status_code == 200
        assert cancelled.wait(1)
        status = c.get(f"/api/files/{file_id}/length-scan")
        assert status.status_code == 404
        assert status.json()["error"]["code"] == "E_FILE_NOT_FOUND"
        assert len(calls) == 1
        c.delete(f"/api/files/{file_id}")


def test_ddl_uses_estimate_then_exact_after_background_scan(tmp_path, monkeypatch):
    from app.core import typemap

    estimate_path = tmp_path / "estimate-scan.parquet"
    pq.write_table(pa.table({"comment": ["x"] * 2000}), estimate_path, row_group_size=100)
    started = threading.Event()
    release = threading.Event()
    real_measure = typemap.measure_string_lengths

    def delayed_measure(path, schema, **kwargs):
        started.set()
        assert release.wait(3)
        return real_measure(path, schema, **kwargs)

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", delayed_measure)
    cfg = AppConfig(server=ServerConfig(upload_dir=tmp_path / "uploads"))
    with client_with(cfg) as c:
        uploaded = c.post(
            "/api/files/upload",
            files={"file": ("scan.parquet", estimate_path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file"]["file_id"]
        try:
            assert started.wait(1)
            scan = c.get(f"/api/files/{file_id}/length-scan")
            assert scan.status_code == 200
            assert scan.json()["status"] == "running"

            estimated = c.post(
                f"/api/files/{file_id}/ddl",
                json={"dialect": "oracle", "mode": "none", "table_name": "PB_TEST_SCAN"},
            )
            assert estimated.status_code == 200, estimated.text
            assert estimated.json()["length_measurement"] == "estimated"
            assert estimated.json()["length_scan_status"]["status"] == "running"
            length_warnings = {
                item["code"] for item in estimated.json()["warnings"]
                if item["code"].startswith("W_STRING_LENGTH_")
            }
            assert length_warnings == {"W_STRING_LENGTH_ESTIMATED"}

            sampled = c.post(
                f"/api/files/{file_id}/ddl",
                json={
                    "dialect": "oracle",
                    "mode": "none",
                    "table_name": "PB_TEST_SCAN_SAMPLE",
                    "length_scan": "sample",
                },
            )
            assert sampled.status_code == 200, sampled.text
            length_warnings = {
                item["code"] for item in sampled.json()["warnings"]
                if item["code"].startswith("W_STRING_LENGTH_")
            }
            assert length_warnings == {"W_STRING_LENGTH_SAMPLED"}

            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                scan = c.get(f"/api/files/{file_id}/length-scan").json()
                if scan["status"] == "ready":
                    break
                time.sleep(0.01)
            assert scan["status"] == "ready"
            exact = c.post(
                f"/api/files/{file_id}/ddl",
                json={"dialect": "oracle", "mode": "none", "table_name": "PB_TEST_SCAN"},
            )
            assert exact.status_code == 200, exact.text
            assert exact.json()["length_measurement"] == "exact"
            assert exact.json()["length_scan_status"]["status"] == "ready"
        finally:
            release.set()
            c.delete(f"/api/files/{file_id}")


def test_load_waits_for_full_scan_and_builds_exact_model(tmp_path, monkeypatch):
    from app.core import jobs, typemap

    path = tmp_path / "load-scan.parquet"
    pq.write_table(pa.table({"comment": ["x", "y" * 500]}), path, row_group_size=1)
    scan_started = threading.Event()
    release_scan = threading.Event()
    real_measure = typemap.measure_string_lengths
    captured_types = []
    profile = DbProfile(
        kind="mysql",
        host="db.invalid",
        port=3306,
        target="unit_test",
        user="unit_test_user",
        password="UNIT_TEST_ONLY_SECRET",
    )
    cfg = AppConfig(
        server=ServerConfig(upload_dir=tmp_path / "uploads"),
        db={"mysql": profile},
    )

    def delayed_measure(path, schema, **kwargs):
        scan_started.set()
        assert release_scan.wait(3)
        return real_measure(path, schema, **kwargs)

    def fake_execute_load(**kwargs):
        captured_types.append(kwargs["tables"][0].columns[0].sql_type_mysql)
        return {"tables": [], "committed_batches": 0, "committed_rows": 0}

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", delayed_measure)
    monkeypatch.setattr(jobs, "execute_load", fake_execute_load)
    with client_with(cfg) as c:
        uploaded = c.post(
            "/api/files/upload",
            files={"file": ("load-scan.parquet", path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file"]["file_id"]
        try:
            assert scan_started.wait(1)
            response = c.post(
                f"/api/files/{file_id}/load",
                json={"dialect": "mysql", "table_name": "PB_TEST_SCAN_LOAD"},
            )
            assert response.status_code == 200, response.text
            job_id = response.json()["job_id"]
            deadline = time.monotonic() + 2
            job = response.json()
            while time.monotonic() < deadline and job["stage"] != "measuring":
                job = c.get(f"/api/jobs/{job_id}").json()
                time.sleep(0.01)
            assert job["stage"] == "measuring"
            assert captured_types == []

            release_scan.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = c.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert job["status"] == "succeeded", job
            assert job["stage"] == "loading"
            assert captured_types == ["VARCHAR(500)"]
        finally:
            release_scan.set()
            c.delete(f"/api/files/{file_id}")


def test_load_reports_background_string_scan_failure(tmp_path, parquet_path, monkeypatch):
    from app.core.errors import PBError

    profile = DbProfile(
        kind="mysql",
        host="db.invalid",
        port=3306,
        target="unit_test",
        user="unit_test_user",
        password="UNIT_TEST_ONLY_SECRET",
    )
    cfg = AppConfig(
        server=ServerConfig(upload_dir=tmp_path / "uploads"),
        db={"mysql": profile},
    )

    def fail_scan(_path, _schema, **_kwargs):
        raise PBError("E_STRING_SCAN_FAILED")

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", fail_scan)
    with client_with(cfg) as c:
        uploaded = c.post(
            "/api/files/upload",
            files={"file": ("failed-scan.parquet", parquet_path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file"]["file_id"]
        try:
            deadline = time.monotonic() + 2
            scan = c.get(f"/api/files/{file_id}/length-scan").json()
            while time.monotonic() < deadline and scan["status"] not in {"failed", "ready"}:
                time.sleep(0.01)
                scan = c.get(f"/api/files/{file_id}/length-scan").json()
            assert scan["status"] == "failed"
            response = c.post(
                f"/api/files/{file_id}/load",
                json={"dialect": "mysql", "table_name": "PB_TEST_SCAN_FAILED"},
            )
            assert response.status_code == 200, response.text
            job_id = response.json()["job_id"]
            deadline = time.monotonic() + 2
            job = c.get(f"/api/jobs/{job_id}").json()
            while time.monotonic() < deadline and job["status"] in {"queued", "running"}:
                time.sleep(0.01)
                job = c.get(f"/api/jobs/{job_id}").json()
            assert job["status"] == "failed"
            assert job["error"]["code"] == "E_STRING_SCAN_FAILED"
        finally:
            c.delete(f"/api/files/{file_id}")


def test_database_apis_reject_offline_profiles_with_409(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        test_response = c.post("/api/db/oracle/test")
        load_response = c.post("/api/files/missing/load", json={"dialect": "mysql"})
    assert test_response.status_code == 409
    assert test_response.json()["error"]["code"] == "E_DB_NOT_CONFIGURED"
    assert load_response.status_code == 409
    assert load_response.json()["error"]["code"] == "E_DB_NOT_CONFIGURED"


def test_api_route_method_and_validation_errors_use_error_envelope(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        responses = [
            c.get("/api/does-not-exist"),
            c.post("/api/status"),
            c.get("/api/files/missing/preview?offset=not-an-integer"),
        ]

    expected_codes = {"E_ROUTE_NOT_FOUND", "E_METHOD_NOT_ALLOWED", "E_BAD_REQUEST"}
    for response in responses:
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {"code", "params"}
        assert response.json()["error"]["code"] in expected_codes
        expected_codes.remove(response.json()["error"]["code"])
        assert isinstance(response.json()["error"]["params"], dict)
    assert not expected_codes


def test_driver_exception_is_reduced_to_code_in_api_and_logs(tmp_path, monkeypatch, caplog):
    sentinel = "UNIT_TEST_ONLY_SECRET"
    oracle = DbProfile(
        kind="oracle",
        host="db.invalid",
        port=1521,
        target="service.invalid",
        user="unit_user",
        password=sentinel,
    )
    mysql = DbProfile(kind="mysql", offline_reason="E_DB_NOT_CONFIGURED")
    cfg = AppConfig(db={"oracle": oracle, "mysql": mysql})

    def fail_with_sensitive_text(**_kwargs):
        raise RuntimeError(f"ORA-01017 login failed for {sentinel}@secret-dsn.invalid")

    monkeypatch.setattr("app.core.db.oracledb.connect", fail_with_sensitive_text)
    with client_with(cfg) as c:
        response = c.post("/api/db/oracle/test")
    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "E_DB_CONNECT",
        "params": {"driver_code": "ORA-01017"},
    }
    assert sentinel not in response.text
    assert "secret-dsn.invalid" not in response.text
    assert sentinel not in caplog.text
    assert "secret-dsn.invalid" not in caplog.text


def test_ddl_api_modes_targets_and_sql_download(tmp_path, parquet_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        uploaded = c.post(
            "/api/files/upload",
            files={"file": ("fixture.parquet", parquet_path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = uploaded.json()["file"]["file_id"]
        try:
            for dialect in ("oracle", "mysql"):
                for mode in ("none", "child", "explode"):
                    options = {
                        "dialect": dialect,
                        "mode": mode,
                        "table_name": "PB_TEST_API",
                        "parent_key": "id" if mode == "child" else None,
                        "explode_column": "list_int" if mode == "explode" else None,
                        "preserve_empty": True,
                        "json_check": True,
                        "length_scan": "sample",
                    }
                    response = c.post(f"/api/files/{file_id}/ddl", json=options)
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert result["ddl"].startswith("CREATE TABLE")
                    assert result["previews"]
                    assert result["select_sql"][0]["parameters"] == ["parquet_path"]
                    assert str(parquet_path) not in result["select_sql"][0]["sql"]
                    if mode == "explode":
                        assert result["previews"][0]["expected_rows"] == result["previews"][0]["actual_rows"]
            download = c.post(
                f"/api/files/{file_id}/ddl/download",
                json={"dialect": "oracle", "mode": "none", "table_name": "PB_TEST_API"},
            )
            assert download.status_code == 200
            assert download.headers["content-type"].startswith("application/sql")
            assert download.text.startswith("CREATE TABLE PB_TEST_API")
            invalid = c.post(
                f"/api/files/{file_id}/ddl",
                json={"dialect": "oracle", "mode": "none", "table_name": "bad-name"},
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "E_TABLE_NAME_INVALID"
        finally:
            c.delete(f"/api/files/{file_id}")


def test_ddl_api_warns_on_large_explode_multiplier(tmp_path):
    path = tmp_path / "wide-list.parquet"
    pq.write_table(pa.table({"items": pa.array([list(range(100))], type=pa.list_(pa.int32()))}), path)
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        uploaded = c.post(
            "/api/files/upload",
            files={"file": ("wide-list.parquet", path.read_bytes(), "application/octet-stream")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file"]["file_id"]
        try:
            response = c.post(
                f"/api/files/{file_id}/ddl",
                json={
                    "dialect": "mysql",
                    "mode": "explode",
                    "table_name": "PB_TEST_WIDE",
                    "explode_column": "items",
                    "preserve_empty": True,
                },
            )
            assert response.status_code == 200, response.text
            warning_codes = {item["code"] for item in response.json()["warnings"]}
            assert "W_EXPLODE_ROW_MULTIPLIER" in warning_codes
        finally:
            c.delete(f"/api/files/{file_id}")


def test_string_scan_wait_accepts_files_without_variable_length_columns(tmp_path):
    path = tmp_path / "ints.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3]}), path)
    manager = StringScanManager(max_workers=1)
    try:
        manager.start("ints", path)
        measured = manager.wait("ints", threading.Event())
        assert isinstance(measured, StringLengthMeasurements)
        assert dict(measured) == {}
        assert measured.margin >= 1.0
    finally:
        manager.shutdown()


@pytest.mark.parametrize("source", ["local", "upload"])
@pytest.mark.parametrize("filename, expected", [
    ("123.parquet", {"oracle": "T_123", "mysql": "_123", "postgres": "_123"}),
    ("_orders.parquet", {"oracle": "ORDERS", "mysql": "_ORDERS", "postgres": "_ORDERS"}),
    ("한글.parquet", {"oracle": "T_", "mysql": "__", "postgres": "__"}),
    ("select.parquet", {dialect: "PARQUET_TABLE" for dialect in ("oracle", "mysql", "postgres")}),
    ("a" * 130 + ".parquet", {"oracle": "A" * 128, "mysql": "A" * 64, "postgres": "A" * 63}),
])
def test_selected_file_supplies_valid_default_table_names(tmp_path, source, filename, expected):
    path = tmp_path / filename
    pq.write_table(pa.table({"id": [1]}), path)
    cfg = AppConfig(server=ServerConfig(allowed_roots=(tmp_path,), upload_dir=tmp_path / "uploads"))
    with client_with(cfg) as client:
        if source == "local":
            response = client.post("/api/files/local", json={"path": str(path)})
        else:
            response = client.post("/api/files/upload", files={"file": (filename, path.read_bytes())})
        assert response.status_code == 200
        selected = response.json()["file"]
        assert selected["default_table_names"] == expected
        for dialect, name in expected.items():
            ddl = client.post(f"/api/files/{selected['file_id']}/ddl", json={
                "dialect": dialect, "table_name": name,
            })
            assert ddl.status_code == 200, ddl.json()
            assert ddl.json()["table_name"] == name
