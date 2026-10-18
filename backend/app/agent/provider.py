from __future__ import annotations

import asyncio
import codecs
import json
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Awaitable, TypeVar

import httpx

from app.errors import (
    AgentFault,
    ProtocolFault,
    ProviderFault,
    RuntimeLimitFault,
)
from app.models import ModelResponse, RuntimeConfig, ToolCall

T = TypeVar("T")

MAX_SSE_EVENT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 1_048_576
MAX_TOOL_NAME_CHARS = 200
MAX_TOOL_ARGUMENT_CHARS = 1_048_576
MAX_CALLS_PER_STEP = 16
MAX_UPSTREAM_RETRIES = 5
MODEL_CONNECT_TIMEOUT_SECONDS = 120
MODEL_IDLE_TIMEOUT_SECONDS = 180


@dataclass
class ToolCallBuilder:
    id: str = ""
    name: str = ""
    name_parts: int = 0
    name_snapshot: str = ""
    name_snapshot_valid: bool = True
    name_overflow: bool = False
    arguments: str = ""
    argument_parts: int = 0
    argument_snapshot: str = ""
    argument_snapshot_valid: bool = True
    argument_overflow: bool = False


@dataclass
class ModelStream:
    response: httpx.Response
    client: httpx.AsyncClient
    touch: Any


async def await_with_cancel(awaitable: Awaitable[T], cancel: asyncio.Event) -> T:
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancel.wait())
    try:
        done, pending = await asyncio.wait(
            {task, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise AgentFault("CANCELLED", "A feladat megszakadt.", 499)
    finally:
        for pending_task in (task, waiter):
            if not pending_task.done():
                pending_task.cancel()
        if waiter.done() and not waiter.cancelled():
            waiter.exception()


def _append_fragment(
    combined: str,
    snapshot: str,
    parts: int,
    snapshot_valid: bool,
    overflow: bool,
    fragment: str,
    limit: int,
) -> tuple[str, str, int, bool, bool]:
    if not fragment:
        return combined, snapshot, parts, snapshot_valid, overflow
    if len(fragment) > limit:
        raise ProtocolFault("Az eszközhívás túl nagy.")
    if parts > 0 and not fragment.startswith(snapshot):
        snapshot_valid = False
    snapshot = fragment
    parts += 1
    if not overflow:
        if len(combined) + len(fragment) > limit:
            if parts < 2 or not snapshot_valid:
                raise ProtocolFault("Az eszközhívás túl nagy.")
            overflow = True
            combined = ""
        else:
            combined += fragment
    if overflow and not snapshot_valid:
        raise ProtocolFault("Az eszközhívás túl nagy vagy következetlen.")
    return combined, snapshot, parts, snapshot_valid, overflow


def _resolve_fragment(
    combined: str,
    snapshot: str,
    parts: int,
    snapshot_valid: bool,
    overflow: bool,
    accept,
) -> str:
    snapshot_usable = parts >= 2 and snapshot_valid
    if overflow:
        return snapshot if snapshot_usable else ""
    if snapshot_usable and not accept(combined) and accept(snapshot):
        return snapshot
    return combined


def _parses_as_json(value: str) -> bool:
    try:
        json.loads(value)
        return True
    except json.JSONDecodeError:
        return False


def _is_tool_name(value: str) -> bool:
    from app.agent.tools import is_tool_name

    return is_tool_name(value)


def finalize_tool_call(builder: ToolCallBuilder) -> ToolCall:
    name = _resolve_fragment(
        builder.name,
        builder.name_snapshot,
        builder.name_parts,
        builder.name_snapshot_valid,
        builder.name_overflow,
        _is_tool_name,
    )
    arguments = _resolve_fragment(
        builder.arguments,
        builder.argument_snapshot,
        builder.argument_parts,
        builder.argument_snapshot_valid,
        builder.argument_overflow,
        _parses_as_json,
    )
    return ToolCall(
        id=builder.id,
        type="function",
        function_name=name,
        function_arguments=arguments if arguments else "{}",
    )


def validate_tool_delta(choice: dict[str, Any], parts: dict[int, ToolCallBuilder]) -> None:
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return
    incoming = delta.get("tool_calls")
    if incoming is None:
        return
    if not isinstance(incoming, list) or len(incoming) > MAX_CALLS_PER_STEP:
        raise ProtocolFault("Érvénytelen eszközfragmentum lista.")
    for item in incoming:
        if not isinstance(item, dict):
            raise ProtocolFault("Hiányzó vagy érvénytelen eszközindex.")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= MAX_CALLS_PER_STEP:
            raise ProtocolFault("Hiányzó vagy érvénytelen eszközindex.")
        previous = parts.get(index)
        if previous is None:
            previous = ToolCallBuilder()
        item_type = item.get("type")
        if item_type is not None and item_type != "function":
            raise ProtocolFault("Nem támogatott eszköztípus.")
        item_id = item.get("id")
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id or (previous.id and previous.id != item_id):
                raise ProtocolFault("Ütköző vagy hiányos eszközazonosító.")
            previous.id = item_id
        function = item.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise ProtocolFault("Hibás függvényfragmentum.")
            name = function.get("name")
            if name is not None:
                if not isinstance(name, str):
                    raise ProtocolFault("Hibás eszköznév.")
                (
                    previous.name,
                    previous.name_snapshot,
                    previous.name_parts,
                    previous.name_snapshot_valid,
                    previous.name_overflow,
                ) = _append_fragment(
                    previous.name,
                    previous.name_snapshot,
                    previous.name_parts,
                    previous.name_snapshot_valid,
                    previous.name_overflow,
                    name,
                    MAX_TOOL_NAME_CHARS,
                )
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    raise ProtocolFault("Hibás argumentumfragmentum.")
                (
                    previous.arguments,
                    previous.argument_snapshot,
                    previous.argument_parts,
                    previous.argument_snapshot_valid,
                    previous.argument_overflow,
                ) = _append_fragment(
                    previous.arguments,
                    previous.argument_snapshot,
                    previous.argument_parts,
                    previous.argument_snapshot_valid,
                    previous.argument_overflow,
                    arguments,
                    MAX_TOOL_ARGUMENT_CHARS,
                )
        parts[index] = previous


async def parse_sse(
    body: AsyncIterator[bytes], cancel: asyncio.Event, touch
) -> AsyncIterator[str]:
    buffer = ""
    data_lines: list[str] = []
    size = 0
    leading = True
    pending_cr = False
    decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def parse_line(line: str) -> str | None:
        nonlocal data_lines, size
        if line == "":
            if not data_lines:
                return None
            out = "\n".join(data_lines)
            data_lines = []
            size = 0
            return out
        if line.startswith(":"):
            return None
        colon = line.find(":")
        if colon < 0:
            field = line
            value = ""
        else:
            field = line[:colon]
            value = line[colon + 1 :]
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
            size += len(value.encode("utf-8")) + 1
            if size > MAX_SSE_EVENT_BYTES:
                raise ProtocolFault("Túl nagy SSE esemény.", code="STREAM_TOO_LARGE")
        return None

    iterator = body.__aiter__()
    while True:
        try:
            chunk = await await_with_cancel(iterator.__anext__(), cancel)
        except StopAsyncIteration:
            break
        touch()
        if len(chunk) > MAX_SSE_EVENT_BYTES:
            raise ProtocolFault("Túl nagy SSE átviteli blokk.", code="STREAM_TOO_LARGE")
        try:
            buffer += decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise ProtocolFault(
                "Érvénytelen UTF-8 a modell adatfolyamában.", code="MODEL_ENCODING"
            ) from exc
        if leading:
            leading = False
            if buffer.startswith("﻿"):
                buffer = buffer[1:]
        if len(buffer) > MAX_SSE_EVENT_BYTES and "\n" not in buffer and "\r" not in buffer:
            raise ProtocolFault("Túl hosszú SSE sor.", code="STREAM_TOO_LARGE")
        index = 0
        for position in range(len(buffer)):
            char = buffer[position]
            if pending_cr:
                pending_cr = False
                if char == "\n":
                    index = position + 1
                    continue
            if char != "\n" and char != "\r":
                continue
            line = buffer[index:position]
            event = parse_line(line)
            if event is not None:
                yield event
            pending_cr = char == "\r"
            index = position + 1
        buffer = buffer[index:]
    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ProtocolFault(
            "Érvénytelen záró UTF-8 szekvencia.", code="MODEL_ENCODING"
        ) from exc
    if buffer:
        event = parse_line(buffer)
        if event is not None:
            yield event
    trailing = parse_line("")
    if trailing is not None:
        yield trailing


def retry_delay(response: httpx.Response | None, attempt: int) -> float:
    wait = -1.0
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            header = header.strip()
            if header.isdigit():
                wait = float(header) * 1000
            else:
                try:
                    parsed = parsedate_to_datetime(header)
                    if parsed is not None:
                        import time

                        wait = max(0.0, parsed.timestamp() * 1000 - time.time() * 1000)
                except (TypeError, ValueError):
                    wait = -1.0
    if wait < 0:
        wait = min(30_000, 1000 * 2**attempt) * (0.5 + random.random() * 0.5)
    return min(60_000, wait) / 1000


def _now() -> float:
    return asyncio.get_running_loop().time()


async def call_model(
    config: RuntimeConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    cancel: asyncio.Event,
    deadline: float,
) -> ModelStream:
    from app.config import get_settings

    settings = get_settings()
    payload: dict[str, Any] = {
        "model": settings.model_name,
        "messages": messages,
        "stream": True,
        "max_completion_tokens": 8192,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=MODEL_CONNECT_TIMEOUT_SECONDS,
            read=MODEL_IDLE_TIMEOUT_SECONDS,
            write=MODEL_CONNECT_TIMEOUT_SECONDS,
            pool=MODEL_CONNECT_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
    )
    for attempt in range(MAX_UPSTREAM_RETRIES + 1):
        if cancel.is_set():
            await client.aclose()
            raise AgentFault("CANCELLED", "A feladat megszakadt.", 499)
        if deadline - _now() <= 0:
            await client.aclose()
            raise RuntimeLimitFault()
        request = client.build_request(
            "POST",
            settings.orcarouter_url,
            headers={
                "Authorization": f"Bearer {config.orca_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            content=body,
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            await client.aclose()
            if attempt < MAX_UPSTREAM_RETRIES:
                await asyncio.sleep(retry_delay(None, attempt))
                continue
            raise ProviderFault("MODEL_TIMEOUT", "A modell nem válaszolt időben.", 504) from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            if cancel.is_set():
                raise AgentFault("CANCELLED", "A feladat megszakadt.", 499) from exc
            if attempt < MAX_UPSTREAM_RETRIES:
                await asyncio.sleep(retry_delay(None, attempt))
                continue
            raise ProviderFault("MODEL_TRANSPORT", "A modellhez nem lehet csatlakozni.") from exc
        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type.lower():
                await response.aclose()
                await client.aclose()
                raise ProtocolFault("A modell nem SSE adatfolyamot adott vissza.")
            return ModelStream(response=response, client=client, touch=lambda: None)
        status = response.status_code
        retryable = status in (429, 502, 503, 504)
        try:
            await response.aread()
        finally:
            await response.aclose()
        if status in (401, 403):
            await client.aclose()
            raise ProviderFault("MODEL_AUTH", "Érvénytelen modell jogosultság.")
        if not retryable or attempt >= MAX_UPSTREAM_RETRIES:
            await client.aclose()
            if status == 429:
                raise ProviderFault("MODEL_RATE_LIMIT", f"A modell HTTP {status} hibát adott.")
            raise ProviderFault("MODEL_HTTP", f"A modell HTTP {status} hibát adott.")
        await asyncio.sleep(retry_delay(response, attempt))
    await client.aclose()
    raise ProviderFault("MODEL_RETRIES_EXHAUSTED", "Elfogyott a modell újrapróbálkozási kerete.")


async def receive_model(
    stream: ModelStream,
    task_id: str,
    step: int,
    send,
    cancel: asyncio.Event,
) -> ModelResponse:
    finish: str | None = None
    done = False
    content = ""
    content_bytes = 0
    usage: dict[str, Any] | None = None
    parts: dict[int, ToolCallBuilder] = {}
    try:
        async for data in parse_sse(stream.response.aiter_bytes(), cancel, stream.touch):
            if data == "[DONE]":
                done = True
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ProtocolFault(
                "Hibás JSON az SSE eseményben.", code="MODEL_PROTOCOL"
            ) from exc
            if not isinstance(event, dict):
                raise ProtocolFault("Érvénytelen SSE esemény.", code="MODEL_PROTOCOL")
            if event.get("error") is not None:
                raise ProviderFault("MODEL_ERROR", "A modell hibát jelzett.")
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices")
            if choices is None:
                continue
            if not isinstance(choices, list) or len(choices) == 0:
                continue
            if len(choices) != 1 or not isinstance(choices[0], dict):
                raise ProtocolFault("Érvénytelen választási rekord.", code="MODEL_PROTOCOL")
            choice = choices[0]
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str) or (finish is not None and finish != finish_reason):
                    raise ProtocolFault(
                        "Ütköző vagy érvénytelen befejezési ok.", code="MODEL_PROTOCOL"
                    )
                finish = finish_reason
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if delta.get("refusal") is not None:
                raise ProviderFault("MODEL_REFUSAL", "A modell visszautasította a kérést.", 422)
            reasoning = ""
            for key in ("reasoning_content", "reasoning", "thinking"):
                value = delta.get(key)
                if isinstance(value, str):
                    reasoning = value
                    break
            if reasoning:
                await send(
                    {
                        "type": "delta",
                        "task_id": task_id,
                        "step": step,
                        "reasoning_content": reasoning,
                    }
                )
            content_delta = delta.get("content")
            if content_delta is not None:
                if not isinstance(content_delta, str):
                    raise ProviderFault(
                        "UNSUPPORTED_MODEL_CONTENT",
                        "A modell nem támogatott tartalomformátumot küldött.",
                    )
                content += content_delta
                content_bytes += len(content_delta.encode("utf-8"))
                if content_bytes > MAX_OUTPUT_BYTES:
                    raise ProviderFault("MODEL_OUTPUT_LIMIT", "A modell kimenete túl hosszú.")
                if content_delta:
                    await send(
                        {
                            "type": "delta",
                            "task_id": task_id,
                            "step": step,
                            "content": content_delta,
                        }
                    )
            validate_tool_delta(choice, parts)
    finally:
        await stream.response.aclose()
        await stream.client.aclose()
    if not done or finish is None:
        raise ProtocolFault(
            "A modell adatfolyama nem fejeződött be szabályosan.", code="MODEL_INCOMPLETE"
        )
    if finish not in ("stop", "tool_calls"):
        if finish == "length":
            raise ProviderFault("MODEL_TRUNCATED", f"A modell befejezési állapota: {finish}", 422)
        if finish == "content_filter":
            raise ProviderFault("MODEL_FILTERED", f"A modell befejezési állapota: {finish}", 422)
        raise ProviderFault(
            "MODEL_FINISH_UNSUPPORTED", f"A modell befejezési állapota: {finish}", 422
        )
    calls = [finalize_tool_call(parts[key]) for key in sorted(parts)]
    if any(not call.id or not call.function_name for call in calls):
        raise ProtocolFault("Hiányos eszközhívási adatok.", code="MODEL_PROTOCOL")
    if len({call.id for call in calls}) != len(calls):
        raise ProtocolFault("Ismételt eszközazonosító.", code="MODEL_PROTOCOL")
    if (finish == "tool_calls" and not calls) or (finish == "stop" and calls):
        raise ProtocolFault(
            "A lezárás és az eszközhívások ellentmondanak.", code="MODEL_PROTOCOL"
        )
    return ModelResponse(content=content, calls=calls, finish=finish, usage=usage)
