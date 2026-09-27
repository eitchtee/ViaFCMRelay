# Via FCM relay

Wakes [Via](https://github.com/eitchtee/Via) Android clients through Firebase Cloud Messaging (FCM).

FCM can only reach an app through the Firebase project the app was built with, and only
someone holding that project's credentials can send through it. Self-hosted Via servers
therefore don't talk to FCM themselves: they ask this relay, which is run by whoever publishes
the Android app, to wake a device. The device then fetches its inbox from its own Via server.
This is the same model ntfy uses.

**The relay never sees content.** It accepts exactly one message shape and sends exactly one:

```
POST /v1/wake   {"token": "<FCM registration token>", "id": "<Via push id>"}   (id optional)
   → FCM data-only, high-priority message   {"t": "wake", "id": "<Via push id>"}
```

It never shows a notification and never forwards free-form text, so the worst a caller can do
is wake a phone that then finds nothing new. Apart from in-memory rate-limit counters (keyed by
hashes), it stores nothing.

## Responses

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"status": "sent"}` | FCM accepted the message |
| 200 | `{"status": "coalesced"}` | This token was woken less than `TOKEN_INTERVAL` ago; dropped |
| 410 | `error.code = token_gone` | The token is unregistered or invalid. Stop using it (Via clears it) |
| 401 | `invalid_instance_key` | An `X-Via-Instance-Key` was sent but isn't known |
| 422 | `validation_error` | Anything other than the fixed shape |
| 429 | `rate_limited` | See `Retry-After` |
| 502 | `fcm_error` | FCM rejected the message for another reason (e.g. relay misconfigured) |
| 503 | `fcm_unavailable` | FCM quota or outage. See `Retry-After` |

## Abuse control

- Anonymous callers are limited **per source IP**: a per-minute rate and a daily cap.
- **Per FCM token**, at most one wake per `TOKEN_INTERVAL` (1 s). Extra wakes are dropped,
  since one wake already makes the device fetch everything.
- FCM tokens can't be guessed: only the device and the Via server it registered with know them.
- Operators can hand out **instance keys** to busy servers. A request carrying a known
  `X-Via-Instance-Key` header is limited per key, with higher limits, instead of per IP.
  An unknown key gets a 401, so misconfigurations are obvious.

## Running

1. Download the compose file: `curl -O https://raw.githubusercontent.com/eitchtee/ViaFCMRelay/main/docker-compose.yml`
2. In the Firebase console of the Android app's project: *Project settings → Service accounts
   → Generate new private key*. Save it as `service-account.json` next to
   `docker-compose.yml` (it's git-ignored).
3. `docker compose up -d`
4. Put it behind TLS and point Via servers at it with `VIA_PUSH_RELAY_URL=https://relay.example.com`
   (and `VIA_PUSH_RELAY_KEY` if you gave that server an instance key).

Set `VIA_RELAY_DRY_RUN=true` to have FCM validate messages without delivering them.

## Images

Published to [`ghcr.io/eitchtee/viafcmrelay`](https://github.com/eitchtee/ViaFCMRelay/pkgs/container/viafcmrelay)
for `linux/amd64` and `linux/arm64` by `.github/workflows/docker.yml`:

| Tag | What |
|---|---|
| `latest` | The latest release |
| `v1.2.3` (a release tag) | That release |
| `nightly` | The newest commit on `main`; may be unstable |

Maintainers can also run the workflow by hand (Actions → Docker image → Run workflow) with a
branch or release tag and an optional custom image tag. `latest` only moves when the build is
the repository's latest release.

## Configuration

| Variable | Default | |
|---|---|---|
| `VIA_RELAY_CREDENTIALS_FILE` / `VIA_RELAY_CREDENTIALS_JSON` | (required) | Firebase service account key |
| `VIA_RELAY_HOST` / `VIA_RELAY_PORT` | `0.0.0.0` / `8090` | |
| `VIA_RELAY_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-For` |
| `VIA_RELAY_IP_RATE_PER_MINUTE` | `60` | Anonymous callers, per IP |
| `VIA_RELAY_IP_DAILY_LIMIT` | `5000` | Anonymous callers, per IP per UTC day |
| `VIA_RELAY_INSTANCE_KEYS` | | Comma-separated instance keys |
| `VIA_RELAY_INSTANCE_KEYS_FILE` | | One key per line (`#` comments allowed) |
| `VIA_RELAY_KEY_RATE_PER_MINUTE` | `600` | Per instance key |
| `VIA_RELAY_KEY_DAILY_LIMIT` | `100000` | Per instance key per UTC day |
| `VIA_RELAY_TOKEN_INTERVAL` | `1.0` | Seconds between wakes of the same token |
| `VIA_RELAY_ANDROID_TTL` | `86400` | Seconds FCM keeps the message for an offline device |
| `VIA_RELAY_COLLAPSE_KEY` | `via-wake` | Offline devices get only the latest wake |
| `VIA_RELAY_DRY_RUN` | `false` | FCM `validate_only` |
| `VIA_RELAY_LOG_LEVEL` | `info` | |

The relay runs as a single process: limits are held in memory and reset on restart.

## Development

```sh
uv sync
uv run pytest
uv run ruff format . && uv run ruff check . && uv run mypy
VIA_RELAY_CREDENTIALS_FILE=service-account.json uv run via-relay serve
```

FCM is called directly over its HTTP v1 API with `httpx`. The OAuth access token is obtained
by signing the service-account JWT locally, so the heavy, synchronous Firebase Admin SDK isn't
needed.

## License

[AGPL-3.0-or-later](LICENSE)
