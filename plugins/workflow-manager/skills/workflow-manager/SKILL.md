---
name: workflow-manager
description: Manage quality-first Codex workflows with bounded exploration, compaction continuity, duplicate avoidance, and agent scheduling. Use for multi-step work, independent lanes, repeated tools, context pressure, or token-saving without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

Keep requirements, decisions, facts, and outputs; remove redundancy.

## Quality invariant

- Correctness and required reasoning, evidence, safety, reproducibility, and acceptance outrank savings.
- “Smallest relevant” removes redundancy, never needed stages, sources, diagnostics, corrected retries, or checks.
- Pressure may change order or checkpoint timing, never the depth needed to solve the task. Fetch exact evidence when excerpts are insufficient.
- Efficiency is not success without the requested outcome and acceptance evidence.

## Route and run

1. Route first: **Direct** answer/small edit; **Focused** one causal chain; **Complex/Extensive** lane audit at kickoff and new phase boundaries.
2. Prompt size, “parallel” wording, and many stages show effort, not independence. Build > package > deploy > device is usually one chain.
3. Updates inherit route; a clearly new objective may replace it.
4. Use only relevant stages: `Contract(outcome+acceptance+scope) > Evidence(cause) > Change(coherent edit) > Verify(risk-appropriate checks) > Report(outcome+paths+checks+risk)`.
5. Use a 3-5 step plan for 3+ material stages, risky acceptance, or shared/external state.
6. Reuse native summaries, plans, and unchanged success. Search exact paths/patterns; expand only as evidence requires.
7. With tools, send one kickoff; update `phase | done | next | blocker` only on material change or ~60s wait, never per tool.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% checkpoint and stop unfocused exploration, then continue required evidence narrowly or after compaction.
- After compaction, resume the native summary, re-arm both gates, and do not reconstruct finished work.
- Reclassify on new phases, repeated failures, long builds, or large output. Pressure never justifies delegation; make compaction safe.

## Delegation

Optimize critical-path time, not agent count. For Complex/Extensive work, seek positive-utility lanes whose time saved exceeds coordination/collision cost; bias low-risk close calls parallel.

- Before spawning, note `deliverable | inputs | ready | write owner | shared resource | time saved`; the parent counts as one lane.
- Read-only investigation is only one option. Edits, tests, research, reproduction, and review qualify with exclusive ownership and a compact result contract.
- Parent owns integration/shared state. Treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3. Launch ready positive-utility lanes and keep useful parent work moving.
- The hook denies closed-gate, over-cap, and confirmed duplicate-scope spawns. Audit still needs judgment; `SubagentStart` records continuity but cannot block.
- Dependencies, overlapping edits, one build account, or one device serialize only conflicts. One owner keeps build > deploy > reboot > device acceptance; unrelated lanes may run.
- Delegate even if the prompt omits it or writes are involved. Stay local only when tiny, unready, inseparable, overlapping, slower, or opted out.
- Give each child a concise Chinese purpose summary. If the host needs an ASCII `task_name`, use a schema-safe ID. State scope, exclusions, paths, result shape, and no-redo/no-raw-log.
- Reuse a live agent for one bounded follow-up; old-objective results are validation-only.

## Continuity

- Reuse success only when inputs, cwd, files, device/external state, freshness, and native evidence are unchanged; never reuse failed/nonterminal work.
- Native summaries own semantics; hook fingerprints/counters are hints. Resume recorded work; repair gaps with one narrow check.
- Added constraints preserve valid evidence; objective replacement makes late results validation-only.
- When a newer version is active, `SessionStart` removes only older sibling semantic-version caches; preserve current/higher/non-version/symlinks and fail open.

## Output and command guards

- Bound output with exact paths/patterns/ranges, limits, or full-log redirection. Budget Git status, compilation, logs, recordings, and frames.
- Before first mutation/automation, preflight only unverified path/entrypoint, input/encoding, and acceptance source.
- The hook may deny mounted Git, broad status, or unbudgeted build/log/recording. Correct once; do not retry an equivalent unbounded route.
- Keep the first error; diagnose once; retry after material correction or one bounded alternate. Repeated same-cause failure or ~25 stage actions requires `checkpoint > reclassify`, not skipped acceptance.
- Run one acceptance loop per unchanged revision unless reliability, flakiness, sampling, or explicit criteria require more. PostToolUse preserves oversized results and advises future bounds.
- Ignore unrelated files, logs, and history unless blocking.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL mounts. Use `android-remote-git`, an authoritative Linux tree, or a safe non-mounted terminal.

## Truthfulness and boundaries

- Hook availability or agent count does not prove effectiveness; judge outcomes and fit.
- Agents may reduce main-thread noise but raise total tokens; telemetry is estimated. PreToolUse is not a security boundary.
- Workflow quality is the release gate. Savings remove redundancy, never required reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required workflow gates take precedence.
