from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

INT_TEXT_PATTERN = re.compile(r"[+-]?[0-9]+")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    try:
        offset = value.utcoffset()
    except (TypeError, ValueError, OverflowError):
        offset = None
    if offset is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def iso(value: datetime | None = None) -> str:
    moment = value if value is not None else now()
    if not isinstance(moment, datetime):
        raise TypeError("Az iso() csak datetime értéket fogad el.")
    return _as_aware(moment).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError, OverflowError):
        return None
    return _as_aware(parsed)


def age_ms(value: Any, reference: datetime | None = None) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    if reference is None:
        base = now()
    elif isinstance(reference, datetime):
        base = _as_aware(reference)
    else:
        raise TypeError("Az age_ms() referencia értéke csak datetime lehet.")
    return (base - parsed).total_seconds() * 1000


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def as_str(value: Any, limit: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        return ""
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def as_int(
    value: Any,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    number = default
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if math.isfinite(value):
            number = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if INT_TEXT_PATTERN.fullmatch(stripped):
            try:
                number = int(stripped)
            except (ValueError, TypeError):
                number = default
    if maximum is not None and number > maximum:
        number = maximum
    if minimum is not None and number < minimum:
        number = minimum
    return number


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int):
        try:
            converted = float(value)
        except (OverflowError, ValueError):
            return default
        return converted if math.isfinite(converted) else default
    if isinstance(value, float):
        return value if math.isfinite(value) else default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            converted = float(stripped)
        except (ValueError, OverflowError):
            return default
        return converted if math.isfinite(converted) else default
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return default


def as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = key if isinstance(key, str) else str(key)
        result[normalized] = item
    return result


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def utf8_bytes(value: Any) -> int:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return 0
    try:
        return len(encoded.encode("utf-8"))
    except UnicodeEncodeError:
        try:
            return len(encoded.encode("utf-8", "backslashreplace"))
        except (UnicodeEncodeError, LookupError):
            return 0
