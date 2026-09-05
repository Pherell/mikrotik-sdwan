"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.v1 import auth as auth_api
from app.api.v1 import fabrics as fabrics_api
from app.api.v1 import jobs as jobs_api
from app.api.v1 import ops as ops_api
from app.api.v1 import policies as policies_api
from app.api.v1 import sites as sites_api
from app.config import get_settings
from app.db import SessionLocal, engine
from app.drivers.base import DeviceAuthError, DeviceUnreachable, DriverError
from app.models.enums import Role
from app.models.user import User
from app.security import hash_password

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def _bootstrap_admin() -> None:
    """Seed the first admin so a fresh `docker compose up` is usable."""
    settings = get_settings()
    async with SessionLocal() as session:
        if await session.scalar(select(User).limit(1)):
            return
        session.add(
            User(
                email=settings.bootstrap_admin_email,
                full_name="Bootstrap admin",
                role=Role.admin,
                password_hash=hash_password(settings.bootstrap_admin_password),
            )
        )
        await session.commit()
        log.warning(
            "Seeded bootstrap admin %s. Change this password before exposing the "
            "controller.",
            settings.bootstrap_admin_email,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _bootstrap_admin()
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="MikroTik SD-WAN Controller",
        version="0.1.0",
        summary="Intent-based SD-WAN orchestration for RouterOS",
        lifespan=lifespan,
        debug=settings.debug,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Device failures are expected operational states, not server bugs. Map them
    # to statuses the UI can act on instead of a bare 500.
    @app.exception_handler(DeviceUnreachable)
    async def _unreachable(_: Request, exc: DeviceUnreachable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=504)

    @app.exception_handler(DeviceAuthError)
    async def _device_auth(_: Request, exc: DeviceAuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(DriverError)
    async def _driver(_: Request, exc: DriverError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    app.include_router(auth_api.router, prefix="/api/v1")
    app.include_router(auth_api.users, prefix="/api/v1")
    app.include_router(sites_api.router, prefix="/api/v1")
    app.include_router(jobs_api.router, prefix="/api/v1")
    app.include_router(fabrics_api.router, prefix="/api/v1")
    app.include_router(policies_api.router, prefix="/api/v1")
    app.include_router(ops_api.router, prefix="/api/v1")
    return app


app = create_app()
