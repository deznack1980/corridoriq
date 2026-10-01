"""Verified-contact view: one contact system, same pick as the trust gates."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from pipeline.auth.rbac import AuthzError
from pipeline.crm import service as crm
from pipeline.sales_lanes.serialize import BANNED_PUBLIC_FIELDS
from pipeline.trust import account_view as trust
from pipeline.trust import gates as G
from pipeline.trust.contacts import NOT_VERIFIED_TEXT, verified_contact_view
from pipeline.tests.test_workspace_ui import _company, _ctx, conn, env  # noqa: F401  (fixtures)

A, B = 1, 2


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chan(c, company_id, ctype, value, *, status="VERIFIED", source="prior_research", name=None, title=None,
          verified_at="2026-08-04", public=1):
    normalized = "".join(ch for ch in value if ch.isdigit()) if ctype == "business_phone" else value.lower()
    c.execute(
        """INSERT INTO company_contact_channels (company_id, contact_type, contact_value, normalized_value,
           contact_name, title, source_family, discovered_at, verified_at, verification_status, status,
           public_business_contact, model_version, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'active', ?, 'test', ?, ?)""",
        (company_id, ctype, value, normalized, name, title, source, _now(), verified_at, status, public, _now(), _now()),
    )
    c.commit()


@pytest.fixture()
def db(conn):  # full production schema, in memory
    _company(conn, "Alpha Gas LLC")
    _company(conn, "Alpha Gas L L C")  # similarly named, separate record
    return conn


def test_verified_phone_with_named_contact(db):
    _chan(db, A, "business_phone", "480-555-0101", name="Jerry Example", title="General Manager")
    _chan(db, A, "named_contact", "Jerry Example", name="Jerry Example", title="General Manager")
    v = verified_contact_view(db, A)
    assert v["status"] == "VERIFIED" and v["actionable"] and v["recommended_channel"] == "phone"
    assert v["phone"]["value"] == "480-555-0101"
    assert v["contact_name"] == "Jerry Example" and v["title"] == "General Manager"
    assert v["last_verified"] == "2026-08-04"
    assert G.contact_state({"contacts": _all(db, A)})["state"] == G.VERIFIED_PHONE


def test_verified_email_only(db):
    _chan(db, A, "business_email", "office@alphagas.example")
    v = verified_contact_view(db, A)
    assert v["status"] == "VERIFIED" and v["recommended_channel"] == "email"
    assert v["email"]["value"] == "office@alphagas.example" and v["phone"] is None


def test_business_line_without_a_person(db):
    _chan(db, A, "business_phone", "480-555-0102", source="official_website", verified_at=None)
    v = verified_contact_view(db, A)
    assert v["actionable"] and v["contact_name"] is None
    assert v["phone"]["source"] == "Company website"
    assert v["last_verified"] is None  # not invented


def test_no_verified_contact_says_so(db):
    _chan(db, A, "business_phone", "480-555-0199", status="CANDIDATE", source="roc")
    _chan(db, A, "qualifying_party", "Some Person", status="CANDIDATE", source="roc", name="Some Person")
    v = verified_contact_view(db, A)
    assert v["status"] == "NOT_VERIFIED" and v["status_label"] == NOT_VERIFIED_TEXT
    assert not v["actionable"] and v["phone"] is None and v["contact_name"] is None
    assert "480-555-0199" not in json.dumps(v)


def test_inherited_contact_is_labelled_and_not_actionable(db):
    _chan(db, A, "business_phone", "480-555-0103", source="canonical_peer", name="Peer Person")
    v = verified_contact_view(db, A)
    assert v["status"] == "INHERITED_UNVERIFIED" and not v["actionable"]
    assert v["phone"] is None and v["recommended_channel"] is None
    assert v["inherited"][0]["value"] == "480-555-0103"
    assert "Not verified for this company" in v["inherited"][0]["note"]


def test_duplicate_company_contact_is_never_borrowed(db):
    _chan(db, B, "business_phone", "480-555-0104", name="Other Record")
    v = verified_contact_view(db, A)
    assert v["status"] == "NOT_VERIFIED"
    assert "480-555-0104" not in json.dumps(v)
    assert verified_contact_view(db, B)["phone"]["value"] == "480-555-0104"


def test_non_public_channel_is_hidden(db):
    _chan(db, A, "business_phone", "480-555-0105", public=0)
    assert verified_contact_view(db, A)["status"] == "NOT_VERIFIED"


def _all(c, cid):
    return [dict(r) for r in c.execute("SELECT * FROM company_contact_channels WHERE company_id=?", (cid,))]


def test_company_detail_exposes_verified_contact_with_permissions(env):
    c = env["conn"]
    _chan(c, env["co1"], "business_phone", "602-555-0110", name="Pat Buyer", title="Purchasing")
    _chan(c, env["co1"], "business_address", "1 Main St, Phoenix, AZ 85001")
    _chan(c, env["co1"], "qualifying_party", "QP Person", status="CANDIDATE", source="roc", name="QP Person")
    detail = crm.get_company_detail(c, env["rep"], env["co1"])
    vc = detail["verified_contact"]
    assert vc["phone"]["value"] == "602-555-0110" and vc["address"]["value"].startswith("1 Main St")
    blob = json.dumps(vc)
    for banned in (*BANNED_PUBLIC_FIELDS, "normalized_value", "decision_maker_class", "confidence"):
        assert f'"{banned}"' not in blob, banned
    assert "QP Person" not in blob and '"roc"' not in blob
    with pytest.raises(AuthzError):  # rep cannot read an unassigned company's contacts
        crm.get_company_detail(c, env["rep"], env["co2"])


def test_call_and_email_cards_match_displayed_contact(tmp_path, monkeypatch):
    from agents.ceo.evals import morning_fixture as F

    trust.clear_cache()
    c = sqlite3.connect(F.build(tmp_path / "m.db"))
    c.row_factory = sqlite3.Row
    items = trust.todays_accounts(c, as_of=F.AS_OF)["items"]
    assert items
    for card in items:
        cd = card["contact_details"]
        assert cd["actionable"] and cd["status"] == "VERIFIED"
        if card["action"] == "Call":
            assert cd["phone"] and cd["recommended_channel"] == "phone"
        if card["action"] == "Email":
            assert cd["email"] and cd["recommended_channel"] == "email"
    # If the displayed contact ever disagreed with the gate, the card is dropped.
    import pipeline.trust.contacts as contacts_mod

    monkeypatch.setattr(
        contacts_mod, "verified_contact_view",
        lambda conn, cid: {"actionable": False, "phone": None, "email": None, "status": "NOT_VERIFIED"},
    )
    trust.clear_cache()
    assert trust.todays_accounts(c, as_of=F.AS_OF)["items"] == []
    c.close()
