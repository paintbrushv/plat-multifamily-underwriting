"""Streamable HTTP transport for the MCP workflow server (``plat-uw mcp --http``).

Loopback by default; a non-loopback bind needs ``--allow-remote`` and a
bearer token (``PLAT_UW_TOKEN``), checked on every request, which also turns
off ``path`` inputs. Workspace files are served under ``/files/<deal_id>/...``
behind the same check. Synthetic data only; no socket is opened (Starlette's
TestClient drives the ASGI app in-process).
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from engine import cli, mcp_http, mcp_server
from engine.workflow import config as wf_config
from engine.workflow import service

TOKEN = "synthetic-test-token-0123456789"
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {},
               "clientInfo": {"name": "synthetic-test", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv(wf_config.HOME_ENV, str(tmp_path / "workspace"))
    monkeypatch.delenv(mcp_http.TOKEN_ENV, raising=False)
    yield tmp_path / "workspace"
    service.configure(transport="stdio", remote=False, base_url=None)


def _client(settings: mcp_http.HttpSettings) -> TestClient:
    app = mcp_http.build_http_app(settings)
    return TestClient(app, base_url=f"http://{settings.host}:{settings.port}")


# ── launch settings ───────────────────────────────────────────────────────


def test_loopback_is_the_default_and_needs_no_token(workspace):
    settings = mcp_http.http_settings(env={})
    assert settings.host == "127.0.0.1" and settings.port == 8765
    assert settings.remote is False and settings.token is None
    assert settings.base_url == "http://127.0.0.1:8765"


def test_a_non_loopback_bind_needs_allow_remote_and_a_token():
    with pytest.raises(mcp_http.LaunchError, match="--allow-remote"):
        mcp_http.http_settings(host="0.0.0.0", env={})
    with pytest.raises(mcp_http.LaunchError, match=mcp_http.TOKEN_ENV):
        mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={})
    with pytest.raises(mcp_http.LaunchError, match="at least"):
        mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: "short"})
    settings = mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: TOKEN})
    assert settings.remote is True and settings.token == TOKEN


def test_cli_refuses_a_remote_bind_without_the_flag(capsys):
    code = cli.main(["mcp", "--http", "--host", "0.0.0.0"])
    assert code == 2
    assert "--allow-remote" in capsys.readouterr().err


def test_cli_parser_defaults_to_stdio():
    args = cli.build_parser().parse_args(["mcp"])
    assert args.http is False
    args = cli.build_parser().parse_args(["mcp", "--http", "--port", "9000"])
    assert args.http is True and args.port == 9000 and args.host == "127.0.0.1"


# ── bearer token on every request ─────────────────────────────────────────


def test_remote_mode_checks_the_bearer_token_on_every_request(workspace):
    settings = mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: TOKEN})
    with _client(settings) as client:
        missing = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"].startswith("Bearer")
        assert "PLAT_UW_TOKEN" not in missing.text and TOKEN not in missing.text
        wrong = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        basic = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Basic {TOKEN}"})
        assert basic.status_code == 401
        ok = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert '"protocolVersion"' in ok.text
        assert client.get("/files/x-abc123/deal.json").status_code == 401


def test_remote_mode_turns_off_path_inputs(workspace, tmp_path):
    settings = mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: TOKEN})
    mcp_http.build_http_app(settings)  # building the app configures the server's mode
    deal_id = mcp_server.create_deal("Synthetic Remote", "stabilized")["deal_id"]
    source = tmp_path / "t12.csv"
    source.write_text("Account Name,Total\n", encoding="utf-8")
    response = mcp_server.add_document(deal_id, "t12", "t12.csv", path=str(source))
    assert response["error"]["code"] == "PATH_NOT_ALLOWED"


def test_a_token_set_for_local_http_is_enforced_too(workspace):
    settings = mcp_http.http_settings(env={mcp_http.TOKEN_ENV: TOKEN})
    assert settings.remote is False and settings.token == TOKEN
    with _client(settings) as client:
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401


def test_the_authorize_seam_is_one_function():
    settings = mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: TOKEN})
    assert mcp_http.authorize({"authorization": f"Bearer {TOKEN}"}, settings).allowed is True
    denied = mcp_http.authorize({}, settings)
    assert denied.allowed is False and denied.status == 401


# ── files and export links ────────────────────────────────────────────────


def test_files_are_served_with_the_same_auth_and_never_outside_the_deal(workspace):
    settings = mcp_http.http_settings(host="0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: TOKEN})
    with _client(settings) as client:
        deal_id = mcp_server.create_deal("Synthetic Files", "stabilized")["deal_id"]
        auth = {"Authorization": f"Bearer {TOKEN}"}
        manifest = client.get(f"/files/{deal_id}/deal.json", headers=auth)
        assert manifest.status_code == 200
        assert json.loads(manifest.text)["deal_id"] == deal_id
        for bad in (f"/files/{deal_id}/..%2Fother/deal.json", f"/files/{deal_id}/%2E%2E/%2E%2E/x",
                    "/files/..%2F..%2Fetc/passwd", f"/files/{deal_id}/missing.json"):
            assert client.get(bad, headers=auth).status_code == 404, bad
        exported = mcp_server.export_deal(deal_id)
        (entry,) = [f for f in exported["files"] if f["path"] == "deal.json"]
        assert entry["url"] == f"http://0.0.0.0:8765/files/{deal_id}/deal.json"
        assert "local_path" not in entry


def test_local_http_export_has_both_paths_and_urls(workspace):
    settings = mcp_http.http_settings(env={})
    with _client(settings) as client:
        deal_id = mcp_server.create_deal("Synthetic Local", "stabilized")["deal_id"]
        exported = mcp_server.export_deal(deal_id)
        (entry,) = [f for f in exported["files"] if f["path"] == "deal.json"]
        assert entry["url"] == f"http://127.0.0.1:8765/files/{deal_id}/deal.json"
        assert entry["local_path"].endswith("deal.json")
        assert client.get(f"/files/{deal_id}/deal.json").status_code == 200
        # DNS-rebinding guard: a loopback server without a token only answers loopback Host headers.
        assert client.get(f"/files/{deal_id}/deal.json", headers={"Host": "evil.example"}).status_code in (403, 421)


def test_public_url_overrides_the_link_base(workspace):
    settings = mcp_http.http_settings(env={mcp_http.PUBLIC_URL_ENV: "https://uw.example.invalid/"})
    assert settings.base_url == "https://uw.example.invalid"
