"""Streamable HTTP transport for the MCP server, with the workspace files beside it.

``plat-uw mcp --http`` serves ``engine.mcp_server`` at ``http://<host>:<port>/mcp``
and the deal workspace files at ``/files/<deal_id>/<path>`` (the links
``export_deal`` returns), behind one access check.

Modes:

- **Local** (the default): bound to a loopback address. No token is needed;
  requests must carry a loopback ``Host`` header (DNS-rebinding guard). If
  ``PLAT_UW_TOKEN`` is set it is enforced here too. ``path`` inputs work.
- **Remote**: a non-loopback bind. Needs ``--allow-remote`` and a bearer token
  in ``PLAT_UW_TOKEN`` (at least ``MIN_TOKEN_LENGTH`` characters), checked on
  every request with a constant-time compare. ``add_document(path=)`` is
  refused and responses carry no server paths.

The bearer token is interim access control for a trusted user or a tunnel,
not production authentication: no users, scopes, rotation or audit. When the
server is hosted, OAuth plugs in at ``authorize`` (the single seam every
request passes through, MCP and files alike), and ``AuthDecision.principal``
carries the signed-in user to the workflow layer.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from engine.workflow import service
from engine.workflow.config import load_config
from engine.workflow.workspace import Workspace, WorkspaceError

TOKEN_ENV = "PLAT_UW_TOKEN"
PUBLIC_URL_ENV = "PLAT_UW_PUBLIC_URL"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
FILES_PREFIX = "/files/"
MIN_TOKEN_LENGTH = 16
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class LaunchError(Exception):
    """The HTTP server cannot start with these settings (the message says how to fix it)."""


def is_loopback(host: str) -> bool:
    name = host.strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class HttpSettings:
    host: str
    port: int
    remote: bool
    token: str | None
    base_url: str

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}{MCP_PATH}"


def http_settings(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, allow_remote: bool = False,
                  env: Mapping[str, str] | None = None) -> HttpSettings:
    """Check the launch flags and environment; raise LaunchError with the fix when they do not fit."""
    env = os.environ if env is None else env
    token = env.get(TOKEN_ENV) or None
    remote = not is_loopback(host)
    if remote:
        if not allow_remote:
            raise LaunchError(f"{host} is not a loopback address. Binding beyond this machine needs --allow-remote "
                              f"and a bearer token in {TOKEN_ENV}; for local use keep the default {DEFAULT_HOST}.")
        if not token:
            raise LaunchError(f"--allow-remote needs a bearer token: set {TOKEN_ENV} to a long random secret "
                              "(for example: python -c 'import secrets; print(secrets.token_urlsafe(32))').")
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise LaunchError(f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters.")
    if not 0 < int(port) < 65536:
        raise LaunchError(f"port {port} is out of range.")
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    base_url = (env.get(PUBLIC_URL_ENV) or f"http://{shown}:{int(port)}").rstrip("/")
    return HttpSettings(host=host, port=int(port), remote=remote, token=token, base_url=base_url)


# ── the access seam ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    status: int = 200
    reason: str = ""
    principal: str | None = None  # who is calling; OAuth fills this in when hosted


def _host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return is_loopback(host)


def authorize(headers: Mapping[str, str], settings: HttpSettings) -> AuthDecision:
    """Decide whether one HTTP request may proceed. The only place access is checked.

    Today: a static bearer token (``PLAT_UW_TOKEN``) compared in constant time,
    and, for a token-less loopback server, a loopback ``Host`` header. Hosting
    replaces the token branch with OAuth token validation.
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}
    if settings.token:
        scheme, _, credential = (lowered.get("authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential.strip().encode(), settings.token.encode()):
            return AuthDecision(False, 401, "a valid bearer token is required")
        return AuthDecision(True, principal="token")
    if not settings.remote and not _host_is_loopback(lowered.get("host")):
        return AuthDecision(False, 403, "this local server only answers loopback requests")
    return AuthDecision(True, principal="local")


class AccessGate:
    """ASGI middleware: ``authorize`` every HTTP request, then serve /files/ or pass on to MCP."""

    def __init__(self, app: Any, settings: HttpSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifespan
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        decision = authorize(headers, self.settings)
        if not decision.allowed:
            await _json(scope, receive, send, decision.status, {"error": decision.reason},
                        {"WWW-Authenticate": 'Bearer realm="plat-underwriting"'} if decision.status == 401 else None)
            return
        path = scope.get("path") or ""
        if path.startswith(FILES_PREFIX):
            await self._file(scope, receive, send, path[len(FILES_PREFIX):])
            return
        await self.app(scope, receive, send)

    async def _file(self, scope: dict, receive: Any, send: Any, rest: str) -> None:
        from starlette.responses import FileResponse

        if scope.get("method") not in ("GET", "HEAD"):
            await _json(scope, receive, send, 405, {"error": "use GET"})
            return
        deal_id, _, relpath = rest.partition("/")
        try:
            target = Workspace(load_config().deals_dir).resolve_file(deal_id, relpath)
        except WorkspaceError:
            await _json(scope, receive, send, 404, {"error": "not found"})
            return
        await FileResponse(target, filename=target.name)(scope, receive, send)


async def _json(scope: dict, receive: Any, send: Any, status: int, body: dict,
                headers: Mapping[str, str] | None = None) -> None:
    from starlette.responses import Response

    await Response(json.dumps(body), status_code=status, media_type="application/json",
                   headers=dict(headers or {}))(scope, receive, send)


def build_http_app(settings: HttpSettings, server: Any = None) -> Any:
    """The ASGI app: the MCP Streamable HTTP endpoint and /files/, behind ``AccessGate``.

    Also switches the workflow tools to this mode (remote mode refuses ``path``
    inputs; ``export_deal`` links point at ``settings.base_url``).
    """
    if server is None:
        from engine.mcp_server import mcp as server

    service.configure(transport="http", remote=settings.remote, base_url=settings.base_url)
    # Base64 inflates uploads by a third; leave room for the JSON-RPC envelope.
    max_body = load_config().max_upload_bytes * 4 // 3 + 1024 * 1024
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, host=settings.host,
                                     max_request_body_size=max_body)
    return AccessGate(app, settings)


def serve(settings: HttpSettings) -> None:  # pragma: no cover - opens a socket
    import uvicorn

    uvicorn.run(build_http_app(settings), host=settings.host, port=settings.port, log_level="info")
