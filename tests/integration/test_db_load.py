import threading
import uuid
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.config import load_config
from app.core.db import close_quietly, connect, driver_code, table_exists
from app.core.errors import PBError
from app.core.flatten import plan_tables
from app.core.jobs import execute_load


def _round_trip(profile, dialect, tmp_path):
    table_name = f"PB_TEST_{uuid.uuid4().hex[:10].upper()}"
    parquet_path = tmp_path / "round_trip.parquet"
    pq.write_table(
        pa.table({
            "id": [1, 2],
            "label": ["한글", "왕복"],
            "event_ts": pa.array(
                [
                    datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
                    datetime(2024, 1, 2, 18, 30, tzinfo=timezone.utc),
                ],
                type=pa.timestamp("us", tz="UTC"),
            ),
        }),
        parquet_path,
    )
    with pq.ParquetFile(parquet_path) as parquet:
        models = plan_tables(
            parquet.schema_arrow,
            table_name=table_name,
            dialect=dialect,
            string_strategy="max",
        )
        source_rows = parquet.metadata.num_rows

    try:
        result = execute_load(
            path=parquet_path,
            tables=models,
            profile=profile,
            source_rows=source_rows,
            batch_size=10_000,
            existing_policy="fail",
            cancel_event=threading.Event(),
            progress=lambda *_: None,
        )
        report = result["tables"][0]
        assert report["expected_rows"] == report["database_count"] == 2
        assert report["matched"] is True

        connection = connect(profile)
        try:
            cursor = connection.cursor()
            try:
                if dialect in {"oracle", "postgres"}:
                    name = '"label"' if dialect == "postgres" else '"LABEL"'
                else:
                    name = "`LABEL`"
                cursor.execute(f"SELECT {name} FROM {table_name} ORDER BY ID")
                assert [row[0] for row in cursor.fetchall()] == ["한글", "왕복"]
                if dialect == "oracle":
                    cursor.execute(
                        "SELECT TO_CHAR(EVENT_TS, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6 TZH:TZM') "
                        f"FROM {table_name} ORDER BY ID"
                    )
                    actual_timestamps = [
                        datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S.%f %z").astimezone(timezone.utc)
                        for row in cursor.fetchall()
                    ]
                elif dialect == "postgres":
                    cursor.execute(f"SELECT EVENT_TS FROM {table_name} ORDER BY ID")
                    actual_timestamps = [row[0].astimezone(timezone.utc) for row in cursor.fetchall()]
                else:
                    cursor.execute(f"SELECT EVENT_TS FROM {table_name} ORDER BY ID")
                    actual_timestamps = [row[0].replace(tzinfo=timezone.utc) for row in cursor.fetchall()]
                assert actual_timestamps == [
                    datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
                    datetime(2024, 1, 2, 18, 30, tzinfo=timezone.utc),
                ]
            finally:
                cursor.close()
        finally:
            close_quietly(connection)
    finally:
        # Test code only addresses this generated PB_TEST_ table.
        connection = None
        try:
            connection = connect(profile)
            if table_exists(connection, profile, table_name):
                cursor = connection.cursor()
                try:
                    drop_suffix = " PURGE" if dialect == "oracle" else ""
                    cursor.execute(f"DROP TABLE {table_name}{drop_suffix}")
                    connection.commit()
                finally:
                    cursor.close()
        except PBError:
            raise
        except Exception as exc:
            raise PBError("E_DB_OPERATION_FAILED", driver_code=driver_code(dialect, exc)) from None
        finally:
            if connection is not None:
                close_quietly(connection)


@pytest.mark.oracle
def test_oracle_create_insert_count_korean_round_trip_and_drop(tmp_path):
    profile = load_config().db["oracle"]
    if not profile.online:
        pytest.skip(profile.offline_reason or "Oracle profile is offline")
    _round_trip(profile, "oracle", tmp_path)


@pytest.mark.mysql
def test_mysql_create_insert_count_korean_round_trip_and_drop(tmp_path):
    profile = load_config().db["mysql"]
    if not profile.online:
        pytest.skip(profile.offline_reason or "MySQL profile is offline")
    _round_trip(profile, "mysql", tmp_path)


@pytest.mark.postgres
def test_postgres_create_insert_count_korean_round_trip_and_drop(tmp_path):
    profile = load_config().db["postgres"]
    if not profile.online:
        pytest.skip(profile.offline_reason or "PostgreSQL profile is offline")
    _round_trip(profile, "postgres", tmp_path)
