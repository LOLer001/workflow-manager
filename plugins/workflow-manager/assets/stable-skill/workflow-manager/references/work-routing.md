# Work routing

Use this reference only to decide whether the narrow Hard authorization layer applies. Current Codex owns every ordinary execution decision.

## Decision order

1. Daily requests run natively.
2. High-confidence Simple engineering work runs natively with `Start=0`.
3. Hard requires either one critical production/irreversible/security/full-system/host-continuity signal, or at least two independent strong groups where one is unknown-cause, cross-scope, or continuity.
4. If evidence is ambiguous, start with bounded native read-only diagnosis. Promote only after evidence crosses the Hard threshold.

Build/deploy/device work, many steps, length, shared resources, or vague wording alone do not make work Hard. Workflow Manager does not assign ordinary phases, agent counts, retry policy, progress format, or output shape.

Examples:

| Request | Route |
|---|---|
| Generate today's report | Daily/native |
| Fix one known function and run its tests | Simple/native, `Start=0` |
| Compile, deploy, and run a bounded regression | Simple/native unless another Hard signal exists |
| Diagnose unknown repeated production reboot across modules | Hard |
| Publish an irreversible database migration with rollback | Hard |

## Hard assessment

Request one read-only highest-available assessor at `reasoning_effort=max`, `fork_turns=1`; only explicit whole-session `highest_throughout` uses `ultra`. Any concise safe ASCII task name is acceptable and carries no semantics.

The assessor may return ordinary prose. Do not require a binding line, exact keywords, JSON, numbered table, fixed ending, or plugin marker. Its unique request + accepted Post + full Start establish provenance; the parent model judges the substance and writes the only plan.

Before a confirmed plan exists, allow targeted reads and diagnosis but deny mutation, build/deploy, mutating Git, destructive device/external action, and write-authority children.

## Parent native plan

The parent presents one human-readable plan sufficient for the task and acceptance. It may choose one step, 3–5 slices, or a longer structure. No list or slice count is independently gated.

The Hook appends the complete bounded plan to `plans/<session-token>/hard-plan.md`. Before `plan_state` may become `awaiting_confirmation`, the revision and state transaction must commit. The current trusted revision is the plan-content authority. `update_plan` may only be a UI projection with `projection_only canonical_revision_digest=<digest>`; it is not a second plan store.

A machine-readable `workflow-manager-execution-slices` block is optional. Without one, the complete native plan becomes one logical slice. With one, total 196608-byte / 1024-node budgets protect state capacity without a separate item cap. Budget pressure may stop or split work but never reduce acceptance.

## Confirmation

Accept an unambiguous pure confirmation of the presented plan. Confirmation binds only objective plus explicit acceptance, risk category, and irreversible external action. It does not bind wording, layout, slices, or manifest digest. Same-envelope repair, autosplit, verification, recovery, and compaction inherit it.

If pure confirmation arrives after the assessor completes but before parent Stop lands, preserve the pending plan, repair, Hard route, and assessor lifecycle. Persist a host-bound confirmation-receipt digest only and automatically bind it after the matching trusted revision commits. Do not reset to Daily and do not ask the user to repeat confirmation.

A material change to objective, explicit acceptance, risk category, or irreversible external action needs a new confirmation. Ordinary plan refinement within the same envelope does not.
