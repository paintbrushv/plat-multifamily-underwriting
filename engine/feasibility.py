"""
Mechanical feasibility classifier
==================================

Replaces the LLM-side verdict + sanity-flag enforcement that previously lived in
`.claude/agents/underwriting-runner.md` (lines 39-66 — see audit
`runs/deals/a prior production deal/audit_2026-04-26/stage_5_runner_validator.md`
CRITICAL-2).

Bands and verdict logic are copied verbatim from the runner agent prompt so a
prompt regression cannot silently change classification (the v3=marginal →
v4=fail history that motivated this module).

Contract:
    classify(metrics: Dict) -> FeasibilityResult(verdict, sanity_flags, reasons)

The function is pure, deterministic, and has no side effects. Same input dict
always produces the same output. Missing or non-finite metrics are treated as
HARD failures with explicit ``metric_missing_<name>`` / ``metric_invalid_<name>``
flags rather than silently passing.

Sanity-flag identifiers match those listed in `underwriting-runner.md` and in
the `_provenance.json` written by the underwriting runner:
    HARD-tier:
        - cap_rate_outside_6_12_band
        - dscr_below_1_10_likely_covenant_breach
        - irr_outside_expected_band
    WARN-tier:
        - dscr_below_1_20
        - dscr_below_1_30_lender_refi_floor
        - irr_below_12pct_hurdle
        - equity_multiple_low
    Data-quality (HARD):
        - metric_missing_<path>
        - metric_invalid_<path>      (NaN / Inf)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from engine.underwriting_policy import CAP_RATE_SANITY_BAND

Verdict = Literal["pass", "marginal", "fail"]

# --------------------------------------------------------------------------- #
# Bands — copied verbatim from .claude/agents/underwriting-runner.md          #
# --------------------------------------------------------------------------- #

# HARD-tier: any one → verdict = "fail"
# Cap-rate band is the single shared constant from engine/underwriting_policy.py
# so feasibility and validator always agree.  Owner set it to [6%, 12%] on
# 2026-07-05 (widened from prior [4%, 9%]).
CAP_RATE_BAND_LOW = CAP_RATE_SANITY_BAND[0]    # going-in OR exit cap, inclusive lower
CAP_RATE_BAND_HIGH = CAP_RATE_SANITY_BAND[1]   # going-in OR exit cap, inclusive upper
MIN_DSCR_HARD_FLOOR = 1.10          # min_dscr < 1.10 → HARD
IRR_BAND_LOW = 0.08                 # levered_irr < 0.08 → HARD
IRR_BAND_HIGH = 0.30                # levered_irr > 0.30 → HARD

# WARN-tier (only if NO HARD)
MIN_DSCR_WARN_120 = 1.20            # 1.10 ≤ min_dscr < 1.20 → dscr_below_1_20
MIN_DSCR_WARN_130 = 1.30            # 1.20 ≤ min_dscr < 1.30 → dscr_below_1_30_lender_refi_floor
IRR_HURDLE = 0.12                   # 0.08 ≤ levered_irr < 0.12 → irr_below_12pct_hurdle
EQUITY_MULTIPLE_WARN_FLOOR = 1.5    # equity_multiple < 1.5 (5yr hold) → equity_multiple_low

# Pass-only thresholds (verdict step 3, runner lines 63-64)
PASS_IRR_FLOOR = 0.12
PASS_MIN_DSCR_FLOOR = 1.20
PASS_EM_FLOOR = 1.5


@dataclass(frozen=True)
class FeasibilityResult:
    """Output of `classify`. Immutable — frozen so callers can't mutate state."""
    verdict: Verdict
    sanity_flags: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _coerce_finite_float(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Return (float_value, error_token).

    error_token ∈ {None, "missing", "invalid"}.
    "missing" → value is None or absent.
    "invalid" → value is NaN or Inf or non-numeric.
    """
    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        # bool is a subclass of int in Python; reject explicitly.
        return None, "invalid"
    if not isinstance(value, (int, float)):
        return None, "invalid"
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        return None, "invalid"
    return f, None


def _extract(metrics: Dict[str, Any], *path: str) -> Any:
    """Walk a nested dict, return None if any key is missing or value is not a dict."""
    cur: Any = metrics
    for key in path:
        if not isinstance(cur, dict):
            return None
        if key not in cur:
            return None
        cur = cur[key]
    return cur


def _first_present(metrics: Dict[str, Any], *paths: Tuple[str, ...]) -> Tuple[Any, Optional[Tuple[str, ...]]]:
    """Try each path; return (value, path_used). path_used is None if all missing."""
    for p in paths:
        val = _extract(metrics, *p)
        if val is not None:
            return val, p
    return None, None


def _pct(value: float) -> str:
    """Format a fraction as a percentage with 2dp (e.g. 0.4698 -> '46.98%')."""
    return f"{value * 100:.2f}%"


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def classify(metrics: Dict[str, Any]) -> FeasibilityResult:
    """Mechanically classify a deal's feasibility from engine metrics.

    Replaces LLM-side enforcement in underwriting-runner.md.
    Deterministic; same input always produces the same output.

    Parameters
    ----------
    metrics:
        The ``results['metrics']`` dict returned by ``engine.engine.run_underwriting``
        (or the ``metrics`` block of ``deal_summary.json`` /
        ``value_add_base_outputs_*.json``). Expected sub-paths:

        - ``metrics['irr']['levered_irr']`` (float, fraction)
        - ``metrics['equity_multiple']['levered_em']`` (float)
        - ``metrics['dscr']['minimum_dscr']`` (float; also accepts ``minimum`` / ``min_dscr``)
        - ``metrics['dscr']['average_dscr']`` (float; also accepts ``average`` / ``avg_dscr``)
        - ``metrics['yields']['going_in_cap_rate']`` (float, fraction)
        - ``metrics['yields']['exit_cap_rate']`` (float, fraction)

    Returns
    -------
    FeasibilityResult
        ``verdict`` ∈ {"pass", "marginal", "fail"}; ``sanity_flags`` is a list of
        stable identifiers; ``reasons`` is a parallel list of human-readable
        strings (one per flag, same order).
    """
    if not isinstance(metrics, dict):
        return FeasibilityResult(
            verdict="fail",
            sanity_flags=["metric_invalid_metrics_root"],
            reasons=["`metrics` argument is not a dict; cannot classify."],
        )

    hard_flags: List[str] = []
    warn_flags: List[str] = []
    reasons_by_flag: Dict[str, str] = {}

    # ----- pull metrics defensively -------------------------------------- #
    going_in_raw = _extract(metrics, "yields", "going_in_cap_rate")
    exit_raw = _extract(metrics, "yields", "exit_cap_rate")
    levered_irr_raw = _extract(metrics, "irr", "levered_irr")
    levered_em_raw = _extract(metrics, "equity_multiple", "levered_em")
    # dscr keys vary across outputs; check minimum_dscr (engine canonical),
    # minimum, and min_dscr (runner-prompt convention).
    min_dscr_raw, _ = _first_present(
        metrics,
        ("dscr", "minimum_dscr"),
        ("dscr", "minimum"),
        ("dscr", "min_dscr"),
    )
    avg_dscr_raw, _ = _first_present(
        metrics,
        ("dscr", "average_dscr"),
        ("dscr", "average"),
        ("dscr", "avg_dscr"),
    )

    def _check_metric(value: Any, name: str) -> Optional[float]:
        """Validate value is a finite number; emit HARD flag and return None on failure."""
        f, err = _coerce_finite_float(value)
        if err == "missing":
            flag = f"metric_missing_{name}"
            hard_flags.append(flag)
            reasons_by_flag[flag] = f"Required metric `{name}` is missing or null."
            return None
        if err == "invalid":
            flag = f"metric_invalid_{name}"
            hard_flags.append(flag)
            reasons_by_flag[flag] = (
                f"Required metric `{name}` is not a finite number "
                f"(NaN/Inf or wrong type)."
            )
            return None
        return f

    going_in = _check_metric(going_in_raw, "yields.going_in_cap_rate")
    exit_cap = _check_metric(exit_raw, "yields.exit_cap_rate")
    levered_irr = _check_metric(levered_irr_raw, "irr.levered_irr")
    levered_em = _check_metric(levered_em_raw, "equity_multiple.levered_em")
    min_dscr = _check_metric(min_dscr_raw, "dscr.minimum_dscr")
    avg_dscr = _check_metric(avg_dscr_raw, "dscr.average_dscr")
    # avg_dscr is collected but currently unused by the runner-spec bands.
    # Kept extracted (and validated) so a future band change has the value
    # already coerced; mark as intentionally referenced.
    _ = avg_dscr

    # ----- HARD-tier band checks ----------------------------------------- #

    if going_in is not None and exit_cap is not None:
        out_of_band = (
            going_in < CAP_RATE_BAND_LOW or going_in > CAP_RATE_BAND_HIGH
            or exit_cap < CAP_RATE_BAND_LOW or exit_cap > CAP_RATE_BAND_HIGH
        )
        if out_of_band:
            flag = "cap_rate_outside_6_12_band"
            hard_flags.append(flag)
            offending = []
            if going_in < CAP_RATE_BAND_LOW or going_in > CAP_RATE_BAND_HIGH:
                offending.append(f"going-in {_pct(going_in)}")
            if exit_cap < CAP_RATE_BAND_LOW or exit_cap > CAP_RATE_BAND_HIGH:
                offending.append(f"exit {_pct(exit_cap)}")
            reasons_by_flag[flag] = (
                f"Cap rate ({', '.join(offending)}) outside the expected "
                f"band of {_pct(CAP_RATE_BAND_LOW)}-{_pct(CAP_RATE_BAND_HIGH)}."
            )

    if min_dscr is not None and min_dscr < MIN_DSCR_HARD_FLOOR:
        flag = "dscr_below_1_10_likely_covenant_breach"
        hard_flags.append(flag)
        reasons_by_flag[flag] = (
            f"Min DSCR {min_dscr:.2f} is below the {MIN_DSCR_HARD_FLOOR:.2f} "
            f"covenant-breach threshold."
        )

    if levered_irr is not None and (
        levered_irr < IRR_BAND_LOW or levered_irr > IRR_BAND_HIGH
    ):
        flag = "irr_outside_expected_band"
        hard_flags.append(flag)
        reasons_by_flag[flag] = (
            f"Levered IRR {_pct(levered_irr)} is outside the expected band "
            f"of {_pct(IRR_BAND_LOW)}-{_pct(IRR_BAND_HIGH)}."
        )

    # ----- WARN-tier band checks (only matter if no HARD) ----------------- #

    if min_dscr is not None and MIN_DSCR_HARD_FLOOR <= min_dscr < MIN_DSCR_WARN_120:
        flag = "dscr_below_1_20"
        warn_flags.append(flag)
        reasons_by_flag[flag] = (
            f"Min DSCR {min_dscr:.2f} is below the {MIN_DSCR_WARN_120:.2f} "
            f"warn threshold (above the {MIN_DSCR_HARD_FLOOR:.2f} HARD floor)."
        )
    elif min_dscr is not None and MIN_DSCR_WARN_120 <= min_dscr < MIN_DSCR_WARN_130:
        flag = "dscr_below_1_30_lender_refi_floor"
        warn_flags.append(flag)
        reasons_by_flag[flag] = (
            f"Min DSCR {min_dscr:.2f} is below the {MIN_DSCR_WARN_130:.2f} "
            f"typical lender refi floor."
        )

    if levered_irr is not None and IRR_BAND_LOW <= levered_irr < IRR_HURDLE:
        flag = "irr_below_12pct_hurdle"
        warn_flags.append(flag)
        reasons_by_flag[flag] = (
            f"Levered IRR {_pct(levered_irr)} is below the "
            f"{_pct(IRR_HURDLE)} core-plus hurdle "
            f"(above the {_pct(IRR_BAND_LOW)} HARD floor)."
        )

    if levered_em is not None and levered_em < EQUITY_MULTIPLE_WARN_FLOOR:
        flag = "equity_multiple_low"
        warn_flags.append(flag)
        reasons_by_flag[flag] = (
            f"Equity multiple {levered_em:.2f}x is below the "
            f"{EQUITY_MULTIPLE_WARN_FLOOR:.2f}x floor for a 5-year hold."
        )

    # ----- verdict (mechanical, top-down per runner lines 57-64) --------- #

    sanity_flags = list(hard_flags) + list(warn_flags)

    if hard_flags:
        verdict: Verdict = "fail"
    elif warn_flags:
        verdict = "marginal"
    else:
        # Step 3 of runner: explicit pass-only conjunction. If any of the three
        # core metrics is missing/invalid we already added a HARD flag above,
        # so reaching here implies all three are present and finite.
        if (
            levered_irr is not None
            and min_dscr is not None
            and levered_em is not None
            and levered_irr >= PASS_IRR_FLOOR
            and min_dscr >= PASS_MIN_DSCR_FLOOR
            and levered_em >= PASS_EM_FLOOR
        ):
            verdict = "pass"
        else:
            # Step 4 catch-all (per runner): near-miss / incomplete metrics
            # that didn't trip any explicit band. Should be rare in practice.
            verdict = "marginal"

    reasons = [reasons_by_flag[f] for f in sanity_flags]

    return FeasibilityResult(
        verdict=verdict,
        sanity_flags=sanity_flags,
        reasons=reasons,
    )


__all__ = [
    "FeasibilityResult",
    "Verdict",
    "classify",
    # Bands exposed for downstream consumers (memo composer, validator).
    "CAP_RATE_BAND_LOW",
    "CAP_RATE_BAND_HIGH",
    "MIN_DSCR_HARD_FLOOR",
    "MIN_DSCR_WARN_120",
    "MIN_DSCR_WARN_130",
    "IRR_BAND_LOW",
    "IRR_BAND_HIGH",
    "IRR_HURDLE",
    "EQUITY_MULTIPLE_WARN_FLOOR",
]
