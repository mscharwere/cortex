# CORTEX Evals Harness

**Owner:** SHODAN (Prompt Engineer, AIT)
**Status:** Scaffold — Phase 0 closeout. SHODAN populates per-module evals in Phase 1+.

---

## What This Is

The evals harness is the quality gate for CORTEX decision logic. It tests:

- **R0/R1 rules** — deterministic Python predicates; standard pytest unit tests.
- **L1/L2 LLM skills** — output schema conformance, tier selection, confidence ranges.
  The LiteLLM client is mocked; tests assert on parsed Pydantic output, not raw strings.
- **Overflow conditions** — that the module correctly yields `Tier.OVERFLOW` on parse
  failure, low confidence, or timeout.

Evals do **not** make live LiteLLM / Ollama / Lemonade calls. They are fast,
deterministic, and run in CI (`pytest tests/evals/`).

---

## Harness Layout

```
tests/evals/
├── README.md             ← this file
├── conftest.py           ← shared fixtures (mock LiteLLM, mock DecisionLogger, ModuleContext)
├── test_smoke.py         ← placeholder; SHODAN replaces with real evals per module
└── fixtures/
    └── <module>/
        └── scenario_<name>.json   ← canned LLM responses + expected Decision fields
```

---

## How to Add Evals for a Module

1. **Add scenario fixtures** under `tests/evals/fixtures/<module>/scenario_<name>.json`:

   ```json
   {
     "description": "High dirty score + carlos home → dispatch sam",
     "mock_llm_response": { "action": "dispatch_vacuum", "confidence": 0.87, "reason": "..." },
     "expected_tier": "L1",
     "expected_action": "dispatch_vacuum",
     "expected_confidence_gte": 0.70
   }
   ```

2. **Write the eval test** in `tests/evals/test_<module>.py`:

   ```python
   import json, pytest
   from pathlib import Path
   from tests.evals.conftest import make_context   # see conftest.py

   FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vacuumops"

   @pytest.mark.parametrize("scenario_file", FIXTURE_DIR.glob("scenario_*.json"))
   async def test_vacuumops_scenarios(scenario_file, mock_litellm_client, mock_decision_logger):
       scenario = json.loads(scenario_file.read_text())
       mock_litellm_client.set_response(scenario["mock_llm_response"])
       ctx = make_context(presence={"carlos": "home"})

       from cortex_python.modules.vacuumops import decide
       decision = await decide(ctx)

       assert decision.tier.value == scenario["expected_tier"]
       assert decision.action == scenario["expected_action"]
       assert decision.confidence >= scenario["expected_confidence_gte"]
   ```

3. **Assert on parsed schema only.** Never assert on raw LLM string output — that is
   brittle. Assert on `Decision.tier`, `Decision.action`, `Decision.confidence`,
   and the Pydantic model fields parsed from the mock response.

4. **Cover overflow paths.** Every module must have at least one scenario where the
   mock returns malformed JSON and the test asserts `tier == OVERFLOW`.

---

## Running Evals

```bash
# From repo root
pytest tests/evals/ -v

# Fast (skip slow integration)
pytest tests/evals/ -v -m "not integration"
```

CI runs `pytest tests/evals/` on every PR. Failures block merge (ARIIA enforces).

---

## SHODAN Handoff Notes

- The `conftest.py` in this directory provides `mock_litellm_client`, `mock_decision_logger`,
  and `make_context`. Extend these fixtures as needed — do not duplicate them across test files.
- Per-module scenario fixture JSON schema is informal for now. Formalize once VacuumOps
  evals are written and the pattern is validated.
- Risk R9 (§9 architecture spec): different models have different tool-call reliability.
  Evals should track schema-fail rate per model string. Add a `model` field to scenario
  fixtures and aggregate fail rates in CI output.
- `tests/integration/test_litellm_roundtrip.py` (separate from evals) covers live
  LiteLLM round-trip smoke. Do not merge those concerns into this harness.
