"""Offline Customer-Book Match (supplier tenants only).

A supplier's customer book is imported into that supplier's private tenant
store, matched read-only against shared CorridorIQ company intelligence, and
classified by purchase recency × construction activity. Nothing here writes to
the shared intelligence database or changes CorridorIQ scoring.
"""
