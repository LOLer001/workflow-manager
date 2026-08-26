# Assessment liveness

Schema 28/writer 1.0.48 records bounded progress and budget evidence for the one active Hard assessor. It has no total assessment deadline: 600 seconds is an activity observation, exactly 1200 seconds remains live, and elapsed time alone never produces `assessment_timeout`, `blocked`, `exhausted`, or a terminal recovery state.

Only a new progress digest attached to the current binding, agent, and monotonic sequence resets idle. Repeated `running` status, parent wait/list calls, duplicate delivery results, stale lifecycle events, compaction/resume, and clock rollback do not. Strictly after 1200 seconds with no progress, inspect the current step and remaining byte/node budget, then issue an idempotent status/unblock request, diagnose the cause, or split the step within the same authorization envelope. Never lower acceptance to fit the budget.

If the live assessor has actually stopped with a typed failure, recovery follows the shared evidence-driven sequence contract rather than a time-based replacement: never revive the terminal child or overlap a replacement; reject the same failure fingerprint without new evidence; allow a fresh sequence when evidence, root cause, or material correction changed. Three or more distinct fingerprints remain recoverable while the bounded state budget permits.

Schema 27 migration preserves the canonical journal, authorization envelope, confirmation, accepted slices, and profile-v10 evidence. An active verified assessor is re-anchored at its first Schema 28 observation, so historical elapsed time cannot retroactively stall it.
