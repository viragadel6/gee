from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Any, AsyncIterator, Awaitable
from urllib.parse import urlparse

from app import database as database_module
from app.agent.runtime import run_durable_chat
from app.config import get_settings
from app.database import DatabaseError, SupabaseDatabase
from app.errors import AgentFault
from app.models import TerminalState
from app.services import checkpoints
from app.utils.clock import age_ms, as_bool, as_int, as_list, as_str, iso, now, parse_iso
from app.utils.security import worker_origin
from app.utils.sse import sse_comment, sse_frame

EVENTS_TABLE = "chat_job_events"
JOBS_TABLE = "chat_jobs"
LEASE_MS = 90_000
HEARTBEAT_MS = 20_000
HEARTBEAT_FAILURE_LIMIT = 6
HEARTBEAT_RETRY_MAX = 2
FOLLOW_POLL_MS = 800
FOLLOW_HEARTBEAT_MS = 15_000
FOLLOW_MAX_MS = 21_600_000
FOLLOW_REVIVE_MS = 30_000
FOLLOW_TERMINAL_GRACE_MS = 30_000
FOLLOW_FAILURE_LIMIT = 8
FOLLOW_RETRY_BASE_SECONDS = 0.5
FOLLOW_RETRY_MAX_SECONDS = 6.0
TERMINAL_STATUSES = ("completed", "cancelled", "failed")
ACTIVE_STATUSES = ("queued", "running")
SINK_RETRY_BASE_SECONDS = 0.4
SINK_RETRY_MAX_SECONDS = 3.0
SINK_MAX_BLOCK_SECONDS = 6.0
SINK_FLUSH_EVERY = 10
SINK_MIN_FLUSH_CHARS = 200
FINALIZE_RETRY_MAX = 4
ERROR_MESSAGE_CHARS = 900
LEGACY_COLUMNS = "id, history, status, lease_expires_at, stop_requested, created_at, updated_at"
EXTENDED_COLUMNS = (
    "id, history, status, lease_expires_at, stop_requested, created_at, updated_at, "
    "attempts, next_attempt_at, last_error, progress_at"
)

logger = logging.getLogger("app.chat_jobs")

_background_tasks: set[asyncio.Task[Any]] = set()
_extended_columns: bool | None = None
_extended_lock = asyncio.Lock()


def _retain(coroutine: Awaitable[Any]) -> asyncio.Task[Any]:
    task: asyncio.Task[Any] = asyncio.ensure_future(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _reap(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("background_job_error", extra={"error": str(error)})


def supervisor_origin() -> str | None:
    settings = get_settings()
    candidates: list[str] = []
    for raw in (settings.app_origins, settings.app_origin, settings.published_origin):
        for entry in (raw or "").split(","):
            trimmed = entry.strip()
            if not trimmed:
                continue
            parsed = urlparse(trimmed)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                candidates.append(f"{parsed.scheme}://{parsed.netloc}")
    for candidate in candidates:
        if candidate:
            return candidate
    return None


async def columns_available(database: SupabaseDatabase) -> bool:
    global _extended_columns
    if _extended_columns is not None:
        return _extended_columns
    async with _extended_lock:
        if _extended_columns is not None:
            return _extended_columns
        try:
            await (
                database.table(JOBS_TABLE)
                .select("id, attempts, next_attempt_at, last_error, progress_at")
                .limit(1)
                .execute()
            )
        except DatabaseError as exc:
            if exc.status_code in (400, 404):
                _extended_columns = False
                return False
            return False
        except Exception:
            return False
        _extended_columns = True
        return True


async def dispatch_chat_job(job_id: str, request_url: str) -> None:
    await dispatch_chat_job_origin(job_id, worker_origin(request_url))


async def dispatch_chat_job_origin(job_id: str, origin: str | None) -> None:
    if origin is None:
        _execute_later(job_id)
        return
    database = database_module.get_admin_database()
    try:
        await database.rpc("kick_chat_worker", {"_base_url": origin, "_job_id": job_id}).execute()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "worker_kick_failed", extra={"job_id": job_id, "origin": origin, "error": str(exc)}
        )
        _execute_later(job_id)


def _execute_later(job_id: str) -> None:
    task = _retain(_execute_local_job(job_id))
    task.add_done_callback(_reap)


async def _execute_local_job(job_id: str) -> None:
    try:
        await execute_chat_job(job_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("local_job_failed", extra={"job_id": job_id, "error": str(exc)})


async def revive_stale_chat_job(job_id: str, request_url: str) -> None:
    await revive_stale_chat_job_origin(job_id, worker_origin(request_url))


async def revive_stale_chat_job_origin(job_id: str, origin: str | None) -> bool:
    database = database_module.get_admin_database()
    extended = await columns_available(database)
    columns = EXTENDED_COLUMNS if extended else LEGACY_COLUMNS
    response = await (
        database.table(JOBS_TABLE).select(columns).eq("id", job_id).limit(1).execute()
    )
    row = response.maybe_single()
    if row is None:
        return False
    if as_bool(row.get("stop_requested")):
        return False
    status = as_str(row.get("status"))
    if status not in ACTIVE_STATUSES:
        return False
    lease = parse_iso(row.get("lease_expires_at"))
    if extended:
        scheduled = parse_iso(row.get("next_attempt_at"))
        if scheduled is not None and scheduled > now():
            return False
    stale = status == "queued" or (status == "running" and (lease is None or lease < now()))
    if not stale:
        return False
    await dispatch_chat_job_origin(job_id, origin)
    return True


class DurableEventSink:
    def __init__(self, job_id: str, database: SupabaseDatabase):
        self.job_id = job_id
        self.database = database
        self.content = ""
        self.reasoning = ""
        self.final_status = "failed"
        self.written = 0
        self.buffered = 0
        self.dropped = 0
        self.failures = 0
        self._buffer: list[dict[str, Any]] = []
        self._held: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._delta_counter = 0
        self._done_received = False
        self._progress_columns: bool | None = None
        self.hold_terminal = False

    async def send(self, event: dict[str, Any]) -> None:
        payload = event if isinstance(event, dict) else {"type": "unknown", "payload": event}
        kind = as_str(payload.get("type"))
        if kind == "delta":
            self.content += as_str(payload.get("content"))
            self.reasoning += as_str(payload.get("reasoning_content"))
        elif kind == "done":
            self._done_received = True
            outcome = as_str(payload.get("outcome")) or "failed"
            if outcome == "completed":
                self.final_status = "completed"
            elif outcome == "cancelled":
                self.final_status = "cancelled"
            else:
                self.final_status = "failed"
        if self.hold_terminal and kind in ("terminal", "done"):
            self._held.append(payload)
            return
        await self._write(payload)
        self._delta_counter += 1
        if kind != "delta" or (
            len(self.content) + len(self.reasoning) < SINK_MIN_FLUSH_CHARS
            or self._delta_counter % SINK_FLUSH_EVERY == 0
        ):
            await self.write_progress()

    async def enqueue(self, text: str) -> None:
        if "[DONE]" in text and self.final_status == "failed" and not self._done_received:
            self.final_status = "completed"
        await self.flush()

    async def _write(self, payload: dict[str, Any]) -> bool:
        settings = get_settings()
        async with self._lock:
            self._buffer.append(payload)
            self._trim_locked(settings.chat_job_sink_buffer_max)
            return await self._flush_locked(settings.chat_job_sink_retry_max)

    def _trim_locked(self, limit: int) -> None:
        maximum = max(16, limit)
        if len(self._buffer) <= maximum:
            return
        index = 0
        while len(self._buffer) > maximum and index < len(self._buffer):
            if as_str(self._buffer[index].get("type")) == "delta":
                self._buffer.pop(index)
                self.dropped += 1
                continue
            index += 1
        while len(self._buffer) > maximum:
            self._buffer.pop(0)
            self.dropped += 1

    async def _flush_locked(self, retries: int) -> bool:
        delay = SINK_RETRY_BASE_SECONDS
        waited = 0.0
        attempts = max(0, retries)
        while self._buffer:
            payload = self._buffer[0]
            try:
                await self.database.table(EVENTS_TABLE).insert(
                    {"job_id": self.job_id, "payload": payload}
                ).execute()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures += 1
                if attempts <= 0 or waited + delay > SINK_MAX_BLOCK_SECONDS:
                    self.buffered = len(self._buffer)
                    logger.warning(
                        "event_write_buffered",
                        extra={"job_id": self.job_id, "buffered": self.buffered, "error": str(exc)},
                    )
                    return False
                attempts -= 1
                await asyncio.sleep(delay)
                waited += delay
                delay = min(SINK_RETRY_MAX_SECONDS, delay * 2) * (0.8 + random.random() * 0.4)
                continue
            self._buffer.pop(0)
            self.written += 1
            self.buffered = len(self._buffer)
        return True

    async def flush(self) -> bool:
        async with self._lock:
            return await self._flush_locked(HEARTBEAT_RETRY_MAX)

    async def write_progress(self) -> None:
        payload: dict[str, Any] = {
            "response_content": self.content,
            "response_reasoning": self.reasoning,
            "updated_at": iso(),
        }
        if await self._progress_supported():
            payload["progress_at"] = iso()
        for attempt in range(HEARTBEAT_RETRY_MAX + 1):
            try:
                await self.database.table(JOBS_TABLE).update(payload).eq("id", self.job_id).execute()
                return
            except asyncio.CancelledError:
                raise
            except DatabaseError as exc:
                if exc.status_code in (400, 404) and "progress_at" in payload:
                    self._progress_columns = False
                    payload.pop("progress_at", None)
                    continue
                self.failures += 1
                if attempt >= HEARTBEAT_RETRY_MAX:
                    logger.warning(
                        "progress_write_failed", extra={"job_id": self.job_id, "error": str(exc)}
                    )
                    return
            except Exception as exc:
                self.failures += 1
                if attempt >= HEARTBEAT_RETRY_MAX:
                    logger.warning(
                        "progress_write_failed", extra={"job_id": self.job_id, "error": str(exc)}
                    )
                    return
            await asyncio.sleep(SINK_RETRY_BASE_SECONDS * (attempt + 1))

    async def _progress_supported(self) -> bool:
        if self._progress_columns is not None:
            return self._progress_columns
        self._progress_columns = await columns_available(self.database)
        return self._progress_columns

    def release_held(self) -> list[dict[str, Any]]:
        held = list(self._held)
        self._held.clear()
        return held

    def discard_held(self) -> int:
        count = len(self._held)
        self._held.clear()
        self.hold_terminal = False
        return count

    def stats(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "buffered": len(self._buffer),
            "held": len(self._held),
            "dropped": self.dropped,
            "failures": self.failures,
            "content_chars": len(self.content),
            "reasoning_chars": len(self.reasoning),
        }


async def _renew_lease(database: SupabaseDatabase, job_id: str) -> dict[str, Any] | None:
    response = await (
        database.table(JOBS_TABLE)
        .update(
            {
                "lease_expires_at": iso(now() + timedelta(milliseconds=LEASE_MS)),
                "updated_at": iso(),
            }
        )
        .eq("id", job_id)
        .eq("status", "running")
        .select("stop_requested")
        .execute()
    )
    return response.maybe_single()


async def _heartbeat_loop(
    database: SupabaseDatabase, job_id: str, cancel: asyncio.Event, sink: DurableEventSink
) -> None:
    failures = 0
    while not cancel.is_set():
        try:
            await asyncio.sleep(HEARTBEAT_MS / 1000)
        except asyncio.CancelledError:
            return
        if cancel.is_set():
            return
        row: dict[str, Any] | None = None
        try:
            for attempt in range(HEARTBEAT_RETRY_MAX + 1):
                try:
                    row = await _renew_lease(database, job_id)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if attempt >= HEARTBEAT_RETRY_MAX:
                        raise
                    logger.warning(
                        "heartbeat_retry",
                        extra={"job_id": job_id, "attempt": attempt + 1, "error": str(exc)},
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            failures += 1
            logger.warning(
                "heartbeat_failed",
                extra={"job_id": job_id, "failures": failures, "error": str(exc)},
            )
            if failures >= HEARTBEAT_FAILURE_LIMIT:
                logger.error("heartbeat_lease_lost", extra={"job_id": job_id})
                try:
                    await sink.send(
                        {
                            "type": "progress",
                            "task_id": job_id,
                            "step": 0,
                            "message": (
                                "Az adatbázis-kapcsolat tartósan megszakadt, ezért a feladat "
                                "biztonságosan leáll; a felügyelő automatikusan újraindítja."
                            ),
                        }
                    )
                except Exception:
                    pass
                cancel.set()
                return
            continue
        failures = 0
        if row is not None and as_bool(row.get("stop_requested")):
            cancel.set()
            return


def _resolve_status(sink: DurableEventSink, outcome: TerminalState) -> str:
    status = sink.final_status
    if status == "failed" and outcome == TerminalState.COMPLETED:
        return "completed"
    if status == "failed" and outcome == TerminalState.CANCELLED:
        return "cancelled"
    if status == "failed" and outcome in (
        TerminalState.RUNTIME_LIMIT,
        TerminalState.STEP_LIMIT,
        TerminalState.CONTEXT_LIMIT,
    ):
        return "failed"
    return status


def _retryable(outcome: TerminalState, status: str) -> bool:
    if status != "failed":
        return False
    return outcome == TerminalState.FAILED


async def _schedule_retry(
    database: SupabaseDatabase, job_id: str, reason: str
) -> tuple[bool, int, str]:
    settings = get_settings()
    if await columns_available(database):
        try:
            response = await (
                database.rpc(
                    "claim_chat_job_retry",
                    {
                        "_job_id": job_id,
                        "_max_attempts": settings.chat_job_max_attempts,
                        "_retry_base_ms": settings.chat_job_retry_base_ms,
                        "_retry_max_ms": settings.chat_job_retry_max_ms,
                        "_error": reason[:ERROR_MESSAGE_CHARS],
                    },
                ).execute()
            )
        except DatabaseError as exc:
            if exc.status_code not in (400, 404):
                logger.warning("retry_rpc_failed", extra={"job_id": job_id, "error": str(exc)})
                return False, 0, ""
        except Exception as exc:
            logger.warning("retry_rpc_failed", extra={"job_id": job_id, "error": str(exc)})
            return False, 0, ""
        else:
            row = response.maybe_single() or {}
            status = as_str(row.get("status"))
            attempts = as_int(row.get("attempts"))
            if as_bool(row.get("retry")):
                return True, attempts, status
            return False, attempts, status
    try:
        response = await (
            database.table(JOBS_TABLE)
            .update({"status": "queued", "lease_expires_at": None, "updated_at": iso()})
            .eq("id", job_id)
            .eq("status", "running")
            .execute()
        )
    except Exception as exc:
        logger.warning("legacy_requeue_failed", extra={"job_id": job_id, "error": str(exc)})
        return False, 0, ""
    if not response.rows:
        return False, 0, ""
    return True, 0, "queued"


async def _finalize_job(
    database: SupabaseDatabase, job_id: str, sink: DurableEventSink, status: str, error: str | None
) -> bool:
    payload: dict[str, Any] = {
        "status": status,
        "response_content": sink.content,
        "response_reasoning": sink.reasoning,
        "error": error,
        "finished_at": iso(),
        "lease_expires_at": None,
        "updated_at": iso(),
    }
    if await columns_available(database):
        payload["progress_at"] = iso()
    for attempt in range(FINALIZE_RETRY_MAX + 1):
        try:
            await database.table(JOBS_TABLE).update(payload).eq("id", job_id).execute()
            return True
        except asyncio.CancelledError:
            raise
        except DatabaseError as exc:
            if exc.status_code in (400, 404) and "progress_at" in payload:
                payload.pop("progress_at", None)
                continue
            logger.warning(
                "finalize_failed",
                extra={"job_id": job_id, "attempt": attempt + 1, "error": str(exc)},
            )
        except Exception as exc:
            logger.warning(
                "finalize_failed",
                extra={"job_id": job_id, "attempt": attempt + 1, "error": str(exc)},
            )
        if attempt < FINALIZE_RETRY_MAX:
            await asyncio.sleep(0.6 * (attempt + 1))
    return False


async def fail_chat_job(
    job_id: str, code: str, message: str, outcome: str = "failed"
) -> bool:
    database = database_module.get_admin_database()
    status = "cancelled" if outcome == "cancelled" else "failed"
    reason = message[:ERROR_MESSAGE_CHARS] or "A munka nem fejeződött be."
    events: list[dict[str, Any]] = [
        {"type": "error", "task_id": job_id, "code": code, "message": reason},
        {
            "type": "terminal",
            "task_id": job_id,
            "outcome": status,
            "reason": reason,
            "steps": 0,
            "tool_calls": 0,
            "duration_ms": 0,
        },
        {"type": "done", "task_id": job_id, "outcome": status},
    ]
    for event in events:
        for attempt in range(HEARTBEAT_RETRY_MAX + 1):
            try:
                await database.table(EVENTS_TABLE).insert(
                    {"job_id": job_id, "payload": event}
                ).execute()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= HEARTBEAT_RETRY_MAX:
                    logger.warning(
                        "terminal_event_failed",
                        extra={"job_id": job_id, "event": event.get("type"), "error": str(exc)},
                    )
                    break
                await asyncio.sleep(0.4 * (attempt + 1))
    payload: dict[str, Any] = {
        "status": status,
        "error": reason,
        "finished_at": iso(),
        "lease_expires_at": None,
        "updated_at": iso(),
    }
    updated = False
    for attempt in range(FINALIZE_RETRY_MAX + 1):
        try:
            response = await (
                database.table(JOBS_TABLE)
                .update(payload)
                .eq("id", job_id)
                .in_("status", list(ACTIVE_STATUSES))
                .execute()
            )
            updated = bool(response.rows)
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "fail_job_update_failed",
                extra={"job_id": job_id, "attempt": attempt + 1, "error": str(exc)},
            )
            if attempt < FINALIZE_RETRY_MAX:
                await asyncio.sleep(0.5 * (attempt + 1))
    await checkpoints.clear_checkpoint(job_id)
    return updated


async def job_queue_summary() -> dict[str, Any]:
    database = database_module.get_admin_database()
    extended = await columns_available(database)
    summary: dict[str, Any] = {
        "extended_columns": extended,
        "checkpointing": checkpoints.checkpoint_stats(),
        "oldest_queued_seconds": None,
        "expired_leases": 0,
        "due_retries": 0,
        "stop_requested": 0,
    }
    for status in (*ACTIVE_STATUSES, *TERMINAL_STATUSES):
        try:
            summary[status] = await (
                database.table(JOBS_TABLE)
                .select("id", count="exact", head=True)
                .eq("status", status)
                .count_exact()
            )
        except Exception as exc:
            logger.warning("summary_count_failed", extra={"status": status, "error": str(exc)})
            summary[status] = None
    try:
        expired_count = await (
            database.table(JOBS_TABLE)
            .select("id", count="exact", head=True)
            .eq("status", "running")
            .lt("lease_expires_at", iso())
            .count_exact()
        )
        summary["expired_leases"] = int(expired_count)
    except Exception:
        summary["expired_leases"] = None
    if extended:
        try:
            summary["due_retries"] = int(
                await database.table(JOBS_TABLE)
                .select("id", count="exact", head=True)
                .eq("status", "queued")
                .lte("next_attempt_at", iso())
                .count_exact()
            )
        except Exception:
            summary["due_retries"] = None
    try:
        oldest_response = await (
            database.table(JOBS_TABLE)
            .select("created_at")
            .eq("status", "queued")
            .order("created_at", desc=False, nullsfirst=True)
            .limit(1)
            .execute()
        )
        row = oldest_response.maybe_single()
        if row is not None:
            age = age_ms(row.get("created_at"))
            if age is not None:
                summary["oldest_queued_seconds"] = round(age / 1000, 3)
    except Exception as exc:
        logger.warning("summary_oldest_failed", extra={"error": str(exc)})
    return summary


async def _load_job(database: SupabaseDatabase, job_id: str) -> dict[str, Any] | None:
    extended = await columns_available(database)
    columns = EXTENDED_COLUMNS if extended else LEGACY_COLUMNS
    try:
        response = await (
            database.table(JOBS_TABLE).select(columns).eq("id", job_id).limit(1).execute()
        )
    except DatabaseError as exc:
        if exc.status_code not in (400, 404) or not extended:
            raise
        response = await (
            database.table(JOBS_TABLE).select(LEGACY_COLUMNS).eq("id", job_id).limit(1).execute()
        )
    return response.maybe_single()


async def _claim_job(
    database: SupabaseDatabase, job_id: str, job: dict[str, Any], status: str
) -> bool:
    stamp = iso()
    payload: dict[str, Any] = {
        "status": "running",
        "started_at": stamp,
        "updated_at": stamp,
        "lease_expires_at": iso(now() + timedelta(milliseconds=LEASE_MS)),
    }
    if await columns_available(database):
        payload["progress_at"] = stamp
    query = (
        database.table(JOBS_TABLE).update(payload).eq("id", job_id).eq("status", status)
    )
    lease = job.get("lease_expires_at")
    if lease is not None:
        query = query.eq("lease_expires_at", lease)
    else:
        query = query.is_("lease_expires_at", None)
    try:
        claimed = await query.execute()
    except DatabaseError as exc:
        if exc.status_code in (400, 404) and "progress_at" in payload:
            payload.pop("progress_at", None)
            query = (
                database.table(JOBS_TABLE).update(payload).eq("id", job_id).eq("status", status)
            )
            if lease is not None:
                query = query.eq("lease_expires_at", lease)
            else:
                query = query.is_("lease_expires_at", None)
            claimed = await query.execute()
        else:
            raise
    return bool(claimed.rows)


async def execute_chat_job(job_id: str) -> dict[str, str]:
    settings = get_settings()
    database = database_module.get_admin_database()
    try:
        job = await _load_job(database, job_id)
    except Exception as exc:
        logger.warning("job_load_failed", extra={"job_id": job_id, "error": str(exc)})
        return {"status": "unknown"}
    if job is None:
        return {"status": "missing"}
    status = as_str(job.get("status"))
    if status in TERMINAL_STATUSES:
        return {"status": status}
    if as_bool(job.get("stop_requested")):
        stop_sink = DurableEventSink(job_id, database)
        await stop_sink.send(
            {
                "type": "terminal",
                "task_id": job_id,
                "outcome": "cancelled",
                "reason": "A feladatot a felhasználó megszakította.",
                "steps": 0,
                "tool_calls": 0,
                "duration_ms": 0,
            }
        )
        await stop_sink.send({"type": "done", "task_id": job_id, "outcome": "cancelled"})
        await _finalize_job(database, job_id, stop_sink, "cancelled", None)
        await stop_sink.flush()
        await checkpoints.clear_checkpoint(job_id)
        return {"status": "cancelled"}
    lease = parse_iso(job.get("lease_expires_at"))
    if status == "running" and lease is not None and lease > now():
        return {"status": "running"}
    try:
        if not await _claim_job(database, job_id, job, status):
            return {"status": "running"}
    except Exception as exc:
        logger.warning("job_claim_failed", extra={"job_id": job_id, "error": str(exc)})
        return {"status": "unknown"}

    cancel = asyncio.Event()
    sink = DurableEventSink(job_id, database)
    checkpoint = await checkpoints.load_checkpoint(job_id)
    attempts = as_int(job.get("attempts")) + 1
    if checkpoint is not None:
        await sink.send(
            {
                "type": "progress",
                "task_id": job_id,
                "step": checkpoint.steps,
                "message": (
                    f"A feladat a {attempts}. kísérletben, a mentett {checkpoint.steps}. lépéstől "
                    "folytatódik."
                ),
            }
        )
    sink.hold_terminal = attempts <= settings.chat_job_max_attempts
    heartbeat_task = asyncio.create_task(_heartbeat_loop(database, job_id, cancel, sink))
    history = as_list(job.get("history"))
    outcome = TerminalState.FAILED
    failure: str | None = None
    try:
        outcome = await run_durable_chat(job_id, history, sink.send, sink.enqueue, cancel, checkpoint)
    except asyncio.CancelledError:
        outcome = TerminalState.CANCELLED
        raise
    except AgentFault as fault:
        outcome = TerminalState.FAILED
        failure = fault.message
        sink.final_status = "failed"
        await sink.send(
            {
                "type": "error",
                "task_id": job_id,
                "code": fault.code,
                "message": fault.message,
            }
        )
    except Exception as exc:
        outcome = TerminalState.FAILED
        failure = str(exc) or "A háttérmunka hibával leállt."
        sink.final_status = "failed"
        await sink.send(
            {
                "type": "error",
                "task_id": job_id,
                "code": "BACKGROUND_FAILURE",
                "message": failure,
            }
        )
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    final_status = _resolve_status(sink, outcome)
    if (
        _retryable(outcome, final_status)
        and not cancel.is_set()
        and attempts <= settings.chat_job_max_attempts
    ):
        reason = failure or "A végrehajtás átmeneti hiba miatt megszakadt."
        scheduled, used_attempts, new_status = await _schedule_retry(database, job_id, reason)
        if scheduled and new_status == "queued":
            sink.discard_held()
            await sink.send(
                {
                    "type": "progress",
                    "task_id": job_id,
                    "step": 0,
                    "message": (
                        f"Átmeneti hiba miatt a feladat automatikusan újraindul "
                        f"({used_attempts or attempts}/{settings.chat_job_max_attempts}. "
                        "kísérlet); a mentett állapotból folytatom."
                    ),
                }
            )
            await sink.flush()
            await dispatch_chat_job_origin(job_id, supervisor_origin())
            return {"status": "queued"}
    error_message = None
    if final_status == "failed":
        error_message = (failure or "A háttérmunka hibával leállt.")[:ERROR_MESSAGE_CHARS]
    await _finalize_job(database, job_id, sink, final_status, error_message)
    sink.hold_terminal = False
    for held in sink.release_held():
        await sink.send(held)
    await checkpoints.clear_checkpoint(job_id)
    await sink.flush()
    logger.info(
        "job_finished",
        extra={"job_id": job_id, "status": final_status, "sink": sink.stats()},
    )
    return {"status": final_status}


async def _read_events(
    database: SupabaseDatabase, job_id: str, cursor: int
) -> tuple[list[dict[str, Any]], int]:
    response = await (
        database.table(EVENTS_TABLE)
        .select("id", "payload")
        .eq("job_id", job_id)
        .gt("id", cursor)
        .order("id", desc=False)
        .limit(200)
        .execute()
    )
    latest = cursor
    frames: list[dict[str, Any]] = []
    for row in response.rows:
        row_id = as_int(row.get("id"))
        latest = max(latest, row_id)
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload = {"type": "unknown", "payload": payload}
        frames.append({"id": row_id, "payload": payload})
    return frames, latest


async def _job_status(database: SupabaseDatabase, job_id: str) -> str | None:
    response = await (
        database.table(JOBS_TABLE).select("status").eq("id", job_id).limit(1).execute()
    )
    row = response.maybe_single()
    return as_str(row.get("status")) if row else None


async def follow_chat_job(job_id: str, after: int, request_url: str) -> AsyncIterator[str]:
    database = database_module.get_admin_database()
    origin = worker_origin(request_url)
    cursor = max(0, after)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    last_heartbeat = 0.0
    last_revive = loop.time()
    terminal_seen_at = 0.0
    failures = 0
    delay = FOLLOW_RETRY_BASE_SECONDS
    try:
        await revive_stale_chat_job_origin(job_id, origin)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("follow_revive_failed", extra={"job_id": job_id, "error": str(exc)})
    while True:
        if loop.time() - started_at > FOLLOW_MAX_MS / 1000:
            yield sse_frame(
                {
                    "type": "error",
                    "task_id": job_id,
                    "code": "RUNTIME_LIMIT",
                    "message": "Lejárt a teljes feladathatáridő.",
                }
            )
            return
        try:
            frames, cursor = await _read_events(database, job_id, cursor)
            terminal = False
            for frame in frames:
                payload = frame["payload"]
                yield sse_frame(payload, frame["id"])
                if as_str(payload.get("type")) in ("done", "error"):
                    terminal = True
            if terminal:
                status = await _job_status(database, job_id)
                if status is None or status not in ACTIVE_STATUSES:
                    return
                if terminal_seen_at == 0.0:
                    terminal_seen_at = loop.time()
                    yield sse_comment("retrying")
                elif loop.time() - terminal_seen_at > FOLLOW_TERMINAL_GRACE_MS / 1000:
                    rest, cursor = await _read_events(database, job_id, cursor)
                    for frame in rest:
                        yield sse_frame(frame["payload"], frame["id"])
                    return
            if not frames:
                status = await _job_status(database, job_id)
                if status is None or status not in ACTIVE_STATUSES:
                    rest, cursor = await _read_events(database, job_id, cursor)
                    for frame in rest:
                        yield sse_frame(frame["payload"], frame["id"])
                    return
                if loop.time() - last_revive >= FOLLOW_REVIVE_MS / 1000:
                    last_revive = loop.time()
                    try:
                        revived = await revive_stale_chat_job_origin(job_id, origin)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "follow_revive_failed", extra={"job_id": job_id, "error": str(exc)}
                        )
                    else:
                        if revived:
                            yield sse_comment("revive")
            failures = 0
            delay = FOLLOW_RETRY_BASE_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            logger.warning(
                "follow_read_failed",
                extra={"job_id": job_id, "failures": failures, "error": str(exc)},
            )
            if failures > FOLLOW_FAILURE_LIMIT:
                yield sse_frame(
                    {
                        "type": "error",
                        "task_id": job_id,
                        "code": "FOLLOW_FAILURE",
                        "message": str(exc) or "Az eseményfolyam megszakadt.",
                    }
                )
                return
            yield sse_comment("retry")
            await asyncio.sleep(delay)
            delay = min(FOLLOW_RETRY_MAX_SECONDS, delay * 2) * (0.8 + random.random() * 0.4)
            continue
        if loop.time() - last_heartbeat >= FOLLOW_HEARTBEAT_MS / 1000:
            last_heartbeat = loop.time()
            yield sse_comment("heartbeat")
        await asyncio.sleep(FOLLOW_POLL_MS / 1000)
