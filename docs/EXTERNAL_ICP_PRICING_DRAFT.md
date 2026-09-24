# EXTERNAL ICP AND PRICING — DRAFT

> **STATUS: DRAFT — ALL ASSUMPTIONS UNVALIDATED**
> This document is operator thinking material only. It is not a commitment,
> not a published rate card, and contains no validated market data. Nothing
> here should be shared externally or treated as a product decision.
> Filed for async review; update or discard as strategy firms up.

---

## Context: What the Tool Actually Does

The `plat-multifamily-underwriting` engine is a multifamily real-estate
investment underwriting platform. Its core capabilities (as implemented):

- Accepts a structured deal-input payload (canonical schema v0.1) covering
  unit cohorts, rent/vacancy/opex curves, debt terms, and fund waterfall
  assumptions.
- Runs a full monthly cashflow model: revenue, operating expenses, debt service,
  reserves, distributions.
- Produces key investment metrics: levered/unlevered IRR, equity multiple, DSCR,
  cap rate at entry/exit, cash-on-cash by year.
- Supports renovation programs, sensitivity scenarios, custom scenario variants,
  portfolio aggregation, stress testing, and CRM publishing.
- Exposes the engine via 14 Azure Function HTTP endpoints (13 auth-gated
  business endpoints plus an anonymous health check) and an
  MCP tool server for AI-agent workflows.
- Accepts deal inputs from Excel workbooks (via RediQ bridge) or structured JSON.

This is an *investment analysis computation layer*, not a property data or
market-data provider. Buyers would be bringing their own deal data and paying
for the computation, structuring discipline, and consistent output artifacts.

---

## ICP Evaluation

The suggestion in the fleet cycle notes proposed: *acquisition teams,
syndicators, family offices.*

Evaluated against what the tool actually produces:

### Candidate A — Small/Mid-Market Syndicators (best fit)
- **Why fit:** Syndicators underwrite many deals per year on tight timelines.
  They need consistent, audit-ready output artifacts for LP decks and lender
  packages. The waterfall, IRR, and equity-multiple modules are core to their
  workflow. The Excel integration (RediQ-style workbooks) maps well to how this
  audience already works.
- **Pain it solves:** Excel model sprawl, inconsistent run-to-run assumptions,
  no CI on underwriting logic, no provenance on extracted OM figures.
- **Friction:** These buyers are price-sensitive and often have a "we built it
  ourselves" incumbent. The ICP works only if the tool is materially faster or
  more trusted than their in-house model.

### Candidate B — Institutional Acquisition Teams (moderate fit)
- **Why fit:** Need repeatable underwriting for high deal volume. Portfolio
  aggregation, stress testing, and programmatic scenario variants are valuable
  at scale. The Azure Functions API surface is compatible with integrating into
  a larger deal-management workflow.
- **Friction:** Institutional buyers typically have in-house platforms, long
  security review cycles, and strict data-residency requirements. The current
  tool stores deal data in Azure Cosmos and OneDrive — institutional compliance
  requirements may not be met without significant hardening (see SECURITY.md).
  Selling cycle is long; not a first-market candidate.

### Candidate C — Family Offices / High-Net-Worth Investors (weak fit)
- **Why fit:** Some family offices underwrite 5–20 deals per year and lack
  dedicated technology infrastructure.
- **Friction:** This segment is heterogeneous and hard to reach at scale. The
  tool's schema-first, API-centric design is over-engineered for a buyer who
  wants a simple spreadsheet. The OM extraction (LLM pipeline) could be
  compelling if positioned as "upload the OM, get the model" — but that
  requires more front-end surface than currently exists.

### Recommendation (operator's call)
Start with **Candidate A (syndicators)** as the first market: highest pain,
clearest use case, reachable via industry conferences and LP networks.
Candidate B becomes relevant only after achieving multi-tenant, SOC 2-adjacent
security posture. Candidate C requires a consumer-grade front end that does not
currently exist.

---

## Pricing Model Options

All figures below are illustrative placeholders — no market benchmarking has
been done.

### Option 1 — Per-Run (usage-based)
**Structure:** Charge per underwriting run (each call to `POST /run`).

| Tier | Runs/month | Unit price | Notes |
|------|-----------|------------|-------|
| Trial | 10 | Free | Time-limited onboarding |
| Pay-as-you-go | Unlimited | $X / run | Single rate, no commitment |
| Volume | 200–500 | $Y / run (<X) | Discount bracket |

**Pros:**
- Zero commitment lowers buyer friction at entry.
- Revenue scales with actual usage.
- Easy to instrument; each Azure Function invocation maps cleanly to a billable event.

**Cons:**
- Unpredictable revenue.
- Syndicators may batch runs and complain about cost on high-volume months.
- Incentivizes buyers to minimize runs, which limits product engagement.

**Best for:** Early customers with variable deal flow who resist monthly commits.

---

### Option 2 — Seat/Team Subscription
**Structure:** Monthly or annual subscription per analyst seat, with unlimited runs.

| Tier | Seats | Features | Price/mo (illustrative) |
|------|-------|----------|------------------------|
| Solo | 1 | Core engine, Excel I/O, 1 portfolio | $A |
| Team | Up to 5 | + CRM publishing, portfolio dashboard, stress testing | $B |
| Firm | Unlimited | + custom scenarios, API access, priority support | $C |

**Pros:**
- Predictable MRR.
- Aligns with how syndicators buy software (per-user SaaS).
- Encourages heavy usage (no per-run friction), which drives retention.

**Cons:**
- Harder to justify for buyers with only occasional deal flow.
- Seat expansion is the growth lever — requires a sales motion.

**Best for:** Syndicators with a dedicated acquisitions team running 20+ deals/year.

---

### Option 3 — Output-Artifact Licensing
**Structure:** Charge per published output artifact (LP-ready PDF report,
branded Excel model, CRM record).

**Rationale:** The tool already generates distinct, high-value deliverables
(PPTX decks, Excel workbooks, CRM pushes). Pricing on the artifact rather
than the compute aligns cost with perceived value — the buyer pays when they
produce something they give to an LP or lender.

**Pros:**
- Directly tied to value delivered (artifact used externally = monetizable event).
- Low barrier: free to underwrite internally, pay only when publishing.

**Cons:**
- Hard to enforce; artifacts are files that can be copied.
- Requires instrumentation at the publish/export layer.
- Unusual model for SaaS — may confuse buyers.

**Best for:** A freemium wedge strategy where internal analysis is free and
external-facing output is the upgrade gate.

---

## Recommendation (operator's call)

None of these is definitively correct without talking to 5–10 potential
buyers. The implementation-level bet is: **Option 2 (seat subscription)**
for a first commercial offering, with **Option 1 (per-run)** available for
API-only integrators who want programmatic access. Option 3 could be layered
as a future "branded report" add-on.

Before any external launch, the prerequisites from the fleet cycle audit apply:
- SECURITY.md compliance (Tier A data handling)
- RUNBOOK.md completeness
- Auditable deployment pipeline
- Multi-tenant data isolation (currently not implemented)

---

*Last updated: 2026-07-02. Owner: operator. Do not distribute.*
