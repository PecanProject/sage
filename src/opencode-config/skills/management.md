# Skill: management / treatment extraction

Use when extracting `Treatment` or `Management` records.

- `Treatment.study_id` may be `UNRESOLVED` when processing one paper in
  isolation — this is expected, not a gap to fill by guessing. Study
  reconciliation across citations is a separate downstream step.
- `Treatment.name`/`definition`: keep the source's own label. If a name
  looks like it encodes a continuous quantity (e.g. `n_fert_100kgha`), that
  is allowed if it's literally the source's own label — `propose_record`
  will surface a non-blocking warning, not a rejection, in that case. Don't
  strip real source-label information to avoid the warning.
- `Management.date` and `Management.amount` must never be `INFERRED` —
  `EXTRACTED` or `UNRESOLVED` only. `Management.event_type` (occurrence) MAY
  be `INFERRED` — e.g. inferring a harvest occurred because yields are
  reported, even without an explicit "we harvested on..." statement.
- `Management.treatment_ids` may be `UNRESOLVED` when the source doesn't
  state which treatments an event applies to — non-emptiness is only
  enforced once resolved.
- A control-plot management event conceptually applies to every treatment
  in the experiment — represent that explicitly by listing every applicable
  `treatment_id`, not by leaving `treatment_ids` incomplete.
