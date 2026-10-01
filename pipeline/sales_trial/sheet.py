"""Internal-only call-sheet prototype. Not the customer dashboard."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import REPORTS_GENERATED_DIR, SALES_LANE_VERSION
from pipeline.sales_lanes.lanes import PLUMBING_CORE


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def sibling_lanes(conn, company_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT lane_key FROM company_sales_lanes
        WHERE company_id=? AND model_version=? AND presentable=1
        ORDER BY lane_key
        """,
        (company_id, SALES_LANE_VERSION),
    ).fetchall()
    keys = [r["lane_key"] for r in rows]
    if PLUMBING_CORE not in keys:
        keys.insert(0, PLUMBING_CORE)
    return keys


def serialize_call_sheet_card(row: dict, lanes: list[str], crm: str | None) -> dict:
    after = row.get("after") or {}
    return {
        "company": row.get("canonical_name"),
        "lanes": lanes,
        "priority": row.get("account_priority_score"),
        "callability": row.get("callability"),
        "why_now": row.get("sales_why_now"),
        "likely_demand": row.get("likely_buy"),
        "recent_activity": (
            f"{row.get('relevant_30d') or 0} / {row.get('relevant_90d') or 0} / "
            f"{row.get('relevant_180d') or 0} relevant (30/90/180d)"
        ),
        "best_contact_name": after.get("contact_name"),
        "best_contact_role": after.get("role") or after.get("decision_class"),
        "phone": after.get("phone"),
        "email": after.get("email"),
        "website": after.get("website"),
        "current_project": row.get("strongest_project"),
        "learning_objective": (
            "Confirm buying categories, timing, decision-maker, current supplier, "
            "and whether a material-list workflow would help."
        ),
        "notes": row.get("callability_reasons"),
        "crm_status": crm,
        "identity": row.get("identity_status"),
    }


BANNED_PUBLIC_FIELDS = (
    "source_family",
    "verification_status",
    "decision_maker_class",
    "contact_confidence",
    "canonical_company_id",
    "match_status",
    "model_version",
    "callability",
)


def _card_html(card: dict) -> str:
    pills = "".join(
        f'<span class="pill">{_esc(l.replace("_", " "))}</span>' for l in card["lanes"]
    )
    crm = card.get("crm_status")
    crm_html = (
        f'<p class="crm">CRM: {_esc(crm)}</p>'
        if crm
        else '<p class="muted">No CRM relationship.</p>'
    )
    contact = " · ".join(
        x
        for x in (
            card.get("best_contact_name"),
            card.get("best_contact_role"),
            card.get("phone"),
            card.get("email"),
        )
        if x
    ) or "No public business contact on file"
    return f"""
<article class="sheet">
  <header>
    <h2>{_esc(card["company"])}</h2>
    <div class="pills">{pills}</div>
    <p class="meta">Priority {_esc(card["priority"])} · {_esc(card["callability"])} · {_esc(card.get("identity"))}</p>
  </header>
  <h3>Why now</h3>
  <p>{_esc(card["why_now"])}</p>
  <h3>Likely demand</h3>
  <p>{_esc(card["likely_demand"])}</p>
  <h3>Recent activity</h3>
  <p>{_esc(card["recent_activity"])}</p>
  <h3>Best contact</h3>
  <p>{_esc(contact)}</p>
  <p class="muted">{_esc(card.get("website"))}</p>
  <h3>Current project</h3>
  <p>{_esc(card.get("current_project") or "—")}</p>
  <h3>Learning objective</h3>
  <p>{_esc(card["learning_objective"])}</p>
  <h3>Notes</h3>
  <p>{_esc(card.get("notes"))}</p>
  {crm_html}
</article>
"""


def write_call_sheet(conn, enriched: list[dict], trial: dict) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_GENERATED_DIR / f"internal_call_sheet_{ts}.html"
    cards = []
    trial_ids = {int(a["company_id"]) for a in trial.get("accounts") or []}
    for rec in enriched:
        crm_row = conn.execute(
            "SELECT relationship_status FROM crm_company_relationships WHERE company_id=? LIMIT 1",
            (int(rec["primary_company_id"]),),
        ).fetchone()
        crm = None if crm_row is None else crm_row["relationship_status"]
        lanes = sibling_lanes(conn, int(rec["primary_company_id"]))
        card = serialize_call_sheet_card(rec, lanes, crm)
        cards.append((card, int(rec["primary_company_id"]) in trial_ids))
    trial_html = "\n".join(_card_html(c) for c, is_t in cards if is_t)
    rest_html = "\n".join(_card_html(c) for c, is_t in cards if not is_t)
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>CorridorIQ internal plumbing call sheet</title>
  <style>
    body {{ font-family: Georgia, serif; margin: 24px; background: #f4f1ea; color: #1b1b1b; }}
    .banner {{ background: #3d2c1e; color: #f4f1ea; padding: 12px 16px; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }}
    .sheet {{ background: #fff; border: 1px solid #d9d0c4; padding: 16px; }}
    .sheet h2 {{ margin: 0 0 8px; font-size: 1.15rem; }}
    .sheet h3 {{ margin: 12px 0 4px; font-size: 0.72rem; letter-spacing: .08em; text-transform: uppercase; color: #6b5c4c; }}
    .pill {{ display: inline-block; border: 1px solid #b9a894; padding: 2px 8px; margin: 0 6px 6px 0; font-size: 0.7rem; }}
    .meta {{ font-size: 0.9rem; color: #5a4a3a; }}
    .muted {{ color: #6b5c4c; font-size: 0.85rem; }}
    .crm {{ font-weight: 600; }}
  </style>
</head>
<body>
  <div class="banner">INTERNAL ONLY — plumbing-core call sheet — not the customer dashboard — {ts}</div>
  <h1>Four-account learning trial</h1>
  <p>Opening objective is to learn buying behavior. Scores were not recalculated. No CRM rows were created.</p>
  <div class="grid">
  {trial_html}
  </div>
  <h1>Rest of frozen plumbing-core Top 25</h1>
  <div class="grid">
  {rest_html}
  </div>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")
    return path
