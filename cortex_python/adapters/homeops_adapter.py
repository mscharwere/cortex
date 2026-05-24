"""HomeOps adapter for CORTEX VacuumOps.

Calls:
  GET  /api/vacuum/units          — parse zone scores from data[].zones[]
  POST /api/vacuum/trigger        — dispatch a mission
  POST /api/decisions/vacuumops   — log a decision entry (fire-and-forget)

All calls use:
  Authorization: Bearer {settings.cortex_api_key}
  Base URL: settings.homeops_base_url

Spec: C:/Jarvis/Team/TARS/cortex_vacuumops_module_spec.md §11
"""

from __future__ import annotations

import httpx
import structlog

from cortex_python.config.settings import Settings
from cortex_python.modules.vacuumops.schemas import DecisionEntry

log = structlog.get_logger()

# HomeOps adapter timeout
_HOMEOPS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


class HomeOpsAdapter:
    """Async HTTP adapter for HomeOps API calls from CORTEX VacuumOps."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.homeops_base_url.rstrip("/")
        self._api_key = settings.cortex_api_key
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=_HOMEOPS_TIMEOUT,
            follow_redirects=True,
        )

    async def get_zone_scores(self) -> dict[str, float]:
        """Fetch current zone dirtiness scores from HomeOps.

        GET /api/vacuum/units
        Parses response: data[].zones[].{label, score}
        Returns a flat dict: {"Litter Box": 78.3, "Hallway": 41.0, ...}

        Returns {} on failure (caller should skip tick if scores unavailable).
        Raises on error so the caller (synth) can handle the skip-tick path
        per §8.5.
        """
        async with self._client() as client:
            r = await client.get("/api/vacuum/units")
            r.raise_for_status()
            data = r.json()

            scores: dict[str, float] = {}
            for unit in data.get("data", []):
                for zone in unit.get("zones", []):
                    label = zone.get("label")
                    score = zone.get("score")
                    if label is not None and score is not None:
                        scores[label] = float(score)
            return scores

    async def trigger_vacuum(
        self,
        robot: str,
        zones: list[dict],
        trigger_metadata: dict,
        dry_run: bool,
    ) -> dict:
        """POST /api/vacuum/trigger — dispatch a multi-zone mission.

        Request body matches spec §11.1:
          {
            "robot": "ethan",
            "zones": [...],
            "trigger_source": "cortex",
            "trigger_metadata": {...},
            "dry_run": false
          }

        Returns the HomeOps response dict.
        Raises httpx.HTTPStatusError on 4xx/5xx — caller handles per §10.2.
        """
        payload = {
            "robot": robot,
            "zones": zones,
            "trigger_source": "cortex",
            "trigger_metadata": trigger_metadata,
            "dry_run": dry_run,
        }
        async with self._client() as client:
            r = await client.post("/api/vacuum/trigger", json=payload)
            r.raise_for_status()
            return r.json()

    async def log_decision(self, entry: DecisionEntry) -> None:
        """POST /api/decisions/vacuumops — log a decision entry to HomeOps.

        Fire-and-forget. Logs errors but does NOT raise (spec: "log the error
        but do NOT abort the loop tick").
        """

        payload = {
            "tick_id": entry.tick_id,
            "timestamp": entry.timestamp,
            "zones": [
                {
                    "label": z.label,
                    "score": z.score,
                    "bundled": z.bundled,
                    "l1_confidence": z.l1_confidence,
                }
                for z in entry.zones
            ],
            "tier_reached": entry.tier_reached,
            "gate_failed": entry.gate_failed,
            "decision": entry.decision,
            "reason": entry.reason,
            "l1_confidence": entry.l1_confidence,
            "dry_run": entry.dry_run,
            "dispatched_at": entry.dispatched_at,
        }
        try:
            async with self._client() as client:
                r = await client.post("/api/decisions/vacuumops", json=payload)
                r.raise_for_status()
        except Exception as exc:
            log.error(
                "homeops_log_decision_failed",
                tick_id=entry.tick_id,
                decision=entry.decision,
                error=str(exc),
            )
            # Do NOT re-raise — fire-and-forget per spec
