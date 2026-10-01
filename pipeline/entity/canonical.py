"""Additive canonical mapping. Never merges, never rewrites project FKs."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.company_resolution.merge import add_alias
from pipeline.config.settings import ENTITY_MATCH_VERSION, ENTITY_MODEL_VERSION
from pipeline.db.database import now_iso
from pipeline.entity.classify import AUTO_LINK, SAME_HIGH, review_duplicate_candidates
from pipeline.entity.names import compact_company_name, distinctive_tokens, token_jaccard

# Extra clusters that share compact names but not necessarily a ROC license pair.
WATCH_COMPACT = (
    "RCI SYSTEMS",
    "KERNS PLUMBING",
    "RP GAS PIPING",
    "GAS PIPING",
    "WHITING TURNER CONTRACTING",
    "WHITING TURNER",
)


def _forbidden_counts(conn: sqlite3.Connection) -> dict:
    return {
        "merged": conn.execute(
            "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
        ).fetchone()["n"],
        "crm": conn.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"],
    }


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, x: int) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: int) -> int:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return out


def _ensure_canonical(
    conn: sqlite3.Connection,
    *,
    name: str,
    primary_id: int,
    recommendation: str,
    notes: str,
    now: str,
) -> int:
    norm = compact_company_name(name)
    existing = conn.execute(
        """
        SELECT id FROM canonical_companies
        WHERE normalized_canonical_name=? AND model_version=?
        """,
        (norm, ENTITY_MODEL_VERSION),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO canonical_companies (
            canonical_name, normalized_canonical_name, primary_company_id,
            recommendation, notes, model_version, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (name, norm, primary_id, recommendation, notes, ENTITY_MODEL_VERSION, now, now),
    )
    return int(cur.lastrowid)


def _link(
    conn: sqlite3.Connection,
    raw_id: int,
    canonical_id: int,
    rel: str,
    confidence: float,
    evidence: dict,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO company_entity_links (
            raw_company_id, canonical_company_id, relationship_type,
            match_confidence, evidence, matching_version, created_at
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(raw_company_id, canonical_company_id, relationship_type) DO UPDATE SET
            match_confidence=excluded.match_confidence,
            evidence=excluded.evidence
        """,
        (
            raw_id,
            canonical_id,
            rel,
            confidence,
            json.dumps(evidence),
            ENTITY_MATCH_VERSION,
            now,
        ),
    )
    raw = conn.execute(
        "SELECT display_name FROM companies WHERE id=?", (raw_id,)
    ).fetchone()
    if raw and raw["display_name"]:
        add_alias(conn, raw_id, raw["display_name"], source="entity-link-v1")


def link_reviewed_duplicates(conn: sqlite3.Connection) -> dict:
    """Create canonical clusters for HIGH/PROBABLE pairs only."""
    now = now_iso()
    before = _forbidden_counts(conn)
    uf = _UnionFind()
    pair_meta: dict[tuple[int, int], dict] = {}
    for row in conn.execute(
        """
        SELECT company_id_a, company_id_b, classification, canonical_recommendation,
               evidence, normalized_license_number
        FROM entity_duplicate_reviews
        WHERE matching_version=?
        """,
        (ENTITY_MATCH_VERSION,),
    ):
        a, b = int(row["company_id_a"]), int(row["company_id_b"])
        pair_meta[(min(a, b), max(a, b))] = dict(row)
        if row["classification"] in AUTO_LINK:
            uf.union(a, b)

    created = 0
    linked = 0
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        names = []
        for cid in members:
            n = conn.execute("SELECT display_name FROM companies WHERE id=?", (cid,)).fetchone()
            if n:
                names.append((cid, n["display_name"]))
        rec = SAME_HIGH
        rec_name = names[0][1] if names else str(members[0])
        notes = "canonical mapping only; source rows preserved; project FKs unchanged"
        for i, cid_a in enumerate(members):
            for cid_b in members[i + 1 :]:
                meta = pair_meta.get((min(cid_a, cid_b), max(cid_a, cid_b)))
                if meta:
                    rec = meta["classification"]
                    rec_name = meta["canonical_recommendation"] or rec_name
        cid0 = min(members)
        existing = conn.execute(
            f"SELECT canonical_company_id FROM company_entity_links "
            f"WHERE raw_company_id IN ({','.join('?' * len(members))}) LIMIT 1",
            members,
        ).fetchone()
        if existing:
            can_id = int(existing["canonical_company_id"])
        else:
            can_id = _ensure_canonical(
                conn, name=rec_name, primary_id=cid0, recommendation=rec, notes=notes, now=now
            )
            created += 1
        for cid in members:
            rel = "license_variant"
            conf = 92.0 if rec == SAME_HIGH else 80.0
            _link(
                conn,
                cid,
                can_id,
                rel,
                conf,
                {"classification": rec, "members": members, "do_not_merge": True},
                now,
            )
            linked += 1
    conn.commit()
    after = _forbidden_counts(conn)
    if after != before:
        raise RuntimeError(f"canonical linking mutated merge/CRM state: {before} -> {after}")
    return {"clusters": created, "links": linked, "merged_rows": after["merged"], "crm_rows": after["crm"]}


def link_compact_name_clusters(conn: sqlite3.Connection) -> dict:
    """Link known punctuation/initials variants (RCI, Kerns, Gas Piping, RP, Whiting)."""
    now = now_iso()
    before = _forbidden_counts(conn)
    rows = conn.execute(
        """
        SELECT id, display_name, normalized_name FROM companies
        WHERE lifecycle_state='active'
        """
    ).fetchall()
    by_key: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = compact_company_name(row["display_name"] or row["normalized_name"])
        if key in WATCH_COMPACT:
            by_key[key].append(dict(row))
    # Whiting Turner short form belongs with contracting form when distinctive overlap.
    extra = [dict(r) for r in rows if compact_company_name(r["display_name"]).startswith("WHITING TURNER")]
    if extra:
        by_key["WHITING TURNER"] = extra

    linked = 0
    clusters = 0
    for key, members in by_key.items():
        if len(members) < 2:
            continue
        # Require compact equality or high distinctive overlap; never fuzzy-alone VERIFIED.
        filtered = []
        for m in members:
            ok = compact_company_name(m["display_name"]) == key or key == "WHITING TURNER"
            if ok:
                filtered.append(m)
        if len(filtered) < 2:
            continue
        # Drop weak Whiting attachments (e.g. unrelated) via distinctive token check.
        if key == "WHITING TURNER":
            filtered = [
                m
                for m in filtered
                if "WHITING" in distinctive_tokens(m["display_name"])
                and "TURNER" in distinctive_tokens(m["display_name"])
            ]
            if len(filtered) < 2:
                continue
        rec_name = filtered[0]["display_name"]
        primary = min(int(m["id"]) for m in filtered)
        ids = [int(m["id"]) for m in filtered]
        existing = conn.execute(
            f"SELECT canonical_company_id FROM company_entity_links "
            f"WHERE raw_company_id IN ({','.join('?' * len(ids))}) LIMIT 1",
            ids,
        ).fetchone()
        if existing:
            can_id = int(existing["canonical_company_id"])
        else:
            can_id = _ensure_canonical(
                conn,
                name=rec_name,
                primary_id=primary,
                recommendation=SAME_HIGH,
                notes="compact/initials variant; source names preserved",
                now=now,
            )
            clusters += 1
        for m in filtered:
            rel = "initials_variant" if key in {"RCI SYSTEMS", "RP GAS PIPING"} else "name_variant"
            _link(
                conn,
                int(m["id"]),
                can_id,
                rel,
                90.0,
                {
                    "compact": key,
                    "raw_name": m["display_name"],
                    "jaccard_peer": token_jaccard(m["display_name"], rec_name),
                    "do_not_merge": True,
                },
                now,
            )
            linked += 1
    conn.commit()
    after = _forbidden_counts(conn)
    if after != before:
        raise RuntimeError("compact linking mutated merge/CRM state")
    return {"clusters": clusters, "links": linked}


def link_presentation_pair(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    canonical_name: str,
    evidence: dict,
    relationship_type: str = "name_variant",
    confidence: float = 92.0,
) -> dict:
    """Additive presentation cluster. Never merges rows or rewrites project FKs."""
    now = now_iso()
    before = _forbidden_counts(conn)
    ids = sorted({int(x) for x in company_ids})
    if len(ids) < 2:
        return {"canonical_id": None, "links": 0, "skipped": "need_two_raw_rows"}
    existing = conn.execute(
        f"SELECT canonical_company_id FROM company_entity_links "
        f"WHERE raw_company_id IN ({','.join('?' * len(ids))}) LIMIT 1",
        ids,
    ).fetchone()
    if existing:
        can_id = int(existing["canonical_company_id"])
    else:
        can_id = _ensure_canonical(
            conn,
            name=canonical_name,
            primary_id=ids[0],
            recommendation=SAME_HIGH,
            notes="presentation collapse only; source rows preserved; project FKs unchanged",
            now=now,
        )
    for cid in ids:
        payload = dict(evidence)
        payload["do_not_merge"] = True
        payload["members"] = ids
        _link(conn, cid, can_id, relationship_type, confidence, payload, now)
    conn.commit()
    after = _forbidden_counts(conn)
    if after != before:
        raise RuntimeError(f"presentation link mutated merge/CRM state: {before} -> {after}")
    return {
        "canonical_id": can_id,
        "links": len(ids),
        "merged_rows": after["merged"],
        "crm_rows": after["crm"],
    }


def run_entity_resolution(conn: sqlite3.Connection) -> dict:
    review = review_duplicate_candidates(conn)
    linked = link_reviewed_duplicates(conn)
    compact = link_compact_name_clusters(conn)
    return {"review": review, "license_links": linked, "compact_links": compact}
