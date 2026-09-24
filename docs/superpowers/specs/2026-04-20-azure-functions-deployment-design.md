# Azure Functions Deployment — Design Spec

**Date:** 2026-04-20
**Task:** 3.3 from `docs/plan.md`
**Status:** Approved

---

## Summary

Deploy the underwriting engine API to the existing Azure Function App (`func-stack-std-12345` in `rg-stack-dev`). Preserve all existing v1 functions (async run pipeline, custom scenarios). Add 5 new synchronous endpoints for engine execution, portfolio analytics, refi comparison, market context, and health. Provision Cosmos DB serverless for the persistence layer (completing Task 3.2 infrastructure). Establish `azure-functions/` as the canonical source directory in this repo.

---

## Existing Infrastructure (Preserved)

| Resource | Name | Purpose |
|----------|------|---------|
| Function App | `func-stack-std-12345` | Python 3.11, Linux, v1 model |
| Storage Account | `ststackdev12345` | Blob (stack-runs), Table (runindex), Queue (run-jobs) |
| App Insights | `appi-stack-dev` | Monitoring |
| App Service Plan | `ASP-rgstackdev-b86a` | Consumption plan |

### Existing Functions (Unchanged)

| Function | Route | Pattern |
|----------|-------|---------|
| `api_create_run` | `POST /api/runs` | Validate → queue for async |
| `worker_execute_run` | Queue trigger | Run engine → save outputs |
| `api_get_run` | `GET /api/runs/{run_id}` | Status lookup |
| `api_get_deal_run` | `GET /api/deals/{deal_id}/runs/{run_id}` | Point read |
| `api_list_deal_runs` | `GET /api/deals/{deal_id}/runs` | List runs |
| `api_get_run_outputs` | `GET /api/runs/{run_id}/outputs` | Return engine output JSON |
| `api_create_custom_scenario` | `POST /api/scenarios/custom` | Store client-side metrics |
| `api_list_custom_scenarios` | `GET /api/deals/{deal_id}/scenarios/custom` | List custom scenarios |

---

## New Functions

### `api_run_sync` — `POST /api/run`

Synchronous engine execution. Wraps `engine.api.handle_run_deal()`.

**Request:**
```json
{
  "inputs": {...},
  "options": {
    "scenarios": false,
    "scenario_type": "stabilized",
    "include_cashflow": false,
    "persist": false
  }
}
```

**Response (200):**
```json
{
  "status": "success",
  "deal_id": "...",
  "metrics": { "levered_irr": ..., "unlevered_irr": ..., ... },
  "cashflow_summary": { "years": 5, "noi_year_1": ..., "noi_exit": ... },
  "scenario_comparison": {...},
  "persisted": { "id": "...", "run_id": "..." },
  "elapsed_seconds": 1.2
}
```

Use case: Quick deals from CLI/Excel plugin where results are needed immediately.

### `api_portfolio` — `POST /api/portfolio`

Portfolio analytics. Wraps `engine.api.handle_portfolio_summary()` with added Cosmos source.

**Request:**
```json
{
  "cosmos_deal_ids": ["deal_001", "deal_002"],
  "filters": { "metro": "DFW" }
}
```

Or inline:
```json
{
  "deals": [{"inputs": {...}, "results": {...}}],
  "filters": {}
}
```

**Response (200):**
```json
{
  "status": "success",
  "summary": {...},
  "by_metro": {...},
  "by_vintage": {...},
  "deals": [...]
}
```

### `api_refi` — `POST /api/refi`

Refi-vs-sell comparison. Wraps `engine.api.handle_refi_vs_sell()`.

**Request:**
```json
{
  "inputs": {...},
  "refi_year": 3,
  "sell_year": 5,
  "refi_loan_terms": {...}
}
```

**Response (200):**
```json
{
  "status": "success",
  "comparison": {...},
  "elapsed_seconds": 0.8
}
```

### `api_market` — `GET /api/market/{slug}`

Market context and comp analysis. Wraps `engine.api.handle_market_context()`.

**Response (200):**
```json
{
  "status": "success",
  "summary": {...},
  "suggested_rent_growth": {...},
  "elapsed_seconds": 0.1
}
```

### `api_health` — `GET /api/health`

Health check. Verifies storage connectivity. Does not import engine (fast cold start).

**Response (200):**
```json
{
  "status": "healthy",
  "engine_version": "0.1.0",
  "schema_version": "0.1",
  "timestamp": "2026-04-20T19:00:00+00:00",
  "storage": "connected",
  "cosmos": "connected"
}
```

---

## Cosmos DB Provisioning

| Property | Value |
|----------|-------|
| Account name | `cosmos-stack-dev` |
| Resource group | `rg-stack-dev` |
| Capacity mode | Serverless |
| Database | `underwriting` |
| Container | `deal_runs` |
| Partition key | `/deal_id` |
| Estimated cost | <$5/mo at current scale |

App settings added to Function App:
- `COSMOS_ENDPOINT` — account endpoint URL
- `COSMOS_KEY` — primary key

Integrates with existing `engine/persistence.py` (`CosmosStore` class) which already expects these env vars.

---

## Repository Structure

```
azure-functions/
├── host.json
├── local.settings.json.example
├── requirements.txt
├── .funcignore
├── shared/
│   ├── __init__.py
│   ├── http_json.py
│   ├── run_ids.py
│   ├── settings.py          (extended: optional cosmos_endpoint, cosmos_key)
│   ├── storage.py
│   └── vendor.py
├── scripts/
│   ├── vendor_engine.sh     (copy engine/ → .vendor/engine/)
│   ├── package.sh           (build deployment zip)
│   ├── deploy.sh            (vendor + package + az deploy)
│   ├── provision_cosmos.sh  (one-time Cosmos setup)
│   └── smoke_test.sh        (post-deploy verification)
├── .vendor/                  (gitignored)
│
├── # Existing functions (preserved verbatim)
├── api_create_run/
├── api_get_run/
├── api_get_deal_run/
├── api_get_run_outputs/
├── api_list_deal_runs/
├── api_create_custom_scenario/
├── api_list_custom_scenarios/
├── worker_execute_run/
│
├── # New functions
├── api_run_sync/
├── api_portfolio/
├── api_refi/
├── api_market/
└── api_health/
```

---

## Deployment

**Scripts (bash, replacing existing PowerShell):**

- `vendor_engine.sh`: Copies `engine/` into `.vendor/engine/`. Excludes `__pycache__`, `.pyc`, tests. Also copies `engine/schemas/`.
- `package.sh`: Zips `azure-functions/` (with `.vendor/`) excluding `local.settings.json`, `scripts/`, `.git`.
- `deploy.sh`: Runs vendor → package → `az functionapp deployment source config-zip`.
- `provision_cosmos.sh`: One-time Cosmos account + database + container creation + app settings.

**requirements.txt:**
```
azure-functions==1.21.3
azure-storage-blob==12.25.1
azure-storage-queue==12.13.0
azure-data-tables==12.6.0
azure-cosmos>=4.7
jsonschema==4.25.1
openpyxl>=3.1
```

**No CI/CD auto-deploy** — deployment stays manual via `scripts/deploy.sh`. Can be added later.

---

## Authentication

None for this task. All endpoints use `authLevel: "anonymous"`. Auth will be layered separately as a follow-up (Azure AD EasyAuth or in-code JWT validation).

---

## Testing

**Unit tests** (`tests/test_azure_functions.py`): ~12 tests

- Health endpoint: returns version + healthy status
- `api_run_sync`: happy path (mocked engine), validation failure (400), persist option
- `api_portfolio`: inline deals, cosmos_deal_ids path
- `api_refi`: happy path
- `api_market`: happy path, missing slug (400)
- CORS preflight → 204

Tests construct mock `azure.functions.HttpRequest` objects and call `main()` directly. No live Azure required.

**Smoke test** (`scripts/smoke_test.sh`): Post-deploy curl verification against live endpoints.

---

## Non-Goals

- No Azure AD auth (separate follow-up)
- No file generation endpoints (PDF/PPTX/XLSX upload to Blob — future task)
- No CI/CD auto-deploy
- No changes to existing function behavior
- No migration of existing Table Storage data to Cosmos (both coexist)

---

## Key Decisions

1. **v1 model preserved**: Folder-per-function with `function.json`. Consistent with existing deployed code.
2. **Additive only**: Existing 8 functions unchanged. 5 new functions added alongside.
3. **Cosmos serverless**: Pay-per-request avoids fixed cost. Scale is trivial (<200 docs).
4. **Sync endpoints**: New functions return results inline. The async queue pattern already exists for heavy workloads via `api_create_run`.
5. **Engine vendored at deploy time**: Same pattern as existing — `shared/vendor.py` patches `sys.path`.
6. **Bash scripts**: Replace PowerShell for macOS dev environment.
7. **Dual persistence**: Table Storage (run index, status) + Cosmos (full inputs/results, versioned). Both serve different query patterns.
