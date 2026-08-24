# Work routing reference

Use this reference for difficult-work classification, plan construction, strict confirmation, and guard-boundary checks. It extends the single invocable `workflow-manager` Skill; it is not a second Skill.

## Decision order and runtime truth

1. Decide Daily or Work independently from Direct/Focused/Complex/Extensive.
2. Daily requests keep the current session settings and have no work-plan gate.
3. Simple/Focused objectives stay local with zero child starts. A true Hard objective, causal/replacement review, reference failure, unknown/critical problem, or failed material correction creates one binding from objective fingerprint plus assessor generation. Request exactly one child with the highest available Codex model and default `reasoning_effort=max`; explicit `highest_throughout` requests `ultra`. Use `fork_turns=1`. Record requested, host-accepted, and observed profile separately; a bound child runs only with a full matching Start observation.
4. Simple work proceeds directly. Hard work remains in analysis until a detailed plan is strictly confirmed.

`current` and `work_assessment` are policy requests recorded by the Hook. The Hook cannot select, inspect, or prove the host model/reasoning setting. Report the requested profile and any host evidence separately; never turn an advisory into a success claim.

## Bound assessor lifecycle

The child request carries exact `assessor_binding_id`, `objective_fingerprint`, `profile_resolution=highest_available`, and a self-contained role contract. Its first turn is read-only. For **Simple**, it ends with `WORK_ASSESSMENT binding_id=<32hex> outcome=simple evidence_digest=<32hex>`; only an exact follow-up to that same agent/binding with solve+verify may mutate, and it must end with line-level `SIMPLE_EXECUTION binding_id=<32hex> evidence_digest=<32hex>`. For **Hard**, it remains read-only, produces the detailed plan below, emits the equivalent `outcome=hard` marker, and its final line is exactly `计划已就绪，等待确认后执行`.

Only requested profile fields are recorded at spawn. A bound child may run only after `SubagentStart` supplies a matching active-model echo and the same turn's host transcript supplies matching effort. Absent fields never fabricate evidence or mutation authority. Mismatched/stale marker or child status is typed recovery. There is one materially corrected recovery at most (two attempts); recovery must name the previous typed cause and a substantive correction. A Hard outcome after successful implementation/build/deploy/device/Git mutation fails as `hard_mutation_before_confirmation` and cannot supply a plan. Compaction/resume retains only binding, state, attempt, failure, and fingerprints—never raw prompt, plan, or child result.

Difficulty and execution shape are separate axes. Device interaction, build/deploy, three phases, a shared resource, or ambiguity alone is not Hard. Agent caps never determine difficulty.

## Gold set

| Request | Domain/difficulty | Reason |
|---|---|---|
| “今天天气怎么样？” | Daily / not applicable | Daily information request |
| “根据这些内容生成今天的日报” | Daily / not applicable | Report generation is daily even if its source discusses work |
| “Build me a workout plan” | Daily / not applicable | “build” is ordinary language here, not engineering delivery |
| “修正 Parser.java 的一处错字，现有单测已覆盖” | Work / Simple | One bounded edit and explicit acceptance |
| “解释这个方法当前为什么返回 null，不改代码” | Work / Simple | Bounded code explanation |
| “给定输入输出，修改单个函数并运行现有单测” | Work / Simple | Known scope, at most one causal chain, clear verification |
| “修复 Android 设备反复重启，根因未知” | Work / Hard | Unknown cause plus device behavior |
| “同时修改 Settings、framework 和 SystemUI 的显示模式” | Work / Hard | Cross-module contract |
| “从零开发带离线同步和认证的完整 App” | Work / Hard | Architecture and broad acceptance surface |
| “编译模块、部署到共享实机并完成回归” | Work / Simple | Operational complexity alone; keep one bounded ordered chain |
| “先看天气，再修改应用代码并发布” | Work / Simple | Engineering output dominates domain, but does not prove Hard difficulty |
| “生产发布数据库迁移并提供回滚” | Work / Hard | Critical production/rollback risk |

Hard requires one critical signal (production/irreversible/security/full-system delivery/host-continuity acceptance), or at least two independent signal groups with one from unknown cause, cross-scope, or continuity. Operational external-state, workflow-length, and coordination signals may strengthen a primary signal but cannot promote by themselves. Unresolved ambiguity defaults to a bounded Simple diagnosis with medium confidence and promotes only when evidence crosses this threshold.

## Detailed Hard plan contract

Read enough evidence to make the plan executable, but do not mutate merely to discover the answer. Present a numbered table with:

- objective and acceptance for each step;
- exact known module/path/class/method, or a bounded search target when not yet known;
- intended change and dependency on earlier steps;
- owner/lane and any shared server, account, artifact, or device lock;
- verification command or observable acceptance result;
- risk, rollback, and stop condition.

Separate independent lanes from one ordered chain. Do not promise an exact file or method without evidence; label unresolved locations as bounded discovery in the plan. Finish with the exact sentence `计划已就绪，等待确认后执行`.

Before that readiness sentence, include exactly one fenced JSON block with language `workflow-manager-execution-slices`. Sanitization removes protocol/readiness lines, so this block becomes the tail of the canonical revision. It uses only the exact keys `version`, `global_constraints`, and `slices`; every slice uses `id`, `title`, `scope`, `acceptance`, `rollback`, `stop_conditions`, and `expected_artifacts`. IDs are consecutive `s01` onward, all arrays contain bounded non-empty strings, and a normal Hard revision uses 1..3 acceptance checkpoints with a hard upper bound of 6.

Each slice must have one independently observable acceptance surface and bounded ownership. Preserve global invariants in `global_constraints`; put prerequisites before consumers; keep shared build/device stages sequential; and split large plans so a lower-tier executor cannot silently omit a strong gate. More than 6 meaningful slices means the plan is not yet executable: consolidate or split the objective instead of dropping checks. The complete example and runtime contract are in [confirmed-execution.md](confirmed-execution.md).

## Canonical journal gate

Before `plan_state` may become `awaiting_confirmation`, the Hook must sanitize and append the complete Hard plan as the next revision of the fixed private `plans/<session-token>/hard-plan.md`. Every replan and later objective in the same session appends another complete revision to that file. Only a successful journal-and-state transaction increments `plan_generation`; a failed write remains analyzing or invalidated and cannot be confirmed.

The current trusted revision is the plan-content authority. Plan-detail views, replanning continuity, compaction recovery, and each eventual slice executor must reread it. The Markdown alone never confirms or authorizes anything: objective/difficulty bindings, current revision, journal/manifest digests, strict user confirmation, global execution contract, current slice token, and accepted-prefix chain remain mandatory. An external edit or replacement invalidates the plan and makes any old executor contract stale; recovery requires a trusted new revision and confirmation.

`update_plan` is only a UI projection, never a second plan store. It is allowed only with `projection_only canonical_revision_digest=<digest>` and step text already present in the current canonical revision. A semantic change must go through full journal-backed replanning, not an independent projection update.

A sanitized revision of exactly 983040 UTF-8 bytes is allowed; one byte more is `revision_too_large`. A journal of exactly 10485760 bytes is allowed; an append that exceeds it is `journal_full`. Either typed rejection leaves the existing journal byte-for-byte unchanged and does not consume a generation.

## Strict confirmation and invalidation

A confirmation binds only the current trusted canonical revision digest, its journal digest, objective fingerprint, and difficulty decision. Accept only a pure confirmation such as:

- `确认执行`
- `确认按新计划执行`
- `同意按这个计划执行` / `同意按上述计划执行`
- `按这个计划执行` / `按上述计划执行`
- `开始执行这个计划`
- `confirm and execute this plan` / `execute the plan`

Do not treat “继续”, “可以”, a question, partial approval, or silence as confirmation. A message containing “但是/另外/增加/删除/改为/先不要” or “but/except/add/remove/change” adds or changes a constraint: invalidate the pending confirmation, preserve compatible evidence, append one complete replacement revision, then ask again. `重新规划`, `重做计划`, `修改计划`, `replan`, and `revise the plan` do the same. A new objective needs a new decision and another complete revision in the same session journal.

## Guard boundary before confirmation

Allow targeted reads, searches, static inspection, safe metadata queries, journal-backed replanning, digest-bound `update_plan` projections, user questions, and explicitly read-only child investigation. These actions improve plan evidence and must not be misblocked merely because the task is Hard.

Block explicit file or directory creation/edit/deletion (including `mkdir`), mutating child execution, mutating Git, compilation or packaging, deployment/install/flash/device mutation, and equivalent nested commands. After confirmation, mounted-source, destructive-action, conflicting ownership/resource, contract-evidence, and project-specific gates still apply. Output shape is telemetry, not an authorization gate.

After a valid confirmation, continue with [confirmed-execution.md](confirmed-execution.md); confirmation opens one global contract and at most one current-slice executor, not parent mutation, parallel slice execution, or an unbound child.

Do not apply the Hard plan gate to Daily or Simple work. If a safe read is falsely blocked, record the first guard reason and fix the narrow classifier/guard boundary; do not weaken the confirmation binding or bypass the guard with an equivalent command form.
