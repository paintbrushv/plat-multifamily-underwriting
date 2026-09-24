# CRM Tracker — Internals

Implementation reference for engineers extending or maintaining the CRM
package. End-user docs live in `docs/crm-tracker.md`. Design rationale
lives in `docs/superpowers/specs/2026-05-07-deal-crm-tracker-design.md`.

## Module map

| File | Responsibility |
|---|---|
| `engine/crm/config.py` | Path constants, env-var overrides, schema version |
| `engine/crm/schema.py` | Engine column definitions, sentinel literal, sheet names |
| `engine/crm/snapshot.py` | `CrmRow` dataclass + `build_crm_row(deal_dir, run_id)` |
| `engine/crm/tracker.py` | openpyxl operations: `create_fresh_workbook`, `read_workbook_view`, `compute_decision`, `apply_decision`, `migrate_schema_if_needed` |
| `engine/crm/publisher.py` | Composes onedrive transport + tracker mutation |
| `engine/crm/restore.py` | Catastrophe-recovery entry point |
| `runs/publish_to_crm.py` | CLI |
| `runs/restore_crm.py` | CLI |

Dependency direction is one-way: `publisher → tracker → schema`,
`publisher → snapshot`. Neither `tracker` nor `snapshot` imports from
`engine.onedrive` — they are testable with no auth and no network. The
publisher is the only module that crosses the OneDrive transport boundary.

## Diff-based provenance

The most recent archive is the engine's memory of what it last wrote.
`compute_decision(live_view, archive_view, slug, flags) → Decision`
returns one of:

- `INSERT` — row absent in both.
- `REFRESH` — row in both, engine cells match.
- `SKIP_HANDEDIT` — row in both, engine cells diverge.
- `SKIP_MANUAL_ADD` — row in live, absent in archive.
- `SKIP_DELETED` — row in archive, absent in live.

Override flags (`force`, `adopt`, `reinstate`) upgrade SKIP outcomes to
their write equivalent. Timestamp/version columns (`Last Underwritten`,
`Run ID`, `Engine Version`) are excluded from the diff because they tick
on every publish by design.

## Type-aware comparison

`_normalize_for_compare(value, col_def)` coerces an openpyxl cell value to
the type the engine writes:

- `currency`, `percent`, `number` → `round(float(value), 6)`
- `int` → `int(value)`
- everything else → `str(value)`

Six decimals is tight enough to catch real edits, loose enough to absorb
openpyxl's float-roundtrip drift. Without this normalization a numeric
`0.20` and a string `"0.20"` would false-positive a diff.

## Schema versioning

`engine/crm/config.SCHEMA_VERSION` is the engine's current version. The
hidden `_meta` sheet in every tracker file records its own
`schema_version`.

`migrate_schema_if_needed(path)` runs at the start of every publish:

- File version > engine version → `SchemaTooNewError`. Update the engine.
- File version == engine version → no-op.
- File version < engine version → insert each declared engine column not
  present in the file at its declared 1-indexed position. Sentinel and
  manual zone shift right by N (where N = number of new columns). Update
  `_meta.schema_version` and `_meta.column_order`.

To add a column: edit `schema.ENGINE_COLUMNS`, bump `SCHEMA_VERSION`. Done.

## Sentinel discovery

`read_workbook_view` locates the sentinel column by reading
`_meta.sentinel_header` and finding the column whose row-1 value matches.
This is rename-proof: the sentinel can be visually relocated by the
analyst as long as `_meta` stays in sync. The engine zone is "everything
left of the sentinel," not a fixed range.

## Archive contract

- Server-side `/copy` (Graph) is used for archives — no re-upload.
- Archive filename: `Deal_Tracker_<UTC ISO timestamp>.xlsx`. Colons are
  replaced with `-` for filesystem safety on Windows OneDrive clients
  viewing the archive folder.
- Archive happens **after** a successful upload — the post-mutation live
  state is server-side-copied into `Archive/`. This means every successful
  publish leaves an archive behind, including the very first one. The
  archived state is exactly what subsequent publishes diff against to
  recognize "engine wrote this, nothing changed → REFRESH". If the upload
  fails (412 ETag conflict, 423 locked), no archive is created — there is
  no successful new state to snapshot.

## ETag flow

1. `OneDriveFetcher.fetch_one_with_etag(path, dir)` returns
   `(local_path, etag)`.
2. `upload_file_conditional(local, dest, if_match=etag)` issues the PUT
   with `If-Match: <etag>`.
3. Graph returns 412 if the item changed since the fetch (concurrent
   edit). Publisher surfaces this as `status="etag_conflict"` and
   instructs the user to re-run.
4. Graph returns 423 if the item is locked (open in Excel). Publisher
   surfaces `status="locked"` with a "close and retry" message.
5. On first publish ever, no live file exists; `if_match=None` upgrades
   the PUT to unconditional.

## Testing patterns

- `tests/test_crm_*.py` test pure functions (no auth, no network) using
  temporary openpyxl files in `tmp_path`.
- `tests/test_crm_publisher.py` mocks at the
  `engine.onedrive.client.OneDriveFetcher` /
  `engine.onedrive.uploader.upload_file_conditional` /
  `engine.onedrive.uploader.copy_item` boundary — patched on the
  `engine.crm.publisher` module since that's the import-binding scope.
- All env-var-controlled paths default to a fake test directory so a
  test run cannot accidentally write to the production OneDrive folder.

## Adding a transport (e.g., S3)

The split between tracker and publisher is designed to make this cheap:

1. Add `engine/crm/<transport>_publisher.py` with the same `publish()`
   signature.
2. Replace the three `engine.crm.publisher`-imported transport names
   (`OneDriveFetcher`, `upload_file_conditional`, `copy_item`) with the
   new transport's equivalents.
3. The existing tracker, snapshot, schema, and config modules are
   unchanged.

The decision tree, schema migration, and manual-zone preservation are all
transport-agnostic.
