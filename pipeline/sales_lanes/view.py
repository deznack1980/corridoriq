"""Internal HTML salesperson prototype. Not the customer dashboard."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import REPORTS_GENERATED_DIR
from pipeline.sales_lanes.serialize import serialize_internal_card


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _card(row: dict, lanes: list[str]) -> str:
    card = serialize_internal_card(row, lanes)
    pills = "".join(f'<span class="pill">{_esc(l.replace("_", " "))}</span>' for l in lanes)
    contact = " · ".join(
        x for x in (
            card.get("contact_name"),
            card.get("contact_role"),
            card.get("phone"),
            card.get("email"),
        ) if x
    ) or "No public business contact on file"
    return f"""
<article class="card">
  <header>
    <h2>{_esc(card["canonical_name"])}</h2>
    <div class="pills">{pills}</div>
    <p class="pri">Priority { _esc(card["priority"]) }</p>
  </header>
  <h3>Why now</h3>
  <p>{_esc(card["why_now"])}</p>
  <h3>Likely demand</h3>
  <p>{_esc(card["likely_demand"])}</p>
  <h3>Activity</h3>
  <p>{_esc(card["activity_90d"])} relevant jobs / 90d · {_esc(card["historical_relevant"])} historical relevant projects</p>
  <h3>Best contact</h3>
  <p>{_esc(contact)}</p>
  <h3>Current project</h3>
  <p>{_esc(card["current_project"] or "—")}</p>
  <p class="id">{_esc(card["identity_summary"])}</p>
</article>
"""


def _detail(row: dict) -> str:
    lanes = row.get("sales_lanes") or ["PLUMBING_CORE"]
    card = serialize_internal_card(row, lanes)
    return f"""
<section class="detail">
  <h2>Account detail — {_esc(card["canonical_name"])}</h2>
  <h3>Account overview</h3>
  <p>{_esc(card["identity_summary"])} · Priority {_esc(card["priority"])}</p>
  <h3>Why now</h3>
  <p>{_esc(card["why_now"])}</p>
  <h3>Likely material demand</h3>
  <p>{_esc(card["likely_demand"])}</p>
  <h3>Recent projects</h3>
  <p>{_esc(card["activity_90d"])} relevant / 90 days · strongest: {_esc(card["current_project"] or "—")}</p>
  <h3>Project history</h3>
  <p>{_esc(card["historical_relevant"])} historical relevant projects</p>
  <h3>Contacts</h3>
  <p>{_esc(" · ".join(x for x in (card.get("contact_name"), card.get("contact_role"), card.get("phone"), card.get("email")) if x) or "No public business contact on file")}</p>
  <h3>Trade / sales lanes</h3>
  <p>{_esc(", ".join(lanes))}</p>
  <h3>License / identity status</h3>
  <p>{_esc(card["identity_summary"])}</p>
  <h3>Sales notes</h3>
  <p>None on file.</p>
  <p class="muted">CRM status is shown only when a real relationship already exists. None was created for this prototype.</p>
</section>
"""


def write_internal_view(books: dict[str, list[dict]], *, extra_lanes: dict[int, list[str]] | None = None) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_GENERATED_DIR / f"internal_sales_lanes_{ts}.html"
    plumbing = books.get("PLUMBING_CORE") or []
    cards = "\n".join(
        _card(r, r.get("sales_lanes") or ["PLUMBING_CORE"]) for r in plumbing
    )
    detail = _detail(plumbing[0]) if plumbing else ""
    sections = []
    for lane in ("FUEL_GAS_PROPANE", "FIRE_BACKFLOW", "CIVIL_WET_UTILITY", "GENERAL_CONTRACTOR_CM"):
        rows = books.get(lane) or []
        items = "".join(
            f"<li><strong>{_esc(r['canonical_name'])}</strong> · { _esc(r['account_priority_score']) } · {_esc(r['sales_why_now'])[:160]}</li>"
            for r in rows[:10]
        )
        sections.append(f"<section><h2>{_esc(lane.replace('_',' ')) } Top 10</h2><ol>{items}</ol></section>")
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>CorridorIQ internal sales lanes</title>
  <style>
    body {{ font-family: Georgia, serif; margin: 24px; background: #f4f1ea; color: #1b1b1b; }}
    .banner {{ background: #3d2c1e; color: #f4f1ea; padding: 12px 16px; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
    .card {{ background: #fff; border: 1px solid #d9d0c4; padding: 16px; }}
    .card h2 {{ margin: 0 0 8px; font-size: 1.15rem; }}
    .card h3 {{ margin: 12px 0 4px; font-size: 0.75rem; letter-spacing: .08em; text-transform: uppercase; color: #6b5c4c; }}
    .pill {{ display: inline-block; border: 1px solid #b9a894; padding: 2px 8px; margin: 0 6px 6px 0; font-size: 0.7rem; }}
    .pri {{ font-size: 0.9rem; color: #5a4a3a; }}
    .id {{ font-size: 0.8rem; color: #6b5c4c; }}
    .detail {{ background: #fff; border: 1px solid #d9d0c4; padding: 20px; margin-top: 28px; }}
    .muted {{ color: #6b5c4c; font-size: 0.85rem; }}
    ol {{ padding-left: 1.2rem; }}
    li {{ margin: 8px 0; }}
  </style>
</head>
<body>
  <div class="banner">INTERNAL ONLY — not the customer dashboard — {ts}</div>
  <h1>Plumbing core account cards</h1>
  <p>Filtered from existing account priority. Scores were not recalculated. Canonical duplicates collapsed.</p>
  <div class="grid">
  {cards}
  </div>
  {detail}
  {''.join(sections)}
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")
    return path
