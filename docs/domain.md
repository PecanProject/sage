# SAGE domain reference

This page defines the words SAGE uses, the shape of the extracted data (the IR), the evidence rules, the checker, and how code settles each value's status. The code is the source of truth. Each section names the module it describes.

The agent's instructions, the SAGE skill in `sage/agents/skill/` (`SKILL.md` and its `references/`), are written from this page. When a rule here changes, change the skill in the same edit. The words a scientist sees in the UI are mapped in `docs/design/04-ui.md`.

## Glossary

| Term | Meaning |
|---|---|
| anchor | The id of one place in the paper: a text block, a table, a table row or a table cell. See [Anchors](#anchors). |
| basis | How a field's value relates to the paper: `stated`, `derived` or `inferred`. |
| blocking issue | A checker issue that makes `done` refuse. At settle, it makes its target unresolved. |
| cause | Why a value or record is unresolved. See [Causes](#causes). |
| check | `sage extract check`: compile the agent's files, run the checker, and answer with `file:line` findings. Every driver calls it. See [The paper folder](#the-paper-folder). |
| checker | The deterministic code in `sage/pipeline/checker/` that tells the agent what is wrong or missing. |
| document | The parsed paper in `.sage/document.json`: pages, text blocks and detected tables with boxes. The effective document adds the agent's table transcriptions and scan-page blocks. Each run keeps a copy of the one it was last checked against. Model in `sage/parse/document.py`. |
| done | `sage extract done`: check once more and, when nothing blocks, settle the run. |
| draft | What the agent's files compile to: the records, the table decisions, the unresolved marks, the overwrites and the summary. Each check writes it to `.sage/runs/<run_id>/draft.json`. Model in `sage/ir/draft.py`. |
| driver | Who drives a run: `sage` (SAGE's own agent on an API model), `agent` (a coding agent SAGE starts over ACP) or `here` (whoever ran `sage extract start` in a terminal). `RunInfo.driver`. |
| entity | One of the 12 record types in `sage/ir/schema.py`. |
| evidence | An anchor plus a verbatim quote of the text at that anchor. |
| export | The run's records as the PEcAn curation workbook (xlsx, or one CSV per tab), or as JSON. See [Export](#export). |
| field | A `Field[T]`: a value with its basis, evidence and note. |
| field shape | One of the five ways the agent writes a field in YAML, from which code works out the basis and the quote. See [Field shapes](#field-shapes). |
| grounding | Matching each cell of an agent-transcribed table (`tables/<anchor>.csv`) to characters in the PDF text layer. A grounded cell gets its box and its verbatim text from the text layer. See [Anchors](#anchors). Code in `sage/parse/grounding.py`. |
| issue | One checker finding: a level, a severity (`blocking` or `warning`), a target and a message that says what to do next. |
| open issue | An issue whose target, and whose target's record, carry no standing unresolved mark. |
| observation group | A list item in `records/observations/*.yaml` that writes many Observations at once: a table's rows and columns, or a set of values from the text. See [Observation groups](#observation-groups). |
| outcome | How the run ended: `finished`, `budget_exhausted` or `failed`. `Outcome` in `sage/ir/settled.py`. |
| overwrite | A logged replacement of a model-supplied value by a value code computed. |
| paper folder | `papers/<paper_id>/`: the PDF, what SAGE writes for the agent to read, the agent's files, and SAGE's own state in `.sage/`. See [The paper folder](#the-paper-folder). |
| paper view | The anchored text the agent reads, `paper.md` in the paper folder, built by `sage/agents/paper_view.py`. |
| record | One instance of an entity, with an `id` that is unique within the run. |
| review | A scientist's `accept`, `edit` or `reject` of one record, appended to `reviews.jsonl`. |
| run | One extraction session on one paper, from start to done or stop, in `.sage/runs/<run_id>/`. The paper folder carries on from one run to the next. The UI calls it an extraction. |
| settle | The code step that turns the draft into `records.json` and gives every record and field a status. Code in `sage/pipeline/settle.py`. |
| status | `verified`, `flagged` or `unresolved`. Only settle sets it. |
| snapshot | A copy of the agent's files, `.sage/snapshots/<n>/`, taken by each check that compiles when the files changed since the last one. |
| strike | The count of one error message on one target. See [The same-error rule](#the-same-error-rule). |
| table decision | Whether a table was mapped (`expanded`, with its observation ids and the rows or cells skipped on purpose) or marked `irrelevant` with the reason. |
| target | What an issue or an unresolved mark points at: a record id, `record_id.field`, a table anchor, or `results` for the Results-coverage warning. |
| unresolved mark | A `{target, cause, reason}` entry in the draft. The agent writes marks in `records/unresolved.yaml`, and the same-error rule adds them too. A mark from code stands while its target still has a blocking error. A mark from the agent stands only while its target holds no value, so it never hides a failing value. On a whole record it stands only for a cause that fits a whole record (see [The done gate](#the-done-gate)). `Draft.mark` decides. |

## Anchors

Every anchor resolves to a page, a box and a text through `Document.resolve`. The review UI draws evidence boxes from those boxes.

| Anchor | Points at |
|---|---|
| `p{page}.b{n}` | Text block `n` on page `page` |
| `p{page}.t{n}` | The `n`th `Table N` caption on page `page`, and the table under it |
| `p{page}.t{n}.r{i}` | Row `i` of that table |
| `p{page}.t{n}.r{i}.c{j}` | The cell in row `i`, column `j` |

- Pages, blocks and tables count from 1. Rows and columns count from 0, and header rows come first.
- The `t` number counts captions on the page. It is not the paper's own table number, so the paper's "Table 3" can be `p6.t1`.
- A box is `(x0, y0, x1, y1)` in PDF points, with the origin at the top left of the page.
- A cell's text is the text layer's own text when the cell grounded, and the transcription when it did not. A cell that did not ground uses the table region as its box.
- Grounding matches cells in reading order inside the table region, leaving out the caption, whose words are not the table's. A value cell grounds on characters of its own, each used once, so a value transcribed twice but printed once grounds only once. A header or a row label (a body cell that starts with a letter and holds a word of three letters, such as "Experiment 1") that did not ground exactly on characters of its own is composed from page text that governs it, when every word is printed there. That is how one spanning header ("Weight per reproductive tiller" over "Arlington" and "Brookings") or one stub label printed beside several rows grounds every cell the CSV repeats it in. Each run of that text is one printed line, read left to right, never a value cell's characters. A header's runs are printed over its column (on the column, or centred within the gutters on either side of it) and above the body, top to bottom. A row label's runs are printed over its column at or above its row; the column prints nothing else on that row, and nothing on the rows in between but what their own cells say. A header or label the page does not print there keeps whatever match it made on its own characters, if any.
- A cell's text, like a text block's, has a space only where the page shows one (a new line or a visible gap) or the text layer writes a space character. A line break pdftext adds at the end of its own lines is not one. An exponent stays on its unit ("g m−2"), and a smaller run after a number keeps its space, so letters and footnote calls stay their own token ("12.8 a"). `spaced` in `sage/parse/layout.py` decides for both.
- A table's cells come from the agent's `tables/<anchor>.csv`. `effective_document` in `sage/pipeline/paper_files.py` grounds each CSV at check time and caches the result in `.sage/tables/<anchor>.json` by the CSV, the grounding code (`GROUNDER_VERSION`) and its settings, so an unchanged table is not grounded again. A table with no CSV has no cells and shows as not transcribed.
- When the parser changes (`PARSER_VERSION`), the next run SAGE drives parses the PDF again. The tables keep their CSVs, so every cell keeps its anchor and is grounded again on the new text layer.
- A run keeps the document it was last checked against in `.sage/runs/<run_id>/document.json`, written by each check and by done. Its records, `sage show`, the review screen and settling read that copy (`run_document` in `sage/pipeline/run.py`), so a later reparse or a later run's table edits never move a run's evidence. Before a reparse replaces the paper's `document.json`, every run without a copy of its own gets the old one.
- A row's text is its cells joined by ` | `. A table's text is its rows joined by newlines, or its caption when it has no rows.
- `ANCHOR` in `sage/parse/document.py` is the pattern a well-formed anchor matches.
- A table's footnotes are text blocks under its grid. The agent copies them into the CSV's `# notes:` lines, which become `Table.notes`, and `attach_notes` in `sage/parse/grounding.py` records the text blocks in the table's region whose words are those notes in `Table.notes_anchors`. The transcription only finds the blocks. Evidence quotes the blocks' own text.
- pdftext writes U+0002 for two glyphs, and the parser tells them apart by the character's own box against the one before it. Set smaller than it and raised off the line, U+0002 is an exponent's minus ("Mg ha⁻¹" reaches pdftext as "Mg ha", a raised U+0002 and "1"), and the parser writes U+2212. Otherwise it is the soft hyphen at a line break ("pro\u0002 cessing"), and the parser writes U+00AD. A full-size U+0002 before a number stays a soft hyphen, so no sign is invented. The parser drops every other C0 control character.

Block kinds are `text`, `caption`, `table`, `furniture` and `references`. The paper view shows `text` and `caption` blocks. It shows `furniture` (running heads, page numbers, margins) only on page 1 or when the line holds a DOI. It hides `references`, and it hides the `table` blocks inside a grounded or partly grounded table, which it shows as a grid instead. Each table's head says how far the text layer backs it: "grounded", "partly grounded" or "not grounded", then how many of its non-empty cells match the PDF text exactly, approximately (at least `grounding_fuzzy_threshold` similar, 0.85 by default) and not at all, as in "17 of 18 cells match the PDF text exactly, 1 approximately, 0 not at all". A table is grounded when at least `table_min_grounded` of those cells match (0.9 by default), partly grounded from half, and not grounded below that.

## Fields and evidence

```python
class Evidence(Model):
    anchor: str  # "p6.t1.r1.c2" or "p3.b4"
    quote: str  # verbatim text at that anchor


class Field[T](Model):
    value: T | None
    basis: Literal["stated", "derived", "inferred"]
    evidence: list[Evidence] = []
    note: str | None = None
```

| basis | Meaning | The checker requires |
|---|---|---|
| `stated` | The quote says it. | At least one evidence item. Numbers, units and the words of a text value must be in the quotes. |
| `derived` | Computed from stated inputs. Code recomputes it. | At least one evidence item that cites the inputs. |
| `inferred` | A judgement the paper implies. | A note that says what it is inferred from. Evidence is optional. |

- A value the paper does not give is `value: null` with a note that says what was looked for. The checker requires the note. Settle marks the field unresolved with cause `absent`.
- An optional attribute that is left out (`None` instead of a `Field`) is not a value at all. It gets no status. Leave out an optional attribute the paper doesn't print, such as `units` for a unitless variable or `control_status` when no treatment is named the control or baseline. Code leaves out a null `Observation.statistical_encoding` itself when neither the value nor its quotes show a dispersion (a "±", "+/-" or a bracketed number).
- Status is never part of a `Field`. Settle attaches it in `records.json`.
- Every model in `sage/ir/schema.py` forbids unknown keys (`extra="forbid"`).

## Entities

All 12 entities live in `sage/ir/schema.py`. Each record has a `type` tag and an `id`. The `Record` union is discriminated on `type`.

References between records are plain id strings, never nested copies. `REFERENCE_FIELDS` lists each plain reference and the entity type it must point at. `FIELD_REFERENCES` lists the references wrapped in a `Field`, `Treatment.study_id` and `Management.treatment_ids`. The dataset check resolves both kinds. `Observation.variable_id` and `Observation.method_id` may be left out while a record is drafted, but the record level blocks on each until it is set.

In the table, a field ending in `?` is optional. Every other field is required. Types other than `str` are given in parentheses.

| Entity | What it is | References | Fields |
|---|---|---|---|
| Citation | This paper itself, never a work it cites | none | `author`, `year` (int), `title`, `persistent_identifier` |
| Study | One experiment with its own design, site and years | `citation_ids` (at least one) | `design?`, `period?` (DateRange, when it ran) |
| Site | A place where an experiment ran | none | `name`, `latitude?`, `longitude?`, `elevation?` (all QuantityValue), `country?`, `state_or_region?`, `nearest_city?`, `soil_context?`, `description?` |
| Species | An organism the measurements are about | none | `genus`, `species_epithet`, `scientific_name`, `common_name?` |
| Crop | A species as grown, with its cultivar | `citation_id`, `species_id` | `cultivar?`, `common_name?`, `cover_crop?` (bool), `notes?` |
| Method | How a variable was measured | `citation_id` | `name`, `description` |
| Variable | One measured quantity | none | `name`, `description?`, `units?`, `notes?` |
| Treatment | One combination of factor levels that values are reported for | `citation_id`, `site_id`, and `study_id` as a Field | `name`, `definition`, `control_status?` (bool), plus plain `factor_levels` (`{factor: level}`) |
| TreatmentPair | A comparison the paper draws between two treatments | `citation_id`, `treatment_id_1` (the baseline), `treatment_id_2` (the comparison) | `comparison_factor`, `comparison_label`, `use_for_validation?` (bool), plus plain `notes?` |
| Management | A field event such as planting, fertilization or harvest | `citation_id`, `crop_id?`, and `treatment_ids?` as a Field (left out when the event applies to every treatment) | `event_type` (one of `EVENT_TYPES` when it names one, see [Derived values](#derived-values)), `date` (DateRange), `amount?` (QuantityValue), `material?`, `notes?` |
| Observation | One reported value, of a Treatment. Values measured once for a site (weather, initial soil) have no Observation: their table or text is marked `schema_limitation`. | `citation_id`, `site_id`, `treatment_id`, `method_id?`, `species_id?`, `crop_id?`, `variable_id?` | `variable_name`, `value` (QuantityValue), `statistical_encoding?` (StatisticalSummary), `n?` (int, the replicates the value summarises), `reported_effect_scope` (`treatment_mean` or `aggregated_mean`), `aggregated_over_factors?` (list of factor names), `temporal_info` (DateRange), `depth?` (DepthRange), `replicate_id?`, `is_raw_replicate_level` (bool), `notes?` |
| Coverage | A curation rollup of how much data exists | `citation_id`, `site_id`, `variable_id?` | No `Field` attributes. Plain `system?`, `treatment_contrast?`, five row counts (`daily_rows?`, `annual_rows?`, `seasonal_rows?`, `static_observation_rows?`, `weather_rows?`), `initial_data_sources?`, `notes?` |

### Value types

| Type | Fields |
|---|---|
| QuantityValue | `reported_text`, `reported_numeric_value?` (float), `reported_units`, `unit_basis_notes?`, `converted_value?` (float), `converted_units?`, `conversion_formula?`, `conversion_rationale?` |
| DateRange | `reported_text`, `earliest?` (date), `latest?` (date), `relative_timing?`, `relative_timing_days?` (int) |
| DepthRange | `reported_text`, `top_cm?`, `bottom_cm?` (float) |
| StatisticalSummary | `statistic_name` as the paper names it (for example `SE`, `SD`, `LSD (0.05)`, `p`), `statistic_value` (float or str) |

`reported_text` and `reported_units` hold the text as printed. Code derives the rest. See [Derived values](#derived-values).

### Construction invariants

Pydantic validators enforce these when a record is built. Check reports a record that breaks one at its line, with the validator's message, and leaves it out of the draft.

1. **Observation effect scope.** `reported_effect_scope = treatment_mean` takes no factor list: a non-empty `aggregated_over_factors` is rejected, and code sets it to `[]` (see [Derived values](#derived-values)). `aggregated_mean` needs `aggregated_over_factors` with a non-empty list of factor names, or a null value with a note.
2. **Management date and amount are never inferred.** A `Management.date` or `Management.amount` with basis `inferred` is rejected. A date the paper does not give is a null value with a note.
3. **Species name.** When `genus`, `species_epithet` and `scientific_name` all have values, `scientific_name` starts with `"{genus} {species_epithet}"`. An authority may follow.
4. **TreatmentPair treatments are distinct.** `treatment_id_1` and `treatment_id_2` differ.
5. **QuantityValue conversions are complete.** If `converted_value` is set, `converted_units`, `conversion_formula` and `conversion_rationale` are set too.
6. **DateRange is an interval or a relative timing, never both.** An interval sets both `earliest` and `latest`, with `earliest` on or before `latest`. A single date uses the same date twice.
7. **DepthRange is a layer.** `top_cm` and `bottom_cm` are both set or both left out, with `top_cm` at or above `bottom_cm`. One depth uses the same number twice.

Two more shape rules apply. `Study.citation_ids` holds at least one id, and every Coverage row count is 0 or more. Check also requires each record id to match `^[A-Za-z0-9_-]{1,80}$`, so ids have no dots or spaces, and to be unique across the files.

### Derived values

Code recomputes derived values at every check, before the checker runs. Any number the model computed is overwritten. A derived value that code cannot recompute is dropped, so model arithmetic never stands. The record level then raises a blocking issue that names what code could not read and how to fix it. `recompute` in `sage/pipeline/checker/record.py` calls the domain functions below.

| Value | Recomputed by | Rule |
|---|---|---|
| `QuantityValue.reported_numeric_value` | `recompute_quantity` in `sage/domain/units.py` | Re-read from `reported_text` when the text holds one exact number with no `<` or `>` qualifier. A unit code doesn't know is read past when the text spells it as `reported_units` does ("139.9 mg CO2-C m-2 h-1"). Otherwise it is dropped, so a range ("250 to 300"), a bound ("<0.5") or a sentence keeps no number the model chose. |
| `QuantityValue.converted_value` and `conversion_formula` | `recompute_quantity` | Computed when the model set `converted_units` and the two units convert. `conversion_rationale` is filled in when it is missing. When they do not convert, both are dropped and `conversion_problem` blocks. |
| Every `DepthRange` | `recompute_depth` in `sage/domain/depth.py` | `top_cm` and `bottom_cm` from `reported_text`: one layer ("0-15 cm", "15 to 30 cm", "the top 10 cm") or one depth ("30 cm"), converted to centimetres. Text holding any other number ("0-15 and 15-30 cm"), or one depth that ends an open layer ("below 30 cm", "> 30 cm", "30 cm or more"), gives none, and `depth_problem` blocks. |
| `Site.latitude` and `Site.longitude` | `recompute_coordinate` in `sage/domain/coordinates.py` | Signed decimal degrees from `reported_text`. The conversion fields are set when the text is degrees, minutes and seconds, or when an S or W makes the value negative. Text that is not one coordinate, including a sign its hemisphere letter contradicts ("-42.5 N"), drops the number and the conversion fields, and `coordinate_problem` blocks. |
| Every `DateRange` | `recompute_date` in `sage/domain/dates.py` | The interval or relative timing parsed from `reported_text`. A sign before a count turns it around, so "−32 DAP" is 32 days before planting. Two days of one month joined by "and" ("May 18 and 19") are the interval from the first to the second. A day or month with no year takes the one year that the field's own quotes state. A span of years ("2005-06", "1992 to 1998") is the whole calendar years. A quote states a year in a date ("15 May 1999", "May 1999", "September of 2006", "2005-06", "Summer/Fall, 2005") or in words that make it one ("in 1999", "during 1999 and 2000", "the 1999 growing season"). A bare number such as "2000 seeds" is not a year, so code never guesses one. A season keeps only `reported_text`, because the months it covers depend on the hemisphere and the paper, and `date_warning` flags it; the export reads its months by the site's hemisphere (see [Export](#export)). Code reads no date at all from text that joins another number or month to the one it reads by a range or list word ("28-32 DAP", "May 18, 19 and 20"), or that makes it one end of an open span ("before 2000", "since 1990", "15 May 1999 or later"), rather than part of one. Text that does not parse keeps only `reported_text`. When that text holds a number or a month name, `date_problem` blocks. It says when the year is missing (and to cite the sentence elsewhere in the paper that states it, setting the value null only when none does), the quotes state several, or which part code could not read. Text with neither, such as "harvest", needs no date. |
| `Observation.aggregated_over_factors` of a treatment mean | `recompute` | `[]`, with the basis and evidence of `reported_effect_scope` (`derived` for a stated or derived scope, `inferred` for an inferred one). |
| `Observation.n` of a single replicate's value | `recompute` | `1` when `is_raw_replicate_level` is `true`, with its evidence: `derived`, or `inferred` when `is_raw_replicate_level` is inferred. |
| `Observation.is_raw_replicate_level` | `replicate_level` in `sage/domain/stats.py` | Code takes the first of these that holds. `false` when the value's text carries a "±" or bracketed dispersion or letters. `true` when a quote the record cites says "individual" or "single" plot, replicate, rep, sample or plant, or "raw data" or "raw values". "Per plot" and "each plot" don't count, since they name a unit basis or the layout. `false` when a cited quote says "mean", "average" or "averaged", or holds a significance test ("P <0.05", for example the quote of a `p` statistic). When the quotes say both ("means of individual plot values"), code decides nothing. Basis `derived`, citing that text. Otherwise the agent's value stands. |
| `Management.event_type` | `event_type` in `sage/domain/management.py` | The one type in `EVENT_TYPES` that the agent's value names and a quote of the field holds a word for: "transplanted", "sown" and "seeded" are `planting`, "side-dressed" and "urea" are `fertilization`, "compost applied" and "manure" are `compost_application`, "disked" or "incorporated" is `tillage`. The first five types are PEcAn's `events_schema` types. Basis `derived`, citing that quote. A value that names no type, or several, stays as written. |
| `Treatment.study_id` | `recompute_dataset` in `sage/pipeline/checker/dataset.py` | When the draft holds exactly one Study and that Study cites evidence, every Treatment points at it, `derived` from that evidence. A Treatment that leaves `study_id` out gets it this way. When the files write several Studies, check refuses a Treatment that leaves `study_id` out, at its line. |

Each replacement of a value that was set appends an `Overwrite` (`record_id`, `field`, `before`, `after`, `why`) to the draft. Filling in a value that was left out, or changing only a basis, is not logged. `records.json` carries the list.

## Evidence rules

The evidence level of the checker (`sage/pipeline/checker/evidence.py`) applies these rules to every `Field` on a record. Each broken rule is a blocking issue unless the rule says warning.

1. A null value needs a note.
2. A `stated` or `derived` value needs at least one evidence item.
3. An `inferred` value needs a note.
4. Every anchor resolves in the run's document. The message says whether the anchor is malformed or just not in this paper.
5. Every quote is non-empty and appears in its anchor's text. The match is the `contains` function in `sage/domain/normalize.py`. It applies NFKC, turns every dash and minus sign into `-`, straightens curly quotes, joins a word broken at a soft hyphen (U+00AD, or U+0002 in a document from an older parser, which it reads as a soft hyphen) with the line break after it, drops zero-width characters, joins line-break hyphenation, and then ignores all whitespace. A hyphen between two letters of the quote may also be missing from the text, because a compound split at a line break ("mid- September") reaches the text layer as a soft hyphen. Case still counts. The message quotes the closest stretch of the anchor's text, so the agent can copy the right words.
6. For a `stated` value, the number must be written in the quotes at the precision the text uses. This covers an `int` or `float` value, `QuantityValue.reported_numeric_value` and a numeric `StatisticalSummary.statistic_value`. A QuantityValue with no `reported_numeric_value` ("<0.5", "250 to 300") needs every number its `reported_text` writes outside the unit in the quotes. The sign is ignored. Only the quote counts, for a table cell too. An `int` up to twenty may be written as a word ("four replicates" states `n` = 4). Code reads a number as a table cell is read (`read_number` in `sage/domain/normalize.py`): thousands commas ("1,234.5"), a decimal comma ("12,5") or a decimal point. Digits joined by more points or commas ("15.05.1999") are one token that states no number, so a date in a quote never states its day; a QuantityValue whose text writes such a token needs that token, as printed, in the quotes.
7. For a `stated` QuantityValue, `reported_units` must appear in the quotes, as printed or after unit normalisation. Unitless values (`unitless`, `ratio`, `count`, an empty string and similar) are exempt.
8. For a `stated` StatisticalSummary, the statistic's name should appear in the quotes, without regard to case ("P <0.05" names `p`). This rule is a warning.
9. For a `stated` free-text value (a `Field[str]` such as a name, a cultivar or a place), one quote must contain the value. The match is `contains_loose`, which is `contains` without case. A Literal such as an effect scope and a `FIELD_REFERENCES` id are exempt. A value the agent reworded is `derived` or `inferred`.
10. For a `stated` DateRange or DepthRange, one quote must contain `reported_text`. The match is `contains_loose`.
11. On an Observation, a `stated` or `derived` `reported_effect_scope` needs a quote of that field that says it, by `scope_statement` in `sage/domain/stats.py` (see [How table groups set bases](#how-table-groups-set-bases) for the words). A `treatment_mean` with no such quote also blocks its `[]` factor list, which follows from it. A `stated` or `derived` non-empty `aggregated_over_factors` needs a quote of that field that pools the values and names every factor. A `stated` or `derived` `is_raw_replicate_level` needs `replicate_level` to find the same answer in the record's quotes.

The checker compares no other boolean value with its quotes.

## The paper folder

Every driver works the same way: the agent edits files in the paper's folder, and `sage extract check` and `sage extract done` judge them. The folder layout is in `sage/store/paths.py`.

| Path | Written by | What it is |
|---|---|---|
| `paper.pdf` | SAGE, when the paper is added | The PDF |
| `paper.md` | SAGE | The paper view, rewritten when the effective document changes |
| `pages/p<N>.png`, `tables/<anchor>.png`, `figures/<anchor>.png` | SAGE | Images at `page_render_dpi`: every page, each detected table upright, each figure it could locate |
| `tables/<anchor>.csv` | The agent | A table's grid as printed: a `# header_rows: N` line, optional `# notes:` lines, then CSV |
| `pages/p<N>.md` | The agent | Blocks `[p<N>.b<K>] …` transcribed from a page with no text layer, only what it cites |
| `records/<entity>.yaml` | The agent | One file per entity type (`citation`, `studies`, `sites`, `species`, `crops`, `methods`, `variables`, `treatments`, `pairs`, `managements`, `coverage`), each a map of `id: attributes` |
| `records/observations/*.yaml` | The agent | [Observation groups](#observation-groups) |
| `records/unresolved.yaml` | The agent | Unresolved marks, `target: {cause, reason}` |
| `.sage/` | SAGE | `document.json`, `paper.json`, `reviews.jsonl`, `runs/`, `snapshots/`, the table grounding cache |

Check never rewrites the agent's files. It reads the YAML with PyYAML's `BaseLoader`, so every scalar is a string the schema types and every node knows its line. `compile_records` in `sage/pipeline/compile.py` turns the files into a draft and keeps, for every record, attribute and table, the `file:line` it came from, so each checker issue points at the line to fix. With one Citation, `citation_id` and `citation_ids` may be left out everywhere.

### Field shapes

`Reader.field` in `sage/pipeline/shapes.py` reads each `Field` attribute from one of five shapes. The agent points at the paper; code reads the quote and decides the basis.

| YAML | Value | Evidence | Basis |
|---|---|---|---|
| `[V, a1, a2…]` | V | each anchor, with the quote located in its text | `stated` |
| `{at: a1}` or `{at: [a1, a2]}` | the text at the anchors | those anchors, whole text | `stated` |
| `[V, a1…, note: N]` | V | the anchors | `derived`, note N |
| `[V, note: N]` | V | none | `inferred`, note N |
| `[null, note: N]` | null | none | absent, note N |

- An anchor may pin its quote, `{p3.b4: "the second 120 kg"}`, when the words occur twice.
- Between the value and the note only anchors may stand: an item that isn't one is refused, with the hint that a value holding a comma must be quoted. The note comes last, so items after it are its own text split at a comma by YAML, and they are joined back with ", ".
- An unpinned quote is the shortest verbatim span of the anchor's text that holds V's text (`span` in `sage/domain/normalize.py`, with the `contains` rules below). When V isn't there, or V is a boolean, the quote is the anchor's whole text, and the evidence rules then say what is missing.
- V's text becomes the attribute's type: text and literals as they are, numbers parsed, booleans from true/false/yes/no. A QuantityValue's `reported_units` is what follows its leading number ("12.4 g m-2" gives "g m-2"), or the agent writes `{text, units}`. Dates and depths keep the text as `reported_text`. A statistic is `{statistic, value}`. A list is a YAML list.
- Plain attributes (ids and references, `factor_levels`, `citation_ids`, Coverage counts, `TreatmentPair.notes`) are plain YAML.

### Observation groups

`build` in `sage/pipeline/observations.py` turns each group into Observations. A group sets what its values share: `variable`, `site`, `method`, `crop`, `species`, `citation`, `treatment`, `time`, `depth`, `n`, `units`, `statistic`, `letters`, `notes`, `replicate`, and, for the whole group only, `scope` (`treatment_mean` or `aggregated_mean`), `over` (the factors), `scope_at` (anchors that say what the values are means of) and `raw`.

- **A table group** starts `table: <anchor>`. `rows` and `columns` map an index to what the whole row or column shares, and `cells` adds or overrides single cells by anchor. The value cells are rows × columns, plus `cells`, minus `skip`, which takes cells or whole rows (`p7.t1.r4`). Keys merge group, then row, then column, then cell. Each value cell becomes the Observation `obs-<anchor with dashes>`. An empty cell is skipped, a cell with no number is an issue, and a header cell is refused. `irrelevant: reason` decides a table that holds no experiment values. A table appears in at most one group.
- **A text group** starts `text: true` and lists `values: {<id>: {value: [V, anchor], …}}`, each with the per-value keys.

How a table group sets each value's basis is in [How table groups set bases](#how-table-groups-set-bases).

## The checker

The checker lives in `sage/pipeline/checker/`. Every message names the target, says what was found, and says what to do next. Before it runs, compile validates each record with `RecordAdapter` in `sage/ir/schema.py`. A record that does not parse or breaks a [construction invariant](#construction-invariants) is left out of the draft, and check reports the validator's message at its line.

| Level | Module | Blocking | Warning |
|---|---|---|---|
| Evidence | `evidence.py` | The [evidence rules](#evidence-rules) | The statistic name is not in the quotes |
| Record | `record.py` | An Observation with no value, `variable_id` or `method_id`, or with a statistic and no `n`. A Site with no name. A Management with no `event_type`. A record whose fields are all null. A derived value code could not recompute (see [Derived values](#derived-values)). | A Citation missing its author, year or title. A date that names a season, so code sets no dates (`date_warning`, see [Derived values](#derived-values)). |
| Dataset | `dataset.py` | A reference to a missing record, or to a record of the wrong type. An Observation whose site is not its Treatment's. Two Observations with the same treatment, variable, reported value, time and site. Two Observations with different values and nothing else to tell them apart: the same treatment, variable and variable name, site, time, depth, method, crop, species, replicate, replicate level, scope and unit. Each level a table reports needs a Treatment of its own, so this is most often a level written onto another level's Treatment. An `aggregated_mean` over a factor its Treatment's `factor_levels` fixes. | Two Sites, Treatments, Variables, Methods or Species with the same name. A `treatment_mean` for a Treatment that leaves out a factor other Treatments of its Study fix, unless the value's own text names a level of that factor (a column header "Arlington"): it is most likely averaged over it. Two Observations with the same treatment, site, time and unit whose values agree at the coarser printed precision, under different Variables or printed differently ("11.5" and "12"); the warning lands on the one not from a table. |
| Coverage | `coverage.py` | A table neither mapped nor marked irrelevant. A Variable with no Observation and no unresolved mark. A Study with no Treatment and no unresolved mark. A body row of a mapped table that holds a number but gives no Observation and isn't in `skip`. | Numbers in a Results sentence that no record quotes from that block, or from a table a record cites, listed sentence by sentence. Single digits, years, test statistics ("P <0.05"), figure and table numbers, sample sizes, times ("28 DAP", "DAP −110, −32"), citations ("[24,29]"), dates ("15.05.1999") and unit exponents are skipped, and so are blocks of fewer than four words (axis titles and tick labels). |

### When each level runs

- `check` (`sage extract check`, `sage/pipeline/extraction.py`) compiles the folder, runs every level through `gate.evaluate` and lists the open issues: blocking first, then warnings, each as `file:line  target: message`, then the totals. With a run open it records the check: a snapshot when the files changed, the same-error count, the run's `draft.json` and document, and a `check.ran` event. Without an open run it only answers.
- `done` runs the same check and refuses while any open blocking issue remains.
- Settle runs every level once more, with no cap on the list.

Check lists at most `error_list_cap` findings of each severity (default 12), then a count of the rest; `--all` lists every one. A reference to a record that failed to compile is left out, since the record's own lines report why. While a run is open, check prints the run's guidance and the scientist's standing reviews first: a rejected record blocks until it is changed or deleted, and an edit is a warning that carries the scientist's value, until the record changes.

### How table groups set bases

A table group builds each Observation from the cell and the text that explains it. That text is the value column's header cells, the caption, the table's footnote blocks (`Table.notes_anchors`) and the group's `scope_at`.

Each Observation is built like this:

- The value is `stated` from the cell and the text that gives its unit: the group's `units`, a "(…)" in the value column's header or the row's label, or the Variable's `units`. The variable name is the label or header that names it, else the Variable's name. The treatment, site, method and the other references are the ids the group, row, column or cell gives; nothing is matched by guessing.
- A time or depth written as a field shape is used as written. Bare text is `stated` from the first header, caption, footnote or `scope_at` text that holds it, and `inferred` otherwise.
- The ± or bracketed statistic is `stated` when one of those quotes names it, such as "± SE". When the quote names it in other words, such as "standard error", it is `derived`. A name only the group gives in `statistic` is `inferred`. With no name the statistic is null, with a note.
- `reported_effect_scope` and `aggregated_over_factors` come from the group's `scope` and `over`. They are `derived` when code finds the scope in any of those texts, citing the one that says it. When none says it they are `inferred` from `scope_at`, and null with a note when there is no `scope_at` either. The note says what code looked for and what the cited text says. `scope_statement` in `sage/domain/stats.py` decides. Means over factors need a text that pools the values and names every factor. Pooling is "mean(s)", "average(s)", "averaged", "pooled" or "combined" followed by "over", "across", "for all", "of all", "for both", "of the two" (or another count), "regardless of" or "irrespective of", as in "the mean ± SE for all cultivar mixture treatments", or "pooled" alone. Averaging over replicates, blocks, plots, samples or plants pools nothing, "means for all treatments" lists every treatment's mean, and a "pooled SE" names a statistic. Treatment means need a text that says "mean" or "average" while none of the texts pools them. For `treatment_mean`, `aggregated_over_factors` is `[]`, as the [derived values](#derived-values) say.
- `is_raw_replicate_level` is false and `derived` from the cell when the cell carries a dispersion or letters. Otherwise it is `derived` when a `scope_at` quote says the values are means, or individual plot or replicate values, as `raw` claims; with `raw` left out, only a quote that says means counts. It is `inferred` from `raw` when no quote says so. With `raw` left out and no quote that says means, or with neither `raw` nor `scope_at`, it is null with a note, so settle marks it `absent`.
- Letter groups go in `notes`, `derived` from the cell and the sentence that says what they mean, or `inferred` from the group's `letters`. With neither, the note holds only the letter group, `derived` from the cell.
- `n` is the group's, row's or cell's `n` field shape, which the evidence level checks like any other.

### The same-error rule

Check counts each open blocking error by its target and message (`gate.evaluate`, `strikes.json`), but only when the agent edited the file the error points at since the last check: an attempt at a fix. When the same error survives 3 attempts (`gate.SAME_ERROR_LIMIT`), check marks the target unresolved with cause `repeated_error` (`marks.json`), lists it as marked, and stops reporting it. The mark stands only while the target still has a blocking error, so a target fixed later counts again.

### The done gate

`done` reads the agent's marks from `records/unresolved.yaml`, as every check does. A mark's cause is one the agent may set (see [Causes](#causes)). Its target must be a record id, an attribute of a known record, a table anchor, or the target of an open issue. Check refuses a mark on an attribute that holds a value, as a blocking finding at its line. The agent fixes the value's evidence, or sets it null with a note. It also refuses a mark that could not stand, as `Draft.mark_problem` says:

- `figure_only` targets a Variable that no Observation measures, or one of its fields, never another record, a table or `results`. A Variable with Observations is not only in a figure.
- On a whole record, `ambiguous`, `conflict`, `schema_limitation` and `blocked_prerequisite` stand whatever its values (`RECORD_CAUSES`), and `absent` stands only on a record that holds no value. A record whose values pass is not absent; the null field is what gets marked.

`done` refuses while any open blocking issue remains, and otherwise settles the run with the agent's summary (`sage extract done <paper> -m "…"`). A paper with no primary data is done with the summary "no extractable experiments".

## Settle

Settle (`sage/pipeline/settle.py`) runs at the end of every run, whatever the outcome: `done` settles a finished run, and a run that ends any other way settles what its folder compiles to (`extraction.salvage`). A folder that can't be compiled settles empty. For an open run, `core.records` compiles the folder and settles it on the fly, so the UI shows the agent's work as it saves it.

### Statuses

| Status | Meaning |
|---|---|
| `verified` | Every check passed on stated or derived evidence, and no mark stands in for a value. |
| `flagged` | It passed, but a person should look at it. |
| `unresolved` | It is not settled. It carries a cause and a reason. |

### Field status

Settle takes the first rule that applies.

Settle reads every issue, whatever marks the draft holds.

1. A mark stands on the field. The status is `unresolved`, with the mark's cause and reason.
2. The field has a blocking issue. The status is `unresolved`, and the reason is the last such issue's message. The cause is `budget_exhausted` when the outcome is `budget_exhausted`. Otherwise it is `grounding_failure` for an evidence-level issue and `validation_failure` for any other level.
3. The value is null. The status is `unresolved` with cause `absent`, and the note is the reason.
4. The field is `flagged` when any of these hold. A warning targets the field. The basis is `inferred`. An evidence cell matched the text layer only fuzzily. An evidence cell was read from the table image and the text layer does not hold it. An evidence anchor is on a page with no text layer.
5. Otherwise the field is `verified`.

A plain attribute such as `variable_id` gets a status entry only when a mark stands on it or it has a blocking issue.

### Record status

- A record is `unresolved` when a mark stands on it, or when a blocking issue targets the record itself, such as a record whose fields are all null.
- Otherwise it takes the worst status of its fields, in the order `verified`, `flagged`, `unresolved`. Fields whose cause is `absent` do not count, so a value the paper never gives does not sink its record.
- A record that would be `verified` but has a warning on the record itself is `flagged`.

`records.json` keeps the unresolved marks whose target is not a record (a table, or `results`) in `Settled.unresolved`.

### Causes

`Cause` in `sage/ir/draft.py` lists every cause. It is `AgentCause`, the ones `records/unresolved.yaml` takes, plus `CodeCause`, the ones only code sets.

| Cause | Meaning | Set by |
|---|---|---|
| `absent` | The paper does not give it. | Settle, for a null value. The agent may also use it. |
| `ambiguous` | The paper does not tie it to one treatment, site or time. | The agent |
| `conflict` | The paper contradicts itself. The reason gives both values. | The agent |
| `figure_only` | It is shown only in a figure. | The agent, on a Variable with no Observation, or one of its fields |
| `schema_limitation` | The IR cannot hold it, for example a value pooled over all treatments. | The agent |
| `blocked_prerequisite` | A record it depends on cannot be written. | The agent |
| `grounding_failure` | Its last open error is an evidence-level error. | Settle |
| `validation_failure` | Its last open error is a record, dataset or coverage error. | Settle |
| `repeated_error` | The same error survived 3 edits of its file. | Check |
| `budget_exhausted` | The run stopped on its turn, cost or time budget before it was fixed. | Settle |

### Outcomes and run status

A run ends with one outcome.

| Outcome | When |
|---|---|
| `finished` | `done` was accepted. |
| `budget_exhausted` | SAGE's own agent ran out of its turn budget (`max_turns`) or its cost budget (`max_cost_usd`), or an agent SAGE started ran past its time limit (`AgentConfig.time_limit_minutes`). `RunInfo.limit` and `run.finished` say which: `turns`, `cost` or `time`. A resumed run shares the budget, so earlier attempts' turns and spend count against it. A terminal run has no budget. |
| `failed` | The provider, the agent or the run raised an error, or an agent ended its turns without `done`. The message is in `run.json` and in a `run.error` event. Settle still writes `records.json`. |

`run.json` holds the run status, `RunStatus` in `sage/store/models.py`.

- **SAGE's own agent and an agent SAGE starts** queue a run that starts `queued`, becomes `running`, and ends `finished`, `budget_exhausted`, `failed` or `interrupted`. One runner process executes every run, up to `parallel_runs` at a time ([design/01-stack-and-architecture.md](design/01-stack-and-architecture.md)); `sage/pipeline/run.py` drives SAGE's own agent and `sage/pipeline/agent_run.py` an ACP agent. A run whose provider lacks a key, or whose agent isn't installed or signed in, ends `failed`, with `refused` set, before it starts. Stop ends a run `interrupted` and settles what the folder holds. Only a `running` run whose pid is dead reads as `interrupted`.
- **A terminal run** is `running` (open) from `sage extract start` until `done` or `stop`. `done` with no open run opens one first.
- An agent SAGE starts runs whatever it asks to, unless the `agents_ask` setting is on. Then anything beyond what SAGE allows on its own (reading files it names in the folder and the skill, editing the agent's own files, `sage extract check` and `done`, and read-only shell commands that use no option that writes, deletes or runs something, such as `find -delete`, `sort -o` or a `sed` script that does more than print lines by number) makes the run show `waiting` until the scientist answers in the UI or with `sage extract answer`, and edits to SAGE's own files are refused.
- SAGE never starts a run on a paper while another is open on it. `sage extract start` refuses a paper SAGE's own agent is on, and joins any other open run.

### Guidance and continued runs

- **Guidance** is the scientist's text on what a run should add or fix (`RunInfo.guidance`). It goes in the agent's first message, after the cached prefix, `run.started` records it, and check prints it first while the run is open. The scientist's reviews reach the agent through check (see [When each level runs](#when-each-level-runs)).
- **Resume.** `sage extract run --resume <run_id> --guidance "…"` carries on a run SAGE's own agent drove, in a new conversation over the same folder, with what its budget has left. The guidance is added to what the run was already given.
- **Continue.** `sage extract run --continue <run_id> --guidance "…"` starts a new run on the folder as the earlier one left it, with a budget of its own and the earlier run's driver unless `--with` says otherwise. `parent_id` names the run it continues. The earlier run is left as it was, so the two can be compared.

## Export

`core.export` writes a run as the PEcAn curation workbook (`xlsx`), the same tabs as one CSV each in a zip (`csv`), or `records.json` as it is (`json`). The workbook is built by `workbook` in `sage/pipeline/export.py`, and [pecan/workbook.md](pecan/workbook.md) says where every column comes from.

- Tabs: README, citations, sites, treatments, methods, observations, managements, variables, crops, treatment_pairs, and coverage when the run has Coverage records. Each tab has the curators' columns first, in their order, then the columns SAGE adds, ending with `sage_id`, `status` and `review`. On observations SAGE adds the effect scope, the relative timing, the printed text, the source table or page and the quote before those three, and `status_reason` after them.
- Keys are record ids, except the citations tab's `citation_id`, which is the citation key. A row's citation is the paper's DOI, else `citation_key` (`authorYYYYabc`), which is also the `dataset_id`.
- Values stay in the units the paper prints, and `statname` is the protocol's name for the statistic (`statname` in `sage/domain/stats.py`: SE, SD, MSE, CI, LSD, HSD, MSD or P); a statistic it doesn't name keeps its printed name and the README lists it.
- `min_date` and `max_date` are a DateRange's interval. A season is read by `season_window` in `sage/domain/dates.py` as the meteorological months in the site's hemisphere, with a note; a season that crosses the new year, a relative timing and a missing date stay blank.
- `observation_level` is `replicate` for a raw replicate value, else the effect scope. `fill_status` is `PAPER_TABLE` or `PAPER_TEXT`, from the value's first evidence anchor.
- A Management with no `treatment_ids` is one row with a blank treatment and the note "applies to every treatment"; one with several is a row per treatment.
- Every record is exported with its status, except those a scientist rejected; the README counts those. Edits are shown in the `review` column, not applied, since a review's value is the scientist's text.

## Review

**Reviews.** A review (`Review` in `sage/store/models.py`) names a run, a record, an action (`accept`, `edit` or `reject`), and optionally a field, a corrected value and a note. Reviews are appended to the paper's `reviews.jsonl`. The latest review per record counts.

**Standing reviews.** The reviews that count for a run are its own, plus each run it continues' reviews of records it holds unchanged (`standing` in `sage/pipeline/review.py`). A record the agent rewrote comes back unreviewed. The queue, the counts and the export all read the standing reviews.

**Review queue.** `queue` in `sage/pipeline/review.py` lists every record of the run: unresolved ones, then flagged, then verified, each by type. Reviewed records stay in it with their latest review. `core.accept_looks_good` accepts, in one call, every verified record that has no review yet.
