from decimal import Decimal
from engine.modules.metrics import find_stabilized_year


def _yr(noi=1.0, vacancy_rate=0.05):
    return {"noi": Decimal(str(noi)), "vacancy_rate": Decimal(str(vacancy_rate))}


def test_returns_year_1_when_already_stabilized():
    cf = [_yr(vacancy_rate=0.05), _yr(vacancy_rate=0.05), _yr(vacancy_rate=0.05)]
    capex_by_year = [Decimal("100"), Decimal("100"), Decimal("100")]
    total_basis = Decimal("10000")  # capex < 5% of basis (=500)
    steady_state_vac = Decimal("0.05")
    assert find_stabilized_year(cf, capex_by_year, total_basis, steady_state_vac) == 1


def test_returns_year_2_when_y1_capex_too_high():
    cf = [_yr(vacancy_rate=0.05), _yr(vacancy_rate=0.05), _yr(vacancy_rate=0.05)]
    capex_by_year = [Decimal("600"), Decimal("100"), Decimal("100")]  # Y1 capex 6% > 5%
    total_basis = Decimal("10000")
    assert find_stabilized_year(cf, capex_by_year, total_basis, Decimal("0.05")) == 2


def test_returns_year_3_when_y1_y2_vacancy_too_high():
    cf = [_yr(vacancy_rate=0.10), _yr(vacancy_rate=0.07), _yr(vacancy_rate=0.05)]
    capex_by_year = [Decimal("100"), Decimal("100"), Decimal("100")]
    total_basis = Decimal("10000")
    assert find_stabilized_year(cf, capex_by_year, total_basis, Decimal("0.05")) == 3


def test_falls_back_to_year_2_when_no_year_qualifies_by_year_3():
    cf = [_yr(vacancy_rate=0.10), _yr(vacancy_rate=0.10), _yr(vacancy_rate=0.10)]
    capex_by_year = [Decimal("600"), Decimal("600"), Decimal("600")]
    total_basis = Decimal("10000")
    assert find_stabilized_year(cf, capex_by_year, total_basis, Decimal("0.05")) == 2


def test_tolerance_band_half_percent_above_steady_state_still_counts():
    # vacancy 0.054 vs steady-state 0.05 → within +0.5pp tolerance, should qualify
    cf = [_yr(vacancy_rate=0.054)]
    capex_by_year = [Decimal("100")]
    total_basis = Decimal("10000")
    assert find_stabilized_year(cf, capex_by_year, total_basis, Decimal("0.05")) == 1


def test_handles_short_series_gracefully():
    # Only 1 year available; doesn't qualify; fallback to 2 even though Y2 doesn't exist
    cf = [_yr(vacancy_rate=0.10)]
    capex_by_year = [Decimal("100")]
    assert find_stabilized_year(cf, capex_by_year, Decimal("10000"), Decimal("0.05")) == 2
