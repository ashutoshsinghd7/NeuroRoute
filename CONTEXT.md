# CONTEXT.md — Scope Lock for NeuroRoute (ModelRouter, A-01)

> This file is the single source of truth. If an idea is not in "IN SCOPE", we do not build it.
> Every decision below points to evidence: the **Problem Statement (PS)** or the **Judging Criteria (JC)**.

## 1. Fixed facts

| Fact | Value | Evidence |
|---|---|---|
| Event | VORTEXA 3.0, undergraduate hackathon | JC: Event Introduction |
| Problem | A-01 ModelRouter — cost-, latency- and quality-aware AI routing | PS title |
| Team | 2–4 people | JC: "Teams of two to four" |
| Our time limit | **6 hours** (the event is 12 h, but our own constraint is 6 h) | Stated by team |
| Deliverable | A working, live-testable software MVP | JC: Common Rules → Deliverable |

## 2. The problem in one paragraph
Apps can use many AI models. Each differs in price, speed, smartness, and how much text it can read. Every request has hard limits (max time, max cost, minimum smartness, required context size). We build a **gateway** that picks a model that follows *all* limits, switches to another valid model when one fails, refuses clearly when none fits, explains every choice, and records everything.

## 3. IN SCOPE — what we build

| # | Feature | Evidence it is needed |
|---|---|---|
| 1 | One API door (`POST /route`) that takes a prompt + constraints + preference | PS: "Each request carries explicit constraints" |
| 2 | **Hard filter**: reject any model that breaks latency, cost, capability, context, or is down | PS: "These are hard conditions; a model that violates any of them is not a valid choice" |
| 3 | **Ranking** of valid models by preference (`cheapest`, `fastest`, `highest_quality`) | PS: "choose according to a stated preference, such as the lowest cost" |
| 4 | **Decision trace**: considered models, rejected models, reasons | PS: "show which models were considered and why others were rejected" |
| 5 | **Safe failover** from the already-valid list, with remaining-time re-check | PS: "Failover must never relax a constraint" |
| 6 | **Clear refusal** ("NO_VALID_MODEL") | PS: "say so clearly rather than sending the request to an invalid model" |
| 7 | **AI complexity classifier** with confidence; low confidence → safer (harder) level | PS: "Treat such estimates as uncertain… choose the safer option"; JC: AI must be meaningful (classification) |
| 8 | **Simulated models** (adjustable delay, failure rate, up/down) | JC: no paid API, local fallback; reproducible testing |
| 9 | **SQLite logging** of cost, latency, failures, decisions | PS: "Track cost, latency, failures, and routing decisions" |
| 10 | **Streamlit dashboard** with a judge control panel | PS: "Judges must be able to change a model's latency or availability and see a valid alternative selected"; JC: judges test live |
| 11 | **pytest tests** for the filter, ranking, failover, refusal | JC: Correctness → "explicit, checkable rules" |
| 12 | Fictional model catalogue + hand-written sample prompts | JC: Data → "Use fictional data only" |

## 4. OUT OF SCOPE — what we do NOT build

| Not building | Reason (evidence) |
|---|---|
| Browser extension | Not asked in PS; adds permissions, packaging and store rules; no judging benefit |
| Desktop app (Electron / .exe) | Not asked; JC only needs something judges can test live; wastes hours on packaging |
| Real paid APIs (OpenAI, Anthropic, etc.) | JC: "no paid API… core demonstration must not depend on them" |
| LiteLLM / OpenRouter in the core | Core must work offline; simulated models already meet the need |
| Real network proxy (HTTP forwarding, SSL, VPN) | The PS asks for an *inference gateway*, not a network proxy |
| Model training / fine-tuning of LLMs | Not in PS; no GPU allowed |
| Sentence-transformer embeddings | TF-IDF is enough in 6 hours; revisit only if finished early |
| User accounts, login, billing | Not in PS |
| Streaming responses, caching, rate limiting | Not in PS |
| Real customer data or real transactions | JC: fictional data only |
| Multiple deployed cloud services | JC: no paid cloud required; local run is fine |
| Static slides or mockup-only UI | JC: "Mockups, static dashboards, slides… alone are not enough" |

## 5. Optional (only if CP0–CP11 are finished early, in this order)
1. Ollama real local model as one extra endpoint
2. `highest_quality` preference polish
3. Extra chart on the dashboard
4. Embeddings classifier upgrade

## 6. Non-negotiable rules (never break)
1. **Rules before AI.** Hard constraints are plain code. The AI estimate may only *raise* the needed capability; it never lowers the user's own minimum.
2. **Failover never relaxes a constraint.** Backups come only from the validated list.
3. **No valid model = refuse.** Never send to an invalid model.
4. **Every response carries a decision trace.**
5. **Works with no internet.**
6. **Fictional data only.**

## 7. Tech stack (locked)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Fastest for team, best AI libraries |
| API | FastAPI + Pydantic | Quick, auto input validation, built-in test page |
| Classifier | scikit-learn (TF-IDF + Logistic Regression) | Small, CPU-only, gives probabilities (confidence) |
| Model calls | Simulated async functions (`asyncio`) | Reproducible, offline, adjustable by judges |
| Storage | SQLite | Zero setup |
| UI | Streamlit | Interactive dashboard in about an hour |
| Tests | pytest | Proves rules work |

## 8. Interface decision: Dashboard, not extension, not desktop app

| Option | Matches PS/JC? | Time cost | Risk | Verdict |
|---|---|---|---|---|
| **Streamlit dashboard** | Yes: PS needs judges to change latency/availability live; JC wants a live-testable MVP | Low (about 1 h) | Low | **BUILD** |
| Browser extension | No requirement | High | High (packaging, permissions) | Skip |
| Desktop file / app | No requirement | High | Medium–High | Skip |

The "API + dashboard" pair is enough: the API is the gateway, the dashboard is how judges control and watch it.

## 9. Definition of done (MVP)
- [ ] A request with valid constraints returns an answer plus a trace
- [ ] Setting a model "down" in the dashboard makes another valid model answer
- [ ] Raising a model's latency above the budget makes it rejected or timed out → valid alternative
- [ ] Impossible constraints return `NO_VALID_MODEL` with reasons
- [ ] Vague prompt shows low confidence and the safer choice
- [ ] Logs show cost, latency, failures, decisions
- [ ] Runs fully offline; `pytest` passes
- [ ] README explains the problem for a non-expert

## 10. Build order and timeline (6 h)
Setup 0:00 → Validate input → Token count → **Hard filter** → Rank → Call + timeout → Failover → Refusal → **Classifier** → Logging → Dashboard → Tests/demo (6:00). Details: `CHECKPOINTS.md`.

## 11. Cut list if time runs short (in order)
Charts → `highest_quality` → classifier (last resort; JC requires meaningful AI). **Never cut:** hard filter, safe failover, refusal, decision trace.
