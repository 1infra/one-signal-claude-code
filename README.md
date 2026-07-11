# One Signal for Claude Code

Trace every Claude Code session — turns, generations, tool calls, and token
usage/cost — to your **One Infra** organization, with zero Langfuse
credentials on your machine.

This plugin is a One Infra fork of Langfuse's official
[Claude-Observability-Plugin](https://github.com/langfuse/Claude-Observability-Plugin)
(MIT License). The transcript-parsing and turn-assembly logic is unchanged;
the only architectural difference is the transport: instead of shipping
trace data straight to Langfuse using Langfuse API keys, this plugin ships
it to your organization's **One Connector** ingest proxy using a One
Connector access token. One Connector holds the organization's real
Langfuse project credentials server-side and forwards/attributes the data —
you and your teammates never see or handle a Langfuse key.

## Install

```bash
claude plugin marketplace add 1infra/1Infra
claude plugin install one-signal@one-infra --config ONE_SIGNAL_API_TOKEN=<token>
```

Restart Claude Code after install. `claude plugin install` does not prompt
for configuration interactively — pass it via `--config` at install time as
above (see "Getting a token" below for `<token>`), or set/change any option
later with `/plugin configure one-signal@one-infra` in Claude Code.

Available options:

| Field | Description |
| --- | --- |
| `ONE_SIGNAL_BASE_URL` | Your One Connector deployment URL. Default `https://connector.1infra.io`. The hook POSTs to `<this>/api/v1/observe/ingest`. |
| `ONE_SIGNAL_API_TOKEN` | Your One Connector access token (`oc_...`). Stored in your OS keychain. See "Getting a token" below. |
| `ONE_SIGNAL_USER_ID` | Optional. User identifier attached to every trace (shown as the user in One Signal). |
| `CC_ONE_SIGNAL_DEBUG` | Verbose logging to `~/.claude/state/one_signal_hook.log`. Default `false`. |
| `CC_ONE_SIGNAL_MAX_CHARS` | Truncate captured inputs/outputs to this many characters. Default `20000`. |
| `CC_ONE_SIGNAL_SKILL_TAGS` | Tag traces with `skill:<name>` for every skill invoked in the turn. Default `true`. |
| `CC_ONE_SIGNAL_CAPTURE_SKILL_CONTENT` | Include injected skill instruction text in the Skill tool span output. Default `false`. |

## Getting a token

1. Open Console → **Access tokens**.
2. Create a new token (`oc_...`). Give it a name you'll recognize later, e.g. "laptop — Claude Code".
3. Use it as `<token>` in the install command above (or re-run
   `/plugin configure one-signal@one-infra` in Claude Code to set
   `ONE_SIGNAL_API_TOKEN` later).

The token is capped by your own organization permissions; it is not a
Langfuse key and cannot be used to call Langfuse directly.

## Requirements

Python 3.10+ as `python3`, or [uv](https://docs.astral.sh/uv/) (used automatically if present). No third-party
packages are required — the hook uses only the Python standard library
(unlike the upstream Langfuse plugin, which needs the `langfuse` SDK).

If neither `uv` nor `python3` is set up, the hook exits silently — no impact
on Claude Code.

## How it works

A hook reads the session transcript incrementally on every turn (`Stop`)
and at session end (`SessionEnd`), and builds one trace per turn: a root
span ("Turn N"), one nested generation per assistant message, and nested
tool-call spans under the generation that issued them. Token usage is
captured when present. The resulting batch is POSTed as JSON to
`<ONE_SIGNAL_BASE_URL>/api/v1/observe/ingest` with
`Authorization: Bearer <ONE_SIGNAL_API_TOKEN>`; large batches are split into
multiple requests to respect the server's per-request caps (200 events /
3.5 MB).

State is kept in `~/.claude/state/one_signal_state.json` so re-runs only
process new turns — restarting Claude Code or re-enabling the plugin will
not re-upload turns already sent.

If your organization hasn't connected Langfuse yet, the proxy responds
`503 signal_not_configured`; the hook logs a hint ("connect Langfuse in
Console → Integrations") to the debug log and exits cleanly. The checkpoint
is not advanced past turns that failed to deliver, so once Langfuse is
connected, the next turn's hook run retries them automatically.

## Reliability notes

- The incremental-upload checkpoint (byte offset + turn count) only ever
  advances past turns whose events were fully accepted upstream (every
  HTTP chunk 2xx, and any 207 response's per-event `errors` empty) *and*
  that were completely parsed (never past a trailing incomplete turn, e.g.
  a user message with no assistant reply yet). A transient delivery
  failure or an in-progress turn is retried on the next hook run rather
  than silently dropped; deterministic trace/observation IDs make retries
  idempotent upserts, not duplicates.
- The state-file lock (`~/.claude/state/one_signal_state.lock`) is a
  single global lock shared by every session on the machine, not one lock
  per session — concurrent hook runs for different sessions serialize on
  it rather than running in parallel. **Known-deferred.**
- The lock uses `fcntl`, which doesn't exist on Windows; on Windows the
  hook proceeds without cross-process locking (best-effort only).
  **Known-deferred.**
- Session state entries older than 30 days are garbage-collected. If a
  session resumes after its entry has been GC'd, the hook re-uploads the
  whole transcript from scratch — mitigated by deterministic per-turn IDs,
  so the re-upload upserts existing traces/observations rather than
  duplicating them. **Known-deferred.**
- The `hooks.json` wrapper (`uv run` / `python3 ... one_signal_hook.py`)
  only exits non-zero if the Python launcher itself is missing (e.g.
  neither `uv` nor `python3` on `PATH`); any failure inside the hook
  script itself always exits 0. **Known-deferred**, by design — see
  "Requirements" above.

## Privacy

This plugin transmits your Claude Code session data — conversation turns,
assistant generations, tool calls, and token-usage statistics — to
`ONE_SIGNAL_BASE_URL` (your One Connector deployment), authenticated with
your One Connector access token. One Connector forwards it into your
organization's own Langfuse project; it is not shared across organizations.
No data is sent anywhere other than the endpoint you configure. On read,
the server masks known secret shapes (API keys, tokens, etc.) before
surfacing trace content back to the Console UI.

For how your organization's Langfuse instance handles the data it
receives, see https://langfuse.com/privacy (Langfuse Cloud) or your own
infrastructure's data-handling policy (self-hosted Langfuse).

## Reconfigure

```bash
claude plugin disable one-signal
claude plugin enable one-signal
```

## Uninstall

```bash
claude plugin uninstall one-signal
```

## Troubleshooting

- Nothing showing up in Console → Observe: check `~/.claude/state/one_signal_hook.log` (enable `CC_ONE_SIGNAL_DEBUG`).
- `503 signal_not_configured` in the log: your organization hasn't connected Langfuse yet — do that in Console → Integrations, then it will pick up on the next turn.
- Hook not firing: confirm with `claude plugin list` that `one-signal` is enabled; restart Claude Code.

## License

MIT. This plugin is derived from Langfuse's Claude-Observability-Plugin
(MIT License, © Langfuse GmbH); see [LICENSE](./LICENSE) for the full
original license text plus the additional One Infra license covering the
changes made here.
