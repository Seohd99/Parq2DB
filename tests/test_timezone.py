from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pyarrow.parquet as pq
import pytest

from app.core import duckdb_utils
from app.core.flatten import plan_tables, query_table_preview
from app.core.inspect import preview_parquet
from app.core.jobs import _record_batch_rows, _stream_batches
from app.core.typemap import measure_string_lengths


UTC_INSTANT = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)


def _timestamp_instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _use_non_utc_duckdb_default(monkeypatch, timezone_name):
    original_connect = duckdb.connect

    def connect_with_host_timezone(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.execute(f"SET TimeZone='{timezone_name}'")
        return connection

    monkeypatch.setattr(duckdb_utils.duckdb, "connect", connect_with_host_timezone)


@pytest.mark.parametrize("host_timezone", ["Pacific/Honolulu", "Asia/Kathmandu"])
def test_previews_and_database_binds_use_utc_despite_host_timezone(
    monkeypatch, parquet_path, host_timezone
):
    monkeypatch.setenv("TZ", host_timezone)
    _use_non_utc_duckdb_default(monkeypatch, host_timezone)

    inspected = preview_parquet(parquet_path, limit=1)
    assert _timestamp_instant(inspected["rows"][0]["event_ts"]) == UTC_INSTANT

    with pq.ParquetFile(parquet_path) as parquet:
        schema = parquet.schema_arrow
    lengths = measure_string_lengths(parquet_path, schema, strategy="max")

    for dialect in ("oracle", "mysql"):
        (table,) = plan_tables(
            schema,
            table_name="PB_TEST_TZ",
            lengths=lengths,
            mode="none",
            dialect=dialect,
            string_strategy="max",
        )
        preview = query_table_preview(parquet_path, table, dialect=dialect, limit=1)
        assert _timestamp_instant(preview["rows"][0]["EVENT_TS"]) == UTC_INSTANT

        stream = _stream_batches(parquet_path, table, dialect, batch_size=2)
        try:
            batch = next(stream)
            rows = _record_batch_rows(batch, table, dialect)
        finally:
            stream.close()
        timestamp_index = next(index for index, column in enumerate(table.columns) if column.name == "EVENT_TS")
        assert rows[0][timestamp_index] == datetime(2024, 1, 1, 12)
