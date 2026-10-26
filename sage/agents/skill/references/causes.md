# Unresolved causes

`records/unresolved.yaml` maps a target to `{cause, reason}`. The reason is one sentence a curator can act on.

| Cause | Use when | Fits |
|---|---|---|
| `absent` | The paper doesn't give it | A null field, or a record that holds no value |
| `ambiguous` | The paper doesn't tie it to one treatment, site or time | A record, a field or a Results block |
| `conflict` | The paper contradicts itself. Give both values in the reason. | A record, a field or a Results block |
| `figure_only` | Shown only in a figure | A Variable with no Observation, or one of its fields |
| `schema_limitation` | The records can't hold it, e.g. a value pooled over every treatment, or a table of site-level data such as weather | Any target |
| `blocked_prerequisite` | A record it depends on can't be written | A record or a field |

Targets:
- a record id: `soil_N_flux`
- a null field: `arlington.elevation`
- a table of site-level data: `p9.t2`, with `schema_limitation` only. Any other table is mapped, or decided `irrelevant: why` in its observations file.
- a Results block a warning names: `p6.b46`

A field that holds a value can't be marked. Fix it, or write `[null, note: …]` and mark that.

SAGE sets other causes itself: `repeated_error` when an error check showed survives three edits of its file, and `grounding_failure`, `validation_failure` or `budget_exhausted` for what is still broken when a run ends.
