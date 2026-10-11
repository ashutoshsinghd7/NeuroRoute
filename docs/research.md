# Research notes
- LiteLLM: one interface to many models, with fallbacks and cooldowns. Not constraint-first. Optional call layer later.
- RouteLLM: a classifier chooses strong vs weak model. We borrow the idea (classify, then route).
- Portkey / Helicone: logging dashboards. We borrow logging + dashboard.
- Ollama: free local models. Optional "local-llama" real call (commented in `simulated.py`).
Our own part: constraint-first filter, safe failover, honest refusal, full trace.
