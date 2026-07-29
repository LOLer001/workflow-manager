---
name: workflow-manager
description: Manage quality-first, efficiency-biased Codex workflows with bounded exploration, compaction-safe continuity, duplicate avoidance, and dynamic subagent scheduling. Use for long/multi-step work, independent lanes, repeated tools, context pressure/compaction, or token-saving requests without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

Keep the main thread to requirements, decisions, facts, and outputs. Remove redundancy, never evidence needed for correctness or acceptance.

## Quality invariant

- Correctness and required reasoning, evidence, safety, reproducibility, and acceptance outrank context savings.
- “Smallest relevant” removes redundancy, never a needed stage, source, diagnostic, corrected retry, or check.
- Pressure may change order, representation, or checkpoint timing, never the depth needed to solve the task. If an excerpt is insufficient, fetch exact evidence or compact safely and continue.
- Shorter output, fewer tools/agents, or fewer tokens is not success without the requested outcome and complete acceptance evidence.

## Route and run

1. Route first: **Direct** is an answer/small edit; **Focused** is one causal chain; **Complex/Extensive** requires a lane audit at kickoff and whenever phase boundaries expose new work.
2. Prompt size, “parallel” wording, and many stages show effort, not independence. Build > package > deploy > device is usually one chain.
3. Progress updates inherit the active route; a clearly new objective may replace it.
4. For Focused/Complex work use only relevant stages, while satisfying the quality invariant: `Contract(outcome+acceptance+scope) > Evidence(cause) > Change(coherent edit) > Verify(risk-appropriate checks) > Report(outcome+paths+checks+risk)`.
5. Use a 3-5 step plan only for more than two material stages, risky acceptance, or shared/external state.
6. Reuse native summaries, plans, and unchanged success. Search exact paths/patterns; expand only as evidence requires.
7. With tools, send one kickoff; update `phase | done | next | blocker` only on material change or ~60s wait, never per tool.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% stop unfocused exploration, checkpoint, then continue required evidence narrowly or after native compaction.
- After compaction, resume from the native summary, re-arm both gates, and never reconstruct completed work.
- Reclassify on new phases, repeated failures, long builds, or large output. Pressure never justifies delegation; make compaction safe.

## Delegation

Optimize wall-clock/critical-path time, not agent count. For Complex/Extensive work, actively find positive-utility lanes whose expected time saved exceeds coordination, merge, and collision cost; if close and low-risk/reversible, bias parallel.

- Before spawning, note `deliverable | inputs | ready | write owner | shared resource | time saved`. The user need not name two lanes: the parent counts as one; infer candidates.
- Read-only investigation is only one option. Code/doc edits, tests, research, reproduction, and review qualify with exclusive path/module ownership and a compact result contract.
- Parent owns integration and shared mutable state. Treat caps as ceilings, never quotas: Direct/Focused 0; Complex up to 2 subagents; Extensive up to 3. Launch `min(positive_utility_ready_lanes, route_cap)` and keep doing useful parent work while children run.
- The hook denies closed-gate, over-cap, and confirmed duplicate-scope spawn requests. Audit still needs judgment; `SubagentStart` records continuity but cannot block.
- Dependencies, overlapping edits, one build account, or one device serialize only conflicting operations. One owner keeps build > deploy > reboot > device acceptance; unrelated source/test/research/review lanes may run.
- Do not skip delegation because the prompt omitted it or writes are involved. Stay local only when tiny, not ready, inseparable, overlapping, slower after coordination, or explicitly opted out.
- Give each child a concise Chinese purpose summary. If the host needs an ASCII `task_name`, use a schema-safe ID and put the Chinese purpose in its prompt/update. State scope, exclusions, paths, result shape, and no-redo/no-raw-log.
- Reuse a live agent for one bounded follow-up. Old-objective results are validation-only.

## Continuity

- Reuse success only when inputs, cwd, files, device/external state, freshness, and native evidence are unchanged; never reuse nonterminal or failed work.
- Native summaries own semantics; hook fingerprints/counters are hints. Resume recorded work and repair missing fields with one narrow check.
- Added constraints preserve valid evidence; objective replacement makes late results validation-only.

## Output and command guards

- Bound output with exact paths/patterns/ranges, limits, or full-log redirection. Budget Git status, builds, logs, recordings, and frames.
- Before first mutation/automation, preflight only unverified path/entrypoint, input/encoding, and acceptance source.
- The hook may deny mounted Git, broad status, or unbudgeted build/log/recording. Correct once; do not try an equivalent unbounded route.
- Keep the first error; diagnose once; retry after material correction or one bounded alternate. A repeated same-cause failure or ~25 stage actions requires `checkpoint > reclassify`, never abandonment of needed diagnosis/acceptance.
- Run one acceptance loop per unchanged revision unless reliability, flakiness, sampling, or explicit criteria require repetition. PostToolUse preserves oversized results and only advises future bounds.
- Ignore unrelated files, generated output, old logs, and history unless blocking.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL-mounted source. Use `android-remote-git`, the authoritative remote Linux tree, or a safe non-mounted terminal; mounts are only for targeted reads/searches/edits.

## Truthfulness and boundaries

- Hook availability or agent count does not prove effectiveness; judge outcomes and delegation fit.
- Agents may reduce main-thread noise but raise total tokens; telemetry is estimated. PreToolUse is a guardrail, not a security boundary.
- Workflow quality is the release gate. Savings may remove redundancy, never reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required workflow gates take precedence.
