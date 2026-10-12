"""The NOI basis for loan sizing and DSCR (ADR 0003 §3, ``loan_sizing_noi_basis``).

Agency lenders size a loan on underwritten net cash flow: NOI less the
replacement reserve, not NOI. The house sizes the DSCR constraint, and reports
DSCR, on NOI less the same below-NOI capex charge the cash flow deducts
(``max(recurring capex, lender reserve)``, carried on the ``replacement_reserves``
line; see ``engine/modules/capital_plan.py``). The basis is an overridable
assumption with three values (``engine/underwriting_policy.LOAN_SIZING_NOI_BASES``):

- ``ncf_after_capex_charge`` (house default): NOI - ``replacement_reserves``
  (the charge: max(recurring capex, lender reserve));
- ``ncf_after_lender_reserve``: NOI - the lender reserve only
  (``replacement_reserve_floor`` when a recurring capex budget is modeled,
  else ``replacement_reserves``, which is then the reserve);
- ``noi_before_capex``: NOI.

The same functions read a cash flow row (by month or by year) in the engine's
DSCR and in backsolve's agency sizing at every candidate price, so the loan is
sized on the cash flow its DSCR is reported on.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from engine import underwriting_policy as house
from engine.modules.util import dec

FIELD = "loan_sizing_noi_basis"
BASES = house.LOAN_SIZING_NOI_BASES
NCF_AFTER_CAPEX_CHARGE = house.LOAN_SIZING_NCF_AFTER_CAPEX_CHARGE
NCF_AFTER_LENDER_RESERVE = house.LOAN_SIZING_NCF_AFTER_LENDER_RESERVE
NOI_BEFORE_CAPEX = house.LOAN_SIZING_NOI_BEFORE_CAPEX

LABELS = {
    NCF_AFTER_CAPEX_CHARGE: "underwritten NCF: NOI less the below-NOI capex charge, max(recurring capex, lender reserve)",
    NCF_AFTER_LENDER_RESERVE: "underwritten NCF: NOI less the lender replacement reserve",
    NOI_BEFORE_CAPEX: "NOI (before capex)",
}
# Short labels for tables and the CLI.
SHORT_LABELS = {
    NCF_AFTER_CAPEX_CHARGE: "NCF after capex charge",
    NCF_AFTER_LENDER_RESERVE: "NCF after lender reserve",
    NOI_BEFORE_CAPEX: "NOI before capex",
}


def resolve_basis(value: Any) -> str:
    """``value`` validated, or the house default when it is None."""
    if value is None:
        return house.HOUSE_LOAN_SIZING_NOI_BASIS
    if value not in BASES:
        raise ValueError(f"{FIELD} must be one of {', '.join(BASES)}, got {value!r}")
    return str(value)


def basis_from_inputs(inputs: Mapping[str, Any]) -> str:
    """The canonical's ``loan_sizing_noi_basis``, else the house default."""
    return resolve_basis(inputs.get(FIELD))


def below_noi_deduction(row: Mapping[str, Any], basis: str) -> Decimal:
    """What ``basis`` deducts from a cash flow row's NOI (0 for ``noi_before_capex``)."""
    basis = resolve_basis(basis)
    if basis == NOI_BEFORE_CAPEX:
        return Decimal("0")
    charge = dec(row.get("replacement_reserves", 0) or 0)
    if basis == NCF_AFTER_LENDER_RESERVE and row.get("replacement_reserve_floor") is not None:
        return dec(row["replacement_reserve_floor"])
    return charge


def sizing_cash_flow(row: Mapping[str, Any], basis: str) -> Decimal:
    """NOI less ``below_noi_deduction``: the cash flow DSCR and loan sizing use."""
    return dec(row.get("net_operating_income", 0) or 0) - below_noi_deduction(row, basis)


__all__ = [
    "BASES",
    "FIELD",
    "LABELS",
    "NCF_AFTER_CAPEX_CHARGE",
    "NCF_AFTER_LENDER_RESERVE",
    "NOI_BEFORE_CAPEX",
    "SHORT_LABELS",
    "basis_from_inputs",
    "below_noi_deduction",
    "resolve_basis",
    "sizing_cash_flow",
]
