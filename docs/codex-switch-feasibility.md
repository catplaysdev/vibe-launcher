# Codex account switching: Phase 0 evidence and technical direction

Reviewed on 2026-10-11. This is an evidence report and implementation direction, not a passed end-to-end capability test.
Requirements and scope are in the [quota management plan](quota-management-plan.md). Design attribution is in the [CLIProxyAPI research](cliproxyapi-reference.md).

## Verified local baseline

The installed executable reported **codex-cli 0.160.1**.
Its `resume --help` accepts a specific session ID, a follow-up prompt, `--remote`, and configuration/model overrides.
Its `app-server --help` exposes stdio and WebSocket listeners, WebSocket authentication options and schema generation.
These are interface checks; no account login, model request or SSH connection was performed.

Generated the non-experimental JSON schema into ignored scratch storage at `.local-test/quota-phase0/schema` using a separate empty `CODEX_HOME`.
The executable emitted a profile-path canonicalization access warning, but produced readable schema files. Do not count this as a successful runtime profile-isolation test.

| Schema artifact | SHA-256 |
| --- | --- |
| `ClientRequest.json` | `1c7fec8758deb95ffe967060130e8d1ddb7a0a5a437ee9b64086dddbed8f5831` |
| `ServerNotification.json` | `d3479ecf59ca421a8b052d3d15e8196d23d297a0bd85ae5a270a34ddd9c865c9` |

The schemas establish these contracts, not successful execution:

| Contract | Inspected fields |
| --- | --- |
| `ThreadResumeParams` | Required `threadId`; overrides include model, config, cwd, approval policy, sandbox and instructions. |
| `ThreadResumeResponse` | Model/provider, reasoning effort, cwd, approval policy/reviewer, sandbox, instruction sources, disabled plugins and thread. |
| `TurnStartParams` | Required thread ID and input; supports model, effort, cwd and permission overrides. |
| `TurnInterruptParams` | Required thread ID and turn ID. |
| `TurnCompletedNotification` | Required thread ID and turn, allowing correlation to the execution being managed. |
| `ConfigReadResponse` | Effective on-disk configuration, origins and optional layers; not a complete live-turn settings snapshot. |

## Official documentation evidence

The [CLI reference](https://learn.chatgpt.com/docs/cli/reference) documents exact-ID resume and optional follow-up input.
The [app-server documentation](https://learn.chatgpt.com/docs/app-server) documents a native TUI connection using `codex --remote`, thread/turn events, interruption and multi-bucket rate limits.
It labels WebSocket transport experimental and unsupported for production. It also describes restrictions on paginated-history resume.
These documentation statements do not prove that the installed binary supports every described history mode or cross-profile behavior.

Version-matched evidence is available in the official
[0.160.1 protocol source](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L328).
`thread/resume` rejoins a running thread; its unstable `path` must match that thread's active rollout.
The experimental `history` input is explicitly reserved for Codex Cloud, so it is not the proposed transfer interface.
[Upstream tests](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/app-server/tests/suite/v2/thread_resume.rs#L303)
reject a second process's resume while the first still owns the writer, for both legacy and paginated history.
The test resumes successfully after the original process shuts down. A separate test rejects an unmaterialized thread.
Consequently, target capability checks can precede shutdown, but a second writer cannot safely be started first.

The generated `ThreadSettingsUpdatedNotification` carries current model, effort, permissions and other settings.
`Thread.model` and `Thread.reasoningEffort` explicitly are not per-turn execution telemetry, and `TurnStartedNotification`
does not contain a complete effective-settings snapshot. Settings preservation must account for that distinction.

## Offline runtime experiment

The reproducible harness is [scripts/check_codex_control.py](../scripts/check_codex_control.py).
It starts the official executable with empty profiles and a loopback mock Responses provider that requires no authentication.
The successful report is in ignored scratch storage at `.local-test/quota-phase0/offline-01/report.json`.
It records successful isolated initialization, one structured completed turn, exact-ID resume across two profiles,
presence of history, and equality of model/provider/cwd/approval policy/reviewer/sandbox between start and resume.
There was one mock provider request, no external inference, no real account, and no SSH.

The two profiles deliberately share a synthetic `sqlite_home`; the first app-server is shut down before the second starts.
This proves a narrow storage/control path, not arbitrary profile portability or account switching.
The harness does not run a continuation turn on the target, verify reasoning effort or plugin/MCP equivalence,
exercise native TUI clients, tools, approvals, descendants, or prove an unfinished task can continue safely.

A separate native TUI startup smoke check connected official Codex 0.160.1 through ConPTY with `--remote`
to an isolated loopback app-server using the same mock-provider configuration. The terminal remained alive,
displayed the official version banner and `offline-test` model, and made zero mock inference requests.
The owned TUI and server were closed afterward. Ignored evidence is under `.local-test/quota-phase0/native-tui-01/`.
This establishes native remote startup only; it does not validate the launcher's bridge, live account handoff,
multiple frontend clients, reconnects, or continuation.

## Capability matrix

| Requirement | Evidence now | Still required |
| --- | --- | --- |
| Exact conversation identity | CLI and schema accept an ID. | Capture the ID from the actual terminal-owned session; prove two same-directory terminals never cross-bind. |
| Native terminal plus structured control | Official `--remote` option and app-server protocol exist. | Prove TUI and control connection observe the same live thread, with correct subscription and reconnect behavior. |
| Same settings after switching | Resume response exposes part of the effective configuration. | Capture live TUI overrides; validate instruction/plugin/MCP equivalence without reading or copying secrets. |
| Safe handoff | Completion and interruption contracts identify the thread/turn. | Account for outstanding tools, approvals and descendant agents; prove old execution is quiescent. |
| Cross-account resume | Offline exact-ID resume across empty profiles sharing synthetic SQLite state passed after stopping the first server. | Prove authenticated account handoff, full settings/history equivalence and successful target continuation. |
| Unfinished task continuation | Follow-up input can initiate another turn. | Prove a continuation checkpoint can resume intent without replaying uncertain effects; reopening a thread alone is insufficient. |
| SSH persistence | Launcher already uses tmux supervision. | Prove the control endpoint and exact thread survive UI detach, and reconcile offline state before switching. |

No row is a passed local/SSH switching guarantee. Linux CLI version and runtime capabilities remain unverified.

## Recommended architecture experiment

Prefer testing an **opt-in managed Codex terminal** using the official app-server and native TUI before evaluating an upstream API proxy.
This recommendation is an inference from the available interfaces; it is conditional on the experiments below.

Use one execution owner per managed terminal initially. The server runs with the selected account's isolated profile; the native TUI remains the displayed client.
Keep quota snapshots shared per account/environment independently of the execution owner.
On SSH, keep the server, TUI and supervisor on that host under the existing persistent-session ownership model. Closing the desktop only detaches display; it must not implicitly stop remote work.

The server endpoint must be loopback-only and authenticated. A per-execution local capability secret is distinct from an account OAuth credential; keep it out of URLs, command arguments, export and logs.
If the launcher must connect across SSH, use an explicit loopback tunnel with the existing host-verification rules; never expose an unauthenticated listener.
Exact endpoint lifecycle, local capability storage and control transport remain implementation details to validate.

Do not attach a newly launched app-server to an unrelated direct TUI and assume it owns that conversation.
Do not use `thread/resume` as a passive subscription until its effects on an already loaded thread are verified.
Do not share one mutable authenticated server across accounts and mutate its identity while other threads run.

## Ordered offline experiments

1. **Protocol behavior:** use a fake server to exercise handshake, correlation IDs, notifications, server requests, bounded queues, cancellation and disconnect. Confirm stale events cannot update a replaced owner.
2. **Isolated official startup:** run 0.160.1 with an empty test profile and no inference. Check initialization, authentication absence and shutdown with strict timeouts and owned-process cleanup.
3. **Native TUI control:** connect two test clients to one isolated endpoint. Determine how a terminal-selected thread is identified and how its control observer subscribes without changing it. Do not start a paid turn.
4. **Synthetic history:** create documented fixtures for the supported history format. Resume two exact IDs through separate empty profiles and compare IDs/configuration. Do not read real conversations or treat hand-written history as proof of real inference continuation.
5. **Mock handoff:** simulate running tools, child agents, waiting approvals, interruption, missing acknowledgements, crashes and SSH disconnect. Only a verified quiescent execution may release its owner lease.

After these checks, authorized real test accounts and a test SSH host must validate native `/model`/reasoning changes, real tool completion, account exhaustion, cross-account continuation and platform behavior.
If no trustworthy control path exists, compare the following alternatives before changing the backend.

| Direction | Benefit | Limitation / decision point |
| --- | --- | --- |
| Official native TUI with managed app-server | Native interface plus structured lifecycle. | Experimental transport; shared-client control, settings and history must be proven. Recommended experiment. |
| Direct CLI with explicit ID recovery | Smallest launch change. | Current implementation cannot observe safe turn boundaries or live settings. Alone it cannot fulfill the agreed automatic switching scope. |
| Launcher-owned app-server interface | Full ownership of inputs, approvals and event flow. | Requires a new interaction UI and changes the native-terminal experience; needs a product decision. |
| Optional CLIProxyAPI integration | May retain one agent process while upstream accounts change. | Adds request proxy and credential responsibility; must verify tool/conversation semantics and settings independently. Not currently selected. |

## Handoff semantics to prove

For a proactive low-quota trigger, queue the switch until a safe boundary rather than interrupting a healthy tool. A completed turn does not imply another turn should be started.
If quota exhaustion failed an unfinished turn, preserve its exact identity and classify the last tool outcomes. Only start continuation input when the workflow already permits it and all previous effects are known.
An interruption acknowledgement or a disconnected TUI alone does not prove all descendant work stopped. Unknown outcomes require attention.

Validate the target before stopping the source wherever possible. Acquire a terminal transition lease, confirm source quiescence and persistence, then resume the exact conversation with the saved effective settings.
The target must remain non-executing until identity/settings checks succeed. Failure leaves a recoverable checkpoint; it must not produce two live agents working on the same task.
Model catalog presence is not proof of subscription entitlement. Fail clearly if actual access rejects the requested model; do not silently substitute another model.

## Verification status

Passed: version/help and schema inspection; version-matched upstream source/test inspection; launcher seam inspection;
offline app-server initialization, structured turn completion and cross-profile exact-ID resume under the constraints above.
Also passed: isolated native TUI remote startup without inference.
Not performed at that stage: multi-client/native TUI handoff experiment, external inference, real account switching,
credential transfer, target continuation, real SSH or packaged build. Scratch schemas and probe reports are not product assets.

### Launcher bridge with the native TUI (codex-cli 0.162.1, offline)

`scripts/check_managed_codex.py` runs the official native TUI (`codex --remote`) in the launcher's ConPTY against
`runtime.ManagedCodexBridge` and `ManagedCodexSession`, with official `codex app-server --stdio` owners in two blank,
isolated profiles and a loopback Responses fixture; control requests use the production file channel. Passed in four
complete runs: thread binding, a turn on account A, an exact manual handoff to B (the original owner exited before the
new one started, the TUI stayed connected, effective settings and instruction sources matched), a turn on B, a turn that
failed with `usageLimitExceeded`, the flow selecting A again, the launcher continuing the failed task on A, and reopening
the exact thread in a new managed terminal with all four turns.

Observed protocol facts that shaped the bridge:
- After each user turn the TUI starts an ephemeral, tool-free side thread (`threadSource: "thread_title"`, shell, MCP,
  multi-agent and plugins disabled). The bridge accepts exactly that shape as a side thread; its turns block a handoff
  until they finish, and any tool item in it fails closed.
- After a turn fails on quota, `thread/read` reports the thread as `systemError`, not `idle`. It counts as a checkpoint
  only together with the bridge's own record that the last turn failed with a quota error.
- Passing `serviceTier: null` to `thread/resume` makes the target report `"default"`; unset values are therefore not
  forwarded, and the comparison stays strict.
- `mcpServerStatus/list` with the thread id answers without starting servers; the target's OAuth sign-in is read from
  Codex's own status, never from credential files.

Still not performed: real subscription accounts and quota recovery, SSH/tmux hosts (including the compact helper
transport), and use from the packaged build.

### Real accounts, local (codex-cli 0.162.1, 2026-10-12)

Two subscription accounts on the user's machine, one project, minimal prompts, through the production factory, bridge
and controller. Findings that only real profiles exposed, each fixed and covered by tests:
- `plugin/list` answered with one 13.2 MB line, above the bridge's former 8 MiB limit; the reader stopped and the TUI
  queued input. The limit is now 64 MiB and outgoing messages are fragmented into 4 MiB frames.
- With MCP servers configured, the TUI's title thread disables each server by name instead of sending an empty table.
- A `personality` set in one profile made every handoff impossible under the "unattestable means blocked" rule; it is
  now requested explicitly on the target and confirmed by the next turn.
- Per-account homes drift: plugin caches keep old versions and account-linked remote plugins, approval rules diverge.
  Loaded skills (`skills/list`) were identical; plugins (`plugin/installed`), connected apps (`app/installed`) and one
  MCP server whose sign-in Codex reports as `unknown` differed. Remote plugins load asynchronously, so their skills are
  compared through installed plugin versions instead.
- A remote plugin may not exist for another account at all (`plugin/read` 404); such differences are shown to the user.

Result: manual switch Account A → Account B blocked with the list of differences, then completed after the user's choice;
the exact thread holds both turns (`thread/read`), the source app-server exited, the TUI stayed connected.

Low-cost flow check (gpt-6-luna, low effort, two one-word turns): with thresholds placed around the accounts' real
remaining quota (Account B 68 %, Account A 87 % on the weekly window), the flow waited with the real weekly reset as its
retry time, then chose Account A, stopped on the listed account differences, and after the flow was changed to allow them
switched automatically at the safe point; model and effort were kept, the thread held both turns and reopened by id.
One real bug surfaced and was fixed: an old "blocked" report was re-applied after the flow changed.
