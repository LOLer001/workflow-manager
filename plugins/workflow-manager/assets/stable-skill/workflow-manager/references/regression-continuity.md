# Regression and acceptance continuity

Read this reference when the user reports a remaining, returned, or new symptom while accepting work completed earlier in the same task. It extends the single invocable `workflow-manager` Skill; it is not another Skill.

## Seal the prior execution baseline

After the confirmed-plan executor finishes, keep one bounded baseline for causal comparison:

- prior objective fingerprint and confirmed `plan_digest`;
- prior `execution_contract_id`;
- digest of successful implementation/build/delivery operations;
- digest of verification performed after the last recorded change;
- bounded acceptance state: `passed`, `failed`, `incomplete`, or `unknown`.

Agent completion is not automatically user acceptance. Store only fingerprints, digests, identifiers, and enums—never the user's report, plan text, commands, tool output, or child result. If no change-set digest exists, do not pretend there is an execution-caused regression to review.

If acceptance later fails but that executor recorded no successful change, do not leave the succeeded contract active and do not invent a causal review. Mark the baseline acceptance failed, invalidate the old executor contract, return to high-reasoning analysis, and prepare one replacement Hard plan for strict confirmation.

## Open a causal review narrowly

A same-task regression report opens a read-only causal review only when it follows a succeeded bound executor with recorded changes and still refers to the active objective. Explicit acceptance success, control/progress questions, and a clearly separate new objective do not open this gate. User wording is only a trigger, never proof of causality.

Before any corrective mutation, compare:

1. the prior objective, confirmed plan, and execution contract;
2. the bounded change and post-change verification baseline;
3. symptom timing and overlap with the changed path, state, interface, or acceptance criterion;
4. input, environment, dependency, device, build, and configuration changes since verification;
5. evidence for the original symptom, the reported symptom, and unaffected surfaces.

Allow targeted reads, searches, safe inspection, reproduction that does not mutate shared state, and clearly read-only investigation agents. Block file/device mutations, mutating Git, builds or deployment used as an unreviewed fix, and any old or replacement executor until the review is bound and resolved.

## Record one evidence-bound outcome

End the causal assessment with the exact machine-readable line requested by the Hook:

```text
CAUSAL_REVIEW baseline_id=<32hex> review_id=<32hex> outcome=<introduced|fix_ineffective|unrelated|uncertain> evidence_digest=<32hex>
```

The IDs must match the active baseline and review. `evidence_digest` identifies the bounded evidence actually inspected; never invent it or derive a conclusion from wording alone.

- `introduced`: evidence shows the prior change created the reported symptom.
- `fix_ineffective`: the intended problem remains or the prior approach did not satisfy its acceptance criterion.
- `unrelated`: evidence separates the symptom from the prior change; reclassify it as a new work objective without reusing the old contract.
- `uncertain`: evidence cannot yet distinguish the causes; remain read-only and gather the smallest missing evidence. Do not plan or mutate on a guess.

## Replan as one coherent system

For `introduced` or `fix_ineffective`, replace the old plan instead of applying a local patch on top of it. The new Hard plan must account for the prior method, regression mechanism, rollback or correction, original acceptance, reported symptom, and risk-based regression checks for adjacent behavior. Request strict confirmation again.

For `unrelated`, detach the symptom from the old contract, reclassify it as a separate Hard work follow-up, and prepare a separately bounded plan and acceptance surface. The old executor, plan digest, and contract remain evidence only. A resolved review binds its `review_id` into the replacement execution contract so a stale executor cannot mutate.

For `uncertain`, report the missing evidence and next read-only check. Do not weaken acceptance, manufacture certainty, or repeatedly run the same inconclusive probe.

## Resume and migration

Schema 11 preserves the execution baseline and causal-review IDs, state, outcome, and evidence digest across compaction. Resume from the native summary plus those bindings; do not reconstruct raw feedback or repeat completed evidence. If the objective, baseline, review, files, inputs, environment, device, or verification freshness changed, take one bounded check and rebind or replan.

When migrating Schema 10, derive only a baseline supported by existing contract and recorded-operation fingerprints. Mark acceptance incomplete and causality unset unless native evidence proves otherwise; migration must never invent user acceptance, a regression, or a causal conclusion.
