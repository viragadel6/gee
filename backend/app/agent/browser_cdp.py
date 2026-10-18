from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import connect as ws_connect

CDP_CONNECT_TIMEOUT_SECONDS = 15
CDP_COMMAND_TIMEOUT_SECONDS = 15


class BrowserCdpError(RuntimeError):
    pass


async def browser_command(
    url: str, action: str, input_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
    next_id = 0
    loop = asyncio.get_running_loop()

    async with ws_connect(
        url,
        open_timeout=CDP_CONNECT_TIMEOUT_SECONDS,
        close_timeout=5,
        max_size=64 * 1024 * 1024,
    ) as socket:

        async def send(
            method: str, params: dict[str, Any] | None = None, session_id: str | None = None
        ) -> dict[str, Any]:
            nonlocal next_id
            next_id += 1
            request_id = next_id
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            pending[request_id] = future
            payload: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
            if session_id:
                payload["sessionId"] = session_id
            await socket.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=CDP_COMMAND_TIMEOUT_SECONDS)

        async def receive() -> None:
            try:
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    request_id = message.get("id")
                    waiter = pending.pop(request_id, None)
                    if waiter is None or waiter.done():
                        continue
                    error = message.get("error")
                    if error:
                        message_text = (
                            error.get("message") if isinstance(error, dict) else str(error)
                        )
                        waiter.set_exception(
                            BrowserCdpError(str(message_text or "CDP hiba"))
                        )
                    else:
                        result = message.get("result")
                        waiter.set_result(result if isinstance(result, dict) else {})
            except asyncio.CancelledError:
                raise
            except Exception:
                for waiter in pending.values():
                    if not waiter.done():
                        waiter.set_exception(
                            BrowserCdpError("A böngésző kapcsolata megszakadt.")
                        )
                return

        receiver = asyncio.create_task(receive())
        try:
            targets = await send("Target.getTargets")
            target_infos = targets.get("targetInfos")
            pages = [
                target
                for target in (target_infos or [])
                if isinstance(target, dict) and target.get("type") == "page"
            ]
            page = next(
                (
                    target
                    for target in pages
                    if not str(target.get("url") or "").startswith("chrome-extension:")
                ),
                None,
            )
            if page is None:
                raise BrowserCdpError("Nincs megnyitott böngészőlap.")
            attached = await send(
                "Target.attachToTarget",
                {"targetId": page.get("targetId"), "flatten": True},
            )
            session_id = str(attached.get("sessionId"))
            if action == "frame":
                result = await send(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": 75, "captureBeyondViewport": False},
                    session_id,
                )
                result["url"] = page.get("url", "")
                return result
            if not input_data:
                raise BrowserCdpError("Hiányzó beviteli művelet.")
            kind = input_data.get("kind")
            if kind == "text":
                await send(
                    "Input.insertText", {"text": input_data.get("text", "")}, session_id
                )
            elif kind == "key":
                await send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": input_data.get("type"),
                        "key": input_data.get("key"),
                        "code": input_data.get("code"),
                        "windowsVirtualKeyCode": input_data.get("keyCode"),
                        "nativeVirtualKeyCode": input_data.get("keyCode"),
                        "modifiers": input_data.get("modifiers", 0),
                    },
                    session_id,
                )
            else:
                params: dict[str, Any] = {
                    "type": input_data.get("type"),
                    "x": input_data.get("x"),
                    "y": input_data.get("y"),
                    "button": input_data.get("button", "none"),
                    "buttons": input_data.get("buttons", 0),
                    "clickCount": input_data.get("clickCount", 0),
                }
                if kind == "wheel":
                    params["deltaX"] = input_data.get("deltaX", 0)
                    params["deltaY"] = input_data.get("deltaY", 0)
                await send("Input.dispatchMouseEvent", params, session_id)
            return {"ok": True}
        finally:
            receiver.cancel()
            try:
                await receiver
            except (asyncio.CancelledError, Exception):
                pass
