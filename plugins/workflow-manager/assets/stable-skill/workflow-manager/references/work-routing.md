# Work routing

Use this reference only to decide whether the narrow Hard authorization layer applies. Current Codex owns every ordinary execution decision.

## Decision order

1. Daily requests run natively.
2. High-confidence Simple engineering work runs natively with `Start=0`.
3. Hard requires explicit production release/deployment, irreversible, security/data-loss, system-wide outage, or host-continuity evidence; or at least two independent strong groups where one is unknown-cause, cross-scope, or continuity.
4. If evidence is ambiguous, start with bounded native read-only diagnosis. Promote only after evidence crosses the Hard threshold.

Build/deploy/device work, many steps, length, shared resources, or vague wording alone do not make work Hard. Workflow Manager does not assign ordinary phases, agent counts, retry policy, progress format, or output shape.
`production`, `core`, `customer-visible`, and `business-critical` labels alone are not critical-production evidence. A known, bounded, reversible single-function bug with clear acceptance stays Simple/native: do not call an assessor or ask for plan confirmation.
Explicit exclusions or no-risk bounds—such as “do not modify, test, publish, or write Git”—are not positive Hard evidence. They do not cancel genuine production-release, irreversible, or cross-scope evidence elsewhere in the same request.

Examples:

| Request | Route |
|---|---|
| Generate today's report | Daily/native |
| Fix one known function and run its tests | Simple/native, `Start=0` |
| Compile, deploy, and run a bounded regression | Simple/native unless another Hard signal exists |
| Diagnose unknown repeated production reboot across modules | Hard |
| Publish an irreversible database migration with rollback | Hard |

## Hard assessment

Request one read-only `gpt-5.6-sol` assessor at `reasoning_effort=max`, `fork_turns=1`. Any concise safe ASCII task name is acceptable and carries no semantics. The current Hard authorization envelope has one assessor slot; a failed lifecycle remains fail-closed rather than starting a same-envelope replacement.

The assessor may return ordinary prose. Do not require a binding line, exact keywords, JSON, numbered table, fixed ending, or plugin marker. Its unique request + accepted Post + full Start establish provenance; the parent model judges the substance and writes the only plan.

Before a confirmed plan exists, allow targeted reads and diagnosis but deny mutation, build/deploy, mutating Git, destructive device/external action, and write-authority children.

## Parent native plan

The parent presents one human-readable plan sufficient for the task and acceptance. It may choose one step, 3–5 slices, or a longer structure. No list or slice count is independently gated.

The Hook appends the complete bounded plan to `plans/<session-token>/hard-plan.md`. Before `plan_state` may become `awaiting_confirmation`, the revision and state transaction must commit. The current trusted revision is the plan-content authority. After a fully bound assessor completes but before revision 1 commits, a bounded parent-native `update_plan` may project the pending plan into the UI without changing state or authority; the following parent Stop still creates revision 1. Once a canonical revision exists, `update_plan` is allowed only as `projection_only canonical_revision_digest=<digest>`. It is never a second plan store.

A machine-readable `workflow-manager-execution-slices` block is optional. Without a valid one, the complete native plan becomes one logical slice; malformed projection data is not a format gate. With a valid one, total 196608-byte / 1024-node budgets protect state capacity without a separate item cap. Budget pressure may stop or split work but never reduce acceptance.

## Confirmation

Accept an unambiguous semantic confirmation of the presented plan without prescribing an exact phrase. A committed plan awaiting confirmation accepts bounded contextual assent such as “可以” or “yes” as well as explicit execution intent. Before the canonical revision exists, only explicit execution intent may create an early receipt. Negation, conditions, questions, quotation or retelling, code blocks, and scope changes never confirm.

Confirmation binds only objective plus explicit acceptance, risk category, and irreversible external action. It does not bind wording, layout, slices, or manifest digest. Same-envelope repair, autosplit, verification, recovery, and compaction inherit it.

If pure confirmation arrives after the assessor completes but before parent Stop lands, preserve the pending plan, repair, Hard route, and assessor lifecycle. Persist a host-bound confirmation-receipt digest only and automatically bind it after the matching trusted revision commits. Do not reset to Daily and do not ask the user to repeat confirmation.

A material change to objective, explicit acceptance, risk category, or irreversible external action needs a new confirmation. Ordinary plan refinement within the same envelope does not.
