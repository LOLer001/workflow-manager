---
name: workflow-manager
description: Manage context-efficient Codex workflows with bounded exploration, live context-pressure hints, compaction-safe continuity, duplicate-work avoidance, and selective subagent delegation. Use for long or multi-step tasks, parallelizable investigation, repeated tool work, context pressure or compaction, or explicit requests to conserve tokens and preserve progress.
---

# Workflow Manager

Keep the main thread to requirements, decisions, verified facts, and final outputs. Spend tokens only on evidence that changes the next action.

## Route and run

1. Route first: **Direct** is an answer/small edit; **Focused** is one causal chain; **Complex** has noisy or separable work and needs a lane audit.
2. Prompt size, “comprehensive/parallel” wording, and many stages measure effort, not independence. Build > package > deploy > device is usually one chain.
3. Progress updates inherit the active objective/route; a clearly new objective may replace or downgrade it.
4. For Focused/Complex work use only relevant stages: `Contract(outcome+acceptance+scope) > Evidence(cause/assumption) > Change(small coherent edit) > Verify(decisive check) > Report(outcome+paths+checks+risk)`.
5. Use a 3-5 step plan only for more than two material stages, risky acceptance, or shared/external state.
6. Reuse the native summary, plan, and unchanged success before rerunning. Search exact paths/patterns and read short excerpts.
7. With tools, send one kickoff; update `phase | done | next | blocker` only on material change or ~60s wait, never per read/edit/poll.

## Context gates

- Below 55% work normally; at 55-70% trim output; at 70% stop broad exploration, checkpoint, then use narrow work or allow native compaction.
- After compaction, resume from the native summary and re-arm the 55%/70% gates for the next context cycle; never reconstruct completed work from scratch.
- Reclassify on new phases, repeated failures, long builds, or large output. Pressure alone never justifies delegation; make compaction safe instead of suppressing it.

## Delegation

Delegate only when at least two bounded, summary-friendly lanes are independent, ready now, and worth the coordination.

- Before spawning, note `deliverable | inputs | ready now | file/write owner | shared resource` for each candidate. If an initially sequential objective later exposes a safe lane, state explicitly that the child lane and parent lane are independent, non-overlapping, read-only, and ready now so the hook can re-audit it.
- Parent owns integration/shared state. Use `subagents=min(ready_lanes-1, route_cap)`: Direct/Focused 0; Complex defaults to parent+1; expand only when a phase transition exposes another high-cost lane.
- The hook denies closed-gate, cap-exceeded, and confirmable duplicate-scope `Agent/spawn_agent` requests before start; an audit gate still requires judgment. `SubagentStart` records continuity and injects the child contract, but cannot block a start.
- Dependencies, one artifact, overlapping edits, one build account, or one device override parallel wording. Keep build > deploy > reboot > device acceptance under one owner.
- Routine show/submit/mount/rename/one-step automation stays local unless an expensive evidence lane is ready.
- Prefer agents for read-heavy research, source/log triage, or independent verification. Describe each child to the user with a concise Chinese purpose summary. When the host requires an ASCII `task_name`, use a short schema-safe internal ID and put the Chinese purpose at the start of the child prompt and progress update. Prompts state scope/exclusions, paths, result shape, and no-redo/no-raw-log.
- Reuse a live agent for one bounded follow-up. An older-objective result is validation-only and cannot restart old mutations.

## Continuity

- Reuse terminal success only when input, cwd, files, device/external state, freshness, and native evidence are unchanged; never reuse unknown/running/failed/cancelled/timed-out work.
- Native summary owns semantics; hook fingerprints/counters are hints. Resume the recorded stage, keep active agents, and repair each missing checkpoint field with one narrow check—not a full skill/protocol/source reload.
- Added constraints extend the objective and preserve valid evidence; explicit replacement stops old-objective tools and makes late results validation-only.

## Output and command guards

- Bound output with exact paths/patterns/ranges, quiet modes, limits, or log redirection. Budget Git status, build/package, logs, recording, and 1-3 representative frames.
- Before first mutation/automation, preflight only unverified path/entrypoint, input/encoding, and acceptance source.
- The hook may deny mounted Git, broad status, unbudgeted build/log/recording. Correct once; do not try an equivalent unbounded route.
- Keep the first error; diagnose once; retry only after material correction or one bounded alternate. Same-cause second failure or ~25 actions in one stage forces `checkpoint > reclassify`.
- Run one build/deploy/device acceptance loop per unchanged revision. If PostToolUse compacts output, use its excerpt or one narrower query—never rerun the full command.
- Ignore unrelated files, generated output, old logs, and history unless blocking.

## Mounted-source safety

Never run Git from CIFS, Samba, UNC, DrvFS, or WSL-mounted source, including path-limited Git commands. Use `android-remote-git`, the authoritative remote Linux tree, or an explicitly safe non-mounted terminal. Mounted paths are for targeted reads/searches/edits only.

## Truthfulness and boundaries

- Hook availability, a route, or zero agents does not prove effectiveness; judge bounded output, duplication, safe commands, checkpoints, and delegation fit.
- Agents often reduce main-thread noise but increase total tokens; telemetry/ranges are estimates. PreToolUse is a guardrail, not a security boundary.
- User instructions, safety, project-local skills, and required workflow gates take precedence.
