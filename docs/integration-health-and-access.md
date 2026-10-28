# Integration health and gateway access

## Run the checks

From WSL after deploying Keeper, run these inside the sandbox:

```bash
nemohermes keeper exec -- /sandbox/keeper/bin/keeper health adapters
nemohermes keeper exec -- /sandbox/keeper/bin/keeper health permissions
```

`adapters` checks that credential files are private, the Google token declares both required scopes, and the Calendar and Telegram SQLite tables are readable. It makes no network calls. `permissions` prints its effective UID and checks readable gateway configuration, the read-only Hermes session database needed by `/teach`, read-only Keeper product database access for `/apps`, temporary writes in Keeper's data and learned-skill directories, and write permission on the SOUL files needed by `/keep`. It reads but never prints secrets, and never changes an existing SOUL or skill file. It fails if relevant directories or files are group/world writable, or if secrets are group/world readable. Compare the printed UID with the gateway process's UID; an `exec` launched as a different user cannot prove gateway access. You can supply `--expected-uid N` to make a mismatch fail.

To test API credentials and egress, opt in to live probes:

```bash
nemohermes keeper exec -- /sandbox/keeper/bin/keeper health adapters --live
```

Gmail calls `users.getProfile`, Calendar lists one event page, Telegram calls `getMe`, and Tavily makes one bounded `POST /search` request. None sends mail or a Telegram message, or creates a Calendar event. The Tavily check consumes a search request. A failed provider is reported by error class only so that tokens and response bodies do not appear in terminal output. `--provider gmail` (or `calendar`, `telegram`, `tavily`) narrows a run. The existing `keeper router smoke` command separately checks Token Factory model access and makes model calls.

The gateway probe must run as the same effective user as Hermes. `/teach` reads the latest correction in `runtime/state.db` and writes a pending draft under `/sandbox/keeper/data/skill-drafts`; `/keep` writes a learned `SKILL.md` under `/sandbox/.hermes/skills/keeper` and updates SOUL files. The `keeper` launcher sets `HERMES_HOME`, selects the runtime session database when present, and uses umask `077` for newly created draft and skill files. Keep these paths owner writable; do not use `chmod 777` to make a failed probe pass. If a path is owned by a different user, correct ownership for the gateway account and rerun the probe. Existing files with broad permissions require an explicit, reviewed correction.

## Access inventory

| Service | Credential or scope | Runtime host and operation |
| --- | --- | --- |
| Gmail | Google OAuth `gmail.modify` | `gmail.googleapis.com`: profile and message reads, label edits, draft creation. The scope also permits send even though Keeper exposes no send action. [Google scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes). |
| Calendar | Google OAuth `calendar.events` | `www.googleapis.com`: event list, get, insert, patch. [Calendar scope reference](https://developers.google.com/workspace/calendar/api/auth). |
| Google OAuth refresh | Desktop OAuth refresh token | `oauth2.googleapis.com`: token refresh; Google client discovery may also contact `www.googleapis.com`. [Google OAuth documentation](https://developers.google.com/identity/protocols/oauth2/web-server). |
| Telegram | BotFather bot token; NemoClaw channel user allowlist | `api.telegram.org`: gateway traffic and Keeper `getMe`/`sendMessage`. [Bot API](https://core.telegram.org/bots/api), [NemoClaw channel setup](https://docs.nvidia.com/nemoclaw/user-guide/hermes/manage-sandboxes/messaging-channels/set-up-telegram). |
| Tavily | Tavily API key | `api.tavily.com`: `POST /search`. [Tavily search reference](https://docs.tavily.com/documentation/api-reference/endpoint/search). |
| Token Factory | Nebius API key or OpenShell provider credential | `api.tokenfactory.us-central1.nebius.com`: `GET /v1/models`, `POST /v1/chat/completions` in this repo's policy. |

Review OpenShell's live policy before enabling traffic. NemoClaw's `telegram` and `tavily` presets cover their maintained gateway integrations, but a preset may not authorize Keeper's direct Python adapter binary. A blocked live health check calls for a scoped policy review of the exact host, method, path, and binary. Do not enable unrestricted egress or broaden file permissions to work around a denial. [NemoClaw policy guidance](https://docs.nvidia.com/nemoclaw/user-guide/hermes/network-policy/integration-policy-examples) explains the preset preview and removal commands.

## Revoke access

1. **Google:** Remove the demo app under Google Account third-party connections or revoke its OAuth token, then remove the sandbox token copy. Google states that revocation invalidates project grants. [Google account access](https://support.google.com/accounts/answer/13533235), [OAuth revocation](https://developers.google.com/identity/protocols/oauth2/web-server).
2. **Telegram:** Revoke the bot token through @BotFather, remove the NemoClaw Telegram channel from the WSL host with `nemohermes keeper channels remove telegram`, and remove the separate `telegram-bot.key` copy used by Keeper. [Telegram bot tutorial](https://core.telegram.org/bots/tutorial), [NemoClaw channel removal](https://docs.nvidia.com/nemoclaw/user-guide/hermes/manage-sandboxes/messaging-channels/manage-messaging-channels).
3. **Tavily:** Delete the key from the Tavily account's API Keys page, remove the sandbox `tavily.key` copy, and remove the `tavily` policy preset if unused. Deleted keys stop working. [Tavily key removal](https://help.tavily.com/articles/4695639825-how-can-i-delete-my-api-key).
4. **Token Factory:** Rotate or revoke the project key in the account that issued it, detach its OpenShell provider or remove the fallback key file, and remove the Token Factory policy if the router is retired. Confirm the account's current key management UI before acting.

## Known setup failure risks

- No `nemohermes` binary, bot token, or API keys are available in this checkout. Offline tests and local permission simulations do not establish that the live gateway is configured or reachable.
- NemoClaw rebuilds generate channel configuration. In-sandbox manual edits may disappear on rebuild; reinstall Keeper code or credentials if a rebuild replaced them.
- Google desktop OAuth testing grants may expire, refresh may fail, and `gmail.modify` carries broader authority than Keeper's draft-only code path. Health checks do not prove that the network policy blocks Gmail send endpoints.
- A Telegram timeout or 5xx after `sendMessage` has an unknown delivery outcome. Keeper leaves that notification pending for reconciliation rather than retrying blindly.
- Tavily search consumes credits and can time out. A valid key with a denied egress path fails the live probe; a successful local check proves only the file and schema are present.
- A permission probe run under a different UID, especially root, cannot prove the Hermes gateway user can make the same writes. Resolve ownership only for the documented paths and rerun as the real gateway UID.
