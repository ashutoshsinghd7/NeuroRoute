# 8 test / demo scenarios
| # | Scenario | How | Expected |
|---|---|---|---|
| 1 | Simple question, cheapest | Preset "Simple question" | Cheapest valid model (local-llama, ₹0) |
| 2 | Long contract | Preset "Long contract" | Small-context models rejected with "context X < Y" |
| 3 | Primary down | Preset "Primary model down", or click "Take down" | Next valid model answers |
| 4 | Latency raised | Drag a model's latency slider above the budget, send | Rejected "latency X > Y" |
| 5 | Backup breaks cost | Lower Max cost, set winner failure rate to 100% | Costly backups never tried |
| 6 | Impossible | Preset "Impossible" | `NO_VALID_MODEL`, no attempts |
| 7 | Vague prompt | Preset "Vague prompt" | "low-confidence routing" flag, level raised |
| 8 | Same request twice | Send twice | Same decision (seeded) |
