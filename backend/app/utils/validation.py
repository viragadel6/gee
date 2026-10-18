from __future__ import annotations

import json
from typing import Any

from app.errors import RequestFault

MAX_TEXT_LENGTH = 400_000
MAX_HISTORY_MESSAGES = 300
ALLOWED_CLIENT_ROLES = ("user", "assistant")


def parse_json_body(raw: bytes) -> Any:
    if not raw:
        raise RequestFault("Hiányzó kéréstartalom.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestFault("Hibás UTF-8 kérés.") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RequestFault("Hibás JSON kérés.") from exc


def validate_client_history(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RequestFault("Hiányzó vagy hibás üzenetlista.")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_HISTORY_MESSAGES:
        raise RequestFault("Hiányzó vagy hibás üzenetlista.")
    validated: list[dict[str, Any]] = []
    last_role: str | None = None
    for raw in messages:
        if not isinstance(raw, dict):
            raise RequestFault("Az üzenet szerepköre nem megengedett.")
        role = raw.get("role")
        if not isinstance(role, str) or role not in ALLOWED_CLIENT_ROLES:
            raise RequestFault("Az üzenet szerepköre nem megengedett.")
        for key in raw:
            if key not in ("role", "content"):
                raise RequestFault("Kliens oldali utasítás- vagy eszköztörténet nem fogadható el.")
        content = raw.get("content")
        if not isinstance(content, str) or len(content) > MAX_TEXT_LENGTH or not content.strip():
            raise RequestFault("Az üzenet tartalma hibás vagy túl nagy.")
        validated.append({"role": role, "content": content})
        last_role = role
    if last_role != "user":
        raise RequestFault("A legutolsó üzenetnek felhasználói feladatnak kell lennie.")
    return validated


def parse_conversation_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    from app.utils.security import LOOSE_UUID_PATTERN

    return value if LOOSE_UUID_PATTERN.match(value) else None


def is_record(value: Any) -> bool:
    return isinstance(value, dict)
