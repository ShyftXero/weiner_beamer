"""WeinerBeamer - laser-engrave hotdogs from a web queue."""

import argparse
import logging
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="weiner-beamer", description="Hotdog laser engraving queue")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 to reach it from phones/tablets)")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--data", default=os.environ.get("WB_DATA", "data"), help="data directory (db, fonts)")
    ap.add_argument("--password", default=os.environ.get("WB_PASSWORD"),
                    help="require HTTP basic auth with this password (any username)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.host not in ("127.0.0.1", "localhost") and not args.password:
        logging.warning("listening on %s with no --password: anyone on the network can fire the laser", args.host)

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(Path(args.data), args.password), host=args.host, port=args.port, log_level="info")
