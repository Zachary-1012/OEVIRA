"""Run the local OEVIRA API and its durable worker loop."""
from __future__ import annotations

import argparse
import threading

from . import create_app
from .auth import issue_local_capability
from .store import Store
from .worker import run_forever


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--port", type=int, default=8317)
    parser.add_argument("--web-root")
    parser.add_argument("--bootstrap-file")
    parser.add_argument("--issue-session", action="store_true", help="rotate a single-use local login capability and exit")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.issue_session:
        if not args.bootstrap_file:
            parser.error("--issue-session requires --bootstrap-file")
        issue_local_capability(Store(args.data_root), args.bootstrap_file)
        return 0
    from waitress import serve

    app = create_app(
        args.data_root,
        args.web_root,
        expected_port=args.port,
        bootstrap_file=args.bootstrap_file,
    )
    stop_event = threading.Event()
    worker = threading.Thread(target=run_forever, args=(app, stop_event), name="oevira-worker", daemon=True)
    worker.start()
    try:
        serve(app, host="127.0.0.1", port=args.port, threads=8, ident="OEVIRA")
    finally:
        stop_event.set()
        worker.join(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
