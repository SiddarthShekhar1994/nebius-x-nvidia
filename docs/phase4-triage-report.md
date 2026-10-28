# Phase 4: triage implementation and acceptance

Implementation covers the Phase 4 requirements in the master development plan.
Offline acceptance is complete. Live Nebius, demo Gmail, and deployed sandbox
acceptance are pending; no live quality or egress claims are made.

## Architecture

triage/sources.py allows synthetic fixtures or the Phase 3 seed manifest.
Gmail mode verifies the operator-specified demo account against the actual
profile and each message against its synthetic Message-ID. The classifier uses
the full bounded body and the existing Nano router, then applies deterministic
career protection and warning indicators.

triage/store.py saves canonical emails and immutable TriageResult records in one
SQLite transaction before label changes. Migration 003 binds a product database
to one demo account without replacing existing data. Repeat runs reuse saved
decisions; Gmail's additive label operation reconciles actual provider state.
No outbox or duplicated product tables are introduced.

Transient inference failures remain retryable. Conflicting content/accounts and
approved corrections require reconciliation. Errors contain classes only, not
provider bodies or credentials. Partial progress remains visible on fatal errors.
The scorer sees gold answers only after the predictions exist.

## Phase 4 checklist

| Requirement | Implementation | Offline evidence | Live acceptance |
| --- | --- | --- | --- |
| Constrained Nano classification and reasons | classifier.py, model.py | real router with injected client | Pending |
| JSON validation, bounded repair, review fallback | parsing.py, classifier.py | malformed types, retry, timeout tests | Pending |
| Deterministic career protection | rules.py, existing workflow helper | false/unknown model overrides | Pending |
| Scam warning indicators | rules.py | fee, sender, urgency, pay tests | Pending |
| Timezone-aware dates with evidence | parsing.py | explicit source timestamps; ambiguous stays null | Pending |
| Safe labels and persistent results | run.py, store.py, Gmail adapter | real temporary SQLite, fake Gmail operations | Pending |
| Requested/served model metadata | model.py, store.py | router logging and persistence round-trip | Pending |
| Synthetic and adversarial cases | test_triage*.py | bounded bodies, no gold leakage, errors | Pending |
| Dry run and explicit apply | __main__.py, keeper launcher | subprocess, no-write, restart tests | Pending |
| Compare actual versus expected | evaluate.py | denominator, missingness, mapped IDs, full fixture | Pending |

Deadline parsing intentionally leaves natural-language or timezone-ambiguous
dates unconfirmed. Scam indicators are warnings, not proof. Prompt instructions
and no-tool classification do not substitute for Phase 9's actual deny evidence.

## Offline validation

Current results: all 200 repository tests pass. The 76 focused triage tests
achieve 96% coverage with branch measurement enabled. Both modified shell
scripts pass syntax validation. RED/GREEN details are in
testing/triage-classifier.tdd.md.

Run the complete suite:

    .venv/bin/python -m unittest discover -s tests -q

Triage coverage:

    .venv/bin/python -m coverage run --branch --source=triage -m unittest tests.test_triage tests.test_triage_preview tests.test_triage_run tests.test_triage_credentials tests.test_triage_evaluate
    .venv/bin/python -m coverage report --show-missing --fail-under=80

Tests cover all 75 fixture records through save, replay, and comparison; additive
Gmail apply and uncertain failure recovery; wrong accounts/Message-IDs; existing
Phase 3 ingestion; version 2 database upgrade; approved corrections; private
runtime token validation; partial fatal reports; and the real shell launcher.
No live provider calls occur in these tests.

## First live finding

The first live preview (5 emails, Nano) returned review for every email. The model
answered correctly but sent scam_signals as a comma-separated string, which the
strict parser rejected, and one email likely ran out of tokens while reasoning.
Prompt triage-v2 asks for a JSON array and names the failed check on retry, the
parser accepts the string form, and max_tokens is 4096. See NOTES_FEEDBACK.md note 26.

## Live acceptance for the teammate

Use triage/README.md's exact commands, with a disposable seeded Gmail account,
its Phase 3 seed manifest, private runtime OAuth token, and a Token Factory key.

1. Check out the PR branch, install requirements, and run the full offline suite.
2. Back up the development database. Keep offline and live acceptance DBs separate.
3. Run the verified Gmail dry run on all 75 messages. Inspect reasons, review
   cases, error counts, source evidence, model names, and router usage.
4. Run explicit apply. Confirm only additive KEEPER_* labels in the demo inbox,
   all original INBOX messages retained, and no emails sent.
5. Repeat apply using the same DB. Confirm reused=75, zero failed/unprocessed,
   unchanged record counts, no duplicate label effects, and no additional model
   calls for saved decisions.
6. Compare the report against the fixture. Record accuracy by bucket, review and
   failure counts, career-to-junk count, warning precision/recall, deadline
   mismatches, missing coverage, and all mismatch IDs. Investigate every
   career-to-junk error and any missing/failed message.
7. Have the sandbox owner deploy the branch and repeat using keeper triage run.
   Confirm permissions and network policy with the existing integration checks.

Record real command output with credentials removed. Tests with injected
clients are not evidence that the real model, Gmail scopes, or sandbox egress
works. Mark Phase 4 fully accepted only after these live steps pass and the team
reviews any classification misses.
