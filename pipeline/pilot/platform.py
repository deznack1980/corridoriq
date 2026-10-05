"""Pilot platform database and pilot root.

Accounts use the portal's own auth tables — extracted verbatim from
``pipeline/db/schema.sql`` — so ``pipeline.auth.service`` (password hashing,
lockout, sessions, audit) runs unchanged against this database. Pilot roles are
seeded here only; they are never added to the portal's role catalog.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import settings
from pipeline.config.settings import SCHEMA_PATH
from pipeline.tenancy.contractor import ContractorRegistry
from pipeline.tenancy.registry import TenantRegistry

ENV_ROOT = "CORRIDORIQ_PILOT_ROOT"
PLATFORM_FILENAME = "platform.db"
PILOT_COOKIE = "corridoriq_pilot_session"

AUTH_TABLES = ("organizations", "users", "sessions", "roles", "permissions",
               "role_permissions", "user_roles", "security_audit_log")

# Pilot roles (platform database only).
ROLE_CONTRACTOR_OWNER = "contractor_owner"
ROLE_CONTRACTOR_MEMBER = "contractor_member"
ROLE_SUPPLIER_INBOX = "supplier_inbox"
PILOT_ROLES = {
    ROLE_CONTRACTOR_OWNER: ("Contractor owner/admin", {"pilot.contractor.requests", "pilot.contractor.profile"}),
    ROLE_CONTRACTOR_MEMBER: ("Contractor member", {"pilot.contractor.requests"}),
    ROLE_SUPPLIER_INBOX: ("Supplier request inbox", {"pilot.supplier.inbox"}),
}
PILOT_PERMISSIONS = {
    "pilot.contractor.requests": "Create, edit and send this contractor's material requests",
    "pilot.contractor.profile": "Edit this contractor's organization profile",
    "pilot.supplier.inbox": "View material requests sent to this supplier",
}

_PILOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_meta (
    singleton  INTEGER PRIMARY KEY CHECK (singleton = 1),
    purpose    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- user -> tenant. kind: contractor | supplier. One membership per user.
CREATE TABLE IF NOT EXISTS memberships (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    tenant_kind TEXT NOT NULL CHECK (tenant_kind IN ('contractor', 'supplier')),
    tenant_id   TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memberships_tenant ON memberships(tenant_kind, tenant_id);
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id            INTEGER PRIMARY KEY REFERENCES users(id),
    preferred_language TEXT NOT NULL CHECK (preferred_language IN ('en', 'es')),
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contractor_profiles (
    tenant_id                    TEXT PRIMARY KEY,
    business_name                TEXT NOT NULL,
    owner_user_id                INTEGER NOT NULL REFERENCES users(id),
    phone                        TEXT,
    preferred_language           TEXT NOT NULL CHECK (preferred_language IN ('en', 'es')),
    created_at                   TEXT NOT NULL,
    acquisition_source           TEXT NOT NULL,
    acquisition_channel          TEXT NOT NULL,
    referral_code                TEXT,
    referring_supplier_tenant_id TEXT,
    utm_source                   TEXT,
    utm_medium                   TEXT,
    utm_campaign                 TEXT,
    utm_content                  TEXT,
    email_verification_status    TEXT NOT NULL DEFAULT 'PENDING_NOT_IMPLEMENTED',
    onboarded_at                 TEXT,
    -- Reserved reference to a shared-universe company. Never set by signup;
    -- account creation does not touch company identity resolution.
    linked_company_ref           TEXT
);
CREATE TABLE IF NOT EXISTS referral_codes (
    code               TEXT PRIMARY KEY,
    supplier_tenant_id TEXT NOT NULL,
    label              TEXT,
    active             INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_connections (
    connection_id        TEXT PRIMARY KEY,
    contractor_tenant_id TEXT NOT NULL,
    supplier_tenant_id   TEXT NOT NULL,
    source               TEXT NOT NULL,
    referral_code        TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE (contractor_tenant_id, supplier_tenant_id)
);
-- TRANSACTIONAL: a request the contractor explicitly sent to one supplier.
-- A snapshot is taken at send time; the contractor's private store is never
-- readable by the supplier.
CREATE TABLE IF NOT EXISTS request_shares (
    share_id             TEXT PRIMARY KEY,
    request_id           TEXT NOT NULL,
    contractor_tenant_id TEXT NOT NULL,
    supplier_tenant_id   TEXT NOT NULL,
    connection_id        TEXT NOT NULL,
    sender_user_id       INTEGER NOT NULL REFERENCES users(id),
    sent_at              TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('NEW', 'VIEWED', 'ACKNOWLEDGED')),
    contractor_name      TEXT NOT NULL,
    title                TEXT NOT NULL,
    reference            TEXT,
    -- Copied from the request at send time. Not recomputed, and not a response-time promise.
    priority             TEXT NOT NULL DEFAULT 'standard' CHECK (priority IN ('priority', 'standard')),
    viewed_at            TEXT,
    viewed_by            INTEGER REFERENCES users(id),
    acknowledged_at      TEXT,
    acknowledged_by      INTEGER REFERENCES users(id),
    UNIQUE (request_id, supplier_tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_shares_supplier ON request_shares(supplier_tenant_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_shares_contractor ON request_shares(contractor_tenant_id);
CREATE TABLE IF NOT EXISTS request_share_lines (
    share_id        TEXT NOT NULL REFERENCES request_shares(share_id),
    line_no         INTEGER NOT NULL,
    raw_description TEXT NOT NULL,
    quantity        REAL NOT NULL,
    uom             TEXT,
    note            TEXT,
    PRIMARY KEY (share_id, line_no)
);
-- First-party acquisition events. No IP address, no user agent, no
-- fingerprinting, no credentials, no email.
CREATE TABLE IF NOT EXISTS acquisition_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event                TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    visit_id             TEXT,
    user_id              INTEGER,
    contractor_tenant_id TEXT,
    channel              TEXT,
    referral_code        TEXT,
    supplier_tenant_id   TEXT,
    utm_source           TEXT,
    utm_medium           TEXT,
    utm_campaign         TEXT,
    utm_content          TEXT,
    page                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_event ON acquisition_events(event, created_at);
"""

PURPOSE = "corridoriq-contractor-pilot-platform"


class PilotNotConfigured(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def auth_schema_sql() -> str:
    """The portal's auth DDL, verbatim, for the tables in AUTH_TABLES."""
    text = Path(SCHEMA_PATH).read_text(encoding="utf-8")
    parts = []
    for table in AUTH_TABLES:
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\(.*?\n\);", text, re.S)
        if not m:
            raise RuntimeError(f"auth table {table} not found in schema.sql")
        parts.append(m.group(0))
    return "\n".join(parts)


def validate_root(root) -> Path:
    """A pilot root must be explicit and must not hold the production database."""
    if root is None or not str(root).strip():
        raise PilotNotConfigured("pilot root is not configured")
    path = Path(root).expanduser().resolve()
    prod = Path(settings.DB_PATH).expanduser().resolve()
    if path == prod or path == prod.parent or path in prod.parents:
        raise PilotNotConfigured("pilot root must not contain the production database")
    if path.exists() and not path.is_dir():
        raise PilotNotConfigured("pilot root must be a directory")
    return path


def configured_root() -> Path:
    return validate_root(os.environ.get(ENV_ROOT))


class Platform:
    """Handles for one pilot root."""

    def __init__(self, root):
        self.root = validate_root(root)
        self.path = self.root / PLATFORM_FILENAME
        self.contractors = ContractorRegistry(self.root / "contractors")
        self.suppliers = TenantRegistry(self.root / "suppliers")

    def connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if new:
            conn.execute("PRAGMA journal_mode = WAL")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & {"permits", "companies", "raw_record"}:
            conn.close()
            raise PilotNotConfigured("refusing to use an intelligence database as the pilot platform")
        conn.executescript(auth_schema_sql())
        conn.executescript(_PILOT_SCHEMA)
        share_cols = {row[1] for row in conn.execute("PRAGMA table_info(request_shares)")}
        if share_cols and "priority" not in share_cols:
            conn.execute("ALTER TABLE request_shares ADD COLUMN priority TEXT NOT NULL DEFAULT 'standard'")
            conn.commit()
        row = conn.execute("SELECT purpose FROM pilot_meta WHERE singleton=1").fetchone()
        if row is None:
            if tables - {"sqlite_sequence"} and "pilot_meta" not in tables:
                conn.close()
                raise PilotNotConfigured("existing database is not a pilot platform")
            conn.execute("INSERT INTO pilot_meta VALUES (1, ?, ?)", (PURPOSE, _now()))
            _seed_roles(conn)
            conn.commit()
        elif row["purpose"] != PURPOSE:
            conn.close()
            raise PilotNotConfigured("existing database is not a pilot platform")
        return conn


def _seed_roles(conn: sqlite3.Connection) -> None:
    now = _now()
    conn.execute("INSERT INTO organizations (name, slug, is_active, created_at, updated_at) "
                 "VALUES ('CorridorIQ pilot', 'pilot-platform', 1, ?, ?) ON CONFLICT(slug) DO NOTHING", (now, now))
    for key, desc in PILOT_PERMISSIONS.items():
        conn.execute("INSERT INTO permissions (permission_key, description, created_at) VALUES (?,?,?) "
                     "ON CONFLICT(permission_key) DO NOTHING", (key, desc, now))
    for name, (display, perms) in PILOT_ROLES.items():
        conn.execute("INSERT INTO roles (name, display_name, description, is_system_role, created_at, updated_at) "
                     "VALUES (?,?,?,1,?,?) ON CONFLICT(name) DO NOTHING", (name, display, display, now, now))
        rid = conn.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()["id"]
        for p in perms:
            pid = conn.execute("SELECT id FROM permissions WHERE permission_key=?", (p,)).fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO role_permissions VALUES (?,?)", (rid, pid))
