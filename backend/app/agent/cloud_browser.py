from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app import database as database_module
from app.config import get_settings

API_URL = "https://api.browser-use.com/api/v4/browsers"
BROWSER_SCREEN_WIDTH = 1280
BROWSER_SCREEN_HEIGHT = 1029
MAX_TIMEOUT_MINUTES = 240
STOP_POLL_SECONDS = 2
STOP_DEADLINE_SECONDS = 90


class CloudBrowserError(RuntimeError):
    pass


def _api_key() -> str:
    key = get_settings().browser_use_api_key
    if not key:
        raise CloudBrowserError("A BROWSER_USE_API_KEY nincs beállítva.")
    return key


async def _call(
    path: str, method: str, body: Any, cancel: asyncio.Event
) -> dict[str, Any]:
    headers = {
        "X-Browser-Use-API-Key": _api_key(),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        request = client.build_request(
            method, f"{API_URL}{path}", headers=headers, json=body
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise CloudBrowserError(f"Browser Use: {exc}") from exc
        try:
            text = (await response.aread()).decode("utf-8", errors="replace")
        finally:
            await response.aclose()
    if cancel.is_set():
        raise asyncio.CancelledError()
    if response.status_code < 200 or response.status_code >= 300:
        raise CloudBrowserError(f"Browser Use ({response.status_code}): {text[:500]}")
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CloudBrowserError("Browser Use: érvénytelen JSON válasz.") from exc
    if not isinstance(parsed, dict):
        raise CloudBrowserError("Browser Use: érvénytelen válaszobjektum.")
    return parsed


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
        browser = await _call(
            "",
            "POST",
            {
                "proxyCountryCode": country_code or "us",
                "timeout": min(MAX_TIMEOUT_MINUTES, max(1, timeout_minutes or MAX_TIMEOUT_MINUTES)),
                "allowResizing": True,
                "enableRecording": True,
                "browserScreenWidth": BROWSER_SCREEN_WIDTH,
                "browserScreenHeight": BROWSER_SCREEN_HEIGHT,
            },
            cancel,
        )
        browser_id = str(browser.get("id") or "")
        cdp_url = browser.get("cdpUrl")
        if not browser_id or not isinstance(cdp_url, str) or not cdp_url:
            raise CloudBrowserError("A böngésző nem adott vissza CDP címet.")
        return {
            "status": "completed",
            "result": {
                "browser_id": browser_id,
                "cdp_url": cdp_url,
                "live_url": browser.get("liveUrl"),
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
        await _call(f"/{browser_id}", "PATCH", {"action": "stop"}, cancel)
        deadline = asyncio.get_running_loop().time() + STOP_DEADLINE_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            state = await _call(f"/{browser_id}", "GET", None, cancel)
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
                    "result": {"browser_id": browser_id, "stopped": True, "recording_url": None},
                }
            await asyncio.sleep(STOP_POLL_SECONDS)
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
    database = database_module.get_admin_database()
    response = await (
        database.table("chat_browser_sessions")
        .insert(
            {
                "job_id": job_id,
                "browser_id": str(result.get("browser_id")),
                "cdp_url": str(result.get("cdp_url")),
                "live_url": result.get("live_url")
                if isinstance(result.get("live_url"), str)
                else None,
                "agent_busy": True,
            }
        )
        .execute()
    )
    if not response.rows:
        raise CloudBrowserError("A böngészőnézet mentése sikertelen.")
    return str(response.rows[0].get("id"))


async def wait_for_browser_control(job_id: str, cancel: asyncio.Event) -> None:
    database = database_module.get_admin_database()
    while not cancel.is_set():
        response = await database.rpc(
            "claim_chat_browser_step", {"_job_id": job_id}
        ).execute()
        if response.data is True:
            return
        await asyncio.sleep(0.7)
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
