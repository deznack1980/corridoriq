"""Account relevance and the trust-gated daily queue, for the sales portal.

Relevance comes from the sales-lane assignment (company_sales_lanes), which is
where the pipeline decides whether an account belongs in a supply-house selling
motion. It is not a blacklist: an equipment dealer, a municipality, a permit
service, and any future company like them fall out because of their lanes, not
their names.

Nothing here writes. Internal scores are used only for ordering and are never
returned; the portal gets labels through pipeline.trust.public.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pipeline.trust import scope as S
from pipeline.trust.evidence import LANE_VERSION, PROFILE_KEY
from pipeline.trust.singleflight import SingleFlightCache

CORE_LANES = tuple(S.CALLABLE_LANES)  # plumbing core, fuel gas / propane
ADJACENT_LANES = ("FIRE_BACKFLOW", "CIVIL_WET_UTILITY", "HVAC_MECHANICAL", "GENERAL_CONTRACTOR_CM", "MULTI_TRADE")
REVIEW_LANE = "IDENTITY_REVIEW"
BOOK_FITS = ("HIGH", "MEDIUM")

CORE = "CORE"
ADJACENT = "ADJACENT"
NOT_ASSESSED = "NOT_ASSESSED"
NOT_RELEVANT = "NOT_RELEVANT"
NOT_SALES_READY = "NOT_SALES_READY"

RANK = {CORE: 0, ADJACENT: 1, NOT_ASSESSED: 2, NOT_RELEVANT: 3, NOT_SALES_READY: 4}
# Statuses a "priority" surface may promote. The rest stay visible in full lists.
PROMOTABLE = (CORE, ADJACENT, NOT_ASSESSED)

LABELS = {
    CORE: "Plumbing / fuel-gas account",
    ADJACENT: "Adjacent wet-side account",
    NOT_ASSESSED: "Not yet assessed",
    NOT_RELEVANT: "Outside plumbing-supply focus",
    NOT_SALES_READY: "Not a sales account (applicant, owner, or person record)",
}
LANE_LABELS = {
    "PLUMBING_CORE": "Plumbing",
    "FUEL_GAS_PROPANE": "Fuel gas / propane",
    "FIRE_BACKFLOW": "Fire line / backflow",
    "CIVIL_WET_UTILITY": "Civil wet utility",
    "HVAC_MECHANICAL": "HVAC / mechanical",
    "GENERAL_CONTRACTOR_CM": "General contractor (influences plumbing purchasing)",
    "MULTI_TRADE": "Multi-trade",
    "SPECIALTY_OTHER": "Specialty / other",
}


def status_from_lanes(rows: list[dict]) -> str:
    if not rows:
        return NOT_ASSESSED
    book = {
        r["lane_key"]
        for r in rows
        if r.get("fit") in BOOK_FITS and int(r.get("presentable") or 0) and r["lane_key"] != REVIEW_LANE
    }
    if not book:
        return NOT_SALES_READY if any(r["lane_key"] == REVIEW_LANE for r in rows) else NOT_RELEVANT
    if book & set(CORE_LANES):
        return CORE
    if book & set(ADJACENT_LANES):
        return ADJACENT
    return NOT_RELEVANT


def relevance_for(conn: sqlite3.Connection, company_ids: list[int]) -> dict[int, dict]:
    ids = sorted({int(i) for i in company_ids})
    lanes: dict[int, list[dict]] = {i: [] for i in ids}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"""
                SELECT company_id, lane_key, fit, presentable FROM company_sales_lanes
                WHERE model_version = ? AND company_id IN ({marks})
                """,
                (LANE_VERSION, *chunk),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            lanes[int(r["company_id"])].append(dict(r))
    out = {}
    for cid, rows in lanes.items():
        status = status_from_lanes(rows)
        book = sorted(
            {r["lane_key"] for r in rows if r.get("fit") in BOOK_FITS and int(r.get("presentable") or 0)}
            - {REVIEW_LANE}
        )
        out[cid] = {
            "status": status,
            "label": LABELS[status],
            "rank": RANK[status],
            "lanes": [LANE_LABELS.get(k, k.replace("_", " ").title()) for k in book],
        }
    return out


def _in(values) -> str:
    return ",".join(f"'{v}'" for v in values)


def relevance_sql(company_col: str = "c.id") -> tuple[str, str, list]:
    """(join, rank_expr, params) for paged SQL. Mirrors status_from_lanes; a test
    checks the two agree. Lane keys are module constants, not user input."""
    book = f"presentable = 1 AND fit IN ({_in(BOOK_FITS)})"
    join = f"""
        LEFT JOIN (
            SELECT company_id,
                   MAX(CASE WHEN {book} AND lane_key IN ({_in(CORE_LANES)}) THEN 1 ELSE 0 END) AS core,
                   MAX(CASE WHEN {book} AND lane_key IN ({_in(ADJACENT_LANES)}) THEN 1 ELSE 0 END) AS adj,
                   MAX(CASE WHEN {book} AND lane_key <> '{REVIEW_LANE}' THEN 1 ELSE 0 END) AS any_book,
                   MAX(CASE WHEN lane_key = '{REVIEW_LANE}' THEN 1 ELSE 0 END) AS review
            FROM company_sales_lanes WHERE model_version = ? GROUP BY company_id
        ) trl ON trl.company_id = {company_col}
        LEFT JOIN company_customer_priority tap
               ON tap.company_id = {company_col} AND tap.profile_key = ?
    """
    rank = (
        f"(CASE WHEN trl.company_id IS NULL THEN {RANK[NOT_ASSESSED]} "
        f"WHEN trl.core = 1 THEN {RANK[CORE]} "
        f"WHEN trl.adj = 1 THEN {RANK[ADJACENT]} "
        f"WHEN trl.any_book = 0 AND trl.review = 1 THEN {RANK[NOT_SALES_READY]} "
        f"ELSE {RANK[NOT_RELEVANT]} END)"
    )
    return join, rank, [LANE_VERSION, PROFILE_KEY]


def within_account_order() -> str:
    """Secondary order inside a relevance rank. The value is never returned."""
    return "COALESCE(tap.account_priority_score, 0) DESC"


# ------------------------------------------------------------ project rows


def annotate_projects(
    rows: list[dict],
    *,
    include_all: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Label each project row with its own trade scope and, by default, drop:

    - rows whose permit names only a non-wet trade (racking, signage, solar...);
    - when conn is given (company-bound feeds), rows whose account is outside the
      plumbing-supply focus or not a sales account.

    Blank or general building permits stay, labelled as such. include_all keeps
    every row with its labels. Needs permit_type and description columns;
    missing columns read as not stated."""
    relevance: dict[int, dict] = {}
    if conn is not None:
        relevance = relevance_for(conn, [r["company_id"] for r in rows if r.get("company_id") is not None])
    out = []
    for row in rows:
        d = dict(row)
        ts = S.project_trade_scope(
            permit_type=d.pop("permit_type", None),
            permit_subtype=d.pop("permit_subtype", None),
            description=d.get("description"),
            project_description=d.pop("project_description", None),
        )
        rel = relevance.get(d.get("company_id")) if relevance else None
        off_focus = rel is not None and rel["status"] not in PROMOTABLE
        if not include_all and (ts["state"] == S.NOT_WET_STATED or off_focus):
            continue
        d["trade_scope"] = ts["label"]
        d["scope_state"] = ts["state"]
        if rel is not None:
            d["account_relevance"] = {"status": rel["status"], "label": rel["label"], "lanes": list(rel["lanes"])}
        out.append(d)
    return out


def _permit_location(conn: sqlite3.Connection, permit_id: int) -> dict | None:
    from pipeline.trust.geo import permit_location

    try:
        row = conn.execute(
            "SELECT latitude, longitude, jurisdiction, raw_source_json FROM permits WHERE id = ?",
            (int(permit_id),),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else permit_location(dict(row))


# ------------------------------------------------------------ daily queue

# One entry per data signature + day; concurrent first requests (or the
# portal's background warmer and a request) compute the queue once.
_QUEUE = SingleFlightCache(max_entries=2)


def clear_cache() -> None:
    _QUEUE.clear()


def queue_cached(conn: sqlite3.Connection, as_of: datetime | None = None) -> bool:
    from pipeline.trust.dates import phoenix_now

    return _QUEUE.peek(_cache_key(conn, as_of or phoenix_now()))


def _cache_key(conn: sqlite3.Connection, as_of: datetime) -> tuple:
    try:
        db = conn.execute("PRAGMA database_list").fetchone()[2]
    except sqlite3.Error:
        db = None
    if not db:  # in-memory or temporary database: never share a cache entry
        db = f"conn:{id(conn)}"
    else:  # normalized so URI (read-only) and plain-path connections share keys
        import os

        db = os.path.normcase(os.path.abspath(db))
    try:
        run = conn.execute(
            "SELECT id, completed_at FROM pipeline_runs WHERE status IN ('succeeded','success','completed') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run = None if run is None else (run[0], run[1])
    except sqlite3.Error:
        run = None
    try:
        lanes = conn.execute("SELECT MAX(created_at) FROM company_sales_lanes").fetchone()[0]
    except sqlite3.Error:
        lanes = None
    return (db, run, lanes, as_of.date().isoformat())


def daily_queue(conn: sqlite3.Connection, as_of: datetime | None = None) -> dict:
    """The trust-gated result for today, computed once per data refresh."""
    from pipeline.trust.dates import phoenix_now
    from pipeline.trust.opportunities import assemble

    as_of = as_of or phoenix_now()
    return _QUEUE.get_or_compute(_cache_key(conn, as_of), lambda: assemble(conn, as_of))


def todays_accounts(
    conn: sqlite3.Connection,
    *,
    company_ids: set[int] | None = None,
    exclude_ids: set[int] | None = None,
    as_of: datetime | None = None,
    limit: int = 15,
) -> dict:
    """Public payload: trust-gated actions, optionally limited to company_ids."""
    from pipeline.trust.public import public_card, public_data_status

    result = daily_queue(conn, as_of)
    exclude_ids = exclude_ids or set()

    def keep(card):
        cid = card["company"]["company_id"]
        return (company_ids is None or cid in company_ids) and cid not in exclude_ids

    from pipeline.trust.contacts import verified_contact_view

    items = []
    for card in (c for c in result["queue"] if keep(c)):
        pub = public_card(card)
        contact = verified_contact_view(conn, pub["company_id"])
        # Fail closed: a Call needs a displayable verified phone on this
        # company's record, an Email a verified email. Otherwise do not list it.
        need = {"Call": "phone", "Email": "email"}.get(pub["action"])
        if need and not (contact["actionable"] and contact[need]):
            continue
        pub["contact_details"] = contact
        pub["project"]["location"] = _permit_location(conn, card["observed"]["permit_id"])
        items.append(pub)
        if len(items) >= limit:
            break
    queue = items
    return {
        "items": items,
        "data_status": public_data_status(result["health"]),
        "note": (
            None
            if queue
            else "No account passed every check today. The list is empty on purpose; it is not padded."
        ),
    }
