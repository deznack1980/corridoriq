"""Coverage KPI comparison for the frozen plumbing-core cohort."""

from __future__ import annotations

from pipeline.contactability.coverage import account_contact_summary


def _from_summary(summary: dict) -> dict:
    conf = summary.get("contact_confidence") or "none"
    actionable = bool(summary.get("actionable"))
    return {
        "has_phone": bool(summary.get("has_phone")),
        "has_email": bool(summary.get("has_email")),
        "has_website": bool(summary.get("has_website")),
        "has_form": bool(summary.get("has_form")),
        "has_named": bool(summary.get("has_named")),
        "has_purchasing_ops": bool(summary.get("has_purchasing_ops")),
        "has_owner": bool(summary.get("has_owner")),
        "actionable": actionable,
        "verified": actionable and conf == "VERIFIED",
        "candidate_only": actionable and conf == "CANDIDATE",
        "no_contact": not actionable,
    }


def summarize_kpis(flags: list[dict]) -> dict:
    n = len(flags) or 1
    keys = (
        "has_phone",
        "has_email",
        "has_website",
        "has_form",
        "has_named",
        "has_purchasing_ops",
        "has_owner",
        "actionable",
        "verified",
        "candidate_only",
        "no_contact",
    )
    counts = {k: sum(1 for f in flags if f.get(k)) for k in keys}
    pct = {k: round(100.0 * v / n, 1) for k, v in counts.items()}
    return {"n": len(flags), "counts": counts, "pct": pct}


def before_kpis(frozen: list[dict]) -> dict:
    flags = []
    for rec in frozen:
        conf = rec.get("contact_confidence") or "none"
        actionable = bool(rec.get("actionable"))
        flags.append(
            {
                "has_phone": bool(rec.get("has_phone") or rec.get("phone")),
                "has_email": bool(rec.get("has_email") or rec.get("email")),
                "has_website": bool(rec.get("has_website") or rec.get("website")),
                "has_form": False,
                "has_named": bool(rec.get("has_named")),
                "has_purchasing_ops": False,
                "has_owner": False,
                "actionable": actionable,
                "verified": actionable and conf == "VERIFIED",
                "candidate_only": actionable and conf == "CANDIDATE",
                "no_contact": not actionable,
            }
        )
    return summarize_kpis(flags)


def after_kpis(conn, frozen_or_enriched: list[dict]) -> dict:
    flags = []
    for rec in frozen_or_enriched:
        summary = rec.get("after")
        if summary is None:
            summary = account_contact_summary(conn, int(rec["primary_company_id"]))
        flags.append(_from_summary(summary))
    return summarize_kpis(flags)


def existing_inventory(conn, frozen: list[dict]) -> dict:
    rows = []
    for rec in frozen:
        cid = int(rec["primary_company_id"])
        chans = [
            dict(r)
            for r in conn.execute(
                """
                SELECT contact_type, contact_value, source_family, verification_status,
                       contact_name, original_title, decision_maker_class
                FROM company_contact_channels
                WHERE company_id=? AND status='active'
                ORDER BY contact_type, id
                """,
                (cid,),
            )
        ]
        rows.append(
            {
                "rank": rec["presentation_rank"],
                "company": rec["canonical_name"],
                "company_id": cid,
                "channel_count": len(chans),
                "sources": sorted({c["source_family"] for c in chans}),
                "channels": chans,
            }
        )
    return {
        "accounts_with_any_channel": sum(1 for r in rows if r["channel_count"]),
        "rows": rows,
    }
