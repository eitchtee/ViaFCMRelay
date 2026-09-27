from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from via_relay.config import Settings
from via_relay.fcm import FcmClient, ServiceAccount
from via_relay.main import create_app

TOKEN_URI = "https://oauth2.example/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/via-test/messages:send"
FCM_TOKEN = "fcm-token-" + "a" * 40


@pytest.fixture(scope="session")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def account_json(private_key: rsa.RSAPrivateKey) -> str:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "via-test",
            "private_key_id": "kid1",
            "private_key": pem,
            "client_email": "relay@via-test.iam.gserviceaccount.com",
            "token_uri": TOKEN_URI,
        }
    )


@dataclass
class FakeGoogle:
    """Stands in for Google's OAuth token endpoint and the FCM send endpoint."""

    token_requests: list[httpx.Request] = field(default_factory=list)
    sends: list[dict[str, Any]] = field(default_factory=list)
    send_auth: list[str] = field(default_factory=list)
    # Queue of (status, body, headers) replies for the send endpoint; default is success.
    replies: list[tuple[int, dict[str, Any], dict[str, str]]] = field(default_factory=list)
    token_counter: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URI:
            self.token_requests.append(request)
            self.token_counter += 1
            return httpx.Response(
                200, json={"access_token": f"access-{self.token_counter}", "expires_in": 3600}
            )
        assert str(request.url) == SEND_URL
        self.sends.append(json.loads(request.content))
        self.send_auth.append(request.headers["authorization"])
        if self.replies:
            status, body, headers = self.replies.pop(0)
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(200, json={"name": "projects/via-test/messages/1"})

    def reply(
        self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        self.replies.append((status, body, headers or {}))


def fcm_error(status: int, code: str, grpc_status: str = "NOT_FOUND") -> dict[str, Any]:
    return {
        "error": {
            "code": status,
            "status": grpc_status,
            "details": [
                {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}
            ],
        }
    }


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def fcm(account_json: str, google: FakeGoogle) -> FcmClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    return FcmClient(ServiceAccount.from_json(account_json), http)


@pytest.fixture
def settings() -> Settings:
    return Settings(instance_keys="good-key", token_interval=0)


@asynccontextmanager
async def relay_client(settings: Settings, fcm: FcmClient) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, fcm)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://relay") as client,
    ):
        yield client


@pytest.fixture
async def client(settings: Settings, fcm: FcmClient) -> AsyncIterator[AsyncClient]:
    async with relay_client(settings, fcm) as c:
        yield c
