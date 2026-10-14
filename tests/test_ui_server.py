"""``plat-uw serve``: the analyst screens as a browser app (engine/ui/server.py).

The bridge stands in for the MCP host: ``POST /api/tools/<name>`` runs the same workflow function
and returns the same envelope. Checked here end to end on the bundled synthetic sample (load ->
ingest -> review -> edit -> run -> solve_price -> export) in a temporary workspace, plus the
multipart upload, and the security posture shared with ``plat-uw mcp --http``: loopback Host
check, bearer token behind ``/login`` in remote mode (one ``authorize`` seam), CSRF header and
same-origin checks on every POST, upload caps, and no file served from outside a deal folder.
Synthetic data only; Starlette's TestClient drives the ASGI app in-process (no socket).
"""

from __future__ import annotations

import base64
import json

import pytest
from starlette.testclient import TestClient

from engine import cli, mcp_http
from engine.ui import server
from engine.workflow import config as wf_config
from engine.workflow import service

TOKEN = "synthetic-test-token-0123456789"
CSRF = {"X-Plat-UW": "1"}
PCA = {"immediate_capex": 25000, "recurring_capex_per_unit": 300, "as_of": "2026-08-15", "reference": "Synthetic PCA"}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    for name in ("PLAT_MARKET_STUDY_ROOT", "PLAT_SUPPLY_DEMAND_ROOT"):
        monkeypatch.setenv(name, str(tmp_path / "no-market-data" / name.lower()))
    for name in ("PLAT_MARKET_DATA_PROVIDERS", "PLAT_MARKET_DATA_USER_FILE", "PLAT_MARKET_STUDY_COMPS",
                 "PLAT_UW_MAX_UPLOAD_MB", "PLAT_UW_ALLOWED_EXTENSIONS", mcp_http.TOKEN_ENV, mcp_http.PUBLIC_URL_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLAT_UNDERWRITING_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv(wf_config.HOME_ENV, str(tmp_path / "workspace"))
    yield tmp_path / "workspace"
    service.configure(transport="stdio", remote=False, base_url=None)


def _client(settings=None) -> TestClient:
    settings = settings or server.serve_settings(env={})
    return TestClient(server.build_app(settings), base_url=f"http://127.0.0.1:{settings.port}")


def _tool(client: TestClient, tool: str, /, **args) -> dict:
    response = client.post(f"/api/tools/{tool}", json=args, headers=CSRF)
    assert response.status_code == 200, response.text
    return response.json()


# ── the app and the bridge ────────────────────────────────────────────────


def test_settings_default_to_loopback_on_8766():
    settings = server.serve_settings(env={})
    assert (settings.host, settings.port, settings.remote) == ("127.0.0.1", 8766, False)
    args = cli.build_parser().parse_args(["serve"])
    assert (args.host, args.port, args.allow_remote) == ("127.0.0.1", 8766, False)


def test_root_serves_the_single_page_app_with_a_strict_csp(workspace):
    response = _client().get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="deal-list"' in response.text and "Try the sample deal" in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_sample_runs_end_to_end_through_the_bridge(workspace):
    client = _client()
    assert _tool(client, "list_deals")["deals"] == []
    loaded = _tool(client, "load_sample_deal")
    deal_id = loaded["deal_id"]
    assert loaded["ok"] and loaded["pricing_defaults"]["benchmark_5yr_treasury"]

    deal = _tool(client, "get_deal", deal_id=deal_id)
    assert deal["pricing_defaults"]["benchmark_5yr_treasury"] == loaded["pricing_defaults"]["benchmark_5yr_treasury"]

    ingest = _tool(client, "ingest_deal", deal_id=deal_id)
    assert ingest["ok"] and ingest["gate"]["status"] == "PASS"
    review = _tool(client, "review_assumptions", deal_id=deal_id)
    assert review["ok"] and len(review["rows"]) == ingest["assumptions"]["total"]

    edit = _tool(client, "edit_assumptions", deal_id=deal_id, editor="Sample Analyst",
                 edits={"r_and_m_per_unit": {"value": 900, "note": "bring R&M to the 1980s floor"}})
    assert edit["ok"] and edit["changed"][0]["after"]["value"] == 900
    assert edit["edits"]["r_and_m_per_unit"]["edited_by"] == "Sample Analyst"

    run = _tool(client, "run_deal", deal_id=deal_id)
    assert run["ok"]
    annual = run["annual_cash_flow"]
    assert [row["analysis_year"] for row in annual] == [1, 2, 3, 4, 5]
    assert annual[0]["start"] == "2026-11" and annual[0]["months"] == 12
    assert annual[0]["net_operating_income"] == pytest.approx(run["metrics"]["noi_year_1"], abs=0.05)
    window = run["metrics"]["exit"]["noi_window"]
    assert (window["start"], window["end"], window["basis"]) == ("2031-11", "2032-10", "forward")

    price = _tool(client, "solve_price", deal_id=deal_id)
    assert price["ok"] and price["pricing"]["status"] == "converged"
    assert price["prefilled_from_deal"]  # the sample's illustrative benchmark, never an invented one

    exported = _tool(client, "export_deal", deal_id=deal_id)
    paths = {f["path"] for f in exported["files"]}
    assert {"run/underwriting.xlsx", "price/backsolve_summary.json"} <= paths
    url = next(f["url"] for f in exported["files"] if f["path"] == "run/underwriting.xlsx")
    assert url == f"http://127.0.0.1:8766/files/{deal_id}/run/underwriting.xlsx"
    workbook = client.get(f"/files/{deal_id}/run/underwriting.xlsx")
    assert workbook.status_code == 200 and workbook.headers["content-disposition"].startswith("attachment")

    listed = _tool(client, "list_deals")["deals"]
    assert [d["deal_id"] for d in listed] == [deal_id]


def test_refusals_come_back_as_the_same_envelope(workspace):
    client = _client()
    missing = _tool(client, "get_deal", deal_id="no-such-deal-000000")
    assert missing["ok"] is False and missing["error"]["code"] == "DEAL_NOT_FOUND" and missing["error"]["fix"]
    bad = _tool(client, "get_deal", deal_id="x", surprise=1)
    assert bad["error"]["code"] == "BAD_ARGUMENTS"
    lacking = _tool(client, "get_deal")
    assert lacking["error"]["code"] == "BAD_ARGUMENTS"
    unknown = client.post("/api/tools/backsolve_deal_price", json={}, headers=CSRF)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "UNKNOWN_TOOL"
    not_json = client.post("/api/tools/list_deals", content=b"[1]", headers={**CSRF, "Content-Type": "application/json"})
    assert not_json.status_code == 400
    form = client.post("/api/tools/list_deals", data={"a": "b"}, headers=CSRF)
    assert form.status_code == 415


def test_the_browser_never_reads_server_paths(workspace, tmp_path):
    client = _client()
    deal_id = _tool(client, "create_deal", name="Synthetic Path Check")["deal_id"]
    secret = tmp_path / "t12.csv"
    secret.write_text("a,b\n1,2\n")
    refused = _tool(client, "add_document", deal_id=deal_id, kind="t12", filename="t12.csv", path=str(secret))
    assert refused["error"]["code"] == "PATH_NOT_ALLOWED"


# ── uploads ───────────────────────────────────────────────────────────────


def test_multipart_upload_goes_through_add_document(workspace):
    client = _client()
    deal_id = _tool(client, "create_deal", name="Synthetic Upload")["deal_id"]
    response = client.post("/api/upload", headers=CSRF, data={"deal_id": deal_id, "kind": "pca"},
                           files={"file": ("pca.json", json.dumps(PCA).encode(), "application/json")})
    body = response.json()
    assert response.status_code == 200 and body["ok"], body
    assert body["document"]["stored_as"] == "documents/pca.json"
    stored = workspace / "deals" / deal_id / "documents" / "pca.json"
    assert json.loads(stored.read_text()) == PCA

    bad_ext = client.post("/api/upload", headers=CSRF, data={"deal_id": deal_id, "kind": "t12"},
                          files={"file": ("t12.exe", b"MZ", "application/octet-stream")})
    assert bad_ext.json()["error"]["code"] == "BAD_EXTENSION"
    missing = client.post("/api/upload", headers=CSRF, data={"deal_id": deal_id},
                          files={"file": ("pca.json", b"{}", "application/json")})
    assert missing.status_code == 400


def test_uploads_over_the_cap_are_refused(workspace, monkeypatch):
    monkeypatch.setenv("PLAT_UW_MAX_UPLOAD_MB", "0.01")  # about 10 KB
    client = _client()
    deal_id = _tool(client, "create_deal", name="Synthetic Cap")["deal_id"]
    big = b"x" * 200_000
    response = client.post("/api/upload", headers=CSRF, data={"deal_id": deal_id, "kind": "t12"},
                           files={"file": ("t12.csv", big, "text/csv")})
    assert response.status_code == 413 and response.json()["error"]["code"] == "TOO_LARGE"
    as_json = client.post("/api/tools/add_document", headers=CSRF, json={
        "deal_id": deal_id, "kind": "t12", "filename": "t12.csv", "content_base64": base64.b64encode(big).decode()})
    assert as_json.status_code == 413
    assert not (workspace / "deals" / deal_id / "documents" / "t12.csv").exists()


# ── CSRF and Host ─────────────────────────────────────────────────────────


def test_posts_need_the_app_header_and_a_same_origin(workspace):
    client = _client()
    assert client.post("/api/tools/list_deals", json={}).status_code == 403
    assert client.post("/api/tools/list_deals", json={}, headers={"X-Plat-UW": "0"}).status_code == 403
    cross = client.post("/api/tools/list_deals", json={}, headers={**CSRF, "Origin": "http://evil.example"})
    assert cross.status_code == 403 and cross.json()["error"]["code"] == "CSRF_REJECTED"
    site = client.post("/api/tools/list_deals", json={}, headers={**CSRF, "Sec-Fetch-Site": "cross-site"})
    assert site.status_code == 403
    same = client.post("/api/tools/list_deals", json={}, headers={**CSRF, "Origin": "http://127.0.0.1:8766",
                                                                   "Sec-Fetch-Site": "same-origin"})
    assert same.status_code == 200
    upload = client.post("/api/upload", data={"deal_id": "x", "kind": "t12"}, files={"file": ("t.csv", b"a", "text/csv")})
    assert upload.status_code == 403


def test_a_local_server_only_answers_loopback_hosts(workspace):
    client = _client()
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403
    assert client.post("/api/tools/list_deals", json={}, headers={**CSRF, "Host": "attacker.example:8766"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:8766"}).status_code == 200


# ── remote mode: the token, through /login ────────────────────────────────


def test_remote_mode_needs_the_token_and_signs_in_with_a_cookie(workspace):
    with pytest.raises(mcp_http.LaunchError):
        server.serve_settings("0.0.0.0", allow_remote=True, env={})
    settings = server.serve_settings("0.0.0.0", allow_remote=True, env={mcp_http.TOKEN_ENV: TOKEN})
    client = TestClient(server.build_app(settings), base_url="http://192.0.2.10:8766")

    first = client.get("/", follow_redirects=False)
    assert first.status_code == 303 and first.headers["location"] == "/login"
    assert client.post("/api/tools/list_deals", json={}, headers=CSRF).status_code == 401
    page = client.get("/login")
    assert page.status_code == 200 and 'name="token"' in page.text

    wrong = client.post("/login", data={"token": "not-the-token-at-all"}, follow_redirects=False)
    assert wrong.status_code == 401
    cross = client.post("/login", data={"token": TOKEN}, headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert cross.status_code == 403
    signed = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert signed.status_code == 303
    cookie = signed.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    assert client.get("/").status_code == 200
    listed = client.post("/api/tools/list_deals", json={}, headers=CSRF)
    assert listed.status_code == 200 and listed.json()["ok"]
    assert client.post("/api/tools/list_deals", json={}).status_code == 403  # still needs the CSRF header
    bearer = TestClient(server.build_app(settings), base_url="http://192.0.2.10:8766")
    ok = bearer.post("/api/tools/start_here", json={}, headers={**CSRF, "Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert "deals_dir" not in ok.json()["workspace"]  # remote mode: no server paths in responses


def test_login_is_not_used_without_a_token(workspace):
    response = _client().get("/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


# ── files ─────────────────────────────────────────────────────────────────


def test_files_never_leave_the_deal_folder(workspace):
    client = _client()
    deal_id = _tool(client, "create_deal", name="Synthetic Files")["deal_id"]
    (workspace / "secret.txt").write_text("outside")
    for path in (f"/files/{deal_id}/../secret.txt", f"/files/{deal_id}/..%2F..%2Fsecret.txt",
                 f"/files/{deal_id}/%2Fetc%2Fpasswd", "/files/..%2Fsecret.txt/x", f"/files/{deal_id}/"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "outside" not in response.text
    assert client.get(f"/files/{deal_id}/deal.json").status_code == 200
    assert client.get("/files/no-such-deal-000000/deal.json").status_code == 404


def test_html_files_are_shown_sandboxed(workspace):
    client = _client()
    deal_id = _tool(client, "load_sample_deal")["deal_id"]
    _tool(client, "ingest_deal", deal_id=deal_id)
    page = client.get(f"/files/{deal_id}/assumptions.html")
    assert page.status_code == 200 and page.headers["content-disposition"].startswith("inline")
    assert page.headers["content-security-policy"].startswith("sandbox allow-scripts")


# ── guides ────────────────────────────────────────────────────────────────


def test_guides_render_as_escaped_html(workspace):
    client = _client()
    guide = client.get("/api/guide/user-guide").json()
    assert guide["ok"] and guide["title"] == "User guide"
    assert '<h1 id="user-guide-new-start-here">' in guide["html"] and "<script" not in guide["html"]
    assert 'href="#/guide/glossary"' in guide["html"]  # links to the glossary stay inside the app
    assert client.get("/api/guide/glossary").json()["ok"]
    assert client.get("/api/guide/../../etc/passwd").status_code == 404
    assert client.get("/api/guide/secrets").status_code == 404
