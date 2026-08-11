from __future__ import annotations

import argparse
import json
import os

import requests


USER_AGENT = "KleinAntikMeissenScout/1.0 (broad porcelain discovery)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the governed broad-porcelain Meissen discovery pilot on Railway."
    )
    parser.add_argument("--dashboard-url", required=True)
    return parser.parse_args()


def start_run(session: requests.Session, dashboard_url: str) -> dict[str, object]:
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("DASHBOARD_USER and DASHBOARD_PASSWORD are required")
    response = session.post(
        f"{dashboard_url.rstrip('/')}/api/runs/meissen-broad-marketplace-pilot",
        auth=(username, password),
        headers={"User-Agent": USER_AGENT},
        json={},
        timeout=60,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Broad marketplace discovery was not started: {detail}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Broad marketplace discovery returned an invalid response")
    return payload


def main() -> int:
    args = parse_args()
    payload = start_run(requests.Session(), args.dashboard_url)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
