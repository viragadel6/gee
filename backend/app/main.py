from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import close_databases
from app.middleware.error_handler import register_error_handlers
from app.middleware.security import RequestContextMiddleware
from app.routers import browser as browser_router
from app.routers import chat as chat_router
from app.routers import chat_jobs as chat_jobs_router
from app.routers import conversations as conversations_router
from app.routers import images as images_router
from app.routers import operator as operator_router
from app.routers import public_worker as public_worker_router

logger = logging.getLogger("app")


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def resolve_static_directory() -> Path | None:
    configured = get_settings().static_dir
    candidates = [
        Path(configured),
        Path(__file__).resolve().parent.parent.parent / "public",
        Path(__file__).resolve().parent.parent / "public",
        Path.cwd() / "public",
        Path.cwd() / ".." / "public",
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and (resolved / "chat.html").is_file():
            return resolved
    return None


@asynccontextmanager
async def lifespan(_application: FastAPI):
    logger.info("application_started")
    yield
    await close_databases()
    logger.info("application_stopped")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="Chat Pal Backend",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    origins = [
        origin.strip()
        for origin in (settings.app_origins or settings.app_origin or "").split(",")
        if origin.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    register_error_handlers(app)
    app.include_router(chat_router.router)
    app.include_router(chat_jobs_router.router)
    app.include_router(conversations_router.router)
    app.include_router(operator_router.router)
    app.include_router(browser_router.router)
    app.include_router(images_router.router)
    app.include_router(public_worker_router.router)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "service": "chat-pal-backend"},
            headers={"Cache-Control": "no-store"},
        )

    static_directory = resolve_static_directory()
    if static_directory is not None:

        @app.get("/", include_in_schema=False)
        async def index() -> Response:
            return RedirectResponse(url="/chat.html")

        app.mount(
            "/",
            StaticFiles(directory=str(static_directory), html=True),
            name="static",
        )

    return app


app = create_app()
