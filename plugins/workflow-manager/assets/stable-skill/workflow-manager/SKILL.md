---
name: workflow-manager
description: Manage quality-first Codex workflows with daily/work and simple/hard routing, plan confirmation, regression continuity, compaction safety, and agent scheduling without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

## Quality invariant

- Correctness and required reasoning, evidence, safety, and acceptance outrank savings.
- Remove redundancy, never required stages, evidence, correction, or checks.
- Pressure may change order/checkpoints, never the depth needed to solve the task.

## Route and run

1. Classify **Daily** (chat, weather, reports, personal help, cleanup) versus **Work** (device/App/code/engineering). Daily keeps current settings and safety.
2. Work creates one objective+generation-bound high assessor, always spawned with highest available model/effort and explicit `fork_turns=none`/positive integer. Record requested versus observed profile only.
3. First assessor turn is read-only: **Simple** returns `WORK_ASSESSMENT`, then the same bound child follow-up solves+verifies and returns `SIMPLE_EXECUTION`; **Hard** returns detailed plan+marker and ends `计划已就绪，等待确认后执行`.
4. Hard covers unknown cause, cross-module design, device/shared resources, or long delivery. Gather read-only evidence; plan modules/files, changes, ownership, delivery, verification, risk, rollback. End `计划已就绪，等待确认后执行`.
5. Before strict Hard-plan confirmation, allow reads; block writes, mutating children/Git, builds, deployment, and device mutation. Changed constraints invalidate it. Read [references/work-routing.md](references/work-routing.md).
6. Default confirmed Hard uses one lower-tier/medium executor; an explicit whole-session highest-model+effort policy instead requests the assessor tier and host-max effort. Read [references/confirmed-execution.md](references/confirmed-execution.md) for proof/reset/resume.
7. Independently route **Direct**, **Focused**, or **Complex/Extensive** execution shape. Use only relevant `Contract > Evidence > Change > Verify > Report` stages.
8. After confirmed execution, seal a fingerprint-only baseline. If same-task acceptance reports a symptom, read [references/regression-continuity.md](references/regression-continuity.md): compare prior objective/plan/contract/change/verification read-only. Wording is only a trigger. `introduced`/`fix_ineffective` replan and reconfirm; `unrelated` reclassifies; `uncertain` needs evidence.
9. Only for an explicit reference-match request, read [reference acceptance](references/reference-acceptance.md); bind it to Hard execution and separate engineering, function, fidelity candidate, and user acceptance.
10. Follow-ups inherit valid bindings; reclassify new objectives. Update `phase | done | next | blocker` only at kickoff, change, or ~60s wait.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% checkpoint and narrow exploration.
- After compaction resume native summary/gates and normalized profile preference, never its raw prompt. Reclassify on new phases, repeated failures, long builds, or large output.

## Delegation

Optimize critical-path time. Launch ready positive-utility lanes only when time saved exceeds coordination/collision cost.

- Before spawning note `deliverable | ready | owner | resource | time saved`. Read-only investigation is only one option; owned write/test/research/review lanes qualify.
- Parent owns integration/shared state. Treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3.
- Serialize overlapping edits/shared stages only. Cross-task notices require a fresh active same-host peer plus the same resource/conflict; read [live coordination](references/live-coordination.md). Pending Hard plans permit only read-only children.
- Confirmed Hard has one executor; only read-only side lanes continue. Stay local when tiny, unready, overlapping, slower, or opted out.
- Give each child a concise Chinese purpose summary and safe ASCII `task_name`; state scope, ownership, and result shape. Reuse a live agent for one bounded follow-up.

## Continuity

- Reuse success only when inputs, files, state, freshness, and evidence are unchanged; never reuse failure.
- Native summaries own semantics; Hook hints do not. Added constraints preserve compatible evidence but invalidate the plan; new-objective results are validation-only.

## Upgrade continuity

- Migrate through a supported host API; never edit rollout JSONL or live databases/indexes/tasks. Else install `$CODEX_HOME/skills/workflow-manager` and verify new/resumed tasks load it.
- Keep old caches until either route covers all tasks; fail open and report gaps.

## Output and command guards

- Bound output by paths/patterns/ranges or full-log redirection; budget large output.
- Before mutation, preflight only unverified paths, inputs, and acceptance sources.
- Obey Hook denials; do not retry an equivalent unbounded route.
- Keep the first error; diagnose once; retry only after correction or one bounded alternate. Same-cause repetition or ~25 stage actions requires `checkpoint > reclassify`.
- Run one acceptance loop per unchanged revision unless required; preserve oversized results.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL mounts. Use `android-remote-git`, an authoritative Linux tree, or a safe non-mounted terminal.

## Truthfulness and boundaries

- Profiles only request policy; Hooks cannot switch models. Claim overrides only from matching host acceptance/echo.
- Availability or agent count do not prove effectiveness; agents may raise total tokens. PreToolUse is not security.
- Quality is the release gate. Savings remove redundancy, never required reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required gates take precedence.
