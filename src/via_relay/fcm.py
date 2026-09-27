"""Minimal async client for the FCM HTTP v1 API.

Authenticates with a Firebase service account via the OAuth 2.0 JWT bearer flow (the
assertion is signed locally with the account's RSA key) and caches the access token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
# Treat the access token as expired this long before it really does.
_REFRESH_MARGIN = 300

# FCM error codes meaning "this token will never work again".
_GONE_CODES = {"UNREGISTERED", "SENDER_ID_MISMATCH", "INVALID_ARGUMENT"}


class FcmError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"FCM returned {status} {code}")
        self.status = status
        self.code = code


class TokenGone(FcmError):
    """The FCM registration token is invalid or no longer registered."""


class FcmUnavailable(FcmError):
    """FCM is overloaded or failing; try again later."""

    def __init__(self, status: int, code: str, retry_after: int) -> None:
        super().__init__(status, code)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: rsa.RSAPrivateKey
    private_key_id: str
    token_uri: str

    @classmethod
    def from_json(cls, raw: str) -> ServiceAccount:
        data = json.loads(raw)
        if data.get("type") != "service_account":
            raise ValueError("credentials are not a service account key")
        key = serialization.load_pem_private_key(data["private_key"].encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("service account key is not an RSA key")
        return cls(
            project_id=data["project_id"],
            client_email=data["client_email"],
            private_key=key,
            private_key_id=data.get("private_key_id", ""),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        )

    @classmethod
    def load(cls, path: Path | None, raw: str | None) -> ServiceAccount:
        if raw:
            return cls.from_json(raw)
        if path:
            return cls.from_json(path.read_text())
        raise ValueError(
            "no Firebase credentials: set VIA_RELAY_CREDENTIALS_FILE or VIA_RELAY_CREDENTIALS_JSON"
        )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FcmClient:
    def __init__(
        self,
        account: ServiceAccount,
        http: httpx.AsyncClient,
        *,
        android_ttl: int = 86400,
        collapse_key: str = "via-wake",
        dry_run: bool = False,
    ) -> None:
        self.account = account
        self.http = http
        self.android_ttl = android_ttl
        self.collapse_key = collapse_key
        self.dry_run = dry_run
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def _assertion(self) -> str:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.account.private_key_id}
        claims = {
            "iss": self.account.client_email,
            "scope": SCOPE,
            "aud": self.account.token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = (
            f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
        )
        signature = self.account.private_key.sign(
            signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_b64url(signature)}"

    async def access_token(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._expires - _REFRESH_MARGIN:
                return self._token
            try:
                response = await self.http.post(
                    self.account.token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": self._assertion(),
                    },
                )
            except httpx.HTTPError as e:
                raise FcmUnavailable(0, "AUTH_UNREACHABLE", 30) from e
            if response.is_error:
                raise FcmUnavailable(response.status_code, "AUTH_FAILED", 60)
            body = response.json()
            self._token = str(body["access_token"])
            self._expires = time.time() + float(body.get("expires_in", 3600))
            return self._token

    async def send_wake(self, fcm_token: str, push_id: str | None) -> None:
        """Send the data-only wake-up message. Raises :class:`FcmError` subclasses."""
        data = {"t": "wake"}
        if push_id:
            data["id"] = push_id
        body: dict[str, Any] = {
            "message": {
                "token": fcm_token,
                "data": data,
                "android": {
                    "priority": "high",
                    "ttl": f"{self.android_ttl}s",
                    "collapse_key": self.collapse_key,
                },
            }
        }
        if self.dry_run:
            body["validate_only"] = True
        url = SEND_URL.format(project=self.account.project_id)

        for attempt in range(2):
            access = await self.access_token(force=attempt > 0)
            try:
                response = await self.http.post(
                    url, json=body, headers={"Authorization": f"Bearer {access}"}
                )
            except httpx.HTTPError as e:
                raise FcmUnavailable(0, "UNREACHABLE", 30) from e
            if response.status_code != 401:
                break  # 401: the cached access token was revoked; refresh once and retry

        if response.is_success:
            return
        code = _error_code(response)
        if response.status_code == 404 or code in _GONE_CODES:
            raise TokenGone(response.status_code, code)
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("retry-after", "")
            raise FcmUnavailable(
                response.status_code, code, int(retry_after) if retry_after.isdigit() else 60
            )
        raise FcmError(response.status_code, code)


def _error_code(response: httpx.Response) -> str:
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return "UNKNOWN"
    for detail in error.get("details", []):
        if str(detail.get("@type", "")).endswith("FcmError") and detail.get("errorCode"):
            return str(detail["errorCode"])
    return str(error.get("status", "UNKNOWN"))
