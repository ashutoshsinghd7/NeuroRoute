# Decision trace example (shortened)
```json
{"status":"OK","chosen_model":"local-llama",
 "trace":{"complexity":{"level":1,"raw_level":1,"confidence":0.75,"low_confidence_routing":false,"min_capability_used":1},
  "considered":[{"model":"nano-1","valid":true,"est_cost":0.0025},
                {"model":"local-llama","valid":true,"est_cost":0.0}],
  "attempts":[{"model":"local-llama","status":"success","ms":1201}],
  "rejected":[],"cost":0.0,"total_latency_ms":1201}}
```
Read it as: every model was considered, each has a verdict, the winner was tried, and the cost and time are recorded.
