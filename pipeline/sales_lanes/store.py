"""Load features and persist company_sales_lanes. No score writes."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE, SALES_LANE_VERSION
from pipeline.db.database import now_iso
from pipeline.roc.normalize import looks_like_person_name
from pipeline.sales_gate.persons import classify_person_record
from pipeline.sales_lanes.assign import classify_features
from pipeline.sales_lanes.lanes import ALL_LANES, LANE_META


def seed_lanes(conn: sqlite3.Connection) -> None:
    now = now_iso()
    for key in ALL_LANES:
        name, desc, view = LANE_META[key]
        conn.execute(
            """
            INSERT INTO sales_lanes (lane_key, display_name, description,
                is_salesperson_view, model_version, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(lane_key) DO UPDATE SET
                display_name=excluded.display_name,
                description=excluded.description,
                is_salesperson_view=excluded.is_salesperson_view,
                model_version=excluded.model_version
            """,
            (key, name, desc, view, SALES_LANE_VERSION, now),
        )
    conn.commit()


def _roc_classes_by_company(conn: sqlite3.Connection) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT m.company_id, l.raw_class, l.normalized_class, l.corridor_capability
        FROM roc_company_matches m
        LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
        """
    ):
        cls = (row["normalized_class"] or row["raw_class"] or "").strip()
        if cls:
            out[int(row["company_id"])].append(cls)
    return out


def _person_class_by_company(conn: sqlite3.Connection) -> dict[int, str]:
    out = {}
    for row in conn.execute(
        "SELECT company_id, person_class FROM sales_person_reviews"
    ):
        out[int(row["company_id"])] = row["person_class"]
    return out


def assign_all(conn: sqlite3.Connection) -> dict:
    seed_lanes(conn)
    now = now_iso()
    roc = _roc_classes_by_company(conn)
    persons = _person_class_by_company(conn)
    conn.execute(
        "DELETE FROM company_sales_lanes WHERE model_version=?",
        (SALES_LANE_VERSION,),
    )
    accounts = conn.execute(
        """
        SELECT a.*, c.display_name
        FROM company_customer_priority a
        JOIN companies c ON c.id = a.company_id
        WHERE a.profile_key=?
        """,
        (CUSTOMER_RELEVANCE_PROFILE,),
    )
    written = 0
    presentable = 0
    for acct in accounts:
        cid = int(acct["company_id"])
        name = acct["display_name"] or ""
        person = persons.get(cid)
        if person is None and looks_like_person_name(name):
            person = classify_person_record(conn, cid, name)["person_class"]
        feat = {
            "display_name": name,
            "trade_identity": acct["trade_identity"],
            "demand_categories": acct["demand_categories"],
            "n_plum": acct["plumbing_project_count"],
            "n_gas": acct["fuel_gas_project_count"],
            "roc_classes": roc.get(cid, []),
            "person_class": person or "not_person_name",
        }
        for rec in classify_features(feat):
            conn.execute(
                """
                INSERT INTO company_sales_lanes (
                    company_id, lane_key, fit, subtype, evidence, presentable,
                    model_version, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    cid,
                    rec["lane_key"],
                    rec["fit"],
                    rec.get("subtype"),
                    json.dumps(rec.get("evidence") or []),
                    int(rec.get("presentable") or 0),
                    SALES_LANE_VERSION,
                    now,
                ),
            )
            written += 1
            if rec.get("presentable") and rec["lane_key"] != "IDENTITY_REVIEW":
                presentable += 1
    conn.commit()
    return {"written": written, "presentable_memberships": presentable}
