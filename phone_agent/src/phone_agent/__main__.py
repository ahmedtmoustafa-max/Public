"""``python -m phone_agent`` -- run the server."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "phone_agent.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    main()
