from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import Any

import httpx

from app.config import get_settings

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
FAST_MODEL = "gemini-3.8-flash"
ADVISOR_MODEL = "gemini-3.1-pro-preview"
CONSENSUS_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
)
TOOLS = [
    {"type": "code_execution"},
    {"type": "google_search"},
    {"type": "url_context"},
]
MAX_RESPONSE_CHARS = 500
MAX_TIMEOUT_RETRIES = 1
MAX_NETWORK_RETRIES = 2
MAX_STATUS_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30.0
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=30.0)
CLIENT_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=60.0)

_client: httpx.AsyncClient | None = None
_client_lock: asyncio.Lock | None = None


def get_client_lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def get_client() -> httpx.AsyncClient:
    global _client
    client = _client
    if client is not None and not client.is_closed:
        return client
    async with get_client_lock():
        client = _client
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=CLIENT_LIMITS, http2=False)
            _client = client
        return client


async def close_client() -> None:
    global _client
    async with get_client_lock():
        client = _client
        _client = None
    if client is not None and not client.is_closed:
        await client.aclose()


def truncate(message: str) -> str:
    text = message.strip()
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    return text[: MAX_RESPONSE_CHARS - 1].rstrip() + "\u2026"


def collect_text(value: Any, out: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            collect_text(item, out)
        return
    if not isinstance(value, dict):
        return
    if value.get("type") == "text" and isinstance(value.get("text"), str):
        out.append(value["text"])
    for key in ("outputs", "steps", "content", "parts"):
        if key in value:
            collect_text(value[key], out)


def extract_error_message(text: str) -> str:
    message = text
    try:
        parsed_error = json.loads(text)
    except json.JSONDecodeError:
        return truncate(message)
    if isinstance(parsed_error, dict):
        error = parsed_error.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(parsed_error.get("message"), str):
            message = parsed_error["message"]
    return truncate(message)


async def sleep_unless_cancelled(delay: float, cancel: asyncio.Event) -> None:
    if delay <= 0:
        if cancel.is_set():
            raise asyncio.CancelledError()
        return
    try:
        await asyncio.wait_for(cancel.wait(), timeout=delay)
    except (asyncio.TimeoutError, TimeoutError):
        return
    raise asyncio.CancelledError()


async def post_cancellable(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    body: dict[str, Any],
    cancel: asyncio.Event,
) -> httpx.Response:
    if cancel.is_set():
        raise asyncio.CancelledError()
    request_task: asyncio.Task[httpx.Response] = asyncio.ensure_future(
        client.post(INTERACTIONS_URL, headers=headers, json=body)
    )
    cancel_task: asyncio.Task[bool] = asyncio.ensure_future(cancel.wait())
    try:
        done, _pending = await asyncio.wait(
            {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        request_task.cancel()
        cancel_task.cancel()
        with contextlib.suppress(BaseException):
            await request_task
        with contextlib.suppress(BaseException):
            await cancel_task
        raise
    cancel_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cancel_task
    if request_task in done:
        return request_task.result()
    request_task.cancel()
    with contextlib.suppress(BaseException):
        await request_task
    raise asyncio.CancelledError()


def backoff_delay(attempt: int) -> float:
    return min(MAX_BACKOFF_SECONDS, 2.0 * (2**attempt) + random.random())


def retry_after_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw is not None:
        value = raw.strip()
        if value.isdigit():
            return min(MAX_RETRY_AFTER_SECONDS, float(value))
    return backoff_delay(attempt)


async def interact(model: str, prompt: str, cancel: asyncio.Event) -> dict[str, str]:
    settings = get_settings()
    key = settings.gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    headers = {
        "x-goog-api-key": key,
        "Content-Type": "application/json",
        "Api-Revision": API_REVISION,
    }
    body = {
        "model": model,
        "input": prompt,
        "tools": TOOLS,
        "store": False,
        "generation_config": {"max_output_tokens": 65536, "thinking_level": "high"},
    }
    client = await get_client()
    timeout_attempts = 0
    network_attempts = 0
    status_attempts = 0
    while True:
        if cancel.is_set():
            raise asyncio.CancelledError()
        try:
            response = await post_cancellable(client, headers, body, cancel)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as exc:
            if timeout_attempts < MAX_TIMEOUT_RETRIES:
                timeout_attempts += 1
                await sleep_unless_cancelled(backoff_delay(timeout_attempts), cancel)
                continue
            raise RuntimeError(f"{model}: timeout.") from exc
        except httpx.HTTPError as exc:
            if network_attempts < MAX_NETWORK_RETRIES:
                network_attempts += 1
                await sleep_unless_cancelled(backoff_delay(network_attempts), cancel)
                continue
            raise RuntimeError(f"{model}: network error: {truncate(str(exc))}") from exc
        text = response.text
        if response.status_code == 200:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{model}: invalid response.") from exc
            parts: list[str] = []
            collect_text(parsed, parts)
            content = "\n".join(parts).strip()
            if not content:
                raise RuntimeError(f"{model}: empty response.")
            return {"model": model, "text": content}
        message = extract_error_message(text)
        if response.status_code in RETRYABLE_STATUS_CODES and status_attempts < MAX_STATUS_RETRIES:
            status_attempts += 1
            await sleep_unless_cancelled(retry_after_delay(response, status_attempts), cancel)
            continue
        raise RuntimeError(f"{model} ({response.status_code}): {message}")


def error_result(error: BaseException) -> dict[str, Any]:
    message = truncate(str(error))
    return {
        "status": "error",
        "result": {
            "code": "SUBAGENT_FAILED",
            "message": message or "The subagent stopped with an error.",
        },
    }


async def delegate_task(task: str, context: str, cancel: asyncio.Event) -> dict[str, Any]:
    try:
        prompt = (
            "You are an autonomous subagent. Execute the subtask precisely; you may use code execution, "
            "Google search and URL reading. Provide a concise, verified result in Hungarian, with "
            f"sources if you searched.\n\nContext:\n{context or 'none'}\n\nSubtask:\n{task}"
        )
        answer = await interact(FAST_MODEL, prompt, cancel)
        return {
            "status": "completed",
            "result": {"model": answer["model"], "answer": answer["text"]},
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return error_result(exc)


async def consult_advisor(question: str, context: str, cancel: asyncio.Event) -> dict[str, Any]:
    try:
        prompt = (
            "You are an expert advisor assisting an autonomous agent. Analyze the situation, highlight "
            "errors and risks, and give concrete, ordered recommendations for how to proceed in "
            f"Hungarian.\n\nSituation:\n{context or 'none'}\n\nQuestion:\n{question}"
        )
        answer = await interact(ADVISOR_MODEL, prompt, cancel)
        return {
            "status": "completed",
            "result": {"model": answer["model"], "advice": answer["text"]},
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return error_result(exc)


async def multi_model_consensus(
    question: str, context: str, cancel: asyncio.Event
) -> dict[str, Any]:
    prompt = (
        "Answer the question independently and with justification in Hungarian; verify with search "
        f"or code where possible.\n\nContext:\n{context or 'none'}\n\nQuestion:\n{question}"
    )
    settled = await asyncio.gather(
        *(interact(model, prompt, cancel) for model in CONSENSUS_MODELS),
        return_exceptions=True,
    )
    answers: list[dict[str, str]] = []
    failures: list[str] = []
    for entry in settled:
        if isinstance(entry, BaseException):
            if isinstance(entry, Exception):
                failures.append(truncate(str(entry)))
                continue
            raise entry
        answers.append(entry)
    if not answers:
        return {
            "status": "error",
            "result": {
                "code": "SUBAGENT_FAILED",
                "message": "None of the models responded.",
                "failures": failures,
            },
        }
    synthesis_input = (
        "You have received independent answers from multiple models to the same question. Compare "
        "them: where they agree, where they differ, and which argument is stronger. Provide a final, "
        f"well-founded consensus answer and a confidence level in Hungarian.\n\nQuestion:\n{question}\n\n"
        + "\n\n".join(
            f"### {index + 1}. answer ({answer['model']})\n{answer['text']}"
            for index, answer in enumerate(answers)
        )
    )
    try:
        synthesis = await interact(ADVISOR_MODEL, synthesis_input, cancel)
        return {
            "status": "completed",
            "result": {
                "consensus": synthesis["text"],
                "answers": [
                    {"model": answer["model"], "answer": answer["text"]} for answer in answers
                ],
                "failures": failures,
            },
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {
            "status": "completed",
            "result": {
                "consensus": None,
                "synthesis_error": truncate(str(exc)),
                "answers": [
                    {"model": answer["model"], "answer": answer["text"]} for answer in answers
                ],
                "failures": failures,
            },
        }
