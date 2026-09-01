# Assessment liveness

Schema 34/writer 1.0.66 records bounded event evidence for the one active Hard assessor. It has no elapsed-time policy: clocks, waits, polling, and repeated status observations never produce a workflow action, replacement assessor, `assessment_timeout`, `blocked`, `exhausted`, or recovery authority.

Only a new progress digest attached to the current binding, agent, and sequence is recorded as progress. The model natively decides whether to keep waiting, diagnose, or report a blocker. Workflow Manager changes authority only from exact host lifecycle facts, never from silence or elapsed time.

If the assessor actually stops or its host lifecycle conflicts, the current Hard envelope remains fail-closed. Never revive it, overlap it, or reserve a second assessor for the same envelope. A materially new authorization envelope may start its own single assessment.

Historical liveness timestamps are bounded migration evidence only and have no execution semantics.
