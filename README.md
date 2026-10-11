# NeuroRoute — ModelRouter (A-01)
### A smart "traffic police" for AI models: cost-, latency- and quality-aware routing

> VORTEXA 3.0 · Domain 2 — AI Systems & Infrastructure · Problem A-01

---

## 1. The problem in plain words (no AI background needed)

### 1.1 The everyday picture
Think of a hospital with three kinds of doctors:

| Doctor | Fee | Speed | Can handle |
|---|---|---|---|
| Nurse / junior doctor | ₹ | Very fast | Cold, small injuries |
| General physician | ₹₹ | Medium | Most normal illnesses |
| Specialist surgeon | ₹₹₹₹ | Slow | Serious, complex cases |

- If **every** patient sees the surgeon → very expensive and slow.
- If **every** patient sees the nurse → hard cases get bad treatment.
- If the doctor you picked **doesn't show up** → you need a backup, **and the backup must still be right for the patient** (not just "whoever is free").

Now replace "doctors" with **AI models** (like GPT, Claude, Llama, Gemini). Companies use several of them. Each model differs in:

| Property | Meaning | Simple example |
|---|---|---|
| **Cost** | Price per request | Small model: ₹0.01, big model: ₹1 |
| **Latency** | How long it takes to answer | 0.5 s vs 8 s |
| **Capability** | How "smart" it is on hard tasks | Can it analyse a 40-page contract? |
| **Context length** | How much text it can read at once | 8,000 words vs 200,000 words |
| **Reliability** | Does it stay online? | Servers crash or slow down sometimes |

### 1.2 What goes wrong today
1. **Overpaying:** a simple question like *"What is the capital of France?"* is sent to the most expensive model.
2. **Bad quality:** a hard task like *"Find risky clauses in this contract"* is sent to the cheapest model.
3. **Breaking:** the app is hard-wired to one model. If that model is down, the whole feature stops.
4. **Unsafe backup:** in a hurry, the backup model chosen is cheaper but cannot read long documents → silently wrong result.

### 1.3 What we must build
An **AI Gateway** (a "router"): one door that all requests pass through. For each request it:

1. Reads the request's **rules** (constraints).
2. Picks the **best allowed model**.
3. If the model fails or is too slow, **switches to another allowed model**.
4. If nothing is allowed, says **"No valid model"** — it never sends the request to a wrong model.
5. **Explains** every decision and **records** cost, speed and failures.

### 1.4 The rules a request carries (HARD constraints)
"Hard" means **no exceptions**. A model that breaks even one rule is *not a valid choice*.

| Rule | Meaning | Example |
|---|---|---|
| `max_latency_ms` | Answer must come within this time | 2000 ms |
| `max_cost` | Must not cost more than this | ₹0.05 |
| `min_capability` | Model must be at least this smart (e.g. level 1–5) | level 3 |
| `required_context` | Model must be able to read this much text | 50,000 tokens |
| `preference` | Among valid ones, what do we prefer? | "cheapest" |

> **Important idea:** *Rules are not preferences.* First remove every model that breaks a rule. Only then choose among the rest.

### 1.5 Glossary (words you will see)
- **LLM / model:** the AI that reads text and writes answers.
- **Token:** a small piece of text (≈ ¾ of a word). Models charge and limit by tokens.
- **Endpoint:** the web address where a model runs.
- **Failover (fallback):** automatically switching to another model when the first fails.
- **Gateway / router:** the middle layer that decides which model gets the request.
- **Complexity estimate:** our guess of how hard a request is, based on its text.

### 1.6 Five things the judges will check (from the problem statement)
1. ✅ Hard constraints are never broken — not even during failover.
2. ✅ Every decision is **explainable** (who was considered, who was rejected, and why).
3. ✅ Failure/slowness → a **valid** alternative is chosen.
4. ✅ Impossible request → **clear refusal**, not a wrong model.
5. ✅ Complexity guesses are treated as **uncertain**; when unsure → pick the **safer** option.
6. ✅ Logs of cost, latency, failures and decisions; judges can **change a model's latency/availability live** and see the router react.

---

## 2. Hackathon requirements — what we must satisfy

From the *Judging Criteria / Common Rules*:

| Rule | What it means for us |
|---|---|
| Working **live MVP** | A running app judges can click and test. No slides-only / static dashboards. |
| **Reproducible** testing | Same input → same, repeatable behaviour. Include sample requests and a seed. |
| **Meaningful AI** | Not just "an LLM call that rewrites text". Our AI = **request-complexity classifier** that affects routing. |
| **Correctness by explicit rules** | AI may *inform*, but **hard constraints are plain, checkable code** and AI can never override them. |
| **Fictional data only** | Use our own made-up model catalogue and sample prompts. |
| **No paid API / GPU** | Core demo must run **fully local**. External AI is optional. |
| **Local fallback** | Provide simulated and/or local models (e.g. Ollama small model) so the demo never depends on internet. |

---

## 3. Existing solutions (what already exists, and what we borrow)

| Tool | What it does | Strength | Gap vs. this problem | How we use it |
|---|---|---|---|---|
| **LiteLLM** (open source) | One interface to 100+ models; has router with fallbacks, retries, cooldowns, cost tracking, latency-based routing | Very popular, reliable, easy | Does not enforce a *per-request* set of hard constraints with an explainable rejection list | Optional: use as the **call layer** for real models |
| **OpenRouter** | Hosted gateway to many models with price/latency info and fallbacks | Easy | Paid, external, black-box decisions | Reference only (not allowed as core) |
| **RouteLLM** (LMSYS, open source) | Trained routers that decide *strong vs. weak model* to save cost | Proven idea: classifier decides model | Only 2-model, no hard constraints/failover | **Idea borrowed**: classify difficulty → route |
| **Martian / Not Diamond** | Commercial "model routers" that predict the best model | Good quality | Paid, closed | Reference only |
| **Portkey / Helicone / Cloudflare AI Gateway** | Gateways with logging, caching, retries, observability | Great dashboards | Hosted/paid, not constraint-first | **Idea borrowed**: logging + dashboard |
| **Ollama** | Runs small open models locally (e.g. Llama / Qwen / Phi) | Free, offline, simple | Not a router | **Local fallback models** |
| **scikit-learn** | Classic ML (TF-IDF, logistic regression) | Tiny, fast, no GPU, trustworthy | — | **Complexity classifier** |
| **FastAPI** | Python web API framework | Fast to build, auto docs (Swagger) | — | The **gateway server** |
| **Streamlit** (or simple HTML) | Quick dashboards | Built in an hour | — | **Judge control panel** |
| **SQLite** | File-based database | Zero setup | — | **Logs / metrics** |

**Takeaway:** existing tools solve *"call many models"* and *"save cost with a classifier"*, but none gives a **constraint-first, explainable, never-relax-on-failover** decision out of the box. That decision engine is **our core contribution**; everything else we reuse.

---

## 4. Technical approach

### 4.1 Design principle: "Rules first, AI second"
```
HARD RULES (plain code, 100% checkable)   →   decide who is ALLOWED
AI estimate (uncertain, helpful)           →   helps choose among the ALLOWED, never overrides rules
```

### 4.2 Pipeline flowchart

```mermaid
flowchart TD
    A([Client request<br/>prompt + constraints + preference]) --> B[1. Validate input<br/>FastAPI + Pydantic]
    B --> C[2. Estimate prompt size in tokens<br/>tiktoken or length/4]
    C --> D[3. AI complexity classifier<br/>TF-IDF + Logistic Regression<br/>outputs level + confidence]
    D --> E{Confidence<br/>high enough?}
    E -- Yes --> F[Use estimated level]
    E -- No --> G[Safer option:<br/>assume HARDER level]
    F --> H
    G --> H[4. HARD FILTER over model registry<br/>latency, cost, capability, context, health<br/>every rejection gets a reason]
    H --> I{Any valid<br/>models left?}
    I -- No --> Z([Clear refusal:<br/>NO VALID MODEL + reasons<br/>request is NOT sent])
    I -- Yes --> J[5. RANK valid models<br/>by preference: cheapest / fastest / best quality]
    J --> K[6. Call top model<br/>with timeout = latency budget]
    K --> L{Success within<br/>budget?}
    L -- Yes --> M[7. Return answer + decision trace]
    L -- No: error / timeout --> N[Mark model unhealthy<br/>log failure]
    N --> O[8. FAILOVER: take next model<br/>from the SAME valid list<br/>constraints never relaxed<br/>remaining budget re-checked]
    O --> P{Another valid<br/>model left?}
    P -- Yes --> K
    P -- No --> Z
    M --> Q[(SQLite logs<br/>cost, latency, failures, decisions)]
    Z --> Q
    Q --> R[Dashboard + judge control panel<br/>edit latency / availability live]
    R -.-> H
```

### 4.3 Step-by-step explanation

**Step 1 — Validate the request.** Use FastAPI + Pydantic so wrong inputs (negative cost, missing fields) are rejected immediately.

**Step 2 — Count tokens.** Estimate how long the prompt is. This tells us the *required context* (prompt + expected answer) and the *expected cost* (tokens × price).

**Step 3 — AI complexity classifier (our "meaningful AI").**
- A small text classifier (TF-IDF + Logistic Regression, scikit-learn) trained on ~100–200 **self-written fictional prompts** labelled *simple / medium / hard*.
- Output: a **level** and a **confidence**. Example: `hard (0.62)`.
- **If confidence is low → choose the safer option** = treat as the harder level.
- Optional upgrade: sentence embeddings (`sentence-transformers`, runs on CPU) for better accuracy. Keep TF-IDF as the always-works fallback.
- Add simple signals too: length, words like "analyse / compare / legal clause / prove", code blocks, numbers.

**Step 4 — Hard filter (pure code, no AI).** For each model in the registry, check in order and record the **first failing reason**:

| Check | Reject if… |
|---|---|
| Health | model marked down |
| Context | `required_context > model.context_limit` |
| Capability | `model.capability < max(min_capability, estimated_level)` |
| Latency | `model.latency_ms > max_latency_ms` |
| Cost | `estimated_cost > max_cost` |

> Detail on safety: the AI estimate may only **raise** the needed capability (safer). It never lowers the user's own `min_capability`. If raising it makes the list empty, we fall back to the user's stated minimum and flag the answer as **"low-confidence routing"** — we do not break the user's hard cost/latency limits.

**Step 5 — Rank.** Among valid models, sort by the preference (`cheapest`, `fastest`, `highest_quality`). Ties break by reliability score.

**Step 6 — Call with a timeout.** The timeout equals the latency budget, so "too slow" is treated as a failure.

**Step 7/8 — Failover without relaxing.** On error or timeout, move to the **next model in the already-validated list**. Because the list was built with all constraints, the backup *automatically* respects them. We also re-check the **remaining** time budget so a slow first attempt doesn't make the second attempt illegal. If the list is exhausted → clear refusal.

**Step 9 — Record everything.** Save: request id, chosen model, rejected models + reasons, cost, latency, attempts, failures.

### 4.4 Model registry (fictional, editable)

| Model | Capability (1–5) | Context (tokens) | Cost / 1K tokens | Avg latency | Status |
|---|---|---|---|---|---|
| `nano-1` | 1 | 4,000 | ₹0.01 | 300 ms | up |
| `mini-2` | 2 | 16,000 | ₹0.05 | 700 ms | up |
| `pro-3` | 3 | 64,000 | ₹0.30 | 1,500 ms | up |
| `ultra-4` | 4 | 128,000 | ₹1.00 | 3,000 ms | up |
| `max-5` | 5 | 200,000 | ₹2.50 | 5,500 ms | up |
| `local-llama` (Ollama) | 2 | 8,000 | ₹0 | 1,200 ms | up |

- **Simulated models** (fake endpoints with adjustable delay and failure rate) make the demo **fully reproducible and offline**.
- 1–2 can be **real local models via Ollama**, so the gateway also does real inference.

### 4.5 Example decision trace (what judges will see)

```json
{
  "request_id": "r-1042",
  "constraints": {"max_latency_ms": 2000, "max_cost": 0.10, "min_capability": 3, "required_context": 20000},
  "complexity_estimate": {"level": 3, "confidence": 0.58, "action": "low confidence -> kept safer level 3"},
  "considered": [
    {"model": "nano-1",  "valid": false, "reason": "capability 1 < 3"},
    {"model": "mini-2",  "valid": false, "reason": "capability 2 < 3"},
    {"model": "pro-3",   "valid": true,  "est_cost": 0.06, "latency_ms": 1500},
    {"model": "ultra-4", "valid": false, "reason": "latency 3000 > 2000"},
    {"model": "max-5",   "valid": false, "reason": "latency 5500 > 2000"}
  ],
  "chosen": "pro-3",
  "attempts": [{"model": "pro-3", "status": "timeout"}],
  "result": "NO VALID MODEL left after failover (all other models violate constraints)"
}
```

### 4.6 Judge control panel (for live testing)
- Sliders/inputs to **change each model's latency**, **toggle it up/down**, and set a **failure rate**.
- A request form: prompt + the 5 constraints.
- Buttons for **preset scenarios**: "Simple question", "Long contract", "Primary model down", "Impossible constraints", "Ambiguous prompt".
- Live view: decision trace, cost/latency charts, failure counter, request history table.

### 4.7 Suggested tech stack (all free, all offline-capable)

| Layer | Tool |
|---|---|
| API / gateway | Python + **FastAPI** |
| Data validation | **Pydantic** |
| AI classifier | **scikit-learn** (TF-IDF + Logistic Regression); optional **sentence-transformers** |
| Token counting | **tiktoken** or `len(text)/4` |
| Model calls | **httpx** (async, timeouts); optional **LiteLLM** for real providers |
| Local real model | **Ollama** (small model) |
| Storage | **SQLite** |
| UI | **Streamlit** (fastest) or plain HTML + JS |
| Tests | **pytest** for constraint and failover rules |

---

## 5. Test scenarios (also our demo script)

| # | Scenario | Expected result |
|---|---|---|
| 1 | Short factual question, cheap preference | Cheapest valid model chosen |
| 2 | Long contract, large context needed | Only large-context models considered; small ones rejected with reasons |
| 3 | Judge sets chosen model to **down** | Next valid model chosen automatically |
| 4 | Judge raises chosen model's **latency** above budget | Model rejected / timed out → valid alternative |
| 5 | Failover candidate would break the cost limit | Skipped; never used |
| 6 | Impossible constraints (e.g. 100 ms + level 5) | Clear **"No valid model"** with reasons; nothing sent |
| 7 | Vague prompt, low classifier confidence | Safer (higher) level assumed, flagged in trace |
| 8 | Same request run twice | Same decision (reproducible) |

---

## 6. 6-hour build plan (our real time limit)

**Rule of the day:** build the core first, make it demo-able by hour 4, polish after.

| Hour | Work | Done when |
|---|---|---|
| 0:00–0:30 | Fix the model registry (JSON file), request format, folder structure. Split work among teammates. | Everyone knows their part |
| 0:30–2:00 | **Core engine:** hard filter + ranking + decision trace (plain Python, with a few pytest tests) | Given constraints, correct valid/rejected list with reasons |
| 2:00–3:00 | **Simulated models + failover:** fake endpoints with adjustable delay / on-off; timeout; failover from the validated list | Switching a model off picks a valid alternative |
| 3:00–4:00 | **Complexity classifier:** ~60 hand-written prompts, TF-IDF + Logistic Regression, confidence, "low confidence -> safer level" rule | Classifier changes routing; ambiguous prompt is flagged |
| 4:00–5:00 | **Streamlit panel:** request form, latency/availability controls, decision trace, SQLite log table, simple cost/latency chart | Judges can test everything live |
| 5:00–5:30 | 5 preset scenarios (simple, long contract, model down, impossible, ambiguous) + edge-case checks | Scenarios 1-7 in Section 5 pass |
| 5:30–6:00 | README polish, demo rehearsal, backup screenshots | Demo runs end to end twice |

### What we cut or postpone (only add if time is left)
| Feature | 6-hour decision |
|---|---|
| Ollama real local model | **Optional**, only after hour 5 |
| LiteLLM / real providers | **Skip** (simulated models already satisfy "no paid API") |
| sentence-transformers embeddings | **Skip** (TF-IDF is enough) |
| Fancy dashboard / charts | One simple chart + a log table |
| Multiple preference types | Build `cheapest` and `fastest` first; add `highest_quality` last |

### Suggested split for a team of 2-4
| Person | Owns |
|---|---|
| A | Core engine: filter, ranking, trace, tests |
| B | Simulated models, timeouts, failover |
| C | Classifier and its small dataset |
| D | Streamlit panel, logging, README, demo script |

(With two people: A+B and C+D.)

---

## 7. Judging-criteria mapping

| Judge requirement | Where we satisfy it |
|---|---|
| Working live MVP | FastAPI + Streamlit app, runs locally |
| Meaningful AI | Complexity classifier changes routing; handled with confidence |
| Correctness by explicit rules | Hard filter is plain code + pytest; AI cannot override |
| No paid API / local fallback | Simulated models + Ollama; no internet needed |
| Fictional data | Self-written catalogue and prompts |
| Explainability | Per-request trace: considered / rejected / reason |
| Failover without relaxing constraints | Fallbacks come only from the pre-validated list |
| Clear failure when impossible | "No valid model" response with reasons |
| Observability | SQLite logs + dashboard of cost, latency, failures |
| Live tweaking by judges | Control panel for latency and availability |

---

## 8. Risks and how we handle them

| Risk | Handling |
|---|---|
| Classifier is wrong | Treat as uncertain; low confidence → safer level; hard rules still protect |
| Failover wastes time budget | Re-check remaining latency before each attempt |
| Internet or Ollama fails at demo | Simulated models always work offline |
| Too much scope | Core (filter + failover + trace) first; UI and extras after |

---

## 9. Quick start (to fill in once built)
```bash
pip install -r requirements.txt
uvicorn gateway.main:app --reload      # start the router
streamlit run dashboard/app.py         # start the judge panel
pytest                                 # run rule tests
```
