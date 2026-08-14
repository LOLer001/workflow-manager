---
name: workflow-manager
description: Manage quality-first Codex workflows with daily/work and simple/hard routing, plan confirmation, regression continuity, compaction safety, and agent scheduling without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

## Quality invariant

- Correctness and required reasoning, evidence, safety, and acceptance outrank savings; remove only redundancy.
- Pressure may change order/checkpoints, never the depth needed to solve the task.

## Route and run

1. Classify **Daily** (chat/weather/reports/personal/cleanup) versus **Work** (device/App/code/engineering). Daily keeps current settings/safety.
2. Work creates one objective-bound high assessor using highest available model/effort, a positive fork, and a state-bound ASCII task name. Record requested versus observed only.
3. First assessor turn is read-only. **Simple** returns `WORK_ASSESSMENT`, then the same child solves+verifies and returns `SIMPLE_EXECUTION`. **Hard** returns the detailed plan+marker.
4. Hard covers unknown cause, cross-module design, device/shared resources, or long delivery. Gather read-only evidence; plan scope/paths, changes, ownership, delivery, verification, risk, rollback. End `计划已就绪，等待确认后执行`.
5. Before confirmation allow reads; block write/agent/Git/build/deploy/device mutation. A private Markdown mirror is review-only, not authorization. Changes invalidate it. Read [work routing](references/work-routing.md).
6. Default confirmed Hard uses lower-tier/medium; an explicit whole-session highest policy uses assessor tier+host-max. Read [confirmed execution](references/confirmed-execution.md) for proof/reset/resume.
7. Independently route **Direct**, **Focused**, or **Complex/Extensive** shape; use only relevant `Contract > Evidence > Change > Verify > Report` stages.
8. Seal a fingerprint-only baseline after confirmed execution. On same-task symptom feedback, read [regression continuity](references/regression-continuity.md) and compare prior objective/plan/contract/change/verification read-only. Wording only triggers review: `introduced`/`fix_ineffective` replan; `unrelated` reclassifies; `uncertain` needs evidence.
9. For explicit reference matching, read [reference acceptance](references/reference-acceptance.md); bind Hard execution and separate engineering, function, fidelity candidate, and user acceptance.
10. Follow-ups inherit valid bindings; reclassify new objectives. Update `phase | done | next | blocker` at kickoff, change, or ~60s wait.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% checkpoint and narrow exploration.
- After compaction resume native summary/gates, never raw prompt. Reclassify on changed inputs, repeated failures, long builds, or large output.

## Delegation

Launch ready positive-utility lanes only when critical-path time saved exceeds coordination/collision cost.

- Read-only investigation is only one option; owned write/test/research/review lanes qualify.
- Parent owns integration/shared state; treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3.
- Cross-task notices require fresh active same-host source+peer and a same-resource conflict; read [live coordination](references/live-coordination.md). Serialize only those shared stages. Pending Hard plans permit only read-only children.
- A stalled bound Hard executor stops with exact evidence. Reuse its high assessor once read-only; resume the bound profile for an in-plan remedy, else replan. Read [stall recovery](references/stall-recovery.md).
- Confirmed Hard has one executor; only read-only side lanes continue. Stay local when tiny, unready, overlapping, slower, or opted out.
- Give each child a concise Chinese purpose summary, ASCII `task_name`, scope/owner/result contract. Reuse it once. After its result, confirm terminal; interrupt only if still running. Keep live/pending; prune only terminal excess above 10. Read [agent lifecycle](references/agent-lifecycle.md).

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

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL mounts. Use remote Git or an authoritative Linux tree. If a Bash Hook exposes only mounted session `cwd`, put one verified native directory visibly in the command as absolute `git -C`; never chain Git calls.

## Truthfulness and boundaries

- Profiles only request policy; Hooks cannot switch models. Claim overrides only from matching host acceptance/echo.
- Availability or agent count do not prove effectiveness; agents may raise total tokens. PreToolUse is not security.
- Quality is the release gate. Savings remove redundancy, never required reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required gates take precedence.
