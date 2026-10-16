# ParqBridge agent and contributor guide

## Project

ParqBridge is a local web and CLI tool for inspecting one Parquet file, generating DDL for Oracle 12.2+ (designed around 19c), MySQL 8+, and PostgreSQL 16+, and optionally creating tables and loading rows when a database profile is configured.

## Repository map

- `app/main.py`, `app/cli.py`: FastAPI web entry point and command-line interface.
- `app/core/config.py`, `files.py`: configuration, database profiles, file registration, uploads, and allowed-root checks.
- `app/core/inspect.py`, `flatten.py`, `model.py`: Parquet inspection, preview and flatten plans, and shared table/column models.
- `app/core/typemap.py`, `ddl_oracle.py`, `ddl_mysql.py`, `ddl_postgres.py`, `dialects.py`: type mapping, DDL emitters, and supported database dialects. See the **Main type mapping** section in `README.md` for the mapping table.
- `app/core/db.py`, `load_oracle.py`, `load_mysql.py`, `load_postgres.py`, `jobs.py`: database connections, batch inserts, background load jobs, progress, cancellation, and row-count checks.
- `app/core/errors.py`, `app/core/languages.py`, `app/i18n/*.json`: error and warning codes, catalog discovery, and translated messages.
- `app/static/`: HTML, CSS, and vanilla JavaScript; there is no frontend build chain.
- `config/parqbridge.example.toml`: safe configuration template. Local configuration files are ignored by git.
- `tests/`, `tests/golden/`: unit and optional database integration tests, plus DDL golden files.
- `tools/`: developer utilities, including large-file measurement tooling.
- `docs/DESIGN.md`: current architecture and design constraints.

## Setup and run

Use Python 3.14 and a project-local virtual environment. Do not install dependencies or change system settings on a user's behalf without their approval.

Recommended (uv; `.python-version` selects Python 3.14, `uv.lock` pins every package):

```bash
uv run --no-dev parqbridge   # start the app
uv run pytest -q -rs         # tests need the dev dependency group
```

Alternative with pip, PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\parqbridge.example.toml config\parqbridge.toml
.\.venv\Scripts\python.exe -m app
```

macOS/Linux (bash):

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config/parqbridge.example.toml config/parqbridge.toml
.venv/bin/python -m app
```

Only `config/parqbridge.example.toml` belongs in version control. `config/parqbridge.toml` and `config/parqbridge.local.toml` are ignored, and the local file overrides the main file. Keep passwords in the environment variable named by `password_env` or in the local file; a `password` in the main file is ignored and produces a warning.

The default server bind is `127.0.0.1`. Database profiles are optional; a missing or incomplete profile, or a missing password, leaves that database in offline mode.

## Fixed implementation decisions

- Keep Python 3.14, FastAPI, and vanilla JavaScript with no frontend build chain. Dependency versions are pinned in `pyproject.toml` (runtime in `[project].dependencies`, tests in the `dev` group). After changing a pin, run `uv lock` and `uv export --format requirements-txt --no-hashes --no-emit-project -o requirements.txt`; never edit `uv.lock` or `requirements.txt` by hand.
- Use `pyarrow.parquet.ParquetFile` for metadata, schema, and `iter_batches`. Use DuckDB `read_parquet` for paged previews and child/explode transformations.
- Stream database loads in batches; never materialize the whole Parquet file in memory. Commit each batch; the default batch size is 10,000 rows. Oracle uses `executemany` array binds and `setinputsizes` for LOBs; MySQL and PostgreSQL use `executemany`.
- Run loads through background jobs with progress polling, cancellation, and failure location reporting.
- Keep DDL generation as pure functions from `TableModel`/`ColumnModel` to SQL. Compare DDL output with files in `tests/golden/`.
- Keep user-facing API errors and warnings as codes (`E_*`, `W_*`) with parameters; translate messages in every `app/i18n/*.json` catalog.

## Safety and data handling

- Never modify, move, delete, or copy an original Parquet file. Local-path mode is read-only; only app-managed uploads may be written under `workspace/uploads/`.
- Never put real passwords or DSNs in commits, logs, API responses, the UI, or test fixtures. Show database host/port/target and username only.
- Never run `DROP` or `TRUNCATE` without the UI confirmation that requires the exact parent table name; enforce the same check in the API.
- Never access paths outside configured `allowed_roots`, including traversal or symlink/junction escapes.
- Keep the default bind at `127.0.0.1`.
- Database integration tests must use only tables prefixed with `PB_TEST_` and clean them up afterward. Use a dedicated test database where available.
- Do not report tests that were not run or performance figures that were not measured as results.

## Change and test rules

- When adding an error or warning code, update `app/core/errors.py` and every `app/i18n/*.json` catalog; `tests/test_i18n.py` checks consistency.
- To add a language, add `app/i18n/<code>.json` with `meta.native_name` and the same message keys and placeholders as every existing catalog. The server discovers catalog filenames for the web selector and CLI, so do not add hard-coded language choices.
- Generate test Parquet files in code. Do not commit generated fixtures under `tests/fixtures/`.
- Database integration tests use the `oracle`, `mysql`, and `postgres` markers and run only when the corresponding profile is configured; otherwise they skip.
- Update DDL golden files only when a DDL change is intentional. To refresh them, set `UPDATE_GOLDEN=1` for `tests/test_ddl.py`, review every file under `tests/golden/`, then run the full suite again.

PowerShell:

```powershell
$env:UPDATE_GOLDEN = "1"; .\.venv\Scripts\python.exe -m pytest -q tests/test_ddl.py; Remove-Item Env:UPDATE_GOLDEN
.\.venv\Scripts\python.exe -m pytest -q -rs
```

macOS/Linux (bash):

```bash
UPDATE_GOLDEN=1 .venv/bin/python -m pytest -q tests/test_ddl.py
.venv/bin/python -m pytest -q -rs
```

- Keep `docs/DESIGN.md` aligned with the current implementation when its architecture or constraints change.
- Update `README.md` and `README.ko.md` together when user-facing behavior or setup changes.
