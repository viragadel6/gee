from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from app import database as database_module
from app.agent.runtime import run_durable_chat
from app.models import TerminalState
from app.utils.security import worker_origin
from app.utils.sse import sse_comment, sse_frame

LEASE_MS = 90_000
HEARTBEAT_MS = 20_000
FOLLOW_POLL_MS = 800
FOLLOW_HEARTBEAT_MS = 15_000
FOLLOW_MAX_MS = 21_600_000
FOLLOW_REVIVE_MS = 30_000
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

logger = logging.getLogger("app.chat_jobs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_background_tasks: set[asyncio.Task[None]] = set()


async def dispatch_chat_job(job_id: str, request_url: str) -> None:
    origin = worker_origin(request_url)
    if origin is None:
        task = asyncio.create_task(_execute_local_job(job_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return
    database = database_module.get_admin_database()
    try:
        await database.rpc(
            "kick_chat_worker", {"_base_url": origin, "_job_id": job_id}
        ).execute()
    except Exception as exc:
        raise RuntimeError("A háttérmunka indítása sikertelen.") from exc


async def _execute_local_job(job_id: str) -> None:
    try:
        await execute_chat_job(job_id)
    except Exception:
        pass


async def revive_stale_chat_job(job_id: str, request_url: str) -> None:
    database = database_module.get_admin_database()
    response = await (
        database.table("chat_jobs")
        .select("status", "lease_expires_at", "stop_requested")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not response.rows:
        return
    job = response.rows[0]
    if job.get("stop_requested"):
        return
    status = str(job.get("status") or "")
    lease = _parse_iso(job.get("lease_expires_at"))
    stale = status == "queued" or (
        status == "running" and (lease is None or lease < _now())
    )
    if stale:
        await dispatch_chat_job(job_id, request_url)


class DatabaseJobSink:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.database = database_module.get_admin_database()
        self.content = ""
        self.reasoning = ""
        self.final_status = "failed"
        self._delta_counter = 0
        self._done_received = False

    async def send(self, event: dict[str, Any]) -> None:
        if event.get("type") == "delta":
            self.content += str(event.get("content") or "")
            self.reasoning += str(event.get("reasoning_content") or "")
        if event.get("type") == "done":
            self._done_received = True
            outcome = str(event.get("outcome") or "failed")
            if outcome == "completed":
                self.final_status = "completed"
            elif outcome == "cancelled":
                self.final_status = "cancelled"
            else:
                self.final_status = "failed"
        await self.database.table("chat_job_events").insert(
            {"job_id": self.job_id, "payload": event}
        ).execute()
        self._delta_counter += 1
        if event.get("type") != "delta" or (
            len(self.content) + len(self.reasoning) < 200 or self._delta_counter % 10 == 0
        ):
            await self.database.table("chat_jobs").update(
                {
                    "response_content": self.content,
                    "response_reasoning": self.reasoning,
                    "updated_at": _iso(_now()),
                }
            ).eq("id", self.job_id).execute()

    async def enqueue(self, text: str) -> None:
        if "[DONE]" in text and self.final_status == "failed" and not self._done_received:
            self.final_status = "completed"


async def execute_chat_job(job_id: str) -> dict[str, str]:
    database = database_module.get_admin_database()
    response = await (
        database.table("chat_jobs")
        .select("id", "history", "status", "lease_expires_at", "stop_requested")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not response.rows:
        return {"status": "missing"}
    job = response.rows[0]
    status = str(job.get("status") or "")
    if status in TERMINAL_STATUSES:
        return {"status": status}
    if job.get("stop_requested"):
        await database.table("chat_jobs").update(
            {
                "status": "cancelled",
                "finished_at": _iso(_now()),
                "lease_expires_at": None,
                "updated_at": _iso(_now()),
            }
        ).eq("id", job_id).execute()
        return {"status": "cancelled"}
    now = _now()
    lease = _parse_iso(job.get("lease_expires_at"))
    if status == "running" and lease is not None and lease > now:
        return {"status": "running"}
    claim_query = (
        database.table("chat_jobs")
        .update(
            {
                "status": "running",
                "started_at": _iso(now),
                "updated_at": _iso(now),
                "lease_expires_at": _iso(now + timedelta(milliseconds=LEASE_MS)),
            }
        )
        .eq("id", job_id)
        .eq("status", status)
    )
    if lease is not None:
        claim_query = claim_query.eq("lease_expires_at", job.get("lease_expires_at"))
    else:
        claim_query = claim_query.is_("lease_expires_at", None)
    claimed = await claim_query.execute()
    if not claimed.rows:
        return {"status": "running"}

    cancel = asyncio.Event()
    sink = DatabaseJobSink(job_id)

    async def heartbeat() -> None:
        while not cancel.is_set():
            try:
                await asyncio.sleep(HEARTBEAT_MS / 1000)
            except asyncio.CancelledError:
                return
            try:
                response = await (
                    database.table("chat_jobs")
                    .update(
                        {
                            "lease_expires_at": _iso(
                                _now() + timedelta(milliseconds=LEASE_MS)
                            ),
                            "updated_at": _iso(_now()),
                        }
                    )
                    .eq("id", job_id)
                    .select("stop_requested")
                    .execute()
                )
                if response.rows and response.rows[0].get("stop_requested"):
                    cancel.set()
                    return
            except Exception as exc:
                logger.warning("heartbeat_failed", extra={"job_id": job_id, "error": str(exc)})
                continue

    heartbeat_task = asyncio.create_task(heartbeat())
    history = job.get("history")
    if not isinstance(history, list):
        history = []
    try:
        outcome = await run_durable_chat(job_id, history, sink.send, sink.enqueue, cancel)
    except asyncio.CancelledError:
        outcome = TerminalState.CANCELLED
    except Exception as exc:
        outcome = TerminalState.FAILED
        sink.final_status = "failed"
        await sink.send(
            {
                "type": "error",
                "task_id": job_id,
                "code": "BACKGROUND_FAILURE",
                "message": str(exc) or "A háttérmunka hibával leállt.",
            }
        )
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    final_status = sink.final_status
    if final_status == "failed" and outcome == TerminalState.COMPLETED:
        final_status = "completed"
    if final_status == "failed" and outcome == TerminalState.CANCELLED:
        final_status = "cancelled"
    error_message = (
        "A háttérmunka hibával leállt." if final_status == "failed" else None
    )
    await database.table("chat_jobs").update(
        {
            "status": final_status,
            "response_content": sink.content,
            "response_reasoning": sink.reasoning,
            "error": error_message,
            "finished_at": _iso(_now()),
            "lease_expires_at": None,
            "updated_at": _iso(_now()),
        }
    ).eq("id", job_id).execute()
    return {"status": final_status}


async def follow_chat_job(
    job_id: str, after: int, request_url: str
) -> AsyncIterator[str]:
    database = database_module.get_admin_database()
    cursor = after
    started_at = asyncio.get_running_loop().time()
    last_heartbeat = 0.0
    last_revive = asyncio.get_running_loop().time()
    try:
        await revive_stale_chat_job(job_id, request_url)
    except Exception:
        pass
    while True:
        if asyncio.get_running_loop().time() - started_at > FOLLOW_MAX_MS / 1000:
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
            response = await (
                database.table("chat_job_events")
                .select("id", "payload")
                .eq("job_id", job_id)
                .gt("id", cursor)
                .order("id", desc=False)
                .limit(200)
                .execute()
            )
            terminal = False
            for row in response.rows:
                cursor = max(cursor, int(row.get("id") or 0))
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    payload = {"type": "unknown", "payload": payload}
                yield sse_frame(payload, row.get("id"))
                if payload.get("type") in ("done", "error"):
                    terminal = True
            if terminal:
                return
            if response.rows:
                continue
            job_response = await (
                database.table("chat_jobs").select("status").eq("id", job_id).limit(1).execute()
            )
            if not job_response.rows or str(
                job_response.rows[0].get("status") or ""
            ) not in ("queued", "running"):
                rest = await (
                    database.table("chat_job_events")
                    .select("id", "payload")
                    .eq("job_id", job_id)
                    .gt("id", cursor)
                    .order("id", desc=False)
                    .limit(200)
                    .execute()
                )
                for row in rest.rows:
                    cursor = max(cursor, int(row.get("id") or 0))
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        payload = {"type": "unknown", "payload": payload}
                    yield sse_frame(payload, row.get("id"))
                return
            now = asyncio.get_running_loop().time()
            if now - last_revive >= FOLLOW_REVIVE_MS / 1000:
                last_revive = now
                try:
                    await revive_stale_chat_job(job_id, request_url)
                except Exception:
                    pass
        except Exception as exc:
            yield sse_frame(
                {
                    "type": "error",
                    "task_id": job_id,
                    "code": "FOLLOW_FAILURE",
                    "message": str(exc) or "Az eseményfolyam megszakadt.",
                }
            )
            return
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= FOLLOW_HEARTBEAT_MS / 1000:
            last_heartbeat = now
            yield sse_comment("heartbeat")
        await asyncio.sleep(FOLLOW_POLL_MS / 1000)


