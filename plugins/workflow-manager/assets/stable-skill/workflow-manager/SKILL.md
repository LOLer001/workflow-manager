---
name: workflow-manager
description: Quality-first Codex workflow routing, confirmation, continuity, compaction safety, and agent scheduling.
---

# Workflow Manager

## Quality invariant

- Correctness and required reasoning, evidence, safety, and acceptance outrank savings; remove only redundancy.
- Pressure may change order/checkpoints, never the depth needed to solve the task.

## Route and run

1. Classify **Daily** (chat/weather/reports/personal/cleanup) or **Work** (device/App/code/engineering). Daily keeps current settings/safety.
2. High-confidence **Simple** Work stays local (child Start=0). Other Work creates one high assessor; default `reasoning_effort=max`; only explicit `highest_throughout` uses `ultra`. Bound children use `fork_turns=1`. Activation/identity preflight forbidding tools+children is Daily/local Start=0 and refreshes PLUGIN_ROOT even if it names Work/Hard.
3. A bound assessor's first turn is read-only. **Simple** returns `WORK_ASSESSMENT`, then solves+verifies in the same child and returns `SIMPLE_EXECUTION`. **Hard** returns its plan+marker.
4. Hard covers unknown cause, cross-module/device/shared-resource, or long delivery. Gather read-only evidence; plan scope/ownership/verification/risk/rollback. End `计划已就绪，等待确认后执行`.
5. Before confirmation, append Hard plans to fixed private canonical Markdown. Its trusted current revision defines content, never authorization; edits invalidate. Allow reads/projections and block mutations. Read [work routing](references/work-routing.md).
6. Confirmed Hard defaults to lower-tier/medium; session-highest uses assessor+`ultra`. A unique tail JSON manifest defines 1..8 sequential contract-bound slices (normally 3–5); Start gets global constraints, the current slice, and cursor-minimal delta only. PreTool records `requested`; successful or failed PostToolUse independently records `host_accepted`; bound Start is `full|partial|absent|mismatch` and may run only when full. Start `model` comes from the official Hook payload and effort only from the same-turn host transcript context, never from requested values or child self-report. The Host normalizes only explicit structured tool status or its top-level response wrapper; ordinary command text is not status evidence. An executor's verification proves its candidate, while sealing additionally requires one bound, read-only parent verification operation. Final result/review markers omit digests. Parent pass advances, one fresh v2 may repair, and only the last pass seals. Read [confirmed execution](references/confirmed-execution.md).
7. Independently route **Direct**, **Focused**, or **Complex/Extensive**; use only relevant `Contract > Evidence > Change > Verify > Report` stages.
8. Seal a fingerprint-only baseline only after parent acceptance. For same-task symptoms, read [regression continuity](references/regression-continuity.md); wording only triggers review: `introduced`/`fix_ineffective` replan, `unrelated` reclassifies, `uncertain` needs evidence.
9. For explicit reference matching, read [reference acceptance](references/reference-acceptance.md); bind Hard execution and separate engineering, function, fidelity, and user acceptance.
10. Follow-ups inherit valid bindings; reclassify new objectives. Update `phase | done | next | blocker` at kickoff, change, or ~60s waits.

## Context gates

Use structured host results; exact same-turn evidence may correct one failed parent read-only probe without proving artifacts. Ambiguity, mutation, or another failure stays v2/exhausted.

- Below 55% work normally; at 55-70% trim presentation; at 70% checkpoint and narrow exploration.
- After compaction use the native summary plus canonical Hard plan, never raw prompt. Only same-session resume may reconcile a bounded rollout pair or one-shot gate repair; ambiguity stays unrecorded.

## Delegation

Launch ready positive-utility lanes only when saved critical-path time exceeds coordination/collision cost.

- Read-only investigation is only one option; owned write/test/research/review lanes qualify.
- Parent owns integration/shared state; treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3.
- Cross-task notices require fresh active same-host peers and a same-resource conflict; read [live coordination](references/live-coordination.md). Pending Hard plans allow only read-only children.
- A stalled bound Hard slice executor stops with exact evidence. Reuse its high assessor once read-only; resume the bound profile for an in-plan remedy, else replan. Read [stall recovery](references/stall-recovery.md).
- Confirmed Hard has at most one live slice executor; only read-only side lanes continue. Stay local when tiny, unready, overlapping, slower, or opted out.
- Give each child a concise Chinese purpose summary, ASCII `task_name`, scope/owner/result contract. Never follow up a terminal slice executor; recovery uses one fresh v2 bound to its slice/token and Host review evidence. Keep live/pending; prune only terminal excess above 10. Read [agent lifecycle](references/agent-lifecycle.md).

## Continuity

- Reuse success only when inputs/files/state/freshness/evidence are unchanged; never reuse failure.
- Native summaries own semantics; Hook hints do not. Added constraints preserve compatible evidence but invalidate the plan; new-objective results only validate.

## Upgrade continuity

- Migrate through a supported host API; never edit rollout JSONL or live databases/indexes/tasks. Else install `$CODEX_HOME/skills/workflow-manager` and verify new/resumed tasks load it.
- Clean strictly older caches only after the stable route covers new/resumed tasks; preserve sealed v6 evidence and fail open on migration gaps.

## Output and command guards

- Bound output by paths/patterns/ranges or full-log redirection; budget it.
- Preflight only unverified paths, inputs, and acceptance.
- Obey Hook denials; do not retry an equivalent unbounded route.
- Keep the first error; retry only after correction or one bounded alternate. Repetition or ~25 stage actions requires `checkpoint > reclassify`.
- Run one acceptance loop per unchanged revision unless required; preserve oversized results.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL mounts. Use remote Git or an authoritative Linux tree. If a Bash Hook exposes only mounted session `cwd`, put one verified native directory visibly in the command as absolute `git -C`; never chain Git calls.

## Truthfulness and boundaries

- Profiles request policy; Hooks cannot switch models. Claim overrides need matching host acceptance/echo.
- Availability/agent count do not prove effectiveness; agents may raise tokens. PreToolUse is not security.
- Quality is the release gate. Savings remove redundancy, never required reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required gates take precedence.
