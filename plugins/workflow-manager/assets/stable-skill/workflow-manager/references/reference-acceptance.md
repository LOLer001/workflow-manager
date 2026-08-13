# Reference-driven acceptance

Read this only when the user explicitly requests a supplied reference to be matched (for example “以参考为准”, “对齐”, “复刻”, “一致” or visual/behavioral fidelity). It extends the single invocable `workflow-manager` Skill; it is not a second Skill.

Bind a `reference_contract_digest` into the Hard execution contract. Persist only bounded fingerprints/digests and enum outcomes: authoritative reference/source and permitted-change boundary; A/B device/build hash, orientation, viewport, crop, scene, input and stable/transition phase; static geometry/text/color and dynamic direction/duration/blink/transition axes; user exceptions; and per-axis expected/reference-observed/candidate-observed/evidence/result summaries. Never store media, frames, prompts or large outputs. A reference, version, orientation or phase change invalidates the contract and old fidelity result.

Reject locked/occluded, wrong scene/orientation/version/state, stale, static-for-dynamic, and unequal A/B evidence. Build/lint/no-crash/hash/frame delta are engineering health only: they cannot prove fidelity. Keep engineering health, functional acceptance, fidelity candidate and user final acceptance distinct. A structural or clearly visible difference is `failed`/`candidate`; without an explicit user-approved objective threshold only the user can mark final `accepted`.

If the same objective is corrected, invalidate the old acceptance as `failed`, run causal review, and require a replacement plan. Report candidate differences and remaining risk; do not imply user acceptance.
