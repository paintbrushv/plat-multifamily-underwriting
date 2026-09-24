"""
Market Study Agent Integration
================================
Connects the underwriting engine with the market-study-agent repo
for automated comp analysis, rent benchmarking, and macro context.

Usage:
    from engine.market_integration import MarketContext

    ctx = MarketContext.from_config("dallas_tx_forest_hills")
    comp_report = ctx.comp_comparison()
    adjusted_inputs = ctx.apply_rent_adjustments(inputs)
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Default path to market-study-agent repo (sibling project)
MARKET_AGENT_ROOT = Path(__file__).resolve().parents[1].parent / "market-study-agent"


@dataclass
class FloorplanComp:
    """A single floorplan from a comp property."""
    floorplan_name: str
    beds: int
    baths: float
    sqft: int
    sqft_min: Optional[int] = None
    sqft_max: Optional[int] = None
    rent_min: Optional[float] = None
    rent_max: Optional[float] = None
    rent_per_sf: Optional[float] = None
    available_units: int = 0


@dataclass
class CompProperty:
    """A comparable property with floorplan-level data."""
    name: str
    address: str
    year_built: Optional[int] = None
    wd_status: Optional[str] = None
    floorplans: List[FloorplanComp] = field(default_factory=list)
    specials: List[str] = field(default_factory=list)
    snapshot_date: Optional[str] = None

    @property
    def avg_rent_per_sf(self) -> Optional[float]:
        plans_with_data = [
            fp for fp in self.floorplans
            if fp.rent_per_sf is not None and fp.rent_per_sf > 0
        ]
        if not plans_with_data:
            return None
        return sum(fp.rent_per_sf for fp in plans_with_data) / len(plans_with_data)


@dataclass
class SubjectFloorplan:
    """A floorplan from the subject property's rent roll."""
    plan_code: str
    bed_type: str
    units: int
    sqft: int
    avg_market_rent: float

    @property
    def rent_per_sf(self) -> float:
        return self.avg_market_rent / self.sqft if self.sqft > 0 else 0


@dataclass
class CompComparison:
    """Side-by-side comparison of subject vs comps for a bed type."""
    bed_type: str
    subject_avg_rent: float
    subject_avg_sf: int
    subject_rent_per_sf: float
    comp_avg_rent: float
    comp_avg_sf: int
    comp_rent_per_sf: float
    rent_premium_pct: float  # positive = subject above market
    comp_count: int


# ── Data Loaders ──────────────────────────────────────────────────────────


def load_comp_snapshot(snapshot_path: Path) -> List[CompProperty]:
    """Load comp properties from a JSON snapshot file.

    Expected format: output of market-study-agent's collect_comps_snapshot.py
    """
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    comps = []

    for comp_data in data.get("comps", []):
        direct = comp_data.get("direct", {})
        floorplans = []

        for fp in direct.get("floorplans", []):
            sqft = fp.get("sqft", 0)
            rent_min = fp.get("rent_min")
            rent_max = fp.get("rent_max")
            avg_rent = (
                (rent_min + rent_max) / 2
                if rent_min is not None and rent_max is not None
                else rent_min or rent_max
            )

            floorplans.append(FloorplanComp(
                floorplan_name=fp.get("floorplan_name", fp.get("floorplan_id", "")),
                beds=fp.get("beds", 0),
                baths=fp.get("baths", 1.0),
                sqft=sqft,
                sqft_min=fp.get("sqft_min"),
                sqft_max=fp.get("sqft_max"),
                rent_min=rent_min,
                rent_max=rent_max,
                rent_per_sf=avg_rent / sqft if sqft > 0 and avg_rent else None,
                available_units=fp.get("available_units", 0),
            ))

        comps.append(CompProperty(
            name=comp_data.get("name", ""),
            address=comp_data.get("address", ""),
            year_built=comp_data.get("year_built"),
            wd_status=comp_data.get("wd_status"),
            floorplans=floorplans,
            specials=direct.get("specials", []),
            snapshot_date=data.get("run_date"),
        ))

    return comps


def load_floorplan_summary(csv_path: Path) -> List[SubjectFloorplan]:
    """Load subject property floorplan summary from standardized CSV.

    Expected columns: PlanCode, BedType, Units, SqFt, AvgMarketRent
    """
    plans = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            plans.append(SubjectFloorplan(
                plan_code=row.get("PlanCode", ""),
                bed_type=row.get("BedType", ""),
                units=int(row.get("Units", 0)),
                sqft=int(float(row.get("SqFt", 0))),
                avg_market_rent=float(row.get("AvgMarketRent", 0)),
            ))
    return plans


def find_latest_comp_snapshot(
    metro_slug: str,
    data_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Find the most recent comp snapshot JSON for a metro."""
    if data_dir is None:
        data_dir = MARKET_AGENT_ROOT / "data" / "public" / "processed" / "comps"

    if not data_dir.exists():
        return None

    # Files named: YYYY-MM-DD_<slug>_comps_snapshot.json
    matches = sorted(
        data_dir.glob(f"*_{metro_slug}_comps_snapshot.json"),
        reverse=True,
    )
    return matches[0] if matches else None


def find_floorplan_summary(
    metro_slug: str,
    property_slug: str,
    reports_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Find the floorplan summary CSV for a subject property."""
    if reports_dir is None:
        reports_dir = MARKET_AGENT_ROOT / "reports"

    # Try hyphenated directory structure: reports/<metro-slug>/<prop-slug>/rent-roll/clean/
    metro_dir_slug = metro_slug.replace("_", "-")
    prop_dir_slug = property_slug.replace("_", "-")

    csv_path = (
        reports_dir / metro_dir_slug / prop_dir_slug
        / "rent-roll" / "clean" / "floorplan_summary.csv"
    )
    return csv_path if csv_path.exists() else None


# ── Analysis ──────────────────────────────────────────────────────────────


def comp_comparison(
    subject_plans: List[SubjectFloorplan],
    comps: List[CompProperty],
) -> List[CompComparison]:
    """Compare subject property rents against comps by bed type.

    Groups both subject plans and comp floorplans by bed count,
    computes average rents and $/SF, and calculates premium/discount.
    """
    # Map bed type strings to numeric bed counts
    bed_type_map = {"Studio": 0, "0BR": 0, "1BR": 1, "2BR": 2, "3BR": 3, "4BR": 4}

    # Group subject plans by bed count
    subject_by_beds: Dict[int, List[SubjectFloorplan]] = {}
    for plan in subject_plans:
        beds = bed_type_map.get(plan.bed_type, -1)
        if beds >= 0:
            subject_by_beds.setdefault(beds, []).append(plan)

    # Group comp floorplans by bed count
    comp_by_beds: Dict[int, List[FloorplanComp]] = {}
    for comp in comps:
        for fp in comp.floorplans:
            if fp.beds >= 0 and fp.rent_min is not None:
                comp_by_beds.setdefault(fp.beds, []).append(fp)

    results = []
    bed_labels = {0: "Studio", 1: "1BR", 2: "2BR", 3: "3BR", 4: "4BR"}

    for beds in sorted(set(subject_by_beds.keys()) & set(comp_by_beds.keys())):
        s_plans = subject_by_beds[beds]
        c_plans = comp_by_beds[beds]

        # Subject weighted averages (by unit count)
        total_s_units = sum(p.units for p in s_plans)
        s_avg_rent = (
            sum(p.avg_market_rent * p.units for p in s_plans) / total_s_units
            if total_s_units else 0
        )
        s_avg_sf = (
            int(sum(p.sqft * p.units for p in s_plans) / total_s_units)
            if total_s_units else 0
        )

        # Comp averages (simple average across floorplans)
        c_rents = []
        c_sfs = []
        for fp in c_plans:
            avg_rent = (
                (fp.rent_min + fp.rent_max) / 2
                if fp.rent_min and fp.rent_max
                else fp.rent_min or fp.rent_max or 0
            )
            if avg_rent > 0:
                c_rents.append(avg_rent)
            if fp.sqft > 0:
                c_sfs.append(fp.sqft)

        c_avg_rent = sum(c_rents) / len(c_rents) if c_rents else 0
        c_avg_sf = int(sum(c_sfs) / len(c_sfs)) if c_sfs else 0

        s_rent_psf = s_avg_rent / s_avg_sf if s_avg_sf else 0
        c_rent_psf = c_avg_rent / c_avg_sf if c_avg_sf else 0

        premium = (s_avg_rent - c_avg_rent) / c_avg_rent if c_avg_rent else 0

        results.append(CompComparison(
            bed_type=bed_labels.get(beds, f"{beds}BR"),
            subject_avg_rent=round(s_avg_rent, 0),
            subject_avg_sf=s_avg_sf,
            subject_rent_per_sf=round(s_rent_psf, 2),
            comp_avg_rent=round(c_avg_rent, 0),
            comp_avg_sf=c_avg_sf,
            comp_rent_per_sf=round(c_rent_psf, 2),
            rent_premium_pct=round(premium, 4),
            comp_count=len(c_rents),
        ))

    return results


def suggest_rent_growth_adjustment(
    comparisons: List[CompComparison],
    base_growth: float = 0.03,
) -> float:
    """Suggest an adjusted rent growth rate based on comp positioning.

    If subject rents are below market (negative premium), there may be
    upside for faster rent growth (lease-up / value-add).
    If subject is above market, growth may be constrained.
    """
    if not comparisons:
        return base_growth

    # Weighted average premium across bed types (by comp count)
    total_comps = sum(c.comp_count for c in comparisons)
    if total_comps == 0:
        return base_growth

    wtd_premium = sum(
        c.rent_premium_pct * c.comp_count for c in comparisons
    ) / total_comps

    # Adjustment logic:
    # - Below market (premium < 0): boost growth by up to 100bps
    # - Above market (premium > 0): reduce growth by up to 50bps
    if wtd_premium < 0:
        # Below market → potential for catch-up growth
        adjustment = min(0.01, abs(wtd_premium) * 0.5)
        return round(base_growth + adjustment, 4)
    else:
        # Above market → growth constrained
        adjustment = min(0.005, wtd_premium * 0.3)
        return round(base_growth - adjustment, 4)


# ── MarketContext (High-Level API) ────────────────────────────────────────


class MarketContext:
    """High-level market context for a deal, loaded from market-study-agent data."""

    def __init__(
        self,
        subject_plans: Optional[List[SubjectFloorplan]] = None,
        comps: Optional[List[CompProperty]] = None,
        metro: str = "",
        property_name: str = "",
    ):
        self.subject_plans = subject_plans or []
        self.comps = comps or []
        self.metro = metro
        self.property_name = property_name

    @classmethod
    def from_config(
        cls,
        config_slug: str,
        agent_root: Optional[Path] = None,
    ) -> MarketContext:
        """Load market context from a market-study-agent config file.

        Args:
            config_slug: Config filename without extension (e.g., "dallas_tx_forest_hills")
            agent_root: Path to market-study-agent repo root
        """
        root = agent_root or MARKET_AGENT_ROOT
        config_path = root / "agents" / "configs" / f"{config_slug}.yaml"

        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")

        try:
            import yaml
        except ImportError:
            raise ImportError("PyYAML required: pip install pyyaml")

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        metro = config.get("metro", "")
        notes = config.get("notes", {})
        subject = notes.get("subject_property", {})
        property_name = subject.get("name", "")
        metro_slug = notes.get("metro_slug", config_slug)

        # Derive directory slugs
        parts = config_slug.split("_")
        # e.g., dallas_tx_forest_hills → metro_slug=dallas-tx, prop_slug=forest-hills
        if len(parts) >= 3:
            metro_dir = "-".join(parts[:2])
            prop_dir = "-".join(parts[2:])
        else:
            metro_dir = "-".join(parts)
            prop_dir = ""

        # Load subject floorplan data
        subject_plans = []
        fp_path = find_floorplan_summary(metro_dir, prop_dir, root / "reports")
        if fp_path:
            subject_plans = load_floorplan_summary(fp_path)

        # Load latest comp snapshot
        comps = []
        snapshot = find_latest_comp_snapshot(metro_slug, root / "data" / "public" / "processed" / "comps")
        if snapshot:
            comps = load_comp_snapshot(snapshot)

        return cls(
            subject_plans=subject_plans,
            comps=comps,
            metro=metro,
            property_name=property_name,
        )

    @classmethod
    def from_files(
        cls,
        floorplan_csv: Optional[Path] = None,
        comp_snapshot_json: Optional[Path] = None,
        metro: str = "",
        property_name: str = "",
    ) -> MarketContext:
        """Load from explicit file paths."""
        subject_plans = load_floorplan_summary(floorplan_csv) if floorplan_csv else []
        comps = load_comp_snapshot(comp_snapshot_json) if comp_snapshot_json else []
        return cls(subject_plans, comps, metro, property_name)

    def comp_comparison(self) -> List[CompComparison]:
        """Generate bed-type-level comp comparison."""
        return comp_comparison(self.subject_plans, self.comps)

    def comp_summary(self) -> Dict[str, Any]:
        """Generate a summary dict for reporting."""
        comparisons = self.comp_comparison()
        return {
            "metro": self.metro,
            "property_name": self.property_name,
            "subject_plans": len(self.subject_plans),
            "comp_properties": len(self.comps),
            "snapshot_date": self.comps[0].snapshot_date if self.comps else None,
            "by_bed_type": [
                {
                    "bed_type": c.bed_type,
                    "subject_rent": c.subject_avg_rent,
                    "subject_sf": c.subject_avg_sf,
                    "subject_rent_psf": c.subject_rent_per_sf,
                    "comp_rent": c.comp_avg_rent,
                    "comp_sf": c.comp_avg_sf,
                    "comp_rent_psf": c.comp_rent_per_sf,
                    "premium_pct": c.rent_premium_pct,
                    "comp_count": c.comp_count,
                }
                for c in comparisons
            ],
        }

    def suggested_rent_growth(self, base_growth: float = 0.03) -> float:
        """Suggest rent growth adjustment based on comp positioning."""
        comparisons = self.comp_comparison()
        return suggest_rent_growth_adjustment(comparisons, base_growth)

    def apply_rent_adjustments(
        self, inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return a copy of engine inputs with comp-adjusted rent growth.

        Non-destructive: returns a new dict, doesn't modify original.
        """
        import copy
        modified = copy.deepcopy(inputs)

        base_rg = modified.get("growth_assumptions", {}).get("annual_growth_rate", 0.03)
        adjusted_rg = self.suggested_rent_growth(base_rg)

        ga = modified.setdefault("growth_assumptions", {})
        ga["annual_growth_rate"] = adjusted_rg
        ga["comp_adjusted"] = True
        ga["original_growth_rate"] = base_rg

        return modified


# ── Comp-Reconciler Bridge (Wave 6 Task 6.3) ──────────────────────────────


# Default blend weight for the "use_blended" recommendation. Documented on the
# function signature so downstream callers can override per call.
_DEFAULT_BLEND_WEIGHT_COMP = 0.5


def apply_market_calibration_to_canonical(
    canonical: Dict[str, Any],
    calibration: List[Dict[str, Any]],
    *,
    blend_weight_comp: float = _DEFAULT_BLEND_WEIGHT_COMP,
) -> Dict[str, Any]:
    """Apply a comp-reconciler's calibration to a canonical's market_rent_curve.

    This is the public bridge that plat-agent's comp-reconciler step calls to
    fold comp-finder evidence back into the canonical inputs before
    underwriting-runner runs. Per the Stage 7 audit (CRITICAL-2), prior to this
    function the only API touching market_rent_curve was
    ``MarketContext.apply_rent_adjustments`` which had zero callers in the
    federation chain. ``apply_market_calibration_to_canonical`` is now the
    publicly-callable entry point.

    Parameters
    ----------
    canonical:
        Canonical engine inputs dict (validated against deal_schema_v0_1.json).
        ``market_rent_curve`` rows have shape
        ``{"cohort_id", "start_period", "end_period", "market_rent"}``.
    calibration:
        List of per-cohort calibration entries from the comp-reconciler. Each
        entry has shape::

            {
                "cohort_id": <str>,
                "comp_p50_rent": <number>,            # comp median rent
                "recommendation": "use_canonical" | "use_comp" | "use_blended",
            }
    blend_weight_comp:
        Weight applied to ``comp_p50_rent`` when ``recommendation == "use_blended"``.
        The canonical receives weight ``1 - blend_weight_comp``. Default 0.5
        (50/50 blend).

    Returns
    -------
    A *new* canonical dict with ``market_rent_curve`` segments updated for any
    cohort where the recommendation says to use comp or blended evidence. Input
    is never mutated. Cohorts with ``recommendation == "use_canonical"`` and
    cohorts with no calibration entry are left untouched.
    """
    import copy

    if blend_weight_comp < 0 or blend_weight_comp > 1:
        raise ValueError(
            f"blend_weight_comp must be in [0, 1]; got {blend_weight_comp}"
        )

    updated = copy.deepcopy(canonical)
    by_cohort: Dict[str, Dict[str, Any]] = {}
    for entry in calibration or []:
        cid = entry.get("cohort_id")
        if cid is None:
            continue
        by_cohort[cid] = entry

    if not by_cohort:
        return updated

    new_rows: List[Dict[str, Any]] = []
    for row in updated.get("market_rent_curve", []) or []:
        cid = row.get("cohort_id")
        cal = by_cohort.get(cid)
        if cal is None:
            new_rows.append(row)
            continue

        recommendation = cal.get("recommendation", "use_canonical")
        comp_p50 = cal.get("comp_p50_rent")

        if recommendation == "use_canonical":
            new_rows.append(row)
            continue

        if comp_p50 is None:
            # Defensive: a recommendation that names comp evidence without
            # supplying it falls back to canonical (no silent corruption).
            new_rows.append(row)
            continue

        canonical_rent = row.get("market_rent")
        new_row = dict(row)

        if recommendation == "use_comp":
            new_row["market_rent"] = comp_p50
        elif recommendation == "use_blended":
            if canonical_rent is None:
                new_row["market_rent"] = comp_p50
            else:
                w = blend_weight_comp
                new_row["market_rent"] = (
                    comp_p50 * w + canonical_rent * (1 - w)
                )
        else:
            # Unknown recommendation: leave canonical untouched, add a marker.
            new_rows.append(row)
            continue

        new_row["market_rent_provenance"] = {
            "source": recommendation,
            "comp_p50_rent": comp_p50,
            "canonical_rent": canonical_rent,
            "blend_weight_comp": blend_weight_comp if recommendation == "use_blended" else None,
        }
        new_rows.append(new_row)

    updated["market_rent_curve"] = new_rows
    return updated
