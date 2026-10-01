"""Shadow-to-production diff, coverage metrics and source contribution.
Production is read (read-only connection); results go to the shadow store."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from pipeline.identity_shadow import normalize as N
from pipeline.identity_shadow.config import family_class

CONFIDENT = ("VERIFIED", "HIGH_CONFIDENCE")


def _families_by_canonical(s) -> dict:
    fams = defaultdict(set)
    for r in s.execute("SELECT canonical_id, family FROM identity_signatures WHERE canonical_id IS NOT NULL"):
        fams[r["canonical_id"]].add(r["family"])
    return fams


def production_baseline(prod) -> dict:
    """What production knows per company today (for 'new evidence')."""
    known = defaultdict(lambda: {"license": False, "phone": False, "address": False, "email": False,
                                 "families": set()})
    for r in prod.execute("SELECT id, license_number FROM companies"):
        k = known[r["id"]]
        k["families"].add("permits")
        if N.license_key(r["license_number"]):
            k["license"] = True
    for r in prod.execute("SELECT DISTINCT company_id FROM roc_company_matches "
                          "WHERE match_status IN ('VERIFIED_MATCH','HIGH_CONFIDENCE_MATCH')"):
        known[r["company_id"]]["license"] = True
        known[r["company_id"]]["families"].add("roc")
    for r in prod.execute("SELECT company_id, contact_type, source_family, verification_status "
                          "FROM company_contact_channels WHERE status='active'"):
        k = known[r["company_id"]]
        t = r["contact_type"]
        if t in ("business_phone", "office"):
            k["phone"] = True
        elif t == "business_address":
            k["address"] = True
        elif t == "business_email":
            k["email"] = True
        if r["verification_status"] == "VERIFIED":
            k["families"].add("contact:" + r["source_family"])
    return known


def analyze(s, prod) -> dict:
    out: dict = {}
    s.execute("DELETE FROM shadow_diff")
    s.execute("DELETE FROM shadow_metrics")
    canon = {r["canonical_id"]: dict(r) for r in s.execute("SELECT * FROM canonical_companies")}
    fams = _families_by_canonical(s)
    prod_names = {r["id"]: r["display_name"] for r in prod.execute("SELECT id, display_name FROM companies")}

    # identity → canonical, per production company (crosswalk hints excluded).
    p2c, held_prod = defaultdict(set), defaultdict(int)
    for r in s.execute(
            "SELECT si.prod_company_id AS pid, il.canonical_id AS cid FROM source_identities si "
            "JOIN identity_links il ON il.identity_id = si.identity_id "
            "WHERE si.prod_company_id IS NOT NULL AND si.family <> 'roc_crosswalk'"):
        if r["cid"]:
            p2c[r["pid"]].add(r["cid"])
        else:
            held_prod[r["pid"]] += 1
    c2p = defaultdict(set)
    for pid, cids in p2c.items():
        for cid in cids:
            c2p[cid].add(pid)

    diff_rows = []
    resolved_conf = unresolved = 0
    split_reasons = Counter()
    for pid in prod_names:
        cids = p2c.get(pid, set())
        if not cids:
            unresolved += 1
            diff_rows.append(("UNREPRESENTED", str(pid), json.dumps({
                "held_identities": held_prod.get(pid, 0),
                "reason": "all identities held (person names)" if held_prod.get(pid) else
                          "no current source identity names this company"})))
            continue
        if len(cids) == 1 and canon[next(iter(cids))]["confidence_state"] in CONFIDENT:
            resolved_conf += 1
        if len(cids) > 1:
            states = sorted({canon[c]["confidence_state"] for c in cids})
            locs = s.execute(
                f"SELECT COUNT(DISTINCT COALESCE(NULLIF(city_key,''), NULLIF(zip5,''))) FROM company_addresses "
                f"WHERE canonical_id IN ({','.join('?' * len(cids))}) "
                f"AND COALESCE(NULLIF(city_key,''), NULLIF(zip5,'')) IS NOT NULL", tuple(cids)).fetchone()[0]
            if "CONFLICT" in states:
                reason = "license/name conflict inside the production company"
            elif locs >= 2:
                reason = "separately evidenced business locations"
            elif "REVIEW_REQUIRED" in states:
                reason = "ambiguous name across separately located businesses"
            else:
                reason = "names or identifiers the shadow rules do not corroborate as one business"
            split_reasons[reason] += 1
            diff_rows.append(("WOULD_SPLIT", str(pid), json.dumps({
                "shadow_entities": len(cids), "states": states, "reason": reason,
                "entities": sorted(cids)[:10]})))
    merge_rules = defaultdict(set)
    for r in s.execute("SELECT canonical_id, rule FROM resolution_decisions WHERE canonical_id IS NOT NULL"):
        merge_rules[r["canonical_id"]].add(r["rule"])
    would_merge = 0
    for cid, pids in c2p.items():
        if len(pids) > 1:
            would_merge += 1
            diff_rows.append(("WOULD_MERGE", cid, json.dumps({
                "production_companies": sorted(pids)[:20], "count": len(pids),
                "state": canon[cid]["confidence_state"], "rules": sorted(merge_rules[cid])})))
    s.executemany("INSERT INTO shadow_diff VALUES (?,?,?)", diff_rows)

    roc_only = [c for c, row in canon.items() if fams.get(c) == {"roc"}]
    no_prod = [c for c, row in canon.items() if not c2p.get(c)]
    out["diff"] = {
        "production_companies": len(prod_names),
        "shadow_canonical_companies": len(canon),
        "production_resolved_confidently": resolved_conf,
        "production_represented": len(prod_names) - unresolved,
        "production_unrepresented": unresolved,
        "production_would_split": sum(1 for r in diff_rows if r[0] == "WOULD_SPLIT"),
        "would_split_reasons": dict(split_reasons),
        "shadow_would_merge_entities": would_merge,
        "production_companies_in_would_merge": sum(len(p) for p in c2p.values() if len(p) > 1),
        "shadow_entities_without_production_company": len(no_prod),
        "roc_only_entities": len(roc_only),
        "roc_only_sole_proprietors": sum(1 for c in roc_only if canon[c]["entity_kind"] == "SOLE_PROPRIETOR"),
        "licensed_without_observed_activity": sum(1 for r in canon.values()
                                                  if r["licensed_without_observed_activity"]),
    }

    # New evidence for production-linked companies vs what production knows.
    known = production_baseline(prod)
    have = defaultdict(lambda: {"license": False, "phone": False, "address": False, "email": False})
    # Shared (non-identifying) phones/addresses never count as new evidence.
    for table, key, where in (("company_licenses", "license", "1=1"), ("company_phones", "phone", "shared=0"),
                              ("company_emails", "email", "1=1")):
        for r in s.execute(f"SELECT DISTINCT canonical_id FROM {table} WHERE {where}"):
            have[r["canonical_id"]][key] = True
    for r in s.execute("SELECT DISTINCT canonical_id FROM company_addresses WHERE shared=0"):
        have[r["canonical_id"]]["address"] = True
    new_ev = Counter()
    multi_prod_before = multi_prod_after = 0
    for pid, cids in p2c.items():
        k = known[pid]
        if len(k["families"]) >= 2:
            multi_prod_before += 1
        if len(cids) == 1:
            c = next(iter(cids))
            if canon[c]["corroborated_family_count"] >= 2:
                multi_prod_after += 1
            for t in ("license", "phone", "address", "email"):
                if have[c][t] and not k[t]:
                    new_ev[t] += 1
    out["new_evidence"] = {**{f"production_companies_gaining_{k}": v for k, v in new_ev.items()},
                           "production_companies_multi_source_before": multi_prod_before,
                           "production_companies_corroborated_multi_source_after": multi_prod_after}

    # Coverage metrics.
    m = Counter()
    m["canonical_companies"] = len(canon)
    for r in canon.values():
        m[f"state:{r['confidence_state']}"] += 1
        m[f"kind:{r['entity_kind']}"] += 1
        fc = r["family_count"]
        if fc == 1:
            m["families:1"] += 1
        if fc >= 2:
            m["families:2+"] += 1
        if fc >= 3:
            m["families:3+"] += 1
        if fc >= 4:
            m["families:4+"] += 1
        if r["independent_family_count"] >= 2:
            m["independent_families:2+"] += 1
        if r["corroborated_family_count"] >= 2:
            m["corroborated_families:2+"] += 1
        if r["has_license"]:
            m["with_roc_identity"] += 1
        if r["recent_permit_count"]:
            m["with_recent_permit_activity"] += 1
        if r["permit_count"]:
            m["with_any_permit_activity"] += 1
    for table, key, extra in (("company_addresses", "with_address", "shared=0"), ("company_phones", "with_phone", "shared=0"),
                              ("company_phones", "with_verified_phone", "verified=1"),
                              ("company_emails", "with_email", "1=1"), ("company_domains", "with_domain", "shared=0"),
                              ("company_name_relationships", "with_dba_relationship", "1=1")):
        m[key] = s.execute(f"SELECT COUNT(DISTINCT canonical_id) FROM {table} WHERE {extra}").fetchone()[0]
    m["with_trade_evidence"] = s.execute(
        "SELECT COUNT(*) FROM canonical_companies WHERE has_license=1 OR permit_count>0").fetchone()[0]
    link_counts = Counter(r[0] for r in s.execute("SELECT link_state FROM identity_links"))
    total_ids = sum(link_counts.values())
    held = link_counts.get("HELD", 0)
    m["source_identities_considered"] = total_ids
    m["held_identities"] = held
    m["orphan_source_identity_rate"] = round(held / total_ids, 4) if total_ids else 0
    m["resolution_rate"] = round((total_ids - held) / total_ids, 4) if total_ids else 0
    n = len(canon) or 1
    m["review_rate"] = round(m["state:REVIEW_REQUIRED"] / n, 4)
    m["conflict_rate"] = round(m["state:CONFLICT"] / n, 4)
    m["would_merge_rate"] = round(out["diff"]["production_companies_in_would_merge"] / max(len(prod_names), 1), 4)
    m["would_split_rate"] = round(out["diff"]["production_would_split"] / max(len(prod_names), 1), 4)
    m["new_entities_created"] = len(no_prod)
    m["existing_entities_enriched"] = sum(1 for pid, cids in p2c.items() if len(cids) == 1 and
                                          any(have[next(iter(cids))][t] and not known[pid][t]
                                              for t in ("license", "phone", "address", "email")))
    reviews = Counter(r[0] for r in s.execute("SELECT review_kind FROM resolution_reviews"))
    out["reviews"] = dict(reviews)
    out["metrics"] = dict(m)

    # Source contribution.
    contrib = {}
    fam_ident = Counter(r[0] for r in s.execute("SELECT family FROM source_identities si JOIN identity_links il "
                                                "USING (identity_id)"))
    fam_held = Counter(r[0] for r in s.execute("SELECT si.family FROM source_identities si JOIN identity_links il "
                                               "USING (identity_id) WHERE il.canonical_id IS NULL"))
    review_fams = Counter()
    conflict_fams = Counter()
    for r in s.execute("SELECT review_kind, families FROM resolution_reviews"):
        for f in r[1].split(","):
            review_fams[f] += 1
            if "CONFLICT" in r[0]:
                conflict_fams[f] += 1
    for f in sorted(fam_ident):
        cids = [c for c, fs in fams.items() if f in fs]
        row = {
            "class": family_class(f)[0], "independent": family_class(f)[1],
            "source_identities": fam_ident[f], "held_identities": fam_held.get(f, 0),
            "entities_touched": len(cids),
            "new_entities_contributed": sum(1 for c in cids if not c2p.get(c)),
            "existing_entities_enriched": sum(1 for c in cids if c2p.get(c) and len(fams[c]) >= 2),
            "conflicts_introduced": conflict_fams.get(f, 0), "review_cases_introduced": review_fams.get(f, 0),
        }
        for table, key, not_shared in (("company_addresses", "addresses", "shared=0"), ("company_phones", "phones", "shared=0"),
                                       ("company_emails", "emails", "1=1"), ("company_licenses", "licenses", "1=1"),
                                       ("company_domains", "domains", "shared=0")):
            like = (f"%,{f},%",)
            row[f"{key}_contributed"] = s.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {not_shared} AND ','||families||',' LIKE ?", like).fetchone()[0]
            row[f"{key}_unique"] = s.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {not_shared} AND families=?", (f,)).fetchone()[0]
            if not_shared != "1=1":
                row[f"{key}_shared_non_identifying"] = s.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE shared=1 AND ','||families||',' LIKE ?", like).fetchone()[0]
        contrib[f] = row
    out["source_contribution"] = contrib

    rows = [(k, "all", float(v)) for k, v in m.items()]
    rows += [(f"diff:{k}", "all", float(v)) for k, v in out["diff"].items() if isinstance(v, (int, float))]
    for f, row in contrib.items():
        rows += [(k, f, float(v)) for k, v in row.items() if isinstance(v, (int, float))]
    s.executemany("INSERT INTO shadow_metrics VALUES (?,?,?)", rows)
    return out
