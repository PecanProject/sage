# Skill: traits / Observation extraction

Use when extracting `Observation` records (values, statistics, effect scope).

- `reported_effect_scope` is either `"treatment_mean"` or `"aggregated_mean"`
  — never leave `aggregated_over_factors` absent for either:
  - `treatment_mean` -> `aggregated_over_factors` = `EXTRACTED`, `[]`
    (not applicable, but explicitly recorded as such — not the same as
    `UNRESOLVED`).
  - `aggregated_mean` -> `aggregated_over_factors` = `EXTRACTED` with the
    real factor names, or `UNRESOLVED` with a reason — never absent.
- Use `apply_reconstruction(kind="stat_encoding", ...)` when a table gives
  you a mean plus SE/SD/n/95%CI columns — don't hand-build
  `StatisticalSummary` entries when the reconstruction tool can normalize
  the labels for you (it does NOT apply the BETYdb closed vocabulary —
  that's materialization's job, not yours).
- `Observation.citation_id` is a source-of-record pointer only. It does not
  have to match its Treatment's `citation_id` (Study design, Playbook
  Section 5.1) — don't "fix" a legitimate mismatch.
- `Observation.site_id` DOES have to match its Treatment's `site_id` — that
  check is still enforced.
