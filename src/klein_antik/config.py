from __future__ import annotations

import os


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL ist nicht gesetzt.")
    return value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def serpapi_key() -> str:
    return (
        os.environ.get("SERPAPI_API_KEY_PRIMARY", "").strip()
        or os.environ.get("SERPAPI_API_KEY", "").strip()
    )

