# Confirmed-execution stall recovery

Use this reference only after a confirmed Hard plan has one bound executor. It extends the single invocable `workflow-manager` Skill; it is not a second Skill.

## Trigger on strong evidence, not every error

An ordinary first implementation, build, deploy, or verification failure keeps the existing typed lower-profile recovery path. Do not spend a high-tier turn on a routine compiler error that the executor can correct locally.

Escalate only when the live bound executor has a current typed failure, cannot make progress within the confirmed contract, stops mutation, and ends with exactly one independent line:

`EXECUTION_STALL contract_id=<32hex> failure_kind=<typed-kind> evidence_digest=<32hex>`

The Hook first verifies the canonical journal, then binds the stall to the current objective, canonical revision/journal digests, execution contract, executor attempt, and failure kind. Wrong caller/status/contract/type, embedded or duplicate markers, external journal drift, or a second stall on the same contract exhausts or invalidates the route instead of opening a loop.

## One high-tier read-only diagnosis

Reuse the original objective-bound high assessor with one exact `followup_task`; do not create another diagnostician. Bind `stall_id`, assessor binding, objective fingerprint, execution contract, and `mode=read_only`. Until diagnosis completes, block executor recovery, parent/old-executor mutation, builds, deployment, device actions, and unrelated agent creation.

Validation and pending reservation are one state-lock transaction: concurrent identical follow-ups yield exactly one delivery. An explicit delivery error permits one normal retry; a second explicit error exhausts. An unknown host response may already have delivered the task, so treat it as unconfirmed diagnosing, never automatic failure/retry, and accept only the bound assessor's later result.

The assessor rereads the canonical current revision and ends with one exact line containing the current stall/binding/plan/contract and `outcome=resume|replan` plus a remediation digest. It remains read-only:

- `resume` means the remedy stays within the confirmed objective, plan, ownership, acceptance, and rollback. Store only its digest.
- `replan` means scope, ordering, risk, or acceptance must change. Invalidate the old plan/executor contract, append one complete replacement revision to the same session journal, and return to strict user confirmation.
- Missing, malformed, stale, failed, or mismatched diagnosis exhausts; never infer success from prose.

## Resume the prior execution profile

For `resume`, the recovery request must bind the exact stall/remediation and canonical journal digests, require rereading the current revision, name the typed failure, and provide a substantive correction. Restore the profile that was bound before the stall: normally lower-tier+medium; if the user explicitly enabled whole-session `highest_throughout`, restore that highest profile instead of silently downgrading. Host acceptance/echo rules remain unchanged.

Successful implementation and verification resolve the stall. Any resumed execution failure becomes terminal for this stall and returns to replan or user-visible blockage; it never launches a second high diagnosis. Compaction/resume and Schema migration retain only bounded fingerprints, enums, attempts, profile, timestamps, and canonical bindings in state—never raw errors, prompts, commands, plans, or child results; plan semantics are reread from the journal.
