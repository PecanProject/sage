# Entities

Each file in `records/` holds one entity type as `id: attributes`. **Required** attributes must be present, as a value or as `[null, note: …]`. Optional ones are left out when the paper doesn't give them. `citation_id` is left out when there is one citation.

## citation.yaml: Citation

This paper itself, never a work it cites.

| Attribute | Required | |
|---|---|---|
| `author` | yes | as the byline prints it |
| `year` | yes | |
| `title` | yes | |
| `persistent_identifier` | yes | the DOI, often in a page header or footer |

## studies.yaml: Study

One experiment with its own design, site and years. A paper with a greenhouse trial and a field trial has two.

| Attribute | Required | |
|---|---|---|
| `citation_ids` | yes | plain list; left out with one citation |
| `design` | no | as stated: "randomized complete block with four replicates" |
| `period` | no | when it ran, as printed: "2003-2011" |

With one Study that cites evidence, code points every Treatment at it.

## sites.yaml: Site

A place where an experiment ran.

| Attribute | Required | |
|---|---|---|
| `name` | yes | |
| `country`, `state_or_region`, `nearest_city` | no | as printed |
| `latitude`, `longitude` | no | one coordinate as printed, `"38°32′N"`; code converts it |
| `elevation` | no | a quantity, `"270 m"` |
| `soil_context` | no | soil series or texture |
| `description` | no | |

## species.yaml: Species

One for every organism measured, cover crops and weeds included.

| Attribute | Required | |
|---|---|---|
| `genus`, `species_epithet` | yes | |
| `scientific_name` | yes | starts with "Genus epithet", may carry the authority |
| `common_name` | no | a synonym goes here or in a note |

Use the name the paper leads with.

## crops.yaml: Crop

A species as grown: one per cultivar the paper reports separately, and one per crop a Management plants or harvests.

| Attribute | Required | |
|---|---|---|
| `species_id` | yes | plain id |
| `cultivar`, `common_name` | no | |
| `cover_crop` | no | true for a crop grown as a cover crop |
| `notes` | no | |

## methods.yaml: Method

How a variable was measured: instrument, sampling and protocol. Depth is not part of a Method.

| Attribute | Required | |
|---|---|---|
| `name` | yes | from the section heading or the sentence's subject, so it is stated |
| `description` | yes | |

## variables.yaml: Variable

One measured quantity, named and with its units as printed. One per quantity, not per value, depth or date: soil carbon at four depths is one Variable. Reuse it when the text restates a table's quantity in other words.

| Attribute | Required | |
|---|---|---|
| `name` | yes | |
| `units` | no | as printed; left out for a unitless variable |
| `description`, `notes` | no | |

## treatments.yaml: Treatment

One combination of factor levels that values are reported for.

| Attribute | Required | |
|---|---|---|
| `site_id` | yes | plain id. A treatment run at several sites is one Treatment per site. |
| `name` | yes | |
| `definition` | yes | your wording is fine when it says more, as derived |
| `factor_levels` | no | plain map `{<factor>: <level>}` for every factor it fixes, levels spelled as in the tables |
| `control_status` | no | true only for the stated control or baseline |
| `study_id` | with several Studies | `[<study id>, anchor]`, citing the text that places it; with one Study, code fills it in |

- Once one level of a factor is the baseline, the other levels are false. With no named control, leave `control_status` out.
- When tables report main effects (values averaged over another factor), write the main-effect Treatments they use, with only that factor in `factor_levels`.
- Every level a table reports gets a Treatment of its own. Two values for one Treatment that nothing else tells apart (the same variable, site, time, depth, method, replicate, scope and unit) block.

## pairs.yaml: TreatmentPair

A comparison the paper draws.

| Attribute | Required | |
|---|---|---|
| `treatment_id_1` | yes | plain id: the baseline |
| `treatment_id_2` | yes | plain id: the comparison |
| `comparison_factor`, `comparison_label` | yes | |
| `use_for_validation` | no | |
| `notes` | no | plain text |

## managements.yaml: Management

One field event per record, irrigation and weeding included.

| Attribute | Required | |
|---|---|---|
| `event_type` | yes | cite the sentence with the verb ("transplanted", "side-dressed", "disked"); code names the type |
| `date` | yes | as printed; never inferred. Null with a note when the paper doesn't give it. |
| `amount` | no | a quantity; never inferred |
| `material` | no | what was applied, as printed: "urea" |
| `crop_id` | no | plain id of the Crop planted or harvested |
| `treatment_ids` | no | `[[N60, N120], p3.b5]`; left out when it applies to all |
| `notes` | no | |

The event types code knows: planting, harvest, irrigation, fertilization, tillage, compost_application, mowing, herbicide, pesticide, weeding, liming, burning, grazing, thinning. Any other event keeps the paper's words.

## coverage.yaml: Coverage

Skip it unless the paper states how much data exists. Plain attributes: `site_id`, `variable_id`, `system`, `treatment_contrast`, `daily_rows`, `annual_rows`, `seasonal_rows`, `static_observation_rows`, `weather_rows`, `initial_data_sources`, `notes`.

## Observations

Observations are written as groups in `records/observations/`, never one by one. A group or a row, column or cell can set:

| Key | What it gives each value |
|---|---|
| `treatment`, `variable`, `site`, `method`, `crop`, `species`, `citation` | the record ids it refers to; treatment, site and variable are needed |
| `time` | when it was measured, as printed. Bare text (`time: "2019"`) is stated when the table's header, caption or notes hold it; otherwise use a field shape. |
| `depth` | the soil layer sampled, for a soil value; bare text or a field shape, like `time` |
| `n` | how many replicates the value summarises. A value with a ± or letters needs it. |
| `units` | only when the header, the row label and the Variable all miss it |
| `statistic` | `statistic: SE`, only when no header, caption or note names the ± (it is then inferred) |
| `letters` | `letters: Tukey HSD at P<0.05`, only when no text says what the letters mean |
| `notes` | anything a reviewer should know |
| `replicate` | the plot or block a raw value comes from, as labelled |

Only the group sets these, for all its values:
- `scope`: `treatment_mean` for a value of one treatment, or `aggregated_mean` with `over: [factors]` it is averaged over. Code checks the scope against the table's caption and notes, and the `scope_at` sentences. An aggregated mean needs a text that pools the values and names every factor ("means over two cultivars", "averaged across years"). Averaging over replicates, blocks or plots pools nothing. A value for a main-effect Treatment is usually averaged over the other factors.
- `scope_at`: anchors of sentences that say what the values are means of.
- `raw: true` for individual replicate or plot values, `raw: false` for means. Code reads it from the cell first: a ± or letters mean a mean. Left out, the values are means only when a `scope_at` sentence says so; otherwise the field stays null.

A table group starts `- table: <anchor>` and adds `rows`, `columns`, `cells`, `skip`, or `irrelevant: reason`. A text group starts `- text: true` and lists `values: {<id>: {value: [...], ...}}`.

## Site-level data

Weather, initial soil properties and other values measured once for a site, not per treatment, have no Observation: every Observation belongs to a Treatment. Don't invent a Treatment to hold them, and don't call their table irrelevant. Mark the table, or the text, in `records/unresolved.yaml` with `schema_limitation`: `p2.t1: {cause: schema_limitation, reason: yearly rainfall per site, not per treatment}`. A soil description goes in the Site's `soil_context`.

## Figures

Values only in figures are not extracted. Write a Variable for each quantity only a figure shows and mark it `figure_only`. A figure that splits a table's quantity further (by mixture, by date) shows a separate Variable.
