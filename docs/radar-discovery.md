# Radar discovery stage

`keeper_core.radar` loads the private Markdown profile described in [the profile contract](profile.md), builds at most six Tavily queries by default, and requests at most five results per query. Queries use only roles, opportunity types, preferred topics, and one broad location. They omit education and work authorization. User text is reduced to words before it enters a query.

When both target roles and opportunity types are unspecified, radar searches up to two stated interests with the same sanitization and query limits. It does not invent a role or opportunity type. A profile with no roles, types, or usable interests cannot produce a search.

## Run the command

From the checkout, run the synthetic preview without credentials or network access:

```sh
python3 -m keeper_core.radar_cli run
```

After deployment, the same entry point is `keeper radar run`. It uses the synthetic `demo/profile.md`, fake Tavily and model responses, a fake Telegram adapter, and a temporary database. Output labels scoring as `simulated`. The candidate has unverified eligibility, so its digest is `empty`, with no message ID. Even an explicit `--db` is untouched in this mode. Run `python3 -m demo.radar_lifecycle` to also exercise a same-run retry and next-day deduplication, or `python3 -m demo.reviewed_radar_lifecycle` for the complete reviewed-and-approved path.

When the private runtime is ready, these are separate live operations:

```sh
keeper radar run --live
keeper radar run --live --apply
```

`--live` reads `$KEEPER_DATA_DIR/memory/profile.md` (`data/memory/profile.md` in the checkout), or `--profile PATH`. It calls Tavily and Nemotron Nano, so it consumes provider usage and may write the router's usage log. It copies the existing Keeper database read-only into a temporary database to preserve discovery history while previewing; it never sends a real Telegram message or writes the product database.

`--live --apply` saves results to the Keeper database and delivers through the live Telegram adapter. It requires `--recipient CHAT_ID` or `TELEGRAM_CHAT_ID`. `--db PATH` selects the database, otherwise the existing `KEEPER_DB_PATH` / `KEEPER_DATA_DIR` defaults apply. `--secrets-dir DIR` selects `tavily.key` and `telegram-bot.key` (default `/sandbox/keeper/secrets`); Nano uses the existing [router configuration](../router/README.md). Missing credentials fail before searching. No credentials or recipient IDs are printed.

The default run ID is `radar:YYYY-MM-DD` in UTC. Keep the same `--run-id ID` when retrying a run. Query budgets default to six searches with five results each, and accept `--max-queries 1..12` / `--results-per-query 1..20`. The top-five digest requires a score of at least 7 and confirmed eligibility. Discovery alone normally leaves eligibility unverified. Use the [review, approval, and digest commands](reviewed-postings.md) to check primary-page evidence before delivery or Calendar creation. Offline tests cover the live branches with fakes; real provider delivery still needs a runtime smoke test.

`search_opportunities(profile, tavily, discovered_at=utc_time)` returns the exact queries, normalized `Opportunity` records, and a reason for every skipped hit. It prioritizes known application systems and employer career pages, accepts `.edu` and `.gov` opportunity sources, drops known aggregator and unverified page links, removes URL tracking parameters, and deduplicates by canonical URL and normalized organization/title within the run. A result needs an identifiable role and organization in its title; the radar does not invent either from a URL slug. The `source` field is the canonical URL's host, and the discovery time is the supplied UTC run time.

The filter skips explicit closed notices, senior roles for a student profile, clear role mismatches, and explicit profile exclusions. Search snippets are not eligibility evidence. `run_radar` accepts separately verified application-page requirements; without them, eligibility stays `unverified`.

## Nano relevance score

`run_radar` sends each accepted result to `RadarRouterModel`, which routes the `radar` task to Nemotron Nano. The prompt contains target roles, opportunity types, preferred topics, title, organization, and a bounded snippet. It excludes graduation year, major, and work authorization. The model must return an integer score and an exact phrase from the title or snippet. Keeper validates the phrase and builds a one-line explanation from the model's short reason and that phrase. Invalid or non-Nano responses produce a skip reason; no unscored result is silently promoted.

| Score | Meaning |
| --- | --- |
| 1–2 | Unrelated to the profile. |
| 3–4 | Weak type or topic match. |
| 5–6 | Plausible role and type match. |
| 7–8 | Direct target role and type match. |
| 9–10 | Direct role and type match plus a preferred topic. |

## Eligibility and history

`VerifiedPostingRequirements` carries a primary application URL, source evidence, explicitly stated requirements, and whether the full requirements section was checked. `check_eligibility` compares only those requirements with profile values. A demonstrated conflict is `ineligible`; a missing profile value, ambiguous match, or partially checked page is `unverified`. `eligible` requires a complete checked requirements section and no unresolved condition. Work authorization is compared only when the user voluntarily supplied a statement; the matcher recognizes only a few explicit phrases and leaves everything else unverified. The caller is responsible for verifying the page and supplying this typed evidence; the current Tavily adapter supplies search snippets only.

`run_radar` saves results through migration `006_radar_observations.sql`. The first `opportunities` row keeps its original ID, URL, and discovery time; its score and eligibility reflect the latest observation or a later human page review. Each daily `run_id` adds an immutable observation with the URL seen on that run, score, explanation, and eligibility decision. Canonical URL or normalized organization/title resolves a repeat to the original row. A retry with the same run ID preserves its first observation; conflicting identity stops the batch and rolls back its writes. `opportunity_history` reads the ordered observations. The caller owns the outer SQLite commit. Daily runs reuse the latest saved page requirements and compare them with the current profile, while historical observations stay unchanged.

## Daily delivery and deadlines

`keeper_core.radar_delivery.run_daily_radar` migrates the product database, runs bounded search and Nano scoring, commits opportunities and observations, then sends a Telegram digest of up to five confirmed-eligible candidates scoring at least 7. Each line includes organization, role, score, eligibility label, one-line source-backed reason, and the application link. A previously unverified candidate can enter a future digest once it qualifies. Snapshots reserve each selected posting for that recipient before delivery; later runs do not resend it. Empty runs send no message. Migrations `007_radar_digest_snapshots.sql` and `009_radar_digest_policy.sql` freeze the text, selected IDs, and policy version so retries use the same idempotency key and body. Legacy snapshots or candidates that no longer qualify return `review` without delivery and retain their reservations. The live Telegram adapter records delivery separately and holds uncertain outcomes for reconciliation.

`VerifiedPostingDeadline` requires an explicit ISO timestamp with a timezone, the exact timestamp in the checked application-page excerpt, and a UTC instant matching that timestamp. `sync_posting_deadline` also requires the page URL to match the opportunity or one of its recorded URLs, then uses the existing `deadline:<opportunity-id>:application-deadline` Calendar identity. It commits a pending event before the provider call and stores the provider ID after success. A repeat is unchanged; a changed date returns `review` unless an exact revision-bound approval is supplied. The operator-facing `schedule` command requires the latest saved review, confirmed eligibility, and an explicit approval. A newly approved date updates the same provider event. A search snippet, date-only phrase, missing timezone, or past instant cannot create a Calendar event. The caller supplies checked application-page evidence; Tavily search does not verify page contents.
