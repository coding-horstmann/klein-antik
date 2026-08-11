from __future__ import annotations

import argparse
import json
import os

import requests


USER_AGENT = "KleinAntikMeissenScout/1.0 (broad porcelain discovery)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start a governed Meissen marketplace discovery pilot on Railway."
    )
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--pilot", choices=("broad", "implicit"), default="broad")
    return parser.parse_args()


def start_run(
    session: requests.Session, dashboard_url: str, pilot: str
) -> dict[str, object]:
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("DASHBOARD_USER and DASHBOARD_PASSWORD are required")
    response = session.post(
        f"{dashboard_url.rstrip('/')}/api/runs/meissen-{pilot}-marketplace-pilot",
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
        raise RuntimeError(f"Marketplace discovery was not started: {detail}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Marketplace discovery returned an invalid response")
    return payload


def main() -> int:
    args = parse_args()
    payload = start_run(requests.Session(), args.dashboard_url, args.pilot)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
