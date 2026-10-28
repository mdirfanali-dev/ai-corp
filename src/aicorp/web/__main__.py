"""Run the dashboard: ``python -m aicorp.web [--port 8765] [--open]``. Binds to localhost only."""
from __future__ import annotations

import argparse
import os
import threading
import time

import uvicorn

from ..launcher import open_window
from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="aicorp-web", description="AICorp dashboard (localhost only)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AICORP_PORT", "8765")))
    parser.add_argument("--open", action="store_true", help="open an app-style browser window")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    if args.open:
        def later() -> None:
            time.sleep(1.5)
            print("window:", open_window(url))
        threading.Thread(target=later, daemon=True).start()
    print(f"AICorp dashboard: {url}  (Ctrl+C to stop)")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
