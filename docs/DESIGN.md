# Design

ParqBridge inspects one Parquet file, plans DDL for Oracle, MySQL, or PostgreSQL, and can create tables and load rows when a database profile is available. This document describes the implementation as it exists today. The [README](../README.md) contains the full type mapping table and user setup instructions.

## Stack and dependency policy

The application targets Python 3.14. FastAPI serves the local web UI, which is plain HTML, CSS, and JavaScript without a frontend build step. Dependency versions are pinned in `requirements.txt` so the supported runtime is reproducible. The CLI and web API call the same code under `app/core` for file checks, inspection, planning, DDL generation, and loading.

The supported database targets are Oracle 12.2 and later, designed around Oracle 19c; MySQL 8 and later; and PostgreSQL 16 and later. A database profile is optional. Without a usable profile and password, that database remains offline while file inspection and DDL generation continue to work.

## Web UI presentation

The web UI uses hand-written CSS over native HTML controls. It has no CSS framework, build step, web font, image, or remote asset, so it stays usable offline and adds little work to the browser when preview tables are large. Shared CSS tokens drive the light and dark palettes, spacing, borders, focus rings, badges, and status colors. The initial palette follows `prefers-color-scheme`; the header toggle can override it and saves an explicit choice as `parqbridge.theme` in `localStorage`. If storage is unavailable, the current session toggle still works and the system preference remains the default on the next visit.

## Configuration and secrets

Configuration is read from `config/parqbridge.toml`, then recursively overridden by `config/parqbridge.local.toml`. Both files are local-only; the versioned `config/parqbridge.example.toml` is a safe template. Database passwords come from the configured environment variable or the local configuration file. A password in the main configuration is ignored and produces a warning. Public configuration responses expose the connection target and username, not the password or DSN.

The server binds to `127.0.0.1` by default. Binding to all interfaces is configurable and produces a warning. Database connection failures and incomplete profiles are represented as error codes so the UI can explain offline status without returning driver messages or secrets.

## File access and uploads

Local file selection is read-only. Each selected path is resolved strictly and must remain beneath a configured allowed root. Directory entries are checked individually, and paths are checked again when a file handle is used; traversal, symlink, and junction escapes are rejected. The browser and API use a process-local `file_id` instead of repeatedly accepting a filesystem path.

Uploads are streamed in bounded chunks into UUID-named Parquet files beneath the app-managed upload directory. The upload size and Parquet magic bytes are checked before registration. The client filename is display metadata only. Failed partial uploads are removed, and the upload deletion API only accepts registered uploads inside that directory. Local source files are never copied, moved, or modified. File handles are process-local and must be selected again after a server restart.

## Schema inspection and preview

PyArrow's `ParquetFile` supplies footer metadata, schema, row-group information, and batch readers. Footer statistics are reported only when the metadata needed for a statistic is present across the relevant row groups; missing values are not inferred from a partial set. The schema response preserves the distinction between lists, structs, maps, and scalar fields.

DuckDB reads a requested page with a bound file path and `LIMIT`/`OFFSET`; the maximum page size is 200 rows. Nested values are serialized as JSON for preview. Timestamp display uses UTC rules so results do not depend on the host time zone. Preview reads only the requested page rather than materializing the full file.

## Type mapping and string sizing

Arrow types are mapped into a shared `TableModel` and `ColumnModel`; the target-specific DDL emitters are pure functions over those models. See [Main type mapping](../README.md#main-type-mapping) for the complete mapping table. Unsupported or lossy conversions are surfaced as warnings instead of being hidden.

String sizing has two separate choices. `length_scan=full` and `strings=sample` are the defaults:

- `length_scan` controls how much of the file is measured. `full` is the default and scans variable-length string and binary leaves in bounded batches, retaining only maximum character and UTF-8 byte lengths. Nested list, struct, and map leaves are measured recursively. `sample` examines up to 512 rows distributed across as many as four row groups and adds a 20% margin; it is an estimate and can miss longer values.
- `strings` controls the target type. `sample` chooses a width from measured lengths, with the scan margin when the measurement itself is sampled. `max` uses the widest suitable inline type and promotes to a large text type when needed. `lob` chooses a large text type directly. The exact type for each option varies by database, as described below and in the README mapping table.

In the web UI, selecting a file starts a cancellable background full measurement when variable-length fields need sizing. While that scan runs, DDL requested with `length_scan=full` is marked `estimated` and uses a quick sample; after completion it becomes `exact`. A load job waits for the full measurement before it finalizes its table plan and starts CREATE or INSERT. The CLI performs its requested scan synchronously. Full measurements describe the current file only: an exact width does not guarantee that a later, different file appended to the same table will fit. Choose `max` or `lob` when that wider reuse is required.

One read-only measurement of a roughly 1.2 GiB Parquet file with 400,000 rows and 13 row groups completed full string-length measurement in about four seconds in a Python 3.14 run. This is workload-specific evidence, not a performance guarantee; file shape, storage, and the runtime environment affect the result. The measurement utility is `tools/measure_large_parquet.py`.

## Flattening modes and identifiers

`none` keeps each nested value in one JSON column. `child` flattens struct fields into path-based columns and creates related tables for list and map values. Child rows carry the parent key and zero-based `ORD` positions; if no usable parent key is selected, a generated row identifier is used. Nesting beyond the configured child depth remains JSON. `explode` expands one selected top-level list into the parent result. It preserves null and empty-list source rows by default; disabling preservation uses a cross unnest and can remove those rows. Plans warn when expansion is expected to exceed the input row count by more than ten times.

Unquoted generated table and column names use uppercase ASCII letters, digits, and underscores; table names must start with a letter or underscore and must not be reserved words. Names are checked against database-specific limits: Oracle 128 UTF-8 bytes, MySQL 64 characters, and PostgreSQL 63 UTF-8 bytes. Generated column names are normalized to fit, and collisions or reserved names receive a numbered suffix such as `_2`. The `quoted` column policy preserves source spelling and escapes the target database's quote character; invalid, overlong, or duplicate quoted names are rejected. Oracle may augment its offline reserved-word list with an online catalog lookup when configured.

## Database-specific behavior

- **Oracle:** Type choices follow the Oracle 19c design target. Inline strings use a conservative `VARCHAR2(1000 CHAR)` ceiling based on a 4,000-byte inline limit and a four-byte-per-character AL32UTF8 worst case; larger values use `CLOB`. Binary values use `RAW` when they fit the inline limit and `BLOB` otherwise. Loads use `executemany` array binds. `setinputsizes` specifies CLOB/BLOB inputs and timezone-aware timestamp types.
- **MySQL:** Connections use `utf8mb4`. The planner budgets at most 60,000 bytes for the row representation, below the 65,535-byte server limit, and promotes the largest `VARCHAR`/`VARBINARY` columns to `TEXT`/`BLOB` tiers when needed. This conservative estimate does not guarantee every InnoDB page-layout constraint. Timezone-aware timestamps are written as UTC-naive `DATETIME`, avoiding session-dependent `TIMESTAMP` conversion.
- **PostgreSQL:** Identifiers are limited to 63 UTF-8 bytes and tables to 1,600 columns. Text and JSON values containing NUL are rejected with the table, column, batch, and row location; NUL is not silently removed. A configured schema is quoted and applied through `search_path`, otherwise PostgreSQL's default path is used. `TEXT` is used for unbounded strings and `BYTEA` for binary data.

See the official references for [Oracle object names](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/Database-Object-Names-and-Qualifiers.html), [MySQL row-size limits](https://dev.mysql.com/doc/refman/8.0/en/column-count-limit.html) and [InnoDB row formats](https://dev.mysql.com/doc/refman/8.0/en/innodb-row-format.html), and [PostgreSQL limits](https://www.postgresql.org/docs/16/limits.html) and [character types](https://www.postgresql.org/docs/16/datatype-character.html).

## Loading and transaction boundaries

Loads run as background jobs. Plain tables stream through PyArrow batches; child and explode plans stream through a DuckDB Arrow reader. The default batch size is 10,000 rows, is configurable, and the full Parquet file is not materialized as one load result. Each successful batch is committed. Oracle uses array-bound `executemany` and `setinputsizes` for LOBs; MySQL and PostgreSQL use `executemany`.

Before loading, the job checks planned tables and applies one of four existing-table policies: `fail`, `append`, `truncate`, or `drop_create`. Append verifies that the existing column names match the plan. Destructive policies require the exact parent table name to be entered; the API enforces the same check as the UI. Related child tables receive the selected policy in parent/child order.

Jobs report progress, can be cancelled at batch boundaries, and retain batches already committed when cancellation or a later error occurs. If an insert batch fails, the loader rolls it back and locates a failing row within that batch where the driver supports that diagnostic path. Errors report a driver code and table/batch/row position, not raw driver text. After loading, the app compares expected rows with database counts; for append, it compares the count delta.

## Time zones

Every DuckDB connection used by the app sets its time zone to UTC. Timezone-aware Parquet timestamps are normalized as UTC instants for preview and load: Oracle uses `TIMESTAMP WITH TIME ZONE` with a UTC session and typed binds, MySQL uses UTC-naive `DATETIME`, and PostgreSQL uses `TIMESTAMPTZ`. Timestamps without a source time zone remain without one.

## Errors, translations, and entry points

API errors and warnings are returned as stable codes with parameters. Their translated text lives in every `app/i18n/*.json` catalog; the browser loads the selected catalog directly. `app/core/languages.py` discovers catalog filenames and each catalog's `meta.native_name`; `/api/status` returns that list for the web selector, while the CLI uses the same list for `--lang` choices and locale matching. A new language needs a catalog with the same message keys and placeholders as the existing catalogs.

The browser honors a valid saved `parqbridge.lang` choice first. Without one, it selects the first supported language in `navigator.languages` (or `navigator.language` when the list is unavailable), matching the locale to a discovered catalog code; unsupported preferences default to English. The HTML uses English as its static language and initial copy so that untranslated markup is readable before the language catalog loads. A user selection is saved, while a detected default is not persisted. If `localStorage` is unavailable, browser language detection still works. The language selector displays each catalog's self-name from `meta.native_name` in either UI language.

The CLI uses an explicit `--lang` value when supplied. Otherwise it checks `PARQBRIDGE_LANG`, `LC_ALL`, `LC_MESSAGES`, and `LANG` in that order, then on Windows checks `locale.getlocale()` and the user UI language, and finally defaults to English. A locale matching a discovered catalog code selects that catalog; unsupported values select English. Static files and i18n JSON responses use `Cache-Control: no-cache`, preserving ETag revalidation while preventing heuristic reuse of stale catalogs.

The CLI (`python -m app` with a subcommand) and web app share the same inspection, planning, DDL, and job code. CLI load progress is reported in the terminal, and interrupting a load requests cancellation at a batch boundary.

## Tests and known limits

Tests create Parquet data in code. DDL output is compared against files in `tests/golden/`. Unit tests use fake connections to exercise driver calls, transaction handling, error locations, and cancellation without requiring a database. Optional integration tests run only when the matching Oracle, MySQL, or PostgreSQL profile is configured; they use disposable tables with the `PB_TEST_` prefix.

String estimates can miss values outside the sample, and an exact scan covers only the selected source file. The MySQL row-size budget is a planning safeguard, not a promise about every storage-engine layout. Wide nested batches can still require substantial memory, so lowering the configured batch size may be appropriate on constrained machines. Database behavior beyond the stated target versions and configured integration environments is not guaranteed by unit tests alone.
