"""Contractor account foundation + single-supplier pilot.

Everything here lives under an explicitly configured pilot root
(``CORRIDORIQ_PILOT_ROOT``), never in the shared CorridorIQ intelligence
database. When the root is not configured, every pilot endpoint answers 503.

    <pilot_root>/platform.db            pilot accounts (same auth tables and
                                        code as the portal), contractor
                                        profiles, supplier connections,
                                        referral codes, sent-request exchange,
                                        acquisition events
    <pilot_root>/contractors/...        contractor tenants (private requests)
    <pilot_root>/suppliers/...          supplier tenants (existing registry)
"""
