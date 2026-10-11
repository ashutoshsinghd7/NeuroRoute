# DECISIONS.md — NeuroRoute

A short log of choices the team made, and why.
Format: **Decision → Reason → Who/where it affects.**
Rule from `AGENT.md` section 10: when unsure, choose the safer option.

---

## CP2 — Token size and cost (`src/gateway/tokens.py`)

### D-01: Token count = `ceil(len(prompt) / 4)`
- **Reason:** `CHECKPOINTS.md` CP2 says "length / 4 is simple and good enough". It needs no library and works offline (`AGENT.md` rule 8).
- **Safer side:** we round **up**. Guessing too small could let a model that is too small look valid.
- **Edge case:** an empty prompt gives 0 tokens. Any non-empty prompt gives at least 1.

### D-02: Expected answer size = 500 tokens
- **Reason:** the docs say "expected_answer_tokens" but give no number. We picked 500 as a placeholder.
- **Where:** the constant `EXPECTED_ANSWER_TOKENS` in `tokens.py`. Change it in one place if the team wants a different value.

### D-03: Needed context = `max(required_context, prompt_tokens + answer_tokens)`
- **Reason:** copied from `CHECKPOINTS.md` CP2 step 2. The model must hold the user's stated need and the real prompt plus its answer.
- **Where it is used:** CP4 compares this number to each model's `context_limit`.

### D-04: Cost is calculated per model, not per request
- **Reason:** every model has its own price, so one request has many possible costs.
- **Formula:** `total_tokens / 1000 * cost_per_1k`.
- **Where it is used:** CP4 calls `estimate_cost()` once for each model inside its loop.

### D-05: Registry field name is `cost_per_1k`
- **Reason:** `AGENT.md` section 7 uses `cost_per_1k`, but `CHECKPOINTS.md` says `cost_per_1k_tokens`. `AGENT.md` is the stricter file, so we follow it.
- **Action:** CP0 owner must use `cost_per_1k` in `registry.json`.

### D-06: CP2 uses plain values, not schema objects
- **Reason:** CP0 and CP1 are owned by other teammates. `tokens.py` takes a `str` and an `int`, so it does not import `schemas.py` or read `registry.json`.
- **Result:** CP2 can be built and tested alone, and plugs in later with no changes.

### D-07: No AI in CP2
- **Reason:** `AGENT.md` rule 1: hard constraints are plain Python. Token and cost math must be 100% checkable.

### D-08: Single entry point for CP4 is `analyse_request()`
- **Reason:** one call returns `prompt_tokens`, `total_tokens` and `needed_context`, so the CP4 owner does not have to rebuild the math.

### D-09: Tests live in `tests/test_token.py`
- **Reason:** `AGENT.md` section 8 says every change needs tests.
- **Note:** run with `python -m pytest tests/test_token.py`. Running with `python -m` adds the project folder to Python's path, so `from src.gateway.tokens import ...` works.

---

## Open items

| # | Item | Owner |
|---|---|---|
| O-01 | Make sure `registry.json` uses `cost_per_1k` | CP0 owner |
| O-02 | Delete or ignore the empty `src/main.py`; the real file is `src/gateway/main.py` | CP0 owner |
| O-03 | Confirm `src/__init__.py` and `src/gateway/__init__.py` exist | CP0 owner |
| O-04 | In `main.py`, call `analyse_request(req.prompt, req.required_context)` and pass the result to CP4 | CP1 / CP4 owner |