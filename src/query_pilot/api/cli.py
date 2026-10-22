"""Command-line interface for the QueryPilot Hosted/Local API Service.

Provides a dedicated console runner for the FastAPI application defined in
query_pilot.api.app:app, honoring host, port, and auto-reload flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the API CLI."""
    parser = argparse.ArgumentParser(
        prog="querypilot-api",
        description="QueryPilot Hosted/Local API Service - LLM reasoning and SQL generation.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.getenv("QUERYPILOT_API_HOST", "127.0.0.1"),
        help="Host address to bind (default: 127.0.0.1 or QUERYPILOT_API_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("QUERYPILOT_API_PORT", "8000")),
        help="TCP port to bind (default: 8000 or QUERYPILOT_API_PORT).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Enable auto-reload for development (default: False).",
    )
    return parser


def run_api_cli(args: Optional[list[str]] = None) -> int:
    """Parse arguments and launch the QueryPilot API service via Uvicorn."""
    parser = build_parser()
    parsed = parser.parse_args(args)

    try:
        import uvicorn
        uvicorn.run(
            "query_pilot.api.app:app",
            host=parsed.host,
            port=parsed.port,
            reload=parsed.reload,
            log_level="info",
        )
        return 0
    except ImportError:
        sys.stderr.write("uvicorn is required to run the API service.\n")
        return 1


def main() -> None:
    """Console script entry point for querypilot-api."""
    sys.exit(run_api_cli())


if __name__ == "__main__":
    main()
