# Claude Code account switching: design draft

Status, 2026-10-12: implemented (`ClaudeSupervisor`, `ManagedClaudeSession`, `run_managed_claude` in `runtime.py`; helper
`claude_hook`). A real end-to-end run through the production worker passed: Account A turn, switch, Account B turn, exit.
Claude Code 2.1.296.
Codex is implemented through a launcher-owned app-server ([plan](quota-management-plan.md),
[evidence](codex-switch-feasibility.md)); Claude Code has no remote front end, so the mechanism differs.

## What carries over from the Codex work

- **Two layers.** Environment-level state is shared by every account of one environment; account-level state stays in
  each `CLAUDE_CONFIG_DIR`. The launcher already shares `skills`, `agents`, `commands`, `hooks`, `output-styles`,
  nonsecret `settings.json` keys and `CLAUDE.md` files (`SHARE_DIRS` / `SHARE_SETTINGS` in `runtime.py`), and passes the
  native plugins with `--plugin-dir` (`user_plugin_dirs`), so Claude plugins are already environment-level.
  Account-level: the sign-in, MCP OAuth sign-ins, claude.ai connectors.
- **Account differences go to the user.** Same rule and UI as Codex: list what the target lacks; a manual switch asks,
  and a flow follows its "when the target lacks..." block. Different instructions, settings or skills always stop.
- **Exact conversation, never "the latest".** Switching only at a verified safe point; an uncertain tool outcome is
  never replayed.

## Mechanism (proposed)

| Need | Official means | Note |
| --- | --- | --- |
| Exact conversation id | Start with `--session-id <uuid>` chosen by the launcher | No transcript guessing; replaces `--continue` for managed sessions |
| Structured events | Hooks injected with `--settings <json>` for this session only: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `SessionEnd`; the hook command is a launcher helper that appends one event file per call in the control folder | No change to the user's settings files; terminal output is never a status source |
| Safe point | `Stop` after the last `UserPromptSubmit`, no `PreToolUse` without its `PostToolUse`, no running subagent. Work-starting events (`UserPromptSubmit`, `PreToolUse`, `SessionStart`, `SubagentStart`, `PermissionRequest`) are synchronous hooks with `onFailure: "block"` (Claude Code 2.1.295+): the event is on disk before the CLI goes on, and a hook that cannot run, fails or times out blocks the action. A handoff takes a lock file, then reads the whole event queue (a backlog larger than one read postpones the handoff); a gated hook records first, then checks the lock and refuses (exit 2) while it is held. Ending events stay asynchronous (a late one only keeps the session busy). The earlier two-second quiet period was removed: late asynchronous hooks could make a busy session look idle | Same gate as Codex |
| Handoff | End the CLI at the safe point (the worker owns its process), copy the session transcript (and its subagent/todo files) into the target profile, start `claude --resume <id>` with the target `CLAUDE_CONFIG_DIR` in the same terminal, with the same `--model`, `--effort`, permission mode and plugin dirs. The target becomes the active account only after its `SessionStart` and its status line both report exactly the expected session id, and the status line's `model.id` is exactly the full model id the source itself reported (status line, or `SessionStart` when it carries one). The target is started with that id, so no alias is resolved; a missing or different session id or model is not verification. Until the source has reported its model (after start, a resume or a `/model` switch) the switch waits and says so; an account's default model is never used instead. The settings (proven model, effort, permission mode) are read before the target check and confirmed again after the lock and the full event read; if they changed, the lock is released without stopping the source and the next safe point checks again. The confirmed snapshot is what the target starts with, what verification expects and what a rollback restarts. Within 60 s. Measured with Claude Code 2.1.296: on an interactive `--resume` (the handoff case) `SessionStart` carries no `model`, while the status line, which runs once whenever a session starts or resumes, carries `model.id`; the source's model is learned the same way. Otherwise the source account is started again on the unchanged conversation | The terminal visibly restarts; the conversation stays |
| Continue after a quota stop | `claude --resume <id> "<continue prompt>"` | Only after a verified quota failure, as for Codex |
| Quota | Status line input `rate_limits`; `StopFailure` hook with matcher `rate_limit` | See gate 4 below |

## Feasibility gates: results

Checked with two real subscription accounts (Account A, Account B) on this PC, `--model haiku` (claude-haiku-5-5), prompts
answered in one word or number, cwd = an already trusted project. Six short turns in total over three runs.

| Gate | Result | Evidence / solution |
| --- | --- | --- |
| 1. Resume under another account, including signed thinking | **Passed** | Turn 1 on Account A with `alwaysThinkingEnabled` produced a signed thinking block; the transcript copied to Account B's profile at the same relative path (`projects/<project folder>/<session>.jsonl`) and `claude --resume <id>` answered turn 2 with no API error. Claude Code stores signed thinking in transcripts (269 signed blocks in one long session), so this was a real test. |
| 2. Hooks from `--settings` in an interactive session | **Passed** | `SessionStart` (`source: startup` / `resume`), `Stop`, `SessionEnd` fired with `session_id` and `transcript_path`. Hooks are lists and lists merge across settings sources (settings documentation), so the user's own hooks keep running; that merge was not observed directly. |
| 3. End the CLI at `Stop` | **Passed** | `/exit` after `Stop` ended the process within seconds; `SessionEnd` fired; the transcript was complete and resumable. |
| 4. Structured quota | **Solved** | The status line receives `rate_limits.five_hour` / `seven_day` (`used_percentage`, `resets_at`) for subscription accounts after the first response; observed on both accounts. A quota stop is the `StopFailure` hook with matcher `rate_limit` (documented, not triggered here). `statusLine` is a single value, so the launcher's status line must wrap and then run the user's own command. |
| 5. Settings parity | Open, low risk | Model and effort are passed as flags; permission mode with `--permission-mode`; plugins already come from the native folder via `--plugin-dir`. MCP sign-ins are account-level: compare with `claude mcp list` (to verify) and treat differences like Codex account differences. |

Other observations: `--session-id <uuid>` makes the transcript file name the launcher's id. A subfolder whose
`CLAUDE.md` imports a file above it triggers an "external imports" prompt; a managed launch must not answer prompts for
the user, so such a prompt simply stays on screen.

## Implementation outline

Hook and status-line event files use a shared sequence allocated under an OS file lock. The sequence is reserved before
the complete JSON event is published atomically; a hook crash can leave a gap but cannot reuse an event identifier.
The consumer orders events by this sequence, independently of Windows wall-clock precision and writer process IDs.
A sequence lock timeout or damaged sequence blocks work-starting hooks rather than silently losing their events.

1. Launch managed Claude sessions with `--session-id`, `--model`, `--effort`, `--permission-mode` from the account or the
   previous session, and `--settings` carrying the launcher hooks and a wrapping status line.
2. Hook and status-line commands call a new launcher helper (added through `selfcmd.HELPERS`, `entry.py` and the spec's
   `hiddenimports`; on SSH hosts the same function in `runtime.py`). It appends one small event file per call in the
   session's control folder and, for the status line, records `rate_limits` into the shared quota cache before running
   the user's own status line command with the same input.
3. The worker supervises the CLI: safe point = `Stop` after the last `UserPromptSubmit`, every `PreToolUse` matched by
   `PostToolUse`, no running subagent. Switch: `/exit`, wait for the process and `SessionEnd`, copy the transcript (and
   its subagent files), restart `claude --resume <id>` with the target `CLAUDE_CONFIG_DIR` in the same terminal.
4. Quota failures (`StopFailure` / `rate_limit`) allow the Codex rules: switch at the safe point and continue with
   `claude --resume <id> "<continue prompt>"`, or wait for a trusted reset (`resets_at`) and re-check.
5. Reuse the Codex flow editor, account-difference prompt, monitor cache and UI; Claude quota is known only from
   accounts' recent sessions (status line), so unknown accounts are not chosen automatically.
