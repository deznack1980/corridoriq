"""Build lane books from existing account_priority_score. No recalculation."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE, SALES_LANE_VERSION
from pipeline.contactability.coverage import account_contact_summary
from pipeline.db.database import now_iso
from pipeline.sales_gate.evidence import demand_counts, demand_summary, sales_why_now
from pipeline.sales_gate.identity import identity_status_for
from pipeline.sales_lanes.collapse import canonical_names, presentation_groups
from pipeline.sales_lanes.lanes import BOOK_FITS, FUEL_GAS_PROPANE, PLUMBING_CORE, SALESPERSON_LANES
from pipeline.sales_lanes.quality import label_plumbing_core
from pipeline.sales_lanes.wording import identity_safe


def _strongest(conn: sqlite3.Connection, company_ids: list[int]) -> dict:
    if not company_ids:
        return {}
    ph = ",".join("?" * len(company_ids))
    row = conn.execute(
        f"""
        SELECT pr.opportunity_date, COALESCE(p.project_description, p.description, '') AS description,
               p.permit_number, p.city, p.job_address
        FROM project_customer_relevance r
        JOIN projects pr ON pr.id = r.project_id
        JOIN permits p ON p.id = pr.permit_id
        WHERE r.profile_key=? AND r.company_id IN ({ph})
        ORDER BY pr.opportunity_date DESC, r.relevance_score DESC
        LIMIT 1
        """,
        (CUSTOMER_RELEVANCE_PROFILE, *company_ids),
    ).fetchone()
    return dict(row) if row else {}


def _roc_classes(conn: sqlite3.Connection, company_ids: list[int]) -> list[str]:
    if not company_ids:
        return []
    ph = ",".join("?" * len(company_ids))
    rows = conn.execute(
        f"""
        SELECT l.normalized_class, l.raw_class
        FROM roc_company_matches m
        LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
        WHERE m.company_id IN ({ph})
        """,
        company_ids,
    )
    out = []
    for r in rows:
        c = (r["normalized_class"] or r["raw_class"] or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _best_contact(conn: sqlite3.Connection, company_ids: list[int]) -> dict:
    best = {
        "phone": None, "email": None, "website": None,
        "contact_name": None, "role": None, "contact_confidence": "none",
    }
    rank = {"VERIFIED": 3, "CANDIDATE": 2, "none": 0}
    for cid in company_ids:
        s = account_contact_summary(conn, cid)
        if rank.get(s.get("contact_confidence") or "none", 0) >= rank.get(best["contact_confidence"], 0):
            if s.get("phone") or s.get("email") or s.get("website"):
                best = s
    return best


def lane_members(conn: sqlite3.Connection, lane_key: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.company_id, l.fit, l.subtype, l.evidence, a.account_priority_score,
               a.trade_identity, a.relevant_30d, a.relevant_90d, a.relevant_180d,
               a.active_relevant_project_count, a.fuel_gas_project_count,
               a.plumbing_project_count, a.primary_demand_category, a.demand_categories,
               a.most_recent_relevant_date, c.display_name
        FROM company_sales_lanes l
        JOIN company_customer_priority a
          ON a.company_id = l.company_id AND a.profile_key=?
        JOIN companies c ON c.id = l.company_id
        WHERE l.lane_key=? AND l.model_version=? AND l.presentable=1 AND l.fit IN (?, ?)
        ORDER BY a.account_priority_score DESC
        """,
        (CUSTOMER_RELEVANCE_PROFILE, lane_key, SALES_LANE_VERSION, *sorted(BOOK_FITS)),
    )
    return [dict(r) for r in rows]


def build_lane_book(conn: sqlite3.Connection, lane_key: str, *, limit: int = 25) -> list[dict]:
    members = lane_members(conn, lane_key)
    names = {int(m["company_id"]): m["display_name"] for m in members}
    by_id = {int(m["company_id"]): m for m in members}
    groups = presentation_groups(conn, list(by_id), names=names)
    cnames = canonical_names(conn)
    clustered = []
    for key, ids in groups.items():
        rows = [by_id[i] for i in ids if i in by_id]
        if not rows:
            continue
        primary = max(rows, key=lambda r: float(r["account_priority_score"] or 0))
        n30 = sum(int(r["relevant_30d"] or 0) for r in rows)
        n90 = sum(int(r["relevant_90d"] or 0) for r in rows)
        n180 = sum(int(r["relevant_180d"] or 0) for r in rows)
        n_rel = sum(int(r["active_relevant_project_count"] or 0) for r in rows)
        n_gas = sum(int(r["fuel_gas_project_count"] or 0) for r in rows)
        n_plum = sum(int(r["plumbing_project_count"] or 0) for r in rows)
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            for k, v in demand_counts(r.get("demand_categories")).items():
                counts[k] += v
        demand = demand_summary(counts, trade_identity=primary["trade_identity"])
        strongest = _strongest(conn, ids)
        contact = _best_contact(conn, ids)
        classes = _roc_classes(conn, ids)
        ident = identity_status_for(conn, int(primary["company_id"]), None)
        match = conn.execute(
            "SELECT match_status FROM roc_company_matches WHERE company_id=? LIMIT 1",
            (int(primary["company_id"]),),
        ).fetchone()
        ident = identity_status_for(
            conn,
            int(primary["company_id"]),
            None if match is None else match["match_status"],
        )
        why = sales_why_now(
            display_name=primary["display_name"],
            trade_identity=primary["trade_identity"],
            n30=n30, n90=n90, n180=n180, n_rel=n_rel, n_gas=n_gas, n_plum=n_plum,
            counts=dict(counts),
            strongest_desc=strongest.get("description"),
            strongest_date=strongest.get("opportunity_date"),
            roc_class=None,
            identity_status=ident if ident not in {"UNRESOLVED"} else None,
        )
        if key.startswith("canonical:"):
            can_id = int(key.split(":")[1])
            can_name = cnames.get(can_id) or primary["display_name"]
        else:
            can_name = primary["display_name"]
        subtype = next((r.get("subtype") for r in rows if r.get("subtype")), None)
        ph = ",".join("?" * len(ids))
        sibling_lanes = [
            r["lane_key"]
            for r in conn.execute(
                f"""
                SELECT DISTINCT lane_key
                FROM company_sales_lanes
                WHERE company_id IN ({ph}) AND presentable=1 AND fit IN (?, ?)
                  AND model_version=?
                ORDER BY lane_key
                """,
                (*ids, *sorted(BOOK_FITS), SALES_LANE_VERSION),
            )
        ]
        rec = {
            "canonical_key": key,
            "canonical_name": can_name,
            "primary_company_id": int(primary["company_id"]),
            "member_company_ids": ids,
            "account_priority_score": float(primary["account_priority_score"]),
            "trade_identity": primary["trade_identity"],
            "identity_status": ident,
            "roc_status_safe": identity_safe(ident, classes, [lane_key]),
            "relevant_30d": n30,
            "relevant_90d": n90,
            "relevant_180d": n180,
            "historical_relevant": n_rel,
            "primary_demand": demand["primary"] or primary.get("primary_demand_category"),
            "secondary_demand": demand["secondary"],
            "likely_buy": demand["likely_buy"],
            "sales_why_now": why,
            "contact_name": contact.get("contact_name"),
            "contact_role": contact.get("role"),
            "phone": contact.get("phone"),
            "email": contact.get("email"),
            "website": contact.get("website"),
            "strongest_project": " | ".join(
                x for x in (
                    (strongest.get("opportunity_date") or "")[:10],
                    strongest.get("city"),
                    (strongest.get("description") or "").replace("\n", " ")[:140],
                ) if x
            ),
            "fuel_gas_subtype": subtype if lane_key == FUEL_GAS_PROPANE else None,
            "sales_lanes": sibling_lanes,
            "quality_label": None,
        }
        if lane_key == PLUMBING_CORE:
            rec["quality_label"] = label_plumbing_core(rec, classes)
        clustered.append(rec)
    clustered.sort(
        key=lambda r: (-r["account_priority_score"], -int(r["relevant_90d"] or 0), r["primary_company_id"])
    )
    for i, rec in enumerate(clustered[:limit], start=1):
        rec["presentation_rank"] = i
    return clustered[:limit]


def persist_books(conn: sqlite3.Connection, books: dict[str, list[dict]]) -> int:
    now = now_iso()
    conn.execute("DELETE FROM sales_lane_books WHERE model_version=?", (SALES_LANE_VERSION,))
    n = 0
    for lane, rows in books.items():
        for rec in rows:
            conn.execute(
                """
                INSERT INTO sales_lane_books (
                    lane_key, presentation_rank, canonical_key, canonical_name,
                    primary_company_id, member_company_ids, account_priority_score,
                    trade_identity, identity_status, roc_status_safe,
                    relevant_30d, relevant_90d, relevant_180d, historical_relevant,
                    primary_demand, secondary_demand, likely_buy, sales_why_now,
                    contact_name, contact_role, phone, email, website,
                    strongest_project, fuel_gas_subtype, quality_label,
                    model_version, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lane, rec["presentation_rank"], rec["canonical_key"], rec["canonical_name"],
                    rec["primary_company_id"], json.dumps(rec["member_company_ids"]),
                    rec["account_priority_score"], rec["trade_identity"], rec["identity_status"],
                    rec["roc_status_safe"], rec["relevant_30d"], rec["relevant_90d"],
                    rec["relevant_180d"], rec["historical_relevant"], rec["primary_demand"],
                    json.dumps(rec["secondary_demand"]), rec["likely_buy"], rec["sales_why_now"],
                    rec["contact_name"], rec["contact_role"], rec["phone"], rec["email"],
                    rec["website"], rec["strongest_project"], rec.get("fuel_gas_subtype"),
                    rec.get("quality_label"), SALES_LANE_VERSION, now,
                ),
            )
            n += 1
    conn.commit()
    return n
