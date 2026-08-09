from __future__ import annotations

import os


def main() -> None:
    mode = os.environ.get("APP_MODE", "dashboard").strip().lower()
    if mode == "worker":
        from .worker import main as worker_main

        worker_main()
        return
    if mode == "matcher":
        from .match_worker import main as matcher_main

        matcher_main()
        return
    if mode != "dashboard":
        raise RuntimeError(f"Unbekannter APP_MODE: {mode}")

    port = os.environ.get("PORT", "8080")
    command = [
        "gunicorn",
        "--bind",
        f"0.0.0.0:{port}",
        "--workers",
        "2",
        "--threads",
        "4",
        "klein_antik.web:app",
    ]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
