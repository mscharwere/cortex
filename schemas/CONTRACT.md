# CORTEX Module-Author Contract

**Version:** 1.0 — Phase 0 closeout
**Spec ref:** `cortex_architecture.md` v3.1 §6, §3.3, §F
**Owner:** TARS (architecture), KAREN (implementation), SHODAN (evals)

---

## What is a CORTEX Module?

A module is a **Python skill bundle** — a package under `cortex_python/modules/<name>/`.
It contains Pydantic schemas, R0/R1 rule logic, optional L1/L2 LLM skill code, and prompt
files. There is no second runtime; all code runs inside the CORTEX-Python container on NAS.

```
cortex_python/modules/vacuumops/
├── __init__.py
├── rules.py        # R0 hard gates + R1 scored rules
├── skill.py        # L1 Python skill (if needed)
├── schema.py       # Pydantic input/output types
└── prompts/        # Jinja2 or plain-text prompt files
```

---

## Decision Tiers

| Tier | When to use | Latency | Cost |
|------|-------------|---------|------|
| **R0** | Safety gates, opt-outs, schedule guards, ACL enforcement. Always runs first; hard fail = no action taken. | <5 ms | free |
| **R1** | Scored dispatch — most "should I act now?" calls (e.g. VacuumOps dirty-score threshold). Runs on NAS; graceful-degrades when MS-S1 MAX is down. | <20 ms | free |
| **L1** | Cross-domain trade-offs, persona reply composition, novel situations. Python → HTTPX → LiteLLM → `gemma4:31b` (Ollama, GPU). | 1–4 s | GPU |
| **L2** | Vision (camera frames). Python → HTTPX → LiteLLM → `gemma4:e4b` or `gemma4:31b`. | 3–8 s | GPU |
| **L3** | Cloud escalation (Jarvis). Requires explicit `allow_l3: true` in module config + Carlos approval. Never a hot path. | variable | API $ |
| **OVERFLOW** | Confidence < 0.65, schema parse fail, missing tool, or 90 s timeout. Return `Decision(tier=Tier.OVERFLOW, ...)`. Jarvis picks it up from `ait_overflow_queue`. | — | — |

**Always try the cheapest tier first.** L1/L2 hops require justification in code review (ARIIA gate). Never use L3 without Carlos's explicit per-module approval.

---

## Required Module Interface

Every module must expose a `decide` coroutine:

```python
from cortex_python.modules.base import ModuleContext, Decision, Tier

async def decide(context: ModuleContext) -> Decision:
    """Entry point called by the Decision Engine for this module.

    Args:
        context: Snapshot of cross-domain state at decision time.

    Returns:
        Decision — the action to take (or OVERFLOW / SUPPRESS).
    """
    ...
```

### `ModuleContext` (read-only snapshot)

```python
@dataclass(frozen=True)
class ModuleContext:
    ts: datetime                      # snapshot timestamp (UTC)
    presence: dict[str, str]          # {"carlos": "home", "daniel": "school", ...}
    module_config: dict[str, Any]     # hot-reloadable thresholds from module_config table
    raw: dict[str, Any]               # full context snapshot (aspect builders output)
```

### `Decision` contract

```python
@dataclass
class Decision:
    tier: Tier                        # R0 | R1 | L1 | L2 | L3 | OVERFLOW | SUPPRESS
    action: str | None                # adapter action key, e.g. "dispatch_vacuum"
    payload: dict[str, Any]           # passed through to Action Layer → adapter
    confidence: float                 # 0.0–1.0; <0.65 → overflow
    reason: str                       # human-readable; logged to decision_log
    model: str | None = None          # raw LiteLLM model string if L1/L2; else None
```

---

## Logging to `decision_log`

Every `decide()` call is logged automatically by the Decision Engine — modules do not write
to the DB directly. To annotate the log entry with structured reasoning, use the
`DecisionLogger` adapter:

```python
from cortex_python.adapters.decision_logger import DecisionLogger

# DecisionLogger interface (scaffold — implementation ships Phase 1)
class DecisionLogger:
    async def annotate(
        self,
        trigger_id: str,
        note: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append a structured note to the in-flight decision_log row.

        Call from inside your skill before returning Decision.
        The row is committed by the engine after decide() returns.
        """
        ...
```

The `decision_log` row schema (§3.5):

| Column | Type | Notes |
|--------|------|-------|
| `trigger_id` | UUID | Injected by engine; pass through for `annotate()` |
| `module` | str | Module name, e.g. `"vacuumops"` |
| `tier` | enum | R0/R1/L1/L2/L3/OVERFLOW/SUPPRESS |
| `model` | str\|NULL | Raw LiteLLM model string (`"gemma4:31b"`) or NULL |
| `action` | str\|NULL | Action key dispatched |
| `confidence` | float | |
| `reason` | str | |
| `latency_ms` | int | Engine-measured wall time |
| `ts` | datetime | UTC |

---

## Triggering Overflow

Return a `Decision` with `tier=Tier.OVERFLOW` from your `decide()` function. The engine
enqueues to `ait_overflow_queue` and the item surfaces in Jarvis's next briefing.

```python
from cortex_python.modules.base import Decision, Tier

return Decision(
    tier=Tier.OVERFLOW,
    action=None,
    payload={"context_snapshot": context.raw},
    confidence=0.0,
    reason="Schema parse failed — LLM returned malformed JSON after 3 retries",
)
```

Overflow triggers: confidence < 0.65 · schema parse fail · missing tool · 90 s timeout.

---

## LLM Calls — `litellm_client`

Use the shared HTTPX client from `cortex_python/adapters/litellm_client.py`. Do NOT create
your own `httpx.AsyncClient` or call Ollama/Lemonade directly.

```python
from cortex_python.adapters.litellm_client import get_litellm_client
from cortex_python.config.settings import get_settings

async def _call_l1(rendered_prompt: str) -> MySchema:
    settings = get_settings()
    async with get_litellm_client(settings) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemma4:31b",           # raw model name — LiteLLM routes to Ollama GPU
                "messages": [{"role": "user", "content": rendered_prompt}],
                "response_format": {"type": "json_object"},
                "fallbacks": ["gemma3:27b"],      # LiteLLM handles fallback transparently
                "metadata": {"module": "mymodule", "trigger_id": trigger_id},
            },
        )
    resp.raise_for_status()
    return MySchema.model_validate_json(
        resp.json()["choices"][0]["message"]["content"]
    )
```

**Model strings (raw names — §3.3 v3.1):**

| Use | Model string |
|-----|-------------|
| L1 default | `"gemma4:31b"` → Ollama GPU |
| L1 fallback | `"gemma3:27b"` → Ollama GPU |
| L3 light (NPU) | `"qwen3-8b-FLM"` → Lemonade NPU |
| L2 vision triage | `"gemma4:e4b"` → Ollama GPU |

---

## Persona ACL Rules

Persona skill bundles (under `cortex_python/personas/`) must respect `persona_acl.yaml`
keyed by `(subject, persona)`. ACL violations are **R0 hard fails** — return a SUPPRESS
decision immediately; do not apply heuristics or soft-fail.

```python
# In a persona skill bundle — check ACL before any action
if not acl.check(subject=context.raw["subject"], persona="friday", action=requested_action):
    return Decision(tier=Tier.SUPPRESS, action=None, payload={},
                    confidence=1.0, reason="ACL: subject not permitted for this persona+action")
```

Rule (§9 R4): **a persona may never use adult-scoped tools when the subject is a kid.** This
is enforced at R0, not as an LLM heuristic.

---

## What NOT to Do

- **No direct DB writes from modules.** All persistence goes through the engine or adapters.
  `decision_log` is written by the engine. `dirty_scores` are written by the adapter.
- **No calling HA directly.** Use `ha_adapter.emit_action(decision)`. Modules return a
  `Decision`; the Action Layer dispatches it.
- **No importing FamilyOps / Nexus / BookQuest client code.** Use their adapters only.
- **No hardcoded model URLs or IPs.** Use `get_litellm_client(settings)` — the endpoint
  is `settings.litellm_base_url` (default: `http://ollama.perwnet.com:4000`). IPs drift
  on VLAN moves; the hostname is stable (§4.1).
- **No silent L3 escalation.** `allow_l3: true` must appear in `module_config` and requires
  Carlos's approval at configuration time (§3.3).
- **No agent runtimes.** CORTEX-Python is the only runtime. Don't spawn subprocesses,
  import LangGraph orchestration primitives, or shell out to OpenClaw. (§11)

---

## Adding Evals

All per-module evals live in `tests/evals/`. SHODAN owns the harness; module authors
contribute scenario fixtures.

1. Add a fixture file: `tests/evals/fixtures/<module>/scenario_<name>.json`
2. Write an eval test: `tests/evals/test_<module>.py`
3. Use the shared fixtures from `tests/evals/conftest.py` (mock LiteLLM client,
   mock `DecisionLogger`, sample `ModuleContext`).
4. Every eval must assert on `Decision.tier`, `Decision.confidence`, and `Decision.action`.
   No golden-string matching on LLM output — assert on the parsed Pydantic schema only.

See `tests/evals/README.md` for the full harness description.
