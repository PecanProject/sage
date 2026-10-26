# The PEcAn curation workbook

SAGE's output target. PEcAn curates published experiments into one spreadsheet per dataset for crop-model calibration and validation (the MAGiC project), following David LeBauer's *Calibration and Validation Data Collection Protocol* (see [sources.md](sources.md)). Later steps convert the workbook to model inputs, BETYdb rows or PEcAn `events.json`; SAGE stops at the workbook.

`sage export RUN` writes it (`--format xlsx`, or `csv` for one CSV per tab in a zip). The code is `workbook` in `sage/pipeline/export.py`; the rules SAGE applies are in [domain.md](../domain.md#export).

## Rules the format carries

From the protocol, and visible in both MAGiC workbooks:

- **Capture first, harmonize later.** Values in the units the paper prints, statistics as published. No unit or statistic conversion during curation (§2, §11).
- **Raw over summary.** Replicate-level rows (`observation_level = replicate`, `n = 1`) when the source has them; summaries are labelled as summaries (§8.2).
- **Uncertainty is explicit.** Dates are `min_date`/`max_date` windows bounded only by what the source states; a month or season is its first to last day, explained in `notes` (§10.1). Relative timing ("before planting") leaves the dates blank (§10.2).
- **Stable text keys.** Sites, treatments, methods and variables are keyed by readable text, never database ids. A key holds one concept: a site key holds no treatment (§5.1).
- **Long format.** One observation row is one source × site × treatment × replicate or summary × time window × variable (§6.5).
- **Depth outside names.** Soil depth goes in `min_depth`/`max_depth` (cm), not in variable or method names (MAGiC README, 2026-05-24).
- **Events are not treatments.** A treatment is the condition; a management row is one dated event of one treatment (§6.6, §9.3).

## Tabs and columns

Columns in the order the curators use, then what SAGE adds. "SAGE source" names the IR field each column comes from. Every tab but README ends with `sage_id`, `status` and `review`.

### observations

| Column | Meaning | SAGE source |
|---|---|---|
| `min_date`, `max_date` | Earliest and latest plausible date, ISO | `temporal_info` interval; a season by the site's hemisphere |
| `crop`, `variety` | Crop common name and cultivar | `crop_id` → Crop (or `species_id` → scientific name) |
| `sitename` | Site key | `site_id` |
| `variable` | Variable key (was `trait`) | `variable_id` |
| `method` | Method key | `method_id` |
| `value` | The value in reported units (was `mean`) | `value.reported_numeric_value`, or the text ("<0.1") |
| `n` | Replicates the value summarises; 1 for a replicate row | `n` |
| `statname`, `stat` | SE, SD, MSE, CI, LSD, HSD, MSD or P, and its value | `statistical_encoding` |
| `treatment_id` | Treatment key | `treatment_id` |
| `replicate_id` | Plot or block label | `replicate_id` |
| `study_year` | Experimental year, when a dataset counts them | left blank |
| `min_depth`, `max_depth` | Sampling depth, cm; required for soil carbon | `depth` |
| `citation` | DOI of the row's source | the Citation's DOI |
| `fill_status` | Where the value came from | `PAPER_TABLE` or `PAPER_TEXT` |
| `notes` | Caveats a second curator needs | `notes`, plus date notes |
| `dataset_id` | The source packet | `authorYYYYabc` of the paper |
| `reported_units` | Units as printed | `value.reported_units` |
| `observation_level` | `replicate`, `treatment_mean`, `aggregated_mean` | `is_raw_replicate_level`, else `reported_effect_scope` |
| `attributes_json` | Overflow | the statistic as printed when `statname` differs, and the value's `converted_value`, `converted_units` and `conversion_formula` when code converted it |

SAGE adds `reported_effect_scope`, `aggregated_over_factors` (protocol §16.1), `relative_timing`, `relative_timing_days` (§10.2), the printed time, depth, variable and value, `source` ("Table 3, p. 6"), `quote`, `sage_id`, `status`, `review` and `status_reason`.

### managements

`event_id`, `sites.name`, `treatments.name`, `min_date`, `max_date`, `mgmttype`, `cover_crop`, `crop_name`, `level`, `units`, `notes`, `attributes_keyvalue`, `citation`. One row per event and treatment. SAGE adds `relative_timing`, `relative_timing_days`, and the printed date and amount (`reported_date`, `reported_amount`).

- `mgmttype` is `Management.event_type`. PEcAn's `events_schema` v0.1.2 allows `planting`, `harvest`, `irrigation`, `fertilization`, `tillage` (and `leafon`, `leafoff`); the workbooks add `compost_application`. SAGE also names `mowing`, `herbicide`, `pesticide`, `weeding`, `liming`, `burning`, `grazing` and `thinning`, and keeps any other event in the paper's words.
- `level` and `units` are `amount` as printed.
- `cover_crop` and `crop_name` come from the event's Crop; `attributes_keyvalue` carries `material=…` and the crop's `cultivar=…`.
- An event that applies to every treatment is one row with `treatments.name` blank.

### The other tabs

| Tab | Curators' columns | SAGE source |
|---|---|---|
| citations | `citation_id`, `author`, `year`, `title`, `doi` | Citation; `citation_id` is `authorYYYYabc` (§6.1) |
| sites | `name`, `latitude`, `longitude` | Site id and decimal degrees; SAGE adds elevation, the printed name, country, state, city, soil |
| treatments | `name`, `definition`, `start_date`, `end_date`, `control` | Treatment id and definition; dates from its Study's `period`; `control` yes/no. SAGE adds `label`, `factor_levels`, `sitename`, `study` and `citation` |
| methods | `method name`, `description`, `citation_id` | Method id and description; SAGE adds `label` |
| variables | `name`, `description`, `units`, `notes` | Variable id; the printed name when there is no description; SAGE adds `label` |
| crops | `scientific name`, `common name`, `notes` | Crop with its Species; SAGE adds `variety` and `cover_crop`; a Species with no Crop is a row too |
| treatment_pairs | `pair_id`, `treatment_id_1` (baseline), `treatment_id_2`, `comparison_factor`, `comparison_label`, `use_for_validation`, `notes` | TreatmentPair |
| coverage | `System`, `Treatment contrast`, row counts, `Initial data sources`, `Notes` | Coverage, only when the run has any |
| README | Dataset name, sources, design, factors and levels, flag definitions, what was not extracted | Citation, Studies, Treatments, table decisions, unresolved marks |

## What the curators still add

SAGE leaves these to a person, because the paper alone doesn't decide them: `use_for_calibration` and `use_for_validation` on rows and pairs, `healthy_soils_practice` on pairs, `study_year` numbering, the coverage planning tab, and data from archives or supplements that aren't in the PDF.

## Not yet decided by PEcAn

From the protocol's open TODOs and the IR spec's "Remaining mentor decisions":

- No controlled vocabulary for variable names yet. The two MAGiC workbooks disagree (`total_C_pct` and `total_carbon_pct`), so SAGE only asks for the `<quantity>_<units>` style.
- Whether `citation_id` or the DOI is the canonical citation key. SAGE writes both.
- A design tab versus a README section (§7.1, §16.2). SAGE writes the design in the README.
- How to encode repeated measures and plot hierarchy (§7.2, §18).
