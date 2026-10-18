from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import database as database_module
from app.utils.rate_limit import client_key, rate_limiter
from app.utils.security import is_loose_uuid

router = APIRouter()

MAX_CONVERSATION_IDS = 100
MAX_EVENT_ROWS = 1000


@router.get("/api/conversations")
async def get_conversations(request: Request) -> Any:
    try:
        rate_limiter.check("conversations", client_key(request))
    except Exception:
        return JSONResponse(
            status_code=429,
            content={"error": "Túl sok kérés. Próbáld újra később."},
            headers={"Cache-Control": "no-store"},
        )
    raw_ids = request.query_params.get("ids") or ""
    ids = [
        value.strip()
        for value in raw_ids.split(",")
        if is_loose_uuid(value.strip())
    ][:MAX_CONVERSATION_IDS]
    if not ids:
        return JSONResponse(
            status_code=200, content={"conversations": []}, headers={"Cache-Control": "no-store"}
        )
    database = database_module.get_admin_database()
    conversations_response = await (
        database.table("chat_conversations")
        .select("id", "title", "messages", "created_at", "updated_at")
        .in_("id", ids)
        .execute()
    )
    jobs_response = await (
        database.table("chat_jobs")
        .select(
            "id",
            "conversation_id",
            "status",
            "response_content",
            "response_reasoning",
            "error",
            "created_at",
            "updated_at",
        )
        .in_("conversation_id", ids)
        .order("created_at", desc=True)
        .execute()
    )
    latest_by_conversation: dict[str, dict[str, Any]] = {}
    for job in jobs_response.rows:
        conversation_id = str(job.get("conversation_id") or "")
        if conversation_id and conversation_id not in latest_by_conversation:
            latest_by_conversation[conversation_id] = job
    job_ids = [str(job.get("id")) for job in jobs_response.rows if job.get("id")]
    last_event_by_job: dict[str, int] = {}
    if job_ids:
        events_response = await (
            database.table("chat_job_events")
            .select("job_id", "id")
            .in_("job_id", job_ids)
            .order("id", desc=True)
            .limit(MAX_EVENT_ROWS)
            .execute()
        )
        for row in events_response.rows:
            job_id = str(row.get("job_id") or "")
            if job_id and job_id not in last_event_by_job:
                last_event_by_job[job_id] = int(row.get("id") or 0)
    result = []
    for conversation in conversations_response.rows:
        conversation_id = str(conversation.get("id") or "")
        job = latest_by_conversation.get(conversation_id)
        result.append(
            {
                "conversation_id": conversation_id,
                "title": conversation.get("title"),
                "messages": conversation.get("messages"),
                "created_at": conversation.get("created_at"),
                "updated_at": conversation.get("updated_at"),
                "latest_job": (
                    {
                        "job_id": job.get("id"),
                        "conversation_id": job.get("conversation_id"),
                        "status": job.get("status"),
                        "response_content": job.get("response_content"),
                        "response_reasoning": job.get("response_reasoning"),
                        "error": job.get("error"),
                        "last_event_id": last_event_by_job.get(str(job.get("id")), 0),
                    }
                    if job
                    else None
                ),
            }
        )
    return JSONResponse(
        status_code=200, content={"conversations": result}, headers={"Cache-Control": "no-store"}
    )
