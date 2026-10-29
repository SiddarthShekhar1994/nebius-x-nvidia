# dashboard

A self-contained, read-only HTML evidence report built from Keeper's own databases: triage buckets, opportunities, application stages and history, router calls with tokens, latency and cost per model, evaluation results, and sanitized security events.

Generate it from the repository root:

```bash
python dashboard/generate.py --keeper-db data/keeper.db --router-db data/router.db --eval-json data/eval.json --output dashboard/report.html
```

`--eval-json` takes the output of `python -m triage compare`. Missing inputs are shown as unavailable, never as invented numbers. The report works offline. No raw email contents, security destinations or tokens are rendered, and router counts exclude Hermes-native traffic that the router doesn't log.

Don't commit a report generated from real personal inbox data without review; `dashboard/report.html` is ignored by git.

Remaining work: Gmail drafts and calendar events, the per-bucket, scam and deadline scores from `triage compare`, charts, replay, and the security proof once its sources exist.
