# Repo Structure (GitHub)

Rule: **src** = code, **docs** = explanations, **markdown files** = project guides at the root.

```
neuroroute/
├── README.md              # Problem for non-experts, existing solutions, approach, flowchart, quick start
├── CONTEXT.md             # Scope lock: what we build / do not build
├── CHECKPOINTS.md         # Pipeline broken into build checkpoints (CP0–CP11)
├── AGENT.md               # Instructions for AI coding assistants working on this repo
├── requirements.txt       # fastapi, uvicorn, pydantic, scikit-learn, streamlit, httpx, pytest
├── .gitignore             # __pycache__/, *.db, .venv/
│
├── src/
│   ├── gateway/                   # The API + decision engine (CP1, CP2, CP4, CP5, CP7, CP8)
│   │   ├── main.py                # FastAPI app, POST /route
│   │   ├── schemas.py             # Pydantic request / response / trace models
│   │   ├── tokens.py              # token + cost estimate
│   │   ├── filter.py              # HARD filter, returns valid + rejected with reasons
│   │   ├── ranking.py             # cheapest / fastest / highest_quality
│   │   ├── failover.py            # call loop, timeouts, remaining-budget check
│   │   └── logger.py              # SQLite logging (CP9)
│   │
│   ├── models/                    # Fictional models (CP0, CP6)
│   │   ├── registry.json          # model catalogue the judges can edit
│   │   └── simulated.py           # fake model calls: delay, failure rate, up/down
│   │
│   ├── classifier/                # The AI part (CP3)
│   │   ├── prompts.csv            # ~60 hand-written fictional prompts + level 1–5
│   │   ├── train.py               # trains TF-IDF + Logistic Regression
│   │   ├── predict.py             # returns level + confidence + safer-option rule
│   │   └── model.joblib           # saved trained model (generated)
│   │
│   └── dashboard/                 # Judge control panel (CP10)
│       └── app.py                 # Streamlit app
│
├── tests/                         # Proves the rules (CP11)
│   ├── test_filter.py
│   ├── test_ranking.py
│   ├── test_failover.py
│   └── test_refusal.py
│
└── docs/
    ├── architecture.md            # Flowchart + how the parts connect
    ├── scenarios.md               # 8 demo/test scenarios with expected results
    ├── decision-trace-example.md  # Sample JSON trace explained
    └── demo-script.md             # 2-minute judge walk-through
```

## Notes
- `tests/` is a small extra folder. It is kept because the judging criteria ask for checkable rules, and pytest is the evidence.
- Everything runs locally: `uvicorn src.gateway.main:app` and `streamlit run src/dashboard/app.py`.
- Keep `data/` and generated files (`*.db`, `model.joblib`) out of git unless needed for the demo.
