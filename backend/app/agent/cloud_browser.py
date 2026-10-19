from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app import database as database_module
from app.config import get_settings

API_URL = "https://api.browser-use.com/api/v4/browsers"
BROWSER_SCREEN_WIDTH = 1280
BROWSER_SCREEN_HEIGHT = 1029
MAX_TIMEOUT_MINUTES = 240
STOP_POLL_SECONDS = 2
STOP_DEADLINE_SECONDS = 90
CONTROL_POLL_SECONDS = 0.7
DEFAULT_PROXY_COUNTRY_CODE = "us"
REQUEST_TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0


class CloudBrowserError(RuntimeError):
    pass


def _api_key() -> str:
    key = get_settings().browser_use_api_key
    if not key:
        raise CloudBrowserError("A BROWSER_USE_API_KEY nincs beállítva.")
    return key


def _browser_path(browser_id: str) -> str:
    encoded = quote(browser_id, safe="")
    return f"/{encoded}"


def _raise_if_cancelled(cancel: Optional[asyncio.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError()


async def _sleep_unless_cancelled(cancel: asyncio.Event, seconds: float) -> None:
    if cancel.is_set():
        return
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


def _normalize_country_code(country_code: Any) -> str:
    if not isinstance(country_code, str):
        return DEFAULT_PROXY_COUNTRY_CODE
    normalized = country_code.strip().lower()
    return normalized or DEFAULT_PROXY_COUNTRY_CODE


def _normalize_timeout(timeout_minutes: Any) -> int:
    try:
        minutes = int(timeout_minutes)
    except (TypeError, ValueError, OverflowError):
        minutes = MAX_TIMEOUT_MINUTES
    if minutes <= 0:
        minutes = MAX_TIMEOUT_MINUTES
    return min(MAX_TIMEOUT_MINUTES, max(1, minutes))


async def _call(
    path: str, method: str, body: Any, cancel: Optional[asyncio.Event]
) -> dict[str, Any]:
    _raise_if_cancelled(cancel)
    headers = {
        "X-Browser-Use-API-Key": _api_key(),
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method, f"{API_URL}{path}", headers=headers, json=body
            )
    except httpx.HTTPError as exc:
        detail = str(exc) or exc.__class__.__name__
        raise CloudBrowserError(f"Browser Use: {detail}") from exc
    _raise_if_cancelled(cancel)
    text = response.content.decode("utf-8", errors="replace")
    if response.status_code < 200 or response.status_code >= 300:
        raise CloudBrowserError(f"Browser Use ({response.status_code}): {text[:500]}")
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CloudBrowserError("Browser Use: érvénytelen JSON válasz.") from exc
    if not isinstance(parsed, dict):
        raise CloudBrowserError("Browser Use: érvénytelen válaszobjektum.")
    return parsed


async def _stop_quietly(browser_id: str) -> None:
    try:
        await _call(_browser_path(browser_id), "PATCH", {"action": "stop"}, None)
    except Exception:
        return


def _failure(error: BaseException) -> dict[str, Any]:
    message = str(error) or "A felhőböngésző hibával leállt."
    return {
        "status": "error",
        "result": {"code": "CLOUD_BROWSER_FAILED", "message": message},
    }


async def start_cloud_browser(
    country_code: str, timeout_minutes: int, cancel: asyncio.Event
) -> dict[str, Any]:
    try:
        _raise_if_cancelled(cancel)
        browser = await _call(
            "",
            "POST",
            {
                "proxyCountryCode": _normalize_country_code(country_code),
                "timeout": _normalize_timeout(timeout_minutes),
                "allowResizing": True,
                "enableRecording": True,
                "browserScreenWidth": BROWSER_SCREEN_WIDTH,
                "browserScreenHeight": BROWSER_SCREEN_HEIGHT,
            },
            None,
        )
        browser_id = str(browser.get("id") or "")
        cdp_url = browser.get("cdpUrl")
        if cancel.is_set():
            if browser_id:
                await _stop_quietly(browser_id)
            raise asyncio.CancelledError()
        if not browser_id or not isinstance(cdp_url, str) or not cdp_url:
            if browser_id:
                await _stop_quietly(browser_id)
            raise CloudBrowserError("A böngésző nem adott vissza CDP címet.")
        live_url = browser.get("liveUrl")
        return {
            "status": "completed",
            "result": {
                "browser_id": browser_id,
                "cdp_url": cdp_url,
                "live_url": live_url if isinstance(live_url, str) and live_url else None,
                "usage": (
                    "Csatlakozz a VM-ből Playwrighttal: chromium.connect_over_cdp(cdp_url). "
                    "Munka végén hívd a cloud_browser_stop eszközt."
                ),
            },
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _failure(exc)


async def stop_cloud_browser(browser_id: str, cancel: asyncio.Event) -> dict[str, Any]:
    try:
        if not isinstance(browser_id, str) or not browser_id.strip():
            raise CloudBrowserError("Hiányzik a böngésző azonosítója.")
        path = _browser_path(browser_id)
        await _call(path, "PATCH", {"action": "stop"}, None)
        _raise_if_cancelled(cancel)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_DEADLINE_SECONDS
        while loop.time() < deadline:
            state = await _call(path, "GET", None, cancel)
            recording_url = state.get("recordingUrl")
            if isinstance(recording_url, str) and recording_url:
                return {
                    "status": "completed",
                    "result": {
                        "browser_id": browser_id,
                        "stopped": True,
                        "recording_url": recording_url,
                    },
                }
            if state.get("recordingAvailable") is False:
                return {
                    "status": "completed",
                    "result": {
                        "browser_id": browser_id,
                        "stopped": True,
                        "recording_url": None,
                    },
                }
            await _sleep_unless_cancelled(cancel, STOP_POLL_SECONDS)
            _raise_if_cancelled(cancel)
        return {
            "status": "completed",
            "result": {
                "browser_id": browser_id,
                "stopped": True,
                "recording_url": None,
                "message": "A felvétel még feldolgozás alatt van.",
            },
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _failure(exc)


async def register_chat_browser(job_id: str, result: dict[str, Any]) -> str:
    raw_browser_id = result.get("browser_id")
    browser_id = str(raw_browser_id).strip() if raw_browser_id is not None else ""
    if not browser_id:
        raise CloudBrowserError("Hiányzik a böngésző azonosítója.")
    cdp_url = result.get("cdp_url")
    if not isinstance(cdp_url, str) or not cdp_url.strip():
        raise CloudBrowserError("Hiányzik a böngésző CDP címe.")
    live_url = result.get("live_url")
    database = database_module.get_admin_database()
    response = await (
        database.table("chat_browser_sessions")
        .insert(
            {
                "job_id": job_id,
                "browser_id": browser_id,
                "cdp_url": cdp_url,
                "live_url": live_url if isinstance(live_url, str) and live_url else None,
                "agent_busy": True,
            }
        )
        .execute()
    )
    rows = response.data
    if not isinstance(rows, list) or not rows:
        raise CloudBrowserError("A böngészőnézet mentése sikertelen.")
    first = rows[0]
    session_id = first.get("id") if isinstance(first, dict) else None
    if session_id is None:
        raise CloudBrowserError("A böngészőnézet mentése sikertelen.")
    return str(session_id)


async def wait_for_browser_control(job_id: str, cancel: asyncio.Event) -> None:
    database = database_module.get_admin_database()
    while not cancel.is_set():
        response = await database.rpc(
            "claim_chat_browser_step", {"_job_id": job_id}
        ).execute()
        if response.data is True:
            return
        await _sleep_unless_cancelled(cancel, CONTROL_POLL_SECONDS)
    raise asyncio.CancelledError()


async def set_browser_agent_busy(job_id: str, busy: bool) -> None:
    database = database_module.get_admin_database()
    await (
        database.table("chat_browser_sessions")
        .update({"agent_busy": busy})
        .eq("job_id", job_id)
        .eq("status", "active")
        .execute()
    )
    if not busy:
        await (
            database.table("chat_browser_sessions")
            .update({"control_mode": "human"})
            .eq("job_id", job_id)
            .eq("status", "active")
            .eq("control_mode", "requested")
            .execute()
        )


async def mark_chat_browser_stopped(browser_id: str) -> None:
    database = database_module.get_admin_database()
    await (
        database.table("chat_browser_sessions")
        .update({"status": "stopped", "control_mode": "agent", "agent_busy": False})
        .eq("browser_id", browser_id)
        .execute()
    )
