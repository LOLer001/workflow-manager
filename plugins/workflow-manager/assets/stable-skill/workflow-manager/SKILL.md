---
name: workflow-manager
description: Minimal Hard-work authorization, host-runtime evidence, and mounted-tree safety for Codex.
---

# Workflow Manager

Workflow Manager is a narrow authorization and evidence layer, not a second task runner. Current Codex owns planning, decomposition, progress judgment, normal recovery, compaction, model choice, and subagent scheduling. Do not make model prose, task naming, list shape, phase count, elapsed time, or output formatting into workflow gates.

## Route narrowly

- Daily and Simple work run natively with `Start=0`: no assessor, executor, or confirmation.
- Hard needs explicit evidence: production release/deployment, irreversible action, security/data loss, system-wide outage, host-continuity risk; or two strong groups including unknown-cause, cross-scope, or continuity. `production`, `core`, `customer-visible`, or `business-critical` labels alone do not upgrade a bounded known single-function bug with clear acceptance.
- Explicit exclusions or no-risk bounds—such as “do not modify, test, publish, or write Git”—are not positive Hard evidence. They do not erase genuine production-release, irreversible, or cross-scope evidence elsewhere in the same request.
- If uncertain, begin with bounded native read-only diagnosis and promote only when evidence crosses that threshold. See [work routing](references/work-routing.md).

## Keep only irreducible gates

1. **Host truth.** A bound child needs one unique PreTool request, matching PostTool acceptance with `host_accepted=true`, and one unique full Start. Request, acceptance, Start, and flattened state must agree on objective/contract, sequence, model, effort, and `fork_turns=1`. Missing, unknown, or rejected host acceptance is `model_unavailable`; other lifecycle conflicts are `start_mismatch`. Never infer these facts from child prose or flattened fields alone.
2. **Authorization scope.** Strict confirmation binds only the normalized objective plus explicit acceptance, risk category, and irreversible external action. Plan prose, layout, slice count, and manifest digest are not user authority. Same-envelope repair, autosplit, verification, recovery, and compaction successors inherit confirmation.
3. **Mutation ownership.** At most one live writer for a Hard contract. Children never nest, terminal children never revive, and parent/child writers never overlap.
4. **External safety.** Preserve mounted-tree Git restrictions and require explicit authority for a materially new irreversible external action.

Everything else is advisory. Let the model decide whether a plan needs one step or many, whether work is progressing, how to diagnose a failure, and how to present results. Byte/node budgets protect state capacity; they do not lower acceptance or impose item-count caps.

## Hard flow

- Spawn one read-only assessor exactly as `collaboration.spawn_agent(task_name=<safe ASCII>, fork_turns="1", model="gpt-5.6-sol", reasoning_effort="max", message=<read-only assessment>)`. Omit `agent_type` and `fork_context`; the task name is opaque. Each Hard envelope has one assessor slot and a failed lifecycle stays fail-closed.
- The assessor may answer with any nonempty bounded native result. No `WORK_ASSESSMENT`, JSON fence, fixed keywords, closing sentence, or minimum prose length is required. The host-bound lifecycle proves provenance; the parent model judges the content and writes one nonempty bounded native plan.
- Store the bounded parent plan in the private append-only journal. Tail seals never grant authority. A slice manifest is optional; missing or malformed data becomes one native slice. A valid manifest may expand within the 196608-byte / 1024-node budget with no item cap.
- Before confirmation, permit read-only diagnosis but deny mutation. A pure confirmation that arrives after assessor completion but before parent Stop is stored only as a digest receipt; preserve Hard/repair state and automatically bind it after the matching trusted revision commits. Do not ask the user to repeat it.
- After confirmation, exactly one writer owns the current slice. If no child writer is pending, live, or unknown and no causal/stall diagnosis is unfinished, the parent may atomically take the slice lease and implement, repair, verify, or publish directly. Otherwise it may start one bound child using a current lower-tier model at `medium`, `fork_turns=1`.
- The Hard parent remains `gpt-5.6-sol` at `max` and is the only summary, review, recovery, and final-acceptance entry. An execution child reports only four material events: location complete, mutation start, verification end, or a blocker.
- A child executor output is a candidate; any nonempty bounded native prose is valid and fixed result markers have no protocol authority. A parent-held lease instead binds its PostTool operations directly to the current contract/slice/attempt; a failed parent operation retains the same lease for correction. Taking over an old child candidate monotonically increments attempt and clears the old review candidate. Advance only with host-recorded bounded verification and parent review/Stop evidence; strong acceptance evidence is never optional.

## Recovery and liveness

- Failure, stall, incomplete, and verification recovery all enter through the high-reasoning parent. If the parent chooses a fresh execution child, it still uses a current lower-tier model at `medium`, `fork_turns=1`, and the original assessor lifecycle must remain valid.
- Recovery state is advisory: the parent may diagnose, verify, replan, or finish. A chosen child binds digest-only failure evidence to the root cause and correction. Reject only unchanged replay without new evidence or correction; otherwise monotonic sequences continue within byte/node budgets, with no attempt ceiling.
- Assessor progress is event-driven. Elapsed time, polling, or repeated status observations never create a workflow action, replacement assessor, timeout, or failure. See [assessment liveness](references/assessment-liveness.md).

## Continuity and safety

- Native summaries own ordinary compaction continuity. Replay plugin metadata or plan text only for a live Hard, causal-review, or reference-acceptance contract.
- A same-session trusted rollout may reconcile a missed parent Stop and later pure confirmation, but only with matching session/cwd/objective evidence; persist digests, never user or plan prose in state.
- Never run Git from CIFS, Samba, UNC, DrvFS, or another mounted working tree. Use an authoritative native Linux clone or supported remote Git path.
- User instructions, project-local skills, safety, and acceptance requirements take precedence. Read [confirmed execution](references/confirmed-execution.md) only when operating a confirmed Hard contract.
