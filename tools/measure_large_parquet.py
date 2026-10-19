"""Measure Parq2DB's large-file API and streaming load paths on Windows.

The selected Parquet file is only opened for reading. Database writes are sent to
an in-memory fake MySQL connection that records counts without retaining rows.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

MIN_PARQUET_BYTES = 1024**3
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def process_memory() -> dict[str, int]:
    """Return current and process-peak resident working set from Windows API."""
    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_memory_info = psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
    get_memory_info.restype = wintypes.BOOL
    ok = get_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    )
    if not ok:
        raise ctypes.WinError()
    return {
        "working_set_bytes": int(counters.WorkingSetSize),
        "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
    }


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.result: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        upper = sql.upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            self.result = (1,) if params[0] in self.connection.existing else None
        elif upper.startswith("CREATE TABLE"):
            name = re.match(r"CREATE TABLE\s+([^\s(]+)", sql, re.IGNORECASE).group(1).strip("`\" ")
            self.connection.existing.add(name)
            self.connection.row_counts[name] = 0
        elif upper.startswith("SELECT COUNT(*) FROM"):
            name = re.search(r"\bFROM\s+[`\"]?([^`\"\s]+)", sql, re.IGNORECASE).group(1)
            self.result = (self.connection.row_counts.get(name, 0),)
        elif upper.startswith("SAVEPOINT") or upper.startswith("ROLLBACK TO"):
            self.result = None
        else:
            raise RuntimeError("Unexpected statement in fake database")

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        match = re.match(r"INSERT INTO\s+([^\s(]+)", sql, re.IGNORECASE)
        if match is None:
            raise RuntimeError("Unexpected INSERT statement")
        table = match.group(1).strip("`\" ")
        self.connection.pending_rows[table] = self.connection.pending_rows.get(table, 0) + len(rows)
        self.connection.batch_sizes.append(len(rows))

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        pass


class FakeConnection:
    """Count-only fake database; row values and batches are not retained."""

    def __init__(self) -> None:
        self.existing: set[str] = set()
        self.row_counts: dict[str, int] = {}
        self.pending_rows: dict[str, int] = {}
        self.batch_sizes: list[int] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        for table, count in self.pending_rows.items():
            self.row_counts[table] = self.row_counts.get(table, 0) + count
        self.pending_rows.clear()
        self.commits += 1

    def rollback(self) -> None:
        self.pending_rows.clear()

    def close(self) -> None:
        pass


def worker(path: Path, batch_size: int) -> dict[str, Any]:
    import pyarrow.parquet as pq

    from app.core.config import DbProfile
    from app.core.flatten import plan_tables
    from app.core.jobs import execute_load

    with pq.ParquetFile(path) as parquet:
        source_rows = parquet.metadata.num_rows
        row_groups = parquet.metadata.num_row_groups
        schema = parquet.schema_arrow
    tables = plan_tables(
        schema,
        table_name="PB_TEST_MEMORY_PROBE",
        mode="none",
        dialect="mysql",
        string_strategy="lob",
    )
    fake = FakeConnection()
    profile = DbProfile(kind="mysql", offline_reason="E_DB_NOT_CONFIGURED")

    from app.core import jobs

    original_connect = jobs.connect
    jobs.connect = lambda _profile: fake
    rss_samples: list[dict[str, int]] = []

    def sample_progress(_table: str, rows_done: int, _expected: int | None) -> None:
        if rows_done and rows_done % 50000 == 0:
            rss_samples.append({"rows": rows_done, **process_memory()})

    try:
        before = process_memory()
        started = time.perf_counter()
        result = execute_load(
            path=path,
            tables=tables,
            profile=profile,
            source_rows=source_rows,
            batch_size=batch_size,
            existing_policy="fail",
            cancel_event=__import__("threading").Event(),
            progress=sample_progress,
        )
        elapsed = time.perf_counter() - started
        after = process_memory()
    finally:
        jobs.connect = original_connect

    return {
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed, 3),
        "rows": source_rows,
        "row_groups": row_groups,
        "committed_rows": result["committed_rows"],
        "committed_batches": result["committed_batches"],
        "max_batch_rows": max(fake.batch_sizes, default=0),
        "matched": result["tables"][0]["matched"],
        "rss_before_bytes": before["working_set_bytes"],
        "rss_peak_after_bytes": after["peak_working_set_bytes"],
        "rss_peak_increase_bytes": max(
            0,
            after["peak_working_set_bytes"] - before["peak_working_set_bytes"],
        ),
        "rss_samples": rss_samples,
    }


def parse_batch_sizes(value: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch sizes must be comma-separated positive integers") from exc
    if not sizes or any(item <= 0 for item in sizes):
        raise argparse.ArgumentTypeError("batch sizes must be comma-separated positive integers")
    if len(sizes) > 3:
        raise argparse.ArgumentTypeError("provide at most three batch sizes")
    return sizes


def measure_api(path: Path, total_rows: int, page_size: int) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from app.core.config import AppConfig, DbProfile, ServerConfig
    from app.main import app

    app.state.config = AppConfig(
        server=ServerConfig(allowed_roots=(path.parent.resolve(strict=True),)),
        db={
            "oracle": DbProfile(kind="oracle", offline_reason="E_DB_NOT_CONFIGURED"),
            "mysql": DbProfile(kind="mysql", offline_reason="E_DB_NOT_CONFIGURED"),
        },
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        selected = client.post("/api/files/local", json={"path": str(path)})
        selected.raise_for_status()
        file_id = selected.json()["file"]["file_id"]

        started = time.perf_counter()
        inspected = client.get(f"/api/files/{file_id}/inspect")
        inspect_seconds = time.perf_counter() - started
        inspected.raise_for_status()

        started = time.perf_counter()
        first = client.get(f"/api/files/{file_id}/preview", params={"offset": 0, "limit": page_size})
        first_seconds = time.perf_counter() - started
        first.raise_for_status()

        deep_offset = max(0, int(total_rows * 0.9))
        started = time.perf_counter()
        deep = client.get(
            f"/api/files/{file_id}/preview",
            params={"offset": deep_offset, "limit": page_size},
        )
        deep_seconds = time.perf_counter() - started
        deep.raise_for_status()

    return {
        "inspect_seconds": round(inspect_seconds, 3),
        "preview_first_page_seconds": round(first_seconds, 3),
        "preview_deep_offset": deep_offset,
        "preview_deep_page_seconds": round(deep_seconds, 3),
        "page_size": page_size,
        "first_page_rows": len(first.json().get("rows", [])),
        "deep_page_rows": len(deep.json().get("rows", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path, help="existing 1 GiB+ Parquet file to read without modifying")
    parser.add_argument("--page-size", type=int, default=25)
    parser.add_argument("--batch-sizes", type=parse_batch_sizes, default=(1000, 5000, 10000))
    parser.add_argument("--worker-batch-size", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if os.name != "nt":
        parser.error("RSS measurement currently uses the Windows GetProcessMemoryInfo API")
    path = args.parquet.expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".parquet":
        parser.error("parquet must be an existing .parquet file")
    if path.stat().st_size < MIN_PARQUET_BYTES:
        parser.error("parquet must be at least 1 GiB")
    if not 1 <= args.page_size <= 200:
        parser.error("page size must be between 1 and 200")

    import pyarrow.parquet as pq

    with pq.ParquetFile(path) as parquet:
        metadata = parquet.metadata
        file_info = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "size_gib": round(path.stat().st_size / 1024**3, 3),
            "rows": metadata.num_rows,
            "row_groups": metadata.num_row_groups,
            "columns": metadata.num_columns,
        }

    if args.worker_batch_size:
        print(json.dumps(worker(path, args.worker_batch_size), ensure_ascii=False))
        return 0

    original_stat = path.stat()
    api = measure_api(path, file_info["rows"], args.page_size)
    memory_runs: list[dict[str, Any]] = []
    for batch_size in args.batch_sizes:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            str(path),
            "--worker-batch-size",
            str(batch_size),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"memory worker failed for batch_size={batch_size}: {completed.stderr.strip()}"
            )
        memory_runs.append(json.loads(completed.stdout))

    final_stat = path.stat()
    source_unchanged = (
        original_stat.st_size == final_stat.st_size
        and original_stat.st_mtime_ns == final_stat.st_mtime_ns
    )
    if not source_unchanged:
        raise RuntimeError("source Parquet metadata changed during read-only measurement")

    report = {
        "machine": {
            "os": platform.platform(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "rss_method": "Windows psapi.GetProcessMemoryInfo / PeakWorkingSetSize",
        },
        "file": file_info,
        "api": api,
        "fake_load": {
            "driver": "count-only in-memory fake MySQL connection",
            "retains_inserted_rows": False,
            "runs": memory_runs,
        },
        "source_unchanged": source_unchanged,
        "real_database_load": "not measured; no DB connection was made",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
