# Confirmed hard-work execution

Read this reference after a Hard plan is strictly confirmed. It extends the single invocable `workflow-manager` Skill; it is not another Skill.

## Roles and runtime truth

- Keep the parent conversation on its high-reasoning coordination and review role. It owns contract integrity, dependency ordering, shared-resource scheduling, progress synthesis, failure decisions, and final acceptance. It must not perform the executor's mutations itself.
- Use exactly one child as the confirmed plan's contract executor. Other agents are not co-executors: during confirmed execution they may only run genuinely independent read-only investigation or review within the existing Direct/Focused/Complex/Extensive caps.
- Daily requests and Simple work keep their existing behavior. They do not create this executor contract. Existing positive-utility parallel routing remains unchanged outside the exclusive confirmed-plan mutation scope.

The Hook cannot change the parent model. A logical profile only requests a child handoff. Record requested, host-accepted, and host-observed model/effort separately. Host acceptance of explicit spawn fields proves the accepted request; matching runtime echo proves the observed profile. A Hook message, state field, attempted spawn, guessed product name, or absent echo is not proof that a model or effort took effect.

Daily/current behavior and the bound assessor's Simple two-stage flow remain unchanged. Only an explicit whole-session user policy such as “本会话全程最高模型和最高推理” activates `highest_throughout`; one-step, one-agent, current-task, “尽量”, or “高质量” requests do not. After strict Hard-plan confirmation, this preference changes only that plan's executor: resolve `highest_available` to the same currently available high model tier used for coordination/assessment and request the highest reasoning effort the host supports for that model. Never infer support or silently fall back; unresolved availability is `model_unavailable`, rejection is `spawn_failed`/`invalid_spawn_config`, and mismatching runtime echo is `start_mismatch`.

If the user explicitly restores the default, clear `highest_throughout` and invalidate every pending, running, or reusable execution contract created under it; stop further mutation under that contract. Re-resolve the default profile and create a fresh contract/request from still-valid plan bindings, or re-plan and reconfirm if a binding changed. Completed evidence remains evidence but cannot authorize new work under the invalidated contract.

## Resolve the executor profile

The default confirmed-Hard profile remains `work_executor_low_latest`: from overrides the current host exposes, choose the newest available lower-tier model relative to the high coordination/assessment tier and request `reasoning_effort=medium` exactly. For `highest_throughout`, use the high tier and host-max effort resolved above. Do not hard-code product names; if the resolved profile is unavailable, record `model_unavailable` and stop for parent reassessment rather than inventing or silently substituting a profile.

Spawn with all of these settings:

- the resolved explicit `model` override;
- the resolved effort: `medium` by default, or host-max for `highest_throughout`;
- because a model override is explicit, `fork_turns=none` or a positive integer, never an omitted/invalid override contract;
- one schema-safe ASCII `task_name` plus the normal concise Chinese purpose summary.

Host acceptance of this exact spawn is handoff-request evidence. Subagent start must match the pending request before mutation; report only the model/effort fields the host accepted or echoed, never assumed fields.

## Execution contract

Compute `execution_contract_id` from the execution-profile version, normalized resolved policy/profile, and all four bindings:

1. objective fingerprint;
2. difficulty decision ID;
3. positive `plan_generation`;
4. confirmed `plan_digest`.

Do not store or reconstruct the ID from raw prompt text. Any policy/profile, objective, difficulty decision, generation, or digest change makes the contract stale and requires a new executor request; changed plan bindings also require new confirmation.

The spawn request must include the exact `execution_contract_id`, `plan_digest`, and `plan_generation`; declare the child the only executor/exclusive owner; provide the full actionable confirmed plan, owned paths/modules, forbidden scope, dependencies and shared resources, expected artifacts, acceptance checks, rollback/stop conditions, and a compact result contract. This full handoff is required even with `fork_turns=none`; do not rely on implicit parent context.

The executor must return decisive changes, paths/identifiers, verification evidence, unresolved risk, and typed failure if any. Raw logs stay in files or bounded excerpts. The parent independently checks contract match and acceptance evidence before reporting success.

## Typed state and failure handling

Use the lifecycle `spawn_required → spawn_pending → running → succeeded`. Classify failures rather than collapsing them into an uninformative retry:

- `model_unavailable`: the resolved policy's eligible model/effort is not actually available;
- `invalid_spawn_config`: wrong/missing resolved policy/profile, model override, reasoning effort, fork context, contract marker, exclusivity, or acceptance;
- `spawn_failed`: the host rejected or failed the spawn;
- `start_mismatch`: the started child does not match the pending request;
- `stale_contract`: policy/profile/objective/difficulty/generation/digest no longer bind;
- `executor_failed`: uncategorized executor failure;
- `implementation_failed`, `build_failed`, `deploy_failed`, or `verification_failed`: stage-specific execution failure.

An initial attempt may receive at most one recovery attempt, so the total is at most two. Recovery is allowed only after a material correction to the recorded cause—for example resolving real model availability, correcting explicit spawn fields, repairing a stale binding through re-analysis/reconfirmation, or changing the implementation/verification route based on concrete diagnostics. Record the correction and new evidence.

Never repeat the same spawn or failed command unchanged, disguise an identical retry through another shell form, create a second simultaneous executor, let the parent take over mutations, or weaken acceptance. Without a material correction, or after the recovery attempt fails, set the executor state to `exhausted`, return control to high-reasoning parent assessment, and report the typed blocker.

## Migration and resume

Schema 10 adds executor state. When migrating a Schema 9 state whose plan was already `confirmed`, preserve the valid confirmed-plan binding but treat execution as not started: derive the current `execution_contract_id`, set `executor_state=spawn_required`, clear executor agent/model/reasoning/fork/attempt/failure fields, and request the one explicit executor. Never infer that a child was spawned or work was executed merely because the old plan was confirmed.

After compaction/resume preserve only normalized `highest_throughout`/default preference and typed contract evidence, never the original policy prompt. Re-resolve host availability before a new spawn. Reuse a running/succeeded executor only when native evidence, resolved policy/profile, and every binding still match; otherwise fail closed to `spawn_required`, `recovery_required`, or re-planning. Hook metadata does not replace native summary or host evidence.

After a succeeded executor, seal the bounded change and post-change verification baseline. If the user reports a remaining, returned, or new symptom during same-task acceptance, continue with [regression-continuity.md](regression-continuity.md) before any corrective mutation; executor success is not a causal conclusion or user acceptance.
