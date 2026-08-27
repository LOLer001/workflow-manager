# Confirmed Hard execution

This reference applies only after a Hard plan is confirmed. Schema 30/writer 1.0.53 uses execution profile v11 and an append-only canonical journal v3.

## Minimal authority model

Only four boundaries grant or deny mutation:

1. a valid Hard authorization envelope;
2. exact host lifecycle truth for the current child;
3. one live writer with no child nesting or terminal-child revival;
4. unchanged external-safety authority.

Task names and request prose are opaque host data. Any concise safe ASCII `task_name` is valid; it does not encode the contract, slice, attempt, failure fingerprint, or review digest. Native plan, executor, and review prose need no plugin marker. Explicit malformed marker intent still fails closed, because it is contradictory evidence rather than a missing format.

## Host lifecycle truth

For assessor, executor, and typed recovery, resolve one original lifecycle from three independent sources:

- one unique PreTool request;
- its matching PostTool receipt with explicit `host_accepted=true`;
- one unique full Start.

These sources and flattened state must agree on current objective/contract, monotonic sequence, requested and observed model, reasoning effort, and `fork_turns=1`. Do not infer missing facts from flattened fields, child messages, partial events, or later summaries. Missing, unknown, or rejected host acceptance maps to `model_unavailable`; every other request/Start/state conflict maps to `start_mismatch`.

The Hook may privately inject the verified plan and host-issued `execution_contract_id` at Start without reading plugin state from the child. A Start seen before Post remains mutation-locked; only the later exact Post plus an in-lock journal recheck can make it running.

If Desktop omits `SubagentStop` but a parent `list_agents` result exposes one structured `completed` mailbox entry for the uniquely bound executor, the Hook may append a separate `mailbox_terminal` equivalent boundary. It requires the current request, accepted Post, full Start, agent/task, contract, slice, and attempt to agree and the completed body to end with one exact matching `EXECUTION_RESULT`. It never fabricates or increments `SubagentStop`; `wait_agent` update summaries, running snapshots, commentary, ordinary unbound agents, duplicates, ambiguity, malformed markers, and drift remain non-terminal.

## Native plan and journal

The parent writes one bounded ordinary plan to private plugin data at `plans/<session-token>/hard-plan.md`. Before `plan_state` becomes `awaiting_confirmation`, the current trusted revision must commit successfully. The journal alone never grants authority. Objective, revision digest, journal digest, confirmation envelope, execution contract, and current ownership must also agree.

Plan formatting is native Codex behavior. A `workflow-manager-execution-slices` JSON block is optional:

- without one, the Hook projects the complete parent plan as one logical slice;
- with one, the plan may use any useful number of slices within the inclusive 196608-byte / 1024-node total budget;
- 3–5 slices are a normal model choice, not a gate;
- no separate slice or list-count ceiling exists;
- insufficient budget means stop or split, never weaken acceptance.

The current trusted executable revision is the plan-content authority. `update_plan` is only a digest-bound UI projection. v3 terminal seals and durable conclusions append after the immutable revision prefix and cannot rewrite a completed revision or grant execution authority. The v11 contract binds the selected executable revision plus that immutable prefix, so an allowed tail record does not invalidate an already sealed contract. An external edit, replacement, link race, wrong path identity, or digest mismatch makes the active contract stale.

One revision may contain exactly 983040 UTF-8 bytes and the journal exactly 10485760 bytes. One byte over produces `revision_too_large` or `journal_full` without consuming a generation. Journal/state publication follows `marker → journal → state → cleanup`; recovery accepts only old journal/old state or new journal/new state. Mixed or unprovable states fail closed.

## Confirmation continuity

Strict confirmation binds a normalized authorization envelope containing only:

- objective;
- explicit acceptance;
- risk category;
- irreversible external action.

It does not bind plan prose, slice layout, or manifest digest. Repair, autosplit, verification, typed recovery, and compaction successors inherit confirmation while that envelope is unchanged. Only a material change to one of its four fields requires new confirmation.

If pure confirmation arrives after the assessor completes but before parent Stop commits the plan, retain the Hard route, assessor binding, pending plan, and repair state. Store only a host-bound confirmation-receipt digest. Automatically bind it after the matching trusted revision commits; never ask the user to resend it.

Desktop may omit Hook delivery for parent Stop or a programmatic delegated confirmation. A bounded same-session rollout may reconcile them only when session identity, cwd fingerprint, parent `task_complete`, later pure confirmation, and objective continuity are unique and consistent. Persist only digests; cross-session, ambiguous, or prose-only inference is rejected.

## Executor and parent review

The default first executor uses a current lower-tier model at `medium`, `fork_turns=1`. Explicit whole-session `highest_throughout` uses the highest available model at `ultra`. A request is reserved from trusted state; copied contract text in its message is neither required nor authoritative.

Executor Stop may be ordinary nonempty native prose. `EXECUTION_RESULT` is optional; an exact line remains compatible. If marker-like intent is present and malformed, duplicated, or bound to the wrong contract, fail closed. A successful result becomes only `verification_required`, and only when the host operation ledger contains bounded verification for the current contract. Child claims cannot substitute for that evidence.

The high-reasoning parent independently reviews artifacts and acceptance evidence. Its Stop may also be ordinary nonempty native prose. `EXECUTION_REVIEW` is optional. A pass requires current candidate binding plus independent host-recorded verification. Missing host verification remains incomplete even when prose sounds confident. Another slice advances only after review; only the final accepted slice seals global `succeeded`.

For shell verification, preserve the complete structured host result, including the underlying exit status. For `apply_patch`, bind the exact host call to its unique success receipt. If Desktop omits only the generic status, a PostToolUse mutation bound to the exact executor, contract, slice, input, command, and turn may count as change evidence only after independent parent verification succeeds; it does not force another recovery child. Multiple calls bind one-to-one; missing, duplicate, conflicting, cross-turn, or ambiguous evidence remains unknown.

## Typed recovery

All failure, stall, incomplete, and verification recovery uses the same production path. After the unique original assessor lifecycle is proven, typed recovery uses the current highest available `gpt-5.6-sol` at `max`, `fork_turns=1`; explicit `highest_throughout` remains highest plus `ultra`.

At each boundary the Hook derives one host-generated evidence digest and failure fingerprint from lifecycle, terminal, operation-ledger, and review facts. Never search state files or infer them from child prose. Recovery state does not force another turn or child: the parent may diagnose, independently verify, replan, or finish natively. Only if the model chooses an encrypted or plaintext recovery spawn, the parent supplies those Hook-issued facts plus the diagnosed root cause and material correction. The Hook persists only digests and atomically reserves that fresh child inside the existing authorization envelope; this is not another confirmation.

An exact recovery delegation may race ahead of the matching mailbox terminal observation. While the current executor remains uniquely live, retain only a contract/task/agent/attempt-bound `terminal_pending` digest record with no spawn or mutation authority. Promote it only when the later completed boundary reports the same typed failure fingerprint and evidence digest; if the terminal boundary arrives first, a later exact recovery binds normally. Conflicts are discarded fail-closed and raw prompt, result, root-cause, and correction text are never persisted.

Recovery sequence is positive, monotonic, and limited only by bounded state byte/node budget. Reject an unchanged replay of the same failure fingerprint when it has no new evidence, root cause, progress, or material correction. Allow new evidence, a diagnosed root cause, a material correction, or a different fingerprint. Three or more distinct failure fingerprints are valid. Never revive a terminal child, use `followup_task` on it, nest an executor, overlap the writer, repeat an unchanged failed method, or lower acceptance.

## Migration and resume

Historical sealed success preserves its real profile/contract. Active old-profile write authority does not silently become v11; it must acquire a current trusted plan and lifecycle. Schema 29/writer 1.0.51 or Schema 30/writer 1.0.52 profile-v11 running continuity may migrate lazily only with its exact live request/Post/full-Start binding intact. Compaction preserves only bounded bindings, digests, sequences, and evidence—not prompt, plan, child-result, diagnosis, or review prose.

Native summaries own ordinary continuity. A same-session resume may reconcile a verified host compaction window, but compaction evidence never grants mutation or replaces parent review.
