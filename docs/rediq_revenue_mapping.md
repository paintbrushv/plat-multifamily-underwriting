# RedIQ → New Underwriting Engine
## Revenue & Other Income Mapping (1-for-1)

This document maps typical RedIQ export fields to the new underwriting
engine schema to ensure full parity (or improvement) in revenue modeling.

---

## 1. Base Rent Mapping

### RedIQ Fields
- Gross Market Rent
- In-Place Rent
- Loss to Lease
- Physical Vacancy %
- Concessions (if broken out)

### New Engine Tables

| RedIQ Concept        | New Engine Table          | Notes |
|---------------------|---------------------------|-------|
| Gross Market Rent   | MarketRentCurve           | Explicit, cohort-based |
| In-Place Rent       | Derived                   | Market × (1 − LTL) |
| Loss to Lease       | LossToLease               | Explicit % by cohort |
| Physical Vacancy %  | PhysicalVacancyCurve      | Units not occupied |
| Concessions         | RevenuePrograms (optional)| Modeled as program if needed |

---

## 2. Other Income / Revenue Programs

### Typical RedIQ Other Income Lines
- Washer/Dryer Rental
- Renter’s Insurance
- Carports / Parking
- Storage
- Pet Rent
- Utility Reimbursements
- Internet / Cable Bulk Billing
- Valet Trash

### New Engine Representation

Each RedIQ line item becomes **one Revenue Program**.

| RedIQ Line Item | New Engine Object |
|-----------------|------------------|
| W/D Rental | RevenuePrograms + AdoptionCurve |
| Renter’s Insurance | RevenuePrograms + AdoptionCurve |
| Carports | RevenuePrograms + Capacity + Adoption |
| Storage | RevenuePrograms + Capacity |
| Pet Rent | RevenuePrograms |
| Utilities (RUBS) | UtilityRecoveryRules |
| Internet | RevenuePrograms |
| Valet Trash | RevenuePrograms (+ optional cost) |

---

## 3. Lease-Up / Ramp Assumptions

### RedIQ Behavior
- Implicit ramp assumptions
- Often blended or opaque

### New Engine
- Explicit monthly AdoptionCurves per program
- Not constrained by lease turnover unless specified
- Can exceed turnover (e.g., mid-lease opt-in)

---

## 4. Collection Loss / Bad Debt

### RedIQ Fields
- Bad Debt
- Vacancy Loss (blended)

### New Engine Tables

| RedIQ Concept | New Engine Table | Notes |
|---------------|-----------------|-------|
| Bad Debt | CollectionLossCurve | % of billed dollars |
| Blended Vacancy | Split explicitly | Physical vs Collection |

This yields clearer NOI diagnostics.

---

## 5. Net Effective Gross Income (EGI)

### RedIQ
- Often aggregated early

### New Engine
