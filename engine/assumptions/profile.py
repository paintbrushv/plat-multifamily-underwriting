"""Deal profile: the context that drives assumption defaults (ADR 0003 §1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

SUPPORTED_DEAL_TYPES = ("stabilized", "value_add")
# Reserved for later profiles. The resolver and validator reject them until
# those profiles exist.
RESERVED_DEAL_TYPES = ("lease_up", "distressed")
DEFAULT_DEAL_TYPE = "stabilized"

# deal_type -> house underwriting strategy (engine/underwriting_policy.py).
DEAL_TYPE_STRATEGY = {
    "stabilized": "cashflow",
    "value_add": "value_add",
}

ASSET_CLASSES = ("A", "B", "C")
STAR_TIERS = ("1_2", "3", "4_5")
# asset_class -> the CoStar star tier market data is read at when the profile
# names no star_tier (class A ~ 4 & 5 Star, B ~ 3 Star, C ~ 1 & 2 Star).
ASSET_CLASS_STAR_TIER = {"A": "4_5", "B": "3", "C": "1_2"}
_MSA_FIELDS = ("cbsa_code", "name", "metro_slug")
_SUBMARKET_FIELDS = ("name", "source_id")


class UnsupportedDealTypeError(ValueError):
    """Raised for a deal_type the assumption layer does not support yet."""

    code = "DEAL_TYPE_UNSUPPORTED"


def unsupported_deal_type_message(deal_type: Any) -> str:
    supported = ", ".join(SUPPORTED_DEAL_TYPES)
    if deal_type in RESERVED_DEAL_TYPES:
        return (
            f"deal_type {deal_type!r} is reserved for a later profile and is not supported yet "
            f"(ADR 0003 §1). Supported deal types: {supported}."
        )
    return f"deal_type {deal_type!r} is not a known deal type. Supported deal types: {supported}."


def check_deal_type(deal_type: Any) -> str:
    if deal_type not in SUPPORTED_DEAL_TYPES:
        raise UnsupportedDealTypeError(unsupported_deal_type_message(deal_type))
    return deal_type


def _clean_mapping(value: Any, fields: tuple[str, ...], label: str) -> Optional[dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"deal_profile.{label} must be an object")
    unknown = set(value) - set(fields)
    if unknown:
        raise ValueError(f"deal_profile.{label} has unknown fields: {sorted(unknown)}")
    cleaned = {
        field: str(value[field])
        for field in fields
        if value.get(field) not in (None, "")
    }
    return cleaned or None


@dataclass(frozen=True)
class DealProfile:
    deal_type: str = DEFAULT_DEAL_TYPE
    msa: Optional[dict[str, str]] = None
    submarket: Optional[dict[str, str]] = None
    year_built: Optional[int] = None
    asset_class: Optional[str] = None
    star_tier: Optional[str] = None

    def __post_init__(self) -> None:
        check_deal_type(self.deal_type)
        object.__setattr__(self, "msa", _clean_mapping(self.msa, _MSA_FIELDS, "msa"))
        object.__setattr__(
            self, "submarket", _clean_mapping(self.submarket, _SUBMARKET_FIELDS, "submarket")
        )
        if self.year_built is not None and (
            isinstance(self.year_built, bool) or not isinstance(self.year_built, int)
        ):
            raise ValueError("deal_profile.year_built must be an integer year or null")
        if self.asset_class is not None and self.asset_class not in ASSET_CLASSES:
            raise ValueError(f"deal_profile.asset_class must be one of {ASSET_CLASSES}")
        if self.star_tier is not None and self.star_tier not in STAR_TIERS:
            raise ValueError(f"deal_profile.star_tier must be one of {STAR_TIERS}")
        if self.star_tier is None and self.asset_class is not None:
            # Recorded on the profile, so to_dict() and later resolutions carry it.
            object.__setattr__(self, "star_tier", ASSET_CLASS_STAR_TIER[self.asset_class])

    @property
    def strategy(self) -> str:
        """The house underwriting strategy for this deal type."""
        return DEAL_TYPE_STRATEGY[self.deal_type]

    def geo(self) -> dict[str, Any]:
        """Geography handed to a market-data source; missing fields are omitted."""
        geo: dict[str, Any] = {}
        if self.msa:
            geo["msa"] = dict(self.msa)
        if self.submarket:
            geo["submarket"] = dict(self.submarket)
        if self.star_tier:
            geo["star_tier"] = self.star_tier
        return geo

    def with_year_built(self, year_built: Optional[int]) -> "DealProfile":
        if self.year_built is not None or year_built is None:
            return self
        return DealProfile(
            deal_type=self.deal_type,
            msa=self.msa,
            submarket=self.submarket,
            year_built=year_built,
            asset_class=self.asset_class,
            star_tier=self.star_tier,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"deal_type": self.deal_type}
        if self.msa:
            out["msa"] = dict(self.msa)
        if self.submarket:
            out["submarket"] = dict(self.submarket)
        if self.year_built is not None:
            out["year_built"] = self.year_built
        if self.asset_class is not None:
            out["asset_class"] = self.asset_class
        if self.star_tier is not None:
            out["star_tier"] = self.star_tier
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DealProfile":
        if not isinstance(data, Mapping):
            raise ValueError("deal_profile must be an object")
        allowed = {"deal_type", "msa", "submarket", "year_built", "asset_class", "star_tier"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"deal_profile has unknown fields: {sorted(unknown)}")
        return cls(
            deal_type=data.get("deal_type", DEFAULT_DEAL_TYPE),
            msa=data.get("msa"),
            submarket=data.get("submarket"),
            year_built=data.get("year_built"),
            asset_class=data.get("asset_class"),
            star_tier=data.get("star_tier"),
        )

    @classmethod
    def from_inputs(
        cls,
        *,
        deal_type: str = DEFAULT_DEAL_TYPE,
        msa: Any = None,
        submarket: Any = None,
        metro_slug: Optional[str] = None,
        year_built: Optional[int] = None,
    ) -> "DealProfile":
        """Build a profile from CLI-style strings.

        ``msa`` may be a mapping, a five-digit CBSA code, or an MSA name.
        ``submarket`` may be a mapping or a submarket name.
        """
        msa_map: dict[str, str] = {}
        if isinstance(msa, Mapping):
            msa_map.update({k: v for k, v in msa.items() if v not in (None, "")})
        elif msa not in (None, ""):
            text = str(msa).strip()
            msa_map["cbsa_code" if text.isdigit() and len(text) == 5 else "name"] = text
        if metro_slug not in (None, ""):
            msa_map["metro_slug"] = str(metro_slug).strip()
        if isinstance(submarket, Mapping):
            submarket_map = dict(submarket)
        elif submarket not in (None, ""):
            submarket_map = {"name": str(submarket).strip()}
        else:
            submarket_map = None
        return cls(
            deal_type=deal_type,
            msa=msa_map or None,
            submarket=submarket_map,
            year_built=year_built,
        )
