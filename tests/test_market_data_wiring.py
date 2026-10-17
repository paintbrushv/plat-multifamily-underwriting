"""Feeder-root discovery, geography matching and provider diagnostics.

Synthetic roots and slugs only. ``dallas_tx`` / ``19100`` appear only as the
public metro identifiers the providers ship aliases for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

from engine.market_data import (  # noqa: E402
    Geo,
    PlatMarketStudyAgentProvider,
    PlatSupplyDemandProvider,
    default_registry,
)
from engine.market_data.paths import (  # noqa: E402
    feeder_root,
    load_user_config,
    main_checkout,
    user_config_path,
)
from tests.market_data_feeders import (  # noqa: E402
    costar_row,
    write_costar_submarket,
    write_ensemble,
    write_metro_config,
    write_supply_demand_forecast,
)

METRO = "testville_zz_usa"


def _rows(stem: str, vacancy: float = 0.07, inventory: int = 1000):
    return [
        costar_row(q, "All", is_forecast=False, stem=stem, vacancy_rate=vacancy, stabilized_vacancy=0.06,
                   asking_rent_unit=1500.0, effective_rent_unit=1450.0, market_cap_rate=0.055,
                   inventory_units=inventory, under_construction=50)
        for q in ("2025 Q4", "2026 Q1")
    ]


# -- feeder roots: env > user config file > sibling > main checkout's sibling ---------


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cfg" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_user_config_path_honors_override_xdg_and_home(tmp_path):
    assert user_config_path({"PLAT_UNDERWRITING_CONFIG": str(tmp_path / "c.toml")}) == tmp_path / "c.toml"
    assert user_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "plat-underwriting" / "config.toml"
    assert user_config_path({"HOME": str(tmp_path)}) == tmp_path / ".config" / "plat-underwriting" / "config.toml"
    assert user_config_path({}) is None


def test_user_config_path_falls_back_to_userprofile_on_windows(tmp_path):
    # Windows sets USERPROFILE, usually not HOME; the config must still be found.
    assert user_config_path({"USERPROFILE": str(tmp_path)}) == tmp_path / ".config" / "plat-underwriting" / "config.toml"
    assert user_config_path({"HOME": str(tmp_path / "h"), "USERPROFILE": str(tmp_path)}) == (
        tmp_path / "h" / ".config" / "plat-underwriting" / "config.toml")


def test_windows_backslash_path_in_double_quotes_reports_a_fix(tmp_path):
    # "C:\Users\..." in a TOML basic string is an invalid escape; say how to write it.
    path = _config(tmp_path, '[market_data]\nmarket_study_root = "C:\\Users\\analyst\\feeds"\n')
    cfg = load_user_config({"PLAT_UNDERWRITING_CONFIG": str(path)})
    assert cfg.exists and cfg.market_data == {}
    assert cfg.error and "single quotes" in cfg.error and "forward slashes" in cfg.error


def test_user_config_supplies_feeder_roots_below_the_environment(tmp_path):
    ms, sd = tmp_path / "feeds" / "ms", tmp_path / "feeds" / "sd"
    ms.mkdir(parents=True)
    sd.mkdir(parents=True)
    path = _config(tmp_path, '[market_data]\nmarket_study_root = "../feeds/ms"\n'
                             f'supply_demand_root = "{sd.as_posix()}"\n')
    env = {"PLAT_UNDERWRITING_CONFIG": str(path)}
    found = feeder_root("market_study", env, siblings=tmp_path / "nowhere")
    assert found.path == ms.resolve()  # relative to the config file's folder
    assert found.origin.startswith("config ") and str(path) in found.origin
    assert feeder_root("supply_demand", env, siblings=tmp_path / "nowhere").path == sd.resolve()
    # The environment still wins.
    env_wins = dict(env, PLAT_MARKET_STUDY_ROOT=str(tmp_path / "x"))
    assert feeder_root("market_study", env_wins).path == tmp_path / "x"
    assert feeder_root("market_study", env_wins).origin == "env PLAT_MARKET_STUDY_ROOT"


def test_unreadable_user_config_is_reported_not_raised(tmp_path):
    path = _config(tmp_path, "[market_data\nbroken = ")
    config = load_user_config({"PLAT_UNDERWRITING_CONFIG": str(path)})
    assert config.exists and config.error and "unreadable" in config.error
    found = feeder_root("market_study", {"PLAT_UNDERWRITING_CONFIG": str(path)}, siblings=tmp_path)
    assert found.path == tmp_path / "plat-market-study-agent"
    assert "not found" in found.origin


def test_default_registry_reads_provider_order_and_roots_from_the_user_config(tmp_path):
    ms = tmp_path / "ms"
    write_costar_submarket(ms, f"{METRO}_north", _rows(f"{METRO}_north"))
    path = _config(tmp_path, f'[market_data]\nproviders = ["plat_market_study_agent"]\nmarket_study_root = "{ms.as_posix()}"\n')
    registry = default_registry(env={"PLAT_UNDERWRITING_CONFIG": str(path)})
    assert registry.names == ["plat_market_study_agent"]
    assert registry.providers[0].root == ms.resolve()
    assert registry.config_source["provider_order"].startswith("config ")
    assert registry.config_source["user_config"]["exists"] is True
    obs, _ = registry.resolve("vacancy_current", Geo(metro_slug="testville_zz"))
    assert obs is not None and obs.provenance.provider == "plat_market_study_agent"


def _fake_worktree(tmp_path: Path) -> Path:
    """<tmp>/main checkout with <tmp>/main/.claude/worktrees/wt as a git worktree of it."""
    main = tmp_path / "main"
    gitdir = main / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = main / ".claude" / "worktrees" / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return worktree


def test_a_worktree_finds_the_feeders_next_to_its_main_checkout(tmp_path):
    worktree = _fake_worktree(tmp_path)
    assert main_checkout(worktree) == tmp_path / "main"
    assert main_checkout(tmp_path / "main") is None  # a .git directory is not a worktree
    (tmp_path / "plat-supply-demand").mkdir()
    found = feeder_root("supply_demand", {}, repo_root=worktree)
    assert found.path == tmp_path / "plat-supply-demand"
    assert "main checkout" in found.origin
    missing = feeder_root("market_study", {}, repo_root=worktree)
    assert missing.path == worktree.parent / "plat-market-study-agent"
    assert "not found" in missing.origin


# -- geography matching -------------------------------------------------------------


def test_submarket_names_match_on_letters_and_digits(tmp_path):
    write_costar_submarket(tmp_path, f"{METRO}_north_lake_flats", _rows(f"{METRO}_north_lake_flats", 0.05))
    # The older CoStar naming drops the separator: "Oldtown/River Bend" -> oldtownriver_bend.
    write_costar_submarket(tmp_path, f"{METRO}_oldtownriver_bend", _rows(f"{METRO}_oldtownriver_bend", 0.09))
    provider = PlatMarketStudyAgentProvider(tmp_path, env={})
    north = provider.get("vacancy_current", Geo(metro_slug="testville_zz", submarket="North Lake/Flats"))
    assert north.value == pytest.approx(0.05) and north.geo.submarket == f"{METRO}_north_lake_flats"
    old = provider.get("vacancy_current", Geo(metro_slug="testville_zz", submarket="Oldtown/River Bend"))
    assert old.value == pytest.approx(0.09) and old.geo.submarket == f"{METRO}_oldtownriver_bend"


def test_an_unmatched_submarket_uses_the_metro_aggregate_and_says_so(tmp_path):
    write_costar_submarket(tmp_path, f"{METRO}_north", _rows(f"{METRO}_north", 0.05))
    write_costar_submarket(tmp_path, f"{METRO}_south", _rows(f"{METRO}_south", 0.09))
    provider = PlatMarketStudyAgentProvider(tmp_path, env={})
    obs = provider.get("vacancy_current", Geo(metro_slug="testville_zz", submarket="Nowhere Flats"))
    assert obs.geo.submarket is None and obs.value == pytest.approx(0.07)
    assert obs.attributes["submarket_requested"] == "Nowhere Flats"
    report = provider.diagnose(Geo(metro_slug="testville_zz", submarket="Nowhere Flats"))
    assert report["matched"] is True
    assert report["geography"]["submarket_file"] is None
    assert report["geography"]["submarkets_available"] == ["north", "south"]
    assert "'Nowhere Flats' not found" in report["summary"] and "metro aggregate" in report["summary"]


def test_a_cbsa_code_alone_maps_to_a_metro(tmp_path):
    write_costar_submarket(tmp_path, f"{METRO}_north", _rows(f"{METRO}_north", 0.05))
    config = tmp_path / "agents" / "configs" / "subject.yaml"
    config.parent.mkdir(parents=True)
    config.write_text('notes:\n  metro_slug: "testville_zz"\n  msa_code: "99977"\n', encoding="utf-8")
    provider = PlatMarketStudyAgentProvider(tmp_path, env={})
    obs = provider.get("vacancy_current", Geo(cbsa_code="99977"))
    assert obs is not None and obs.value == pytest.approx(0.05)
    assert provider.diagnose(Geo(cbsa_code="99977"))["geography"]["metro_via"] == "cbsa_code"
    # The shipped default: CBSA 19100 is the dallas_tx metro (and its CoStar aliases).
    stem = "dallas_fort_worth_tx_usa_alpha"
    write_costar_submarket(tmp_path, stem, _rows(stem, 0.08))
    assert provider.get("vacancy_current", Geo(cbsa_code="19100")).value == pytest.approx(0.08)


# -- availability and diagnostics ---------------------------------------------------


def test_an_empty_market_study_root_is_unavailable_and_says_what_is_missing(tmp_path):
    provider = PlatMarketStudyAgentProvider(tmp_path, env={}, root_origin="test root")
    assert provider.available() is False
    reason = provider.unavailable_reason
    assert "holds no feeder outputs" in reason and "data/paid/costar-submarket" in reason
    report = provider.diagnose(Geo(metro_slug="testville_zz"))
    assert report["root_found"] is True and report["root_origin"] == "test root"
    assert report["matched"] is False and "no feeder outputs" in report["summary"]
    missing = PlatMarketStudyAgentProvider(tmp_path / "nope", env={})
    assert missing.diagnose(Geo(metro_slug="x"))["root_found"] is False


def test_market_study_diagnose_reports_an_unmatched_metro(tmp_path):
    write_costar_submarket(tmp_path, f"{METRO}_north", _rows(f"{METRO}_north"))
    write_ensemble(tmp_path, "subject", metro_slug="testville_zz", tier="All", tier_slug="all",
                   panel_end="2026Q1", periods={"2026Q2": 0.02})
    provider = PlatMarketStudyAgentProvider(tmp_path, env={})
    assert provider.available()
    report = provider.diagnose(Geo(metro_slug="otherville_zz", submarket="North"))
    assert report["matched"] is False
    assert report["inventory"]["costar_parquet_files"] == 1
    assert report["inventory"]["ensemble_metros"] == ["testville_zz"]
    assert "no CoStar parquet for metro otherville_zz" in report["summary"]
    assert "no ensemble forecast for metro otherville_zz" in report["summary"]
    good = provider.diagnose(Geo(metro_slug="testville_zz", submarket="North"))
    assert good["matched"] is True and good["summary"] is None
    assert good["geography"]["submarket_file"] == f"{METRO}_north"


def test_swallowed_provider_errors_are_recorded(tmp_path):
    bad = tmp_path / "data" / "paid" / "costar-submarket"
    bad.mkdir(parents=True)
    (bad / f"{METRO}_north.parquet").write_bytes(b"not a parquet file")
    provider = PlatMarketStudyAgentProvider(tmp_path, env={})
    assert provider.get("vacancy_current", Geo(metro_slug="testville_zz")) is None
    assert "vacancy_current" in provider.errors
    assert "vacancy_current" in provider.diagnose(Geo(metro_slug="testville_zz"))["errors"]


def test_a_supply_demand_root_without_forecasts_is_unavailable(tmp_path):
    write_metro_config(tmp_path, "testville_zz", "99977")
    provider = PlatSupplyDemandProvider(tmp_path, env={})
    assert provider.available() is False
    assert "econ forecast" in provider.unavailable_reason
    report = provider.diagnose(Geo(cbsa_code="99977", metro_slug="testville_zz"))
    assert report["inventory"]["metro_configs"] == {"99977": "testville_zz"}
    assert report["geography"]["metro_id"] == "testville_zz"
    assert "econ forecast --metro testville_zz" in report["summary"]
    write_supply_demand_forecast(tmp_path, "testville_zz", "2026Q1", {"2026Q2": 0.02})
    assert provider.available()
    ok = provider.diagnose(Geo(cbsa_code="99977"))
    assert ok["matched"] is True and ok["summary"] is None and ok["geography"]["metro_via"] == "cbsa_code"
