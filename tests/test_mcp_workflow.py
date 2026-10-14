"""The MCP workflow server: a deal workspace an AI assistant drives end to end.

Covers the tool contract in docs/MCP_TOOLS.md through the tool functions in
``engine.mcp_server`` (and one smoke pass through the in-memory MCP client):
create -> add documents (base64) -> describe -> ingest -> review -> edit ->
run -> solve_price -> export, the workspace's path-traversal and upload
guards, remote mode refusing ``path`` inputs, tool annotations, prompts and
resources, and the original stateless tools.

Synthetic data only: library deal d (tests/fixtures/deals) stands in for the
bundled sample deal. Runs offline with an empty market-data configuration.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import anyio
import pytest

from engine import mcp_server
from engine.workflow import config as wf_config
from engine.workflow import service
from engine.workflow.workspace import DEAL_ID_RE, Workspace, WorkspaceError
from tests import yardi_fixtures

REPO = Path(__file__).resolve().parents[1]
LIBRARY_DEAL = REPO / "tests" / "fixtures" / "deals" / "d_value_add_1980s_msa_only"
BENCHMARK = {"benchmark_5yr_treasury": "0.04", "benchmark_as_of": "2026-10-01",
             "benchmark_source": "synthetic:test"}

# Library deal d as an analyst would describe it (all invented).
PROFILE = {
    "year_built": 1986,
    "msa": "Synthetic Test MSA",
    "metro_slug": "synthetic_test_metro",
    "submarket": "Synthetic South",
    "purchase_price": 11380000,
    "ltv": 0.62,
    "rate": 0.06,
    "amort": 30,
    "io_months": 24,
    "start": "2026-11",
    "end": "2031-10",
    "exit_month": "2031-10",
    "sale_cost": 0.02,
    "pca_immediate_capex": 180000,
    "pca_recurring_capex_per_unit": 1500,
    "pca_as_of": "2026-08-15",
    "pca_reference": "Synthetic PCA",
}
TAX = {"millage": 17.0, "assessment_ratio": 0.75}

READ_ONLY_TOOLS = {"start_here", "list_deals", "get_deal", "review_assumptions", "export_deal"}
WRITE_TOOLS = {"load_sample_deal", "create_deal", "add_document", "describe_deal", "ingest_deal",
               "edit_assumptions", "run_deal", "solve_price"}
LEGACY_TOOLS = {"backsolve_deal_price", "validate_deal_inputs", "run_deal_summary",
                "assumption_register", "check_deal_feasibility"}


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _hermetic(mp: pytest.MonkeyPatch, base: Path) -> Path:
    """An empty market-data setup and a fresh workspace under ``base``."""
    for name in ("PLAT_MARKET_STUDY_ROOT", "PLAT_SUPPLY_DEMAND_ROOT"):
        mp.setenv(name, str(base / "no-market-data" / name.lower()))
    mp.setenv("PLAT_UNDERWRITING_CONFIG", str(base / "no-config.toml"))
    for name in ("PLAT_MARKET_DATA_PROVIDERS", "PLAT_MARKET_DATA_USER_FILE", "PLAT_MARKET_STUDY_COMPS",
                 "PLAT_UW_MAX_UPLOAD_MB", "PLAT_UW_ALLOWED_EXTENSIONS", "PLAT_UW_TOKEN"):
        mp.delenv(name, raising=False)
    home = base / "workspace"
    mp.setenv(wf_config.HOME_ENV, str(home))
    return home


@pytest.fixture
def workspace(monkeypatch, tmp_path) -> Path:
    home = _hermetic(monkeypatch, tmp_path)
    service.configure(transport="stdio", remote=False, base_url=None)
    yield home
    service.configure(transport="stdio", remote=False, base_url=None)


@pytest.fixture
def sample_dir(monkeypatch, tmp_path) -> Path:
    """A stand-in for engine/samples/sample_deal built from library deal d."""
    folder = tmp_path / "sample_deal"
    folder.mkdir()
    shutil.copy(LIBRARY_DEAL / "t12.csv", folder / "t12.csv")
    shutil.copy(LIBRARY_DEAL / "rent_roll.csv", folder / "rent_roll.csv")
    profile = {"name": "Synthetic sample deal", "deal_type": "value_add", **PROFILE, **TAX,
               "not_a_field": "ignored"}
    (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(service, "SAMPLE_DEAL_DIR", folder)
    return folder


# ── the whole flow, once ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    """Run create -> ... -> export once through the tool functions; keep every response."""
    base = tmp_path_factory.mktemp("mcp_flow")
    out: dict = {}
    with pytest.MonkeyPatch.context() as mp:
        out["home"] = _hermetic(mp, base)
        service.configure(transport="stdio", remote=False, base_url=None)
        out["start"] = mcp_server.start_here()
        created = mcp_server.create_deal("Synthetic Test Deal", "value_add")
        out["create"] = created
        deal_id = created["deal_id"]
        out["deal_id"] = deal_id
        out["add_t12"] = mcp_server.add_document(deal_id, "t12", "t12.csv",
                                                 content_base64=_b64(LIBRARY_DEAL / "t12.csv"))
        out["add_rr"] = mcp_server.add_document(deal_id, "rent_roll", "Rent Roll.csv",
                                                content_base64=_b64(LIBRARY_DEAL / "rent_roll.csv"))
        out["ingest_early"] = mcp_server.ingest_deal(deal_id)  # no analysis period yet
        out["describe"] = mcp_server.describe_deal(deal_id, **PROFILE)
        out["describe_tax"] = mcp_server.describe_deal(deal_id, **TAX)  # a partial update
        out["ingest"] = mcp_server.ingest_deal(deal_id)
        out["review_all"] = mcp_server.review_assumptions(deal_id)
        out["review_needs"] = mcp_server.review_assumptions(deal_id, filter="needs_review")
        out["review_defaults"] = mcp_server.review_assumptions(deal_id, filter="defaults")
        out["edit"] = mcp_server.edit_assumptions(
            deal_id, {"collection_loss": {"value": 0.012, "note": "IC view"}}, editor="Analyst A")
        out["review_edited"] = mcp_server.review_assumptions(deal_id, filter="edited")
        out["reingest"] = mcp_server.ingest_deal(deal_id)
        out["review_after_reingest"] = mcp_server.review_assumptions(deal_id, filter="edited")
        out["run"] = mcp_server.run_deal(deal_id)
        out["price_no_benchmark"] = mcp_server.solve_price(deal_id, target_coc="0.03")
        out["price"] = mcp_server.solve_price(deal_id, target_coc="0.03", max_price="20000000", **BENCHMARK)
        out["review_enforced"] = mcp_server.review_assumptions(deal_id, filter="enforced", case="priced")
        out["export"] = mcp_server.export_deal(deal_id)
        out["get"] = mcp_server.get_deal(deal_id)
        out["list"] = mcp_server.list_deals()
    return out


def _assert_envelope(response: dict) -> None:
    assert isinstance(response.get("summary"), str) and response["summary"]
    assert isinstance(response.get("next_step"), str) and response["next_step"]
    json.dumps(response)  # JSON-serializable


def test_every_response_narrates_with_a_summary_and_next_step(flow):
    for name, response in flow.items():
        if isinstance(response, dict) and name not in ("home",):
            _assert_envelope(response)


def test_start_here_explains_the_eight_steps_and_the_sample(flow):
    start = flow["start"]
    assert start["ok"] is True
    assert [step["tool"] for step in start["steps"]] == [
        "create_deal", "add_document", "describe_deal", "ingest_deal",
        "review_assumptions", "edit_assumptions", "run_deal", "solve_price",
    ]
    assert "load_sample_deal" in start["try_the_sample"]
    assert {"guide://start-here", "guide://user-guide", "guide://glossary"} <= set(start["resources"])


def test_create_deal_returns_a_safe_slug_id_and_a_workspace_outside_the_repo(flow):
    created = flow["create"]
    assert created["ok"] is True
    assert DEAL_ID_RE.fullmatch(created["deal_id"])
    assert created["deal_id"].startswith("synthetic-test-deal-")
    deal_dir = Path(created["workspace"])
    assert deal_dir.parent == flow["home"] / "deals"
    assert (deal_dir / "deal.json").is_file()
    assert REPO not in deal_dir.parents


def test_documents_are_stored_as_copies_in_the_workspace(flow):
    added = flow["add_rr"]
    assert added["ok"] is True
    doc = added["document"]
    assert doc["kind"] == "rent_roll" and doc["original_filename"] == "Rent Roll.csv"
    stored = Path(flow["create"]["workspace"]) / doc["stored_as"]
    assert stored.read_bytes() == (LIBRARY_DEAL / "rent_roll.csv").read_bytes()
    assert doc["bytes"] == stored.stat().st_size


def test_ingest_asks_for_the_analysis_period_before_running(flow):
    early = flow["ingest_early"]
    assert early["ok"] is False
    assert early["error"]["code"] == "PROFILE_INCOMPLETE"
    assert "start" in early["error"]["fix"] and "describe_deal" in early["error"]["fix"]


def test_describe_deal_merges_partial_updates(flow):
    assert flow["describe"]["ok"] is True
    profile = flow["describe_tax"]["profile"]
    assert profile["year_built"] == 1986 and profile["millage"] == 17.0
    assert profile["purchase_price"] == 11380000
    assert set(flow["describe_tax"]["updated"]) == {"millage", "assessment_ratio"}


def test_ingest_reports_gates_capital_plan_market_data_and_review_count(flow):
    ingest = flow["ingest"]
    assert ingest["ok"] is True, ingest
    assert ingest["gate"]["status"] == "PASS"
    assert ingest["gate"]["blockers"] == []
    assert ingest["assumptions"]["needing_review"] >= 1
    assert ingest["assumptions"]["total"] > ingest["assumptions"]["needing_review"]
    buckets = {b["key"]: b for b in ingest["capital_plan"]["buckets"]}
    assert buckets["immediate_capex"]["value"] == 180000
    assert buckets["immediate_capex"]["source"] == "deal:pca"
    assert "Market data" in ingest["market_data"]["summary"]
    assert ingest["validation"]["status"] == "PASS"
    deal_dir = Path(flow["create"]["workspace"])
    for name in ("canonical.json", "assumptions.json", "assumptions.html"):
        assert (deal_dir / name).is_file(), name


def test_review_filters_rows(flow):
    assert flow["review_all"]["ok"] is True
    total = flow["review_all"]["summary_counts"]["total"]
    assert len(flow["review_all"]["rows"]) == total
    needs = flow["review_needs"]["rows"]
    assert needs and all(row["review_required"] for row in needs)
    defaults = flow["review_defaults"]["rows"]
    assert defaults and all(row["source_family"] in ("house", "engine_fallback") for row in defaults)
    row = flow["review_all"]["rows"][0]
    assert {"key", "label", "value", "default", "source", "why", "user_edit", "flags"} <= set(row)


def test_edit_assumptions_stamps_the_editor_and_reports_the_delta(flow):
    edit = flow["edit"]
    assert edit["ok"] is True, edit
    (change,) = edit["changed"]
    assert change["key"] == "collection_loss"
    assert change["after"]["value"] == pytest.approx(0.012)
    assert change["after"]["user_edit"]["edited_by"] == "Analyst A"
    assert change["after"]["user_edit"]["edited_at"] == datetime.now(timezone.utc).date().isoformat()
    assert change["after"]["user_edit"]["note"] == "IC view"
    assert change["before"]["value"] != change["after"]["value"]
    edited = flow["review_edited"]["rows"]
    assert [row["key"] for row in edited] == ["collection_loss"]


def test_edits_survive_a_re_ingest(flow):
    assert flow["reingest"]["ok"] is True
    rows = flow["review_after_reingest"]["rows"]
    assert [row["key"] for row in rows] == ["collection_loss"]
    assert rows[0]["value"] == pytest.approx(0.012)
    assert rows[0]["user_edit"]["edited_by"] == "Analyst A"


def test_run_deal_returns_headline_metrics_and_writes_the_outputs(flow):
    run = flow["run"]
    assert run["ok"] is True, run
    metrics = run["metrics"]
    for key in ("noi_year_1", "cash_on_cash_year_1", "levered_irr", "unlevered_irr",
                "levered_equity_multiple", "going_in_cap_rate"):
        assert isinstance(metrics[key], (int, float)), key
    assert metrics["dscr"]["basis"] == "ncf_after_capex_charge"
    assert isinstance(metrics["dscr"]["minimum"], (int, float))
    assert isinstance(metrics["dscr"]["minimum_noi_before_capex"], (int, float))
    assert metrics["exit"]["cap_rate"] > 0 and metrics["exit"]["value"] > 0
    assert run["capital_plan"]["immediate_capex"]["amount"] == 180000
    assert run["exit_cap_grid"]["points"]
    run_dir = Path(flow["create"]["workspace"]) / "run"
    for name in ("results.json", "underwriting.xlsx", "assumptions.html", "canonical.json", "validation.json"):
        assert (run_dir / name).is_file(), name


def test_solve_price_asks_for_the_benchmark_instead_of_inventing_one(flow):
    missing = flow["price_no_benchmark"]
    assert missing["ok"] is False
    assert missing["error"]["code"] == "BENCHMARK_REQUIRED"
    assert "5-year" in missing["error"]["fix"]


def test_solve_price_returns_the_pricing_summary_and_writes_the_page(flow):
    price = flow["price"]
    assert price["ok"] is True, price
    pricing = price["pricing"]
    assert pricing["status"] in ("converged", "ceiling_feasible")
    assert pricing["price"] > 0
    assert pricing["price_per_unit"] == pytest.approx(pricing["price"] / 90, rel=1e-6)
    assert pricing["loan"]["amount"] > 0 and pricing["loan"]["sizing_basis"]
    assert pricing["achieved_coc"] >= 0.03 - 1e-9
    for key in ("going_in_cap_rate", "exit_cap_rate", "levered_irr", "unlevered_irr"):
        assert key in pricing
    assert isinstance(pricing["floors_enforced"], list)
    assert pricing["exit_cap_grid"]["points"]
    price_dir = Path(flow["create"]["workspace"]) / "price"
    for name in ("assumptions.html", "backsolve_summary.json"):
        assert (price_dir / name).is_file(), name


def test_review_of_the_priced_case_lists_enforced_floors(flow):
    enforced = flow["review_enforced"]
    assert enforced["ok"] is True
    names = {row["key"] for row in enforced["rows"]}
    assert names == set(flow["price"]["pricing"]["floors_enforced"])


def test_export_lists_files_with_local_paths_in_local_mode(flow):
    export = flow["export"]
    assert export["ok"] is True
    files = {f["path"]: f for f in export["files"]}
    for rel in ("deal.json", "canonical.json", "run/results.json", "run/underwriting.xlsx",
                "run/assumptions.html", "price/assumptions.html"):
        assert rel in files, rel
        assert Path(files[rel]["local_path"]).is_file()
        assert "url" not in files[rel]


def test_get_and_list_deals_show_step_status(flow):
    deal = flow["get"]
    assert deal["ok"] is True
    steps = {step["step"]: step for step in deal["steps"]}
    for name in ("create", "documents", "describe", "ingest", "run", "price"):
        assert steps[name]["done"] is True, name
    assert steps["run"]["stale"] is False
    assert deal["last_run"]["levered_irr"] == flow["run"]["metrics"]["levered_irr"]
    assert deal["last_price"]["price"] == flow["price"]["pricing"]["price"]
    assert set(deal["documents"]) == {"t12", "rent_roll"}
    listed = {d["deal_id"]: d for d in flow["list"]["deals"]}
    assert flow["deal_id"] in listed


# ── workspace guards ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "", "UPPER", "x" * 80, "-lead", "trail-", "a\x00b"])
def test_deal_ids_with_traversal_or_odd_characters_are_refused(workspace, bad):
    response = mcp_server.get_deal(bad)
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_DEAL_ID"


def test_unknown_deal_is_an_actionable_error(workspace):
    response = mcp_server.get_deal("nope-abc123")
    assert response["error"]["code"] == "DEAL_NOT_FOUND"
    assert "list_deals" in response["error"]["fix"]


def test_workspace_files_cannot_escape_the_deal_folder(workspace):
    deal_id = mcp_server.create_deal("Synthetic Guard", "stabilized")["deal_id"]
    ws = Workspace(wf_config.load_config().deals_dir)
    for rel in ("../deal.json", "../../x", "/etc/passwd", "run/../../x", "a\x00b"):
        with pytest.raises(WorkspaceError):
            ws.resolve_file(deal_id, rel)
    assert ws.resolve_file(deal_id, "deal.json").is_file()


def test_uploaded_filenames_never_choose_the_stored_path(workspace):
    deal_id = mcp_server.create_deal("Synthetic Guard", "stabilized")["deal_id"]
    data = base64.b64encode(b"Account Name,Total\n").decode()
    added = mcp_server.add_document(deal_id, "t12", "../../../evil.csv", content_base64=data)
    assert added["ok"] is True
    stored = (Path(added["workspace"]) / added["document"]["stored_as"]).resolve()
    assert stored.parent == (Path(added["workspace"]) / "documents").resolve()
    assert not (workspace.parent / "evil.csv").exists()


def test_upload_extension_kind_and_size_are_checked(workspace, monkeypatch):
    deal_id = mcp_server.create_deal("Synthetic Guard", "stabilized")["deal_id"]
    data = base64.b64encode(b"x" * 2048).decode()
    assert mcp_server.add_document(deal_id, "t12", "t12.exe", content_base64=data)["error"]["code"] == "BAD_EXTENSION"
    assert mcp_server.add_document(deal_id, "lease", "a.csv", content_base64=data)["error"]["code"] == "BAD_KIND"
    assert mcp_server.add_document(deal_id, "t12", "t12.csv", content_base64="@@not base64@@")["error"]["code"] == "BAD_BASE64"
    neither = mcp_server.add_document(deal_id, "t12", "t12.csv")
    assert neither["error"]["code"] == "NO_CONTENT"
    monkeypatch.setenv("PLAT_UW_MAX_UPLOAD_MB", "0.001")  # about 1 KB
    too_big = mcp_server.add_document(deal_id, "t12", "t12.csv", content_base64=data)
    assert too_big["error"]["code"] == "TOO_LARGE"
    monkeypatch.setenv("PLAT_UW_ALLOWED_EXTENSIONS", json.dumps({"t12": [".xlsx"]}))
    monkeypatch.delenv("PLAT_UW_MAX_UPLOAD_MB")
    assert mcp_server.add_document(deal_id, "t12", "t12.csv", content_base64=data)["error"]["code"] == "BAD_EXTENSION"


def test_local_path_inputs_work_locally_and_are_refused_in_remote_mode(workspace, tmp_path):
    deal_id = mcp_server.create_deal("Synthetic Paths", "stabilized")["deal_id"]
    t12 = yardi_fixtures.write_yardi_t12(tmp_path / "Synthetic T12.xlsx")
    local = mcp_server.add_document(deal_id, "t12", "ignored-name.xlsx", path=str(t12))
    assert local["ok"] is True
    assert local["document"]["stored_as"] == "documents/t12.xlsx"
    assert local["document"]["original_filename"] == "Synthetic T12.xlsx"
    missing = mcp_server.add_document(deal_id, "t12", "t12.xlsx", path=str(tmp_path / "nope.xlsx"))
    assert missing["error"]["code"] == "FILE_NOT_FOUND"
    both = mcp_server.add_document(deal_id, "t12", "t12.xlsx", path=str(t12), content_base64="eA==")
    assert both["error"]["code"] == "AMBIGUOUS_CONTENT"

    service.configure(transport="http", remote=True, base_url="https://example.invalid")
    remote = mcp_server.add_document(deal_id, "t12", "t12.xlsx", path=str(t12))
    assert remote["ok"] is False
    assert remote["error"]["code"] == "PATH_NOT_ALLOWED"
    assert "content_base64" in remote["error"]["fix"]
    exported = mcp_server.export_deal(deal_id)
    assert all("local_path" not in f for f in exported["files"])  # no server paths leak remotely
    assert all(f["url"].startswith(f"https://example.invalid/files/{deal_id}/") for f in exported["files"])


def test_replacing_a_document_keeps_one_file_per_kind(workspace, tmp_path):
    deal_id = mcp_server.create_deal("Synthetic Replace", "stabilized")["deal_id"]
    mcp_server.add_document(deal_id, "rent_roll", "rr.csv", content_base64=_b64(LIBRARY_DEAL / "rent_roll.csv"))
    rr = yardi_fixtures.write_yardi_rent_roll(tmp_path / "rr.xlsx")
    replaced = mcp_server.add_document(deal_id, "rent_roll", "rr.xlsx", content_base64=_b64(rr))
    assert replaced["ok"] is True and replaced["replaced"] is True
    docs = sorted(p.name for p in (Path(replaced["workspace"]) / "documents").iterdir())
    assert docs == ["rent_roll.xlsx"]


def test_create_deal_rejects_an_unsupported_deal_type(workspace):
    response = mcp_server.create_deal("Synthetic", "lease_up")
    assert response["ok"] is False
    assert response["error"]["code"] == "BAD_DEAL_TYPE"
    assert "stabilized" in response["error"]["fix"]


def test_describe_deal_validates_fields(workspace):
    deal_id = mcp_server.create_deal("Synthetic Fields", "stabilized")["deal_id"]
    assert mcp_server.describe_deal(deal_id, ltv=1.5)["error"]["code"] == "BAD_FIELD"
    assert mcp_server.describe_deal(deal_id, start="May 2026")["error"]["code"] == "BAD_FIELD"
    assert mcp_server.describe_deal(deal_id, star_tier="9")["error"]["code"] == "BAD_FIELD"
    empty = mcp_server.describe_deal(deal_id)
    assert empty["ok"] is True and empty["updated"] == []
    assert "profile_fields" in empty


def test_edit_assumptions_refuses_unknown_keys_and_needs_an_ingested_deal(workspace):
    deal_id = mcp_server.create_deal("Synthetic Edit", "stabilized")["deal_id"]
    early = mcp_server.edit_assumptions(deal_id, {"rent_growth": 0.03}, editor="A")
    assert early["error"]["code"] == "NOT_INGESTED"
    assert "ingest_deal" in early["error"]["fix"]
    assert mcp_server.run_deal(deal_id)["error"]["code"] == "NOT_INGESTED"
    assert mcp_server.review_assumptions(deal_id)["error"]["code"] == "NOT_INGESTED"


def test_review_assumptions_rejects_an_unknown_filter(workspace):
    deal_id = mcp_server.create_deal("Synthetic Filter", "stabilized")["deal_id"]
    response = mcp_server.review_assumptions(deal_id, filter="nope")
    assert response["error"]["code"] == "BAD_FILTER"


def test_ingest_needs_both_core_documents(workspace):
    deal_id = mcp_server.create_deal("Synthetic Docs", "stabilized")["deal_id"]
    response = mcp_server.ingest_deal(deal_id)
    assert response["error"]["code"] == "DOCUMENTS_MISSING"
    assert "t12" in response["error"]["fix"] and "rent_roll" in response["error"]["fix"]


# ── the sample deal ───────────────────────────────────────────────────────


def test_load_sample_deal_creates_a_ready_to_ingest_deal(workspace, sample_dir):
    loaded = mcp_server.load_sample_deal()
    assert loaded["ok"] is True, loaded
    deal = mcp_server.get_deal(loaded["deal_id"])
    assert set(deal["documents"]) == {"t12", "rent_roll"}
    assert deal["deal_type"] == "value_add"
    assert deal["profile"]["millage"] == 17.0
    assert loaded["ignored_profile_fields"] == ["not_a_field"]
    assert "ingest_deal" in loaded["next_step"]


def test_load_sample_deal_without_the_bundle_is_a_clear_error(workspace, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "SAMPLE_DEAL_DIR", tmp_path / "missing")
    response = mcp_server.load_sample_deal()
    assert response["ok"] is False
    assert response["error"]["code"] == "SAMPLE_NOT_FOUND"


# ── configuration ─────────────────────────────────────────────────────────


def test_workspace_location_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv(wf_config.HOME_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PLAT_UNDERWRITING_CONFIG", str(tmp_path / "none.toml"))
    assert wf_config.load_config().deals_dir == tmp_path / "xdg" / "plat-underwriting" / "deals"

    config = tmp_path / "config.toml"
    config.write_text('[workspace]\ndir = "my-workspace"\nmax_upload_mb = 5\n', encoding="utf-8")
    monkeypatch.setenv("PLAT_UNDERWRITING_CONFIG", str(config))
    loaded = wf_config.load_config()
    assert loaded.deals_dir == (tmp_path / "my-workspace" / "deals").resolve()
    assert loaded.max_upload_bytes == 5 * 1024 * 1024

    monkeypatch.setenv(wf_config.HOME_ENV, str(tmp_path / "env-home"))
    assert wf_config.load_config().deals_dir == tmp_path / "env-home" / "deals"


# ── the MCP surface ───────────────────────────────────────────────────────


def _list_surface():
    async def go():
        from mcp import Client

        async with Client(mcp_server.mcp) as client:
            tools = (await client.list_tools()).tools
            prompts = (await client.list_prompts()).prompts
            resources = (await client.list_resources()).resources
            templates = (await client.list_resource_templates()).resource_templates
            return tools, prompts, resources, templates

    return anyio.run(go)


def test_tool_annotations_mark_read_only_and_write_tools():
    tools, _, _, _ = _list_surface()
    by_name = {tool.name: tool for tool in tools}
    assert READ_ONLY_TOOLS | WRITE_TOOLS | LEGACY_TOOLS <= set(by_name)
    for name in READ_ONLY_TOOLS | LEGACY_TOOLS:
        assert by_name[name].annotations.read_only_hint is True, name
    for name in WRITE_TOOLS:
        annotations = by_name[name].annotations
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is not None, name
        assert annotations.idempotent_hint is not None, name
    assert by_name["add_document"].annotations.destructive_hint is True  # replaces a document of that kind
    assert by_name["create_deal"].annotations.idempotent_hint is False


def test_describe_deal_schema_documents_every_field_and_why_it_matters():
    tools, _, _, _ = _list_surface()
    schema = next(t for t in tools if t.name == "describe_deal").input_schema
    properties = schema["properties"]
    for name in ("year_built", "msa", "metro_slug", "submarket", "purchase_price", "ltv", "rate", "amort",
                 "io_months", "term_months", "start", "end", "millage", "assessment_ratio",
                 "deferred_maintenance", "pca_immediate_capex", "pca_recurring_capex_per_unit",
                 "immediate_capex", "immediate_capex_funding", "star_tier", "asset_class",
                 "loan_sizing_noi_basis"):
        assert name in properties, name
        assert "Why it matters:" in properties[name].get("description", ""), name


def test_prompts_and_resources_are_listed():
    _, prompts, resources, templates = _list_surface()
    assert {"new_deal_walkthrough", "explain_assumption"} <= {p.name for p in prompts}
    assert {"guide://start-here", "guide://user-guide", "guide://glossary"} <= {str(r.uri) for r in resources}
    assert "deal://{deal_id}/assumptions.html" in {t.uri_template for t in templates}


def test_guides_fall_back_when_the_docs_are_not_there(monkeypatch, tmp_path):
    from engine.workflow import guides

    monkeypatch.setattr(guides, "DOCS_DIR", tmp_path / "no-docs")
    glossary = guides.glossary_markdown()
    assert "rent_growth" in glossary and "deal:t12" in glossary
    assert "start_here" in guides.user_guide_markdown()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ASSUMPTION_GLOSSARY.md").write_text("# Synthetic glossary\n", encoding="utf-8")
    (docs / "USER_GUIDE.md").write_text("# Synthetic guide\n", encoding="utf-8")
    monkeypatch.setattr(guides, "DOCS_DIR", docs)
    assert guides.glossary_markdown() == "# Synthetic glossary\n"
    assert guides.user_guide_markdown() == "# Synthetic guide\n"


def test_smoke_through_the_in_memory_client(workspace, sample_dir):
    async def go():
        from mcp import Client

        async with Client(mcp_server.mcp) as client:
            start = json.loads((await client.call_tool("start_here", {})).content[0].text)
            loaded = json.loads((await client.call_tool("load_sample_deal", {})).content[0].text)
            deal = json.loads((await client.call_tool("get_deal", {"deal_id": loaded["deal_id"]})).content[0].text)
            walkthrough = await client.get_prompt("new_deal_walkthrough", {})
            explain = await client.get_prompt("explain_assumption", {"key": "exit_cap_rate"})
            guide = await client.read_resource("guide://start-here")
            refused = await client.call_tool("get_deal", {"deal_id": "../../etc"})
            return start, loaded, deal, walkthrough, explain, guide, refused

    start, loaded, deal, walkthrough, explain, guide, refused = anyio.run(go)
    assert start["ok"] and loaded["ok"] and deal["deal_id"] == loaded["deal_id"]
    text = walkthrough.messages[0].content.text
    assert "start_here" in text and "describe_deal" in text
    assert "Exit cap rate" in explain.messages[0].content.text
    assert "load_sample_deal" in guide.contents[0].text
    assert json.loads(refused.content[0].text)["error"]["code"] == "INVALID_DEAL_ID"


def test_deal_register_resource_serves_the_page_and_refuses_traversal(flow):
    async def go():
        from mcp import Client

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(wf_config.HOME_ENV, str(flow["home"]))
            async with Client(mcp_server.mcp) as client:
                page = await client.read_resource(f"deal://{flow['deal_id']}/assumptions.html")
                errors = []
                for uri in ("deal://..%2F..%2Fetc/assumptions.html", "deal://nope-abc123/assumptions.html"):
                    try:
                        await client.read_resource(uri)
                    except Exception as exc:  # noqa: BLE001 - the SDK raises an MCPError
                        errors.append(str(exc))
                return page, errors

    page, errors = anyio.run(go)
    assert "<html" in page.contents[0].text.lower()
    assert len(errors) == 2


def test_legacy_stateless_tools_still_work(minimal_deal_inputs):
    report = mcp_server.validate_deal_inputs(minimal_deal_inputs)
    assert report["adapter_contract"] == "plat.underwriting.mcp/1"
    assert report["status"] in ("PASS", "FAIL")
    summary = mcp_server.run_deal_summary(minimal_deal_inputs)
    assert summary["status"] == "success"
    assert isinstance(summary["irr"].get("levered_irr"), (int, float))
