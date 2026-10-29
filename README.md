# Keeper offline evidence dashboard

Generate from available artifacts (read-only):

```bash
python dashboard/generate.py --keeper-db path/to/keeper.db --router-db path/to/router.db --eval-json eval/results.json --output dashboard/report.html
```

Missing inputs are displayed as unavailable/zero records, not fabricated metrics. `report.html` is self-contained and works offline. **Do not commit a dashboard generated from real personal inbox data** without review. No raw email contents, security destinations, or tokens are rendered. Router counts exclude Hermes-native traffic not logged by the router. No claim of 98.7% accuracy is shown unless supplied by actual evaluation artifacts.

This is a minimal Phase 11 dashboard skeleton. Remaining work: integrate the team's final eval JSON schema, replay, charts, and security proof after their sources are available.
