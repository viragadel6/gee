from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.config import get_settings
from app.database import SupabaseDatabase
from app.utils.clock import as_str

SESSION_COOKIE = "chat_session"
SESSION_TOKEN_VERSION = "v1"
SESSION_MAX_AGE_SECONDS = 31_536_000
SESSION_CLOCK_SKEW_SECONDS = 300
SESSION_ID_LENGTH = 32
SESSION_HEX_DIGITS = frozenset("0123456789abcdef")
ADMIN_TOKEN_HEADER = "x-admin-token"
WORKER_TOKEN_HEADER = "x-worker-token"

logger = logging.getLogger("app.access")

_ephemeral_secret: str | None = None


def _session_secret() -> str:
    global _ephemeral_secret
    configured = str(get_settings().session_secret or "").strip()
    if configured:
        return configured
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_urlsafe(48)
        logger.warning(
            "session_secret_missing",
            extra={"detail": "Az ephemeral munkamenet-kulcs elvész az újraindításkor."},
        )
    return _ephemeral_secret


def _signature(payload: str) -> str:
    key = _session_secret().encode("utf-8")
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_session(client_id: str) -> str:
    issued_at = int(time.time())
    payload = f"{SESSION_TOKEN_VERSION}.{client_id}.{issued_at}"
    return f"{payload}.{_signature(payload)}"


def _decode_session(token: Any) -> str | None:
    if not isinstance(token, str):
        return None
    if len(token) > 256:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    version, client_id, issued_raw, signature = parts
    if version != SESSION_TOKEN_VERSION:
        return None
    if len(client_id) != SESSION_ID_LENGTH:
        return None
    if any(character not in SESSION_HEX_DIGITS for character in client_id):
        return None
    if not issued_raw.isascii() or not issued_raw.isdigit() or len(issued_raw) > 12:
        return None
    payload = f"{version}.{client_id}.{issued_raw}"
    try:
        provided_signature = signature.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if not hmac.compare_digest(provided_signature, _signature(payload).encode("utf-8")):
        return None
    issued_at = int(issued_raw)
    current = int(time.time())
    if issued_at <= 0:
        return None
    if issued_at > current + SESSION_CLOCK_SKEW_SECONDS:
        return None
    if current - issued_at > SESSION_MAX_AGE_SECONDS:
        return None
    return client_id


def _is_secure_request(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded in ("https", "http"):
        return forwarded == "https"
    return request.url.scheme == "https"


def _cookie_header(client_id: str, secure: bool) -> str:
    attributes = [
        f"{SESSION_COOKIE}={_encode_session(client_id)}",
        "Path=/",
        f"Max-Age={SESSION_MAX_AGE_SECONDS}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


@dataclass(frozen=True)
class Access:
    client_id: str
    set_cookie: str | None

    def headers(self) -> dict[str, str]:
        if not self.set_cookie:
            return {}
        return {"Set-Cookie": self.set_cookie}


def load_access(request: Request) -> Access:
    token = request.cookies.get(SESSION_COOKIE)
    existing = _decode_session(token)
    if existing is not None:
        return Access(client_id=existing, set_cookie=None)
    client_id = uuid.uuid4().hex
    cookie = _cookie_header(client_id, _is_secure_request(request))
    return Access(client_id=client_id, set_cookie=cookie)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    token = value.strip()
    if not token or len(token) > 4096:
        return None
    return token


def _token_matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


def _token_error(
    request: Request, header_name: str, expected: str, missing_message: str
) -> tuple[str, int] | None:
    if not expected:
        logger.error("access_token_not_configured", extra={"header": header_name})
        return (missing_message, 503)
    provided = request.headers.get(header_name) or _bearer_token(request)
    if not provided:
        return ("Bejelentkezés szükséges.", 401)
    if not _token_matches(provided, expected):
        return ("Tiltott művelet.", 403)
    return None


def admin_error(request: Request) -> tuple[str, int] | None:
    expected = str(get_settings().admin_token or "").strip()
    return _token_error(
        request,
        ADMIN_TOKEN_HEADER,
        expected,
        "A rendszerfelügyeleti végpont nincs konfigurálva.",
    )


def worker_error(request: Request) -> tuple[str, int] | None:
    expected = str(get_settings().worker_token or "").strip()
    return _token_error(
        request,
        WORKER_TOKEN_HEADER,
        expected,
        "A háttérmunkás végpont nincs konfigurálva.",
    )


@dataclass(frozen=True)
class ConversationAccess:
    exists: bool
    owner: str | None


@dataclass(frozen=True)
class JobAccess:
    exists: bool
    conversation_id: str | None
    owner: str | None
    status: str | None

    def owned_by(self, client_id: str) -> bool:
        return bool(self.owner) and self.owner == client_id


async def conversation_access(
    database: SupabaseDatabase, conversation_id: str
) -> ConversationAccess:
    response = await (
        database.table("chat_conversations")
        .select("id, client_id")
        .eq("id", conversation_id)
        .limit(1)
        .execute()
    )
    row = response.maybe_single()
    if row is None:
        return ConversationAccess(exists=False, owner=None)
    owner = as_str(row.get("client_id")) or None
    return ConversationAccess(exists=True, owner=owner)


async def job_access(database: SupabaseDatabase, job_id: str) -> JobAccess:
    response = await (
        database.table("chat_jobs")
        .select("id, conversation_id, status")
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    row = response.maybe_single()
    if row is None:
        return JobAccess(exists=False, conversation_id=None, owner=None, status=None)
    conversation_id = as_str(row.get("conversation_id")) or None
    owner: str | None = None
    if conversation_id is not None:
        access = await conversation_access(database, conversation_id)
        owner = access.owner if access.exists else None
    status = as_str(row.get("status")) or None
    return JobAccess(
        exists=True,
        conversation_id=conversation_id,
        owner=owner,
        status=status,
    )
