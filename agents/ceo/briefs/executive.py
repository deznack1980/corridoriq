"""Thirteen-section executive brief. Numbers come from state or stay UNKNOWN."""

from __future__ import annotations

from datetime import datetime, timezone

from agents.ceo.company.loader import load_company_state, may_market_as_live
from agents.ceo.company.signals import sonoran_john


def render_executive(decision) -> str:
    state = load_company_state()
    signal = sonoran_john(state)
    launch = state["launch"]
    maturity = state["maturity"]["capabilities"]
    today = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    kpis = _unknown_line(state)
    priorities = "\n".join(f"{index}. {item}" for index, item in enumerate(decision.do_now, start=1))
    avoid = "\n".join(f"{index}. {item}" for index, item in enumerate(decision.do_not, start=1))
    changed = "\n".join(f"- {item}" for item in decision.what_changed) or "- None supplied."
    remaining = "\n".join(f"- {item}" for item in launch["remaining_before_public_launch"])
    request = maturity["contractor_material_request_text"]["status"]
    inventory = maturity["live_inventory"]["status"]
    photo = maturity["photo_to_bom"]["status"]
    launched = "no" if launch["publicly_launched"] is False else "UNKNOWN"
    return "\n".join(
        [
            "CORRIDORIQ CEO BRIEF",
            f"DATE {today}",
            "INTERNAL ONLY. v0.2 does not execute this brief.",
            "",
            "1. COMPANY STATUS",
            (
                f"{state['constitution']['company']} - {state['constitution']['category']}. "
                f"Market: {state['constitution']['initial_market']}. "
                "Suppliers pay for intelligence. Contractors drive network demand. "
                "Procurement connects the two. No marketplace is assumed."
            ),
            "",
            "2. WHAT CHANGED",
            changed,
            "",
            "3. REVENUE",
            "MRR UNKNOWN. ARR UNKNOWN. Paid suppliers UNKNOWN. No approved public price.",
            "",
            "4. SALES PIPELINE",
            kpis,
            "",
            "5. CUSTOMER SIGNALS",
            f"FACT: {signal['FACT']}",
            f"INFERENCE: {signal['INFERENCE']}",
            f"HYPOTHESIS: {signal['HYPOTHESIS']}",
            f"RECOMMENDATION: {signal['RECOMMENDATION']}",
            "Willingness to pay: UNKNOWN. Signed customer: no. Active paid pilot: no.",
            "",
            "6. PRODUCT USAGE",
            (
                f"Supplier intelligence maturity: {maturity['supplier_intelligence']['status']}. "
                f"Contractor typed material request: {request}. "
                f"Photo-to-BOM: {photo}. Live inventory: {inventory}. "
                "Usage counts are UNKNOWN."
            ),
            "",
            "7. DATA / PIPELINE HEALTH",
            _health(decision),
            "",
            "8. PRODUCTION / INFRASTRUCTURE",
            (
                f"Publicly launched: {launched}. Landing commit {launch['landing_commit']} "
                "is launch-ready website work, not a confirmed cutover."
            ),
            remaining,
            "",
            "9. RISKS",
            "- Treating John's excitement as revenue or product-market fit.",
            "- Describing early-access material requests as inventory, pricing, or a marketplace.",
            "- Selling metro coverage or contacting contractors from untrusted why-now text.",
            "- Cutting DNS over before backup and Microsoft 365 records are preserved.",
            "",
            "10. DECISIONS NEEDED FROM FOUNDER",
            "- Whether to demonstrate the working request flow to Sonoran. The agent will not make that contact.",
            "- No public price, contract, or launch cutover is approved by this brief.",
            "",
            "11. TOP 3 PRIORITIES TODAY",
            priorities or "1. None.",
            "",
            "12. DELEGATABLE WORK",
            "- Read the morning queue and the latest read-only pipeline snapshot.",
            "- Draft a demonstration script. Do not send it.",
            "",
            "13. WHAT NOT TO WORK ON",
            avoid or "1. None.",
            "- Do not publish pricing, provision cloud, change DNS, or contact Sonoran from this agent.",
            "",
            f"Engineering question: {state['priorities']['engineering_question']}",
            "Supplier intelligence may be described as live in the product: "
            + ("yes" if may_market_as_live(state, "supplier_intelligence") else "no")
            + ". Live inventory may be described as live: "
            + ("yes" if may_market_as_live(state, "live_inventory") else "no")
            + ".",
        ]
    )


def _unknown_line(state: dict) -> str:
    names = []
    for group in state["kpis"]["groups"].values():
        for name, metric in group.items():
            if metric.get("status") != "UNKNOWN" or metric.get("value") is not None:
                raise ValueError(f"brief refused a fabricated KPI {name}")
            names.append(name)
    return (
        "Pipeline counts are UNKNOWN ("
        + ", ".join(names[:4])
        + ", and the rest of the KPI model). "
        "Sonoran is one positive customer-development signal, not a pipeline stage."
    )


def _health(decision) -> str:
    lines = [line for line in decision.current_state if "Project rows" in line or "source" in line.lower()]
    if not lines:
        return "Latest refresh, backup health, and failed jobs are UNKNOWN in this brief unless the snapshot section below says otherwise."
    return " ".join(lines)
