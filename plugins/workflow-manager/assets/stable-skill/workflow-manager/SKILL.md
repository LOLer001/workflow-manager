---
name: workflow-manager
description: Manage quality-first Codex workflows with daily/work and simple/hard routing, plan confirmation, compaction continuity, duplicate avoidance, and agent scheduling without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

## Quality invariant

- Correctness and required reasoning, evidence, safety, reproducibility, and acceptance outrank savings.
- Remove redundancy, never needed stages, evidence, corrected retries, or checks.
- Pressure may change order or checkpoint timing, never the depth needed to solve the task.

## Route and run

1. Classify **Daily** (chat, weather, reports, personal help, cleanup) versus **Work** (device changes/bugs, App/code, engineering delivery/diagnosis). Mixed requests with inseparable engineering output are Work. Daily keeps current settings; safety is unchanged.
2. For Work, request `work_assessment`: the highest available model/reasoning decide **Simple** or **Hard**. It is advisory; never claim the host switched or verified a model.
3. Simple has bounded cause/scope and clear verification. Solve it immediately under assessment; add no plan-confirmation round. Normal destructive/external confirmations still apply.
4. Hard includes unknown cause, cross-module/architecture, device change, shared resources, or a long delivery chain. Gather read-only evidence, then give a numbered plan with modules/files/methods, changes, ownership, delivery, verification, risk, and rollback. End with `计划已就绪，等待确认后执行`.
5. Before strict confirmation of the current Hard plan, allow read-only evidence; block explicit writes, mutating children/Git, compilation, packaging, deployment, and device mutation. Constraint/scope/objective/plan changes invalidate it. Read [references/work-routing.md](references/work-routing.md) before Hard routing or gating.
6. Independently route execution shape: **Direct** answer/small edit; **Focused** one causal chain; **Complex/Extensive** lane audit at kickoff and new phase boundaries.
7. Use only relevant stages: `Contract > Evidence > Change > Verify > Report`. Follow-ups inherit valid domain/difficulty/plan bindings; reclassify a new objective.
8. Reuse native summaries, plans, and unchanged success. Search exact paths/patterns; expand only when evidence requires it.
9. With tools, send one kickoff; update `phase | done | next | blocker` only on material change or ~60s wait.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% checkpoint and stop broad exploration, then continue required evidence narrowly or after compaction.
- After compaction resume the native summary and gates; never reconstruct finished work.
- Reclassify on new phases, repeated failures, long builds, or large output. Pressure never justifies delegation; make compaction safe.

## Delegation

Optimize critical-path time. Launch ready positive-utility lanes only when time saved exceeds coordination/collision cost.

- Before spawning, note `deliverable | ready | write owner | shared resource | time saved`; the parent is one lane. Read-only investigation is only one option; exclusive edits, tests, research, reproduction, and review qualify.
- Parent owns integration/shared state. Treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3.
- Serialize only overlapping edits or shared delivery/device stages; unrelated lanes may run. Pending Hard plans permit only explicitly read-only children.
- Delegate writes only with exclusive ownership and an open plan gate. Stay local when tiny, unready, inseparable, overlapping, slower, or opted out.
- Give each child a concise Chinese purpose summary and safe ASCII `task_name`; state scope, ownership, and result shape. Reuse a live agent for one bounded follow-up.

## Continuity

- Reuse success only when inputs, files, external state, freshness, and evidence are unchanged; never reuse failed work.
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

- In 1.0.22, `current` and `work_assessment` request policy only; Hook output cannot prove a model or reasoning switch.
- Hook availability and agent count do not prove effectiveness. Agents may reduce main-thread noise but raise total tokens; PreToolUse is not a security boundary.
- Workflow quality is the release gate. Savings remove redundancy, never required reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required gates take precedence.
