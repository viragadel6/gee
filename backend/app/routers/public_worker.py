from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.chat_jobs import execute_chat_job
from app.utils.rate_limit import client_key, rate_limiter
from app.utils.security import is_loose_uuid

router = APIRouter()


@router.post("/api/public/chat-worker")
async def post_chat_worker(request: Request) -> Any:
    try:
        rate_limiter.check("public_worker", client_key(request))
    except Exception:
        return JSONResponse(
            status_code=429,
            content={"error": "Túl sok kérés. Próbáld újra később."},
            headers={"Cache-Control": "no-store"},
        )
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={"error": "Érvénytelen kérés."},
            headers={"Cache-Control": "no-store"},
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "Érvénytelen kérés."},
            headers={"Cache-Control": "no-store"},
        )
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not is_loose_uuid(job_id):
        return JSONResponse(
            status_code=400,
            content={"error": "Érvénytelen munkaazonosító."},
            headers={"Cache-Control": "no-store"},
        )
    try:
        result = await execute_chat_job(job_id)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc) or "A háttérmunka hibával leállt."},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(status_code=200, content=result, headers={"Cache-Control": "no-store"})
