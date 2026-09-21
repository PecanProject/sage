Real artifacts for tests/test_freeform_dedup.py (item 8: free-form / table Treatment dedup).

- `085532_freeform_treatment_candidates.json`: the free-form Treatment enumeration of real run
  `20260919T085532_98015a8a` (Daren-1997-Canopy), unmodified -- 32 candidates, all citing `b:0606`
  (Table 6's CAPTION block, not its table block `b:0607`) with no `linked_candidates`, which is why
  neither the anchor-based nor the link-based drop removed any of them. They carry no `dimensions`
  (that field did not exist yet).
- `085532_table6_legacy_classifications.json`: the two Step B classifications of that run for Table 6
  (`b:0607`, `b:0656`) in the legacy shape (no declared factors).
- `live_gpt_oss_table6_declared_classification.json`: the classification gpt-oss-120b returned for
  Table 6 (chain `b:0607`+`b:0656`) in the live Step B check with declared factors (Population=crop,
  Maturity=time, Site=site) -- no treatment dimension.
- `live_gpt_oss_felipe_table1_classification.json`: gpt-oss-120b's Step B classification of Felipe Table 1
  (`b:0069`): Cover crop treatment as columns with Fallow/Mustard hints, DAP and Variable as rows.
- `live_gpt_oss_felipe_freeform_production_run.json` / `..._variant_run.json`: the free-form Treatment
  candidates gpt-oss-120b returned for Felipe in the two live validations (production prompt; and the
  scratch variant that withheld the covered list). The first declared the 1-cv/3-cv/5-cv mixtures as
  `crop`, the second as `treatment` -- the inconsistency the mixture-level rule resolves.
