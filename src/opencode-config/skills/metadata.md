# Skill: metadata extraction (Citation, Site, Species, Method)

Use when extracting `Citation`, `Site`, `Species`, or `Method` records.

- `Citation`: `persistent_identifier` is OPTIONAL. Extract a DOI when one is
  explicitly present in the source; otherwise omit `persistent_identifier`.
  Do NOT mark the entire Citation `UNRESOLVED` merely because no persistent
  identifier is present. The Citation may be fully resolved using its other
  extracted fields.
- `Site`: never create a separate Site for a plot/block — plot-level detail
  belongs on Treatment/Observation, not Site (IR spec Section 7.2, "Do not:
  create separate sites for each plot"). `latitude`/`longitude` may be
  `UNRESOLVED` at curation; that's expected, not an error to work around.
- `Species`: `scientific_name` must equal `genus + " " + species_epithet`
  (+ optional infraspecific text) — the schema enforces this as a hard
  construction-time check when all three are populated.
- `Method`: `citation_id` may legitimately differ from the paper's primary
  citation (a paper can cite a method from elsewhere) — that's fine, not a
  bug to "fix" by forcing it to match.
