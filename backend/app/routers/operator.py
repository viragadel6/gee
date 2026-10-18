from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent import cognitive as cog
from app.utils.rate_limit import client_key, rate_limiter
from app.utils.security import is_loose_uuid

router = APIRouter()

MAX_OPERATOR_MESSAGE_LENGTH = 32_000


@router.post("/api/operator")
async def post_operator_message(request: Request) -> Any:
    try:
        rate_limiter.check("operator", client_key(request))
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
            status_code=400, content={"error": "Hibás JSON."}, headers={"Cache-Control": "no-store"}
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400, content={"error": "Hibás JSON."}, headers={"Cache-Control": "no-store"}
        )
    task_id = body.get("task_id")
    content = body.get("content")
    if not isinstance(task_id, str) or not is_loose_uuid(task_id):
        return JSONResponse(
            status_code=400, content={"error": "Hibás task_id."}, headers={"Cache-Control": "no-store"}
        )
    if not isinstance(content, str) or not content.strip():
        return JSONResponse(
            status_code=400, content={"error": "Üres üzenet."}, headers={"Cache-Control": "no-store"}
        )
    if len(content) > MAX_OPERATOR_MESSAGE_LENGTH:
        return JSONResponse(
            status_code=400,
            content={"error": "Az üzenet túl hosszú."},
            headers={"Cache-Control": "no-store"},
        )
    try:
        await cog.post_operator_message(task_id, content.strip())
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": str(exc) or "Mentési hiba."},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        status_code=200, content={"accepted": True}, headers={"Cache-Control": "no-store"}
    )
