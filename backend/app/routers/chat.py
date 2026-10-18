from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import database as database_module
from app.errors import AgentFault, RequestFault
from app.services.chat_jobs import dispatch_chat_job, follow_chat_job
from app.utils.rate_limit import client_key, rate_limiter
from app.utils.sse import NO_STORE_HEADERS
from app.utils.validation import (
    parse_conversation_id,
    parse_json_body,
    validate_client_history,
)

router = APIRouter()

MAX_REQUEST_BYTES = 1_048_576


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_response(error: AgentFault) -> JSONResponse:
    status = 400 if error.status_code == 499 else error.status_code
    return JSONResponse(
        status_code=status,
        content={"error": error.to_payload()},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/chat")
async def post_chat(request: Request) -> Any:
    try:
        rate_limiter.check("chat", client_key(request))
    except AgentFault as error:
        return _error_response(error)
    try:
        content_type = request.headers.get("content-type", "")
        if not re.match(r"^application/json(?:\s*;|\s*$)", content_type, re.IGNORECASE):
            raise RequestFault(
                "application/json szükséges.", code="UNSUPPORTED_MEDIA_TYPE", status_code=415
            )
        declared = request.headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) > MAX_REQUEST_BYTES):
            raise RequestFault("A kérés túl nagy.", code="REQUEST_TOO_LARGE", status_code=413)
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            raise RequestFault("A kérés túl nagy.", code="REQUEST_TOO_LARGE", status_code=413)
        body = parse_json_body(raw)
        history = validate_client_history(body)
        conversation_id = parse_conversation_id(body.get("conversation_id")) or str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        database = database_module.get_admin_database()
        first_user = next(
            (message for message in history if message.get("role") == "user"), None
        )
        title = (
            " ".join(str(first_user.get("content") or "").split())[:80]
            if first_user
            else "Beszélgetés"
        )
        now = _now()
        await database.table("chat_conversations").upsert(
            {
                "id": conversation_id,
                "title": title,
                "messages": history,
                "updated_at": now,
            },
            on_conflict="id",
        ).execute()
        await database.table("chat_jobs").insert(
            {
                "id": job_id,
                "conversation_id": conversation_id,
                "history": history,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
            }
        ).execute()
        await dispatch_chat_job(job_id, str(request.url))
    except AgentFault as error:
        return _error_response(error)
    except Exception:
        return _error_response(
            AgentFault("INTERNAL_ERROR", "Belső kiszolgálói hiba.", 500)
        )

    async def stream() -> AsyncIterator[bytes]:
        meta = json.dumps(
            {"type": "meta", "conversation_id": conversation_id, "job_id": job_id},
            ensure_ascii=False,
        )
        yield f"data: {meta}\n\n".encode()
        async for frame in follow_chat_job(job_id, 0, str(request.url)):
            yield frame.encode("utf-8")

    headers = {
        **NO_STORE_HEADERS,
        "Content-Type": "text/event-stream; charset=utf-8",
        "X-Chat-Job-Id": job_id,
    }
    return StreamingResponse(stream(), status_code=200, headers=headers)
