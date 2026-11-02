# PEcAn sources

What PEcAn shared about the target format, what each document says, and what SAGE took from it. The documents themselves are not in the repository; ask the PEcAn team for current copies.

| Source | What it is | What SAGE took |
|---|---|---|
| *Calibration and Validation Data Collection Protocol*, David LeBauer (working draft, 2026) | How to curate published datasets into the calibration-validation workbook. "Based on the BETYdb data entry docs, but does not assume a web based UI, API, or PostgreSQL database." | The target format and its rules: [workbook.md](workbook.md). It overrides the BETYdb-upload goal of the older proposal. |
| *MAGiC Calibration Validation Dataset (White and Nichols)*, xlsx | A curated workbook: White et al. 2020 (Salinas SOCS, Data in Brief and PLoS ONE) and Nichols 2024 (almond, compost). Its README defines every column and records curation decisions. | The exact tab and column layout, `fill_status`, `observation_level`, depth columns, method names without depth, SE rows with `n`. |
| *MAGiC Calibration Validation Data: Russell Ranch*, xlsx | The same layout at scale: Tautges et al. 2019 and Wolf et al. 2018, 20k observations, 3k management events. | Confirms the layout is the one in use. Shows `mgmttype` values in use and that variable names are not yet harmonized across datasets. |
| *BETYdb trait data upload template*, xlsx | BETYdb's bulk-upload sheets: `datetime, crop, cultivar, sitename, [trait…], [covariate…]` (wide) or `datetime, crop, variety, sitename, trait, method, mean, n, statname, stat` (long). | The workbook's observations tab is this long form renamed (protocol §16.1), so a BETYdb file is a later column mapping plus `dateloc`. |
| *IR Architecture Specification v1.0*, Abhishek | The intermediate representation between paper and workbook: entities, `ExtractedField` provenance, `QuantityValue`, `DateRange`, invariants. | SAGE's IR (`sage/ir/schema.py`) follows it, with `Field` in place of `ExtractedField` and the Study entity added. Its whole-dataset rule that an Observation's site matches its Treatment's is a checker rule. |
| *LLM-Assisted Extraction of Agronomic and Ecological Experiments into Structured Data*, Abhishek (GSoC work plan) | Extraction, deterministic reconstruction, validation, and deferred harmonization as separate steps; scientist review before export; export to BETYdb bulk-upload CSVs, tested in a local BETYdb. | The separation SAGE already has, and two rules: a vocabulary match is never assigned silently, and every exported value traces to its source. Marker, Streamlit, Jetstream2 and the week plan don't apply. |
| `events_schema_v0.1.2.json`, PecanProject/pecan `modules/data.land/inst/` | PEcAn's standard for management `events.json`: `event_type` ∈ planting, harvest, irrigation, fertilization, tillage, leafon, leafoff, with model-facing fields (`org_c_kg_m2`, `amount_mm`, `tillage_eff_0to1`, …). | `Management.event_type` names. The model-facing fields are later conversion targets, never curation fields (protocol §15.3-15.4). |

## Where SAGE differs from the IR spec

- `Field` has `basis` (`stated`, `derived`, `inferred`) and evidence, instead of a provenance label and a confidence score. Status (`verified`, `flagged`, `unresolved`) is set by code at settle.
- The Study entity groups treatments by experiment. `Study.period` gives the workbook's treatment start and end dates.
- `Observation.n` and `Observation.depth` are fields of their own, for the workbook's `n`, `min_depth` and `max_depth`.
- `Management.crop_id`, `material` and `notes`, and `Crop.cover_crop`, carry the workbook's `crop_name`, `cover_crop`, `attributes_keyvalue` and `notes`.
- `dataset_id` and the `authorYYYYabc` citation key are derived at export, not stored.

## Reference workbooks

The two MAGiC workbooks were curated by hand from papers SAGE can read, so a person can compare SAGE's export with them: White et al. 2020 (Data in Brief, 10.1016/j.dib.2020.106481; PLoS ONE, 10.1371/journal.pone.0228677), Tautges et al. 2019 (10.1111/gcb.14762) and Wolf et al. 2018 (10.1002/ecy.2105). Many curated rows come from archived supplements rather than the PDF, so compare only what the paper itself prints.
