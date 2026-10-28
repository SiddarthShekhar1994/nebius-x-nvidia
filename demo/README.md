# demo

Material for the demo video. That means the shot list, a sample `profile.md`, and seed emails for the throwaway demo Gmail account. Nothing here comes from a real inbox or a real person.

Run `python3 -m demo.application_lifecycle` from the repository root for a
deterministic, offline tracker scenario. It creates a temporary product database,
processes a confirmation, interview invitation, and rejection, replays each
email, then prints one application's complete evidence-backed history as JSON.
It does not use credentials or change `data/keeper.db`.
