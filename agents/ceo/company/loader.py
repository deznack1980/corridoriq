"""Load versioned company state. Unknown metrics stay unknown."""

from __future__ import annotations

import json
from pathlib import Path

from agents.ceo.paths import state_dir

FILES = (
    "constitution.json",
    "maturity.json",
    "stakeholders.json",
    "kpis.json",
    "pricing.json",
    "launch.json",
    "priorities.json",
    "approval_gates.json",
)

MATURITY = {"LIVE", "EARLY_ACCESS", "PLANNED", "HYPOTHESIS", "DEPRECATED"}


def load_company_state(root: Path | None = None) -> dict:
    base = root if root is not None else state_dir()
    loaded = {name: _read(base / name) for name in FILES}
    _validate(loaded)
    return {
        "loaded_from": str(base),
        "constitution": loaded["constitution.json"],
        "maturity": loaded["maturity.json"],
        "stakeholders": loaded["stakeholders.json"],
        "kpis": kpi_view(loaded["kpis.json"]),
        "pricing": loaded["pricing.json"],
        "launch": loaded["launch.json"],
        "priorities": loaded["priorities.json"],
        "approval_gates": loaded["approval_gates.json"],
    }


def kpi_view(raw: dict) -> dict:
    """Every named KPI is UNKNOWN unless a measured value with a source is stored."""
    values = raw.get("values") or {}
    view = {}
    for group, names in (raw.get("groups") or {}).items():
        view[group] = {}
        for name in names:
            stored = values.get(name)
            if stored is None:
                view[group][name] = {
                    "status": "UNKNOWN",
                    "value": None,
                    "source": raw.get("source"),
                    "reason": "no measurement is stored",
                }
                continue
            if not isinstance(stored, dict) or stored.get("status") != "KNOWN":
                raise ValueError(f"KPI {name} must be absent or a KNOWN measurement")
            if stored.get("value") is None or not stored.get("source"):
                raise ValueError(f"KPI {name} is KNOWN without a value and source")
            view[group][name] = stored
    return {"schema": raw.get("schema"), "updated_at": raw.get("updated_at"), "groups": view}


def capability(state: dict, key: str) -> dict:
    item = (state["maturity"].get("capabilities") or {}).get(key)
    if item is None:
        return {"status": "UNKNOWN", "key": key}
    status = item.get("status")
    if status not in MATURITY:
        raise ValueError(f"capability {key} has status {status}")
    return {"key": key, **item}


def may_market_as_live(state: dict, key: str) -> bool:
    return capability(state, key).get("status") == "LIVE"


def committed_price(state: dict):
    """Approved public price. Hypotheses are not a commitment."""
    return state["pricing"].get("approved_public_price")


def _read(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(loaded: dict) -> None:
    constitution = loaded["constitution.json"]
    if constitution.get("company") != "CorridorIQ":
        raise ValueError("constitution company is not CorridorIQ")
    if constitution.get("assume_marketplace") is not False:
        raise ValueError("marketplace must not be assumed")
    if constitution.get("commercial_thesis", {}).get("transaction_fee_default") is not False:
        raise ValueError("transaction fees are not the default assumption")
    maturity = loaded["maturity.json"]
    for key, item in (maturity.get("capabilities") or {}).items():
        if item.get("status") not in MATURITY:
            raise ValueError(f"{key} maturity {item.get('status')} is not allowed")
    launch = loaded["launch.json"]
    if launch.get("publicly_launched") is not False:
        raise ValueError("public launch must stay false until cutover evidence replaces this file")
    if loaded["pricing.json"].get("approved_public_price") is not None:
        raise ValueError("an approved public price requires an explicit founder record, not this seed")
    if loaded["pricing.json"].get("claim_class") != "HYPOTHESIS":
        raise ValueError("seed pricing must stay a hypothesis")
