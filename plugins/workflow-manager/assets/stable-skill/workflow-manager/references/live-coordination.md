# Live cross-task coordination

Use this reference only when multiple Codex tasks may contend for the same build account, build server, artifact-delivery stage, or ADB device. It extends the single invocable `workflow-manager` Skill; it is not a second Skill.

## Notify only a live conflict

1. Immediately before a conflicting build, deploy, flash, reboot, install, or device-verification stage, call the host's current `list_threads` tool. Never infer activity from an old session file, task title, prior completion, or a message sent earlier.
2. The current task and peer must both appear in that successful bounded snapshot as `active`, on the same host, with different task IDs. `idle`, `notLoaded`, `completed`, missing, unknown, malformed, ambiguous, or older-than-60-second evidence closes coordination.
3. Establish from current task evidence that both sides actually name the same canonical resource and that their stages conflict. Activity alone is not resource proof. If the resource or stage is uncertain, do not notify; continue unrelated work and recheck only when entering the shared stage.
4. Send one strict `WORKFLOW_COORDINATION_V1` envelope to the exact peer. The Hook binds source/target host+task fingerprints, equal resource fingerprints, resource kind, symmetric conflicting stages, positive lease generation, and `blocked|released` transition. It never intercepts ordinary task messages merely because they mention build, lock, device, or release.

## Lease and retry rules

- A `blocked` generation must increase monotonically for the same peer/resource/conflict scope. A `released` notice must match the latest blocked generation, owner, resource, and phase; an old release cannot unlock a newer claim.
- Validation and pending reservation are one state-lock transaction, so concurrent identical sends yield exactly one pending notice. `pending`, sent, unconfirmed, or exhausted notices are terminal for the same generation/transition.
- After an explicit send failure, refresh `list_threads` and allow one normal retry only. A second failure is exhausted. An unknown host result is `unconfirmed`, not assumed failed and not retried automatically.
- Stop notifying once the peer is no longer active or the local workflow leaves the conflicting stage. Do not send periodic occupancy/release chatter to an idle or completed task.

## Inbound and privacy boundary

A complete current envelope is a control message: record only bounded fingerprints, enums, generation, attempt, and time, and leave objective, assessor, plan, executor, reference, and causal contracts unchanged. Invalid, mixed, prefixed, or legacy `<codex_delegation>` controls are ignored or migration-rejected without becoming a new objective. Compaction/resume never retains raw task IDs, host IDs, titles, summaries, resource names, or message text.

The Hook verifies freshness, topology, structure, generations, and one-shot state; it cannot independently prove the semantic truth of a declared resource. The parent must base that declaration on current host/task evidence and must not manufacture a lock merely to serialize unrelated work.
