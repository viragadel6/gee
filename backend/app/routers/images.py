from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.config import get_settings
from app.utils.rate_limit import client_key, rate_limiter

router = APIRouter()

FLYMYAI_EDIT_URL = (
    "https://api.flymy.ai/api/v1/flymyai/gpt-image-2-5-sunburst_edit/predict"
)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_QUALITIES = ("low", "medium", "high", "auto")
ALLOWED_SIZES = ("1024x1024", "1024x1536", "1536x1024", "auto")


@router.post("/api/images/edits")
async def post_image_edits(request: Request) -> Any:
    try:
        rate_limiter.check("images", client_key(request))
    except Exception:
        return JSONResponse(
            status_code=429,
            content={"message": "Túl sok kérés. Próbáld újra később."},
            headers={"Cache-Control": "no-store"},
        )
    settings = get_settings()
    api_key = settings.flymyai_api_key
    if not api_key:
        return JSONResponse(
            status_code=503,
            content={"message": "A FlyMyAI kulcs nincs beállítva."},
            headers={"Cache-Control": "no-store"},
        )
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        return JSONResponse(
            status_code=400,
            content={"message": "A kérés tartalma multipart/form-data kell legyen."},
            headers={"Cache-Control": "no-store"},
        )
    try:
        form = await request.form()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"message": "A képfeltöltés nem olvasható."},
            headers={"Cache-Control": "no-store"},
        )
    image = form.get("image")
    prompt = form.get("prompt")
    if (
        not isinstance(image, (bytes, bytearray))
        or len(image) == 0
        or not isinstance(prompt, str)
        or not prompt.strip()
    ):
        return JSONResponse(
            status_code=400,
            content={"message": "Adj meg egy kiinduló képet és képleírást."},
            headers={"Cache-Control": "no-store"},
        )
    if len(image) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=413,
            content={"message": "A feltöltött kép túl nagy."},
            headers={"Cache-Control": "no-store"},
        )
    quality = str(form.get("quality") or "medium")
    if quality not in ALLOWED_QUALITIES:
        quality = "medium"
    size = str(form.get("size") or "1024x1024")
    if size not in ALLOWED_SIZES:
        size = "1024x1024"
    filename = getattr(image, "filename", None) or "image.png"
    content_type_value = getattr(image, "content_type", None) or "image/png"
    files = {"image": (filename, bytes(image), content_type_value)}
    data = {"prompt": prompt, "quality": quality, "size": size}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
            upstream = await client.post(
                FLYMYAI_EDIT_URL,
                headers={"X-API-KEY": api_key},
                files=files,
                data=data,
            )
    except httpx.HTTPError:
        return JSONResponse(
            status_code=502,
            content={"message": "A FlyMyAI nem érhető el; próbáld újra később."},
            headers={"Cache-Control": "no-store"},
        )
    content_type_header = upstream.headers.get("content-type") or "application/json"
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"Content-Type": content_type_header, "Cache-Control": "no-store"},
    )
