"""Identity Evidence Shadow — an isolated, rebuildable experiment in resolving
companies from independent identity evidence instead of permit sightings.

Reads production READ-ONLY; writes only to its own shadow database. Never
changes the production company universe, scoring, trust, or any commercial
surface. Tenant (supplier customer-book) data is never read.
"""
