# Excel + Power Query Setup (Path 3)

This document describes a practical workflow for non-technical users:

1) Work in Excel
2) Click/trigger a local runner to produce `inputs.json`
3) Engine produces `outputs.json` + `validation.json`
4) Power Query imports those JSON files back into Excel tables

## 1. Local Runner (recommended)

From repo root:

```powershell
python runs/run_from_excel.py --workbook excel/underwriting_template.xlsx --runs-root runs/deals
```

The run folder will be:

```
runs/deals/<deal_id>/<run_id>/
```

Artifacts:
- `inputs.json`
- `outputs.json`
- `validation.json`
- `metadata.json` (timestamp + commit hash)

## 2. Power Query: Import Validation Status

Goal: load `validation.json` and surface `status` on the `VALIDATION` sheet.

### Option A: Quick import (manual)

1. Excel → Data → Get Data → From File → From JSON
2. Choose: `runs/deals/<deal_id>/<run_id>/validation.json`
3. Convert to table and load to `VALIDATION` sheet

### Option B: Parameterized import (recommended)

1. Put your run folder path in the `PARAMETERS` sheet (e.g. `runs\\deals\\deal_001\\run_001`).
2. Create a named cell `RunFolderPath` that points to that value.
3. Use a query like:

```m
let
  RunFolder = Excel.CurrentWorkbook(){[Name="RunFolderPath"]}[Content]{0}[Column1],
  Source = Json.Document(File.Contents(RunFolder & "\\validation.json")),
  Status = Source[status],
  Issues = Source[issues],
  IssuesTable = Table.FromRecords(Issues),
  Output = #table({"status"}, {{Status}})
in
  Output
```

## 3. Power Query: Import Outputs

`outputs.json` contains nested arrays. Power Query can:
- import the whole JSON and expand,
- or target specific subtrees (recommended for stable tables).

Example (target totals by month):

```m
let
  RunFolder = Excel.CurrentWorkbook(){[Name="RunFolderPath"]}[Content]{0}[Column1],
  Source = Json.Document(File.Contents(RunFolder & "\\outputs.json")),
  Totals = Source[revenue][totals_by_month],
  TotalsTable = Table.FromRecords(Totals)
in
  TotalsTable
```

## 4. “Click a Button” Options

Excel cannot natively execute Python without add-ins or macros. Common approaches:

- VBA macro that shells out to:
  - `python runs\\run_from_excel.py ...`
  - then refreshes queries
- Office Scripts / Power Automate (cloud-dependent)

This repo keeps the engine deterministic and local-first; the runner is the minimal “bridge”.

