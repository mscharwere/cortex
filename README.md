# CORTEX

**Cross-App Intelligence Fabric** — the AI fabric above Home Assistant and the
family-app suite (FamilyOps, Nexus, BookQuest, HomeOps, ProjectOps, FamilyAuth).
CORTEX-Python runs as a single FastAPI service on the Synology NAS; the
inference plane (LiteLLM + Ollama + Lemonade) lives on the MS-S1 MAX box.

**Authoritative spec:** `C:/Jarvis/Team/TARS/cortex_architecture.md` (v3.1).
This README is a getting-started crib, not the architecture document.

## Infrastructure model

CORTEX follows the established family-app pattern: **no bundled databases**.

| Service | Where it runs | Shared with |
|---------|--------------|-------------|
| MariaDB | Synology NAS `192.168.30.4:3306` | FamilyOps, ProjectOps, BookQuest, etc. |
| Redis   | Synology NAS `192.168.30.4:6379` | Shared infra (CORTEX = DB 2) |
| cortex-python | Synology NAS container | — |

See `infra/redis/` for the shared Redis compose and per-app DB map.

## Production deploy (Synology NAS)

### One-time setup (do this before the first `docker compose up`)

```bash
# Step 1 — Bootstrap CORTEX database + user on shared MariaDB
#   Fill in scripts/bootstrap.sql (copy from bootstrap.sql.example, set real password).
mysql -h 192.168.30.4 -u root -p < scripts/bootstrap.sql

# Step 2 — Bring up shared Redis (if not already running)
#   Copy infra/redis/docker-compose.yml + infra/redis/.env to /volume1/docker/redis/
#   REDIS_PASSWORD in both .env files must match.
cd /volume1/docker/redis && docker compose up -d
```

### Per-deploy steps

```bash
# Step 3 — Copy compose + env to NAS
#   scp docker/docker-compose.yml .env  nas:/volume1/docker/cortex/

# Step 4 — Start cortex-python
cd /volume1/docker/cortex && docker compose up -d

# Step 5 — Run migrations
docker compose exec cortex-python alembic upgrade head
```

Production deploy is fully automatic after that: GitHub Actions builds + pushes
`ghcr.io/mscharwere/cortex-python:latest`; the NAS auto-pulls via Watchtower.
See `feedback_nexus_auto_deploy.md` for the canonical family-app pattern.

## Local dev

```bash
# 1. Clone + enter
git clone https://github.com/mscharwere/cortex.git && cd cortex

# 2. Copy .env.example → .env, fill in real values
cp .env.example .env

# 3. Run tests (SQLite used in CI; no MariaDB needed locally)
cd cortex_python && uv run pytest tests/unit/

# 4. Smoke the API (requires NAS VPN or local MariaDB/Redis)
uvicorn cortex_python.api.main:app --reload
curl http://localhost:8000/health   # → {"status":"ok"}
```

## Repo layout

See Appendix A in the architecture spec; the top-level matches it 1:1
(`cortex_python/` (incl. `migrations/`), `docker/`, `infra/`, `schemas/`,
`tests/`, `ops/`, `.github/workflows/`, `scripts/`).

## Phase status

- Phase 0 Item 1 — repo scaffold + GHA build pipeline ✅ (PR #1)
- Phase 0 Item 2 — compose, migrations, alembic init ✅ (PR #2)
- Phase 0 Item 3 — corrective: shared infra (remove bundled mariadb/redis) ✅ (PR #3)
- Items 4–7 (HA WS adapter, LiteLLM wiring, HA-side sensors, schema/eval gates) follow per the §F Phase Table.
