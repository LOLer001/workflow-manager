---
name: workflow-manager
description: Manage quality-first Codex workflows with daily/work and simple/hard routing, plan confirmation, compaction continuity, duplicate avoidance, and agent scheduling without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

## Quality invariant

- Correctness and required reasoning, evidence, safety, and acceptance outrank savings.
- Remove redundancy, never required stages, evidence, corrections, or checks.
- Pressure may change order or checkpoint timing, never the depth needed to solve the task.

## Route and run

1. Classify **Daily** (chat, weather, reports, personal help, cleanup) versus **Work** (device changes/bugs, App/code, engineering delivery/diagnosis). Inseparable engineering output is Work. Daily keeps current settings and safety.
2. For Work, request `work_assessment`: the highest available model/reasoning decide **Simple** or **Hard**. It is advisory; never claim the host switched or verified a model.
3. Simple has bounded cause/scope and clear verification. Solve it under assessment with no plan-confirmation round; normal safety gates still apply.
4. Hard covers unknown cause, cross-module/architecture, device change, shared resources, or long delivery. Gather read-only evidence, then plan modules/files/methods, changes, ownership, delivery, verification, risk, and rollback. End with `计划已就绪，等待确认后执行`.
5. Before strict Hard-plan confirmation, allow read-only evidence; block explicit writes, mutating children/Git, compilation, packaging, deployment, and device mutation. Changed constraints invalidate it. Read [references/work-routing.md](references/work-routing.md) for Hard routing.
6. After confirmation read [references/confirmed-execution.md](references/confirmed-execution.md). The high-reasoning parent coordinates/reviews; one contract executor uses the newest actually available lower-tier Codex model with `reasoning_effort=medium`. Bind `execution_contract_id` to objective/difficulty/plan generation/digest. Only a host-accepted explicit override proves switching; typed failures get one materially corrected recovery, never an identical retry.
7. Independently route **Direct**, **Focused**, or **Complex/Extensive** execution shape. Use only relevant `Contract > Evidence > Change > Verify > Report` stages.
8. Follow-ups inherit valid bindings; reclassify new objectives. Reuse native summaries/plans and unchanged evidence.
9. With tools, update `phase | done | next | blocker` only at kickoff, material change, or ~60s wait.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% checkpoint and stop broad exploration, then continue needed evidence narrowly.
- After compaction resume native summary and gates; never reconstruct finished work.
- Reclassify on new phases, repeated failures, long builds, or large output. Pressure never justifies delegation; make compaction safe.

## Delegation

Optimize critical-path time. Launch ready positive-utility lanes only when time saved exceeds coordination/collision cost.

- Before spawning note `deliverable | ready | owner | resource | time saved`. Read-only investigation is only one option; owned edits, tests, research, reproduction, and review qualify.
- Parent owns integration/shared state. Treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3.
- Serialize only overlapping edits or shared delivery/device stages; unrelated lanes may run. Pending Hard plans permit only read-only children.
- Confirmed Hard has one contract executor; only read-only side lanes continue within caps. Stay local when tiny, unready, overlapping, slower, or opted out.
- Give each child a concise Chinese purpose summary and safe ASCII `task_name`; state scope, ownership, and result shape. Reuse a live agent for one bounded follow-up.

## Continuity

- Reuse success only when inputs, files, state, freshness, and evidence are unchanged; never reuse failure.
- Native summaries own semantics; Hook hints do not. Added constraints preserve compatible evidence but invalidate the plan; new-objective results are validation-only.

## Upgrade continuity

- Migrate paths through a supported host API; never edit rollout JSONL or live databases/indexes/tasks.
- Else install `$CODEX_HOME/skills/workflow-manager`; verify new/resumed tasks load its `SKILL.md`.
- Keep old caches until either route covers all tasks. New writer/state/hook fallback is insufficient; fail open and report gaps.

## Output and command guards

- Bound output by exact paths/patterns/ranges or full-log redirection. Budget large status, logs, recordings, and frames.
- Before mutation, preflight only unverified paths, inputs, and acceptance sources.
- Obey Hook denials; do not retry an equivalent unbounded route.
- Keep the first error; diagnose once; retry only after correction or one bounded alternate. Same-cause repetition or ~25 stage actions requires `checkpoint > reclassify`.
- Run one acceptance loop per unchanged revision unless criteria require more. Preserve oversized results; ignore unrelated history.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL mounts. Use `android-remote-git`, an authoritative Linux tree, or a safe non-mounted terminal.

## Truthfulness and boundaries

- In 1.0.23, profiles request policy only; the Hook cannot switch the parent, and only host acceptance of an explicit child override is switch evidence.
- Availability/agent count do not prove effectiveness; agents may cut main-thread noise but raise total tokens. PreToolUse is not security.
- Quality is the release gate. Savings remove redundancy, never required reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required gates take precedence.
