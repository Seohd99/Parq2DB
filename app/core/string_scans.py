"""Background full-file string length scans for selected Parquet files."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, Event, RLock
from typing import Any, Callable

import pyarrow.parquet as pq

from app.core.errors import PBError
from app.core.model import StringLengthMeasurements
from app.core.typemap import (
    STRING_SAMPLE_ROWS,
    _string_length_cache_key,
    measure_string_lengths,
)

ScanProgress = Callable[[int], None]


@dataclass
class _ScanRecord:
    key: tuple | None
    status: str = "pending"
    progress_percent: int | None = 0
    error: dict[str, Any] | None = None
    measurements: StringLengthMeasurements | None = None
    file_ids: set[str] = field(default_factory=set)
    cancel_event: Event = field(default_factory=Event)
    future: Future | None = None


class StringScanManager:
    """Deduplicate, bound, cancel, and report full length scans."""

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="parqbridge-scan")
        self._condition = Condition(RLock())
        self._by_file: dict[str, _ScanRecord] = {}
        self._by_key: dict[tuple, _ScanRecord] = {}
        self._closed = False

    def start(self, file_id: str, path: Path) -> dict[str, Any]:
        """Start or join the full scan for one registered file handle."""
        with self._condition:
            existing = self._by_file.get(file_id)
            if existing is not None:
                return self._snapshot(existing)
            if self._closed:
                raise PBError("E_STRING_SCAN_FAILED")

        try:
            resolved = path.resolve(strict=True)
            schema = pq.read_schema(resolved)
            key = _string_length_cache_key(resolved, schema, "full", STRING_SAMPLE_ROWS)
        except Exception:
            record = _ScanRecord(key=None, status="failed", progress_percent=100)
            record.error = {"code": "E_STRING_SCAN_FAILED", "params": {}}
            with self._condition:
                self._by_file[file_id] = record
                self._condition.notify_all()
            return self._snapshot(record)

        with self._condition:
            if self._closed:
                raise PBError("E_STRING_SCAN_FAILED")
            existing = self._by_file.get(file_id)
            if existing is not None:
                return self._snapshot(existing)
            record = self._by_key.get(key)
            if record is not None and record.status in {"pending", "running", "ready"}:
                record.file_ids.add(file_id)
                self._by_file[file_id] = record
                return self._snapshot(record)
            record = _ScanRecord(key=key, file_ids={file_id})
            self._by_key[key] = record
            self._by_file[file_id] = record
            record.future = self._executor.submit(self._run, record, resolved, schema)
            return self._snapshot(record)

    def state(self, file_id: str) -> dict[str, Any]:
        with self._condition:
            record = self._by_file.get(file_id)
            if record is None:
                raise PBError("E_FILE_NOT_FOUND")
            return self._snapshot(record)

    def wait(
        self,
        file_id: str,
        cancel_event: Event,
        progress: ScanProgress | None = None,
    ) -> StringLengthMeasurements | None:
        """Wait interruptibly for an existing scan; return None on load cancellation."""
        with self._condition:
            while True:
                if cancel_event.is_set():
                    return None
                record = self._by_file.get(file_id)
                if record is None or record.status == "cancelled":
                    raise PBError("E_STRING_SCAN_CANCELLED")
                if record.status == "ready":
                    # An empty measurement set is falsy but valid (no string/binary columns),
                    # so test for None explicitly instead of using `or`.
                    measured = record.measurements
                    if measured is None:
                        return StringLengthMeasurements({})
                    return StringLengthMeasurements(
                        dict(measured),
                        sampled_rows=measured.sampled_rows,
                        total_rows=measured.total_rows,
                        margin=measured.margin,
                    )
                if record.status == "failed":
                    detail = record.error or {"code": "E_STRING_SCAN_FAILED", "params": {}}
                    raise PBError(detail["code"], **detail.get("params", {}))
                if progress is not None and record.progress_percent is not None:
                    progress(record.progress_percent)
                self._condition.wait(timeout=0.1)

    def release(self, file_id: str, *, wait: bool = False, timeout: float = 30.0) -> None:
        """Drop one selection's interest and cancel an unshared scan if it is active."""
        future: Future | None = None
        with self._condition:
            record = self._by_file.pop(file_id, None)
            if record is None:
                return
            record.file_ids.discard(file_id)
            if not record.file_ids:
                if record.status in {"pending", "running"}:
                    record.status = "cancelled"
                    record.cancel_event.set()
                    future = record.future
                    if future is not None:
                        future.cancel()
                if record.key is not None and self._by_key.get(record.key) is record:
                    del self._by_key[record.key]
            self._condition.notify_all()
        if wait and future is not None and not future.cancelled():
            try:
                future.result(timeout=timeout)
            except (CancelledError, TimeoutError, PBError, Exception):
                # Cancellation and scan failures are reflected by the state API;
                # deleting an uploaded file only needs the reader to release it.
                pass

    def _run(self, record: _ScanRecord, path: Path, schema: Any) -> None:
        with self._condition:
            if record.status == "cancelled" or record.cancel_event.is_set():
                return
            record.status = "running"
            self._condition.notify_all()

        def report(done: int, total: int) -> None:
            percent = 100 if total <= 0 else max(0, min(100, int(done * 100 / total)))
            with self._condition:
                if record.status == "running":
                    record.progress_percent = percent
                    self._condition.notify_all()

        try:
            measurements = measure_string_lengths(
                path,
                schema,
                strategy="sample",
                length_scan="full",
                progress=report,
                cancel_event=record.cancel_event,
            )
            with self._condition:
                if record.status != "cancelled" and not record.cancel_event.is_set():
                    record.measurements = measurements
                    record.progress_percent = 100
                    record.status = "ready"
                self._condition.notify_all()
        except PBError as exc:
            with self._condition:
                if record.status != "cancelled":
                    record.error = exc.to_dict()
                    record.progress_percent = 100
                    record.status = "failed"
                self._condition.notify_all()
        except Exception:
            with self._condition:
                if record.status != "cancelled":
                    record.error = {"code": "E_STRING_SCAN_FAILED", "params": {}}
                    record.progress_percent = 100
                    record.status = "failed"
                self._condition.notify_all()

    @staticmethod
    def _snapshot(record: _ScanRecord) -> dict[str, Any]:
        return {
            "status": record.status,
            "progress_percent": record.progress_percent,
            "error": dict(record.error) if record.error is not None else None,
        }

    def shutdown(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for record in self._by_key.values():
                if record.status in {"pending", "running"}:
                    record.cancel_event.set()
                    record.status = "cancelled"
                    if record.future is not None:
                        record.future.cancel()
            self._condition.notify_all()
        self._executor.shutdown(wait=True, cancel_futures=True)
