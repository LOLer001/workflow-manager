---
name: workflow-manager
description: Manage quality-first, context-efficient Codex workflows with bounded exploration, compaction-safe continuity, duplicate avoidance, and selective subagents. Use for long/multi-step work, independent lanes, repeated tools, context pressure/compaction, or token-saving requests without reducing required reasoning, evidence, or verification.
---

# Workflow Manager

Keep the main thread to requirements, decisions, verified facts, and outputs. Remove redundancy, never evidence needed for correctness, safety, reproducibility, or acceptance.

## Quality invariant

- Correctness and required reasoning, evidence, safety, reproducibility, and acceptance outrank context savings.
- “Smallest relevant” means remove redundancy, never a necessary stage, source, diagnostic, corrected retry, or risk-appropriate check.
- Pressure may change order, representation, or checkpoint timing, never the depth needed to solve the task. If an excerpt is insufficient, fetch exact evidence or compact safely and continue.
- Shorter output, fewer tools/agents, or fewer tokens is not success without the requested outcome and complete acceptance evidence.

## Route and run

1. Route first: **Direct** is an answer/small edit; **Focused** is one causal chain; **Complex** needs a lane audit.
2. Prompt size, “parallel” wording, and many stages show effort, not independence. Build > package > deploy > device is usually one chain.
3. Progress updates inherit the active objective/route; a clearly new objective may replace or downgrade it.
4. For Focused/Complex work use only relevant stages, while satisfying the quality invariant: `Contract(outcome+acceptance+scope) > Evidence(cause) > Change(coherent edit) > Verify(risk-appropriate checks) > Report(outcome+paths+checks+risk)`.
5. Use a 3-5 step plan only for more than two material stages, risky acceptance, or shared/external state.
6. Reuse native summaries, plans, and unchanged success. Search exact paths/patterns; start short, then expand until evidence is sufficient.
7. With tools, send one kickoff; update `phase | done | next | blocker` only on material change or ~60s wait, never per tool.

## Context gates

- Below 55% work normally; at 55-70% trim presentation; at 70% stop unfocused exploration, checkpoint, then continue required evidence narrowly or after native compaction.
- After compaction, resume from the native summary, re-arm both gates, and never reconstruct completed work.
- Reclassify on new phases, repeated failures, long builds, or large output. Pressure never justifies delegation; make compaction safe.

## Delegation

Delegate only when at least two bounded, summary-friendly lanes are independent, ready now, and worth the coordination.

- Before spawning, note `deliverable | inputs | ready now | write owner | shared resource`. A newly safe lane must be explicitly independent, non-overlapping, read-only, and ready now for re-audit.
- Parent owns integration/shared state. Use `subagents=min(ready_lanes-1, route_cap)`: Direct/Focused 0; Complex normally parent+1; expand only for a new high-cost lane.
- The hook denies closed-gate, over-cap, and confirmed duplicate-scope spawn requests. Audit still needs judgment; `SubagentStart` records continuity but cannot block.
- Dependencies, one artifact, overlapping edits, one build account, or one device force serialization. One owner keeps build > deploy > reboot > device acceptance.
- Routine show/submit/mount/rename/one-step automation stays local unless an expensive evidence lane is ready.
- Prefer agents for read-heavy research, log/source triage, or independent verification. Give each child a concise Chinese purpose summary. If the host needs an ASCII `task_name`, use a schema-safe ID and put the Chinese purpose in its prompt/update. State scope, exclusions, paths, result shape, and no-redo/no-raw-log.
- Reuse a live agent for one bounded follow-up. Old-objective results are validation-only.

## Continuity

- Reuse success only when inputs, cwd, files, device/external state, freshness, and native evidence are unchanged; never reuse nonterminal or failed work.
- Native summaries own semantics; hook fingerprints/counters are hints. Resume the recorded stage/agents and repair each missing field with one narrow check, not a full reload.
- Added constraints preserve valid evidence; objective replacement stops old tools and makes late results validation-only.

## Output and command guards

- Bound output with exact paths/patterns/ranges, limits, or full-log redirection. Budget Git status, builds, logs, recordings, and 1-3 frames.
- Before first mutation/automation, preflight only unverified path/entrypoint, input/encoding, and acceptance source.
- The hook may deny mounted Git, broad status, or unbudgeted build/log/recording. Correct once; do not try an equivalent unbounded route.
- Keep the first error; diagnose once; retry after material correction or one bounded alternate. A repeated same-cause failure or ~25 stage actions requires `checkpoint > reclassify`, never abandonment of needed diagnosis/acceptance.
- Run one acceptance loop per unchanged revision unless reliability, flakiness, sampling, or explicit criteria require repetition. PostToolUse preserves oversized results and only advises future bounds.
- Ignore unrelated files, generated output, old logs, and history unless blocking.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or other WSL-mounted source. Use `android-remote-git`, the authoritative remote Linux tree, or a safe non-mounted terminal; mounts are only for targeted reads/searches/edits.

## Truthfulness and boundaries

- Hook availability, a route, or zero agents does not prove effectiveness; judge outputs, duplication, commands, checkpoints, and delegation fit.
- Agents may reduce main-thread noise but raise total tokens; telemetry is estimated. PreToolUse is a guardrail, not a security boundary.
- Workflow quality is the release gate. Savings may remove redundancy, never reasoning, evidence, correction, or verification.
- User instructions, safety, project-local skills, and required workflow gates take precedence.
