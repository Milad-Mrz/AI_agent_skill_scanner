"""Run the web UI: ``python -m skillspector.ext.webui [--port 8765]``.

The server binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill Scanner web UI (localhost only)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run("skillspector.ext.webui.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
