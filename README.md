# CORTEX

**Cross-App Intelligence Fabric** — the AI fabric above Home Assistant and the
family-app suite (FamilyOps, Nexus, BookQuest, HomeOps, ProjectOps, FamilyAuth).
CORTEX-Python runs as a single FastAPI service on the Synology NAS; the
inference plane (LiteLLM + Ollama + Lemonade) lives on the MS-S1 MAX box.

**Authoritative spec:** `C:/Jarvis/Team/TARS/cortex_architecture.md` (v3.1).
This README is a getting-started crib, not the architecture document.

## Getting started

```bash
# 1. Clone + enter
git clone https://github.com/mscharwere/cortex.git && cd cortex

# 2. Build + run via the Synology compose pattern (same shape as familyOps)
cp .env.example .env          # populate real secrets (Item 2 ships a complete .env.example)
docker compose -f docker/docker-compose.yml up --build

# 3. Smoke
curl http://localhost:8000/health   # → {"status":"ok"}
curl http://localhost:8000/version  # → {"version":"0.0.1"}
```

Production deploy is fully automatic: GitHub Actions builds + pushes
`ghcr.io/mscharwere/cortex-python:latest`; the NAS auto-pulls via Watchtower.
See `feedback_nexus_auto_deploy.md` for the canonical family-app pattern.

## Repo layout

See Appendix A in the architecture spec; the top-level matches it 1:1
(`cortex_python/` (incl. `migrations/`), `docker/`, `schemas/`, `tests/`, `ops/`,
`.github/workflows/`).

## Phase status

Phase 0 Item 1 — repo scaffold + GHA build pipeline — landing now.
Items 2–7 (MariaDB schema, HA WS adapter, LiteLLM wiring, HA-side sensors,
schema/eval gates, broader CI) follow per the §F Phase Table.
