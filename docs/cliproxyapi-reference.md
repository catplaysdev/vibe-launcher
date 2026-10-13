# CLIProxyAPI reference research

Reviewed on **2026-10-11**. Status: research and open design questions, not an implementation specification.

## Scope and attribution

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is the reference project.
Its [README](https://github.com/router-for-me/CLIProxyAPI/blob/main/README.md) describes an API proxy providing several compatible client protocols and multi-account access.
Vibe Launcher remains an official Codex / Claude CLI launcher; adding an API proxy is **undecided**.
This note records design lessons, not approval to reproduce its authentication or request translation behavior.

Upstream source retrieval had mixed cache dates. The configuration snapshot at
[fd48ea6](https://github.com/router-for-me/CLIProxyAPI/blob/fd48ea6/config.example.yaml)
was readable; other inspected files below use mutable `main` links.
Do not treat those files as a single immutable revision or infer that fd48ea6 was the latest commit.
Before implementation, resolve a complete commit SHA and recheck the selected behavior against that revision.

## Observed scheduling behavior

The [configuration snapshot](https://github.com/router-for-me/CLIProxyAPI/blob/fd48ea6/config.example.yaml)
offers round-robin, weighted-round-robin and fill-first; round-robin is the default.
Weights are configured values, not necessarily measured remaining quota.
It exposes independent retry-round, credential-count and cooldown-wait limits.
The example uses three additional retry rounds, all eligible credentials per round, and at most 30 seconds of cooldown wait.
It also exposes optional cooldown persistence and session-affinity settings.

The inspected [selector implementation](https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/cliproxy/auth/selector.go)
filters availability through `isAuthBlockedForModel`, groups candidates by priority, and then selects candidates.
It distinguishes credential-wide quota blocking from model-specific states.
An active session binding can retain an available credential across priority tiers;
when its credential is unavailable, selection falls back and creates a new binding.
Relevant symbols: `collectAvailableByPriority`, `getAvailableAuthsWithPriorityMode`,
`RoundRobinSelector.Pick`, `SessionAffinitySelector.Pick`, `isAuthBlockedForModel`.

**Interpretation:** this establishes quota/cooldown-aware eligibility and configurable distribution.
It does not establish that the default selector maximizes remaining quota or predicts exhaustion.
Round-robin, static weighting, availability filtering and proactive remaining-quota ranking are separate concepts.
Any plugin or distributed scheduler behavior beyond these inspected paths remains unverified.

The [cooldown implementation](https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/cliproxy/auth/conductor_cooldown.go)
handles HTTP 429 with a provider retry hint when present, otherwise calculated quota backoff.
It applies a minimum cooldown floor and preserves a later existing recovery time.
Credential-scoped results can block sibling models; model states support narrower blocking.
Transient upstream failures have distinct handling.
Restoration rejects expired cooldown records and applies guards for disabled credentials.
Relevant symbols: `MarkResult`, `quotaCooldownAfterFailure`, `restoreCooldownRecordLocked`.

The [execution implementation](https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/cliproxy/auth/conductor_execution.go)
tracks attempted credentials for each retry round, passes limits to execution, checks retry eligibility,
and waits through a cancellation-aware execution context.
Request termination/stop errors leave the retry loop.
This is request-level behavior inside a proxy; it does not demonstrate safe replay of an interactive CLI task.
Relevant symbols: `Execute`, `ExecuteStream`, `executeMixedOnce`, `waitForCooldown` call sites.

## Current launcher evidence

- [`runtime.py`](../src/vibe_launcher/runtime.py), `fetch_quota` and its helpers (lines 1915-2013 at review): Codex subscription quota is queried through the official app-server `account/rateLimits/read` method, scoped by account and environment. The summary reads `rateLimits`; support for `rateLimitsByLimitId` was not present in this path.
- [`ui_integrations.py`](../src/vibe_launcher/ui_integrations.py), lines 152-210: user-triggered local/SSH quota queries have busy guards and timeouts. Job-result polling is not a periodic refresh of every account.
- [`ui.py`](../src/vibe_launcher/ui.py), lines 1184-1223, and [`workspace_page.py`](../src/vibe_launcher/workspace_page.py), lines 476-480: account selection uses applicability, remembered selection, a single candidate or user choice; these paths do not consult quota.
- [`service.py`](../src/vibe_launcher/service.py), lines 168-182 and 238-239: launches use the selected/preset account; session recovery retains its account.
- [`test_quota.py`](../tests/test_quota.py) covers quota parsing, RPC failures/timeouts, cache isolation and UI/job handling. Tests were inspected, not executed for this documentation change.

These findings identify a gap between quota observation and launch-time selection, not an absence of quota support or proof that the official CLI has no retry behavior.

Follow-up inspection identified continuity limitations in [`runtime.py`](../src/vibe_launcher/runtime.py):
Claude uses `--continue` (line 453) and Codex uses `resume --last` (line 475), rather than an exact native conversation ID.
Launcher session IDs identify managed processes/tmux sessions, not native CLI conversations.
Optional shared project history copies missing transcripts between same-tool profiles in the same environment (lines 1221-1254, 1288-1290), but existing destination copies are not refreshed (lines 1114-1116) and paginated Codex transcripts are excluded (lines 1145-1150).
The worker monitors process exit (lines 1650-1668), which does not establish a safe per-turn or per-tool handoff point.
Consequently, current reload/history sharing is insufficient evidence for exact cross-account continuation or safe automatic replay.
Launch arguments currently use the target account's model (`runtime.py`, lines 448-460); session records do not capture the effective model/settings or later in-terminal changes (`store.py`, lines 59-62).
Local reload starts a replacement before terminating the old terminal, while persistent SSH reload reattaches the existing tmux process (`ui.py`, lines 1007-1046). Neither path is a verified account-switch handoff primitive.
Proposed feasibility gates are exact conversation identity, effective settings preservation, a trustworthy safe handoff signal, and confirmation that the old execution has stopped. Both UI entry points should use the same verified switching mechanism.

## Lessons to evaluate for Vibe Launcher

These are proposed design directions, not claims that all are absent from the launcher.
The launcher already has Codex quota observation; observation and automatic account selection need separate assessment.

| Lesson | Possible launcher application | Decision or evidence needed |
| --- | --- | --- |
| Separate eligibility from ranking | Explain why an account is unavailable before recommending another | Reliable quota source, freshness window and unknown-state policy |
| Distinguish account and model limits | Show which work a limit actually blocks | Provider-specific structured evidence; no terminal-output inference |
| Keep session affinity explicit | Preserve the account of an existing CLI session | Whether recommendations apply only to new launches |
| Bound recovery attempts | Give recovery an attempt/time budget and a visible reason | Which actions are safe without duplicating tools or work |
| Use a small policy interface | Keep recommendation logic testable apart from the UI | Existing service/store seams and the agreed first feature |
| Make capacity policy understandable | Offer user-selected priority or weights if useful | Actual need before adding settings or scheduling complexity |

The upstream [manager contracts](https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/cliproxy/auth/conductor.go)
separate `Selector`, provider execution, result policy and observation hooks.
That separation is a useful architectural reference, without requiring the launcher to adopt its framework.
The result contract also distinguishes credential scope and carries version information;
stale observations deserve explicit treatment in any launcher recommendation design.

## Confirmed product requirements

The user clarified the following requirements during the design interview:

- Users choose the accounts participating in quota management/load balancing and customize the allocation policy.
- A visual, flowchart-like block editor configures quota management and account switching for a terminal. The execution model and available blocks remain to be designed.
- A Workspace terminal toolbar action allows manual account switching even when no policy has been configured.
- Both paths should preserve work progress and allow an ongoing task to continue after switching wherever technically possible.
- Conversation/work continuity and consistency of model, reasoning settings, instructions, skills and MCP configuration are both first-version requirements. Availability and equivalence must be verified rather than assumed.
- First-version support is Codex on both local and SSH environments; Claude is outside the initial guarantee.
- A brief pause while waiting for a verified safe handoff is acceptable. If the previous action's outcome is unknown, prompt the user instead of automatically replaying it.
- Pools contain Codex subscription accounts within one environment. Local and each SSH environment are separate; the first version does not move work between hosts or include API billing accounts.
- The initial visual editor supports explicit account priority, round-robin distribution and available-quota-based selection. Quota selection must consider the applicable limit windows rather than comparing a single percentage.
- Initial blocks cover low-quota/exhaustion triggers, safe handoff, and waiting or notifying when no eligible account remains. Weighted distribution and per-account concurrency caps are not initial requirements.
- Monitoring runs only while terminals with enabled policies exist. Prefer trustworthy structured events; otherwise use bounded polling with a configurable interval and enforced minimum, shared account/environment snapshots, and failure backoff. Stop monitoring when the last relevant terminal closes. This is the explicitly approved exception to the project's background-work restriction.
- When every account is limited, default to preserving work, notifying, waiting until the earliest known recovery opportunity, and rechecking. Automatically continue only after eligibility and safe handoff are verified. Offer a notify-only alternative; unknown reset times or execution outcomes do not permit automatic continuation.

These are product goals, not proof that a running official CLI process can change credentials in place.
Automatic continuation, safe interruption and cross-account resume require a tool-specific feasibility check.

## Implementation proposal and feasibility gates

See the [first-version plan](quota-management-plan.md) for stages, proposed defaults and acceptance criteria.
The interview has established the product scope; technical feasibility remains to be demonstrated.
Whether official CLI interfaces can satisfy continuation, or an optional proxy would be needed, is unresolved.
Do not silently replace the terminal experience or introduce proxy credential management if a feasibility gate fails.

Before implementation, define behavior for missing/stale quota, all accounts limited, reset-time changes,
concurrent launches, account/environment incompatibility, and resuming an existing session.
Tests should prove those decisions without accessing real accounts or SSH hosts.
The requested feature includes switching running tasks, but does not authorize testing against real user sessions now, replaying uncertain work, or silently changing the requested model.

## Development handoff

The user's requested development allocation is:

- GPT-6-Astra: planning; reasoning effort was not specified.
- GPT-6.1-sol, medium: implementation of the agreed plan.
- GPT-6-luna, high: simple tasks.

This is a development workflow, **not a product requirement for automatic model routing**.
It does not change the main session's selected model or global defaults.

## License and verification

Upstream identifies its license as [MIT](https://github.com/router-for-me/CLIProxyAPI/blob/main/LICENSE).
No upstream code or assets were copied into this project; this document contains analysis and source links.
If implementation later copies material, inspect that exact revision's notices and record the attribution in the relevant code and third-party notices.

Verification was source inspection only. No upstream executable, live account, quota exhaustion,
failover, cache benefit, credential refresh, or distributed scheduling was exercised.
The API/GitHub retrieval limitations above prevent claiming a reproducible full-source snapshot today.

## Multi-account state model (reviewed 2026-10-12, commit f1d4a94)

Read at [`f1d4a94`](https://github.com/router-for-me/CLIProxyAPI/tree/f1d4a94ef4398349716f2db5bbc3a27624c174a7) (main, 2026-10-11).
Source inspection only; nothing was run. Items marked *unverified* were not settled by the files read.

**One client home, many credentials.** The proxy sits between an unchanged CLI and the provider. The CLI keeps one home
(skills, plugins, approval rules, conversations); only the upstream credential changes per request. This is why its
accounts never drift apart, unlike one CLI home per account.

- Credentials: one JSON file per account in `oauth.auth-dir` (default `~/.cli-proxy-api`; `sdk/auth/filestore.go`).
  The proxy runs the providers' OAuth flows itself (PKCE with the public Codex / Claude Code client ids,
  `internal/auth/codex/*`, `internal/auth/claude/*`) and refreshes tokens (`sdk/cliproxy/auth/auto_refresh_loop.go`).
- Codex requests go to `https://chatgpt.com/backend-api/codex` with the credential's account header
  (`codex_executor_request.go`, `applyCodexHeaders`). `Execute` deletes `previous_response_id`, so every request carries
  the full history and any credential can serve the next one (`codex_executor_execute.go`).
- That statelessness has a price: reasoning items are kept in a replay cache per model and session and checked for
  valid signatures (`internal/cache/codex_reasoning_replay_cache.go`, `applyCodexReasoningReplayCacheRequired`,
  `clearCodexReasoningReplayOnInvalidSignature`); Claude thinking blocks get the same treatment
  (`claude_thinking_replay_cache.go`). Exact cross-account behaviour is *unverified*.
- Session affinity (`routing.session-affinity`, default off, TTL 1h; subagents inherit the parent's credential) keeps a
  conversation on one credential "maximizing Prompt/KV Cache reuse"; failover rebinds only when that credential becomes
  unavailable (`config.example.yaml`, `SessionAffinitySelector` in `sdk/cliproxy/auth/selector.go`).
- Failover after HTTP 403/408/429/5xx retries the same request on other credentials (`routing.retry`), but only before
  the client has seen output (`conductor_stream.go` bootstrap; optional `stream-bootstrap-buffering`). Tools run in the
  client, so a retried request never re-runs a tool; no tool-level idempotency exists or is needed there.
- Quota is observed passively from response headers (Claude unified 5h/7d utilization, Codex quota headers/events;
  `sdk/cliproxy/auth/quota_signals.go`), with per-credential or per-model cooldown.
- Client identity is emulated (Codex `Originator`/User-Agent, Claude Code beta headers, device profile, optional
  system-prompt "cloaking" for non-Claude-Code clients; `claude_executor_cloaking.go`). The README gives no
  terms-of-service guidance; using subscription credentials outside the official clients is a provider-terms question.

### Consequences for Vibe Launcher

| CLIProxyAPI choice | Fit with the launcher's boundaries |
| --- | --- |
| Single CLI home, credential swap per request | Removes drift, but needs the launcher to hold and refresh OAuth tokens and to proxy the API. AGENTS.md: "Do not reimplement or proxy Codex / Claude sign-in"; credentials stay isolated. Not compatible without a product decision. |
| Session affinity, switch only at turn boundaries | Already the launcher's rule (verified safe point). |
| Failover only before output | Matches "never replay an uncertain action". |
| Passive quota from responses | The launcher already prefers structured `account/rateLimits/updated` events. |
| Identity emulation / cloaking | Not acceptable for the launcher (official CLIs only). |

What the launcher borrows and what it does not (2026-10-13): it borrows the **scheduling ideas** only: account
selection strategies (in order, rotation, most headroom), a shared per-account cool-down after failures, bounded
retries, and keeping a healthy conversation on its account. It does **not** borrow the execution model: CLIProxyAPI
switches credentials per request inside its proxy, while the launcher hands a whole conversation from one official
CLI process to another at a verified safe point (exact conversation id, compared settings, one writer, rollback to the
source account on failure). No CLIProxyAPI code is used.

The launcher-compatible lesson: keep per-account homes for credentials only, and treat skills, plugins, approval
rules and conversation history as **environment-level** state shared by every account of that environment, for both
Codex and Claude. Design and verification of that shared layer are pending.
