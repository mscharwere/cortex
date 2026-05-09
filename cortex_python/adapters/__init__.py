"""Adapter modules — one per app (HA, FamilyOps, Nexus, BookQuest, HomeOps).

Each adapter conforms to the contract in ``cortex_architecture.md`` §3.1:
``ingest_events``, ``emit_action``, ``route_persona``.
Phase 0 Item 1 is scaffold-only; adapter implementations land in later items.
"""
