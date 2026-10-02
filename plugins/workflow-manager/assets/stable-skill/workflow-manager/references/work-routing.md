# Work routing

Use this reference only to decide whether the narrow Hard authorization layer applies. Current Codex owns every ordinary execution decision.

## Decision order

1. The user's direct Simple route instruction takes effect immediately for the current task; do not run the difficulty classifier again.
2. Daily requests run natively.
3. High-confidence Simple engineering work runs natively with `Start=0`.
4. Hard requires that the current requested execution carries production release/deployment, irreversible, security/data-loss, system-wide outage, or host-continuity evidence; or at least two independent strong groups where one is unknown-cause, cross-scope, or continuity. Described topics, examples and completed work are input material.
5. If evidence is ambiguous, start with bounded native read-only diagnosis. Promote only after evidence crosses the Hard threshold.

Build/deploy/device work, many steps, length, shared resources, or vague wording alone do not make work Hard. Workflow Manager does not assign ordinary phases, agent counts, retry policy, progress format, or output shape.
Generating or editing documents, reports, drafts, translations and explanations stays native. Tables, quoted commands and historical material do not request their described operations. Preserve separately requested execution before or after the writing task, including explicit instructions to run supplied commands. A complete new writing objective does not inherit a prior Hard route; active writers and canonical plan controls keep their existing safety boundaries.
Text explicitly pointed back to as a passage to delete is an editorial target, not a request to perform its described engineering work. Keep independent instructions before or after that target in the risk assessment; report or draft labels alone never exempt a mixed request.
Historical conversation titles used only to locate prior context are not reference-fidelity requests. Direct engineering reference-fidelity requests keep their acceptance contract; ordinary document style and template references stay native.
`production`, `core`, `customer-visible`, and `business-critical` labels alone are not critical-production evidence. A known, bounded, reversible single-function bug with clear acceptance stays Simple/native: do not call an assessor or ask for plan confirmation.
Explicit exclusions or no-risk bounds—such as “do not modify, test, publish, or write Git”—are not positive Hard evidence. They do not cancel genuine production-release, irreversible, or cross-scope evidence elsewhere in the same request.

Examples:

| Request | Route |
|---|---|
| Generate today's report | Daily/native |
| Remove a draft passage describing reboot diagnosis, repair, and regression | Daily/native |
| Fix one known function and run its tests | Simple/native, `Start=0` |
| Compile, deploy, and run a bounded regression | Simple/native unless another Hard signal exists |
| Diagnose unknown repeated production reboot across modules | Hard |
| Publish an irreversible database migration with rollback | Hard |

## Hard assessment

Request one read-only `gpt-6-sol` assessor at `reasoning_effort=ultra`, `fork_turns=1`. Any concise safe ASCII task name is acceptable and carries no semantics. The current Hard authorization envelope has one assessor slot; a failed lifecycle remains fail-closed rather than starting a same-envelope replacement.

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

## User-selected Simple route

An explicit single-line instruction such as `按普通任务执行`, `降级判断为普通任务`, or `降级复核：<当前目标>` is a user route choice. The Hook marks the current objective Work/Simple immediately, without classifying the objective again or demanding another Hard confirmation. It atomically retires the prior Hard plan and contract, preserves the journal bytes for audit, clears prior confirmation and reference state, and opens a fresh task epoch. A confirmed plan or old writer lease does not prevent the route change. Old Hard children are isolated and their later tool actions are denied. The user's choice changes only Workflow Manager's route; mounted-tree Git restrictions and external safety boundaries still apply. Questions, quotations, conditionals, and vague discussion are not route commands.
