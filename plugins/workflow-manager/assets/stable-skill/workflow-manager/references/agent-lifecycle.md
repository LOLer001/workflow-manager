# Agent lifecycle and bounded cleanup

Use this reference when creating, following up, stopping, counting, or cleaning subagents. It extends the single invocable `workflow-manager` Skill; it is not another Skill.

## Complete and stop every child

1. Give the child one bounded deliverable, ownership, result shape, and a concise Chinese purpose summary. A final result must explicitly say the assigned work is complete or report its precise blocker; do not leave a finished child waiting for more work.
2. When the result returns, integrate it and check the host's supported agent-status API once. If that exact child already shows terminal, do nothing. If its result has returned but the host still shows it running, use the supported `interrupt_agent` operation for that exact ID. Never interrupt an agent that is live, awaiting a result, diagnosing a stall, or executing a confirmed contract.
3. Reuse a still-live child for at most one bounded follow-up when the contract permits it. A same-agent follow-up is a new result-pending generation even when the host emits no second Start event. A terminal confirmed slice executor is never reusable: ordinary recovery is a fresh contract/slice/token/failure-bound v2; failed parent acceptance uses a fresh v2 visibly bound to the Hook-generated review evidence.

The Hook observes request, Start, and Stop events; it cannot call `list_agents`, interrupt a host agent, delete a task, or remove sidebar/history records. Never claim that state pruning performed a host-side deletion.

## Lifecycle and race rules

- Multi-Agent V2 may encrypt `message` before local `PreToolUse`. For a new assessor use visible `task_name=high_assessor_<assessor_binding_id>_<objective_fingerprint>_v1` and exact `fork_turns=1`; the Hook records only `opaque_v2`, never the ciphertext. Plaintext hosts still require the complete message markers and Simple/Hard contract.
- An opaque Simple follow-up is valid only when its visible canonical target ends in that exact task name and the retained request proves the same binding. Wrong/stale targets fail closed, and the result must still return the exact `SIMPLE_EXECUTION` binding. Opaque executor recovery is a new spawn, never a follow-up: v1 is `confirmed_executor_<contract32>_<slice_id>_<slice_token16>_v1`; ordinary v2 appends its current failure; parent verification v2 is `..._<slice_id>_<slice_token16>_vf_<host_review_evidence32>_v2`. It also requires the pending bounded review, terminal v1, parent dispatch, attempt one, and exact `fork_turns=1`. Opaque stall diagnosis without a visible stall contract must replan rather than weaken authorization.
- Fold records into complete generations: pending request, live Start, result-pending follow-up, and terminal Stop. A Stop without status is still terminal with persisted `unknown` status. Only exact bound request/Start and final result markers may reach assessor or executor checks. A slice's `succeeded` result is only a candidate until the parent's exact final `EXECUTION_REVIEW ... passed`; only the last accepted slice seals global success. Duplicate, orphan, empty, failed, or late events remain failures/no-ops.
- A pending request is consumed atomically. Concurrent Starts for one request yield one live generation; a loser cannot downgrade into an unbound lane. A second executor or later slice cannot silently displace the current slice.
- Reusing an agent ID requires a newer persisted request. After reuse, a Stop must correlate to the current request fingerprint or turn; otherwise it is ambiguous and must not close the new generation. Exact bound Simple or stall-result markers may correlate a result-pending follow-up that has no second Start.
- Preserve all pending, result-pending, live, and current bound assessor/executor generations. If protected generations reach the safety ceiling, deny another spawn and reconcile; never discard evidence to make room.

## Cleanup and compaction

Keep the newest 10 complete terminal generations and remove only older complete terminal groups. Do not trim an event tail that can split request/Start/Stop evidence. Counts, delegation gates, compact checkpoints, and resume summaries must use the same folded lifecycle view. Persist only bounded identifiers, fingerprints, enums, result length, and timestamps; never raw prompts, results, commands, or task text.
