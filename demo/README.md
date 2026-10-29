# demo

Material for the demo video. That means the shot list, a sample `profile.md`, and seed emails for the throwaway demo Gmail account. Nothing here comes from a real inbox or a real person.

Run `python3 -m demo.application_lifecycle` from the repository root for a
deterministic, offline tracker scenario. It creates a temporary product database,
processes a confirmation, interview invitation, and rejection, replays each
email, then prints one application's complete evidence-backed history as JSON.
It does not use credentials or change `data/keeper.db`.

Run `python3 -m demo.calendar_lifecycle` for a deterministic, offline-synthetic
Calendar scenario. Synthetic Example Inc emails create and explicitly extend one
application deadline using the fake Calendar and a temporary database; the JSON
trace includes replay results and persisted source evidence. It makes no live
provider or account claims.

Run `python3 -m demo.radar_lifecycle` for the deterministic, offline-synthetic
radar discovery scenario. One synthetic posting is found and scored with a fake
model. Eligibility stays unverified, so retries and later daily observations
produce no Telegram messages or Calendar events. The temporary database is removed afterward.

Run `python3 -m demo.reviewed_radar_lifecycle` for the full reviewed path. The
synthetic primary-page evidence in `reviewed_posting.json` confirms eligibility.
Explicit approval creates one fake Calendar event, then fresh approval updates
that event for an extension. One digest is delivered to fake Telegram; retries
and the next day keep that single message. Old review approval is rejected.
All data and adapters remain synthetic and temporary.

`python3 -m keeper_core.radar_cli run` previews the same synthetic search through
the command entry point. See [radar commands and live gates](../docs/radar-discovery.md#run-the-command).
See [reviewed evidence and approval commands](../docs/reviewed-postings.md) for
the human review file format and separate local-write and live-provider gates.
