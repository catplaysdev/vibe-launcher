# Codex quota management and account switching plan

Status, 2026-10-13: the fixed five-block editor was replaced by saved plans (templates, block canvas, validation, preview; see the guide), handoff safety was tightened (read-only approval-rule comparison, synchronous Claude hooks with a handoff lock, commit after the target reported, consistent rollback), and plan changes, SSH detach and shared cool-downs follow the lifecycle rules above. Status, 2026-10-11: implemented and integrated (service → worker → bridge → UI → persistence). The complete native TUI handoff, policy selection and continuation after a quota failure pass offline with Codex 0.162.1 (see the technical evidence report). Real subscription accounts, SSH/tmux hosts and the packaged build in use remain to be verified.
Read [CLIProxyAPI reference research](cliproxyapi-reference.md) for upstream attribution, source evidence, current launcher limitations and confirmed decisions.
The [technical evidence report](codex-switch-feasibility.md) records local Codex 0.160.1 interface/schema checks, offline exact resume, native remote TUI startup and remaining experiments. These checks do not establish a real-account or SSH switching guarantee.

## First-version outcome

You can select a pool of Codex subscription accounts in one environment, configure a visual block workflow, and assign it to a terminal. Local and individual SSH environments remain separate.
You can also switch accounts from the Workspace terminal toolbar without assigning a workflow.
Both paths preserve the exact conversation, working directory and effective execution settings, and resume unfinished work when a verified handoff permits it.
A short pause is acceptable. An uncertain previous action requires attention, not automatic replay.

The first version includes three strategies: account priority, round-robin, and available-quota selection.
It includes low-quota/exhaustion triggers, safe handoff, and wait/notify outcomes.
Claude, API billing pools, cross-host work transfer, weighted distribution and configurable concurrency caps are outside this version.
Concurrency correctness and preventing duplicate execution remain required.

## Proposed defaults

These details are recommendations for the implementation, not additional decisions already made by the user.

| Situation | Proposed behavior |
| --- | --- |
| Existing healthy conversation | Keep its account; do not switch just because another account's score improves. |
| New managed terminal | Apply its workflow's selection strategy before starting work. |
| Manual switch | Select a compatible account, show any incompatibility, and use the same handoff mechanism as automatic switching. |
| Manual selection with an active policy | Keep the selected account until a later configured trigger; do not immediately reverse the manual selection. |
| Stale or unknown quota | Refresh on demand; if still unknown, exclude from automatic quota-based selection and explain why. Do not report unknown as zero or unlimited. |
| All accounts limited | Preserve work and notify. Wake at the earliest candidate recovery opportunity, refresh, and continue only if eligible and safe. |
| No trusted reset time | Require attention; do not schedule speculative automatic continuation. |
| Unsupported model or incompatible effective settings | Block the switch with a concrete reason. Never silently downgrade model, reasoning or security settings. |
| Authentication failure | Exclude the account until revalidated; do not launch unattended sign-in loops. |
| Host disconnected or old process state unknown | Preserve the pending transition and require reconciliation before another process starts. |
| Policy disabled or last relevant terminal closed | Cancel monitoring and pending automatic actions; do not terminate unrelated work. |
| Launcher restarts | Restore visible pending state; follow existing explicit terminal restoration behavior rather than starting work invisibly. |

A policy's wait path uses the earliest time that a candidate might become eligible, considering every exhausted applicable limit for that candidate. That time triggers a fresh check; it is not proof that capacity has returned.

## Phase 0: prove continuation before committing the implementation architecture

Inspect the supported installed Codex CLI's official interfaces and matching source/schema. Record the tested version and capability matrix for local Windows and Linux SSH.
Do not infer interactive-terminal capabilities from an unrelated app-server connection.

Prove all of the following with isolated fixtures first:

1. Bind a terminal to an exact native conversation ID, including two terminals in the same project. Never use `--last` as the switching identity.
2. Resume that conversation through another eligible profile without overwriting credentials or using stale transcript copies. Cover current paginated/SQLite history behavior.
3. Capture effective model, reasoning, instructions, skills, MCP configuration, approval/sandbox settings and working directory, including changes made inside the CLI. Compare nonsecret configuration; do not copy account credentials or assume MCP authorization transfers.
4. Obtain trusted turn/tool completion or interruption evidence from the actual running session. Terminal text matching and process liveness alone are insufficient.
5. Stop or quiesce the old execution and verify its state before starting the replacement. SSH detach/reattach alone does not switch accounts.
6. Establish how unfinished work continues without resubmitting an uncertain tool action. A resumed conversation alone does not prove that an unfinished turn continues safely.

Record each item as supported, unsupported or unknown. Tests cannot claim exactly-once effects outside the launcher's control.
Real-account/SSH acceptance requires a separately authorized test environment; source inspection does not pass this gate.
If official interfaces cannot satisfy the requirements, return a concrete architecture comparison before choosing a proxy or a different execution backend. Do not quietly reduce the feature to new-session selection.

## Phase 1: quota snapshots and bounded monitoring

Extend the existing quota path rather than introducing credential readers. Preserve all relevant limit buckets and support the structured schema exposed by the verified Codex version, including multi-bucket responses when available.
Represent source, observed time, applicable model/limit, used percentage, reset time and freshness separately. A missing bucket is not available capacity.

Monitor only accounts needed by enabled terminal policies, with a shared query per account/environment, no overlapping queries, cancellation, deadlines and backoff.
Prefer session-originated structured events where available. Otherwise use configurable bounded polling.
Proposed starting interval: 120 seconds, minimum 60 seconds; validate cost on both local and SSH paths before finalizing. A poll timer must not bypass an in-flight query or provider backoff.
Use one scheduled wakeup for known recovery times rather than repeatedly polling exhausted accounts. No standalone service is introduced.

Treat snapshot freshness and candidate readiness separately. A policy-triggered low-quota switch must not also wake an unrelated terminal or switch every terminal sharing that account at once without per-terminal reconciliation.

## Phase 2: account pools and deterministic selection

Store explicit account IDs, environment, pool order, strategy and workflow association. Account deletion or incompatibility invalidates the affected rule visibly; never substitute an unrelated account.
Use existing persistence seams. Before any database schema change, perform the project's required read-only open check and assess reset impact without rewriting user data.

Separate candidate eligibility from ranking. Eligibility checks membership, environment, subscription type, authentication, model/settings compatibility, quota freshness and cooldown.

| Strategy | Proposed ranking and state |
| --- | --- |
| Priority | First eligible account in the configured order. |
| Round-robin | Next eligible account after the last committed assignment; skipped or failed candidates do not advance the committed cursor. |
| Available quota | Rank by the lowest remaining fraction among all applicable fresh limit windows. Break ties by configured pool order. This is headroom, not an estimate of remaining tokens. |

Compare only compatible limit semantics; if that cannot be established, report insufficient evidence instead of inventing a score.
Serialize selection/assignment updates within a pool so simultaneous requests do not reuse a stale round-robin cursor. This is coordination, not a promise of request-level quota reservations.
Recheck eligibility before committing a switch. Keep cooldown and minimum residency rules to prevent oscillation; expose why a candidate was skipped.

## Phase 3: one switching mechanism for both entry points

Use a small transition model with durable nonsecret state:

`requested -> waiting_for_safe_point -> validating_target -> stopping_old -> resuming_exact_conversation -> active`

Any uncertain outcome enters `needs_attention`; no eligible account enters `waiting_for_quota` or `notify_only` according to the workflow.
Persist enough identity and phase information to reconcile crashes. Do not store raw conversations or credentials in decision logs.

Only one switch may be active for a terminal. Repeated clicks/events must coalesce, and stale callbacks must not commit an old decision.
Cancellation before stopping leaves the original work running. Failure after stopping leaves a recoverable checkpoint; restart the old account only when its safety and eligibility are verified.
Never have both old and new executions continue the same work. Keep the terminal's layout position and explain its current state visibly.

## Phase 4: Workspace action and visual workflow editor

Add the manual switch action first to exercise the shared mechanism. Show current account, eligible choices, exclusion reasons, pending handoff and cancellation.
Unsupported terminals show a clear reason rather than an action that silently does nothing.

Build a bounded visual block editor, not a general scripting system. It must support pool selection, the three strategies, low/exhausted triggers, safe switch, wait/recheck and notify-only outcomes.
Persist the workflow as validated declarative data; the UI renders it and the runtime evaluates that data independently.
Reject missing targets, invalid thresholds, cross-environment accounts, conflicting branches and unrestricted cycles. Implement wait/recheck as a bounded state rather than allowing busy loops.
Provide keyboard-accessible block insertion, ordering and connection editing, clear labels and contrast. Include a dry-run explanation using synthetic quota snapshots without starting a CLI.
Allow saving and assigning workflows without changing the behavior of terminals that have not opted in.

## Verification and release gates

| Area | Required evidence |
| --- | --- |
| Selection | Multi-window limits, ties, expired snapshots, missing buckets, exhausted pools, removed accounts and concurrent assignments. |
| Monitoring | Shared queries, interval minimum, backoff, cancellation, clock/reset changes, and no quota work after the last opted-in terminal closes. |
| Continuation | Exact dialogue with same-directory terminals; current history format; unchanged effective model/settings; incompatible targets rejected. |
| Safety | In-flight tools, ambiguous interruption, stop failure, SSH disconnect, duplicate clicks, stale callbacks and crash recovery never start competing continuations. |
| Waiting | Known reset causes a fresh check; unknown/reset-delayed quota does not trigger speculative resume; notify-only remains idle. |
| UI | Manual switching with no policy; editor validation and keyboard operation; visible reasons; local/SSH state distinctions; terminal layout retained. |
| Data protection | No token copying, credential logging or raw conversation persistence; account/environment boundaries remain intact. |

Run focused tests during each phase, then the full suite on the work branch. UI changes require offscreen widget grabs with the Windows font directory configured.
Load the package-release skill before modifying process launch, runtime packaging, helper commands or package assets; validate source and packaged execution.
Update all three README feature lists, the Traditional Chinese guide, CHANGELOG and affected screenshots when the feature is actually implemented.
Record real Codex/SSH acceptance separately from automated fixtures. Do not label either environment supported solely because mocked tests pass.

## Development handoff and stop conditions

Use GPT-6-Astra for planning, GPT-6.1-sol with medium reasoning for implementation, and GPT-6-luna with high reasoning for clearly bounded simple tasks. Preserve the selected main-session model and effort.
The current documentation branch is `feat/dev_docs_quota_research`. Check status and branch before implementation; preserve these uncommitted documents and follow the repository branch policy. Do not commit, merge or push without a request.

The user has authorized starting the technical plan after selecting GPT-6.1-sol. Phase 0 evidence collection is underway; its offline protocol/TUI experiments are the next engineering step. All later phases remain conditional on that evidence.
Pause architecture-dependent implementation when exact continuation, effective settings or safe handoff cannot be demonstrated; report the failing capability and concrete alternatives.
Complete the feature only when both manual switching and the visual workflow meet the same safety and continuity criteria on local and SSH environments.

## Implementation work packages

File assignments below are proposed locations, not a reason to create every module before Phase 0 succeeds.

| Package | Main seams | Deliverable and focused proof |
| --- | --- | --- |
| A: official control experiment | Ignored scratch harness, version-specific schemas | Native TUI identity, control subscription and safe idle evidence; synthetic tests only initially. |
| B: normalized quota and policy | `runtime.py` quota parsing; proposed `quota_policy.py`; `tests/test_quota.py` | All applicable buckets, eligibility reasons and three deterministic strategies as pure functions. No UI or polling dependencies in selection. |
| C: execution control and handoff | `runtime.py`, `service.py`; proposed desktop `codex_control.py` | Correlated events, exact IDs, effective settings and exclusive transition ownership. Remote implementation stays standalone inside `runtime.py`. |
| D: persistence and monitor | `store.py`, `settings.py`, `ui_integrations.py`; proposed `quota_manager.py` | Pools/workflows/transition records and bounded shared monitoring. `prefs.py` holds editor view/layout preferences only. Read-only user DB check precedes schema edits. |
| E: manual switch | `workspace_page.py` `TerminalPane`, `ui.py`, locales | Switch action, eligible choices, state/reasons and cancellation using C; terminal position retained. |
| F: visual policy editor | Proposed `quota_policy_page.py`, `kit.py`, locales | Accessible block editing, validation, dry run, save/assign, and the same handoff engine as E. |
| G: acceptance and packaging | Focused tests, full suite, package self-test and public docs | Both entry points on local/SSH, no credential leakage, real test outcomes separately recorded. |

Implement A first. B's pure normalization/selection tests can be designed while A proceeds, but no user-visible automatic switching should ship without A and C.
C precedes manual switching E. D supplies monitoring and persistence; F consumes B/C/D rather than embedding execution in the diagram UI.
Add helpers/assets only through `selfcmd.py`, `entry.py` and the packaging spec as required; load package-release before these changes.

For initial workflow data, propose nodes with stable IDs, typed parameters and explicit edges. Triggers are `low_quota`, `quota_exhausted` and user-initiated switching; actions select a pool/strategy, request safe switching, wait/recheck or notify.
Store account IDs rather than secrets. Validate one selection and one terminal outcome per path, reachable nodes and bounded waits. Versioning/validation must follow the project's current-build policy rather than introducing legacy migrations.
Workflow editing does not grant arbitrary command execution. A preview uses synthetic snapshots and lists selection/exclusion reasons without touching live accounts.

Keep execution checkpoint identity distinct from layout slots, Launcher session ID, native thread ID and active turn ID. Persist transition ownership/revision so restarted launchers can reconcile state before sending actions.
Define new persistence fields and import/export behavior after A determines the actual identifiers. Export reusable pool/workflow configuration only after validating account references; omit live checkpoints, login state and capability secrets.
