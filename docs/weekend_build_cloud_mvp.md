# Weekend Build Guide: Stack Run Service MVP (Azure Functions + Storage)

This is a staged, weekend-friendly plan to stand up the Stack Run Service described in `docs/azure_architecture.md`, without requiring you to hand over credentials. It is written so you can stop after any stage and still have a working milestone.

Scope (this weekend):
- Create the Stack Run Service that accepts canonical deal JSON, executes the engine asynchronously, and writes artifacts to Blob Storage.
- Add a run index so you can list prior runs.
- Keep authentication simple for the first end-to-end (function key), then optionally add Entra ID auth.

Out of scope (this weekend):
- Office Add-in + full Excel SSO (bigger lift; do next once the API is stable).
- Phase 3 exploration engine (only after the single-run service is reliable).

---

## What you will build

1) `POST /api/runs`:
- Accept canonical deal JSON (or canonical deal JSON missing `metadata.run_id`), server assigns `run_id`.
- Validate inputs via the engine validator.
- Persist `inputs.json` and `validation.json`.
- Enqueue a job to execute the run.
- Return `202` with `{deal_id, run_id, status}`.

2) Worker (queue-triggered):
- Read `inputs.json`.
- Run `engine.engine.run_underwriting`.
- Write `outputs.json` and `metadata.json`.
- Update run status in a Table Storage index row.

3) Read API:
- `GET /api/runs/{run_id}`: status and artifact paths.
- `GET /api/deals/{deal_id}/runs?top=50`: list recent runs (for "load prior run").

Artifacts per run (same as local, but in Blob):
- `inputs.json`
- `outputs.json`
- `validation.json`
- `metadata.json`

---

## Inputs you need to decide/provide

Fill these in before you start:

- Azure subscription id: `<SUBSCRIPTION_ID>`
- Azure tenant id (Entra): `<TENANT_ID>`
- Region: `<LOCATION>` (example: `eastus`)
- Resource group name: `<RG_NAME>` (example: `rg-stack-dev`)
- Storage account name (globally unique): `<STORAGE_NAME>` (example: `ststackdev12345`)
- Function app name (globally unique): `<FUNCAPP_NAME>` (example: `func-stack-dev-12345`)
- App Insights name (optional but recommended): `<APPINSIGHTS_NAME>`

Optional (if you enable Entra auth this weekend):
- Who should have access? (group name): `<ENTRA_GROUP_NAME>`

---

## Progress Tracker (repo)

- [x] Stage 0: local sanity (tests + local run)
- [x] Stage 1: Stack Run Service scaffolding in `azure-functions/`
- [x] Stage 2: smoke test prep (tools + packaging)
- [ ] Stage 3: Azure resources created and deployed

---

## Stage 0 (30-60 min): prerequisites and local sanity

Goal: prove your engine and schema validation are working locally before involving Azure.

Stage 0 checklist:
- [x] Run unit tests
- [x] Run the local Excel runner end-to-end (Path 3)
- [ ] Verify Power Query import in Excel (manual)

1) Run unit tests:
```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

2) Run from Excel locally (Path 3):
```powershell
Copy-Item excel\underwriting_template.xlsx runs\deals\_scratch_underwriting_template.xlsx -Force
python runs/run_from_excel.py --workbook runs\deals\_scratch_underwriting_template.xlsx --runs-root runs/deals
```

3) Confirm you can load outputs via Power Query:
- Follow `docs/excel_powerquery_setup.md`

If Stage 0 is flaky, fix that first; cloud should not be your debugger.

---

## Stage 1 (60-120 min): scaffold the Azure Functions wrapper in-repo

Goal: get a deployable function app skeleton that can run locally (or at least package cleanly).

Stage 1 checklist:
- [x] `azure-functions/` app skeleton exists (API + worker + shared helpers)
- [x] Packaging scripts exist (`azure-functions/scripts/vendor_engine.ps1`, `azure-functions/scripts/package.ps1`)
- [x] Build a deployable zip (`azure-functions/.build/functionapp.zip`)

Repo folders (committed):
- `azure-functions/` contains the function app wrapper code (not the engine itself).

Important note about Python imports:
- Azure Functions runs with the function app folder as the Python import root.
- The engine lives in the repo root (`engine/`).
- This guide assumes you will "vendor" (copy) the engine into the function app at build/deploy time via `azure-functions/scripts/vendor_engine.ps1` and `azure-functions/scripts/package.ps1`.

Deliverable after Stage 1:
- A new `azure-functions/` skeleton with:
  - `host.json`, `requirements.txt`, and function folders for API + worker
  - a build script that vendors the engine into a deployable folder

You will implement the code in Stage 2.

Repo status:
- This repo already contains the Stage 1 scaffolding.
- Next action is Stage 2 (smoke test locally or after deploy).

---

## Stage 2 (2-4 hours): implement the Stack run loop (function key auth)

Goal: end-to-end with function keys, async execution, and artifacts saved.

Why function keys first:
- It lets you validate storage + queue + execution quickly.
- Entra auth is absolutely the right direction, but it adds setup and token plumbing. Do it after the basics are working.

Stage 2 checklist:
- [x] Stack Run Service code exists under `azure-functions/`
- [x] `azure-functions/.build/functionapp.zip` builds successfully
- [x] Install Azure CLI + Functions Core Tools (winget)
- [ ] Deploy to Azure (Stage 3)
- [ ] Run smoke test against deployed app

Prereqs (install once, via winget):
```powershell
winget install --id Microsoft.AzureCLI --exact --accept-source-agreements --accept-package-agreements --silent
winget install --id Microsoft.Azure.FunctionsCoreTools --exact --accept-source-agreements --accept-package-agreements --silent
```

After installing, open a new PowerShell window (PATH refresh), or refresh PATH in your current session:
```powershell
$env:PATH = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')
```

If `az` fails with permission errors writing to `C:\Users\<you>\.azure`, set this env var (keeps Azure CLI config inside the repo):
```powershell
$env:AZURE_CONFIG_DIR = (Resolve-Path .).Path + "\\.azure"
```

Implementation checklist:
- API: create run, validate, persist `inputs.json` + `validation.json`, enqueue queue message
- Worker: execute engine, persist `outputs.json` + `metadata.json`, update status in Table
- Read endpoints: get run status, list deal runs

Repo status:
- This repo already contains the Stage 2 implementation under `azure-functions/`.
- Next action is to smoke test locally (optional) and/or deploy in Stage 3.

Milestone test:
- You can `POST /api/runs` with a known-good deal payload and get a `run_id`.
- Within ~seconds, the run transitions from `queued -> running -> success`.
- Blob contains all artifacts for that run.
- Listing runs returns the run you just created.

---

## Stage 3 (60-180 min): create Azure resources and deploy

Two paths:
- A) Azure CLI (fastest once installed)
- B) Portal (slower but less tooling)

If you are new to Azure Portal, follow: `docs/stage_3_portal_setup.md`.

### 3A) Azure CLI path (recommended)

1) Login and select subscription:
```powershell
$env:AZURE_CONFIG_DIR = (Resolve-Path .).Path + "\\.azure"
az login --tenant <TENANT_ID>
az account set --subscription <SUBSCRIPTION_ID>
```

2) Create resource group:
```powershell
az group create --name <RG_NAME> --location <LOCATION>
```

3) Create a Storage Account (Blob + Queue + Table):
```powershell
az storage account create `
  --name <STORAGE_NAME> `
  --resource-group <RG_NAME> `
  --location <LOCATION> `
  --sku Standard_LRS
```

4) Create a Function App (Consumption, Python):
```powershell
az functionapp create `
  --name <FUNCAPP_NAME> `
  --resource-group <RG_NAME> `
  --storage-account <STORAGE_NAME> `
  --consumption-plan-location <LOCATION> `
  --runtime python `
  --runtime-version 3.11 `
  --functions-version 4
```

5) (Recommended) Add Application Insights:
- Easiest in Portal, or attach via CLI if you prefer.

6) Configure Function App settings (names below are suggestions; match your code):
- `UNDERWRITING_RUNS_CONTAINER=stack-runs`
- `UNDERWRITING_RUN_QUEUE=run-jobs`
- `UNDERWRITING_RUN_INDEX_TABLE=runindex`
- `UNDERWRITING_RUNS_PREFIX=runs`

7) Deploy:

Option 1: Functions Core Tools (`func`):
```powershell
cd azure-functions
func azure functionapp publish <FUNCAPP_NAME> --python
```

Option 2: zip deploy (works well for CI later):
- Build a zip that contains `host.json` at the zip root (see `azure-functions/scripts/package.ps1`), then:
```powershell
az functionapp deployment source config-zip `
  --resource-group <RG_NAME> `
  --name <FUNCAPP_NAME> `
  --src <PATH_TO_ZIP>
```

## Smoke test (after deploy)

After you deploy the function app, run:

```powershell
PowerShell -File azure-functions/scripts/smoke_test_stack_run_service.ps1 `
  -BaseUrl "https://<YOUR_FUNCTION_APP_DEFAULT_HOSTNAME>" `
  -FunctionKey "<YOUR_FUNCTION_KEY>"
```

### 3B) Portal path

- Create resource group
- Create storage account
- Create function app (Python 3.11, consumption)
- Add Application Insights
- Set configuration values
- Deploy from VS Code or zip deploy

---

## Stage 4 (60-120 min): add Entra ID auth (team-ready)

Target: Entra SSO for 2+ colleagues, no shared secrets embedded in workbooks.

Weekend-friendly approach:
- Enable App Service Authentication ("EasyAuth") on the Function App.
- Restrict to your tenant.
- Then add authorization controls (group/app role) in code.

Steps (Portal-friendly):
1) Function App -> Authentication -> Add identity provider -> Microsoft
2) Choose "Express" to create an app registration (or "Advanced" if you have an existing one)
3) Require authentication for all requests
4) Test with your own user first

Authorization strategy (pick one):
- A) Entra Group membership: require user to be in `<ENTRA_GROUP_NAME>`
- B) App roles: add `Underwriting.Run` role and assign to users/groups

Pragmatic note:
- Excel SSO is easiest with an Office Add-in. Do not block backend progress on Excel auth plumbing.

---

## Stage 5 (optional, 2-4 hours): wire Excel to cloud (minimum viable)

You have three "weekend viable" options; pick one:

1) Keep Excel as the UI, but run the cloud service via a separate "Run" tool:
- A small local script that:
  - posts inputs to the cloud
  - waits for completion
  - downloads outputs into a local run folder
- Excel continues using Power Query from the local folder.

2) Power Query pulls from Blob (requires stable paths or signed URLs):
- Works best when you standardize output tables and keep artifacts small.

3) Office Add-in (best long-term, not always weekend-sized):
- Add-in acquires Entra token and calls the API.

For option (1), you can keep the non-technical UX almost identical to Path 3.

---

## Suggested docs to add (lightweight PRD + ADRs)

Keep `docs/azure_architecture.md` as the overview. Add:

- PRD: Stack Run Service MVP requirements
- ADRs: key architecture decisions that should not drift
- Implementation plan: weekend tasks + checklists + commands

If you want to keep it extremely simple, you can put these in:
- `docs/prd/cloud_run_service_mvp.md`
- `docs/adr/0001-async-run-lifecycle.md`
- `docs/adr/0002-auth-entra-vs-keys.md`

---

## Done criteria (by Sunday night)

You are "done" with the weekend MVP when:

- `POST /api/runs` reliably creates a run and persists inputs + validation
- Worker reliably writes outputs + metadata
- Run index supports listing and status
- You can reproduce a run by re-running the stored `inputs.json`

Next after that:
- Standardize and version an output contract (you already have `docs/output_contract_v0_1.md`)
- Decide your Excel integration path (local bridge vs add-in)
- Add monitoring dashboards and alerts (App Insights)
