from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from app.config import get_settings

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
FAST_MODEL = "models/gemini-3.8-flash"
ADVISOR_MODEL = "models/gemini-3.1-pro-preview"
CONSENSUS_MODELS = (
    "models/gemini-3.8-flash",
    "models/gemini-3.5-flash",
    "models/gemini-3.1-pro-preview",
)
TOOLS = [
    {"type": "code_execution"},
    {"type": "google_search"},
    {"type": "url_context"},
]
MAX_RESPONSE_CHARS = 500


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


async def interact(model: str, prompt: str, cancel: asyncio.Event) -> dict[str, str]:
    settings = get_settings()
    key = settings.gemini_api_key
    if not key:
        raise RuntimeError("A GEMINI_API_KEY nincs beállítva.")
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    body = {
        "model": model,
        "input": prompt,
        "tools": TOOLS,
        "generation_config": {"max_output_tokens": 65536, "thinking_level": "high"},
    }
    attempt = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        while True:
            if cancel.is_set():
                raise asyncio.CancelledError()
            try:
                response = await client.post(INTERACTIONS_URL, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                if attempt < 2:
                    attempt += 1
                    await asyncio.sleep(1.5 * attempt)
                    continue
                raise RuntimeError(f"{model}: időtúllépés.") from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    attempt += 1
                    await asyncio.sleep(1.5 * attempt)
                    continue
                raise RuntimeError(f"{model}: hálózati hiba.") from exc
            text = response.text
            if response.status_code == 200:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{model}: érvénytelen válasz.") from exc
                parts: list[str] = []
                collect_text(parsed, parts)
                content = "\n".join(parts).strip()
                if not content:
                    raise RuntimeError(f"{model}: üres válasz.")
                return {"model": model, "text": content}
            message = text[:MAX_RESPONSE_CHARS]
            try:
                parsed_error = json.loads(text)
                if isinstance(parsed_error, dict):
                    error = parsed_error.get("error")
                    if isinstance(error, dict) and isinstance(error.get("message"), str):
                        message = error["message"]
            except json.JSONDecodeError:
                pass
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < 2 and "quota" not in message.lower():
                attempt += 1
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.strip().isdigit():
                    wait = float(retry_after.strip())
                else:
                    wait = 2.0 * (2**attempt) + random.random()
                await asyncio.sleep(wait)
                continue
            raise RuntimeError(f"{model} ({response.status_code}): {message}")


def error_result(error: BaseException) -> dict[str, Any]:
    message = error.message if isinstance(error, RuntimeError) else str(error)
    return {
        "status": "error",
        "result": {"code": "SUBAGENT_FAILED", "message": message or "Az alügynök hibával leállt."},
    }


async def delegate_task(task: str, context: str, cancel: asyncio.Event) -> dict[str, Any]:
    try:
        prompt = (
            "Önálló alügynök vagy. Hajtsd végre pontosan a részfeladatot; kódfuttatást, Google "
            "keresést és URL-olvasást használhatsz. Adj tömör, ellenőrzött eredményt magyarul, "
            f"forrásokkal ha kerestél.\n\nKontextus:\n{context or 'nincs'}\n\nRészfeladat:\n{task}"
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
            "Szakértő tanácsadó vagy egy autonóm ügynök mellett. Elemezd a helyzetet, mutass rá "
            "hibákra és kockázatokra, és adj konkrét, sorrendbe állított javaslatot a továbblépésre "
            f"magyarul.\n\nHelyzet:\n{context or 'nincs'}\n\nKérdés:\n{question}"
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
        "Válaszolj önállóan és indokolva magyarul a kérdésre; ellenőrizd keresővel vagy kóddal, "
        f"ahol lehet.\n\nKontextus:\n{context or 'nincs'}\n\nKérdés:\n{question}"
    )
    settled = await asyncio.gather(
        *(interact(model, prompt, cancel) for model in CONSENSUS_MODELS),
        return_exceptions=True,
    )
    answers: list[dict[str, str]] = []
    failures: list[str] = []
    for entry in settled:
        if isinstance(entry, asyncio.CancelledError):
            raise entry
        if isinstance(entry, BaseException):
            failures.append(str(entry))
        else:
            answers.append(entry)
    if not answers:
        return {
            "status": "error",
            "result": {
                "code": "SUBAGENT_FAILED",
                "message": "Egyik modell sem válaszolt.",
                "failures": failures,
            },
        }
    synthesis_input = (
        "Több modell független válaszát kaptad ugyanarra a kérdésre. Hasonlítsd össze őket: hol "
        "értenek egyet, hol térnek el, melyik érvelés erősebb. Adj végső, megalapozott "
        f"konszenzusválaszt és bizonyossági szintet magyarul.\n\nKérdés:\n{question}\n\n"
        + "\n\n".join(
            f"### {index + 1}. válasz ({answer['model']})\n{answer['text']}"
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
                "synthesis_error": str(exc),
                "answers": [
                    {"model": answer["model"], "answer": answer["text"]} for answer in answers
                ],
                "failures": failures,
            },
        }
