"""Synthetic SQLite fixture for Morning Operator evals and tests.

Rows mirror the four truth cards from the read-only audit plus the other
gate scenarios. Company ids for the truth cards match production so the
truth-card path is exercised. Nothing here touches the production database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

AS_OF = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
LANE_VERSION = "sales-lane-v1"
PROFILE = "plumbing_supply"

SCHEMA = """
CREATE TABLE jurisdictions (slug TEXT PRIMARY KEY, name TEXT, status TEXT);
CREATE TABLE permits (
    id INTEGER PRIMARY KEY, jurisdiction TEXT, permit_number TEXT, permit_type TEXT,
    permit_subtype TEXT, status TEXT, description TEXT, project_description TEXT,
    issued_date TEXT, filed_date TEXT, job_address TEXT, city TEXT, zip TEXT,
    general_contractor_name TEXT, plumbing_contractor_name TEXT, permit_url TEXT,
    first_seen_at TEXT, last_updated_at TEXT
);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY, permit_id INTEGER, jurisdiction TEXT, contractor_company_id INTEGER,
    project_category TEXT, opportunity_date TEXT, opportunity_date_basis TEXT, opportunity_score REAL
);
CREATE TABLE companies (
    id INTEGER PRIMARY KEY, display_name TEXT, legal_name TEXT, normalized_name TEXT,
    lifecycle_state TEXT, merged_into_id INTEGER
);
CREATE TABLE company_sales_lanes (
    company_id INTEGER, lane_key TEXT, fit TEXT, subtype TEXT, presentable INTEGER,
    evidence TEXT, model_version TEXT, created_at TEXT
);
CREATE TABLE company_customer_priority (
    profile_key TEXT, company_id INTEGER, account_priority_score REAL, trade_identity TEXT,
    why_now TEXT, most_recent_relevant_date TEXT, primary_demand_category TEXT,
    relevant_30d INTEGER, relevant_90d INTEGER, attribution_role TEXT, generated_at TEXT,
    model_version TEXT
);
CREATE TABLE company_capabilities (
    company_id INTEGER, capability TEXT, confidence REAL, attribution_role TEXT,
    attribution_confidence REAL, capability_class TEXT, last_evidence_date TEXT
);
CREATE TABLE company_contact_channels (
    id INTEGER PRIMARY KEY, company_id INTEGER, contact_type TEXT, contact_value TEXT,
    normalized_value TEXT, verification_status TEXT, source_family TEXT,
    public_business_contact INTEGER DEFAULT 1, contact_name TEXT, title TEXT,
    decision_maker_class TEXT, verified_at TEXT, is_primary INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active'
);
CREATE TABLE roc_licenses (id INTEGER PRIMARY KEY, normalized_class TEXT, raw_class TEXT);
CREATE TABLE roc_company_matches (
    company_id INTEGER, roc_license_id INTEGER, normalized_license_number TEXT,
    match_status TEXT, match_confidence REAL
);
CREATE TABLE sales_identity_reviews (
    company_id INTEGER, recommended_identity_status TEXT, legal_entity TEXT, dba TEXT,
    remaining_conflicts TEXT, model_version TEXT, created_at TEXT
);
CREATE TABLE entity_match_overrides (
    company_id INTEGER, original_match_status TEXT, recommended_match_status TEXT,
    applied INTEGER, matching_version TEXT
);
CREATE TABLE canonical_companies (id INTEGER PRIMARY KEY, canonical_name TEXT);
CREATE TABLE company_entity_links (
    raw_company_id INTEGER, canonical_company_id INTEGER, relationship_type TEXT,
    match_confidence REAL, reviewed_at TEXT
);
CREATE TABLE entity_duplicate_reviews (
    company_id_a INTEGER, company_id_b INTEGER, classification TEXT, canonical_recommendation TEXT
);
CREATE TABLE source_health_snapshot (
    captured_at TEXT, jurisdiction_slug TEXT, health_state TEXT, consecutive_failures INTEGER,
    last_success_at TEXT, last_data_at TEXT, newest_source_date TEXT, failures_7d INTEGER, runs_7d INTEGER
);
CREATE TABLE pipeline_runs (
    id INTEGER PRIMARY KEY, run_type TEXT, status TEXT, started_at TEXT, completed_at TEXT,
    jurisdictions_attempted INTEGER, jurisdictions_succeeded INTEGER, jurisdictions_failed INTEGER,
    records_received INTEGER
);
"""

# company_id: (name, lanes, priority row, capabilities, roc status, contacts)
PARKER, KERNS, KERNS_LLC, UMBRELLA, ADVANCED = 37857, 38349, 47437, 40543, 40584
GC_HIGH, CLEAN, UNKNOWN_ROLE_CO, NO_CONTACT_CO, EXPEDITOR, STALE_SRC_CO = 50001, 50002, 50003, 50004, 50005, 50006


def _permit_rows():
    # (permit_id, jurisdiction, number, type, description, issued, company_id, gc_name, plumber_name)
    return [
        # Parker: newest permits are electrical; newest wet is propane tanks (46 days).
        (1, "peoria_az", "2603901", "Residential Electrical", "Installing EV charger circuit", "2026-09-20", PARKER, None, None),
        (2, "phoenix_az", "2603318", "Building",
         "INSTALL (2) 119 GAL A/G PROPANE TANKS — LIQ PROPANE GAS SYS A/G MOD INSTALL — Building",
         "2026-08-14", PARKER, None, None),
        (3, "peoria_az", "2602562", "Residential Electrical",
         "MPU FROM 125A TO NEW 200A SES W/150A MAIN BREAKER.", "2026-08-10", PARKER, None, None),
        (4, "phoenix_az", "2601111", "Plumbing", "Replace water heater 50 gal", "2026-06-29", PARKER, None, None),
        # Kerns 38349: newest permits are gas to pool heater.
        (10, "peoria_az", "2602642", "Residential Plumbing", "Gas line from meter to pool heater.", "2026-09-01", KERNS, None, None),
        (11, "peoria_az", "2602435", "Residential Plumbing", "GAS_48FT OF 2IN PE FROM METER TO HEATER (400K).", "2026-08-12", KERNS, None, None),
        (12, "peoria_az", "2601201", "Residential Plumbing", "Replace 50 gal water heater", "2026-05-06", KERNS, None, None),
        # Kerns L L C 47437: separate row, newer fire line.
        (20, "phoenix_az", "26010383", "Building",
         "MAJESTIC HILTON - FIRE LINE — UNDERGROUND FIRELINE & HYDRANT INSTALL — Building",
         "2026-09-15", KERNS_LLC, None, None),
        # Umbrella: gas-line replacement.
        (30, "mesa_az", "PMT26-11635", "Residential Plumbing",
         "Scope of Work:\nReplace the existing leaking 2-inch underground gas line like for like.\n"
         "Install a new 2-inch underground gas line from the existing gas meter riser to the existing pool heater.",
         "2026-09-10T00:00:00.000", UMBRELLA, None, None),
        # Advanced: July interior hot/cold line replacement.
        (40, "peoria_az", "2602293", "Residential Alteration", "Replace interior hot and cold lines LIKE FOR LIKE", "2026-07-21", ADVANCED, None, None),
        (41, "peoria_az", "2601586", "Residential Alteration", "Replace interior hot and cold lines LIKE FOR LIKE", "2026-06-08", ADVANCED, None, None),
        # High-score GC with plumbing scope.
        (50, "phoenix_az", "2604001", "Commercial Building",
         "Commercial TI - new restroom fixtures and water heater", "2026-09-20", GC_HIGH,
         "SUNSTATE COMMERCIAL BUILDERS", "ROADRUNNER PLUMBING"),
        # Clean recent plumbing specialist.
        (60, "phoenix_az", "2604100", "Plumbing", "Repipe entire house with PEX; replace water heater", "2026-09-24",
         CLEAN, None, "DESERT FLOW PLUMBING LLC"),
        # Unknown role.
        (70, "phoenix_az", "2604200", "Building", "Replace sewer line to street", "2026-09-22", UNKNOWN_ROLE_CO, None, None),
        # Missing contact.
        (80, "mesa_az", "PMT26-12001", "Plumbing", "Water service line replacement", "2026-09-23", NO_CONTACT_CO, None, None),
        # Permit expeditor.
        (90, "phoenix_az", "2604300", "Plumbing", "Water heater replacement", "2026-09-21", EXPEDITOR, None, None),
        # Plumbing specialist in a failing source.
        (95, "goodyear_az", "GY-26-0100", "Plumbing", "Repipe with PEX, new water lines", "2026-09-22", STALE_SRC_CO, None, None),
        # Unattributed wet permit and newest-record fillers.
        (100, "phoenix_az", "2604999", "Plumbing", "Replace water heater", "2026-09-27", None, None, None),
        (101, "peoria_az", "2609999", "Residential Electrical", "Panel upgrade", "2026-09-26", None, None, None),
        (102, "mesa_az", "PMT26-19999", "Electrical", "Solar PV", "2026-09-25T00:00:00.000", None, None, None),
        (103, "chandler_az", "CH-2025-1", "Plumbing", "Water heater", "2025-10-17", None, None, None),
    ]


# projects.opportunity_date precedes the issue date for these permits in production.
OPPORTUNITY_DATE = {10: "2026-08-06", 40: "2026-07-12"}


def _companies():
    return {
        PARKER: "PARKER & SONS, INC",
        KERNS: "KERNS PLUMBING",
        KERNS_LLC: "KERNS PLUMBING L L C",
        UMBRELLA: "UMBRELLA PLUMBING",
        ADVANCED: "ADVANCED PLUMBING AND PIPING",
        GC_HIGH: "SUNSTATE COMMERCIAL BUILDERS",
        CLEAN: "DESERT FLOW PLUMBING LLC",
        UNKNOWN_ROLE_CO: "ACME SERVICES LLC",
        NO_CONTACT_CO: "VALLEY PIPE WORKS",
        EXPEDITOR: "AZ PERMIT SERVICE LLC",
        STALE_SRC_CO: "WESTSIDE REPIPE PLUMBING",
    }


def build(path: Path, *, variant: str = "default") -> Path:
    """variant: default | no_trust (refresh 10 days old, every call gate should fail)."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for slug, name in (
        ("phoenix_az", "Phoenix"), ("peoria_az", "Peoria"), ("mesa_az", "Mesa"),
        ("goodyear_az", "Goodyear"), ("chandler_az", "Chandler"),
    ):
        conn.execute("INSERT INTO jurisdictions VALUES (?,?, 'connected')", (slug, name))
    for pid, jur, num, ptype, desc, issued, cid, gc, plumber in _permit_rows():
        conn.execute(
            """INSERT INTO permits (id, jurisdiction, permit_number, permit_type, status, description,
               issued_date, job_address, city, general_contractor_name, plumbing_contractor_name,
               first_seen_at, last_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, jur, num, ptype, "Issued", desc, issued, f"{pid} W Example St", jur.split("_")[0].title(),
             gc, plumber, "2026-09-25T03:00:00+00:00", "2026-09-28T03:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO projects (id, permit_id, jurisdiction, contractor_company_id, opportunity_date) VALUES (?,?,?,?,?)",
            (pid, pid, jur, cid, OPPORTUNITY_DATE.get(pid, issued[:10])),
        )
    for cid, name in _companies().items():
        conn.execute("INSERT INTO companies VALUES (?,?,?,?, 'active', NULL)", (cid, name, name, name))

    lanes = {
        PARKER: [("PLUMBING_CORE", "HIGH"), ("FUEL_GAS_PROPANE", "HIGH"), ("HVAC_MECHANICAL", "HIGH"), ("MULTI_TRADE", "HIGH")],
        KERNS: [("PLUMBING_CORE", "HIGH"), ("FUEL_GAS_PROPANE", "HIGH"), ("MULTI_TRADE", "HIGH")],
        KERNS_LLC: [("PLUMBING_CORE", "HIGH"), ("FUEL_GAS_PROPANE", "HIGH"), ("FIRE_BACKFLOW", "MEDIUM"), ("MULTI_TRADE", "HIGH")],
        UMBRELLA: [("PLUMBING_CORE", "HIGH")],
        ADVANCED: [("PLUMBING_CORE", "HIGH")],
        GC_HIGH: [("GENERAL_CONTRACTOR_CM", "HIGH")],
        CLEAN: [("PLUMBING_CORE", "HIGH")],
        UNKNOWN_ROLE_CO: [("PLUMBING_CORE", "MEDIUM")],
        NO_CONTACT_CO: [("PLUMBING_CORE", "HIGH")],
        EXPEDITOR: [("IDENTITY_REVIEW", "HIGH")],
        STALE_SRC_CO: [("PLUMBING_CORE", "HIGH")],
    }
    for cid, rows in lanes.items():
        for lane, fit in rows:
            conn.execute(
                "INSERT INTO company_sales_lanes VALUES (?,?,?,NULL,?,NULL,?,?)",
                (cid, lane, fit, 0 if lane == "IDENTITY_REVIEW" else 1, LANE_VERSION, "2026-09-19T17:00:00+00:00"),
            )

    priority = {
        PARKER: (80.4, "plumbing_specialist", "plumbing specialist; 3 relevant jobs in 90d; primary demand plumbing service; latest 2026-08-14.", "2026-08-14", "plumbing_service"),
        KERNS: (76.5, "plumbing_specialist", "plumbing specialist; 4 relevant jobs in 90d; primary demand water heater; latest 2026-08-06.", "2026-08-06", "water_heater"),
        KERNS_LLC: (62.4, "plumbing_specialist", "plumbing specialist; no relevant activity in 90d; primary demand fuel gas; latest 2026-06-11.", "2026-06-11", "fuel_gas"),
        UMBRELLA: (71.0, "plumbing_specialist", "plumbing specialist; 1 relevant jobs in 90d; primary demand plumbing service; latest 2026-06-30.", "2026-06-30", "plumbing_service"),
        ADVANCED: (57.7, "plumbing_specialist", "plumbing specialist; no relevant activity in 90d; primary demand plumbing service; latest 2026-05-18.", "2026-05-18", "plumbing_service"),
        GC_HIGH: (85.0, "gc_with_plumbing_demand", "gc with plumbing demand; 9 relevant jobs in 30d.", "2026-09-20", "commercial_plumbing"),
        CLEAN: (64.0, "plumbing_specialist", "plumbing specialist; 2 relevant jobs in 30d.", "2026-09-24", "plumbing_service"),
        UNKNOWN_ROLE_CO: (55.0, "unknown", "unknown; 1 relevant jobs in 30d.", "2026-09-22", "plumbing_service"),
        NO_CONTACT_CO: (60.0, "plumbing_specialist", "plumbing specialist; 1 relevant jobs in 30d.", "2026-09-23", "plumbing_service"),
        EXPEDITOR: (40.0, "unknown", "unknown.", "2026-09-21", "water_heater"),
        STALE_SRC_CO: (66.0, "plumbing_specialist", "plumbing specialist.", "2026-09-22", "plumbing_service"),
    }
    for cid, (score, ident, why, latest, primary) in priority.items():
        conn.execute(
            "INSERT INTO company_customer_priority VALUES (?,?,?,?,?,?,?,0,0,NULL,?,?)",
            (PROFILE, cid, score, ident, why, latest, primary, "2026-09-19T16:00:00+00:00", "account-priority-v1"),
        )

    trade = ("trade_contractor", 86.0, "specialist_capability")
    caps = {
        PARKER: [("plumbing", *trade), ("fuel_gas", *trade), ("electrical", *trade)],
        KERNS: [("plumbing", *trade), ("fuel_gas", *trade)],
        KERNS_LLC: [("plumbing", *trade), ("fuel_gas", *trade)],
        UMBRELLA: [("plumbing", *trade)],
        ADVANCED: [("plumbing", *trade)],
        GC_HIGH: [("plumbing", "gc_of_record", 80.0, "incidental_project_scope")],
        CLEAN: [("plumbing", *trade)],
        NO_CONTACT_CO: [("plumbing", *trade)],
        STALE_SRC_CO: [("plumbing", *trade)],
    }
    for cid, rows in caps.items():
        for cap, role, conf, klass in rows:
            conn.execute(
                "INSERT INTO company_capabilities VALUES (?,?,100.0,?,?,?,NULL)",
                (cid, cap, role, conf, klass),
            )

    conn.execute("INSERT INTO roc_licenses VALUES (1, 'CR-37', 'CR-37')")
    roc = {
        PARKER: "POSSIBLE_MATCH", KERNS: "VERIFIED_MATCH", UMBRELLA: "VERIFIED_MATCH",
        ADVANCED: "HIGH_CONFIDENCE_MATCH", GC_HIGH: "VERIFIED_MATCH", CLEAN: "VERIFIED_MATCH",
        UNKNOWN_ROLE_CO: "VERIFIED_MATCH", NO_CONTACT_CO: "VERIFIED_MATCH", STALE_SRC_CO: "VERIFIED_MATCH",
    }
    for cid, status in roc.items():
        conn.execute("INSERT INTO roc_company_matches VALUES (?,1,?,?,90.0)", (cid, str(cid), status))
    conn.execute(
        "INSERT INTO sales_identity_reviews VALUES (?,?,?,?,?,?,?)",
        (PARKER, "HIGH_CONFIDENCE", "Environmental Conditioning LLC", "Parker and Sons", "multi-trade", "sales-gate-v1", "2026-09-19"),
    )

    contact_id = 1
    def contact(cid, ctype, value, status="VERIFIED", source="prior_research", name=None):
        nonlocal contact_id
        normalized = "".join(ch for ch in value if ch.isdigit()) if ctype == "business_phone" else value.lower()
        conn.execute(
            """INSERT INTO company_contact_channels (id, company_id, contact_type, contact_value, normalized_value,
               verification_status, source_family, contact_name, title, decision_maker_class, verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (contact_id, cid, ctype, value, normalized, status, source, name, None, "UNKNOWN", "2026-08-04"),
        )
        contact_id += 1

    for cid in (PARKER, KERNS, UMBRELLA, ADVANCED, GC_HIGH, CLEAN, UNKNOWN_ROLE_CO, EXPEDITOR, STALE_SRC_CO):
        contact(cid, "business_phone", "602-555-0100")
    contact(KERNS_LLC, "business_phone", "602-555-0100", source="canonical_peer", name="Kerns peer")
    contact(NO_CONTACT_CO, "business_address", "1 Main St", status="CANDIDATE", source="roc")

    captured = "2026-09-29T03:00:30+00:00"
    health = {
        "phoenix_az": "healthy", "peoria_az": "healthy", "mesa_az": "healthy",
        "goodyear_az": "failing", "chandler_az": "healthy",
    }
    for slug, state in health.items():
        conn.execute(
            "INSERT INTO source_health_snapshot VALUES (?,?,?,0,?,?,NULL,0,14)",
            (captured, slug, state, "2026-09-29T03:00:20+00:00", "2026-09-29T03:00:20+00:00"),
        )
    run_day = "2026-09-19" if variant == "no_trust" else "2026-09-29"
    conn.execute(
        "INSERT INTO pipeline_runs VALUES (1, 'morning_refresh', 'succeeded', ?, ?, 5, 5, 0, 1200)",
        (f"{run_day}T03:00:00+00:00", f"{run_day}T03:01:00+00:00"),
    )
    conn.commit()
    conn.close()
    return path
