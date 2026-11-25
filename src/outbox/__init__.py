"""A Relay-compatible outbox for testing email flows."""

from __future__ import annotations

import argparse
import logging


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from outbox.app import create_app
    from outbox.config import from_env

    settings = from_env()
    parser = argparse.ArgumentParser(prog="outbox", description=__doc__)
    parser.add_argument("--host", default=settings.host, help="bind address (OUTBOX_HOST)")
    parser.add_argument("--port", type=int, default=settings.port, help="port (OUTBOX_PORT, default 8025)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
