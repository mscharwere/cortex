# Shared Redis — Family-App Suite Infrastructure

Single Redis 7.4 instance shared across all Carlos family apps on the Synology NAS. Mirrors the pattern of the shared MariaDB at `192.168.30.4:3306`.

## Purpose

CORTEX is the first consumer. Future apps (ClimateOps, FamilyOps caching layer, etc.) claim logical databases from the table below — no new Redis instance needed per app.

## Per-App Logical DB Assignments

| DB | App | Notes |
|----|-----|-------|
| 0  | Ad-hoc / manual | Reserved; do not claim in app code |
| 1  | Available | |
| 2  | **CORTEX** | Hot path: event streams, snapshot cache, dedup keys |
| 3–15 | Available | Claim one per new app; document here |

When a new app starts using Redis, add a row to this table in the same PR that adds the app's Redis config.

## Deploy (one-time, before any consuming app)

```bash
# 1. Copy compose + env to NAS
#    (REDIS_PASSWORD must match /volume1/docker/cortex/.env)
cp infra/redis/docker-compose.yml /volume1/docker/redis/docker-compose.yml
cp infra/redis/.env               /volume1/docker/redis/.env

# 2. Bring up
cd /volume1/docker/redis && docker compose up -d

# 3. Verify
docker compose exec redis redis-cli -a $REDIS_PASSWORD ping
# → PONG
```

## Notes

- Watchtower auto-updates Redis on new `redis:7.4-alpine` image pushes (label enabled).
- AOF persistence is on; RDB snapshots are off (`--save ""`).
- Data volume: `redis-shared-data` (named Docker volume on NAS).
- Host port 6379 is LAN-exposed so NAS containers reach it at `192.168.30.4:6379`.
