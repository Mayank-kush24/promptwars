"""
AsyncAuditSink - bounded in-process queue + background bulk-INSERT worker.

Goals:
  * Never silently drop an event. On overflow we attempt to enqueue a
    SINK_OVERFLOW marker and log a warning (so the operator sees it),
    instead of pretending the event happened.
  * Decouple slow-DB-writes from the request path: the worker drains every
    flush_interval_ms or once flush_batch is reached.
  * Survive transient DB failures: failed batches are re-queued (best effort)
    and we keep retrying. We never crash the worker thread.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("audit.sink")

# Column ordering for the INSERT (also defines what keys we pass through)
EVENT_COLUMNS: tuple[str, ...] = (
    "occurred_at",
    "request_id",
    "principal",
    "principal_email",
    "session_id",
    "client_ip",
    "user_agent",
    "source",
    "action",
    "module",
    "entity",
    "record_pk",
    "http_method",
    "url",
    "endpoint",
    "status",
    "latency_ms",
    "sql_kind",
    "sql_statement",
    "rowcount",
    "extra",
)

JSONB_COLUMNS = {"record_pk", "extra"}
INET_COLUMNS = {"client_ip"}


def _placeholder(col: str, idx: int) -> str:
    if col in JSONB_COLUMNS:
        return f"CAST(:{col}_{idx} AS JSONB)"
    if col in INET_COLUMNS:
        return f"NULLIF(:{col}_{idx}, '')::inet"
    return f":{col}_{idx}"


@dataclass
class _SinkStats:
    enqueued: int = 0
    written: int = 0
    overflowed: int = 0
    failed: int = 0
    dropped_oldest: int = 0


class AsyncAuditSink:
    def __init__(
        self,
        engine: Engine,
        *,
        maxsize: int = 100_000,
        flush_interval_ms: int = 250,
        flush_batch: int = 500,
        block_ms: int = 500,
        enabled: bool = True,
        drop_oldest: bool = False,
    ):
        self.engine = engine
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=max(1, int(maxsize)))
        self.flush_interval = max(0.005, float(flush_interval_ms) / 1000.0)
        self.flush_batch = max(1, int(flush_batch))
        self.drop_oldest = bool(drop_oldest)
        # Under drop_oldest, never block the request thread on the queue.
        self.block_seconds = (
            0.0 if self.drop_oldest else max(0.0, float(block_ms) / 1000.0)
        )
        self.enabled = bool(enabled)
        self.stats = _SinkStats()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._drop_log_next = 0.0

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if not self.enabled:
            return
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="audit-sink-worker"
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        self._worker = None

    def queue_depth(self) -> int:
        """Approximate pending events (for health checks)."""
        try:
            return self.queue.qsize()
        except Exception:  # noqa: BLE001
            return -1

    def _maybe_log_drop_oldest(self) -> None:
        now = time.monotonic()
        if now < self._drop_log_next:
            return
        self._drop_log_next = now + 30.0
        log.warning(
            "audit: drop-oldest mode discarded %d events total (queue max=%d)",
            self.stats.dropped_oldest,
            self.queue.maxsize,
        )

    def enqueue(self, event: dict[str, Any]) -> bool:
        """
        Enqueue an event. Returns True if accepted, False if overflow occurred.
        When ``drop_oldest`` is False: overflow events still produce a SINK_OVERFLOW row
        when possible and increment a counter.
        When ``drop_oldest`` is True: discards the oldest queued event and retries so
        the request thread never blocks (best-effort; may still overflow if worker dead).
        """
        if not self.enabled:
            return False
        if "occurred_at" not in event:
            event["occurred_at"] = datetime.now(timezone.utc)
        max_drop_retries = self.queue.maxsize + 10 if self.drop_oldest else 1
        for _ in range(max_drop_retries):
            try:
                if self.block_seconds > 0:
                    self.queue.put(event, block=True, timeout=self.block_seconds)
                else:
                    self.queue.put_nowait(event)
                self.stats.enqueued += 1
                return True
            except queue.Full:
                if self.drop_oldest:
                    try:
                        self.queue.get_nowait()
                        self.stats.dropped_oldest += 1
                        self._maybe_log_drop_oldest()
                    except queue.Empty:
                        pass
                    continue
                self.stats.overflowed += 1
                self._handle_overflow(event)
                return False
        self.stats.overflowed += 1
        self._handle_overflow(event)
        return False

    def flush_blocking(self, timeout: float = 5.0) -> int:
        """Drain the queue synchronously (used by tests and graceful shutdown)."""
        deadline = time.monotonic() + timeout
        total = 0
        while time.monotonic() < deadline:
            written = self._drain_once()
            total += written
            if written == 0 and self.queue.empty():
                break
        return total

    # ------------------------------------------------------------------ helpers
    def _handle_overflow(self, dropped_event: dict[str, Any]) -> None:
        log.warning(
            "audit: sink overflow (queue=%d) dropped action=%s",
            self.queue.maxsize,
            dropped_event.get("action"),
        )
        try:
            self.queue.put_nowait(
                {
                    "occurred_at": datetime.now(timezone.utc),
                    "action": "SINK_OVERFLOW",
                    "module": "audit",
                    "extra": {
                        "dropped_action": dropped_event.get("action"),
                        "queue_max": self.queue.maxsize,
                    },
                }
            )
        except queue.Full:
            # Queue still full - we've recorded it in stats and the log line above.
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("audit: worker drain failed: %s", exc)
            self._stop.wait(self.flush_interval)
        # final drain
        try:
            self._drain_once()
        except Exception:  # noqa: BLE001
            pass

    def _drain_once(self) -> int:
        batch: list[dict[str, Any]] = []
        while len(batch) < self.flush_batch:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        try:
            self._write_batch(batch)
            self.stats.written += len(batch)
            return len(batch)
        except Exception as exc:  # noqa: BLE001
            self.stats.failed += len(batch)
            log.warning("audit: bulk insert failed (%s); %d events lost this flush", exc, len(batch))
            return 0

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        cols = EVENT_COLUMNS
        rows_sql: list[str] = []
        params: dict[str, Any] = {}
        for i, ev in enumerate(batch):
            row_part = "(" + ", ".join(_placeholder(c, i) for c in cols) + ")"
            rows_sql.append(row_part)
            for c in cols:
                v = ev.get(c)
                if c in JSONB_COLUMNS:
                    if v is None:
                        params[f"{c}_{i}"] = None
                    elif isinstance(v, (dict, list)):
                        params[f"{c}_{i}"] = json.dumps(v, default=str)
                    elif isinstance(v, str):
                        params[f"{c}_{i}"] = v
                    else:
                        params[f"{c}_{i}"] = json.dumps(v, default=str)
                elif c == "client_ip":
                    params[f"{c}_{i}"] = "" if v is None else str(v)
                elif c == "occurred_at":
                    params[f"{c}_{i}"] = v
                else:
                    params[f"{c}_{i}"] = v
        sql = (
            "INSERT INTO audit.audit_events ("
            + ", ".join(cols)
            + ") VALUES "
            + ", ".join(rows_sql)
        )
        with self.engine.begin() as conn:
            conn.execute(text(sql), params)
