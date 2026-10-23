from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import database as database_module
from app.database import DatabaseError
from app.errors import AgentFault, RateLimitFault, RequestFault
from app.services.chat_jobs import job_queue_summary
from app.services.checkpoints import checkpoint_stats
from app.services.supervisor import RECOVERY_TABLE, heal_now, supervisor_status
from app.utils.clock import as_int, as_str
from app.utils.rate_limit import client_key, rate_limiter
from app.utils.security import is_loose_uuid, verify_origin

router = APIRouter()

logger = logging.getLogger("app.system")

NO_STORE = {"Cache-Control": "no-store"}
ACTIVE_STATUSES = ("queued", "running")
EXTENDED_JOB_COLUMNS = (
    "id, status, created_at, updated_at, started_at, lease_expires_at, stop_requested, "
    "attempts, next_attempt_at, last_error, progress_at"
)
LEGACY_JOB_COLUMNS = (
    "id, status, created_at, updated_at, started_at, lease_expires_at, stop_requested"
)
RECOVERY_COLUMNS = "id, job_id, kind, owner_id, detail, created_at"
MAX_JOB_ROWS = 50
MAX_RECOVERY_ROWS = 50
DEFAULT_JOB_ROWS = 25
DEFAULT_RECOVERY_ROWS = 20
DETAIL_CHARS = 400


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload, headers=NO_STORE)


def _failure(
    message: str, status_code: int = 400, code: str = "SYSTEM_REQUEST_FAILED"
) -> JSONResponse:
    return _json({"error": {"code": code, "message": message}}, status_code)


def _rate_limit_failure(exc: RateLimitFault) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers={**NO_STORE, "Retry-After": str(exc.retry_after)},
    )


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": as_str(row.get("id")),
        "status": as_str(row.get("status")),
        "created_at": as_str(row.get("created_at")),
        "updated_at": as_str(row.get("updated_at")),
        "started_at": as_str(row.get("started_at")),
        "lease_expires_at": as_str(row.get("lease_expires_at")),
        "progress_at": as_str(row.get("progress_at")),
        "next_attempt_at": as_str(row.get("next_attempt_at")),
        "attempts": as_int(row.get("attempts")),
        "last_error": as_str(row.get("last_error"), DETAIL_CHARS),
        "stop_requested": bool(row.get("stop_requested")),
    }


def _recovery_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": as_int(row.get("id")),
        "job_id": as_str(row.get("job_id")),
        "kind": as_str(row.get("kind")),
        "owner_id": as_str(row.get("owner_id")),
        "detail": row.get("detail"),
        "created_at": as_str(row.get("created_at")),
    }


async def _recoverable_jobs(limit: int, job_id: str | None) -> list[dict[str, Any]]:
    database = database_module.get_admin_database()
    for columns in (EXTENDED_JOB_COLUMNS, LEGACY_JOB_COLUMNS):
        try:
            query = database.table("chat_jobs").select(columns).in_(
                "status", list(ACTIVE_STATUSES)
            )
            if job_id:
                query = query.eq("id", job_id)
            response = await (
                query.order("updated_at", desc=False, nullsfirst=True).limit(limit).execute()
            )
        except DatabaseError as exc:
            if exc.status_code in (400, 404) and columns == EXTENDED_JOB_COLUMNS:
                continue
            logger.warning("system_jobs_failed", extra={"error": str(exc)})
            return []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("system_jobs_failed", extra={"error": str(exc)})
            return []
        return [_row_summary(row) for row in response.rows]
    return []


async def _recovery_rows(limit: int) -> list[dict[str, Any]]:
    database = database_module.get_admin_database()
    try:
        response = await (
            database.table(RECOVERY_TABLE)
            .select(RECOVERY_COLUMNS)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
    except DatabaseError as exc:
        if exc.status_code in (400, 404):
            return []
        logger.warning("system_recovery_failed", extra={"error": str(exc)})
        return []
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("system_recovery_failed", extra={"error": str(exc)})
        return []
    return [_recovery_summary(row) for row in response.rows]


def _positive_limit(raw: str | None, fallback: int, ceiling: int) -> int:
    value = as_int(raw if raw is not None else "", fallback, 1, ceiling)
    return value


@router.get("/api/system/status")
async def get_system_status(request: Request) -> Any:
    try:
        rate_limiter.check("system", client_key(request))
        status = supervisor_status()
        try:
            status["queue"] = await job_queue_summary()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("system_queue_failed", extra={"error": str(exc)})
            status["queue"] = {"error": str(exc)[:DETAIL_CHARS]}
        status["checkpoints"] = checkpoint_stats()
        status["recovery_log"] = await _recovery_rows(DEFAULT_RECOVERY_ROWS)
        return _json(status)
    except RateLimitFault as exc:
        return _rate_limit_failure(exc)
    except AgentFault as exc:
        return _failure(exc.message, 400 if exc.status_code == 499 else exc.status_code, exc.code)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("system_status_failed")
        return _failure("Belső szerverhiba.", 500, "INTERNAL_ERROR")


@router.get("/api/system/jobs")
async def get_system_jobs(request: Request) -> Any:
    try:
        rate_limiter.check("system", client_key(request))
        limit = _positive_limit(
            request.query_params.get("limit"), DEFAULT_JOB_ROWS, MAX_JOB_ROWS
        )
        recovery_limit = _positive_limit(
            request.query_params.get("recovery_limit"),
            DEFAULT_RECOVERY_ROWS,
            MAX_RECOVERY_ROWS,
        )
        raw_job_id = request.query_params.get("job_id") or ""
        job_id = raw_job_id.strip()
        if job_id and not is_loose_uuid(job_id):
            raise RequestFault("Érvénytelen feladat azonosító.", code="INVALID_JOB_ID")
        jobs = await _recoverable_jobs(limit, job_id)
        recovery = await _recovery_rows(recovery_limit)
        return _json({"count": len(jobs), "jobs": jobs, "recovery_log": recovery})
    except RateLimitFault as exc:
        return _rate_limit_failure(exc)
    except AgentFault as exc:
        return _failure(exc.message, 400 if exc.status_code == 499 else exc.status_code, exc.code)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("system_jobs_failed")
        return _failure("Belső szerverhiba.", 500, "INTERNAL_ERROR")


@router.post("/api/system/heal")
async def post_system_heal(request: Request) -> Any:
    try:
        origin_error = verify_origin(request)
        if origin_error is not None:
            return _failure(origin_error, 403, "INVALID_ORIGIN")
        rate_limiter.check("heal", client_key(request))
        result = await heal_now()
        return _json({"healed": True, "result": result})
    except RateLimitFault as exc:
        return _rate_limit_failure(exc)
    except AgentFault as exc:
        return _failure(exc.message, 400 if exc.status_code == 499 else exc.status_code, exc.code)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("system_heal_failed")
        return _failure("Belső szerverhiba.", 500, "INTERNAL_ERROR")
