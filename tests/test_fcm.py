import base64
import json
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from via_relay.fcm import SCOPE, FcmClient, FcmError, FcmUnavailable, ServiceAccount, TokenGone

from .conftest import FCM_TOKEN, TOKEN_URI, FakeGoogle, fcm_error


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


async def test_oauth_assertion_is_signed(
    fcm: FcmClient, google: FakeGoogle, private_key: rsa.RSAPrivateKey
) -> None:
    assert await fcm.access_token() == "access-1"
    form = parse_qs(google.token_requests[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    header_b64, claims_b64, signature_b64 = form["assertion"][0].split(".")
    private_key.public_key().verify(
        _b64decode(signature_b64),
        f"{header_b64}.{claims_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    header = json.loads(_b64decode(header_b64))
    claims = json.loads(_b64decode(claims_b64))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
    assert claims["iss"] == "relay@via-test.iam.gserviceaccount.com"
    assert claims["aud"] == TOKEN_URI
    assert claims["scope"] == SCOPE
    assert claims["exp"] - claims["iat"] == 3600


async def test_access_token_is_cached(fcm: FcmClient, google: FakeGoogle) -> None:
    await fcm.send_wake(FCM_TOKEN, None)
    await fcm.send_wake(FCM_TOKEN, None)
    assert len(google.token_requests) == 1
    assert google.send_auth == ["Bearer access-1", "Bearer access-1"]


async def test_wake_message_shape(fcm: FcmClient, google: FakeGoogle) -> None:
    await fcm.send_wake(FCM_TOKEN, "01J9Z3K2Q8V6T4R2N0M8K6H4G2")
    assert google.sends == [
        {
            "message": {
                "token": FCM_TOKEN,
                "data": {"t": "wake", "id": "01J9Z3K2Q8V6T4R2N0M8K6H4G2"},
                "android": {"priority": "high", "ttl": "86400s", "collapse_key": "via-wake"},
            }
        }
    ]


async def test_revoked_access_token_is_refreshed_once(fcm: FcmClient, google: FakeGoogle) -> None:
    google.reply(401, {"error": {"status": "UNAUTHENTICATED"}})
    await fcm.send_wake(FCM_TOKEN, None)
    assert google.send_auth == ["Bearer access-1", "Bearer access-2"]


@pytest.mark.parametrize(
    ("status", "body", "headers", "error", "code"),
    [
        (404, fcm_error(404, "UNREGISTERED"), {}, TokenGone, "UNREGISTERED"),
        (400, fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT"), {}, TokenGone, None),
        (403, fcm_error(403, "SENDER_ID_MISMATCH", "PERMISSION_DENIED"), {}, TokenGone, None),
        (
            429,
            fcm_error(429, "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED"),
            {"Retry-After": "7"},
            FcmUnavailable,
            None,
        ),
        (503, fcm_error(503, "UNAVAILABLE", "UNAVAILABLE"), {}, FcmUnavailable, None),
        (403, fcm_error(403, "THIRD_PARTY_AUTH_ERROR", "PERMISSION_DENIED"), {}, FcmError, None),
    ],
)
async def test_error_mapping(
    fcm: FcmClient,
    google: FakeGoogle,
    status: int,
    body: dict,  # type: ignore[type-arg]
    headers: dict[str, str],
    error: type[FcmError],
    code: str | None,
) -> None:
    google.reply(status, body, headers)
    with pytest.raises(error) as info:
        await fcm.send_wake(FCM_TOKEN, None)
    assert type(info.value) is error
    if code:
        assert info.value.code == code
    if isinstance(info.value, FcmUnavailable) and headers:
        assert info.value.retry_after == 7


def test_credentials_validation(account_json: str) -> None:
    with pytest.raises(ValueError, match="no Firebase credentials"):
        ServiceAccount.load(None, None)
    data = json.loads(account_json)
    with pytest.raises(ValueError, match="not a service account"):
        ServiceAccount.from_json(json.dumps({**data, "type": "authorized_user"}))
    assert ServiceAccount.from_json(account_json).project_id == "via-test"
