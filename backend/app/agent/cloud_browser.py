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
CONTROL_POLL_SECONDS = 0.7
REQUEST_TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0


class CloudBrowserError(RuntimeError):
    pass


def _api_key() -> str:
    key = get_settings().browser_use_api_key
    if not key:
        raise CloudBrowserError("A BROWSER_USE_API_KEY nincs beállítva.")
    if hasattr(key, "get_secret_value"):
        key = key.get_secret_value()
    key_str = str(key).strip()
    if not key_str:
        raise CloudBrowserError("A BROWSER_USE_API_KEY nincs beállítva.")
    return key_str


def _raise_if_cancelled(cancel: asyncio.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError()


async def _sleep_unless_cancelled(seconds: float, cancel: asyncio.Event) -> None:
    _raise_if_cancelled(cancel)
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    raise asyncio.CancelledError()


async def _call(
    path: str, method: str, body: Any, cancel: asyncio.Event | None
) -> dict[str, Any]:
    _raise_if_cancelled(cancel)
    headers = {
        "X-Browser-Use-API-Key": _api_key(),
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        request = client.build_request(
            method, f"{API_URL}{path}", headers=headers, json=body
        )
        try:
            response = await client.send(request, stream=True)
            try:
                content = await response.aread()
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            detail = str(exc) or exc.__class__.__name__
            raise CloudBrowserError(f"Browser Use: {detail}") from exc
    _raise_if_cancelled(cancel)
    text = content.decode("utf-8", errors="replace")
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


def _failure(error: BaseException) -> dict[str, Any]:
    message = str(error) or "A felhőböngésző hibával leállt."
    return {
        "status": "error",
        "result": {"code": "CLOUD_BROWSER_FAILED", "message": message},
    }


async def _stop_browser_task(browser_task: asyncio.Task[dict[str, Any]]) -> None:
    try:
        browser = await browser_task
        browser_id = str(browser.get("id") or "").strip()
        if browser_id:
            await _call(f"/{browser_id}", "PATCH", {"action": "stop"}, None)
    except Exception:
        return


async def start_cloud_browser(
    country_code: str, timeout_minutes: int, cancel: asyncio.Event
) -> dict[str, Any]:
    creation_task: asyncio.Task[dict[str, Any]] | None = None
    try:
        _raise_if_cancelled(cancel)
        proxy_country = (country_code or "us").strip().lower() or "us"
        try:
            timeout_val = int(timeout_minutes)
        except (TypeError, ValueError, OverflowError):
            timeout_val = MAX_TIMEOUT_MINUTES
        if timeout_val <= 0:
            timeout_val = MAX_TIMEOUT_MINUTES
        timeout = min(MAX_TIMEOUT_MINUTES, max(1, timeout_val))

        creation_task = asyncio.create_task(
            _call(
                "",
                "POST",
                {
                    "proxyCountryCode": proxy_country,
                    "timeout": timeout,
                    "allowResizing": True,
                    "enableRecording": True,
                    "browserScreenWidth": BROWSER_SCREEN_WIDTH,
                    "browserScreenHeight": BROWSER_SCREEN_HEIGHT,
                },
                None,
            )
        )

        cancellation_task = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                (creation_task, cancellation_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                raise asyncio.CancelledError()
            browser = creation_task.result()
        finally:
            cancellation_task.cancel()

        browser_id = str(browser.get("id") or "").strip()
        cdp_url = browser.get("cdpUrl")
        if not browser_id or not isinstance(cdp_url, str) or not cdp_url.strip():
            if creation_task is not None:
                await _stop_browser_task(creation_task)
            raise CloudBrowserError("A böngésző nem adott vissza CDP címet.")

        live_url = browser.get("liveUrl")
        return {
            "status": "completed",
            "result": {
                "browser_id": browser_id,
                "cdp_url": cdp_url.strip(),
                "live_url": live_url if isinstance(live_url, str) and live_url else None,
                "usage": (
                    "Csatlakozz a VM-ből Playwrighttal: chromium.connect_over_cdp(cdp_url). "
                    "Munka végén hívd a cloud_browser_stop eszközt."
                ),
            },
        }
    except asyncio.CancelledError:
        if creation_task is not None:
            await _stop_browser_task(creation_task)
        raise
    except Exception as exc:
        if creation_task is not None:
            await _stop_browser_task(creation_task)
        return _failure(exc)


async def stop_cloud_browser(browser_id: str, cancel: asyncio.Event) -> dict[str, Any]:
    try:
        normalized_id = str(browser_id or "").strip()
        if not normalized_id:
            raise CloudBrowserError("Hiányzik a böngésző azonosítója.")
        await _call(f"/{normalized_id}", "PATCH", {"action": "stop"}, None)
        _raise_if_cancelled(cancel)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_DEADLINE_SECONDS
        while loop.time() < deadline:
            state = await _call(f"/{normalized_id}", "GET", None, cancel)
            recording_url = state.get("recordingUrl")
            if isinstance(recording_url, str) and recording_url.strip():
                return {
                    "status": "completed",
                    "result": {
                        "browser_id": normalized_id,
                        "stopped": True,
                        "recording_url": recording_url.strip(),
                    },
                }
            if state.get("recordingAvailable") is False:
                return {
                    "status": "completed",
                    "result": {
                        "browser_id": normalized_id,
                        "stopped": True,
                        "recording_url": None,
                    },
                }
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await _sleep_unless_cancelled(min(float(STOP_POLL_SECONDS), remaining), cancel)
        return {
            "status": "completed",
            "result": {
                "browser_id": normalized_id,
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
    raw_job_id = str(job_id or "").strip()
    if not raw_job_id:
        raise CloudBrowserError("Hiányzik a feladat azonosítója.")
    if not isinstance(result, dict):
        raise CloudBrowserError("Érvénytelen böngészőeredmény.")
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
                "job_id": raw_job_id,
                "browser_id": browser_id,
                "cdp_url": cdp_url.strip(),
                "live_url": live_url if isinstance(live_url, str) and live_url else None,
                "agent_busy": True,
            }
        )
        .execute()
    )
    rows = response.data
    if not isinstance(rows, list) or not rows:
        raise CloudBrowserError("A böngészőnézet mentése sikertelen.")
    first_row = rows[0]
    session_id = first_row.get("id") if isinstance(first_row, dict) else None
    if session_id is None or not str(session_id).strip():
        raise CloudBrowserError("A böngészőnézet mentése sikertelen.")
    return str(session_id).strip()


async def wait_for_browser_control(job_id: str, cancel: asyncio.Event) -> None:
    _raise_if_cancelled(cancel)
    raw_job_id = str(job_id or "").strip()
    if not raw_job_id:
        raise CloudBrowserError("Hiányzik a feladat azonosítója.")
    database = database_module.get_admin_database()
    while not cancel.is_set():
        response = await database.rpc(
            "claim_chat_browser_step", {"_job_id": raw_job_id}
        ).execute()
        if response.data is True:
            return
        await _sleep_unless_cancelled(CONTROL_POLL_SECONDS, cancel)
    raise asyncio.CancelledError()


async def set_browser_agent_busy(job_id: str, busy: bool) -> None:
    raw_job_id = str(job_id or "").strip()
    if not raw_job_id:
        raise CloudBrowserError("Hiányzik a feladat azonosítója.")
    database = database_module.get_admin_database()
    await (
        database.table("chat_browser_sessions")
        .update({"agent_busy": bool(busy)})
        .eq("job_id", raw_job_id)
        .eq("status", "active")
        .execute()
    )
    if not busy:
        await (
            database.table("chat_browser_sessions")
            .update({"control_mode": "human"})
            .eq("job_id", raw_job_id)
            .eq("status", "active")
            .eq("control_mode", "requested")
            .execute()
        )


async def mark_chat_browser_stopped(browser_id: str) -> None:
    raw_browser_id = str(browser_id or "").strip()
    if not raw_browser_id:
        raise CloudBrowserError("Hiányzik a böngésző azonosítója.")
    database = database_module.get_admin_database()
    await (
        database.table("chat_browser_sessions")
        .update({"status": "stopped", "control_mode": "agent", "agent_busy": False})
        .eq("browser_id", raw_browser_id)
        .execute()
    )
