"""Exit caps track entry caps (ADR 0003 §3).

Unless the analyst (or an explicit backsolve policy) sets it, the exit cap is
the going-in cap at the price being modeled (year-1 NOI / purchase price) plus
HOUSE_EXIT_CAP_SPREAD, re-measured at every backsolve candidate price. Only a
"screaming steal" (a market cap rate is known and the going-in cap is more than
HOUSE_STEAL_THRESHOLD above it) compresses the exit. The compression scales
with the excess over the threshold, linearly from 0 at the threshold to
HOUSE_MAX_EXIT_COMPRESSION at HOUSE_STEAL_FULL_COMPRESSION_EXCESS, capped there,
and the entry is flagged.

Synthetic deals only (tests/fixtures/deals).
"""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from functools import lru_cache
import json

import pytest

from engine import underwriting_policy as house
from engine.assumptions import resolve_assumptions
from engine.backsolve import backsolve_price
from engine.engine import run_underwriting
from engine.market_data import MarketDataRegistry, SyntheticProvider
from tests import deal_library as dl

SLUG = "a_stabilized_1990s"
SPREAD = float(house.HOUSE_EXIT_CAP_SPREAD)
BENCHMARK = {"rate": "0.04", "as_of": "2026-10-03", "source": "synthetic:test"}
POLICY = {"version": "plat.backsolve-policy/1", "strategy": "cashflow"}


def _entry(canonical):
    return canonical["assumptions"]["entries"]["exit_cap_rate"]


@lru_cache(maxsize=None)
def _no_market_text() -> str:
    """The library deal ingested with no market data at all."""
    return json.dumps(dl.ingest(dl.load_deal(SLUG), market=None))


def _no_market():
    return json.loads(_no_market_text())


def _market_registry(cap_rate: float) -> MarketDataRegistry:
    """Only a submarket market_cap_rate, flat over the exit year."""
    deal = dl.load_deal(SLUG)
    series = {f"2031Q{q}": cap_rate for q in range(1, 5)}
    provider = SyntheticProvider({("market_cap_rate", dl.deal_geo(deal, "submarket")): series},
                                 as_of=date(2026, 6, 30), confidence="high")
    return MarketDataRegistry([provider])


def _with_market(cap_rate: float):
    return dl.ingest(dl.load_deal(SLUG), market=_market_registry(cap_rate))


def _year1_noi(canonical) -> float:
    return run_underwriting(copy.deepcopy(canonical))["metrics"]["noi"]["year_1_noi"]


# ── the rule ─────────────────────────────────────────────────────────────


def test_house_constants():
    assert house.HOUSE_EXIT_CAP_SPREAD == Decimal("0.0050")
    assert house.HOUSE_STEAL_THRESHOLD == Decimal("0.0150")
    assert house.HOUSE_MAX_EXIT_COMPRESSION == Decimal("0.0100")
    assert house.HOUSE_STEAL_FULL_COMPRESSION_EXCESS == Decimal("0.0350")  # threshold + 200 bps
    assert house.HOUSE_EXIT_CAP_GOING_IN_MAX == Decimal("0.25")


@pytest.mark.parametrize("going_in, market, expected, steal, compression", [
    ("0.09", None, "0.095", False, "0"),           # no market data: never compressed
    ("0.06", "0.065", "0.065", False, "0"),        # paying above market: still entry + spread
    ("0.07", "0.058", "0.075", False, "0"),        # 120 bps over market: under the threshold
    ("0.07", "0.055", "0.075", False, "0"),        # exactly 150 bps: not "more than", no compression
    # Over the threshold the compression grows linearly with (excess - 150 bps),
    # reaching 100 bps at 350 bps of excess.
    ("0.0702", "0.055", "0.0751", True, "0.0001"),  # 152 bps over: 2/200 of 100 bps = 1 bp
    ("0.08", "0.055", "0.080", True, "0.005"),      # 250 bps over (mid-range): half, 50 bps
    ("0.09", "0.055", "0.085", True, "0.010"),      # 350 bps over (full scale): 100 bps
    ("0.11", "0.055", "0.105", True, "0.010"),      # 550 bps over (beyond): capped at 100 bps
])
def test_house_exit_cap_rate(going_in, market, expected, steal, compression):
    out = house.house_exit_cap_rate(Decimal(going_in), market_cap_rate=Decimal(market) if market else None)
    assert out["exit_cap_rate"] == Decimal(expected)
    assert out["steal"] is steal
    assert out["compression"] == Decimal(compression)
    assert out["anchored"] == Decimal(going_in) + house.HOUSE_EXIT_CAP_SPREAD
    assert Decimal("0") <= out["compression"] <= house.HOUSE_MAX_EXIT_COMPRESSION
    if market is not None:
        assert out["excess_over_market"] == Decimal(going_in) - Decimal(market)
        # The existing guard: a compressed exit is never below market + spread
        # (so never below market).
        if steal:
            assert out["exit_cap_rate"] >= Decimal(market) + house.HOUSE_EXIT_CAP_SPREAD


def test_steal_compression_is_continuous_and_monotone_in_the_excess():
    market = Decimal("0.05")
    threshold = house.HOUSE_STEAL_THRESHOLD
    previous = None
    for bps in range(0, 701, 5):
        excess = Decimal(bps) / Decimal(10_000)
        out = house.house_exit_cap_rate(market + excess, market_cap_rate=market)
        if excess <= threshold:
            assert out["compression"] == 0 and not out["steal"]
        if previous is not None:
            assert out["compression"] >= previous  # never falls as the discount grows
            assert out["compression"] - previous <= Decimal("0.00026")  # no jump (5 bps step: 2.5 bps max)
        previous = out["compression"]
    # Just above the threshold the compression is tiny, not a 100 bps jump.
    near = house.house_exit_cap_rate(market + threshold + Decimal("0.0001"), market_cap_rate=market)
    assert Decimal("0") < near["compression"] <= Decimal("0.00005")


# ── the resolver ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("price", [26_000_000, 33_120_000, 41_000_000])
def test_exit_is_entry_plus_spread_at_the_modeled_price(price):
    canonical = _no_market()
    canonical["purchase_assumptions"]["purchase_price"] = price
    resolved = resolve_assumptions(canonical)
    entry = _entry(resolved)
    going_in = _year1_noi(resolved) / price

    assert entry["source"] == "house:exit_cap_spread" and entry["override"] is None
    assert entry["review_required"] is True
    assert entry["value"] == pytest.approx(going_in + SPREAD, abs=1e-6)
    assert resolved["exit_assumptions"]["exit_cap_rate"] == entry["value"]
    assert entry["detail"]["going_in_cap_rate"] == pytest.approx(going_in, abs=1e-6)
    assert entry["detail"]["noi_basis"] == "year-1 NOI"
    assert entry["detail"]["steal"] is False and entry["detail"]["market"] is None
    assert "Entry-anchored" in entry["basis"] and "never compresses" in entry["basis"]
    metrics = run_underwriting(copy.deepcopy(resolved))["metrics"]["yields"]
    assert metrics["exit_cap_rate"] == pytest.approx(metrics["going_in_cap_rate"] + SPREAD, abs=1e-4)


def test_no_compression_without_market_data_even_for_a_cheap_price():
    canonical = _no_market()
    canonical["purchase_assumptions"]["purchase_price"] = 18_000_000  # going-in about 10%
    entry = _entry(resolve_assumptions(canonical))
    going_in = entry["detail"]["going_in_cap_rate"]
    assert going_in > 0.09
    assert entry["value"] == pytest.approx(going_in + SPREAD, abs=1e-6)
    assert entry["detail"]["compression"] == 0.0
    assert not any(c["source"].startswith("market:") for c in entry["candidates"])


def test_steal_compression_scales_with_the_discount_and_is_flagged():
    going_in = _entry(_no_market())["detail"]["going_in_cap_rate"]
    market = round(going_in - 0.025, 6)  # going-in 250 bps over market: halfway to full scale
    canonical = _with_market(market)
    entry = _entry(canonical)

    assert entry["source"] == "house:exit_cap_spread"
    assert entry["detail"]["steal"] is True
    assert entry["detail"]["market"]["rate"] == pytest.approx(market)
    # (250 - 150) / (350 - 150) of 100 bps = 50 bps.
    assert entry["value"] == pytest.approx(going_in + SPREAD - 0.005, abs=1e-6)
    assert entry["detail"]["compression"] == pytest.approx(0.005, abs=1e-6)
    assert entry["detail"]["excess_over_market"] == pytest.approx(0.025, abs=1e-6)
    assert entry["detail"]["steal_scale"] == pytest.approx(0.5, abs=1e-4)
    assert entry["review_required"] is True
    assert f"Steal: going-in {going_in:.2%} vs market {market:.2%}" in entry["basis"]
    # The disclosure states the excess, the scaled compression and the formula.
    assert "250 bps above it" in entry["basis"]
    assert "compression 50 bps" in entry["basis"]
    assert "min(1, (excess - 150 bps) / (350 bps - 150 bps)) x 100 bps" in entry["basis"]
    assert any(c["source"] == "market:synthetic" for c in entry["candidates"])
    # Idempotent without the registry: the market reference is kept on the entry.
    again = resolve_assumptions(canonical)
    assert _entry(again) == entry and again["exit_assumptions"] == canonical["exit_assumptions"]


@pytest.mark.parametrize("gap", [0.012, -0.004])
def test_no_steal_keeps_the_exit_anchored_and_records_the_market(gap):
    """Under the threshold, or paying at or above market: entry + spread."""
    going_in = _entry(_no_market())["detail"]["going_in_cap_rate"]
    entry = _entry(_with_market(round(going_in - gap, 6)))
    assert entry["value"] == pytest.approx(going_in + SPREAD, abs=1e-6)
    assert entry["detail"]["steal"] is False
    assert any(c["source"] == "market:synthetic" for c in entry["candidates"])
    assert "candidate only" in entry["basis"]


def test_the_analyst_exit_cap_wins_and_a_price_change_moves_only_the_house_one():
    canonical = _no_market()
    analyst = resolve_assumptions(canonical, overrides={"exit_cap_rate": 0.0575})
    analyst["purchase_assumptions"]["purchase_price"] = 25_000_000
    repriced = resolve_assumptions(analyst)
    assert _entry(repriced)["source"] == "analyst" and repriced["exit_assumptions"]["exit_cap_rate"] == 0.0575

    # The house exit cap follows a price edit on re-resolution (it is not an analyst edit).
    canonical["purchase_assumptions"]["purchase_price"] = 25_000_000
    moved = resolve_assumptions(canonical)
    assert _entry(moved)["source"] == "house:exit_cap_spread" and _entry(moved)["override"] is None
    assert moved["exit_assumptions"]["exit_cap_rate"] > _no_market()["exit_assumptions"]["exit_cap_rate"]


def test_resolution_is_deterministic_and_idempotent():
    first, second = _no_market(), dl.ingest(dl.load_deal(SLUG), market=None)
    assert first == second
    assert resolve_assumptions(first) == first


# ── backsolve ────────────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def _solve_text(slug: str, market: str, policy_extra: str = "{}") -> str:
    canonical = _no_market() if market == "none" else dl.ingest(dl.load_deal(slug))
    result = backsolve_price(
        canonical, target_coc="0.06", policy={**POLICY, **json.loads(policy_extra)}, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="60000000.00",
    )
    return json.dumps({k: result[k] for k in ("status", "summary", "case", "effective_assumptions")})


def _solve(slug=SLUG, market="none", **policy_extra):
    return json.loads(_solve_text(slug, market, json.dumps(policy_extra, sort_keys=True)))


def test_backsolve_re_anchors_the_exit_cap_at_the_solved_price():
    ask = _no_market()
    ask_exit = ask["exit_assumptions"]["exit_cap_rate"]
    result = _solve()
    summary, case = result["summary"], result["case"]
    calc = summary["exit_cap_calculation"]

    assert result["status"] == "converged"
    assert result["effective_assumptions"]["exit_cap_mode"] == "entry_anchored"
    assert result["effective_assumptions"]["policy_value_sources"]["exit_cap_rate"] == "entry_anchored"
    assert calc["mode"] == "entry_anchored" and calc["purchase_price"] == summary["solved_purchase_price"]
    going_in = calc["year1_noi"] / summary["solved_purchase_price"]
    assert case["exit_assumptions"]["exit_cap_rate"] == pytest.approx(going_in + SPREAD, abs=1e-6)
    # The engine's own going-in and exit at the solved price, not the ask-anchored exit cap.
    assert summary["exit_cap_rate"] == pytest.approx(summary["going_in_cap_rate"] + SPREAD, abs=1e-4)
    assert abs(summary["exit_cap_rate"] - ask_exit) > 0.002
    # The solved canonical's entry is the resolver's default at the solved price.
    entry = _entry(case)
    assert entry["source"] == "house:exit_cap_spread" and entry["override"] is None
    assert entry["value"] == case["exit_assumptions"]["exit_cap_rate"]
    assert entry["detail"]["purchase_price"] == summary["solved_purchase_price"]
    assert resolve_assumptions(case, mode="pricing") == case


def test_backsolve_applies_the_steal_test_at_every_price():
    """The market reference recorded at ingest drives the steal test in pricing."""
    going_in = _entry(_no_market())["detail"]["going_in_cap_rate"]
    canonical = _with_market(round(going_in - 0.025, 6))
    result = backsolve_price(
        canonical, target_coc="0.06", policy=POLICY, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="60000000.00",
    )
    calc = result["summary"]["exit_cap_calculation"]
    market = calc["market"]["rate"]
    solved_going_in = calc["going_in_cap_rate"]
    expected = house.house_exit_cap_rate(Decimal(str(solved_going_in)), market_cap_rate=Decimal(str(market)))
    assert calc["steal"] is expected["steal"]
    assert calc["exit_cap_rate"] == pytest.approx(float(expected["exit_cap_rate"]), abs=1e-6)
    assert result["case"]["exit_assumptions"]["exit_cap_rate"] == calc["exit_cap_rate"]
    assert resolve_assumptions(result["case"], mode="pricing") == result["case"]


def test_backsolve_preserves_the_analyst_exit_cap():
    canonical = resolve_assumptions(_no_market(), overrides={"exit_cap_rate": 0.0575})
    result = backsolve_price(
        canonical, target_coc="0.06", policy=POLICY, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="60000000.00",
    )
    assert result["effective_assumptions"]["exit_cap_mode"] == "canonical_analyst"
    assert result["summary"]["exit_cap_calculation"] == {"mode": "canonical_analyst", "exit_cap_rate": 0.0575}
    assert result["case"]["exit_assumptions"]["exit_cap_rate"] == 0.0575
    assert _entry(result["case"])["source"] == "analyst"
    assert "exit_cap_calculation" not in result["case"]["metadata"]["property_summary"]


def test_an_explicit_policy_exit_cap_is_priced_as_given():
    result = _solve(exit_cap_rate="0.0625")
    assert result["effective_assumptions"]["exit_cap_mode"] == "explicit_policy"
    assert result["case"]["exit_assumptions"]["exit_cap_rate"] == 0.0625
    assert _entry(result["case"])["source"] == "house:backsolve_policy"


def test_backsolve_is_deterministic():
    first = _solve()
    canonical = _no_market()
    second = backsolve_price(
        canonical, target_coc="0.06", policy=POLICY, benchmark=BENCHMARK,
        min_price="1000000.00", max_price="60000000.00",
    )
    assert second["summary"] == first["summary"]
    assert second["case"] == first["case"]
