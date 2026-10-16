import threading
import time

from fastapi.testclient import TestClient
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core import db, insert_target, jobs, load_mysql, load_oracle, load_postgres
from app.core.config import AppConfig, DbProfile, ServerConfig
from app.core.errors import PBError
from app.core.insert_target import (
    insert_warnings,
    match_columns,
    plan_insert_table,
    selected_matches,
)
from app.main import app


def profile(kind="mysql", schema=None):
    return DbProfile(
        kind=kind,
        host="db.invalid",
        port=3306,
        target="unit_test",
        user="unit_test_user",
        password="UNIT_TEST_ONLY_SECRET",
        schema=schema,
    )


def stored(name, *, data_type="VARCHAR(20)", nullable=True, has_default=False, generated=False):
    return {
        "name": name,
        "data_type": data_type,
        "nullable": nullable,
        "has_default": has_default,
        "generated": generated,
    }


def schema_of(path):
    with pq.ParquetFile(path) as parquet:
        return parquet.schema_arrow


def test_match_prefers_exact_names_then_ignores_case_and_skips_generated_columns():
    schema = pa.schema([
        pa.field("id", pa.int64()),
        pa.field("Name", pa.string()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("total", pa.int64()),
        pa.field("extra", pa.string()),
    ])
    match = match_columns(schema, [
        stored("ID", nullable=False),
        stored("Name"),
        stored("NAME"),
        stored("TAGS", data_type="CLOB"),
        stored("TOTAL", generated=True),
        stored("CREATED_AT", nullable=False, has_default=True),
        stored("CODE", nullable=False),
    ])
    sources = {column["name"]: column["source"] for column in match["columns"]}
    assert sources == {
        "ID": "id", "Name": "Name", "NAME": None, "TAGS": "tags",
        "TOTAL": None, "CREATED_AT": None, "CODE": None,
    }
    assert [item["source"] for item in match["file_only"]] == ["total", "extra"]
    by_name = {column["name"]: column for column in match["columns"]}
    assert by_name["TAGS"]["source_kind"] == "list"
    assert by_name["CODE"]["required"] is True
    assert by_name["CREATED_AT"]["required"] is False
    assert by_name["TOTAL"]["required"] is False


def test_ambiguous_case_insensitive_match_is_left_unmatched():
    schema = pa.schema([pa.field("name", pa.string())])
    match = match_columns(schema, [stored("NAME"), stored("Name")])
    assert all(column["source"] is None for column in match["columns"])
    assert match["file_only"] == [{"source": "name", "kind": "scalar"}]


def test_selected_matches_validates_requested_columns():
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string()), pa.field("x", pa.string())])
    match = match_columns(schema, [stored("ID"), stored("NAME")])
    assert [column["name"] for column in selected_matches(match, None)] == ["ID", "NAME"]
    assert [column["name"] for column in selected_matches(match, ["name"])] == ["NAME"]
    with pytest.raises(PBError) as empty:
        selected_matches(match, [])
    assert empty.value.code == "E_COLUMNS_EMPTY"
    with pytest.raises(PBError) as unmatched:
        selected_matches(match, ["x"])
    assert unmatched.value.code == "E_INSERT_COLUMN_NOT_MATCHED"
    none = match_columns(schema, [stored("OTHER")])
    with pytest.raises(PBError) as nothing:
        selected_matches(none, None)
    assert nothing.value.code == "E_INSERT_NO_MATCHING_COLUMNS"


def test_warnings_cover_skipped_file_columns_and_unfilled_required_columns():
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("code", pa.string()), pa.field("x", pa.string())])
    match = match_columns(schema, [stored("ID"), stored("CODE", nullable=False)])
    chosen = selected_matches(match, ["id"])
    assert insert_warnings("T", match, chosen) == [
        {"code": "W_INSERT_FILE_COLUMNS_SKIPPED", "params": {"table": "T", "columns": ["x"]}},
        {"code": "W_INSERT_REQUIRED_COLUMNS", "params": {"table": "T", "columns": ["CODE"]}},
    ]
    assert insert_warnings("T", match, selected_matches(match, None)) == [
        {"code": "W_INSERT_FILE_COLUMNS_SKIPPED", "params": {"table": "T", "columns": ["x"]}},
    ]


def test_insert_plan_reads_chosen_sources_and_names_stored_columns(flat_parquet_path):
    schema = schema_of(flat_parquet_path)
    match = match_columns(schema, [
        stored("Active", data_type="NUMBER(1)"),
        stored("ID", data_type="NUMBER(19)"),
        stored("NOTE"),
    ])
    table = plan_insert_table(
        schema, table_name="Mixed_Case", dialect="oracle", chosen=selected_matches(match, None),
    )
    assert table.name == "Mixed_Case"
    assert table.identifier_policy == "quoted"
    assert table.mode == "none"
    # File order decides the INSERT column order and the projected read.
    assert [column.name for column in table.columns] == ["ID", "Active"]
    assert [column.original_name for column in table.columns] == ["id", "active"]
    assert [column.sql_type("oracle") for column in table.columns] == ["NUMBER(19)", "NUMBER(1)"]
    assert table.select_plan.read_columns == ("id", "active")
    assert load_oracle.insert_sql(table) == 'INSERT INTO "Mixed_Case" ("ID", "Active") VALUES (:1, :2)'
    assert load_mysql.insert_sql(table) == "INSERT INTO `Mixed_Case` (`ID`, `Active`) VALUES (%s, %s)"
    assert load_postgres.insert_sql(table) == 'INSERT INTO "Mixed_Case" ("ID", "Active") VALUES (%s, %s)'


def test_insert_plan_binds_oracle_lob_columns_from_the_stored_type(flat_parquet_path):
    schema = schema_of(flat_parquet_path)
    match = match_columns(schema, [stored("NAME", data_type="CLOB")])
    table = plan_insert_table(schema, table_name="T", dialect="oracle", chosen=selected_matches(match, None))
    import oracledb

    assert load_oracle._input_sizes(table) == [oracledb.DB_TYPE_CLOB]


class RowsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class RowsConnection:
    def __init__(self, rows):
        self.cursor_obj = RowsCursor(rows)

    def cursor(self):
        return self.cursor_obj


@pytest.mark.parametrize("kind", ["oracle", "mysql", "postgres"])
def test_find_table_prefers_exact_name_then_a_single_case_insensitive_match(kind):
    assert db.find_table(RowsConnection([("Orders",), ("ORDERS",)]), profile(kind), "ORDERS") == "ORDERS"
    assert db.find_table(RowsConnection([("orders",)]), profile(kind), "ORDERS") == "orders"
    with pytest.raises(PBError) as missing:
        db.find_table(RowsConnection([]), profile(kind), "ORDERS")
    assert missing.value.code == "E_INSERT_TABLE_MISSING"
    with pytest.raises(PBError) as ambiguous:
        db.find_table(RowsConnection([("Orders",), ("ORDERS",)]), profile(kind), "orders")
    assert ambiguous.value.code == "E_INSERT_TABLE_AMBIGUOUS"
    assert ambiguous.value.params["tables"] == ["ORDERS", "Orders"]


def test_describe_columns_maps_each_dialect_catalog():
    oracle = db.describe_columns(RowsConnection([
        ("ID", "NUMBER", None, None, 19, 0, "N", None, "YES", "NO"),
        ("NAME", "VARCHAR2", 20, "C", None, None, "Y", None, "NO", "NO"),
        ("AMOUNT", "NUMBER", None, None, 10, 2, "N", 3, "NO", "NO"),
        ("TOTAL", "NUMBER", None, None, None, None, "Y", 10, "NO", "YES"),
    ]), profile("oracle"), "T")
    assert oracle == [
        {"name": "ID", "data_type": "NUMBER(19)", "nullable": False, "has_default": True, "generated": False},
        {"name": "NAME", "data_type": "VARCHAR2(20 CHAR)", "nullable": True, "has_default": False, "generated": False},
        {"name": "AMOUNT", "data_type": "NUMBER(10,2)", "nullable": False, "has_default": True, "generated": False},
        {"name": "TOTAL", "data_type": "NUMBER", "nullable": True, "has_default": True, "generated": True},
    ]
    mysql = db.describe_columns(RowsConnection([
        ("id", b"bigint", "NO", None, "auto_increment"),
        ("code", "varchar(10)", "NO", None, ""),
        ("total", "int", "YES", None, "VIRTUAL GENERATED"),
    ]), profile("mysql"), "t")
    assert [(c["name"], c["data_type"], c["has_default"], c["generated"]) for c in mysql] == [
        ("id", "bigint", True, False), ("code", "varchar(10)", False, False), ("total", "int", False, True),
    ]
    connection = RowsConnection([("id", "bigint", False, True, False), ("note", "text", True, False, False)])
    postgres = db.describe_columns(connection, profile("postgres", schema="app"), "t")
    assert postgres[0] == {"name": "id", "data_type": "bigint", "nullable": False, "has_default": True, "generated": False}
    assert connection.cursor_obj.executed[0][1] == ("app", "t")


def online_config(tmp_path):
    return AppConfig(server=ServerConfig(upload_dir=tmp_path / "uploads"), db={"mysql": profile("mysql")})


@pytest.fixture
def fake_target(monkeypatch):
    columns = [
        stored("ID", data_type="bigint", nullable=False),
        stored("NAME", data_type="varchar(20)"),
        stored("LOADED_AT", data_type="datetime", nullable=False, has_default=True),
    ]
    monkeypatch.setattr(insert_target, "connect", lambda _profile: object())
    monkeypatch.setattr(insert_target, "close_quietly", lambda _connection: None)
    monkeypatch.setattr(insert_target, "find_table", lambda _c, _p, name: "PB_TEST_INSERT" if name.upper() == "PB_TEST_INSERT" else (_ for _ in ()).throw(PBError("E_INSERT_TABLE_MISSING", table=name)))
    monkeypatch.setattr(insert_target, "describe_columns", lambda *_args: columns)
    return columns


def upload(client, path):
    response = client.post(
        "/api/files/upload",
        files={"file": ("flat.parquet", path.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()["file"]["file_id"]


def test_check_endpoint_reports_matches_without_ddl(tmp_path, flat_parquet_path, fake_target):
    app.state.config = online_config(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        file_id = upload(c, flat_parquet_path)
        try:
            response = c.post(f"/api/files/{file_id}/insert/check", json={"dialect": "mysql", "table_name": "pb_test_insert"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["table"] == "PB_TEST_INSERT"
            assert body["source_rows"] == 3
            assert [(column["name"], column["source"]) for column in body["columns"]] == [
                ("ID", "id"), ("NAME", "name"), ("LOADED_AT", None),
            ]
            assert body["file_only"] == [{"source": "active", "kind": "scalar"}]
            assert [item["code"] for item in body["warnings"]] == ["W_INSERT_FILE_COLUMNS_SKIPPED"]
            assert "ddl" not in body

            missing = c.post(f"/api/files/{file_id}/insert/check", json={"dialect": "mysql", "table_name": "nope"})
            assert missing.status_code == 409
            assert missing.json()["error"]["code"] == "E_INSERT_TABLE_MISSING"
        finally:
            c.delete(f"/api/files/{file_id}")


def test_insert_endpoint_loads_matched_columns_without_waiting_for_the_length_scan(
    tmp_path, flat_parquet_path, fake_target, monkeypatch,
):
    release_scan = threading.Event()
    captured = []

    def blocked_scan(*_args, **_kwargs):
        assert release_scan.wait(5)
        return {}

    def fake_execute_load(**kwargs):
        captured.append(kwargs)
        return {"tables": [], "committed_batches": 0, "committed_rows": 0}

    monkeypatch.setattr("app.core.string_scans.measure_string_lengths", blocked_scan)
    monkeypatch.setattr(jobs, "execute_load", fake_execute_load)
    app.state.config = online_config(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        file_id = upload(c, flat_parquet_path)
        try:
            response = c.post(
                f"/api/files/{file_id}/insert",
                json={"dialect": "mysql", "table_name": "PB_TEST_INSERT", "columns": ["name"]},
            )
            assert response.status_code == 200, response.text
            job = response.json()
            assert job["existing_policy"] == "insert_only"
            assert job["stage"] == "queued"
            assert job["measurement_progress_percent"] is None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and job["status"] in {"queued", "running"}:
                time.sleep(0.01)
                job = c.get(f"/api/jobs/{job['job_id']}").json()
            # The job finished while the string-length scan was still blocked.
            assert job["status"] == "succeeded", job
            (call,) = captured
            (table,) = call["tables"]
            assert call["existing_policy"] == "insert_only"
            assert table.name == "PB_TEST_INSERT"
            assert [column.name for column in table.columns] == ["NAME"]

            unmatched = c.post(
                f"/api/files/{file_id}/insert",
                json={"dialect": "mysql", "table_name": "PB_TEST_INSERT", "columns": ["active"]},
            )
            assert unmatched.status_code == 400
            assert unmatched.json()["error"]["code"] == "E_INSERT_COLUMN_NOT_MATCHED"
        finally:
            release_scan.set()
            c.delete(f"/api/files/{file_id}")


def test_load_endpoint_sends_insert_only_to_the_insert_endpoint(tmp_path, flat_parquet_path):
    app.state.config = online_config(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        file_id = upload(c, flat_parquet_path)
        try:
            response = c.post(
                f"/api/files/{file_id}/load",
                json={"dialect": "mysql", "table_name": "PB_TEST_INSERT", "existing_policy": "insert_only"},
            )
            assert response.status_code == 400
            assert response.json()["error"] == {"code": "E_BAD_REQUEST", "params": {"fields": ["existing_policy"]}}
        finally:
            c.delete(f"/api/files/{file_id}")


def test_table_list_endpoint_and_offline_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.connect", lambda _profile: object())
    monkeypatch.setattr("app.main.close_quietly", lambda _connection: None)
    monkeypatch.setattr("app.main.list_tables", lambda _c, _p, limit: [f"T{index}" for index in range(limit)])
    app.state.config = online_config(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        body = c.get("/api/db/mysql/tables").json()
        assert len(body["tables"]) == db.TABLE_LIST_LIMIT
        assert body["truncated"] is True
        offline = c.get("/api/db/oracle/tables")
        assert offline.status_code == 409
        check = c.post("/api/files/missing/insert/check", json={"dialect": "oracle", "table_name": "T"})
        assert check.status_code == 409


def test_execute_load_streams_only_matched_columns_into_stored_names(flat_parquet_path, monkeypatch):
    schema = schema_of(flat_parquet_path)
    match = match_columns(schema, [stored("Active", data_type="tinyint(1)"), stored("ID", data_type="bigint")])
    table = plan_insert_table(schema, table_name="pb_test_insert", dialect="mysql", chosen=selected_matches(match, None))
    inserted = []
    counts = {"value": 5}
    monkeypatch.setattr(jobs, "connect", lambda _profile: type("C", (), {"commit": lambda self: None, "close": lambda self: None})())
    monkeypatch.setattr(jobs, "table_exists", lambda _c, _p, name, *, quoted: quoted and name == "pb_test_insert")
    monkeypatch.setattr(jobs, "table_columns", lambda *_args, **_kwargs: {"ID", "Active", "NOTE"})
    monkeypatch.setattr(jobs, "count_table", lambda *_args, **_kwargs: counts["value"])
    statements = []
    monkeypatch.setattr(jobs, "_execute_statement", lambda _c, _p, sql: statements.append(sql))

    def capture(_connection, planned, rows):
        inserted.append((load_mysql.insert_sql(planned), list(rows)))
        counts["value"] += len(rows)
        return len(rows)

    monkeypatch.setattr(load_mysql, "insert_batch", capture)
    result = jobs.execute_load(
        path=flat_parquet_path,
        tables=[table],
        profile=profile("mysql"),
        source_rows=3,
        batch_size=2,
        existing_policy="insert_only",
        cancel_event=threading.Event(),
        progress=lambda *_args: None,
    )
    assert statements == []
    assert [sql for sql, _rows in inserted] == ["INSERT INTO `pb_test_insert` (`ID`, `Active`) VALUES (%s, %s)"] * 2
    assert [row for _sql, rows in inserted for row in rows] == [(1, 1), (2, 0), (3, 1)]
    report = result["tables"][0]
    assert (report["expected_rows"], report["actual_rows"], report["matched"]) == (3, 3, True)
