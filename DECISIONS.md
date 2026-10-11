# DECISIONS.md — choices made while building (read with AGENT.md, CONTEXT.md, CHECKPOINTS.md)

## Overrides of the original guides
| # | Decision | Replaces | Why |
|---|---|---|---|
| D-01 | Dashboard is **plain JavaScript** (`src/dashboard/index.html, app.js, style.css`), served by FastAPI at `/`. | Streamlit in AGENT.md / CONTEXT.md / README | Team asked for a JS frontend. Still a live judge panel, so the JC is met. Streamlit references are superseded. |
| D-02 | Classifier is **hardcoded rules by default**. The TF-IDF + Logistic Regression version is in the same file, **commented out**. | CP3 "train then use" | Project works with zero ML install. Enable the AI in 3 steps (see `docs/models-and-stack.md`). |
| D-03 | Added `src/gateway/router.py` (the whole pipeline, no web framework). | Not in REPO_STRUCTURE.md | Keeps `main.py` thin and lets tests run without FastAPI. |
| D-04 | Judge edits (up/down, latency, failure rate) change an **in-memory copy** of the registry. `Reset fleet` reloads `registry.json`. | CP10 "write to registry.json or memory" | Safe: the file stays clean for the next demo. |
| D-05 | Simulated failures are seeded by `NEUROROUTE_SEED` + model + prompt. | CP11 reproducibility | Same input gives same outcome. |
| D-06 | "Skipped" attempts appear in the trace for valid models with no time left. | CP7 step 3 | Makes the remaining-budget rule visible. |
| D-07 | 6 extra fake models are documented only (`docs/models-and-stack.md`), not loaded. | n/a | Keeps the demo registry small. Paste the JSON to add them. |

## Checkpoint status
CP0 registry.json, CP1 schemas.py, CP2 tokens.py, CP3 classifier/, CP4 filter.py, CP5 ranking.py, CP6 simulated.py + failover.py, CP7 failover.py, CP8 router.py (`NO_VALID_MODEL`), CP9 logger.py, CP10 dashboard/, CP11 tests/ + docs/. All built.

## Honest notes
- The 18 pytest tests were run with a plain Python runner in a sandbox that had no pytest, FastAPI or internet. **The live API and the dashboard were not run here.** Run `pytest` and the server once on your machine.
- `prompts.csv` has 30 rows (6 per level). Add more (about 12 per level) before enabling the ML classifier.
- The hardcoded classifier is a keyword and length heuristic. It is a guess, so low confidence raises the level by 1.
