"""Command-line entry points for ParqBridge.

All Parquet inspection, planning, DDL, validation, and loading behavior is
delegated to the same core functions used by the web API.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from app.core.config import AppConfig, load_config
from app.core.dialects import DIALECT_LABELS, SUPPORTED_DIALECTS
from app.core.errors import PBError, warning
from app.core.files import SelectedFile, resolve_allowed_path, validate_parquet_magic
from app.core.inspect import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, inspect_parquet, preview_parquet


_I18N_DIR = Path(__file__).resolve().parent / "i18n"
_CATALOGS: dict[str, dict[str, str]] = {}


def _catalog(lang: str) -> dict[str, str]:
    if lang not in _CATALOGS:
        with (_I18N_DIR / f"{lang}.json").open("r", encoding="utf-8") as source:
            _CATALOGS[lang] = json.load(source)
    return _CATALOGS[lang]


def _load_config() -> AppConfig:
    path = os.environ.get("PARQBRIDGE_CONFIG")
    return load_config(Path(path) if path else None)


def _message(key: str, lang: str, params: dict[str, Any] | None = None) -> str:
    template = _catalog(lang).get(key, key)
    if not params:
        return template
    try:
        return template.format_map(params)
    except (KeyError, ValueError):
        return template


def _error_message(code: str, lang: str, params: dict[str, Any] | None = None) -> str:
    return _message(f"error.{code}", lang, params)


def _print_warnings(items: Sequence[dict[str, Any]], lang: str) -> None:
    for item in items:
        print(
            _message(f"warning.{item['code']}", lang, item.get("params") or {}),
            file=sys.stderr,
        )


def _selected_file(raw_path: str, config: AppConfig) -> SelectedFile:
    path = resolve_allowed_path(raw_path, config.server.allowed_roots)
    if path.suffix.lower() != ".parquet":
        raise PBError("E_PARQUET_INVALID")
    validate_parquet_magic(path)
    return SelectedFile("cli", path, path.name, "local")


def _context(config: AppConfig) -> SimpleNamespace:
    """Provide the config interface expected by the web API's shared planner."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def _schema_tree_lines(fields: Sequence[dict[str, Any]], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for index, node in enumerate(fields):
        last = index == len(fields) - 1
        branch = "└─ " if last else "├─ "
        nullable = "nullable" if node.get("nullable") else "required"
        lines.append(f"{prefix}{branch}{node['name']}: {node['type']} [{node['kind']}, {nullable}]")
        children = node.get("children") or []
        if children:
            lines.extend(_schema_tree_lines(children, prefix + ("   " if last else "│  ")))
    return lines


def _inspect(args: argparse.Namespace) -> int:
    config = _load_config()
    selected = _selected_file(args.parquet, config)
    result = inspect_parquet(selected.path)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    metadata = result["metadata"]
    classification = result["classification"]
    kind = _message(f"inspect.kind.{classification['kind']}", args.lang)
    print(f"File: {selected.path}")
    print(f"Rows: {metadata['num_rows']}")
    print(f"Row groups: {metadata['num_row_groups']}")
    print(f"File size: {metadata['file_size']} bytes")
    print(f"Compression: {', '.join(metadata['compression']) or '-'}")
    print(f"Created by: {metadata['created_by'] or '-'}")
    print(f"Schema: {kind}")
    if classification["nested_columns"]:
        print(f"Nested columns: {json.dumps(classification['nested_columns'], ensure_ascii=False)}")
    print("Column tree:")
    print("\n".join(_schema_tree_lines(result["schema"]["fields"])))
    return 0


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return value.replace("\r", "\\r").replace("\n", "\\n")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _preview_table(columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    cells = [[_cell(name) for name in columns]]
    cells.extend([_cell(row.get(name)) for name in columns] for row in rows)
    widths = [min(60, max((len(row[index]) for row in cells), default=0)) for index in range(len(columns))]

    def render(value: str, width: int) -> str:
        if len(value) > width:
            value = value[: max(0, width - 1)] + "…"
        return value.ljust(width)

    if not columns:
        print("(no columns)")
        return
    print(" | ".join(render(value, widths[index]) for index, value in enumerate(cells[0])))
    print("-+-".join("-" * width for width in widths))
    for row in cells[1:]:
        print(" | ".join(render(value, widths[index]) for index, value in enumerate(row)))


def _preview(args: argparse.Namespace) -> int:
    config = _load_config()
    selected = _selected_file(args.parquet, config)
    result = preview_parquet(selected.path, offset=args.offset, limit=args.limit)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    _preview_table(result["columns"], result["rows"])
    count = _message(
        "inspect.preview_count",
        args.lang,
        {
            "first": args.offset + 1 if result["rows"] else 0,
            "last": args.offset + len(result["rows"]),
            "total": result["total_rows"],
        },
    )
    print(count)
    return 0


def _ddl_options(args: argparse.Namespace):
    from app.main import DdlOptions

    return DdlOptions(
        dialect=args.dialect,
        mode=args.mode,
        table_name=args.table,
        string_strategy=args.strings,
        length_scan=args.length_scan,
        parent_key=args.parent_key,
        explode_column=args.explode_column,
        json_check=getattr(args, "json_check", False),
    )


def _ddl(args: argparse.Namespace) -> int:
    from app.main import _build_ddl_result

    config = _load_config()
    selected = _selected_file(args.parquet, config)
    result = _build_ddl_result(
        selected,
        _ddl_options(args),
        _context(config),
        include_preview=False,
    )
    _print_warnings(result["warnings"], args.lang)
    if args.output:
        _write_ddl_output(args.output, selected.path, result["ddl"], force=args.force)
    else:
        sys.stdout.write(result["ddl"])
    return 0


def _write_ddl_output(output: str, input_path: Path, ddl: str, *, force: bool) -> None:
    requested_path = Path(output)
    try:
        source_path = input_path.resolve(strict=True)
        output_path = requested_path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise PBError("E_DDL_OUTPUT_PROTECTED", path=output) from None

    same_input = output_path == source_path
    if output_path.exists():
        try:
            same_input = same_input or os.path.samefile(output_path, source_path)
        except OSError:
            raise PBError("E_DDL_OUTPUT_PROTECTED", path=output) from None
    if same_input:
        raise PBError("E_DDL_OUTPUT_PROTECTED", path=output)
    if requested_path.suffix.casefold() == ".parquet" or output_path.suffix.casefold() == ".parquet":
        raise PBError("E_DDL_OUTPUT_PROTECTED", path=output)

    if output_path.exists():
        if output_path.is_file():
            try:
                with output_path.open("rb") as existing:
                    if existing.read(4) == b"PAR1":
                        raise PBError("E_DDL_OUTPUT_PROTECTED", path=output)
            except OSError as exc:
                raise PBError("E_INTERNAL", type=type(exc).__name__) from None
        if not force:
            raise PBError("E_DDL_OUTPUT_EXISTS", path=output)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(ddl.encode("utf-8"))
        os.replace(temporary_path, output_path)
    except OSError as exc:
        raise PBError("E_INTERNAL", type=type(exc).__name__) from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _print_progress(snapshot: dict[str, Any], previous: tuple[tuple[Any, ...], ...] | None, lang: str):
    progress = tuple(
        (item["table"], item["rows_done"], item["rows_expected"])
        for item in snapshot.get("progress", [])
    )
    if progress != previous:
        for table, done, expected in progress:
            if expected is None:
                rendered = str(done)
            else:
                rendered = _message("load.rows_progress", lang, {"done": done, "expected": expected})
            print(f"{table}: {rendered}", file=sys.stderr)
    return progress


def _print_load_result(snapshot: dict[str, Any], lang: str) -> None:
    status = snapshot["status"]
    print(_message(f"load.status.{status}", lang))
    result = snapshot.get("result") or {}
    for table in result.get("tables", []):
        print(
            _message(
                "load.count_result",
                lang,
                {
                    "expected": table.get("expected_rows"),
                    "actual": table.get("actual_rows"),
                    "database": table.get("database_count"),
                },
            )
            + f" [{table.get('table')}]"
        )
    if result:
        print(
            f"Committed batches: {result.get('committed_batches', 0)}; "
            f"committed rows: {result.get('committed_rows', 0)}; "
            f"partial commits kept: {str(bool(result.get('partial_commits_kept'))).lower()}"
        )


def _load(args: argparse.Namespace) -> int:
    from app.core.jobs import LoadJobManager, validate_load_request
    from app.main import _build_models, _warning_for_dialect

    config = _load_config()
    selected = _selected_file(args.parquet, config)
    profile = config.db.get(args.dialect)
    if profile is None:
        raise PBError("E_DB_NOT_CONFIGURED")
    if not profile.online:
        raise PBError(profile.offline_reason or "E_DB_NOT_CONFIGURED", **profile.offline_params)

    options = _ddl_options(args)
    models, source_rows, reserved_warnings, _schema = _build_models(selected, options, _context(config))
    validate_load_request(models, args.if_exists, args.confirm_table)
    warnings = list(reserved_warnings)
    for table in models:
        warnings.extend(item for item in table.warnings if _warning_for_dialect(item, args.dialect))
    _print_warnings(_dedupe_warnings(warnings), args.lang)

    batch_size = args.batch_size or config.load.batch_size
    manager = LoadJobManager(max_workers=1)
    try:
        job = manager.submit(
            path=selected.path,
            tables=models,
            profile=profile,
            source_rows=source_rows,
            batch_size=batch_size,
            existing_policy=args.if_exists,
            confirm_table_name=args.confirm_table,
        )
        previous = None
        interrupted = False
        while True:
            try:
                snapshot = manager.get(job["job_id"])
                previous = _print_progress(snapshot, previous, args.lang)
                if snapshot["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.1)
            except KeyboardInterrupt:
                if not interrupted:
                    interrupted = True
                    manager.cancel(job["job_id"])
                # The existing job runner observes cancellation at a batch boundary;
                # keep polling until its committed-row summary is available.
                continue

        if snapshot["status"] == "failed":
            error = snapshot.get("error") or {"code": "E_INTERNAL", "params": {}}
            print(_error_message(error["code"], args.lang, error.get("params") or {}), file=sys.stderr)
            return 1

        _print_load_result(snapshot, args.lang)
        if snapshot["status"] == "cancelled" or interrupted:
            return 130
        reports = (snapshot.get("result") or {}).get("tables", [])
        return 0 if all(item.get("matched", True) for item in reports) else 1
    finally:
        manager.shutdown()


def _dedupe_warnings(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["code"], repr(sorted((item.get("params") or {}).items())))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _config_check(args: argparse.Namespace) -> int:
    config = _load_config()
    print("Config sources: " + (", ".join(config.sources) if config.sources else "(defaults)"))
    _print_warnings(config.warnings, args.lang)
    for dialect in SUPPORTED_DIALECTS:
        label = DIALECT_LABELS.get(dialect, dialect)
        profile = config.db.get(dialect)
        if profile is None:
            print(f"{label}: {_message('status.offline', args.lang)} — " + _error_message("E_DB_NOT_CONFIGURED", args.lang))
            continue
        state = _message("status.online" if profile.online else "status.offline", args.lang)
        if profile.online:
            print(f"{label}: {state} · {profile.host}:{profile.port}/{profile.target} · user={profile.user}")
        else:
            reason = _error_message(profile.offline_reason or "E_DB_NOT_CONFIGURED", args.lang, profile.offline_params)
            print(f"{label}: {state} — {reason}")
    return 0


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    config = _load_config()
    host = args.host or config.server.host
    port = args.port or config.server.port
    warnings = list(config.warnings)
    if not _is_loopback(host) and not any(item.get("code") == "W_BIND_ALL_INTERFACES" for item in warnings):
        warnings.append(warning("W_BIND_ALL_INTERFACES", host=host))
    _print_warnings(warnings, args.lang)
    uvicorn.run("app.main:app", host=host, port=port)
    return 0


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _page_limit(value: str) -> int:
    number = _positive_int(value)
    if number > MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_PAGE_SIZE}")
    return number


def _offset(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parqbridge", allow_abbrev=False)
    parser.add_argument("--lang", choices=("ko", "en"), default="ko")
    parser.add_argument("--host", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=_port, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")

    def add_lang(command: argparse.ArgumentParser) -> None:
        command.add_argument("--lang", choices=("ko", "en"), default=argparse.SUPPRESS)

    serve = commands.add_parser("serve", allow_abbrev=False, help="start the local web server")
    add_lang(serve)
    serve.add_argument("--host", default=argparse.SUPPRESS, help="override the configured bind host")
    serve.add_argument("--port", type=_port, default=argparse.SUPPRESS, help="override the configured port")
    serve.set_defaults(handler=_serve)

    inspect = commands.add_parser("inspect", allow_abbrev=False, help="inspect Parquet metadata and schema")
    add_lang(inspect)
    inspect.add_argument("parquet")
    inspect.add_argument("--json", dest="as_json", action="store_true", help="write JSON")
    inspect.set_defaults(handler=_inspect)

    preview = commands.add_parser("preview", allow_abbrev=False, help="preview Parquet rows")
    add_lang(preview)
    preview.add_argument("parquet")
    preview.add_argument("--offset", type=_offset, default=0)
    preview.add_argument("--limit", type=_page_limit, default=DEFAULT_PAGE_SIZE)
    preview.add_argument("--json", dest="as_json", action="store_true", help="write JSON")
    preview.set_defaults(handler=_preview)

    ddl = commands.add_parser("ddl", allow_abbrev=False, help="generate DDL")
    add_lang(ddl)
    ddl.add_argument("parquet")
    ddl.add_argument("--dialect", required=True, choices=SUPPORTED_DIALECTS)
    ddl.add_argument("--mode", choices=("none", "child", "explode"), default="none")
    ddl.add_argument("--table")
    ddl.add_argument("--strings", choices=("sample", "max", "lob"), default="sample")
    ddl.add_argument("--length-scan", choices=("full", "sample"), default="full")
    ddl.add_argument("--parent-key")
    ddl.add_argument("--explode-column")
    ddl.add_argument("--json-check", action="store_true", help="add Oracle IS JSON checks")
    ddl.add_argument("-o", "--output")
    ddl.add_argument("--force", action="store_true", help="replace an existing non-Parquet output file")
    ddl.set_defaults(handler=_ddl)

    load = commands.add_parser("load", allow_abbrev=False, help="create tables and stream Parquet rows")
    add_lang(load)
    load.add_argument("parquet")
    load.add_argument("--dialect", required=True, choices=SUPPORTED_DIALECTS)
    load.add_argument("--table", required=True)
    load.add_argument("--mode", choices=("none", "child", "explode"), default="none")
    load.add_argument("--strings", choices=("sample", "max", "lob"), default="sample")
    load.add_argument("--length-scan", choices=("full", "sample"), default="full")
    load.add_argument("--parent-key")
    load.add_argument("--explode-column")
    load.add_argument("--if-exists", choices=("fail", "truncate", "append", "drop_create"), default="fail")
    load.add_argument("--confirm-table")
    load.add_argument("--batch-size", type=_positive_int)
    load.set_defaults(handler=_load)

    config_cmd = commands.add_parser("config", allow_abbrev=False, help="check configuration")
    add_lang(config_cmd)
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", allow_abbrev=False, help="show configured and offline databases")
    add_lang(check)
    check.set_defaults(handler=_config_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        # Keep the historical no-argument invocation: `python -m app` starts serve.
        args.command = "serve"
        args.handler = _serve
        args.lang = getattr(args, "lang", "ko")
    try:
        return args.handler(args)
    except PBError as exc:
        print(_error_message(exc.code, args.lang, exc.params), file=sys.stderr)
        return 2 if exc.code in {
            "E_CONFIRM_REQUIRED",
            "E_DDL_OUTPUT_EXISTS",
            "E_DDL_OUTPUT_PROTECTED",
        } else 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(_error_message("E_INTERNAL", args.lang, {"type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
