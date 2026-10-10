"""Sanitized public exploration payload.

Synthetic demonstration data only. No private contractor records, customer
intelligence, contacts, supplier pricing, private RFQs, scoring weights,
administrative information, or paid products.
"""

from __future__ import annotations

PREVIEW = {
    "kind": "public_preview",
    "disclaimer": (
        "Demonstration data. These examples are synthetic and do not represent "
        "live customer accounts, private contacts, supplier pricing, or paid intelligence."
    ),
    "platform": {
        "name": "CorridorIQ",
        "summary": (
            "Phoenix-area construction intelligence and contractor procurement. "
            "Supply houses see where plumbing and wet-side demand is forming from "
            "public permit records. Contractors source materials without changing "
            "how they already work."
        ),
    },
    "features": [
        {
            "audience": "everyone",
            "title": "Public permit signals",
            "detail": "Projects start from published municipal permit records, not from a scraped contact list.",
        },
        {
            "audience": "contractors",
            "title": "Material lists",
            "detail": "Build or upload a list, then send it only to the supplier you choose.",
        },
        {
            "audience": "contractors",
            "title": "Supplier responses",
            "detail": "See quoted price, availability, and lead time from the suppliers you invited — after your account is verified.",
        },
        {
            "audience": "suppliers",
            "title": "Trade-checked accounts",
            "detail": "A short list of accounts that passed today's checks, with permit context for the first conversation.",
        },
        {
            "audience": "suppliers",
            "title": "Opportunity map",
            "detail": "Phoenix-metro activity placed only where the source published a location.",
        },
    ],
    "opportunities": [
        {
            "id": "demo-opp-1",
            "title": "Medical office tenant improvement",
            "city": "Phoenix, AZ",
            "trade": "Plumbing",
            "stage": "Permit issued",
            "signal": "New commercial plumbing permit on a tenant-improvement job.",
        },
        {
            "id": "demo-opp-2",
            "title": "Multifamily water-heater replacement",
            "city": "Mesa, AZ",
            "trade": "Plumbing",
            "stage": "In review",
            "signal": "Residential multifamily mechanical/plumbing activity.",
        },
        {
            "id": "demo-opp-3",
            "title": "Retail shell plumbing rough-in",
            "city": "Scottsdale, AZ",
            "trade": "Plumbing",
            "stage": "Permit issued",
            "signal": "Commercial shell plumbing work associated with a new suite.",
        },
    ],
    "contractor_workflow": [
        {"step": 1, "title": "Create a free contractor account",
         "detail": "Name, business name, email, and password. You land on a welcome dashboard immediately."},
        {"step": 2, "title": "Explore the workspace",
         "detail": "Learn the material-list and request flow with demonstration data."},
        {"step": 3, "title": "Verify your email",
         "detail": "Verification unlocks live requests, private quotations, and protected intelligence."},
        {"step": 4, "title": "Send a request to your supplier",
         "detail": "A request goes only to the suppliers you pick. Nothing is broadcast."},
    ],
    "supplier_benefits": [
        "See where plumbing and wet-side demand is forming from public records.",
        "Work a short, checked account list instead of an inflated lead dump.",
        "Receive material requests only when a contractor chooses your branch.",
        "Keep your own pricing and availability in your system — CorridorIQ does not invent them.",
    ],
    "registration": {
        "contractor": "/register-contractor.html",
        "supplier": "/register-supplier.html",
        "signin": "/login.html",
        "explore": "/explore.html",
    },
}


def public_preview() -> dict:
    return dict(PREVIEW)
