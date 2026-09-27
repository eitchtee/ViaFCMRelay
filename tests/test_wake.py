from httpx import AsyncClient

from via_relay.config import Settings
from via_relay.fcm import FcmClient

from .conftest import FCM_TOKEN, FakeGoogle, fcm_error, relay_client

PUSH_ID = "01J9Z3K2Q8V6T4R2N0M8K6H4G2"


async def test_wake(client: AsyncClient, google: FakeGoogle) -> None:
    r = await client.post("/v1/wake", json={"token": FCM_TOKEN, "id": PUSH_ID})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "sent"}
    assert google.sends[0]["message"]["data"] == {"t": "wake", "id": PUSH_ID}
    assert (await client.get("/v1/health")).json() == {"status": "ok"}


async def test_only_the_fixed_shape_is_accepted(client: AsyncClient, google: FakeGoogle) -> None:
    for body in [
        {"token": FCM_TOKEN, "id": "not a ulid"},
        {"token": FCM_TOKEN, "title": "Buy crypto now"},  # no free-form fields
        {"token": "short"},
        {"token": "has spaces " * 5},
        {},
    ]:
        r = await client.post("/v1/wake", json=body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == "validation_error"
    assert google.sends == []


async def test_gone_token_returns_410(client: AsyncClient, google: FakeGoogle) -> None:
    google.reply(404, fcm_error(404, "UNREGISTERED"))
    r = await client.post("/v1/wake", json={"token": FCM_TOKEN})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "token_gone"


async def test_fcm_outage_returns_503(client: AsyncClient, google: FakeGoogle) -> None:
    google.reply(503, fcm_error(503, "UNAVAILABLE", "UNAVAILABLE"), {"Retry-After": "12"})
    r = await client.post("/v1/wake", json={"token": FCM_TOKEN})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "12"


async def test_wakes_are_coalesced_per_token(fcm: FcmClient, google: FakeGoogle) -> None:
    async with relay_client(Settings(token_interval=60), fcm) as c:
        statuses = [
            (await c.post("/v1/wake", json={"token": FCM_TOKEN})).json()["status"] for _ in range(3)
        ]
        other = await c.post("/v1/wake", json={"token": FCM_TOKEN + "b"})
    assert statuses == ["sent", "coalesced", "coalesced"]
    assert other.json()["status"] == "sent"
    assert len(google.sends) == 2


async def test_ip_rate_limit(fcm: FcmClient, google: FakeGoogle) -> None:
    settings = Settings(ip_rate_per_minute=2, token_interval=0, instance_keys="k1")
    async with relay_client(settings, fcm) as c:
        codes = [
            (await c.post("/v1/wake", json={"token": FCM_TOKEN})).status_code for _ in range(3)
        ]
        # A registered instance key has its own, larger budget.
        keyed = await c.post(
            "/v1/wake", json={"token": FCM_TOKEN}, headers={"X-Via-Instance-Key": "k1"}
        )
        limited = await c.post("/v1/wake", json={"token": FCM_TOKEN})
    assert codes == [200, 200, 429]
    assert keyed.status_code == 200
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1


async def test_daily_limit(fcm: FcmClient) -> None:
    async with relay_client(Settings(ip_daily_limit=1, token_interval=0), fcm) as c:
        assert (await c.post("/v1/wake", json={"token": FCM_TOKEN})).status_code == 200
        r = await c.post("/v1/wake", json={"token": FCM_TOKEN})
    assert r.status_code == 429
    assert r.json()["error"]["message"] == "Daily limit reached"


async def test_instance_keys(client: AsyncClient, google: FakeGoogle) -> None:
    r = await client.post(
        "/v1/wake", json={"token": FCM_TOKEN}, headers={"X-Via-Instance-Key": "wrong"}
    )
    assert r.status_code == 401
    r = await client.post(
        "/v1/wake", json={"token": FCM_TOKEN}, headers={"X-Via-Instance-Key": "good-key"}
    )
    assert r.status_code == 200
    assert len(google.sends) == 1


def test_instance_keys_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "keys.txt"
    path.write_text("# comment\nkey-a\n\n key-b \n")
    settings = Settings(instance_keys="key-c, ", instance_keys_file=path)
    assert settings.load_instance_keys() == {"key-a", "key-b", "key-c"}
