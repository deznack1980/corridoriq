"""Read-only view of shared CorridorIQ intelligence used for book matching.

Opens the shared database with SQLite's read-only URI mode — this module cannot
write to it. Builds in-memory lookup tables (names, phones, addresses,
licenses, permit activity, lane relevance). The index holds ONLY shared
intelligence; it never sees, stores, or caches any tenant's book, so one index
can be reused across tenants without carrying data between them.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from pipeline.book_match import normalize as N

_ROC_TRUSTED = ("VERIFIED_MATCH", "HIGH_CONFIDENCE_MATCH")
_CHANNEL_OK = ("VERIFIED", "CANDIDATE")
_PHONE_TYPES = ("business_phone", "office", "sales_contact", "owner", "manager", "estimator",
                "purchasing", "operations")
_TOKEN_POSTING_CAP = 400  # very common tokens are too weak to generate candidates


def open_readonly(db_path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("intelligence database not found")
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


@dataclass
class CompanyFacts:
    company_id: int
    display_name: str
    city: str | None = None
    name_keys: set = field(default_factory=set)
    phones: dict = field(default_factory=dict)       # phone_key -> verification status
    streets: set = field(default_factory=set)        # (street_key, zip5, city_key)
    licenses: set = field(default_factory=set)
    cities: set = field(default_factory=set)
    is_person: bool = False
    recent_permits: int = 0
    prior_permits: int = 0
    last_permit_date: str | None = None
    lanes: set = field(default_factory=set)


class IntelIndex:
    def __init__(self):
        self.companies: dict[int, CompanyFacts] = {}
        self.by_name: dict[str, set] = defaultdict(set)
        self.by_token: dict[str, set] = defaultdict(set)
        self.by_phone: dict[str, set] = defaultdict(set)
        self.by_street: dict[str, set] = defaultdict(set)
        self.by_license: dict[str, set] = defaultdict(set)
        self.fingerprint = ""
        self.as_of: date | None = None
        self.window_days = 0

    # ---------------------------------------------------------------- build
    @classmethod
    def load(cls, conn: sqlite3.Connection, *, as_of: date, window_days: int) -> "IntelIndex":
        idx = cls()
        idx.as_of, idx.window_days = as_of, window_days
        for r in conn.execute(
                "SELECT id, display_name, legal_name, dba_name, city, license_number FROM companies "
                "WHERE lifecycle_state='active' AND merged_into_id IS NULL"):
            f = CompanyFacts(r["id"], r["display_name"] or r["legal_name"] or "", city=r["city"])
            for raw in (r["display_name"], r["legal_name"], r["dba_name"]):
                k = N.name_key(raw)
                if k:
                    f.name_keys.add(k)
            ck = N.city_key(r["city"])
            if ck:
                f.cities.add(ck)
            lk = N.license_key(r["license_number"])
            if lk:
                f.licenses.add(lk)
            f.is_person = N.person_name(f.display_name)
            idx.companies[r["id"]] = f
        for r in conn.execute("SELECT company_id, alias_name FROM company_aliases"):
            f = idx.companies.get(r["company_id"])
            k = N.name_key(r["alias_name"])
            if f and k:
                f.name_keys.add(k)
        ph = ",".join("?" * len(_PHONE_TYPES))
        st = ",".join("?" * len(_CHANNEL_OK))
        for r in conn.execute(
                f"SELECT company_id, contact_type, contact_value, normalized_value, verification_status "
                f"FROM company_contact_channels WHERE status='active' AND verification_status IN ({st}) "
                f"AND (contact_type IN ({ph}) OR contact_type='business_address')",
                (*_CHANNEL_OK, *_PHONE_TYPES)):
            f = idx.companies.get(r["company_id"])
            if not f:
                continue
            if r["contact_type"] == "business_address":
                a = N.address_parts(full=r["contact_value"])
                if a["street"]:
                    f.streets.add((a["street"], a["zip5"], a["city"]))
                    if a["city"]:
                        f.cities.add(a["city"])
            else:
                p = N.phone_key(r["normalized_value"] or r["contact_value"])
                if p and f.phones.get(p) != "VERIFIED":
                    f.phones[p] = r["verification_status"]
        rs = ",".join("?" * len(_ROC_TRUSTED))
        for r in conn.execute(
                f"SELECT m.company_id, m.normalized_license_number, l.address_line_1, l.city, l.postal_code "
                f"FROM roc_company_matches m LEFT JOIN roc_licenses l ON l.id = m.roc_license_id "
                f"WHERE m.match_status IN ({rs})", _ROC_TRUSTED):
            f = idx.companies.get(r["company_id"])
            if not f:
                continue
            lk = N.license_key(r["normalized_license_number"])
            if lk:
                f.licenses.add(lk)
            a = N.address_parts(street=r["address_line_1"], city=r["city"], postal=r["postal_code"])
            if a["street"]:
                f.streets.add((a["street"], a["zip5"], a["city"]))
            if a["city"]:
                f.cities.add(a["city"])
        idx._load_activity(conn)
        idx._load_lanes(conn)
        idx._build_lookups()
        n_companies = len(idx.companies)
        newest = conn.execute(
            "SELECT MAX(substr(COALESCE(NULLIF(TRIM(issued_date),''), filed_date),1,10)) AS d FROM permits"
        ).fetchone()["d"]
        idx.fingerprint = f"companies={n_companies};newest_permit={newest};as_of={as_of.isoformat()}"
        return idx

    def _load_activity(self, conn):
        recent_start = (self.as_of - timedelta(days=self.window_days)).isoformat()
        prior_start = (self.as_of - timedelta(days=2 * self.window_days)).isoformat()
        end = self.as_of.isoformat()
        for r in conn.execute(
                """SELECT contractor_company_id AS cid,
                          SUM(CASE WHEN d > ? AND d <= ? THEN 1 ELSE 0 END) AS recent,
                          SUM(CASE WHEN d > ? AND d <= ? THEN 1 ELSE 0 END) AS prior,
                          MAX(CASE WHEN d <= ? THEN d END) AS last_d
                   FROM (SELECT contractor_company_id,
                                substr(COALESCE(NULLIF(TRIM(issued_date),''), filed_date),1,10) AS d
                         FROM permits WHERE contractor_company_id IS NOT NULL)
                   GROUP BY contractor_company_id""",
                (recent_start, end, prior_start, recent_start, end)):
            f = self.companies.get(r["cid"])
            if f:
                f.recent_permits, f.prior_permits, f.last_permit_date = r["recent"] or 0, r["prior"] or 0, r["last_d"]

    def _load_lanes(self, conn):
        try:
            rows = conn.execute(
                "SELECT company_id, lane_key FROM company_sales_lanes "
                "WHERE presentable=1 AND fit IN ('HIGH','MEDIUM')").fetchall()
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            f = self.companies.get(r["company_id"])
            if f:
                f.lanes.add(r["lane_key"])

    def _build_lookups(self):
        for cid, f in self.companies.items():
            for k in f.name_keys:
                self.by_name[k].add(cid)
                for t in N.distinctive(k):
                    self.by_token[t].add(cid)
            for p in f.phones:
                self.by_phone[p].add(cid)
            for street, _zip, _city in f.streets:
                self.by_street[street].add(cid)
            for lk in f.licenses:
                self.by_license[lk].add(cid)
        for t in [t for t, ids in self.by_token.items() if len(ids) > _TOKEN_POSTING_CAP]:
            del self.by_token[t]

    # --------------------------------------------------------------- query
    def get(self, company_id) -> CompanyFacts | None:
        return self.companies.get(company_id)
