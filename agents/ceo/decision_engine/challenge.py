"""Challenge the recommendation before it is shown to the founder."""

from __future__ import annotations


def challenge(proposal: str, *, trial_ready: bool, engineering: bool) -> list[str]:
    if proposal == "cosmetic_score":
        return [
            "Are we solving a demonstrated problem? No. Liking an account is not a systematic scoring error.",
            "What evidence would support a weight change? Repeated field outcomes showing the rank order is wrong for a class of accounts, not one favorite.",
            "Could sales-lane presentation carry a customer-specific book without editing account_priority_score? Yes, when lane evidence supports it.",
            "Are we changing a working system unnecessarily? Yes. Stop.",
            "What happens if we do nothing to the score? The evidence rank stays intact and the learning trial can still proceed.",
            "Does this help revenue or merely improve a demo? It changes a display order. It does not create revenue evidence.",
        ]
    if proposal == "intake_photos":
        return [
            "Are we solving a demonstrated problem? The founder reports a repeated list format. That claim is field evidence and is not yet in the outcome log.",
            "Could this be validated manually? Yes. A salesperson can receive photos on the existing phone while a prototype is scoped.",
            "What is the cheapest experiment? Keep logging photos by hand, and build only a narrow intake path if the pattern holds on the prepared accounts.",
            "What evidence contradicts a full marketplace? Supply-partner economics, routing rules, and quote workflow are still unknown.",
            "Is this reversible? A prototype that does not touch scores, lanes, CRM, or the customer dashboard is reversible. A fulfillment platform is not the same decision.",
            "Does this help revenue or architecture? It tests whether a real request can be captured. It does not prove a marketplace.",
        ]
    if proposal == "fulfillment_marketplace":
        return [
            "Are we solving a demonstrated problem? No logged contractor demand says they want CorridorIQ to route orders.",
            "Are we avoiding customer conversations by building? Yes, if this week is spent on a marketplace before the prepared calls.",
            "What evidence contradicts the build? Fulfillment demand, list formats, workflow preferences, and partner economics are unvalidated.",
            "Could this be validated manually? Yes. The prepared learning trial is the smaller, reversible experiment.",
            "What happens if we do nothing on fulfillment software? We lose a week of building and gain the calls we can already make.",
            "Is there a simpler path to learning? One conversation per prepared account, with a written outcome.",
        ]
    if trial_ready and not engineering:
        return [
            "Are we solving a demonstrated problem? Contact coverage on the frozen cohort is high enough to learn. Willingness to buy is not demonstrated yet.",
            "Are we avoiding customer conversations by building? The recommended action is the conversation.",
            "Could this be validated manually? Yes. That is the point of the trial.",
            "What is the cheapest experiment? Call the prepared accounts and log an outcome for each, including no-answer.",
            "What happens if we do nothing? The book stays internal and neither commercial thesis gets field evidence.",
            "Are we changing a working system? No score, lane, CRM, or dashboard change is required.",
        ]
    return [
        "Are we solving a demonstrated problem? The operating snapshot does not yet support a confident commercial action.",
        "What evidence is missing? See the UNKNOWN list. Do not fill those gaps with a new build.",
        "Could this be validated manually? Not until the missing system evidence is read from reports or the database.",
        "What happens if we do nothing? Better than inventing a metric or a project.",
        "Is there a simpler path? Retrieve the latest sales-trial artifact and re-run the brief.",
        "Does another engineering pass help? Not while the bottleneck is UNKNOWN.",
    ]
