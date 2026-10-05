"""Classify the Sonoran / John signal. Excitement is not revenue."""

from __future__ import annotations

FORBIDDEN = (
    "validated_willingness_to_pay",
    "signed_customer",
    "contracted_revenue",
    "active_paid_pilot",
    "confirmed_product_market_fit",
)


def sonoran_john(state: dict) -> dict:
    org = next(
        item
        for item in state["stakeholders"]["organizations"]
        if item["organization"] == "Sonoran Plumbing Supply"
    )
    person = next(item for item in org["stakeholders"] if item["name"] == "John")
    interaction = person["interactions"][0]
    if interaction.get("signal") != "POSITIVE_CUSTOMER_DEVELOPMENT_SIGNAL":
        raise ValueError("Sonoran signal was upgraded without a new evidence record")
    for label in FORBIDDEN:
        if label not in interaction.get("must_not_classify_as", []):
            raise ValueError(f"missing prohibition {label}")
    if org.get("paid_customer") or org.get("active_paid_pilot"):
        raise ValueError("Sonoran is not a paid or active pilot on current evidence")
    if interaction.get("willingness_to_pay") != "UNKNOWN":
        raise ValueError("willingness to pay is not known")
    price = state["pricing"]["hypotheses"]["preferred_sonoran_or_founding_partner_usd_per_month"]
    return {
        "organization": org["organization"],
        "stakeholder": person["name"],
        "role": person.get("role") or "UNKNOWN",
        "signal": interaction["signal"],
        "FACT": "John expressed that he is very excited and looking forward to seeing and using the product.",
        "INFERENCE": "Sonoran appears engaged. Engagement is not a purchase.",
        "HYPOTHESIS": (
            f"A ${price}/month founding-partner offer may have acceptable sales friction. "
            "That figure is an internal hypothesis, not a quote that was offered or accepted."
        ),
        "RECOMMENDATION": interaction["next_action"],
        "willingness_to_pay": "UNKNOWN",
        "pilot_status": interaction["pilot_status"],
        "conversion_status": interaction["conversion_status"],
        "follow_up_date": interaction.get("follow_up_date"),
        "must_not_classify_as": list(interaction["must_not_classify_as"]),
    }
