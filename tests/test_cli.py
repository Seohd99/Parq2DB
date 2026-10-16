from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from app import cli
from app.core.config import load_config
from app.core.dialects import SUPPORTED_DIALECTS


TEST_PASSWORD = "cli-test-only-secret-value"


def test_message_formats_missing_optional_parameters_cleanly():
    assert cli._message(
        "error.E_CONFIG_INVALID",
        "en",
        {"file": "settings.toml", "detail": "invalid TOML"},
    ) == "Config error: settings.toml invalid TOML"
    assert cli._message(
        "error.E_CONFIG_INVALID",
        "en",
        {"file": "settings.toml", "key": "db.port", "expected": "positive int"},
    ) == "Config error: settings.toml db.port (expected: positive int)"


def configure_cli(
    monkeypatch,
    tmp_path: Path,
    *,
    root: Path | None = None,
    online: bool = False,
    dialect: str = "mysql",
):
    allowed = root or tmp_path
    config_path = tmp_path / "cli-config.toml"
    config_text = (
        "[server]\n"
        f"allowed_roots = [{json.dumps(str(allowed))}]\n"
        "\n[load]\n"
        "batch_size = 2\n"
    )
    if online:
        if dialect == "postgres":
            config_text += (
                "\n[db.postgres]\n"
                'host = "localhost"\n'
                'database = "unit_test_db"\n'
                'user = "unit_test_user"\n'
                'password_env = "PARQBRIDGE_TEST_PASSWORD"\n'
            )
        elif dialect == "oracle":
            config_text += (
                "\n[db.oracle]\n"
                'host = "localhost"\n'
                "port = 1521\n"
                'service_name = "unit_test_service"\n'
                'user = "unit_test_user"\n'
                'password_env = "PARQBRIDGE_TEST_PASSWORD"\n'
            )
        else:
            config_text += (
                "\n[db.mysql]\n"
                'host = "localhost"\n'
                "port = 3306\n"
                'database = "unit_test_db"\n'
                'user = "unit_test_user"\n'
                'password_env = "PARQBRIDGE_TEST_PASSWORD"\n'
            )
    config_path.write_text(config_text, encoding="utf-8")
    env = {"PARQBRIDGE_TEST_PASSWORD": TEST_PASSWORD} if online else {}
    monkeypatch.setattr(
        cli,
        "_load_config",
        lambda: load_config(config_path, local_path=tmp_path / "no-local.toml", env=env),
    )
    return config_path


class FakeDriverError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def execute(self, sql, params=None):
        upper = sql.upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            name = params[-1]
            self.result = (1,) if name in self.connection.existing else None
        elif upper.startswith("SELECT COUNT(*) FROM"):
            name = re.sub(r"[\s;]+$", "", sql.split("FROM", 1)[1]).strip().strip('`"')
            if self.connection.dialect == "postgres":
                name = name.lower()
            self.result = (self.connection.row_counts.get(name, 0),)
        elif upper.startswith("CREATE TABLE"):
            name = re.match(r"CREATE TABLE\s+([^\s(]+)", sql, re.I).group(1).strip('`"')
            if self.connection.dialect == "postgres":
                name = name.lower()
            self.connection.existing.add(name)
            self.connection.row_counts.setdefault(name, 0)
        elif upper.startswith("INSERT INTO") and params is not None:
            if self.connection.fail_value is not None and params[0] == self.connection.fail_value:
                raise FakeDriverError(1062, "driver message must never be shown")
        elif upper.startswith("DROP TABLE"):
            name = sql.split()[2].strip('`"')
            self.connection.existing.discard(name)
            self.connection.row_counts.pop(name, None)
        self.sql = sql

    def copy(self, sql):
        # Emulate psycopg's COPY context: rows written inside it arrive as one batch.
        cursor = self
        insert = sql.replace("COPY", "INSERT INTO", 1)

        class Copy:
            def __init__(self):
                self.rows = []

            def __enter__(self):
                return self

            def write_row(self, row):
                self.rows.append(row)

            def __exit__(self, exc_type, *_):
                if exc_type is None:
                    cursor.executemany(insert, self.rows)

        return Copy()

    def executemany(self, sql, rows):
        rows = list(rows)
        if self.connection.pause_on_insert:
            self.connection.insert_started.set()
            if not self.connection.allow_insert.wait(5):
                raise RuntimeError("test insert barrier timed out")
        if self.connection.fail_value is not None and any(row[0] == self.connection.fail_value for row in rows):
            raise FakeDriverError(1062, "driver message must never be shown")
        table = re.match(r"INSERT INTO\s+([^\s(]+)", sql, re.I).group(1).strip('`"')
        if self.connection.dialect == "postgres":
            table = table.lower()
        self.connection.pending.setdefault(table, []).extend(rows)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result or []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, *, fail_value=None, pause_on_insert=False, dialect="mysql"):
        import threading

        self.existing = set()
        self.row_counts = {}
        self.pending = {}
        self.fail_value = fail_value
        self.pause_on_insert = pause_on_insert
        self.dialect = dialect
        self.insert_started = threading.Event()
        self.allow_insert = threading.Event()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        for table, rows in self.pending.items():
            self.row_counts[table] = self.row_counts.get(table, 0) + len(rows)
        self.pending.clear()

    def rollback(self):
        self.pending.clear()

    def close(self):
        pass


def test_inspect_and_preview_human_and_json(monkeypatch, capsys, flat_parquet_path):
    configure_cli(monkeypatch, flat_parquet_path.parent)

    assert cli.main(["inspect", str(flat_parquet_path), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["metadata"]["num_rows"] == 3
    assert inspected["metadata"]["num_row_groups"] == 3
    assert inspected["schema"]["fields"][0]["name"] == "id"

    assert cli.main(["inspect", str(flat_parquet_path), "--lang", "en"]) == 0
    output = capsys.readouterr().out
    assert "Rows: 3" in output
    assert "Row groups: 3" in output
    assert "Column tree:" in output and "id: int64" in output

    assert cli.main(["preview", str(flat_parquet_path), "--offset", "1", "--limit", "1", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["offset"] == 1 and preview["rows"][0]["id"] == 2

    assert cli.main(["preview", str(flat_parquet_path), "--limit", "2", "--lang", "en"]) == 0
    output = capsys.readouterr().out
    assert "id" in output and "한글" in output and "Rows 1–2 of 3" in output


@pytest.mark.parametrize("dialect", ["oracle", "mysql"])
def test_ddl_matches_existing_goldens(monkeypatch, capsys, parquet_path, dialect):
    configure_cli(monkeypatch, parquet_path.parent)
    argv = [
        "ddl", str(parquet_path), "--dialect", dialect, "--table", "PB_TEST_DATA",
        "--strings", "sample", "--length-scan", "sample",
    ]
    if dialect == "oracle":
        argv.append("--json-check")
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == (Path(__file__).parent / "golden" / f"none_{dialect}.sql").read_text(encoding="utf-8")


def test_postgres_is_available_from_dialect_constants(monkeypatch, capsys, flat_parquet_path):
    if "postgres" not in SUPPORTED_DIALECTS:
        pytest.skip("PostgreSQL dialect has not been added")
    configure_cli(monkeypatch, flat_parquet_path.parent)
    assert cli.main(["ddl", str(flat_parquet_path), "--dialect", "postgres", "--table", "PB_TEST_PG"]) == 0
    output = capsys.readouterr().out
    assert "CREATE TABLE" in output and "PB_TEST_PG" in output


def test_invalid_dialect_is_an_argparse_usage_error(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["ddl", "sample.parquet", "--dialect", "not-a-dialect"])
    assert caught.value.code == 2
    assert "--dialect" in capsys.readouterr().err


def test_length_scan_cli_defaults_to_full_and_accepts_sample():
    default = cli._parser().parse_args(["ddl", "sample.parquet", "--dialect", "oracle"])
    sampled = cli._parser().parse_args([
        "load", "sample.parquet", "--dialect", "mysql", "--table", "PB_TEST_SCAN",
        "--length-scan", "sample",
    ])
    assert default.length_scan == "full"
    assert sampled.length_scan == "sample"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"PARQBRIDGE_LANG": "ko_KR", "LC_ALL": "en_US"}, "ko"),
        ({"PARQBRIDGE_LANG": "en_US", "LANG": "ko_KR"}, "en"),
        ({"LC_ALL": "ko_KR.UTF-8", "LC_MESSAGES": "en_US"}, "ko"),
        ({"LC_MESSAGES": "ko_KR.UTF-8", "LANG": "en_US"}, "ko"),
        ({"LANG": "ko_KR.UTF-8"}, "ko"),
        ({"PARQBRIDGE_LANG": "fr_FR", "LANG": "ko_KR.UTF-8"}, "en"),
        ({"LC_ALL": "fr_FR.UTF-8", "LANG": "ko_KR.UTF-8"}, "en"),
        ({"LANG": "fr_FR.UTF-8"}, "en"),
        ({}, "en"),
    ],
)
def test_default_cli_language_environment_precedence(environ, expected):
    assert cli._default_lang(environ, windows=False) == expected


def test_default_cli_language_uses_windows_preferences_when_available():
    assert cli._default_lang({}, system_locale="Korean_Korea", windows=True) == "ko"
    assert cli._default_lang(
        {}, system_locale="fr_FR", ui_language=0x0412, windows=True,
    ) == "ko"


def test_cli_language_option_overrides_environment(monkeypatch):
    monkeypatch.setenv("PARQBRIDGE_LANG", "en")

    assert cli._parser().parse_args(["config", "check"]).lang == "en"
    assert cli._parser().parse_args(["--lang", "ko", "config", "check"]).lang == "ko"
    assert cli._parser().parse_args(["config", "check", "--lang", "ko"]).lang == "ko"


def test_cli_language_choices_follow_catalogs():
    parser = cli._parser()
    codes = cli.language_codes()
    assert parser._option_string_actions["--lang"].choices == codes
    commands = next(action for action in parser._actions if action.dest == "command")
    assert commands.choices["inspect"]._option_string_actions["--lang"].choices == codes


def test_ddl_output_file_and_warnings_stderr(monkeypatch, capsys, parquet_path, tmp_path):
    configure_cli(monkeypatch, parquet_path.parent)
    output_path = tmp_path / "plan.sql"
    assert cli.main([
        "ddl", str(parquet_path), "--dialect", "mysql", "--table", "PB_TEST_DATA",
        "--length-scan", "sample", "-o", str(output_path)
    ]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "LEVEL_2" in captured.err
    assert "CREATE TABLE PB_TEST_DATA" in output_path.read_text(encoding="utf-8")


def test_ddl_output_rejects_source_and_parquet_targets_without_modifying_source(
    monkeypatch, capsys, flat_parquet_path, tmp_path
):
    configure_cli(monkeypatch, flat_parquet_path.parent)
    original_bytes = flat_parquet_path.read_bytes()
    original_mtime = flat_parquet_path.stat().st_mtime_ns

    assert cli.main([
        "ddl", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_DATA",
        "-o", str(flat_parquet_path),
    ]) == 2
    assert "Parquet" in capsys.readouterr().err
    assert flat_parquet_path.read_bytes() == original_bytes
    assert flat_parquet_path.stat().st_mtime_ns == original_mtime

    parquet_target = tmp_path / "ddl-output.parquet"
    assert cli.main([
        "ddl", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_DATA",
        "-o", str(parquet_target),
    ]) == 2
    assert "Parquet" in capsys.readouterr().err
    assert not parquet_target.exists()


def test_ddl_output_requires_force_and_atomically_replaces_existing_non_parquet_file(
    monkeypatch, capsys, flat_parquet_path, tmp_path
):
    configure_cli(monkeypatch, flat_parquet_path.parent)
    output_path = tmp_path / "plan.sql"
    original_bytes = b"keep existing contents"
    output_path.write_bytes(original_bytes)
    original_mtime = output_path.stat().st_mtime_ns
    argv = [
        "ddl", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_DATA",
        "-o", str(output_path),
    ]

    assert cli.main(argv) == 2
    assert "--force" in capsys.readouterr().err
    assert output_path.read_bytes() == original_bytes
    assert output_path.stat().st_mtime_ns == original_mtime

    assert cli.main([*argv, "--force"]) == 0
    assert "CREATE TABLE PB_TEST_DATA" in output_path.read_text(encoding="utf-8")


def test_ddl_output_protects_parquet_magic_even_with_force(monkeypatch, capsys, flat_parquet_path, tmp_path):
    configure_cli(monkeypatch, flat_parquet_path.parent)
    output_path = tmp_path / "not-obviously-parquet.sql"
    original_bytes = b"PAR1 protected bytes"
    output_path.write_bytes(original_bytes)
    original_mtime = output_path.stat().st_mtime_ns

    assert cli.main([
        "ddl", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_DATA",
        "-o", str(output_path), "--force",
    ]) == 2
    assert "Parquet" in capsys.readouterr().err
    assert output_path.read_bytes() == original_bytes
    assert output_path.stat().st_mtime_ns == original_mtime


def test_load_success_and_failure_location(monkeypatch, capsys, flat_parquet_path):
    configure_cli(monkeypatch, flat_parquet_path.parent, online=True)
    connection = FakeConnection()
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_LOAD", "--batch-size", "2",
        "--lang", "ko",
    ]) == 0
    output = capsys.readouterr()
    assert "적재 완료" in output.out
    assert "예상 3행; 적재 3행" in output.out
    assert "PB_TEST_LOAD" in output.err

    connection = FakeConnection(fail_value=3)
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_FAIL", "--batch-size", "2",
        "--lang", "ko",
    ]) == 1
    output = capsys.readouterr()
    assert "3번째 행" in output.err
    assert "1개와 행 2개는 유지됩니다" in output.err
    assert "driver message" not in output.err


def test_postgres_load_uses_shared_job_dispatch_when_supported(monkeypatch, capsys, flat_parquet_path):
    if "postgres" not in SUPPORTED_DIALECTS:
        pytest.skip("PostgreSQL dialect has not been added")
    configure_cli(monkeypatch, flat_parquet_path.parent, online=True, dialect="postgres")
    connection = FakeConnection(dialect="postgres")
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "postgres", "--table", "PB_TEST_PG_LOAD", "--batch-size", "2",
        "--lang", "ko",
    ]) == 0
    output = capsys.readouterr()
    assert "적재 완료" in output.out
    assert "예상 3행; 적재 3행" in output.out


def test_load_keyboard_interrupt_uses_cooperative_cancel_and_keeps_committed_batch(
    monkeypatch, capsys, flat_parquet_path
):
    from app.core.jobs import LoadJobManager

    configure_cli(monkeypatch, flat_parquet_path.parent, online=True)
    connection = FakeConnection(pause_on_insert=True)
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    real_sleep = time.sleep
    did_interrupt = False

    def interrupt_after_insert_started(_seconds):
        nonlocal did_interrupt
        if not did_interrupt:
            did_interrupt = True
            assert connection.insert_started.wait(5)
            raise KeyboardInterrupt
        real_sleep(0.01)

    class CancelAwareManager(LoadJobManager):
        def cancel(self, job_id):
            connection.allow_insert.set()
            return super().cancel(job_id)

    monkeypatch.setattr("app.core.jobs.LoadJobManager", CancelAwareManager)
    monkeypatch.setattr(cli.time, "sleep", interrupt_after_insert_started)
    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_CANCEL", "--batch-size", "2",
        "--lang", "ko",
    ]) == 130
    output = capsys.readouterr()
    assert "취소됨" in output.out
    assert "Committed batches: 1" in output.out
    assert "committed rows: 2" in output.out


@pytest.mark.parametrize("confirm", [None, "PB_TEST_OTHER"])
def test_destructive_load_requires_exact_parent_confirmation(monkeypatch, capsys, flat_parquet_path, confirm):
    configure_cli(monkeypatch, flat_parquet_path.parent, online=True)
    args = [
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_CONFIRM", "--if-exists", "truncate"
    ]
    if confirm is not None:
        args.extend(["--confirm-table", confirm])
    assert cli.main(args) == 2
    assert "PB_TEST_CONFIRM" in capsys.readouterr().err


def test_destructive_oracle_confirmation_error_uses_normalized_plan_table_before_load_connect(
    monkeypatch, capsys, flat_parquet_path
):
    configure_cli(monkeypatch, flat_parquet_path.parent, online=True, dialect="oracle")
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.main._build_models",
        lambda _selected, _options, _context: ([SimpleNamespace(name="PB_TEST_NORMALIZED", warnings=())], 3, [], None),
    )
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: pytest.fail("load connected before validation"))

    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "oracle", "--table", "pb_test_normalized",
        "--if-exists", "truncate",
    ]) == 2
    error = capsys.readouterr().err
    assert "PB_TEST_NORMALIZED" in error
    assert "pb_test_normalized" not in error


def test_load_rejects_path_outside_allowed_roots(monkeypatch, capsys, flat_parquet_path, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    configure_cli(monkeypatch, tmp_path, root=allowed, online=True)
    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_OUTSIDE", "--lang", "ko",
    ]) == 1
    assert "허용된 폴더 안" in capsys.readouterr().err


def test_config_check_does_not_print_password(monkeypatch, capsys, tmp_path):
    configure_cli(monkeypatch, tmp_path, online=True)
    assert cli.main(["config", "check"]) == 0
    output = capsys.readouterr().out
    assert "localhost:3306/unit_test_db" in output
    assert "unit_test_user" in output
    assert TEST_PASSWORD not in output


def test_no_arguments_still_starts_configured_server(monkeypatch, tmp_path):
    from app import __main__ as entry
    import uvicorn

    configure_cli(monkeypatch, tmp_path)
    started = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(app=app, **kwargs))
    assert entry.main([]) == 0
    assert started == {"app": "app.main:app", "host": "127.0.0.1", "port": 8765}


def test_serve_non_loopback_override_warns(monkeypatch, capsys, tmp_path):
    configure_cli(monkeypatch, tmp_path)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8766", "--lang", "ko"]) == 0
    assert "모든 인터페이스" in capsys.readouterr().err


def test_load_insert_only_with_selected_columns_skips_create(monkeypatch, capsys, flat_parquet_path):
    configure_cli(monkeypatch, flat_parquet_path.parent, online=True)
    connection = FakeConnection()
    connection.existing.add("PB_TEST_LOAD")
    monkeypatch.setattr("app.core.jobs.connect", lambda _profile: connection)
    monkeypatch.setattr("app.core.insert_target.connect", lambda _profile: connection)
    monkeypatch.setattr("app.core.insert_target.find_table", lambda _conn, _profile, name: name.upper())
    stored = [
        {"name": name, "data_type": "varchar(20)", "nullable": True, "has_default": False, "generated": False}
        for name in ("ID", "NAME", "NOTE")
    ]
    monkeypatch.setattr("app.core.insert_target.describe_columns", lambda *_args: stored)
    monkeypatch.setattr("app.core.jobs.table_columns", lambda *_args, **_kwargs: {"ID", "NAME", "NOTE"})
    statements = []
    monkeypatch.setattr("app.core.jobs._execute_statement", lambda _conn, _profile, sql: statements.append(sql))
    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "pb_test_load",
        "--column", "id", "--column", "name", "--insert-only", "--lang", "en",
    ]) == 0
    assert statements == []
    assert connection.row_counts["PB_TEST_LOAD"] == 3
    # The file column "active" has no table column, so it is skipped with a warning.
    assert "active" in capsys.readouterr().err

    assert cli.main([
        "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_LOAD",
        "--insert-only", "--mode", "child", "--lang", "en",
    ]) == 1
    capsys.readouterr()

    with pytest.raises(SystemExit):
        cli.main([
            "load", str(flat_parquet_path), "--dialect", "mysql", "--table", "PB_TEST_LOAD",
            "--insert-only", "--if-exists", "append",
        ])
