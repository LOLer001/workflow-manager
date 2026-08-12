# Confirmed hard-work execution

Read this reference after a Hard plan is strictly confirmed. It extends the single invocable `workflow-manager` Skill; it is not another Skill.

## Roles and runtime truth

- Keep the parent conversation on its high-reasoning coordination and review role. It owns contract integrity, dependency ordering, shared-resource scheduling, progress synthesis, failure decisions, and final acceptance. It must not perform the executor's mutations itself.
- Use exactly one child as the confirmed plan's contract executor. Other agents are not co-executors: during confirmed execution they may only run genuinely independent read-only investigation or review within the existing Direct/Focused/Complex/Extensive caps.
- Daily requests and Simple work keep their existing behavior. They do not create this executor contract. Existing positive-utility parallel routing remains unchanged outside the exclusive confirmed-plan mutation scope.

The Hook cannot change the parent model. A logical `work_executor_low_latest` profile only requests a handoff. The sole switching evidence is that the host accepts a spawn containing an explicit model override; a Hook message, state field, attempted spawn, or model name guessed from documentation is not proof.

## Resolve the executor profile

From the Codex model overrides the current host actually exposes, choose the newest available lower-tier Codex model relative to the parent. Do not hard-code Luna, Terra, a dated alias, or any other product name: availability and ordering can change. If the host exposes no eligible lower-tier override, record `model_unavailable` and stop for parent reassessment; never silently reuse the parent model or invent an identifier.

Spawn with all of these settings:

- the resolved explicit `model` override;
- `reasoning_effort=medium` exactly;
- because a model override is explicit, `fork_turns=none` or a positive integer, never an omitted/invalid override contract;
- one schema-safe ASCII `task_name` plus the normal concise Chinese purpose summary.

Host acceptance of this exact spawn is model-handoff evidence. Subagent start must then match the pending executor request before mutation is allowed.

## Execution contract

Compute `execution_contract_id` from the execution-profile version plus all four bindings:

1. objective fingerprint;
2. difficulty decision ID;
3. positive `plan_generation`;
4. confirmed `plan_digest`.

Do not store or reconstruct the ID from raw prompt text. Any objective, difficulty decision, generation, or digest change makes the contract stale and requires a new confirmation and executor request.

The spawn request must include the exact `execution_contract_id`, `plan_digest`, and `plan_generation`; declare the child the only executor/exclusive owner; provide the full actionable confirmed plan, owned paths/modules, forbidden scope, dependencies and shared resources, expected artifacts, acceptance checks, rollback/stop conditions, and a compact result contract. This full handoff is required even with `fork_turns=none`; do not rely on implicit parent context.

The executor must return decisive changes, paths/identifiers, verification evidence, unresolved risk, and typed failure if any. Raw logs stay in files or bounded excerpts. The parent independently checks contract match and acceptance evidence before reporting success.

## Typed state and failure handling

Use the lifecycle `spawn_required → spawn_pending → running → succeeded`. Classify failures rather than collapsing them into an uninformative retry:

- `model_unavailable`: no eligible actually available lower-tier model;
- `invalid_spawn_config`: wrong/missing model override, reasoning effort, fork context, contract marker, exclusivity, or acceptance;
- `spawn_failed`: the host rejected or failed the spawn;
- `start_mismatch`: the started child does not match the pending request;
- `stale_contract`: objective/difficulty/generation/digest no longer bind;
- `executor_failed`: uncategorized executor failure;
- `implementation_failed`, `build_failed`, `deploy_failed`, or `verification_failed`: stage-specific execution failure.

An initial attempt may receive at most one recovery attempt, so the total is at most two. Recovery is allowed only after a material correction to the recorded cause—for example resolving real model availability, correcting explicit spawn fields, repairing a stale binding through re-analysis/reconfirmation, or changing the implementation/verification route based on concrete diagnostics. Record the correction and new evidence.

Never repeat the same spawn or failed command unchanged, disguise an identical retry through another shell form, create a second simultaneous executor, let the parent take over mutations, or weaken acceptance. Without a material correction, or after the recovery attempt fails, set the executor state to `exhausted`, return control to high-reasoning parent assessment, and report the typed blocker.

## Migration and resume

Schema 10 adds executor state. When migrating a Schema 9 state whose plan was already `confirmed`, preserve the valid confirmed-plan binding but treat execution as not started: derive the current `execution_contract_id`, set `executor_state=spawn_required`, clear executor agent/model/reasoning/fork/attempt/failure fields, and request the one explicit executor. Never infer that a child was spawned or work was executed merely because the old plan was confirmed.

After compaction or resume, use the same rule. Reuse a running/succeeded executor only when native evidence and every contract binding still match; otherwise fail closed to `spawn_required`, `recovery_required`, or re-planning as appropriate. Hook metadata guides recovery but does not replace the native task summary or host tool evidence.
