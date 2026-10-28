# Keeper product record contracts (v1)

The Python definitions live in `keeper_core/contracts.py`. They are transport and storage independent: adapters normalize provider data into these records; persistence and workflow code consume them. These definitions do not create database tables or perform provider calls.

## Shared rules

- Every `id` is a stable, opaque string. Persist it and reuse it on retries; never generate a fresh ID just because an item was processed again. Namespace provider identities: an email can use `gmail:<message-id>`, and a provider event mapping can use `google-calendar:<event-id>`. Internal records use a deterministic key based on the source record and purpose. Do not put private message text in IDs.
- `Opportunity.dedupe_key` represents the same posting across radar runs. Build it from the normalized canonical URL plus normalized organization and title. Preserve the original record ID when a subsequent run matches this key. URL canonicalization and collision handling belong to the radar implementation; the string is opaque to consumers.
- `NotificationRecord.idempotency_key` names one intended delivery (channel, recipient, purpose, and run or source identity). Retrying delivery uses the same key.
- All `datetime` values are timezone aware UTC (`timezone.utc`) in Python. SQLite stores them as ISO 8601 text ending in `Z`, for example `2026-10-26T12:00:00.000000Z`. `keeper_core.time.to_storage` converts an aware provider timestamp to UTC; `from_storage` returns an aware UTC value. Both preserve unknown dates as `None`/SQL `NULL`. Naive timestamps are rejected. A date with no known time or an ambiguous date stays `NULL`, with the original wording in an evidence field; never assign midnight or a guessed timezone.
- Display dates with `keeper_core.time.for_display(value, timezone_name)`, passing the user's explicit IANA timezone (for example `America/Los_Angeles`). It includes the local offset and returns `Unknown` for `None`. Do not use the host machine's timezone implicitly.
- References ending in `_id` point to the corresponding Keeper record, except explicitly named `provider_*_id`. Missing optional relationships are `None`. Collection fields are immutable tuples; an empty tuple means no known items.
- Provider links and evidence references are pointers, not copied raw inbox contents. Do not log or commit sensitive message bodies, credentials, or user profile data.

## Email and classification

| Record | Fields and meaning |
| --- | --- |
| `IngestionMetadata` | `ingested_at`, stable processing `run_id`, and source such as `gmail-inbox` or `demo-fixture`. |
| `EmailRecord` | Stable `id`; `provider` and its `provider_id`; optional `thread_id`; `sender`, `subject`, UTC `received_at`, bounded `body_excerpt`, `labels`, optional `raw_link_reference`, and `ingestion`. A raw link is a provider locator, not message content. |
| `ModelMetadata` | `requested_model`, optional `served_model`, `prompt_version`, and optional `router_call_id`; keep requested and served identities distinct. |
| `TriageResult` | Stable `id` and `email_id`; `bucket` (`junk`, `actionable`, `due`, `newsletter`, or `None` for manual review); short `reason`; `career` (`None` if unknown); zero or more `scam_signals`; optional `extracted_deadline` and `deadline_evidence`; optional `confidence` in `[0, 1]`; optional `model`; UTC `classified_at`. Confidence is a model report, not a calibrated probability. A scam signal is a warning, not proof. |

Career related email must remain reviewable even if a model proposes junk. The triage implementation applies that guard after classification. These records capture the final result and evidence; they do not themselves execute that policy.

## Opportunity and application

| Record | Fields and meaning |
| --- | --- |
| `Opportunity` | Stable `id`, `dedupe_key`, `canonical_url`, `organization`, `title`, `source`, UTC `discovered_at`; optional 1–10 `score` and `explanation`; `eligibility` (`eligible`, `ineligible`, `unverified`) and optional `eligibility_reason`; optional UTC `deadline` and `deadline_evidence`. Unknown eligibility stays `unverified`. A search snippet alone does not confirm a deadline. |
| `ApplicationStatusChange` | Stable history entry `id`, `status`, UTC `changed_at`, optional `source_email_id`, `reason`, `manual` flag, source evidence, optional correction ID, and optional decision outcome. Every repository change retains source evidence. |
| `ManualOverride` | `actor_reference`, `reason`, and UTC `applied_at` for the latest manual intervention. The full series of changes remains in history. |
| `Application` | Stable `id`, `opportunity_id`, `status` (`found`, `applied`, `interview`, `decision`), ordered `status_history`, distinct `source_email_ids`, UTC `created_at` and `updated_at`, optional latest `manual_override`, and optional `decision_outcome` (for example offer or rejection). If history is present, the latest entry agrees with `status`. |

Status transitions, identity matching across emails, deduplication, and manual override precedence are workflow decisions for the tracker. Consumers should not infer an offer merely from `decision`.

## Side effects, security, and learning

| Record | Fields and meaning |
| --- | --- |
| `DeadlineEvent` | Stable Keeper `id`, `opportunity_id`, optional `application_id` and `source_email_id`, UTC `deadline_at`, source `evidence`, optional `provider` and `provider_event_id`, UTC `created_at` and `updated_at`. A changed deadline updates the existing mapping. |
| `DeadlineCandidate` | Stable `id`, source email ID and field, source link/reference, exact source evidence, raw date wording, optional UTC instant, and uncertainty flags. A candidate is not a Calendar event. |
| `DraftRecord` | Stable Keeper `id`, `email_id`, optional `application_id`, `provider`, optional `provider_draft_id`, `subject`, `body`, UTC `created_at` and `updated_at`, `requires_review`. This contract represents an unsent draft only; there is no send operation or sent status. |
| `NotificationRecord` | Stable Keeper `id`, `channel`, `recipient_reference`, `purpose`, `idempotency_key`, `status` (`pending`, `delivered`, `failed`), UTC `created_at`, optional `delivered_at`, `provider_message_id`, and `error`. |
| `SecurityEvent` | Stable `id`, UTC `occurred_at`, `category`, `outcome`, `evidence_reference`, optional `source_email_id` and `attempted_destination`. Behavioral injection handling and OpenShell network denials are separate event categories. A claim of blocked egress requires an actual audit reference. |
| `CorrectionRecord` | Stable `id`, `target_type` and `target_id`, user's `correction`, UTC `created_at`, `approval_status` (`proposed`, `approved`, `rejected`), optional `proposed_skill_name`, `approved_at`, and `actor_reference`. Only a user action can move a proposal to approved; email or model output cannot approve it. |

Provider event and draft IDs may be absent before creation. Keep the Keeper ID and retry using the same source identity; persist provider IDs as soon as a side effect succeeds.

## SQLite schema and bootstrap

The product database defaults to `data/keeper.db` (per the master plan). `KEEPER_DATA_DIR` changes its parent folder; `KEEPER_DB_PATH` or `--db PATH` overrides the complete path. The path `data/kepper/db` can be used explicitly with `--db` if that spelling is intentional. Runtime database files are ignored by Git.

Run `python3 -m keeper_core.db` or `python3 scripts/bootstrap_keeper_db.py` from the repository root. Both create the parent directory if needed, apply pending numbered files in `keeper_core/migrations/`, and print the resulting schema version. Rerunning them is safe: already applied migrations are skipped. Each migration and its `PRAGMA user_version` update run in one transaction; a failed migration rolls back. A database from a newer schema version is rejected. Back up a real database before migrating it to a future version.

The initial SQL file was corrected during development because early copies used `LIKE '*Z'` for several timestamp checks. A database bootstrapped from that early copy still has those checks; the migrator detects it and stops. Back it up and rebuild it from the corrected initial migration. There is no local product database in this checkout to upgrade. Preserve any real data before rebuilding.

Migration `001_initial.sql` stores every record type above. `application_status_changes` and `application_source_emails` hold the two application collections; the other nested metadata fields are stored alongside their parent. `labels_json` and `scam_signals_json` are JSON arrays. Required instants have `NOT NULL`; unknown deadline and optional event dates use SQL `NULL`. Timestamp checks require the `Z` suffix, while Python helpers validate and normalize the complete value before writes. Connections that write these tables must run `PRAGMA foreign_keys = ON`.

Uniqueness is enforced for `email_records.id`, `(provider, provider_id)` email mappings, `opportunities.canonical_url`, `opportunities.dedupe_key`, the provider mappings for drafts and deadline events, and notification idempotency keys. A repeated email or posting should resolve to its existing record, never receive a fresh ID. The schema establishes constraints; ingestion and retry workflows will implement that resolution.

Migration `006_radar_observations.sql` adds one append-only observation per opportunity and radar run. The opportunity row retains its original discovery identity and the latest score and eligibility; prior decisions and alternate observed URLs stay in observation history. `keeper_core.radar_store` resolves repeats by canonical URL or normalized organization/title, and stops on identity conflicts. See [radar discovery](radar-discovery.md) for the scoring rubric and eligibility evidence rules.

Migration `007_radar_digest_snapshots.sql` preserves the exact Telegram top-five digest per run and recipient. Its body and selected opportunity IDs are frozen before delivery so a retry cannot change content under the same idempotency key. Verified application-page deadlines use the existing `deadline_events` mapping and require explicit UTC-resolvable source evidence; search snippets never schedule events.

## Workflow rules and precedence

`keeper_core.workflow` implements the state and identity functions; `keeper_core.ingest` implements idempotent insertion for emails and opportunities. `keeper_core.applications` creates and reads applications, applies evidence-backed transitions, and resolves message identity. Automatic email callers use `apply_message_transition`; it returns a review resolution without changing state when identity is uncertain. The caller owns the SQLite transaction and must enable foreign keys on its connection.

| Current application status | Automatic next status | User correction |
| --- | --- | --- |
| `found` | `applied`, `interview`, `decision` | Any status with a correction ID, actor, and reason |
| `applied` | `interview`, `decision` | Any status with a correction ID, actor, and reason |
| `interview` | `decision` | Any status with a correction ID, actor, and reason |
| `decision` | No automatic change; conflicting outcomes require review | Any status with a correction ID, actor, and reason |

An automatic event needs a source email ID and a reason; a decision also needs its specific outcome. Repository writes additionally require nonblank source evidence and store it in `application_status_changes.source_evidence`. The initial `found` entry records the posting source. Replaying the same email and status is a no-op. Reusing an email or correction ID for a different status or decision outcome is a conflict. Older evidence cannot change newer state. A manual transition requires an approved, persisted `CorrectionRecord` targeted at that application, with an actor and approval time; proposals cannot change state. A manual correction takes precedence over later automatic transitions until another explicit user action; the correction remains in history. A second email confirming the current status adds an evidence-bearing history entry and is attached to `source_email_ids`. The repository uses a savepoint so a failed multi-table update leaves the application unchanged. `resolve_message_application` compares normalized organization and title keys; missing titles, unknown titles at a tracked organization, and multiple matching postings require review before a transition. These rules do not infer identity from subject lines alone.

Triage precedence: validate the model's proposed bucket, then apply the deterministic career guard. Career mail proposed as `junk` becomes `actionable`; if career relevance is unknown, proposed `junk` becomes manual review (`None`). Scam signals remain visible alongside the bucket and never establish a proven scam. A user approved correction outranks an automatic proposal. A verified explicit deadline from the source email outranks an older verified date; ambiguous wording or a search snippet cannot replace a verified event. Updating a deadline uses the original event mapping and provider ID.

`stable_key(namespace, *parts)` hashes unambiguous components and returns a repeatable opaque ID. Use `email:<provider>:<provider-id>` or the existing `EmailRecord.id` for an ingestion identity, `opportunity_key(canonical_url, organization, title)` for radar deduplication, `application-change:<application-id>:<email-or-correction-id>` through the workflow helper for history, `deadline:<opportunity-id>:<purpose>` for a calendar event, `draft:<email-id>:<purpose>` for an unsent draft, and `notification:<channel>:<recipient>:<run-or-source>:<purpose>` for a delivery. Stable IDs are assigned once and reused across retries; use a separate key for an intentionally new delivery. URL canonicalization lowercases the host, removes fragments, default ports, trailing slash, and common tracking parameters, and sorts remaining query parameters. The canonical URL and dedupe key are both unique in SQLite. If one points to an existing opportunity, preserve that opportunity's ID; if they point to different rows, stop with `IdentityConflict` for review.

`insert_email_once` resolves `(provider, provider_id)` and Keeper `id` together. A repeat returns the existing ID without changing the original ingestion metadata. A mismatched mapping raises `IdentityConflict`. Persist side effects before reporting them as complete, and recover by looking up the provider ID after an uncertain timeout; a fake's idempotency behavior is not a guarantee about an external provider.

`keeper_core.application_messages.process_application_email` consumes an already persisted `EmailRecord` and verifies that its source fields match the stored email. It recognizes explicit confirmation, interview, rejection, offer, withdrawal, and closure wording. It matches normalized organization and title mentions against tracked opportunities, then uses `apply_message_transition` for the durable update. A message with conflicting decision signals, uncertain identity, changed source content, or a transition that contradicts the current state returns `review` without changing an application. A message without a lifecycle signal returns `ignored`; replay returns `reused`. The recorded decision outcome is `offered`, `rejected`, `withdrawn`, or `closed`.

`keeper_core.deadlines.extract_deadline_candidates` scans career-relevant, non-junk email for deadline cues and records source-backed ISO or natural-language date candidates. Explicit numeric offsets and valid named IANA zones resolve to UTC; date-only wording, missing or vague zones, DST ambiguity, nonexistent local times, invalid dates, multiple dates, uncertain relevance, and past instants carry flags. `validated_deadline_at` returns a calendar-ready instant only for an unflagged future candidate. Any other candidate requires a `DeadlineReviewPolicy` bound to its candidate ID, an approver, reason, approval time, and confirmed UTC instant; scheduling a past instant additionally requires `allow_past=True`. The live Calendar adapter also rejects past instants without a matching past-date review policy before any provider request. Candidate extraction and validation do not call Calendar or Gmail.

Migration `005_application_activity.sql` adds append-only Calendar and draft activity linked to applications, source emails, evidence, and provider IDs. It also adds draft review flags. Application status changes remain in their own transition history. The application CLI history response shows both collections; saved triage runs show per-email activity lines and calendar/draft counts.

## Provider interfaces and offline fakes

`keeper_core.adapters` defines `GmailAdapter`, `CalendarAdapter`, `TelegramAdapter`, `TavilyAdapter`, and `ModelAdapter` as Python protocols. The data shapes include `EmailPage`, `ReadEmail`, `CalendarEventPage`, `SearchHit`, and `ModelAnswer`. Gmail provides paginated inbox reads, full message reads, label changes, and draft creation; it has no send method. Calendar provides paginated search, deadline creation, and updates against a stable event key and stored provider ID. Telegram sends a summary against a delivery key. Tavily returns bounded search hits. Model calls return content and requested/served model metadata; downstream code validates that content before treating it as a decision.

All adapters raise `AdapterError` subclasses: `AuthenticationError` requires operator attention; `RateLimited` supplies an optional retry delay; `ProviderUnavailable` allows bounded retry; `ProviderNotFound` means the requested object or fixture is absent; `InvalidProviderResponse` means the reply did not satisfy the contract; `IdempotencyConflict` means a retry key was reused for a different target or payload. Do not silently turn provider failures into empty success results. Live implementations are described in [Gmail adapter](gmail-adapter.md), [Calendar adapter](calendar-adapter.md), and [Telegram and Tavily adapters](telegram-tavily-adapters.md). The model provider still uses an offline fake.

`keeper_core.fakes.load_fakes` reads [synthetic adapter responses](../demo/fixtures/adapter_responses.json) and returns all five fake adapters in interface order. The fakes support repeatable pagination, draft/event/message IDs, deadline updates, bounded search, and model responses without network access. [Example records](examples/keeper_records.json) show the complete cross-module JSON shape, including explicit `null` deadlines. They are synthetic examples; timestamps and links carry no live account data.
