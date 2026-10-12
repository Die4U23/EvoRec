"""Bounded, opt-in observations of one owned local catalog connection.

No SQL text, parameters, connection strings or blocker identities are exported.
Sampled absence of waits is not proof of no waits; CPU covers the leader only.
"""

import ctypes
from datetime import datetime
import math
from pathlib import PureWindowsPath
import sys
from threading import Event, Thread
from time import perf_counter

import psycopg
from psycopg.rows import dict_row


ACTIVITY_SQL = """SELECT state, wait_event_type, wait_event,
    cardinality(pg_catalog.pg_blocking_pids(pid)) AS blocking_count
    FROM pg_catalog.pg_stat_activity
    WHERE pid=%s AND backend_start=%s
      AND datname=current_database() AND usename=current_user"""


def _windows_process_times(pid):
    """Read a native handle; do not assume localhost means a shared PID namespace."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),)*4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        image, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        def ticks(value):
            return (value.dwHighDateTime << 32) | value.dwLowDateTime
        return (ticks(created), (ticks(kernel_time)+ticks(user_time))/10_000_000,
                PureWindowsPath(image.value).name.lower())
    finally:
        kernel.CloseHandle(handle)


def _server_cpu(pid, backend_start):
    # Linux CI uses a PostgreSQL service container: its PID need not name the
    # server in the host namespace. Unsupported mappings stay explicitly unknown.
    if sys.platform != "win32":
        return None
    try:
        created, cpu, image = _windows_process_times(pid)
        created_unix = created/10_000_000 - 11_644_473_600
        if image != "postgres.exe" or not 0 <= backend_start.timestamp()-created_unix <= 2:
            return None
        return created, cpu
    except OSError:
        return None


def _sanitized_sample(row, offset):
    if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
        raise ValueError("invalid sample offset")
    if row is None:
        raise ValueError("owned backend identity disappeared")
    state = row["state"]
    if state not in {"active", "idle", "idle in transaction", "idle in transaction (aborted)",
                     "fastpath function call"}:
        raise ValueError("activity state is unavailable")
    for name in ("wait_event_type", "wait_event"):
        if row[name] is not None and (type(row[name]) is not str or len(row[name]) > 80):
            raise ValueError("invalid wait label")
    if type(row["blocking_count"]) is not int or row["blocking_count"] < 0:
        raise ValueError("invalid blocker count")
    return dict(offset_seconds=offset, state=state, wait_event_type=row["wait_event_type"],
                wait_event=row["wait_event"], blocking_count=row["blocking_count"])


class DatabaseCallObserver:
    """The caller owns the target connection; the observer owns only its reader."""

    def __init__(self, connection, database_url, *, max_samples=256, interval=.02):
        if type(max_samples) is not int or not 1 <= max_samples <= 4096:
            raise ValueError("max_samples must be 1..4096")
        if type(interval) not in (int, float) or not math.isfinite(interval) or not .02 <= interval <= 1:
            raise ValueError("interval must be .02..1 seconds")
        if connection.closed or not connection.autocommit:
            raise ValueError("observer requires a live owned autocommit connection")
        self.pid = connection.info.backend_pid
        with connection.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute("SELECT backend_start FROM pg_catalog.pg_stat_activity "
                                 "WHERE pid=pg_backend_pid()").fetchone()
        if row is None or not isinstance(row["backend_start"], datetime):
            raise ValueError("owned backend identity unavailable")
        self.backend_start = row["backend_start"]
        self.database_url, self.max_samples, self.interval = database_url, max_samples, interval
        self.samples, self.error_type, self.observer_pid = [], None, None
        self.truncated, self.joined = False, False
        self.stop_event, self.ready = Event(), Event()
        self.origin = perf_counter()
        self.thread = Thread(target=self._observe, name="owned-db-observer")
        self.cpu_before = self.cpu_after = None
        self.operation_error_type = None
        self.cpu_error_type = None
        self.started = False

    def _read_cpu(self):
        # Optional native observations cannot mask the caller's SQL exception or
        # prevent the reader from being closed. Failure is unknown, never zero.
        try:
            return _server_cpu(self.pid, self.backend_start)
        except Exception as error:
            self.cpu_error_type = type(error).__name__
            return None

    def _observe(self):
        try:
            with psycopg.connect(self.database_url, autocommit=True, connect_timeout=3,
                                 row_factory=dict_row) as reader:
                self.observer_pid = reader.info.backend_pid
                reader.execute("SET statement_timeout='500ms'")
                reader.execute("SET default_transaction_read_only=on")
                while not self.stop_event.is_set():
                    row = reader.execute(ACTIVITY_SQL, (self.pid, self.backend_start)).fetchone()
                    self.samples.append(_sanitized_sample(row, perf_counter()-self.origin))
                    self.ready.set()
                    if len(self.samples) == self.max_samples:
                        self.truncated = True
                        break
                    self.stop_event.wait(self.interval)
        except Exception as error:
            self.error_type = type(error).__name__
        finally:
            self.ready.set()

    def __enter__(self):
        if self.started:
            raise ValueError("observer cannot be reused")
        self.started = True
        self.thread.start()
        if not self.ready.wait(5):
            self.error_type = "ObserverStartupTimeout"
        self.cpu_before = self._read_cpu()
        self.operation_start = perf_counter()-self.origin
        return self

    def __exit__(self, exc_type, *_):
        self.operation_end = perf_counter()-self.origin
        self.cpu_after = self._read_cpu()
        self.operation_error_type = exc_type.__name__ if exc_type else None
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.joined = not self.thread.is_alive()
        if not self.joined:
            raise RuntimeError("owned observer did not stop")

    def report(self):
        if not self.joined:
            raise ValueError("observer must finish before reporting")
        active = [sample for sample in self.samples
                  if self.operation_start <= sample["offset_seconds"] <= self.operation_end
                  and sample["state"] == "active"]
        cpu = None
        if (self.cpu_before is not None and self.cpu_after is not None
                and self.cpu_before[0] == self.cpu_after[0]
                and self.cpu_after[1] >= self.cpu_before[1]):
            cpu = self.cpu_after[1]-self.cpu_before[1]
        return dict(target_pid=self.pid, observer_pid=self.observer_pid, samples=self.samples,
                    operation_start_offset_seconds=self.operation_start,
                    operation_end_offset_seconds=self.operation_end,
                    operation_error_type=self.operation_error_type,
                    observer_error_type=self.error_type, observer_thread_joined=self.joined,
                    truncated=self.truncated, max_samples=self.max_samples, interval_seconds=self.interval,
                    active_sample_count=len(active),
                    blocking_sample_count=sum(sample["blocking_count"] > 0 for sample in active),
                    observer_complete=not self.truncated and self.error_type is None and bool(active),
                    server_leader_cpu_seconds=cpu, cpu_scope="verified native Windows leader only; no workers",
                    cpu_error_type=self.cpu_error_type,
                    completeness_scope="bounded reader ended successfully with active samples; not continuous coverage",
                    no_sampled_wait_does_not_prove_no_wait=True,
                    observer_overhead_not_subtracted=True, exact_sql_or_lock_duration_measured=False)
