"""Market-data status: which provider supplied what for a geography, and why not.

``market_data_status(registry, geo)`` returns a JSON-serializable block that
ingest records on the canonical (``assumptions.market_data_status``), run_deal
copies into ``results.json`` (``metadata.market_data_status``), the assumption
register shows, and ``runs/market_data_doctor.py`` prints. For each provider
it lists availability, the root and where it came from, the geography match
and the metrics supplied; for each assumption slot that reads market data it
says whether any provider supplied it and, if not, why (the fallback note the
resolver appends to that entry's basis).

Nothing here raises for a missing root, file or geography.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from engine.market_data.metrics import METRICS
from engine.market_data.types import Geo, Observation

STATUS_VERSION = "plat.market_data_status/1"

# Assumption key -> the market metrics it reads (resolver specs' market_metrics).
SLOT_METRICS: dict[str, tuple[str, ...]] = {
    "rent_growth": ("rent_growth_forecast", "rent_growth_long_run"),
    "physical_vacancy": ("vacancy_current", "vacancy_stabilized"),
    "concessions": ("concession_rate",),
    "renovation_premium": ("renovation_premium",),
    "exit_cap_rate": ("market_cap_rate",),
}
# Metrics asked at the MSA level only (the resolver drops the submarket).
MSA_ONLY = frozenset({"rent_growth_long_run"})

FALLBACK_PREFIX = "Market data fell back:"
DOCTOR_HINT = "Check the setup with runs/market_data_doctor.py."
NO_GEOGRAPHY = (
    "the deal profile has no geography (set --msa, --metro-slug and --submarket), so no provider was asked"
)

Lookup = Callable[[str], list[Observation]]


def geo_label(geo: Geo | None) -> str:
    if geo is None:
        return "no geography"
    parts = []
    if geo.metro_slug:
        parts.append(f"metro {geo.metro_slug}")
    if geo.cbsa_code:
        parts.append(f"CBSA {geo.cbsa_code}")
    if geo.submarket:
        parts.append(f"submarket {geo.submarket!r}")
    if geo.star_tier:
        parts.append(f"star tier {geo.star_tier}")
    return ", ".join(parts) or "no geography"


def _msa(geo: Geo) -> Geo | None:
    if not (geo.cbsa_code or geo.metro_slug):
        return None
    return Geo(cbsa_code=geo.cbsa_code, metro_slug=geo.metro_slug, star_tier=geo.star_tier)


def registry_lookup(registry: Any, geo: Geo) -> Lookup:
    """A lookup that asks ``registry.resolve`` (MSA level for ``MSA_ONLY`` metrics)."""

    def lookup(metric: str) -> list[Observation]:
        query = _msa(geo) if metric in MSA_ONLY else geo
        if query is None:
            return []
        try:
            observation, candidates = registry.resolve(metric, query, as_of=None)
        except Exception:  # noqa: BLE001 - status never raises
            return []
        items = [c for c in (candidates or []) if isinstance(c, Observation)]
        if isinstance(observation, Observation) and not any(c is observation for c in items):
            items.insert(0, observation)
        return items

    return lookup


def _observation_record(observation: Observation) -> dict[str, Any]:
    return {
        "provider": observation.provenance.provider,
        "level": "submarket" if observation.geo.submarket else "msa",
        "geo": {k: v for k, v in observation.geo.as_dict().items() if v is not None},
        "as_of": observation.as_of.isoformat() if observation.as_of else None,
        "path": observation.provenance.path,
        "vintage": observation.provenance.vintage,
    }


def provider_report(provider: Any, geo: Geo | None) -> dict[str, Any]:
    """Availability, root and geography match of one provider (never raises)."""
    name = str(getattr(provider, "name", type(provider).__name__))
    report: dict[str, Any] = {"name": name}
    try:
        report["available"] = bool(provider.available())
    except Exception as exc:  # noqa: BLE001
        report["available"] = False
        report["unavailable_reason"] = f"available() raised {type(exc).__name__}: {exc}"
    else:
        reason = getattr(provider, "unavailable_reason", None)
        report["unavailable_reason"] = None if report["available"] else (reason or "unavailable")
    diagnose = getattr(provider, "diagnose", None)
    if callable(diagnose) and geo is not None:
        try:
            report.update(diagnose(geo))
        except Exception as exc:  # noqa: BLE001
            report["summary"] = f"diagnose() raised {type(exc).__name__}: {exc}"
    elif getattr(provider, "root", None) is not None:
        report["root"] = str(provider.root)
        report["root_origin"] = getattr(provider, "root_origin", None)
    return report


def _provider_reason(report: Mapping[str, Any], metrics: Iterable[str], errors: Mapping[str, str]) -> str:
    metrics = list(metrics)
    if not report.get("available"):
        reason = str(report.get("unavailable_reason") or "unavailable")
    elif report.get("serves") is not None and not set(metrics) & set(report["serves"]):
        reason = f"does not serve {' or '.join(metrics)}"
    elif report.get("summary"):
        reason = str(report["summary"])
    else:
        reason = f"no value for {' or '.join(metrics)} at this geography"
    failed = [f"{m} raised {errors[m]}" for m in metrics if m in errors]
    if failed:
        reason = f"{reason} ({'; '.join(failed)})"
    return reason


def fallback_note(status: Mapping[str, Any], slot: str) -> str | None:
    """The sentence the resolver appends to a slot's basis when no provider supplied it."""
    record = (status.get("slots") or {}).get(slot)
    if not isinstance(record, Mapping) or record.get("supplied"):
        return None
    return record.get("note")


def market_data_status(
    registry: Any,
    geo: Geo | None,
    *,
    slots: Mapping[str, Iterable[str]] | None = None,
    lookup: Lookup | None = None,
) -> dict[str, Any]:
    """The market-data status block for ``geo`` (see the module docstring).

    ``slots`` maps assumption keys to the metrics they read (default
    ``SLOT_METRICS``). ``lookup(metric)`` returns every provider's observation
    for ``metric`` at ``geo`` (default: ``registry.resolve``); the resolver
    passes its cached lookups so nothing is read twice.
    """
    slots = {k: tuple(v) for k, v in (SLOT_METRICS if slots is None else slots).items()}
    providers = list(getattr(registry, "providers", None) or [])
    reports = [provider_report(p, geo) for p in providers]
    metric_names = sorted({m for metrics in slots.values() for m in metrics} | (set(METRICS) if lookup is None else set()))
    by_metric: dict[str, list[dict[str, Any]]] = {}
    if geo is not None:
        lookup = registry_lookup(registry, geo) if lookup is None else lookup
        for metric in metric_names:
            try:
                observations = lookup(metric)
            except Exception:  # noqa: BLE001
                observations = []
            by_metric[metric] = [_observation_record(o) for o in observations]
    registry_errors = getattr(registry, "errors", None) or {}
    for report in reports:
        name = report["name"]
        errors = dict(report.get("errors") or {})
        errors.update({m: e for (p, m), e in registry_errors.items() if p == name})
        report["errors"] = errors
        report["metrics_supplied"] = sorted(
            m for m, records in by_metric.items() if any(r["provider"] == name for r in records)
        )

    slot_records: dict[str, dict[str, Any]] = {}
    for slot, metrics in slots.items():
        supplied_by = [
            {"metric": m, **r} for m in metrics for r in by_metric.get(m, [])
        ]
        record: dict[str, Any] = {
            "metrics": list(metrics),
            "supplied": bool(supplied_by),
            "supplied_by": supplied_by,
            "note": None,
        }
        if geo is None:
            record["note"] = f"{FALLBACK_PREFIX} {NO_GEOGRAPHY}."
        elif not supplied_by:
            reasons = [f"{r['name']}: {_provider_reason(r, metrics, r['errors'])}" for r in reports]
            if not reasons:
                reasons = ["the registry has no providers"]
            record["note"] = (
                f"{FALLBACK_PREFIX} no provider supplied {' or '.join(metrics)} for {geo_label(geo)} "
                f"({'; '.join(reasons)}). {DOCTOR_HINT}"
            )
        slot_records[slot] = record

    supplied = sum(1 for r in slot_records.values() if r["supplied"])
    if geo is None:
        summary = f"Market data not used: {NO_GEOGRAPHY}."
    else:
        summary = (
            f"{supplied} of {len(slot_records)} market slots supplied by market data for {geo_label(geo)}"
            + (f"; {len(slot_records) - supplied} fell back to deal evidence or house defaults." if supplied < len(slot_records) else ".")
        )
    return {
        "status_version": STATUS_VERSION,
        "enabled": True,
        "geography": None if geo is None else {k: v for k, v in geo.as_dict().items() if v is not None},
        "config": dict(getattr(registry, "config_source", None) or {}),
        "providers": reports,
        "metrics": by_metric,
        "slots": slot_records,
        "summary": summary,
    }


def disabled_status(reason: str) -> dict[str, Any]:
    """The status block when no registry was used (``--no-market-data``)."""
    return {
        "status_version": STATUS_VERSION,
        "enabled": False,
        "geography": None,
        "config": {},
        "providers": [],
        "metrics": {},
        "slots": {},
        "summary": f"Market data not used: {reason}.",
    }


def status_lines(status: Mapping[str, Any] | None, *, indent: str = "  ", verbose: bool = False) -> list[str]:
    """Human-readable lines for a status block (ingest / run_deal / doctor output)."""
    if not isinstance(status, Mapping):
        return [f"{indent}Market data status: not recorded"]
    out = [f"{indent}Market data status: {status.get('summary')}"]
    pad = indent + "  "
    config = status.get("config") or {}
    user = config.get("user_config") or {}
    if user.get("path"):
        state = "found" if user.get("exists") else "not found"
        line = f"{pad}user config: {user['path']} ({state})"
        if user.get("error"):
            line += f": {user['error']}"
        out.append(line)
    if config.get("provider_order"):
        out.append(f"{pad}provider order: {config['provider_order']}")
    for report in status.get("providers") or []:
        state = "available" if report.get("available") else "NOT AVAILABLE"
        matched = report.get("matched")
        geo_state = "" if matched is None else (", geography matched" if matched else ", geography not matched")
        out.append(f"{pad}{report.get('name')}: {state}{geo_state}")
        if report.get("root"):
            found = "found" if report.get("root_found", True) else "not found"
            out.append(f"{pad}  root: {report['root']} ({report.get('root_origin')}; {found})")
        if report.get("unavailable_reason"):
            out.append(f"{pad}  why: {report['unavailable_reason']}")
        elif report.get("summary"):
            out.append(f"{pad}  missing: {report['summary']}")
        supplied = report.get("metrics_supplied") or []
        out.append(f"{pad}  supplies: {', '.join(supplied) if supplied else 'nothing for this geography'}")
        for metric, error in sorted((report.get("errors") or {}).items()):
            out.append(f"{pad}  error ({metric}): {error}")
        if verbose:
            for key, value in (report.get("inventory") or {}).items():
                out.append(f"{pad}  inventory.{key}: {value}")
            for key, value in (report.get("geography") or {}).items():
                out.append(f"{pad}  geography.{key}: {value}")
    for slot, record in (status.get("slots") or {}).items():
        if record.get("supplied"):
            sources = sorted({f"{r['provider']} ({r['level']})" for r in record.get("supplied_by") or []})
            out.append(f"{pad}{slot}: market data from {', '.join(sources)}")
        elif record.get("kept_from_prior_resolution"):
            out.append(f"{pad}{slot}: no market data now; kept the market value or reference recorded earlier")
        else:
            source = record.get("entry_source")
            tail = f" -> {source}" if source else ""
            out.append(f"{pad}{slot}: FELL BACK{tail} (no {' / '.join(record.get('metrics') or [])})")
    return out
