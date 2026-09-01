---
name: workflow-manager
description: Minimal Hard-work authorization, host-runtime evidence, and mounted-tree safety for Codex.
---

# Workflow Manager

Workflow Manager is a narrow authorization and evidence layer, not a second task runner. Current Codex owns planning, decomposition, progress judgment, normal recovery, compaction, model choice, and subagent scheduling. Do not make model prose, task naming, list shape, phase count, elapsed time, or output formatting into workflow gates.

## Route narrowly

- Daily and non-Hard engineering work run natively. High-confidence Simple work has `Start=0`: no assessor and no executor created by this plugin.
- Treat work as Hard only with high confidence: one critical production, irreversible, security, full-system, or host-continuity risk; or two independent strong signal groups where one is unknown-cause, cross-scope, or continuity. Build/deploy/device work, task length, multiple steps, shared resources, or ambiguity alone are not Hard.
- If uncertain, begin with bounded native read-only diagnosis and promote only when evidence crosses that threshold. See [work routing](references/work-routing.md).

## Keep only irreducible gates

1. **Host truth.** A bound child needs one unique PreTool request, matching PostTool acceptance with `host_accepted=true`, and one unique full Start. Request, acceptance, Start, and flattened state must agree on objective/contract, sequence, model, effort, and `fork_turns=1`. Missing, unknown, or rejected host acceptance is `model_unavailable`; other lifecycle conflicts are `start_mismatch`. Never infer these facts from child prose or flattened fields alone.
2. **Authorization scope.** Strict confirmation binds only the normalized objective plus explicit acceptance, risk category, and irreversible external action. Plan prose, layout, slice count, and manifest digest are not user authority. Same-envelope repair, autosplit, verification, recovery, and compaction successors inherit confirmation.
3. **Mutation ownership.** At most one live writer for a Hard contract. Children never nest, terminal children never revive, and parent/child writers never overlap.
4. **External safety.** Preserve mounted-tree Git restrictions and require explicit authority for a materially new irreversible external action.

Everything else is advisory. Let the model decide whether a plan needs one step or many, whether work is progressing, how to diagnose a failure, and how to present results. Byte/node budgets protect state capacity; they do not lower acceptance or impose item-count caps.

## Hard flow

- Spawn one read-only assessor exactly as `collaboration.spawn_agent(task_name=<safe ASCII>, fork_turns="1", model="gpt-5.6-sol", reasoning_effort="max", message=<read-only assessment>)`. Omit `agent_type` and `fork_context`; only `highest_throughout` uses `ultra`. Its safe ASCII `task_name` is an opaque host label.
- The assessor may answer with any nonempty bounded native result. No `WORK_ASSESSMENT`, JSON fence, fixed keywords, closing sentence, or minimum prose length is required. The host-bound lifecycle proves provenance; the parent model judges the content and writes one nonempty bounded native plan.
- Store the complete bounded parent plan in the private append-only canonical journal. v3 typed terminal seals and durable conclusions may follow the immutable executable-revision prefix; they never grant execution authority. A machine-readable slice manifest is optional. If absent, the Hook treats the native plan as one logical execution slice; if present, it may expand within the total 196608-byte / 1024-node budget with no independent list or slice cap. A normal model-generated plan may still choose 3–5 slices.
- Before confirmation, permit read-only diagnosis but deny mutation. A pure confirmation that arrives after assessor completion but before parent Stop is stored only as a digest receipt; preserve Hard/repair state and automatically bind it after the matching trusted revision commits. Do not ask the user to repeat it.
- After confirmation, exactly one writer owns the current slice. If no child writer is pending, live, or unknown and no causal/stall diagnosis is unfinished, the parent may atomically take the slice lease and implement, repair, verify, or publish directly. Otherwise it may start one bound child: the default child uses a current lower-tier model at `medium`, `fork_turns=1`; explicit `highest_throughout` uses highest available at `ultra`.
- A child executor output is a candidate; ordinary native prose is valid and `EXECUTION_RESULT` is optional. A parent-held lease instead binds its PostTool operations directly to the current contract/slice/attempt; a failed parent operation retains the same lease for correction. Taking over an old child candidate monotonically increments attempt and clears the old review candidate. Advance only with host-recorded bounded verification and parent review/Stop evidence; `EXECUTION_REVIEW` is optional, but strong acceptance evidence is never optional.

## Recovery and liveness

- Failure, stall, incomplete, and verification recovery all use the same host-truth path. With a valid original assessor lifecycle, recovery uses `gpt-5.6-sol`, `max`, `fork_turns=1`; `highest_throughout` remains highest plus `ultra`.
- Recovery state is advisory to native model judgment: the parent may diagnose, verify, replan, or finish without manufacturing another turn. Only when the model chooses a fresh recovery child, bind the Hook's digest-only failure/evidence facts to the diagnosed root cause and material correction. Reject only an unchanged child replay of the same failure fingerprint with no new evidence, root cause, or correction. Different fingerprints and genuine evidence/correction may continue for three or more monotonic sequences while byte/node budgets permit. Never use a fixed attempt ceiling.
- A live assessor has no total wall-clock deadline. At 600 seconds observe activity; exactly 1200 seconds remains live. Strictly after 1200 seconds without progress, diagnose, unblock, or split the current step. Do not turn elapsed time into `assessment_timeout`, `blocked`, or exhausted. See [assessment liveness](references/assessment-liveness.md).

## Continuity and safety

- Native summaries own ordinary compaction continuity. Replay plugin metadata or plan text only for a live Hard, causal-review, or reference-acceptance contract.
- A same-session trusted rollout may reconcile a missed parent Stop and later pure confirmation, but only with matching session/cwd/objective evidence; persist digests, never user or plan prose in state.
- Never run Git from CIFS, Samba, UNC, DrvFS, or another mounted working tree. Use an authoritative native Linux clone or supported remote Git path.
- User instructions, project-local skills, safety, and acceptance requirements take precedence. Read [confirmed execution](references/confirmed-execution.md) only when operating a confirmed Hard contract.
