"""``plat-uw``: the command line entry point for the underwriting MCP server and the browser app.

    plat-uw mcp                                   stdio (default; Claude Desktop and other local clients)
    plat-uw mcp --http [--host 127.0.0.1] [--port 8765]
                                                  Streamable HTTP at http://<host>:<port>/mcp
    plat-uw mcp --http --host 0.0.0.0 --allow-remote
                                                  beyond loopback; needs PLAT_UW_TOKEN (bearer token)
    plat-uw serve [--host 127.0.0.1] [--port 8766]
                                                  the analyst screens as a browser app (engine.ui.server)

See ``engine.mcp_http`` for the access rules (``plat-uw serve`` uses the same ones). Status lines go to stderr so
stdout stays the MCP stdio channel.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from engine.mcp_http import DEFAULT_HOST, DEFAULT_PORT, LaunchError

SERVE_PORT = 8766  # engine.ui.server.DEFAULT_PORT (kept here so --help needs no Starlette import)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plat-uw", description="Multifamily underwriting tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    mcp = sub.add_parser("mcp", help="Run the MCP server (stdio by default).")
    mcp.add_argument("--http", action="store_true", help="Serve Streamable HTTP instead of stdio.")
    mcp.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP bind address (default {DEFAULT_HOST}, loopback).")
    mcp.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default {DEFAULT_PORT}).")
    mcp.add_argument("--allow-remote", action="store_true",
                     help="Allow a non-loopback --host. Requires PLAT_UW_TOKEN; disables path inputs.")
    serve = sub.add_parser("serve", help="Serve the analyst screens as a browser app on this computer.")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default {DEFAULT_HOST}, loopback).")
    serve.add_argument("--port", type=int, default=SERVE_PORT, help=f"Port (default {SERVE_PORT}).")
    serve.add_argument("--allow-remote", action="store_true",
                       help="Allow a non-loopback --host. Requires PLAT_UW_TOKEN (sign in at /login).")
    return parser


def _serve(args: argparse.Namespace) -> int:
    from engine.ui import server

    try:
        settings = server.serve_settings(args.host, args.port, allow_remote=args.allow_remote)
    except LaunchError as exc:
        print(f"plat-uw: {exc}", file=sys.stderr)
        return 2
    mode = "remote (sign in with PLAT_UW_TOKEN at /login)" if settings.remote else "local"
    print(f"plat-uw: the browser app is at {settings.base_url}/ ({mode}); press Ctrl+C to stop", file=sys.stderr)
    server.serve(settings)  # pragma: no cover - blocks
    return 0  # pragma: no cover


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command != "mcp":  # pragma: no cover - argparse enforces the choice
        return 2
    if not args.http:
        if args.allow_remote or args.host != DEFAULT_HOST:
            print("--host and --allow-remote apply to --http only.", file=sys.stderr)
            return 2
        from engine.mcp_server import main as stdio_main

        stdio_main()
        return 0
    from engine import mcp_http

    try:
        settings = mcp_http.http_settings(args.host, args.port, allow_remote=args.allow_remote)
    except LaunchError as exc:
        print(f"plat-uw: {exc}", file=sys.stderr)
        return 2
    mode = "remote (bearer token required; path inputs disabled)" if settings.remote else "local"
    print(f"plat-uw: serving MCP at {settings.mcp_url} ({mode})", file=sys.stderr)
    mcp_http.serve(settings)  # pragma: no cover - blocks
    return 0  # pragma: no cover


def run() -> None:  # pragma: no cover - console script wrapper
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
