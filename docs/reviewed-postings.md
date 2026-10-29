# Reviewed radar opportunities

Radar stores discovery separately from a human's check of the primary application page. A reviewer reads the full requirements section, records only stated requirements, and copies the exact source excerpt and deadline. Keeper validates that input and compares it with the private profile. It does not fetch the page or claim the review happened automatically.

The delivery policy is a score of at least **7/10** and **confirmed eligibility**. Partial reviews and missing profile facts remain unverified; clear conflicts are ineligible. Both stay in history without entering Telegram. An empty qualifying set stays silent.

## Review a saved opportunity

Copy [the synthetic evidence example](../demo/reviewed_posting.json) to a private runtime file and replace its placeholder opportunity ID with an ID from `keeper radar run` output. For real use, replace every synthetic source field with evidence you checked on that opportunity's application page. The source URL must be its canonical URL or a recorded URL for the same opportunity.

Preview the local comparison, then save it explicitly:

```sh
keeper radar review /private/posting.json --review-id page-review-1 --reviewer vi
keeper radar review /private/posting.json --review-id page-review-1 --reviewer vi --apply
keeper radar reviews --opportunity OPPORTUNITY_ID
```

These commands use the existing database and `$KEEPER_DATA_DIR/memory/profile.md`; `--db PATH` and `--profile PATH` select alternatives. Review preview opens the product database read-only. Review `--apply` stores the evidence, reviewer, UTC review time, and eligibility locally. It does not contact Tavily, Nano, Telegram, or Calendar. A later radar run reuses the newest reviewed requirements and compares them with the current profile.

Evidence files have exactly these fields:

| Field | Meaning |
| --- | --- |
| `opportunity_id` | Existing Keeper opportunity ID. |
| `source_url` | Checked application URL recorded for that opportunity. |
| `source_excerpt` | Exact page excerpt, at most 8 KiB. |
| `requirements` | `complete` plus the explicitly stated supported requirements. |
| `deadline_timestamp` | Exact timezone-bearing ISO timestamp present in the excerpt, or `null`. |

`requirements.complete` is a required boolean assertion that the reviewer checked the whole requirements section. Supported optional fields are `graduation_years`, `majors`, `locations`, `remote_only`, `relocation_required`, and `authorization`. List fields must contain stated values; omitted or null fields make no claim of a restriction. Authorization supports only `us_citizen`, `us_work_authorization`, and `no_sponsorship`. Unknown conditions must remain a partial review. Do not interpret silence in a snippet as a checked, unrestricted page.

The UTF-8 JSON file is limited to 64 KiB. Duplicate keys, unsupported fields, malformed values, and embedded approval fields are rejected. ISO offsets are preserved in the source and normalized to UTC for scheduling. Date-only or ambiguous local phrases cannot schedule an event.

Review IDs are immutable. An exact retry keeps its original review time. Changed evidence, a changed eligibility decision, or a new reviewer needs a new review ID. Saving an old exact retry does not overwrite a newer review. Old daily observations remain unchanged.

## Approve the exact deadline

Inspect the proposed action without writing approval or calling Calendar:

```sh
keeper radar schedule page-review-1
```

Once the disposable Google runtime is configured, approve and synchronize it:

```sh
keeper radar schedule page-review-1 --live --apply --approve \
  --reviewer vi --reason 'Checked the full posting and its explicit deadline'
```

Only the latest review for that opportunity can be scheduled, and both the review and the current opportunity must be eligible. A future explicit deadline is required. The approval is committed before the provider call and bound to the opportunity, source URL, exact excerpt, and timestamp. Retry the same command with the same reviewer and reason after an outage; it keeps the saved approval and Calendar identity.

For a later deadline change, save a new page review and approve that review. The existing provider event is updated. An older approval cannot authorize the changed page. If the prior creation is pending with different evidence, the command reports `review` without replacing it. Calendar reconciliation keeps the approved evidence after a provider mapping was saved before the final source write completed.

`--secrets-dir DIR` selects `google-token.json` (default `/sandbox/keeper/secrets`). The code does not grant Google scopes or configure the sandbox. Real provider acceptance and delivery need a separate runtime smoke test.

Programmatic operator entry points use `schedule_reviewed_deadline`, which enforces saved-review freshness and durable approval. The lower-level Calendar helper accepts trusted source and approval objects; constructing those objects is the caller's responsibility. Daily radar's optional deadline hook only resumes an exact saved approval.

## Deliver newly qualified candidates

Saving a review never sends a message. Preview the next digest explicitly:

```sh
keeper radar digest --recipient CHAT_ID --run-id reviewed-digest-1
```

The preview copies the database into a temporary database and uses fake Telegram. It shows the same recipient's reservations without saving fake IDs or a snapshot to the product database. `TELEGRAM_CHAT_ID` can supply the recipient. To deliver once the private bot runtime is configured:

```sh
keeper radar digest --live --apply --recipient CHAT_ID --run-id reviewed-digest-1
```

An applied daily radar run can also deliver newly qualified candidates. Previously unverified postings may qualify after review, even if discovered on an earlier day. Each recipient receives a posting at most once through the reserved digest snapshots. Keep the same run ID for retries. The default identity uses the UTC date, so use an explicit new ID for a separate same-day batch after a prior digest was frozen.

Legacy or now-disqualified frozen snapshots return `review` without sending. Their reservations remain held because delivery may have been uncertain; operators must reconcile those separately. A new run ID does not release those reservations. This preserves the existing Telegram adapter's uncertain-delivery handling.

## Reproduce offline

```sh
python3 -m demo.reviewed_radar_lifecycle
python3 -m unittest tests.test_posting_review_cli tests.test_posting_calendar
```

The fixed-clock scenario uses synthetic evidence, fake search/scoring/Calendar/Telegram, and a temporary database. It demonstrates unverified discovery staying silent, eligibility review, one digest, approval, a freshly approved extension of the same event, rejected stale review, and unchanged retries. No runtime credentials are needed.
