"""Extract source records, source identities and identity evidence from data
CorridorIQ already holds. Reads production READ-ONLY; writes only to shadow.

One source record may assert zero, one or several identities (e.g. a Goodyear
permit names a general contractor AND an owner; a Peoria permit names an
applicant organization AND a person). Original strings are preserved;
normalized keys are stored alongside. Raw payloads are referenced by
production row id, never copied.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from pipeline.identity_shadow import normalize as N
from pipeline.identity_shadow.config import NORMALIZER_VERSION, PARSER_VERSION, family_class

log = logging.getLogger("corridoriq.identity_shadow")

# Party fields per jurisdiction, from the actual raw payload structures.
# Jurisdictions without party fields (Gilbert, Buckeye, Chandler) contribute
# activity only — reported as a coverage gap, never filled in.
PARTY_SPECS: dict[str, list[dict]] = {
    "phoenix_az": [{"role": "contractor", "name": "PROFESS_NAME"}],
    "scottsdale_az": [
        {"role": "responsible_party", "name": "ResponsibleParty"},
        {"role": "builder", "name": "Builder"},
        {"role": "owner", "name": "Owner"},
    ],
    "mesa_az": [
        {"role": "contractor", "name": "contractor_name", "street": "contractor_address"},
        {"role": "applicant", "name": "applicant"},
    ],
    "peoria_az": [
        {"role": "applicant_org", "name": "Applicant_Contact_Organization", "phone": "ContactPH"},
        {"role": "applicant_contact", "name": "Applicant_Contact_Name", "contact_person": True},
    ],
    "tempe_az": [{
        "role": "contractor", "name": "ContractorCompanyName", "license": "ContractorLicNum",
        "phone": "ContractorPhone", "street": "ContractorAddress1", "city": "ContractorCity",
        "zip": "ContractorZip", "email": "ContractorEmail",
    }],
    "goodyear_az": [
        {"role": "general_contractor", "name": "GenConName", "license": "GenConLicenseNumber",
         "phone_parts": ("GenConPhoneAreaCode", "GenConPhoneNumber"),
         "street_parts": ("GenConStreetNumber", "GenConStreetDirection", "GenConStreetName", "GenConStreetType"),
         "city": "GenConCity", "zip": "GenConZipCode"},
        {"role": "owner", "name": "OwnerName", "phone_parts": ("OwnerPhoneAreaCode", "OwnerPhoneNumber"),
         "street_parts": ("OwnerStreetNumber", "OwnerStreetDirection", "OwnerStreetName", "OwnerStreetType"),
         "city": "OwnerCity", "zip": "OwnerZip"},
    ],
    "gilbert_az": [], "buckeye_az": [], "chandler_az": [],
}
# Roles that describe a person attached to a record, not a business identity.
CONTACT_ROLES = {"applicant_contact"}
PHONE_CHANNEL_TYPES = {"business_phone", "office", "sales_contact", "owner", "manager", "estimator",
                       "purchasing", "operations"}


def _id(*parts) -> str:
    return hashlib.sha1("\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()[:24]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Writer:
    """Idempotent batched upserts into the shadow store."""

    def __init__(self, shadow, batch_id):
        self.s, self.batch = shadow, batch_id
        self.records, self.identities, self.evidence = [], [], []
        self.counts = defaultdict(int)
        self.now = _now()

    def record(self, key, family, system, rec_id, table, row_id, version, current, payload_hash, observed):
        self.records.append((key, family, system, str(rec_id), table, row_id, version, 1 if current else 0,
                             payload_hash, observed, self.batch, self.batch))

    def identity(self, ident: dict, evidence: list[tuple]):
        self.identities.append(ident)
        self.counts[f"identities:{ident['family']}"] += 1
        for ev_type, value_key, original, strength, status in evidence:
            if not value_key:
                continue
            state = "SUPERSEDED" if ident["state"] == "SUPERSEDED" else "CURRENT"
            self.evidence.append((
                _id(ident["identity_id"], ev_type, value_key), ident["identity_id"], ident["family"], ev_type,
                value_key, original, strength, status, state, ident.get("observed_at"), self.now,
                NORMALIZER_VERSION))
        if len(self.identities) >= 5000:
            self.flush()

    def flush(self):
        c = self.s
        c.executemany(
            """INSERT INTO source_records (record_key, family, source_system, source_record_id, prod_table,
                   prod_row_id, version, is_current, payload_hash, observed_at, first_batch_id, last_batch_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(record_key) DO UPDATE SET is_current=excluded.is_current,
                   payload_hash=excluded.payload_hash, last_batch_id=excluded.last_batch_id""", self.records)
        c.executemany(
            """INSERT INTO source_identities (identity_id, record_key, family, role, entity_kind, eligible, state,
                   original_name, name_key, dba_original, dba_key, phone_key, street_key, zip5, city_key,
                   email_key, domain_key, license_key, prod_company_id, prod_permit_id, observed_at,
                   parser_version, normalizer_version, first_batch_id, last_batch_id)
               VALUES (:identity_id,:record_key,:family,:role,:entity_kind,:eligible,:state,:original_name,
                   :name_key,:dba_original,:dba_key,:phone_key,:street_key,:zip5,:city_key,:email_key,
                   :domain_key,:license_key,:prod_company_id,:prod_permit_id,:observed_at,:parser_version,
                   :normalizer_version,:batch,:batch)
               ON CONFLICT(identity_id) DO UPDATE SET state=excluded.state, eligible=excluded.eligible,
                   entity_kind=excluded.entity_kind, prod_company_id=excluded.prod_company_id,
                   last_batch_id=excluded.last_batch_id, parser_version=excluded.parser_version,
                   normalizer_version=excluded.normalizer_version""",
            [dict(i, batch=self.batch, parser_version=PARSER_VERSION, normalizer_version=NORMALIZER_VERSION)
             for i in self.identities])
        c.executemany(
            """INSERT INTO identity_evidence (evidence_id, identity_id, family, evidence_type, value_key,
                   original_value, strength, source_status, state, observed_at, ingested_at, normalizer_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(evidence_id) DO UPDATE SET state=excluded.state, source_status=excluded.source_status,
                   normalizer_version=excluded.normalizer_version""",
            self.evidence)
        self.records, self.identities, self.evidence = [], [], []


def _identity(record_key, family, role, name_raw, *, state="CURRENT", phone=None, street=None, zip_=None,
              city=None, email=None, website=None, license_raw=None, prod_company_id=None, prod_permit_id=None,
              observed_at=None, eligible_override=None, position=0):
    """Build one identity dict + its evidence list. Returns (None, reason) when
    the name is a placeholder."""
    name_raw = N.clean_text(name_raw)
    if not name_raw or N.is_placeholder(name_raw):
        return None, "placeholder"
    legal, dba = N.split_dba(name_raw)
    kind = N.entity_kind(legal)
    lic = N.license_key(license_raw)
    eligible = kind != "PERSON" or bool(lic)
    if role in CONTACT_ROLES:
        eligible = False
    if eligible_override is not None:
        eligible = eligible_override
    email_k = N.email_key(email)
    domain = N.domain_from_email(email) or N.domain_from_url(website)
    ident = {
        "identity_id": _id(record_key, role, position, N.name_key(name_raw)),
        "record_key": record_key, "family": family, "role": role, "entity_kind": kind,
        "eligible": 1 if eligible else 0, "state": state, "original_name": name_raw,
        "name_key": N.name_key(legal), "dba_original": dba, "dba_key": N.name_key(dba),
        "phone_key": phone, "street_key": street, "zip5": zip_, "city_key": city,
        "email_key": email_k, "domain_key": domain, "license_key": lic,
        "prod_company_id": prod_company_id, "prod_permit_id": prod_permit_id, "observed_at": observed_at,
    }
    lic_strength = "STRONG" if family in ("roc",) or family.startswith("permit:") else "CORROBORATING"
    ev = [("name", ident["name_key"], legal, "WEAK", None),
          ("dba", ident["dba_key"], dba, "WEAK", None),
          ("phone", phone, None, "CORROBORATING", None),
          ("address", f"{street}|{zip_ or ''}" if street else None, None, "CORROBORATING", None),
          ("email", email_k, None, "CORROBORATING", None),
          ("domain", domain, None, "CORROBORATING", None),
          ("license", lic, license_raw, lic_strength, None)]
    return (ident, ev), None


# ------------------------------------------------------------------ permits
def extract_permits(prod, w: _Writer, cfg) -> dict:
    stats = defaultdict(int)
    permit_index = {}
    for r in prod.execute(
            "SELECT p.id, p.jurisdiction, p.permit_number, p.general_contractor_name, p.plumbing_contractor_name, "
            "p.owner_name, p.contractor_company_id, "
            "substr(COALESCE(NULLIF(TRIM(p.issued_date),''), p.filed_date),1,10) AS d, "
            "(SELECT owner_company_id FROM projects pr WHERE pr.permit_id=p.id) AS owner_company_id "
            "FROM permits p"):
        permit_index[(r["jurisdiction"], r["permit_number"])] = dict(r)
    families = cfg.families_limit
    seen_current: dict[tuple, set] = {}
    represented: dict[int, set] = defaultdict(set)
    rows = prod.execute(
        "SELECT raw_record_id, source_system, source_record_id, payload_json, payload_hash, version_number, "
        "is_current, fetched_at FROM raw_record WHERE source_entity_type='permit' "
        "ORDER BY source_system, source_record_id, is_current DESC, version_number DESC")
    for r in rows:
        system = r["source_system"]
        family = f"permit:{system}"
        if families and family not in families:
            continue
        specs = PARTY_SPECS.get(system)
        if specs is None:
            stats["unknown_jurisdiction_records"] += 1
            continue
        if not r["is_current"] and not cfg.include_history:
            continue
        try:
            payload = json.loads(r["payload_json"])
        except (TypeError, ValueError):
            stats["unparseable_payloads"] += 1
            continue
        if isinstance(payload, dict) and isinstance(payload.get("attributes"), dict):
            payload = payload["attributes"]
        if not isinstance(payload, dict):
            stats["unparseable_payloads"] += 1
            continue
        permit = permit_index.get((system, r["source_record_id"]))
        permit_id = permit["id"] if permit else None
        observed = (permit or {}).get("d") or (r["fetched_at"] or "")[:10] or None
        current = bool(r["is_current"])
        record_key = _id("raw_record", r["raw_record_id"])
        w.record(record_key, family, system, r["source_record_id"], "raw_record", r["raw_record_id"],
                 r["version_number"], current, r["payload_hash"], observed)
        stats["records"] += 1
        group_key = (system, r["source_record_id"])
        if current:
            seen_current[group_key] = set()
        for pos, spec in enumerate(specs):
            raw_name = payload.get(spec["name"])
            if not N.clean_text(raw_name):
                continue
            phone = N.phone_key(payload.get(spec["phone"])) if spec.get("phone") else None
            if spec.get("phone_parts"):
                phone = N.phone_from_parts(*(payload.get(k) for k in spec["phone_parts"]))
            street = N.street_key(payload.get(spec["street"])) if spec.get("street") else None
            if spec.get("street_parts"):
                street = N.street_from_parts(*(payload.get(k) for k in spec["street_parts"]))
            zip_ = N.zip5(payload.get(spec["zip"])) if spec.get("zip") else None
            city = N.city_key(payload.get(spec["city"])) if spec.get("city") else None
            built, why = _identity(
                record_key, family, spec["role"], raw_name, state="CURRENT" if current else "SUPERSEDED",
                phone=phone, street=street, zip_=zip_, city=city, email=payload.get(spec.get("email") or ""),
                license_raw=payload.get(spec["license"]) if spec.get("license") else None,
                prod_permit_id=permit_id if current else None, observed_at=observed, position=pos)
            if built is None:
                stats[f"skipped_{why}"] += 1
                continue
            ident, ev = built
            sig = (spec["role"], ident["name_key"], ident["phone_key"], ident["street_key"], ident["license_key"])
            if current:
                seen_current[group_key].add(sig)
                if permit_id:
                    represented[permit_id].add(ident["name_key"])
                    ident["prod_company_id"] = _prod_company_for(permit, spec["role"], ident)
            else:
                if sig in seen_current.get(group_key, set()):
                    continue  # same identity as the current version: not new evidence
                stats["historical_identities"] += 1
            w.identity(ident, ev)
    # Production-recorded names not represented by the raw extraction (keeps the
    # comparison with the production universe complete).
    for (system, num), p in permit_index.items():
        family = f"permit:{system}"
        if families and family not in families:
            continue
        for role, col, company_col in (("recorded_contractor", "general_contractor_name", "contractor_company_id"),
                                       ("recorded_contractor", "plumbing_contractor_name", "contractor_company_id"),
                                       ("recorded_owner", "owner_name", "owner_company_id")):
            name = p.get(col)
            key = N.name_key(N.split_dba(name)[0]) if N.clean_text(name) else None
            if not key or key in represented.get(p["id"], set()):
                continue
            record_key = _id("permit", p["id"])
            w.record(record_key, family, system, num, "permits", p["id"], 1, True, None, p["d"])
            built, why = _identity(record_key, family, role, name, prod_permit_id=p["id"],
                                   prod_company_id=p.get(company_col), observed_at=p["d"], position=col)
            if built is None:
                stats[f"skipped_{why}"] += 1
                continue
            represented[p["id"]].add(key)
            stats["recorded_fallback_identities"] += 1
            w.identity(*built)
    w.flush()
    return dict(stats)


def _prod_company_for(permit, role, ident):
    """Link an extracted identity to the production company only when the
    production record names the same party (exact compact-name agreement)."""
    if not permit:
        return None
    if role == "owner":
        name, cid = permit.get("owner_name"), permit.get("owner_company_id")
    else:
        name = permit.get("general_contractor_name") or permit.get("plumbing_contractor_name")
        cid = permit.get("contractor_company_id")
    if cid and N.name_key(N.split_dba(name)[0] if name else None) in {ident["name_key"], ident["dba_key"]}:
        return cid
    return None


# ------------------------------------------------------------------ ROC roster
def extract_roc(prod, w: _Writer, cfg) -> dict:
    if cfg.families_limit and "roc" not in cfg.families_limit:
        return {}
    stats = defaultdict(int)
    for r in prod.execute(
            "SELECT id, normalized_license_number, raw_license_number, raw_business_name, raw_dba, "
            "normalized_class, raw_class_type, normalized_status, issued_date, expiration_date, "
            "qualifying_party, address_line_1, city, postal_code, phone, email, corridor_capability "
            "FROM roc_licenses"):
        record_key = _id("roc", r["normalized_license_number"])
        w.record(record_key, "roc", "roc_posting_list", r["normalized_license_number"], "roc_licenses", r["id"],
                 1, True, None, r["issued_date"])
        name = r["raw_business_name"]
        built, why = _identity(
            record_key, "roc", "license_holder", name, phone=N.phone_key(r["phone"]),
            street=N.street_key(r["address_line_1"]), zip_=N.zip5(r["postal_code"]), city=N.city_key(r["city"]),
            email=r["email"], license_raw=r["normalized_license_number"], observed_at=r["issued_date"],
            eligible_override=True)
        if built is None:
            stats[f"skipped_{why}"] += 1
            continue
        ident, ev = built
        if r["raw_dba"] and not ident["dba_key"]:
            ident["dba_original"], ident["dba_key"] = N.clean_text(r["raw_dba"]), N.name_key(r["raw_dba"])
            ev.append(("dba", ident["dba_key"], ident["dba_original"], "WEAK", None))
        ev += [("license_class", r["normalized_class"], r["raw_class_type"], "ATTRIBUTE", r["normalized_status"]),
               ("trade_capability", r["corridor_capability"], None, "ATTRIBUTE", None),
               ("qualifying_party", N.name_key(r["qualifying_party"]), None, "ATTRIBUTE", None)]
        w.identity(ident, ev)
        stats["identities"] += 1
    w.flush()
    return dict(stats)


# ------------------------------------------------------------- ROC crosswalk
def extract_roc_crosswalk(prod, w: _Writer, cfg) -> dict:
    """CorridorIQ's own ROC→company candidates. Hint-only evidence (never
    independent, never a seed): records which license the existing matcher
    associated with a production company."""
    if cfg.families_limit and "roc_crosswalk" not in cfg.families_limit:
        return {}
    by_company = defaultdict(dict)
    dates = {}
    for r in prod.execute("SELECT company_id, field_name, value, source_date FROM roc_contact_candidates"):
        by_company[r["company_id"]][r["field_name"]] = r["value"]
        dates[r["company_id"]] = r["source_date"]
    n = 0
    for cid, f in by_company.items():
        if not f.get("legal_name"):
            continue
        record_key = _id("roc_crosswalk", cid)
        w.record(record_key, "roc_crosswalk", "roc_contact_candidates", cid, "roc_contact_candidates", None,
                 1, True, None, dates.get(cid))
        built, _ = _identity(record_key, "roc_crosswalk", "prod_crosswalk", f.get("legal_name"),
                             street=N.street_key(f.get("address_line_1")), zip_=N.zip5(f.get("postal_code")),
                             city=N.city_key(f.get("city")), license_raw=f.get("license_number"),
                             prod_company_id=cid, observed_at=dates.get(cid), eligible_override=False)
        if built is None:
            continue
        ident, ev = built
        if f.get("dba_name") and not ident["dba_key"]:
            ident["dba_original"], ident["dba_key"] = f["dba_name"], N.name_key(f["dba_name"])
        ev = [(t, k, o, s, "candidate") for t, k, o, s, _ in ev]
        w.identity(ident, ev)
        n += 1
    w.flush()
    return {"identities": n}


# ------------------------------------------------------------- contact channels
def extract_contact_channels(prod, w: _Writer, cfg) -> dict:
    """Existing contact channels, grouped per (production company, source
    family). The channel was researched FOR that production company, so the
    identity carries the production company's name as a reference — it is not
    a name the contact source asserted independently."""
    names = {r["id"]: r["display_name"] for r in prod.execute("SELECT id, display_name FROM companies")}
    groups = defaultdict(list)
    for r in prod.execute(
            "SELECT company_id, contact_type, contact_value, normalized_value, contact_name, source_family, "
            "verification_status, status, verified_at, discovered_at FROM company_contact_channels"):
        groups[(r["company_id"], r["source_family"])].append(dict(r))
    stats = defaultdict(int)
    for (cid, sf), rows in groups.items():
        family = f"contact:{sf}"
        if cfg.families_limit and family not in cfg.families_limit:
            continue
        record_key = _id("contact", cid, sf)
        observed = max((x["verified_at"] or x["discovered_at"] or "") for x in rows)[:10] or None
        w.record(record_key, family, "company_contact_channels", f"{cid}:{sf}", "company_contact_channels",
                 None, 1, True, None, observed)
        phones = [(x, N.phone_key(x["normalized_value"] or x["contact_value"])) for x in rows
                  if x["contact_type"] in PHONE_CHANNEL_TYPES]
        addrs = [x for x in rows if x["contact_type"] == "business_address"]
        emails = [x for x in rows if x["contact_type"] == "business_email"]
        sites = [x for x in rows if x["contact_type"] == "website"]
        verified_phone = next((p for x, p in phones if p and x["verification_status"] == "VERIFIED"), None)
        any_phone = verified_phone or next((p for _, p in phones if p), None)
        addr = next((x for x in addrs if x["verification_status"] == "VERIFIED"), addrs[0] if addrs else None)
        parts = [p.strip() for p in (addr["contact_value"] if addr else "").split(",")]
        built, why = _identity(
            record_key, family, "published_contact", names.get(cid),
            phone=any_phone, street=N.street_key(parts[0] if parts else None),
            zip_=N.zip5(addr["contact_value"]) if addr else None,
            city=N.city_key(parts[1]) if len(parts) >= 3 else None,
            email=(emails[0]["contact_value"] if emails else None),
            website=(sites[0]["contact_value"] if sites else None), prod_company_id=cid, observed_at=observed)
        if built is None:
            stats[f"skipped_{why}"] += 1
            continue
        ident, ev = built
        for x, p in phones:
            ev.append(("phone", p, None, "CORROBORATING", x["verification_status"]))
        for x in addrs:
            a = [q.strip() for q in x["contact_value"].split(",")]
            k = N.street_key(a[0] if a else None)
            if k:
                ev.append(("address", f"{k}|{N.zip5(x['contact_value']) or ''}", None, "CORROBORATING",
                           x["verification_status"]))
        for x in emails:
            ev.append(("email", N.email_key(x["contact_value"]), None, "CORROBORATING", x["verification_status"]))
        for x in sites:
            ev.append(("domain", N.domain_from_url(x["contact_value"]), None, "CORROBORATING",
                       x["verification_status"]))
        for x in rows:
            if x["contact_type"] in ("named_contact", "qualifying_party") and x["contact_name"]:
                ev.append(("contact_person", N.name_key(x["contact_name"]), None, "ATTRIBUTE",
                           x["verification_status"]))
        # Later entries carry the source's verification status; identical
        # evidence IDs collapse on insert, so each value is stored once.
        w.identity(ident, ev)
        stats["identities"] += 1
    w.flush()
    return dict(stats)


def register_provenance(shadow):
    origins = {
        "permit": "Municipal permit portals already ingested by CorridorIQ (raw_record / permits)",
        "roc": "Arizona ROC posting list already held (roc_licenses)",
        "roc_crosswalk": "CorridorIQ's own ROC-to-company candidates (roc_contact_candidates)",
    }
    fams = {r[0] for r in shadow.execute("SELECT DISTINCT family FROM source_identities")}
    for f in fams:
        cls, ind = family_class(f)
        origin = origins.get(f.split(":")[0], "Existing contact channels (company_contact_channels)")
        shadow.execute(
            "INSERT INTO source_provenance (family, family_class, independent, origin) VALUES (?,?,?,?) "
            "ON CONFLICT(family) DO UPDATE SET family_class=excluded.family_class, independent=excluded.independent, "
            "origin=excluded.origin", (f, cls, ind, origin))
