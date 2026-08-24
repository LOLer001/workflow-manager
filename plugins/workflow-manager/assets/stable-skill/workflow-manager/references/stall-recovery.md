# Confirmed-execution stall recovery

Use this reference only after a confirmed Hard plan has one live bound slice executor. It extends the single invocable `workflow-manager` Skill; it is not a second Skill.

## Trigger on strong evidence, not every error

An ordinary first implementation, build, deploy, or verification failure gets one immediate material correction in the existing execution path. Do not stop or ask the user to operate merely because that first attempt failed.

If the root cause is unknown, risk is critical, or the material correction fails, immediately use the one bound high-tier diagnosis below and then resume. A recoverable host/tool/path/result mismatch is a diagnosis target, not a blocker. Stop only when the remaining condition is external and still unrecoverable after the bounded diagnosis and alternate route.

Escalate only when the live bound executor has a current typed failure, cannot make progress within the confirmed contract, stops mutation, and ends with exactly one independent line:

`EXECUTION_STALL contract_id=<32hex> failure_kind=<typed-kind> evidence_digest=<32hex>`

The Hook first verifies the canonical journal and slice manifest, then binds the stall to the current objective, revision/journal/manifest digests, global contract, current slice/token, accepted-prefix chain, executor attempt, and failure kind. Wrong caller/status/contract/type, embedded or duplicate markers, external journal drift, or a second stall on the same slice exhausts or invalidates the route instead of opening a loop.

## One high-tier read-only diagnosis

Reuse the original objective-bound high assessor with one exact `followup_task`; do not create another diagnostician. Bind `stall_id`, assessor binding, objective fingerprint, execution contract, and `mode=read_only`. Until diagnosis completes, block executor recovery, parent/old-executor mutation, builds, deployment, device actions, and unrelated agent creation.

Validation and pending reservation are one state-lock transaction: concurrent identical follow-ups yield exactly one delivery. An explicit delivery error permits one normal retry; a second explicit error exhausts. An unknown host response may already have delivered the task, so treat it as unconfirmed diagnosing, never automatic failure/retry, and accept only the bound assessor's later result.

The assessor rereads the canonical current revision and ends with one exact line containing the current stall/binding/plan/contract and `outcome=resume|replan` plus a remediation digest. It remains read-only:

- `resume` means the remedy stays within the confirmed objective, plan, ownership, acceptance, and rollback. Store only its digest.
- `replan` means scope, ordering, risk, or acceptance must change. Invalidate the old plan/executor contract, append one complete replacement revision to the same session journal, and return to strict user confirmation.
- Missing, malformed, stale, failed, or mismatched diagnosis exhausts; never infer success from prose.

## Resume the prior execution profile

For `resume`, the recovery request must bind the exact stall/remediation, canonical journal/manifest digests, current slice/token, and accepted-prefix chain; require rereading only the bound plan context; name the typed failure; and provide a substantive correction. Restore the profile that was bound before the stall: normally lower-tier+medium; if the user explicitly enabled whole-session `highest_throughout`, restore that highest profile instead of silently downgrading. Host acceptance/echo rules remain unchanged.

Successful resumed implementation creates only a current-slice candidate; the stall resolves after the parent independently verifies it and returns exact final `EXECUTION_REVIEW execution_contract_id=<32hex> slice_id=sNN outcome=passed`. Parent review failure exhausts attempt two. Any resumed execution failure is likewise terminal and never launches a second high diagnosis. Compaction/resume and Schema migration retain only bounded fingerprints, enums, attempts, profile, timestamps, review, slice/token, accepted-prefix, and canonical bindings—never raw errors, prompts, commands, plans, or child results.
