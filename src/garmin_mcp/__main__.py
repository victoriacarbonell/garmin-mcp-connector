"""Entry point: `python -m garmin_mcp` (or the `garmin-mcp` command).

The server speaks Streamable HTTP, the transport remote connectors use, at
http://<host>:<port>/mcp. Authorization is on by default; see `.env.example`.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import build_app
from .config import ConfigError, load_dotenv, load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The HTTP library logs every outgoing request line at INFO; keep the log to
    # our own events.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    load_dotenv()
    try:
        settings = load_settings()  # fail early and clearly if something is missing
    except ConfigError as exc:
        sys.exit(f"Incomplete configuration: {exc}")

    mode = f"authorization ON ({settings.auth.public_url})" if settings.auth else "authorization OFF (loopback only)"
    logging.getLogger("garmin_mcp").info("starting on %s:%d, %s", settings.host, settings.port, mode)
    # access_log=False: request lines would put login tokens (query strings of the
    # GitHub callback) and activity ids into the log.
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
