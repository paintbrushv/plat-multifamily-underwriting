# Stage 3 (Portal): Create Azure Resources for Stack Run Service

This is a copy/paste-friendly, beginner-oriented guide to create the Azure resources needed for the Stack Run Service using the Azure Portal (web browser).

If you want the shortest path: follow Part A (create resources) and Part B (configure storage + app settings). Deployment is in Part C (still in the browser via Cloud Shell).

---

## Before you start (pick names)

You will need:

- `TENANT_ID` (your Microsoft Entra tenant id)
- `SUBSCRIPTION_ID` (the Azure subscription you will deploy into)
- `LOCATION` (example: `eastus`)
- `RG_NAME` (example: `rg-stack-dev`)
- `STORAGE_NAME` (example: `ststackdev12345`)
  - Must be globally unique, 3-24 chars, lowercase letters/numbers only.
- `FUNCAPP_NAME` (example: `func-stack-dev-12345`)
  - Must be globally unique.
- `APPINSIGHTS_NAME` (example: `appi-stack-dev`)

The Stack defaults used by the repo:

- Blob container: `stack-runs`
- Queue: `run-jobs`
- Table: `runindex`
- Blob prefix: `runs`

---

## Part A: Create resources (Azure Portal)

### A1) Create a Resource Group

1. Go to `https://portal.azure.com`
2. Search: `Resource groups`
3. Click `Create`
4. Set:
   - Subscription: select your subscription
   - Resource group: `<RG_NAME>`
   - Region: `<LOCATION>`
5. Click `Review + create` -> `Create`

### A2) Create a Storage Account

1. Search: `Storage accounts`
2. Click `Create`
3. Basics:
   - Subscription: your subscription
   - Resource group: `<RG_NAME>`
   - Storage account name: `<STORAGE_NAME>`
   - Region: `<LOCATION>`
   - Performance: `Standard`
   - Redundancy: `Locally-redundant storage (LRS)`
4. Click `Review + create` -> `Create`

### A3) Create an Application Insights resource (recommended)

1. Search: `Application Insights`
2. Click `Create`
3. Set:
   - Resource group: `<RG_NAME>`
   - Name: `<APPINSIGHTS_NAME>`
   - Region: `<LOCATION>`
   - Resource Mode: `Classic` or `Workspace-based` (either is fine for MVP)
4. Click `Review + create` -> `Create`

### A4) Create the Function App

1. Search: `Function App`
2. Click `Create`
3. Basics:
   - Subscription: your subscription
   - Resource group: `<RG_NAME>`
   - Function App name: `<FUNCAPP_NAME>`
   - Runtime stack: `Python`
   - Version: `3.11`
   - Region: `<LOCATION>`
4. Hosting:
   - Plan type: `Consumption (Serverless)`
   - Operating System: choose the default offered for Python (Windows or Linux is fine for MVP)
   - Storage account: select `<STORAGE_NAME>`
   - If Azure asks how the Function App should access the storage account (for example: `Secrets` vs `Managed identity`), choose `Secrets` for the weekend MVP. (Our current Stack code reads `AzureWebJobsStorage` as a connection string; managed identity is a great hardening step later.)
5. Monitoring:
   - Enable Application Insights: `On`
   - Select existing: pick `<APPINSIGHTS_NAME>` (or create new)
6. Click `Review + create` -> `Create`

---

## Part B: Configure Storage + Function App settings

### B1) Create the Blob container (`stack-runs`)

1. Open your storage account: `<STORAGE_NAME>`
2. In left nav, go to `Data storage` -> `Containers`
3. Click `+ Container`
4. Name: `stack-runs`
5. Public access level: `Private (no anonymous access)`
6. Click `Create`

### B2) Create the Queue (`run-jobs`)

1. In the storage account, go to `Data storage` -> `Queues`
2. Click `+ Queue`
3. Name: `run-jobs`
4. Click `Create`

### B3) Create the Table (`runindex`)

1. In the storage account, go to `Data storage` -> `Tables`
2. Click `+ Table`
3. Name: `runindex`
4. Click `Create`

### B4) Set Function App configuration (app settings)

1. Open the Function App: `<FUNCAPP_NAME>`
2. Left nav: `Settings` -> `Environment variables` (or `Configuration`)
3. Under `Application settings`, add/update:
   - `UNDERWRITING_RUNS_CONTAINER` = `stack-runs`
   - `UNDERWRITING_RUNS_PREFIX` = `runs`
   - `UNDERWRITING_RUN_INDEX_TABLE` = `runindex`
   - `UNDERWRITING_RUN_QUEUE` = `run-jobs`
4. Save.

Notes:
- The Function App should already have `AzureWebJobsStorage` set because you selected the storage account during creation.

---

## Part C: Deploy the function app zip (still in browser)

This uses Cloud Shell (built into the portal), so you do not need local Azure CLI.

### C1) Build the zip locally (repo)

From repo root:

```powershell
PowerShell -File azure-functions/scripts/package.ps1
```

This produces: `azure-functions/.build/functionapp.zip`

### C2) Open Cloud Shell

1. In Azure Portal, click the terminal icon: `>_` (Cloud Shell)
2. If prompted, choose:
   - Shell: `Bash` (recommended)
   - Storage: let Azure create the Cloud Shell storage (separate from your Stack storage)

### C3) Upload the zip to Cloud Shell

1. In Cloud Shell, click `Upload/Download files` -> `Upload`
2. Upload: `azure-functions/.build/functionapp.zip`
3. Confirm it appears in your Cloud Shell home directory (usually `~/`)

### C4) Zip deploy

In Cloud Shell (Bash), run:

```bash
az account show 1>/dev/null

az functionapp deployment source config-zip \
  --resource-group "<RG_NAME>" \
  --name "<FUNCAPP_NAME>" \
  --src "functionapp.zip"
```

If it succeeds, the functions should appear under:
- Function App -> `Functions`

---

## Part D: Get a Function Key (for the smoke test)

For MVP, your HTTP functions are `authLevel=function` and need `x-functions-key`.

1. Function App -> `Functions`
2. Click a function (example: `api_create_run`)
3. Click `Function Keys`
4. Copy a key value (or create one)

You can now run the repo smoke test locally:

```powershell
PowerShell -File azure-functions/scripts/smoke_test_stack_run_service.ps1 `
  -BaseUrl "https://<YOUR_FUNCTION_APP_DEFAULT_HOSTNAME>" `
  -FunctionKey "<YOUR_FUNCTION_KEY>"
```

Tip: in the portal, the Function App "Overview" page shows a "Default domain" value. Use that hostname.

---

## Common "gotchas"

- Storage account name rules are strict (lowercase/numbers only).
- If Functions do not show up after deploy, wait 1-2 minutes and refresh the Functions list.
- If the queue trigger is not firing, confirm:
  - `UNDERWRITING_RUN_QUEUE` matches the queue you created (`run-jobs`)
  - `AzureWebJobsStorage` is set on the Function App
