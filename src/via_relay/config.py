"""Relay configuration, read from ``VIA_RELAY_*`` environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIA_RELAY_")

    # Firebase service account (JSON key file of the project the Android app is built with).
    credentials_file: Path | None = None
    credentials_json: str | None = None

    host: str = "0.0.0.0"
    port: int = 8090
    forwarded_allow_ips: str = "127.0.0.1"
    log_level: str = "info"

    # Anonymous callers are limited per source IP.
    ip_rate_per_minute: int = 60
    ip_daily_limit: int = 5000
    # Callers presenting a known X-Via-Instance-Key are limited per key instead.
    instance_keys: str = ""
    instance_keys_file: Path | None = None
    key_rate_per_minute: int = 600
    key_daily_limit: int = 100_000
    # At most one wake per FCM token per interval; extra wakes are dropped (the device
    # fetches everything anyway).
    token_interval: float = 1.0

    # FCM message options.
    android_ttl: int = 86400
    collapse_key: str = "via-wake"
    dry_run: bool = False

    def load_instance_keys(self) -> set[str]:
        keys = {k.strip() for k in self.instance_keys.split(",")}
        if self.instance_keys_file:
            keys |= {line.strip() for line in self.instance_keys_file.read_text().splitlines()}
        return {k for k in keys if k and not k.startswith("#")}
