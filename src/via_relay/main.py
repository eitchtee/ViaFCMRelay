"""HTTP API of the relay.

``POST /v1/wake`` ``{"token": "<FCM token>", "id": "<push id>"?}`` makes FCM deliver the fixed,
data-only message ``{"t": "wake", "id": …}`` to that token. The relay never forwards free-form
content, never shows a notification, and stores nothing but in-memory rate-limit counters.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from via_relay import __version__
from via_relay.config import Settings
from via_relay.fcm import FcmClient, FcmError, FcmUnavailable, ServiceAccount, TokenGone
from via_relay.limits import Coalescer, DailyLimit, Limited, RateLimiter

log = logging.getLogger("via_relay")

ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"


class WakeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=4096, pattern=r"^[A-Za-z0-9_:\-]+$")
    id: str | None = Field(None, pattern=ULID_PATTERN, description="Via push id (a ULID)")


class WakeOut(BaseModel):
    status: Literal["sent", "coalesced"]


@dataclass
class Limits:
    ip_rate: RateLimiter
    ip_daily: DailyLimit
    key_rate: RateLimiter
    key_daily: DailyLimit
    coalescer: Coalescer
    instance_keys: set[str]


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(settings: Settings | None = None, fcm: FcmClient | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = fcm
        http = None
        if client is None:
            account = ServiceAccount.load(settings.credentials_file, settings.credentials_json)
            http = httpx.AsyncClient(timeout=10, headers={"User-Agent": f"via-relay/{__version__}"})
            client = FcmClient(
                account,
                http,
                android_ttl=settings.android_ttl,
                collapse_key=settings.collapse_key,
                dry_run=settings.dry_run,
            )
        app.state.fcm = client
        app.state.limits = Limits(
            ip_rate=RateLimiter(settings.ip_rate_per_minute),
            ip_daily=DailyLimit(settings.ip_daily_limit),
            key_rate=RateLimiter(settings.key_rate_per_minute),
            key_daily=DailyLimit(settings.key_daily_limit),
            coalescer=Coalescer(settings.token_interval),
            instance_keys=settings.load_instance_keys(),
        )
        log.info(
            "Via FCM relay %s ready (project %s%s)",
            __version__,
            client.account.project_id,
            ", dry run" if client.dry_run else "",
        )
        try:
            yield
        finally:
            if http is not None:
                await http.aclose()

    app = FastAPI(title="Via FCM relay", version=__version__, lifespan=lifespan, redoc_url=None)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, "error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "validation_error", "Invalid request")

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/wake",
        response_model=WakeOut,
        responses={
            410: {"description": "The FCM token is gone; stop using it"},
            429: {"description": "Rate limited; see Retry-After"},
            503: {"description": "FCM unavailable; see Retry-After"},
        },
    )
    async def wake(
        body: WakeIn,
        request: Request,
        instance_key: Annotated[str | None, Header(alias="X-Via-Instance-Key")] = None,
    ) -> WakeOut | JSONResponse:
        """Wake the device holding this FCM token."""
        limits: Limits = request.app.state.limits
        client: FcmClient = request.app.state.fcm

        try:
            if instance_key is not None:
                if instance_key not in limits.instance_keys:
                    return _error(401, "invalid_instance_key", "Unknown X-Via-Instance-Key")
                key = f"key:{_fingerprint(instance_key)}"
                limits.key_rate.check(key)
                limits.key_daily.check(key)
            else:
                ip = request.client.host if request.client else "unknown"
                limits.ip_rate.check(ip)
                limits.ip_daily.check(ip)
        except Limited as e:
            return _error(429, "rate_limited", str(e), {"Retry-After": str(e.retry_after)})

        # Keyed by a hash so raw tokens are never kept around.
        if not limits.coalescer.allow(_fingerprint(body.token)):
            return WakeOut(status="coalesced")

        try:
            await client.send_wake(body.token, body.id)
        except TokenGone as e:
            return _error(410, "token_gone", f"FCM token is no longer valid ({e.code})")
        except FcmUnavailable as e:
            log.warning("FCM unavailable: %s", e)
            return _error(
                503, "fcm_unavailable", "FCM is unavailable", {"Retry-After": str(e.retry_after)}
            )
        except FcmError as e:
            log.error("FCM error: %s", e)
            return _error(502, "fcm_error", f"FCM rejected the message ({e.code})")
        return WakeOut(status="sent")

    return app
