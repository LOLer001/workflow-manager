# Work routing reference

Use this reference for difficult-work classification, plan construction, strict confirmation, and guard-boundary checks. It extends the single invocable `workflow-manager` Skill; it is not a second Skill.

## Decision order and runtime truth

1. Decide Daily or Work independently from Direct/Focused/Complex/Extensive.
2. Daily requests keep the current session settings and have no work-plan gate.
3. Work requests use the logical `work_assessment` profile, requesting the highest available model and highest reasoning effort to decide Simple or Hard.
4. Simple work proceeds directly. Hard work remains in analysis until a detailed plan is strictly confirmed.

`current` and `work_assessment` are policy requests recorded by the Hook. The Hook cannot select, inspect, or prove the host model/reasoning setting. Report the requested profile and any host evidence separately; never turn an advisory into a success claim.

Difficulty and execution shape are separate axes. A Focused task can be Hard because it changes a device; a Complex task can have several bounded Simple lanes. Agent caps never determine difficulty.

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
| “编译模块、部署到共享实机并完成回归” | Work / Hard | Ordered external delivery and shared resource |
| “先看天气，再修改应用代码并发布” | Work / Hard | Inseparable engineering output dominates the mixed request |

Prefer explicit hard evidence: unknown/intermittent cause, multiple modules or contracts, architecture/migration/production work, device change, three or more material phases, or shared/ordered external resources. Prefer Simple only when scope, causal chain, change surface, and acceptance are all bounded. Unresolved Work ambiguity defaults to Hard with medium confidence; investigate narrowly rather than guessing Simple.

## Detailed Hard plan contract

Read enough evidence to make the plan executable, but do not mutate merely to discover the answer. Present a numbered table with:

- objective and acceptance for each step;
- exact known module/path/class/method, or a bounded search target when not yet known;
- intended change and dependency on earlier steps;
- owner/lane and any shared server, account, artifact, or device lock;
- verification command or observable acceptance result;
- risk, rollback, and stop condition.

Separate independent lanes from one ordered chain. Do not promise an exact file or method without evidence; label unresolved locations as bounded discovery in the plan. Finish with the exact sentence `计划已就绪，等待确认后执行`.

## Strict confirmation and invalidation

A confirmation binds only the current plan digest, objective fingerprint, and difficulty decision. Accept only a pure confirmation such as:

- `确认执行`
- `确认按新计划执行`
- `同意按这个计划执行` / `同意按上述计划执行`
- `按这个计划执行` / `按上述计划执行`
- `开始执行这个计划`
- `confirm and execute this plan` / `execute the plan`

Do not treat “继续”, “可以”, a question, partial approval, or silence as confirmation. A message containing “但是/另外/增加/删除/改为/先不要” or “but/except/add/remove/change” adds or changes a constraint: invalidate the pending confirmation, preserve compatible evidence, regenerate the plan and digest, then ask again. `重新规划`, `重做计划`, `修改计划`, `replan`, and `revise the plan` also invalidate it. A new objective always needs a new decision and plan.

## Guard boundary before confirmation

Allow targeted reads, searches, static inspection, safe metadata queries, plan updates, user questions, and explicitly read-only child investigation. These actions improve plan evidence and must not be misblocked merely because the task is Hard.

Block explicit file creation/edit/deletion, mutating child execution, mutating Git, compilation or packaging, deployment/install/flash/device mutation, and equivalent nested commands. After confirmation, normal mounted-source, destructive-action, output-budget, shared-resource, and project-specific gates still apply.

After a valid confirmation, continue with [confirmed-execution.md](confirmed-execution.md); confirmation opens creation of one bound executor contract, not parent mutation or an unbound child.

Do not apply the Hard plan gate to Daily or Simple work. If a safe read is falsely blocked, record the first guard reason and fix the narrow classifier/guard boundary; do not weaken the confirmation binding or bypass the guard with an equivalent command form.
