from __future__ import annotations

import argparse
import logging

from via_relay import __version__
from via_relay.config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="via-relay", description="Via FCM relay")
    parser.add_argument("--version", action="version", version=f"via-relay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the relay")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    settings = Settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import uvicorn

    uvicorn.run(
        "via_relay.main:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
