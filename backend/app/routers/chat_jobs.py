from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import database as database_module
from app.agent import cognitive as cog
from app.services.chat_jobs import follow_chat_job
from app.utils.security import is_loose_uuid
from app.utils.sse import NO_STORE_HEADERS

router = APIRouter()

MAX_OPERATOR_MESSAGE_LENGTH = 32_000


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": message}, headers={"Cache-Control": "no-store"}
    )


@router.get("/api/chat/jobs/{job_id}/events")
async def get_job_events(job_id: str, request: Request) -> Any:
    if not is_loose_uuid(job_id):
        return _error("Érvénytelen munkaazonosító.")
    after_raw = request.query_params.get("after") or "0"
    try:
        after = max(0, int(after_raw))
    except ValueError:
        after = 0

    async def stream() -> AsyncIterator[bytes]:
        async for frame in follow_chat_job(job_id, after, str(request.url)):
            yield frame.encode("utf-8")

    headers = {
        **NO_STORE_HEADERS,
        "Content-Type": "text/event-stream; charset=utf-8",
    }
    return StreamingResponse(stream(), status_code=200, headers=headers)


@router.post("/api/chat/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> Any:
    if not is_loose_uuid(job_id):
        return _error("Érvénytelen munkaazonosító.")
    database = database_module.get_admin_database()
    response = await (
        database.table("chat_jobs").select("id", "status").eq("id", job_id).limit(1).execute()
    )
    if not response.rows:
        return _error("A munka nem található.", 404)
    status = str(response.rows[0].get("status") or "")
    if status == "queued":
        await database.table("chat_jobs").update(
            {
                "status": "cancelled",
                "stop_requested": True,
                "finished_at": _now(),
                "updated_at": _now(),
            }
        ).eq("id", job_id).execute()
    elif status == "running":
        await database.table("chat_jobs").update(
            {"stop_requested": True, "updated_at": _now()}
        ).eq("id", job_id).execute()
    return JSONResponse(status_code=200, content={"ok": True})


@router.post("/api/chat/jobs/{job_id}/redirect")
async def redirect_job(job_id: str, request: Request) -> Any:
    if not is_loose_uuid(job_id):
        return _error("Érvénytelen munkaazonosító.")
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("Érvénytelen kérés.")
    if not isinstance(body, dict):
        return _error("Érvénytelen kérés.")
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("Üres üzenet nem küldhető.")
    message = message.strip()
    if len(message) > MAX_OPERATOR_MESSAGE_LENGTH:
        return _error("Az üzenet túl hosszú.")
    database = database_module.get_admin_database()
    response = await (
        database.table("chat_jobs").select("id", "status").eq("id", job_id).limit(1).execute()
    )
    if not response.rows:
        return _error("A munka nem található.", 404)
    status = str(response.rows[0].get("status") or "")
    if status not in ("running", "queued"):
        return _error("A munka már lezárult, új irányt nem kaphat.", 409)
    await cog.post_operator_message(job_id, message)
    return JSONResponse(status_code=200, content={"ok": True})
