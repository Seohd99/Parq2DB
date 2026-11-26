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
from app.api.schemas import DdlOptions, LoadOptions
from app.main import app


def client_with(cfg) -> TestClient:
    app.state.config = cfg
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_index_and_static(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        r = c.get("/")
        app_js = c.get("/static/app.js")
        en_catalog = c.get("/i18n/en.json")
        assert r.status_code == 200
        assert "Parq2DB" in r.text
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


@pytest.mark.parametrize("host", ["evil.example:8765", "evil.example", "127.0.0.1.evil.example", ""])
def test_foreign_host_header_is_refused(tmp_path, host):
    # A DNS-rebinding page reaches 127.0.0.1 but still sends its own domain as Host.
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        for response in (c.get("/api/status", headers={"host": host}), c.get("/", headers={"host": host})):
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "E_HOST_NOT_ALLOWED"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "LOCALHOST", "[::1]:8765"])
def test_loopback_host_headers_are_accepted(tmp_path, host):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        assert c.get("/api/status", headers={"host": host}).status_code == 200


def test_configured_allowed_host_is_accepted(tmp_path):
    cfg = AppConfig(server=ServerConfig(host="0.0.0.0", allowed_hosts=("pb.lan:8765",)))
    with client_with(cfg) as c:
        assert c.get("/api/status", headers={"host": "pb.lan:8765"}).status_code == 200
        assert c.get("/api/status", headers={"host": "0.0.0.0:8765"}).status_code == 403


def test_cross_site_origin_is_refused_for_state_changes(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    with client_with(cfg) as c:
        for origin in ("http://evil.example", "http://127.0.0.1:3000", "null"):
            response = c.post("/api/jobs/missing/cancel", headers={"origin": origin})
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "E_ORIGIN_NOT_ALLOWED"
        # Same-origin posts reach the route, while GET keeps its prior Origin behavior.
        assert c.post("/api/jobs/missing/cancel", headers={"origin": "http://127.0.0.1:8765"}).status_code == 404
        assert c.post("/api/jobs/missing/cancel").status_code == 404
        assert c.get("/api/status", headers={"origin": "http://evil.example"}).status_code == 200


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


def test_scan_hold_keeps_shared_scan_until_last_interest(monkeypatch, parquet_path):
    started = threading.Event()
    finish = threading.Event()
    cancel_events = []

    def blocked_scan(_path, _schema, **kwargs):
        cancel_events.append(kwargs["cancel_event"])
        started.set()
        while not finish.wait(0.01):
            if kwargs["cancel_event"].is_set():
                raise PBError("E_STRING_SCAN_CANCELLED")
        return StringLengthMeasurements({"name": StringLength(3, 3)})

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", blocked_scan)
    manager = StringScanManager(max_workers=1)
    try:
        with pytest.raises(PBError) as missing:
            manager.hold("never-selected")
        assert missing.value.code == "E_STRING_SCAN_CANCELLED"

        manager.start("first-handle", parquet_path)
        manager.start("second-handle", parquet_path)
        assert started.wait(1)
        handle = manager.hold("first-handle")
        manager.release("first-handle")
        manager.release("second-handle")
        assert len(cancel_events) == 1
        assert not cancel_events[0].is_set()

        finish.set()
        assert manager.wait(handle, threading.Event())["name"] == StringLength(3, 3)
        manager.release(handle)
        assert manager._by_file == {}
        assert manager._by_key == {}
    finally:
        finish.set()
        manager.shutdown()


def _held_load_client(tmp_path, monkeypatch):
    """Upload-ready client whose full scan waits for `finish_scan` and whose DB load is faked."""
    from app.core import jobs, typemap

    profile = DbProfile(
        kind="mysql",
        host="db.invalid",
        port=3306,
        target="unit_test",
        user="unit_test_user",
        password="UNIT_TEST_ONLY_SECRET",
    )
    cfg = AppConfig(server=ServerConfig(upload_dir=tmp_path / "uploads"), db={"mysql": profile})
    events = {"scan_started": threading.Event(), "finish_scan": threading.Event(), "finish_load": threading.Event()}
    events["finish_load"].set()
    scan_cancel_events = []
    loaded_types = []
    real_measure = typemap.measure_string_lengths

    def delayed_measure(path, schema, **kwargs):
        scan_cancel_events.append(kwargs["cancel_event"])
        events["scan_started"].set()
        while not events["finish_scan"].wait(0.01):
            if kwargs["cancel_event"].is_set():
                raise PBError("E_STRING_SCAN_CANCELLED")
        return real_measure(path, schema, **kwargs)

    def fake_execute_load(**kwargs):
        assert events["finish_load"].wait(3)
        assert kwargs["path"].is_file()
        loaded_types.append(kwargs["tables"][0].columns[0].sql_type_mysql)
        return {"tables": [], "committed_batches": 0, "committed_rows": 0}

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", delayed_measure)
    monkeypatch.setattr(jobs, "execute_load", fake_execute_load)
    return client_with(cfg), events, scan_cancel_events, loaded_types


def _upload(c, path, name):
    uploaded = c.post("/api/files/upload", files={"file": (name, path.read_bytes(), "application/octet-stream")})
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()["file"]["file_id"]


def _wait_job(c, job_id):
    deadline = time.monotonic() + 3
    job = c.get(f"/api/jobs/{job_id}").json()
    while time.monotonic() < deadline and job["status"] in {"queued", "running"}:
        time.sleep(0.01)
        job = c.get(f"/api/jobs/{job_id}").json()
    return job


def test_selecting_another_file_keeps_submitted_load_scan(tmp_path, monkeypatch):
    path = tmp_path / "held.parquet"
    pq.write_table(pa.table({"comment": ["x", "y" * 500]}), path)
    other = tmp_path / "other.parquet"
    pq.write_table(pa.table({"comment": ["z"]}), other)
    client, events, scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    with client as c:
        scans = c.app.state.string_scans
        file_id = _upload(c, path, "held.parquet")
        assert events["scan_started"].wait(1)
        response = c.post(f"/api/files/{file_id}/load", json={"dialect": "mysql", "table_name": "PB_TEST_HELD"})
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]

        # The UI selects the next file and releases this one while the job still measures.
        other_id = _upload(c, other, "other.parquet")
        assert c.post(f"/api/files/{file_id}/release").status_code == 200
        assert c.get(f"/api/jobs/{job_id}").json()["stage"] == "measuring"
        assert not scan_cancel_events[0].is_set()

        events["finish_scan"].set()
        job = _wait_job(c, job_id)
        assert job["status"] == "succeeded", job
        assert loaded_types == ["VARCHAR(500)"]
        # The job's handle is returned; only the other selection's scan remains.
        assert set(scans._by_file) == {other_id}
        assert len(scans._by_key) == 1
        assert c.delete(f"/api/files/{file_id}").status_code == 200
        c.delete(f"/api/files/{other_id}")


def test_cancelling_load_after_release_cancels_its_unshared_scan(tmp_path, monkeypatch):
    path = tmp_path / "cancel-held.parquet"
    pq.write_table(pa.table({"comment": ["x"]}), path)
    client, events, scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    with client as c:
        scans = c.app.state.string_scans
        file_id = _upload(c, path, "cancel-held.parquet")
        assert events["scan_started"].wait(1)
        job_id = c.post(
            f"/api/files/{file_id}/load", json={"dialect": "mysql", "table_name": "PB_TEST_CANCEL_HELD"},
        ).json()["job_id"]
        assert c.post(f"/api/files/{file_id}/release").status_code == 200
        assert not scan_cancel_events[0].is_set()

        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        job = _wait_job(c, job_id)
        assert job["status"] == "cancelled", job
        assert loaded_types == []
        # The job's handle was the last interest, so ending the job cancels and drops the scan.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and scans._by_file:
            time.sleep(0.01)
        assert scan_cancel_events[0].is_set()
        assert scans._by_file == {}
        assert scans._by_key == {}
        assert c.delete(f"/api/files/{file_id}").status_code == 200


def test_upload_used_by_load_job_cannot_be_deleted_until_job_ends(tmp_path, monkeypatch):
    path = tmp_path / "in-use.parquet"
    pq.write_table(pa.table({"comment": ["x"]}), path)
    client, events, _scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    events["finish_load"].clear()
    with client as c:
        file_id = _upload(c, path, "in-use.parquet")
        job_id = c.post(
            f"/api/files/{file_id}/load", json={"dialect": "mysql", "table_name": "PB_TEST_IN_USE"},
        ).json()["job_id"]

        refused = c.delete(f"/api/files/{file_id}")
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "E_FILE_IN_USE"
        # A refused delete leaves the selection and its scan intact.
        assert c.get(f"/api/files/{file_id}/length-scan").status_code == 200
        assert c.get(f"/api/files/{file_id}/inspect").status_code == 200

        events["finish_scan"].set()
        events["finish_load"].set()
        assert _wait_job(c, job_id)["status"] == "succeeded"
        assert loaded_types == ["VARCHAR(1)"]
        assert c.delete(f"/api/files/{file_id}").status_code == 200
        assert list((tmp_path / "uploads").iterdir()) == []


def test_upload_used_by_insert_job_cannot_be_deleted_until_job_ends(tmp_path, monkeypatch, parquet_path):
    from app.api import loads
    from app.core.model import ColumnModel, TableModel

    model = TableModel(name="PB_TEST_INSERT_HELD", columns=(ColumnModel("x", "x", "X", None, "int"),))
    monkeypatch.setattr(loads, "inspect_insert_target", lambda *_args: (model, {"warnings": []}, 1))
    client, events, _scan_cancel_events, _loaded_types = _held_load_client(tmp_path, monkeypatch)
    events["finish_scan"].set()
    events["finish_load"].clear()
    with client as c:
        file_id = _upload(c, parquet_path, "insert-held.parquet")
        response = c.post(
            f"/api/files/{file_id}/insert", json={"dialect": "mysql", "table_name": "PB_TEST_INSERT_HELD"},
        )
        assert response.status_code == 200, response.text
        assert c.delete(f"/api/files/{file_id}").json()["error"]["code"] == "E_FILE_IN_USE"

        events["finish_load"].set()
        assert _wait_job(c, response.json()["job_id"])["status"] == "succeeded"
        assert c.delete(f"/api/files/{file_id}").status_code == 200


def _wait_scan_ready(c, file_id):
    deadline = time.monotonic() + 3
    scan = c.get(f"/api/files/{file_id}/length-scan").json()
    while time.monotonic() < deadline and scan["status"] != "ready":
        time.sleep(0.01)
        scan = c.get(f"/api/files/{file_id}/length-scan").json()
    assert scan["status"] == "ready", scan


def _barrier(monkeypatch, owner, name):
    """Pause `owner.name` at entry until `proceed` is set; `arrived` reports the pause."""
    arrived = threading.Event()
    proceed = threading.Event()
    original = getattr(owner, name)

    def paused(self, *args, **kwargs):
        arrived.set()
        assert proceed.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, paused)
    return arrived, proceed


def _post_in_thread(c, url, body):
    result = {}

    def send():
        result["response"] = c.post(url, json=body)

    thread = threading.Thread(target=send)
    thread.start()
    return thread, result


def _insert_target_stub(monkeypatch):
    from app.api import loads
    from app.core.model import ColumnModel, TableModel

    model = TableModel(name="PB_TEST_RACE", columns=(ColumnModel("comment", "comment", "COMMENT", None, "string"),))
    monkeypatch.setattr(loads, "inspect_insert_target", lambda *_args: (model, {"warnings": []}, 1))


@pytest.mark.parametrize("endpoint", ["load", "insert"])
def test_delete_that_wins_the_race_refuses_the_later_job_without_leaking_holds(tmp_path, monkeypatch, endpoint):
    from app.core.files import FileRegistry

    path = tmp_path / "race-delete.parquet"
    pq.write_table(pa.table({"comment": ["x"]}), path)
    _insert_target_stub(monkeypatch)
    client, events, _scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    events["finish_scan"].set()
    arrived, proceed = _barrier(monkeypatch, FileRegistry, "hold")
    with client as c:
        file_id = _upload(c, path, "race-delete.parquet")
        thread, result = _post_in_thread(
            c, f"/api/files/{file_id}/{endpoint}", {"dialect": "mysql", "table_name": "PB_TEST_RACE"},
        )
        try:
            # The job request has passed its file lookup but holds nothing yet.
            assert arrived.wait(5)
            assert c.delete(f"/api/files/{file_id}").status_code == 200
        finally:
            proceed.set()
            thread.join(5)
        response = result["response"]
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "E_FILE_NOT_FOUND"
        assert c.app.state.files._holds == {}
        assert c.app.state.string_scans._by_file == {}
        assert c.app.state.jobs._jobs == {}
        assert loaded_types == []
        assert list((tmp_path / "uploads").iterdir()) == []


def test_job_that_wins_the_race_keeps_the_upload_and_the_selection_scan(tmp_path, monkeypatch):
    from app.core.string_scans import StringScanManager as Scans

    path = tmp_path / "race-job.parquet"
    pq.write_table(pa.table({"comment": ["x"]}), path)
    client, events, scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    events["finish_scan"].set()
    arrived, proceed = _barrier(monkeypatch, Scans, "hold")
    with client as c:
        file_id = _upload(c, path, "race-job.parquet")
        _wait_scan_ready(c, file_id)
        thread, result = _post_in_thread(
            c, f"/api/files/{file_id}/load", {"dialect": "mysql", "table_name": "PB_TEST_RACE"},
        )
        try:
            # The job request holds the file and is about to take its scan handle.
            assert arrived.wait(5)
            refused = c.delete(f"/api/files/{file_id}")
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "E_FILE_IN_USE"
            assert c.get(f"/api/files/{file_id}/length-scan").json()["status"] == "ready"
        finally:
            proceed.set()
            thread.join(5)
        response = result["response"]
        assert response.status_code == 200, response.text
        assert _wait_job(c, response.json()["job_id"])["status"] == "succeeded"
        assert loaded_types == ["VARCHAR(1)"]
        assert not scan_cancel_events[0].is_set()
        assert c.app.state.files._holds == {}
        assert c.delete(f"/api/files/{file_id}").status_code == 200


def test_cancelling_a_queued_load_frees_its_upload_while_workers_are_busy(tmp_path, monkeypatch):
    from app.core import jobs

    monkeypatch.setattr("app.main.LoadJobManager", lambda: jobs.LoadJobManager(max_workers=1))
    busy = tmp_path / "busy.parquet"
    pq.write_table(pa.table({"comment": ["x"]}), busy)
    queued = tmp_path / "queued.parquet"
    pq.write_table(pa.table({"comment": ["y"]}), queued)
    client, events, scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    events["finish_scan"].set()
    events["finish_load"].clear()
    with client as c:
        busy_id = _upload(c, busy, "busy.parquet")
        queued_id = _upload(c, queued, "queued.parquet")
        _wait_scan_ready(c, queued_id)
        busy_job = c.post(
            f"/api/files/{busy_id}/load", json={"dialect": "mysql", "table_name": "PB_TEST_BUSY"},
        ).json()["job_id"]
        queued_job = c.post(
            f"/api/files/{queued_id}/load", json={"dialect": "mysql", "table_name": "PB_TEST_QUEUED"},
        ).json()["job_id"]
        try:
            assert c.get(f"/api/jobs/{queued_job}").json()["status"] == "queued"
            assert c.delete(f"/api/files/{queued_id}").json()["error"]["code"] == "E_FILE_IN_USE"

            # The only worker is still busy; cancelling must not wait for it.
            assert c.post(f"/api/jobs/{queued_job}/cancel").json()["status"] == "cancelled"
            assert c.app.state.files._holds == {busy_id: 1}
            # The selection's own interest keeps the finished scan.
            assert c.get(f"/api/files/{queued_id}/length-scan").json()["status"] == "ready"
            assert c.delete(f"/api/files/{queued_id}").status_code == 200
            assert c.get(f"/api/jobs/{busy_job}").json()["status"] == "running"
        finally:
            events["finish_load"].set()
        assert _wait_job(c, busy_job)["status"] == "succeeded"
        assert loaded_types == ["VARCHAR(1)"]
        assert c.app.state.files._holds == {}
        assert not any(event.is_set() for event in scan_cancel_events)
        c.delete(f"/api/files/{busy_id}")


def test_delete_blocked_by_an_open_reader_keeps_the_upload_loadable(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "busy-delete.parquet"
    pq.write_table(pa.table({"comment": ["x"]}), path)
    client, events, _scan_cancel_events, loaded_types = _held_load_client(tmp_path, monkeypatch)
    events["finish_scan"].set()
    real_unlink = Path.unlink
    refused_paths = []

    def sharing_violation_once(self, *args, **kwargs):
        if self.parent.name == "uploads" and not refused_paths:
            refused_paths.append(self)
            # What Windows raises while another reader, such as a preview, has the file open.
            raise PermissionError(13, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", sharing_violation_once)
    with client as c:
        file_id = _upload(c, path, "busy-delete.parquet")
        _wait_scan_ready(c, file_id)

        refused = c.delete(f"/api/files/{file_id}")
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "E_FILE_BUSY"
        assert c.app.state.files._deleting == set()
        assert c.get(f"/api/files/{file_id}/inspect").status_code == 200
        # The delete stopped the selection's scan before the unlink failed; it runs again.
        _wait_scan_ready(c, file_id)
        response = c.post(f"/api/files/{file_id}/load", json={"dialect": "mysql", "table_name": "PB_TEST_BUSY_DELETE"})
        assert response.status_code == 200, response.text
        assert _wait_job(c, response.json()["job_id"])["status"] == "succeeded"
        assert loaded_types == ["VARCHAR(1)"]

        assert c.delete(f"/api/files/{file_id}").status_code == 200
        assert list((tmp_path / "uploads").iterdir()) == []


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


def test_ddl_api_applies_column_selection(tmp_path):
    path = tmp_path / "pick.parquet"
    pq.write_table(pa.table({"id": [1], "name": ["a"], "secret": ["x"]}), path)
    cfg = AppConfig(server=ServerConfig(allowed_roots=(tmp_path,), upload_dir=tmp_path / "uploads"))
    with client_with(cfg) as client:
        file_id = client.post("/api/files/local", json={"path": str(path)}).json()["file"]["file_id"]
        response = client.post(f"/api/files/{file_id}/ddl", json={
            "dialect": "postgres", "table_name": "PB_TEST_PICK", "columns": ["id", "name"],
        })
        assert response.status_code == 200, response.json()
        body = response.json()
        assert [column["name"] for column in body["tables"][0]["columns"]] == ["ID", "NAME"]
        assert "SECRET" not in body["ddl"]
        assert body["previews"][0]["rows"] == [{"ID": 1, "NAME": "a"}]
        invalid = client.post(f"/api/files/{file_id}/ddl", json={
            "dialect": "postgres", "table_name": "PB_TEST_PICK", "columns": [],
        })
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "E_COLUMNS_EMPTY"


def test_release_forgets_local_selection_but_keeps_upload_deletable(tmp_path):
    path = tmp_path / "released-local.parquet"
    pq.write_table(pa.table({"id": [1]}), path)
    cfg = AppConfig(server=ServerConfig(allowed_roots=(tmp_path,), upload_dir=tmp_path / "uploads"))
    with client_with(cfg) as client:
        local_id = client.post("/api/files/local", json={"path": str(path)}).json()["file"]["file_id"]
        upload_id = client.post(
            "/api/files/upload", files={"file": ("released.parquet", path.read_bytes())},
        ).json()["file"]["file_id"]

        assert client.post(f"/api/files/{local_id}/release").status_code == 200
        assert client.post(f"/api/files/{upload_id}/release").status_code == 200

        gone = client.get(f"/api/files/{local_id}/inspect")
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "E_FILE_NOT_FOUND"
        assert client.delete(f"/api/files/{upload_id}").status_code == 200
        assert path.is_file()
