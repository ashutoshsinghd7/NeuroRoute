# AGENT.md — Instructions for AI coding assistants

Read `CONTEXT.md` first. It defines the scope. Do not build anything listed under "OUT OF SCOPE".

## 1. What this project is
**NeuroRoute** is an inference gateway (problem A-01, ModelRouter). For each request it picks an AI model that follows **all** hard constraints (max latency, max cost, min capability, required context), prefers the cheapest/fastest/best by the stated preference, fails over safely, refuses clearly when nothing fits, explains every decision, and logs everything.

The team has **6 hours**. Prefer simple, working code over clever code.

## 2. Non-negotiable rules
1. **Hard constraints are plain Python.** No AI call may decide validity.
2. The classifier may only **raise** the required capability. It must never lower the user's `min_capability`.
3. **Failover uses only the already-validated, already-ranked list.** Never re-run the filter with looser limits.
4. Before each failover attempt, re-check the **remaining time budget** (`max_latency_ms − elapsed`).
5. If no valid model exists, return `NO_VALID_MODEL` with reasons. **Never** call an invalid model.
6. Every response includes a **decision trace**: considered, rejected (with reason), chosen, attempts.
7. Low classifier confidence (< 0.6) → assume the **harder** level, and set `low_confidence_routing: true` in the trace.
8. The app must run **fully offline**. No paid API, no internet, no GPU.
9. Use **fictional data only**.

## 3. Build order (follow `CHECKPOINTS.md`)
CP0 setup → CP1 validate → CP2 tokens → **CP4 filter** → CP5 rank → CP6 call + timeout → CP7 failover → CP8 refusal → CP3 classifier → CP9 logging → CP10 dashboard → CP11 tests.
Build the rule-based core before the classifier. The core must work with the classifier turned off.

## 4. Repo layout
```
src/gateway/     main.py, schemas.py, tokens.py, filter.py, ranking.py, failover.py, logger.py
src/models/      registry.json, simulated.py
src/classifier/  prompts.csv, train.py, predict.py
src/dashboard/   app.py
tests/           test_filter.py, test_ranking.py, test_failover.py, test_refusal.py
docs/            architecture.md, scenarios.md, decision-trace-example.md, demo-script.md
```

## 5. Commands
```bash
pip install -r requirements.txt
python src/classifier/train.py                 # train classifier
uvicorn src.gateway.main:app --reload          # run the API
streamlit run src/dashboard/app.py             # run the judge panel
pytest                                         # run tests
```

## 6. Coding style
- Python 3.10+, type hints on public functions.
- Small functions, one job each. Filter, ranking and failover must be separate, testable functions.
- Write comments and docstrings in **simple, plain English**. The team may include beginners.
- Reasons for rejection are short fixed strings, e.g. `"capability 1 < 3"`, `"latency 3000 > 2000"`, `"model is down"`.
- Make failure simulation **seedable** so demos are reproducible.
- Do not add new dependencies without need. Allowed: fastapi, uvicorn, pydantic, scikit-learn, joblib, streamlit, httpx, pytest (and optionally pandas).

## 7. Data shapes (keep consistent)
**Request**
```json
{ "prompt": "...", "max_latency_ms": 2000, "max_cost": 0.10,
  "min_capability": 3, "required_context": 20000,
  "preference": "cheapest" }
```
**Model (registry.json)**
```json
{ "name": "pro-3", "capability": 3, "context_limit": 64000,
  "cost_per_1k": 0.30, "latency_ms": 1500, "failure_rate": 0.0, "status": "up" }
```
**Response**
```json
{ "status": "OK | NO_VALID_MODEL",
  "answer": "...", "chosen_model": "pro-3",
  "trace": { "complexity": {"level": 3, "confidence": 0.58, "low_confidence_routing": true},
             "considered": [{"model": "...", "valid": true, "reason": null}],
             "attempts": [{"model": "...", "status": "success|error|timeout", "ms": 0}],
             "cost": 0.0, "total_latency_ms": 0 } }
```

## 8. Definition of done for any change
- Relevant pytest tests pass, with at least one test per hard rule.
- A rejected model never appears in the attempts list.
- The trace explains every considered model.
- The change did not add anything from "OUT OF SCOPE".

## 9. Do NOT
- Do not build a browser extension, desktop app, login system, or real network proxy.
- Do not call real paid APIs in the core path.
- Do not silently relax a constraint to "make it work".
- Do not hide errors; log them in the trace.
- Do not use real personal or financial data.

## 10. When unsure
Choose the **safer** option: reject rather than accept, refuse rather than guess, assume the harder level rather than the easier.
