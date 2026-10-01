"""Controlled official-website contacts for Top-25 gaps.

Values below were taken from the company's own public website (or, for
ABS, the EPCOR certified-tester list). Inferred emails are labeled
INFERRED_UNVERIFIED and must never be treated as verified.
"""

from __future__ import annotations

import sqlite3

from pipeline.contactability.normalize import CANDIDATE, INFERRED_UNVERIFIED, VERIFIED
from pipeline.contactability.plumbing_core_official import OFFICIAL_PLUMBING_CORE
from pipeline.contactability.store import canonical_id_for, mark_primaries, upsert_channel
from pipeline.entity.names import compact_company_name

# compact_company_name(display_name) -> channels
OFFICIAL = {
    "RP GAS PIPING": [
        {
            "contact_type": "website",
            "contact_value": "https://rpgaspiping.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://rpgaspiping.com/contact-us/",
            "confidence": 95.0,
            "notes": "Official site lists ROC# 261487 and 344477.",
        },
        {
            "contact_type": "business_phone",
            "contact_value": "602-759-8340",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://rpgaspiping.com/contact-us/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_email",
            "contact_value": "info@rpgaspiping.com",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://rpgaspiping.com/about-rp-gas/employment/",
            "confidence": 90.0,
            "notes": "Published on official employment page as resume inbox.",
        },
        {
            "contact_type": "business_address",
            "contact_value": "731 N. 19th Avenue, Phoenix, AZ 85009",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://rpgaspiping.com/contact-us/",
            "confidence": 95.0,
        },
        {
            "contact_type": "contact_form",
            "contact_value": "https://rpgaspiping.com/contact-us/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://rpgaspiping.com/contact-us/",
            "confidence": 90.0,
        },
    ],
    "MILLENNIUM GAS SERVICES": [
        {
            "contact_type": "website",
            "contact_value": "https://www.millenniumgas.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.millenniumgas.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-633-2176",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.millenniumgas.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_email",
            "contact_value": "info@millenniumgas.com",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.millenniumgas.com/contact-us",
            "confidence": 92.0,
        },
        {
            "contact_type": "estimator",
            "contact_value": "bids@millenniumgas.com",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.millenniumgas.com/contact-us",
            "title": "Request Bid",
            "confidence": 92.0,
            "notes": "Official 'Request Bid for your Project' inbox.",
        },
        {
            "contact_type": "business_email",
            "contact_value": "bids@millenniumgas.com",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.millenniumgas.com/contact-us",
            "title": "Request Bid",
            "confidence": 92.0,
        },
    ],
    "ABC WATER WORKS": [
        {
            "contact_type": "website",
            "contact_value": "https://www.abcwaterworksinc.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.abcwaterworksinc.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-753-5849",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.abcwaterworksinc.com/",
            "confidence": 95.0,
            "notes": "Office line. Emergency 602-517-9852 also published.",
        },
        {
            "contact_type": "business_email",
            "contact_value": "contact@abcwaterworksinc.com",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.abcwaterworksinc.com/",
            "confidence": 92.0,
        },
        {
            "contact_type": "business_address",
            "contact_value": "P.O. Box 14093, Mesa, Arizona 85216",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.abcwaterworksinc.com/",
            "confidence": 90.0,
        },
    ],
    "METERING SERVICES": [
        {
            "contact_type": "website",
            "contact_value": "https://www.msiaz.net/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.msiaz.net/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-894-0200",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.msiaz.net/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_email",
            "contact_value": "backflow@msiaz.net",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.msiaz.net/",
            "confidence": 90.0,
        },
        {
            "contact_type": "named_contact",
            "contact_value": "Keri Frampton",
            "contact_name": "Keri Frampton",
            "title": "President",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.msiaz.net/meet-our-team/",
            "confidence": 90.0,
        },
        {
            "contact_type": "business_address",
            "contact_value": "515 S 48th St, Tempe, AZ 85281",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.msiaz.net/",
            "confidence": 95.0,
        },
    ],
    "MIDWEST CONTRACTING": [
        {
            "contact_type": "website",
            "contact_value": "https://mwcontractinginc.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://mwcontractinginc.com/contact-us",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-710-0448",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://mwcontractinginc.com/contact-us",
            "confidence": 92.0,
        },
        {
            "contact_type": "contact_form",
            "contact_value": "https://mwcontractinginc.com/contact-us",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://mwcontractinginc.com/contact-us",
            "confidence": 90.0,
        },
        {
            "contact_type": "business_address",
            "contact_value": "PO Box 1768, Maricopa AZ 85139",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://mwcontractinginc.com/contact-us",
            "confidence": 90.0,
        },
    ],
    "ARROWMARK": [
        {
            "contact_type": "website",
            "contact_value": "https://teamarrowmark.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://teamarrowmark.com/contact/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-892-8025",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://teamarrowmark.com/contact/",
            "confidence": 95.0,
        },
        {
            "contact_type": "estimator",
            "contact_value": "bids@teamarrowmark.com",
            "title": "Estimates",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://teamarrowmark.com/contact/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_email",
            "contact_value": "bids@teamarrowmark.com",
            "title": "Estimates",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://teamarrowmark.com/contact/",
            "confidence": 95.0,
        },
        {
            "contact_type": "office",
            "contact_value": "info@teamarrowmark.com",
            "title": "General inquiries",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://teamarrowmark.com/contact/",
            "confidence": 90.0,
        },
        {
            "contact_type": "business_address",
            "contact_value": "1620 S Stapley Dr. Suite 118, Mesa, Arizona 85204",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://teamarrowmark.com/contact/",
            "confidence": 95.0,
        },
    ],
    "FERRELL GAS": [
        {
            "contact_type": "website",
            "contact_value": "https://www.ferrellgas.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.ferrellgas.com/contact-us/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "888-337-7355",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.ferrellgas.com/contact-us/",
            "confidence": 90.0,
            "notes": "National customer service. Phoenix office 602-278-8511 on official location page.",
        },
        {
            "contact_type": "office",
            "contact_value": "602-278-8511",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.ferrellgas.com/propane-locations/az/phoenix/nw-grand-ave/201090/",
            "confidence": 92.0,
            "notes": "Phoenix NW Grand Ave official location.",
        },
        {
            "contact_type": "contact_form",
            "contact_value": "https://www.ferrellgas.com/contact-us/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.ferrellgas.com/contact-us/",
            "confidence": 90.0,
        },
    ],
    "GAS PIPING": [
        {
            "contact_type": "website",
            "contact_value": "https://www.gaspipinginc.com/",
            "verification_status": CANDIDATE,
            "source_family": "public_directory",
            "source_reference": "https://az.biznet-us.com/firms/10303113/",
            "confidence": 55.0,
            "notes": "Official host listed publicly; live fetch returned HTTP 500 so not VERIFIED.",
        },
        {
            "contact_type": "business_phone",
            "contact_value": "602-971-4000",
            "verification_status": CANDIDATE,
            "source_family": "public_directory",
            "source_reference": "https://www.bbb.org/us/az/phoenix/profile/propane-supplies/gas-piping-inc-1126-21001928",
            "confidence": 60.0,
            "notes": "BBB business listing. Not confirmed on a live official page this pass.",
        },
        {
            "contact_type": "business_email",
            "contact_value": "info@gaspipinginc.com",
            "verification_status": INFERRED_UNVERIFIED,
            "source_family": "inferred_pattern",
            "source_reference": "https://az.biznet-us.com/firms/10303113/",
            "confidence": 30.0,
            "notes": "INFERRED_UNVERIFIED. Directory-listed; official site did not confirm.",
        },
    ],
    "UMBRELLA PLUMBING": [
        {
            "contact_type": "website",
            "contact_value": "https://www.tryumbrellaplumbing.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.tryumbrellaplumbing.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-869-6952",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.tryumbrellaplumbing.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_email",
            "contact_value": "office@tryumbrellaplumbing.com",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.tryumbrellaplumbing.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_address",
            "contact_value": "3434 N. San Marcos Place, Chandler, AZ 85225",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.tryumbrellaplumbing.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "contact_form",
            "contact_value": "https://www.tryumbrellaplumbing.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.tryumbrellaplumbing.com/",
            "confidence": 90.0,
            "notes": "Contact Us is published on the official homepage footer.",
        },
    ],
    "HILLER COMPANIES": [
        {
            "contact_type": "website",
            "contact_value": "https://hillerfire.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://hillerfire.com/locations/phoenix-az/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "888-222-0532",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://hillerfire.com/locations/phoenix-az/",
            "confidence": 95.0,
            "notes": "Phoenix location line published on official location page.",
        },
        {
            "contact_type": "office",
            "contact_value": "251-661-1275",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://hillerfire.com/",
            "confidence": 90.0,
            "notes": "National quote line on official homepage. Not the Phoenix branch.",
        },
        {
            "contact_type": "contact_form",
            "contact_value": "https://hillerfire.com/locations/phoenix-az/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://hillerfire.com/locations/phoenix-az/",
            "confidence": 90.0,
        },
    ],
    "ARIZONA PROPANE": [
        {
            "contact_type": "website",
            "contact_value": "https://www.arizonapropane.com/",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.arizonapropane.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_phone",
            "contact_value": "480-990-2245",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.arizonapropane.com/",
            "confidence": 95.0,
        },
        {
            "contact_type": "business_address",
            "contact_value": "17251 E Shea Blvd, Fountain Hills, AZ 85268",
            "verification_status": VERIFIED,
            "source_family": "official_website",
            "source_reference": "https://www.arizonapropane.com/",
            "confidence": 95.0,
            "notes": "Official corporate office ZIP matches ROC Dawson Enterprises Inc 85268.",
        },
    ],
    "ABS ARIZONA BACKFLOW SPECIALIST": [
        {
            "contact_type": "business_phone",
            "contact_value": "602-548-1101",
            "verification_status": CANDIDATE,
            "source_family": "public_utility_list",
            "source_reference": "https://www.epcor.com/content/dam/epcor/documents/supporting-documents/arizona_list-certified-backflow-testers.pdf",
            "confidence": 70.0,
            "notes": "EPCOR certified tester list publishes this number for Arizona Backflow Specialist. No official company website found.",
        },
    ],
}


def official_catalog() -> dict:
    merged = dict(OFFICIAL)
    merged.update(OFFICIAL_PLUMBING_CORE)
    return merged


def apply_official_public_contacts(conn: sqlite3.Connection, company_ids: list[int] | None = None) -> dict:
    catalog = official_catalog()
    where = ""
    params: list = []
    if company_ids:
        where = f" AND id IN ({','.join('?' * len(company_ids))})"
        params = list(company_ids)
    rows = conn.execute(
        f"SELECT id, display_name FROM companies WHERE lifecycle_state='active'{where}",
        params,
    ).fetchall()
    written = 0
    matched = 0
    for row in rows:
        key = compact_company_name(row["display_name"])
        channels = catalog.get(key)
        if not channels:
            continue
        matched += 1
        can = canonical_id_for(conn, int(row["id"]))
        for ch in channels:
            payload = dict(ch)
            if upsert_channel(
                conn,
                company_id=int(row["id"]),
                canonical_company_id=can,
                **payload,
            ):
                written += 1
        mark_primaries(conn, int(row["id"]))
    conn.commit()
    return {"compact_keys": len(catalog), "companies_matched": matched, "channels_written": written}
