"""``plat-uw serve``: the analyst screens as a browser app, on this computer.

    plat-uw serve [--port 8766] [--host 127.0.0.1]
    plat-uw serve --host 0.0.0.0 --allow-remote      # needs PLAT_UW_TOKEN; sign in at /login

Routes:

- ``GET /``: the single-page app (``engine.ui.screens.app_html``): the deal list, the four screens
  and the guides.
- ``POST /api/tools/<name>``: a JSON object of the tool's arguments in, the same envelope the MCP
  tool returns out (``engine.workflow.service``). This small bridge stands in for the MCP host.
- ``POST /api/upload``: multipart ``deal_id``, ``kind`` and ``file``; stored through ``add_document``.
- ``GET /api/guide/<name>``: ``user-guide``, ``glossary`` or ``start-here`` as escaped HTML.
- ``GET /files/<deal_id>/<path>``: workspace files (resolved inside the deal folder only).

Security, the same posture as ``plat-uw mcp --http`` (``engine.mcp_http``): loopback by default
with a loopback ``Host`` check against DNS rebinding; a non-loopback bind needs ``--allow-remote``
and a bearer token in ``PLAT_UW_TOKEN``. Every request goes through the one access seam,
``engine.mcp_http.authorize``; a browser cannot send a bearer header on navigation, so ``/login``
trades the token for an HttpOnly, SameSite=Strict cookie that this gate turns back into the
``Authorization`` header before calling ``authorize``. State-changing requests (every POST) also
need the ``X-Plat-UW: 1`` header (a cross-site page cannot send it without a CORS preflight this
server never approves) and a same-origin ``Origin`` / ``Sec-Fetch-Site`` when the browser sends
them. Uploads are capped at the workspace limit and checked by extension; ``path`` inputs are
refused (the browser uploads bytes); files outside a deal's folder are never served.
"""

from __future__ import annotations

import base64
import inspect
import json
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from engine import mcp_http
from engine.mcp_http import HttpSettings, LaunchError, authorize
from engine.ui import markdown
from engine.ui.screens import app_html, script_hashes
from engine.workflow import guides, service
from engine.workflow.config import load_config
from engine.workflow.workspace import Workspace, WorkspaceError

DEFAULT_PORT = 8766
CSRF_HEADER = "x-plat-uw"
COOKIE = "plat_uw_session"
MAX_JSON_BYTES = 1024 * 1024
TOOLS = ("start_here", "list_deals", "get_deal", "load_sample_deal", "create_deal", "add_document", "describe_deal",
         "ingest_deal", "review_assumptions", "edit_assumptions", "run_deal", "solve_price", "export_deal")
GUIDE_ROUTES = {"USER_GUIDE.md": "#/guide/user-guide", "ASSUMPTION_GLOSSARY.md": "#/guide/glossary"}
GUIDES = {
    "user-guide": ("User guide", guides.user_guide_markdown),
    "glossary": ("Assumption glossary", guides.glossary_markdown),
    "start-here": ("Start here", guides.start_here_markdown),
}
BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def serve_settings(host: str = mcp_http.DEFAULT_HOST, port: int = DEFAULT_PORT, *, allow_remote: bool = False,
                   env: Optional[Mapping[str, str]] = None) -> HttpSettings:
    """The launch checks of ``plat-uw mcp --http`` (loopback, or remote with a token)."""
    return mcp_http.http_settings(host, port, allow_remote=allow_remote, env=env)


def _envelope(code: str, message: str, fix: str) -> dict[str, Any]:
    return service.error(code, message, fix)


def _origin_ok(headers: Mapping[str, str], settings: HttpSettings, scheme: str) -> bool:
    """True when the browser says the request comes from this app's own origin (or says nothing)."""
    site = headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    host = headers.get("host", "")
    allowed = {f"{scheme}://{host}".lower()}
    public = urlsplit(settings.base_url)
    allowed.add(f"{public.scheme}://{public.netloc}".lower())
    return origin.lower() in allowed


class BrowserGate:
    """ASGI middleware: one access decision (``authorize``) and the CSRF check for every request."""

    def __init__(self, app: Any, settings: HttpSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse, RedirectResponse

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        cookie = _cookie(headers.get("cookie", ""), COOKIE)
        if self.settings.token and cookie and "authorization" not in headers:
            headers["authorization"] = f"Bearer {cookie}"
        path = scope.get("path") or "/"
        method = scope.get("method", "GET")
        decision = authorize(headers, self.settings)
        login = path == "/login" and self.settings.token is not None
        if not decision.allowed and not login:
            if method in ("GET", "HEAD") and path == "/" and self.settings.token:
                await RedirectResponse("/login", status_code=303, headers=BASE_HEADERS)(scope, receive, send)
                return
            await JSONResponse(_envelope("NOT_AUTHORIZED", decision.reason, "Sign in at /login with the server's token."
                                         if self.settings.token else "Open the app at http://127.0.0.1 on this computer."),
                               status_code=decision.status, headers=BASE_HEADERS)(scope, receive, send)
            return
        if method not in ("GET", "HEAD"):
            scheme = scope.get("scheme", "http")
            if not _origin_ok(headers, self.settings, scheme) or (not login and headers.get(CSRF_HEADER) != "1"):
                await JSONResponse(_envelope("CSRF_REJECTED", "the request did not come from this app",
                                             "Use the app's own buttons; reload the page if this persists."),
                                   status_code=403, headers=BASE_HEADERS)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _cookie(header: str, name: str) -> Optional[str]:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return None


class _TooLarge(Exception):
    pass


def _limited(receive: Any, limit: int) -> Any:
    """An ASGI receive that stops a body (chunked or not) once it passes ``limit`` bytes."""
    seen = 0

    async def wrapped() -> dict:
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise _TooLarge()
        return message

    return wrapped


async def _read_body(request: Any, limit: int) -> Optional[bytes]:
    """The request body, or None when it is larger than ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _call(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    fn = getattr(service, name)
    params = inspect.signature(fn).parameters
    if name == "add_document" and args.get("path") is not None:
        return _envelope("PATH_NOT_ALLOWED", "the browser app does not read paths on the server",
                         "Upload the file instead (drag it onto the document box).")
    takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    unknown = [key for key in args if key not in params and not takes_any]
    if unknown:
        return _envelope("BAD_ARGUMENTS", f"{name} does not take {', '.join(sorted(unknown))}",
                         f"Use the arguments documented for {name} in docs/MCP_TOOLS.md.")
    missing = [key for key, p in params.items() if p.default is inspect.Parameter.empty
               and p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL) and key not in args]
    if missing:
        return _envelope("BAD_ARGUMENTS", f"{name} needs {', '.join(missing)}",
                         f"Pass {', '.join(missing)} (see {name} in docs/MCP_TOOLS.md).")
    return fn(**args)


def build_app(settings: HttpSettings) -> Any:
    """The ASGI app behind ``BrowserGate``; switches the workflow tools to HTTP mode."""
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    service.configure(transport="http", remote=settings.remote, base_url=settings.base_url)

    def page_headers(html: str) -> dict[str, str]:
        csp = ("default-src 'none'; script-src " + " ".join(script_hashes(html)) + "; style-src 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return {**BASE_HEADERS, "Content-Security-Policy": csp, "Cache-Control": "no-store"}

    def api_json(body: Mapping[str, Any], status: int = 200) -> Response:
        return JSONResponse(body, status_code=status, headers={**BASE_HEADERS, "Cache-Control": "no-store"})

    async def index(request: Request) -> Response:
        html = app_html(workspace=load_config().as_dict())
        return HTMLResponse(html, headers=page_headers(html))

    async def tools(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in TOOLS:
            return api_json(_envelope("UNKNOWN_TOOL", f"{name!r} is not a workflow tool",
                                      f"Use one of: {', '.join(TOOLS)}."), 404)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return api_json(_envelope("BAD_REQUEST", "send the arguments as application/json",
                                      "Use the app's own buttons."), 415)
        limit = MAX_JSON_BYTES
        if name == "add_document":
            limit = load_config().max_upload_bytes * 4 // 3 + 64 * 1024
        raw = await _read_body(request, limit)
        if raw is None:
            return api_json(_envelope("TOO_LARGE", "the request is larger than this server accepts",
                                      "Send a smaller file, or raise PLAT_UW_MAX_UPLOAD_MB."), 413)
        try:
            args = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return api_json(_envelope("BAD_JSON", "the request body is not JSON", "Send a JSON object."), 400)
        if not isinstance(args, dict):
            return api_json(_envelope("BAD_JSON", "the request body must be a JSON object", "Send a JSON object."), 400)
        result = await run_in_threadpool(_call, name, args)
        return api_json(result)

    async def upload(request: Request) -> Response:
        config = load_config()
        limit = config.max_upload_bytes + 64 * 1024
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return api_json(_envelope("TOO_LARGE", f"the upload is larger than {config.max_upload_bytes:,} bytes",
                                      "Send a smaller export, or raise PLAT_UW_MAX_UPLOAD_MB."), 413)
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return api_json(_envelope("BAD_REQUEST", "send the file as multipart/form-data", "Use the upload box."), 415)
        try:
            form = await Request(request.scope, _limited(request.receive, limit)).form(max_files=1, max_fields=4)
        except _TooLarge:
            return api_json(_envelope("TOO_LARGE", f"the upload is larger than {config.max_upload_bytes:,} bytes",
                                      "Send a smaller export, or raise PLAT_UW_MAX_UPLOAD_MB."), 413)
        except Exception:  # noqa: BLE001 - a malformed multipart body
            return api_json(_envelope("BAD_REQUEST", "the upload could not be read", "Choose the file again."), 400)
        try:
            deal_id, kind, item = form.get("deal_id"), form.get("kind"), form.get("file")
            if not isinstance(deal_id, str) or not isinstance(kind, str) or item is None or isinstance(item, str):
                return api_json(_envelope("BAD_REQUEST", "the upload needs deal_id, kind and file",
                                          "Use the upload box."), 400)
            data = await item.read(config.max_upload_bytes + 1)
            if len(data) > config.max_upload_bytes:
                return api_json(_envelope("TOO_LARGE", f"{item.filename} is larger than {config.max_upload_bytes:,} bytes",
                                          "Send a smaller export, or raise PLAT_UW_MAX_UPLOAD_MB."), 413)
            filename = str(item.filename or kind)
        finally:
            await form.close()
        content = base64.b64encode(data).decode("ascii")
        result = await run_in_threadpool(service.add_document, deal_id, kind, filename, content_base64=content)
        return api_json(result)

    async def guide(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in GUIDES:
            return api_json(_envelope("GUIDE_NOT_FOUND", f"no guide named {name!r}",
                                      f"Use one of: {', '.join(GUIDES)}."), 404)
        title, source = GUIDES[name]
        html = markdown.render_markdown(source(), link_map=markdown.guide_link_map(GUIDE_ROUTES))
        return api_json({"ok": True, "title": title, "html": html})

    async def files(request: Request) -> Response:
        deal_id = request.path_params["deal_id"]
        rel = request.path_params["path"]
        try:
            target = Workspace(load_config().deals_dir).resolve_file(deal_id, rel)
        except WorkspaceError:
            return api_json(_envelope("NOT_FOUND", "no such file", "Use the links in Export files."), 404)
        if target.suffix.lower() == ".html":
            # Shown inline but sandboxed: an opaque origin with no access to this app or its cookie.
            headers = {**BASE_HEADERS, "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; "
                       "style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"}
            return FileResponse(target, media_type="text/html; charset=utf-8", headers=headers,
                                content_disposition_type="inline", filename=target.name)
        return FileResponse(target, filename=target.name, headers=BASE_HEADERS)

    login_html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in</title>
<style>body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;max-width:380px;
margin:12vh auto;padding:0 16px}input,button{font:inherit;padding:8px;width:100%;margin:6px 0}
.err{color:#a1281b}</style></head><body><h1>Multifamily underwriting</h1>
<p>This server needs its access token (the value of PLAT_UW_TOKEN).</p>{error}
<form method="post" action="/login"><label for="t">Token</label>
<input id="t" name="token" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>"""
    login_headers = {**BASE_HEADERS, "Cache-Control": "no-store",
                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                                "frame-ancestors 'none'; base-uri 'none'"}

    async def login(request: Request) -> Response:
        if not settings.token:
            return RedirectResponse("/", status_code=303, headers=BASE_HEADERS)
        if request.method == "GET":
            return HTMLResponse(login_html.replace("{error}", ""), headers=login_headers)
        raw = await _read_body(request, 4096)
        token = (parse_qs((raw or b"").decode("utf-8", "replace")).get("token") or [""])[0].strip()
        decision = authorize({"authorization": f"Bearer {token}", "host": request.headers.get("host", "")}, settings)
        if not token or not decision.allowed:
            return HTMLResponse(login_html.replace("{error}", '<p class="err" role="alert">That token is not right.</p>'),
                                status_code=401, headers=login_headers)
        response = RedirectResponse("/", status_code=303, headers=BASE_HEADERS)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/",
                            secure=request.url.scheme == "https" or settings.base_url.startswith("https://"))
        return response

    routes = [
        Route("/", index, methods=["GET", "HEAD"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/api/tools/{name}", tools, methods=["POST"]),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/guide/{name}", guide, methods=["GET"]),
        Route("/files/{deal_id}/{path:path}", files, methods=["GET", "HEAD"]),
    ]
    return BrowserGate(Starlette(routes=routes), settings)


def serve(settings: HttpSettings) -> None:  # pragma: no cover - opens a socket
    import uvicorn

    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="info")


__all__ = ["DEFAULT_PORT", "LaunchError", "build_app", "serve", "serve_settings"]
