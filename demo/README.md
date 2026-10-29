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
radar scenario. One synthetic posting is found, scored with a fake model, and
delivered to fake Telegram. A same-run retry keeps one message; a second daily
run adds history without resending it. Eligibility stays unverified, and search
snippets create no Calendar events. The temporary database is removed afterward.

`python3 -m keeper_core.radar_cli run` previews the same synthetic search through
the command entry point. See [radar commands and live gates](../docs/radar-discovery.md#run-the-command).
