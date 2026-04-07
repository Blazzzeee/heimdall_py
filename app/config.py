"""
Centralized configuration with safe defaults for dev/test.
Explicitly require secrets in prod to avoid insecure deployments.
"""

from __future__ import annotations

import os

HEIMDALL_ENV = os.getenv("HEIMDALL_ENV", "dev").strip().lower()
ALLOW_DEFAULTS = os.getenv("HEIMDALL_ALLOW_DEFAULTS", "").strip().lower() in {"1", "true", "yes"}


def _require_secret(name: str, default: str | None = None) -> str:
    value = os.getenv(name)
    if value:
        return value
    if HEIMDALL_ENV in {"dev", "test"} and ALLOW_DEFAULTS and default is not None:
        print(f"⚠️  {name} not set; using dev default. Set {name} for production.")
        return default
    raise RuntimeError(f"{name} must be set for environment '{HEIMDALL_ENV}'.")


INFRA_API_KEY = _require_secret("INFRA_API_KEY")
WEBHOOK_TTL_SECONDS = int(os.getenv("WEBHOOK_TTL_SECONDS", "60"))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MONITOR_INTERVAL_SECONDS = float(os.getenv("MONITOR_INTERVAL_SECONDS", "5"))
HEARTBEAT_TIMEOUT_SECONDS = float(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "2"))
