"""Local entry point: `python -m garmin_mcp` (or the `garmin-mcp` command).

PHASE 1: the server has NO authorization yet, so it only listens on 127.0.0.1
(your own computer). This is hard-coded on purpose: it must not be possible to
expose it to the network until phase 2 adds OAuth.
"""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_dotenv, load_settings
from .server import mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_dotenv()
    try:
        settings = load_settings()  # fail early and clearly if the key is missing
    except ConfigError as exc:
        sys.exit(f"Incomplete configuration: {exc}")
    # Streamable HTTP is the transport remote connectors (claude.ai) use.
    # The endpoint is http://127.0.0.1:<port>/mcp
    mcp.run("streamable-http", host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    main()
