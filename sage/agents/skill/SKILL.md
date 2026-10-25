---
name: sage
description: Extract experiment records from an agronomy or ecology field-study paper in a SAGE library, with evidence for every value. Use when asked to extract a paper with SAGE, to continue or fix a SAGE extraction, or when working in a paper folder that holds paper.md and records/.
---

# SAGE extraction

You turn one field-study paper into a dataset for crop-model calibration: sites, treatments, what was measured and how, and every reported value. You write YAML files in the paper's folder, and SAGE checks them. Every value points at where the paper says it. Code reads the quote, the number and the basis from where you point, and decides each record's status. You never mark anything verified.

Capture what the paper reports, as it reports it. Never fill a value the paper doesn't give, never convert units, and never turn one statistic into another.

## Timeline

1. **Open the run.** `sage extract start <paper>` prints the folder. Work there. If SAGE started you, the run is already open. `sage papers` lists the paper ids.
2. **Read `paper.md` once, whole.** Work out the experiments, sites, crops, factors and levels, what was measured, and where the results are. Look at `pages/pN.png` for anything the text misses.
3. **Transcribe what the text layer lacks.**
   - Each table marked `not transcribed`: read `tables/<anchor>.png` and write `tables/<anchor>.csv`.
   - Each page marked `no text layer`: transcribe the passages you will cite into `pages/pN.md`.
   - Then run check. SAGE grounds them against the PDF and rewrites `paper.md` with the cell anchors.
4. **Write the context records** in `records/`: citation, studies, sites, species, crops, methods, variables, treatments and managements.
5. **Map every table** in `records/observations/<table>.yaml`. A table with no experiment values gets `irrelevant: why`. A table of site-level data, such as weather, is marked `schema_limitation` instead (`references/entities.md`, Site-level data).
6. **Write the values that live only in the text** in `records/observations/text.yaml`.
7. **Check until nothing blocks.** Run `sage extract check <paper>` after each batch of edits and do what each line says. It lists the first few blocking errors and warnings; `--all` lists every one. If something can't be fixed, mark it in `records/unresolved.yaml`.
8. **Finish** with `sage extract done <paper> -m "one-paragraph summary"`. A paper with no primary data (a review or a model study) finishes with "no extractable experiments" and the reason.

Continuing a run is the same loop. Check prints the scientist's guidance and reviews first. Do what they ask without redoing the rest, and say in the summary what you changed.

## The paper folder

| Path | Who writes it |
|---|---|
| `paper.md`, `pages/pN.png`, `tables/<anchor>.png`, `figures/` | SAGE. Read them. |
| `tables/<anchor>.csv`, `pages/pN.md` | You, only for what the text layer lacks |
| `records/*.yaml`, `records/observations/*.yaml`, `records/unresolved.yaml` | You |
| `.sage/`, `paper.pdf` | SAGE. Never edit them. |

Anchors come from `paper.md`. `[p3.b4]` is text block 4 on page 3. `[p7.t1]` is a table, and `p7.t1.r2.c1` is its cell in row 2, column 1, both counted from 0.

## Field shapes

Every scientific field is written in one of five shapes. Code works out the basis from the shape.

```yaml
name: [Arlington Research Station, p2.b3]            # stated: the value is in the text at its anchors
time: {at: p7.t1.r0.c2}                              # stated: the value IS the text at the anchor
period: [2003-2011, p2.b5, note: from both seasons]  # derived: you composed it from what you cite
control_status: [true, note: the only plot left unfertilized]  # inferred: your judgement, and why
elevation: [null, note: no elevation in methods or site description]  # absent: what you looked for
```

- **Point, don't transcribe.** Write the value as printed and cite where it is. Code finds the quote in the anchor's text. When the words occur twice in a block, pin the quote: `[120 kg N ha-1, {p3.b4: "the second 120 kg N ha-1"}]`.
- **Quote YAML specials.** Put a value or note that holds a comma, `: `, ` #` or starts with `[ { & * ! | > % @` in quotes, or YAML splits it: `["35, 69 and 95 DAP", p5.b2]`, `[USA, note: "Stephenville, TX"]`.
- **Value forms.**
  - Quantity: `"12.4 g m-2"`. Code reads the units after the number. When it splits wrongly, write `{text: "12.4", units: g m-2}`.
  - Date: as printed, one per field: `"May 18 and 19"`, `"45 days after sowing"`, `"-32 DAP"`. When the text has no year, also cite a sentence that states one.
  - Depth: one layer with its unit, `"0-15 cm"`. Coordinate: as printed, `"38°32′N"`.
  - Statistic: `{statistic: SE, value: "0.2"}`. List: `[[cultivar, year], p5.b9]`.
- Plain attributes (ids, `site_id`, `species_id`, `factor_levels`, `citation_ids`) are plain YAML, with no shape.
- An optional field the paper never prints is left out. A required one the paper doesn't give is `[null, note: …]`.

## Context records

One file per entity, each a map of `id: attributes`. Ids are letters, digits, `-` and `_`, unique across all files, and a curator must recognise them: the paper's own label (`N120`, `arlington`), and for variables the quantity with its units (`grain_yield_Mg_ha`). With one citation, `citation_id` can be left out everywhere.

```yaml
# records/treatments.yaml
N0:
  site_id: arlington
  name: [0 kg N ha-1, p3.b2]
  definition: [no nitrogen fertilizer, p3.b2]
  control_status: [true, p3.b2]
  factor_levels: {N rate: "0"}
N120:
  site_id: arlington
  name: [120 kg N ha-1, p3.b2]
  definition: [120 kg N ha-1 as urea at planting, p3.b2, p3.b5, note: rate and source from two sentences]
  control_status: [false, p3.b2]
  factor_levels: {N rate: "120"}
```

Read `references/entities.md` before writing these: every entity's attributes and the rules for each.

## Tables: a worked example

The paper prints "Table 2. Grain yield (Mg ha−1) under two N rates. Values are means ± SE over two cultivars; means with different letters differ (P<0.05)." The methods say "four replicate blocks" at `[p2.b6]`. `paper.md` shows the table as not transcribed, so you read `tables/p6.t2.png` and write:

```csv
# header_rows: 2
# notes: Values are means ± SE over two cultivars; means with different letters differ (P<0.05).
N rate (kg N ha−1),Grain yield (Mg ha−1),
,2019,2020
0,4.1 ± 0.2 b,3.8 ± 0.3 b
120,5.6 ± 0.3 a,5.1 ± 0.2 a
```

The first line gives the header row count. `# notes:` lines carry the footnotes. Cells are copied as printed. After the next check, `paper.md` shows the grid as `r0` to `r3`, `c0` to `c2`. Then map the values:

```yaml
# records/observations/p6.t2.yaml
- table: p6.t2
  variable: grain_yield_Mg_ha       # what every value shares: variable, site, method, crop, species,
  site: arlington                   #   treatment, time, depth, n, units, statistic, letters, notes
  method: combine_harvest
  crop: wheat
  scope: aggregated_mean            # treatment_mean, or aggregated_mean with over
  over: [cultivar]
  n: [4, p2.b6]
  rows:                             # row index: what the whole row shares
    2: {treatment: N0}
    3: {treatment: N120}
  columns:                          # column index: what the whole column shares
    1: {time: "2019"}
    2: {time: "2020"}
```

The value cells are rows × columns: four Observations, `obs-p6-t2-r2-c1` and so on. From each cell code reads the number, the ± and its name ("± SE" in the notes), the letters, and the unit from the header. Keys merge group, then row, then column, then cell, with the later one winning.
- `cells: {p6.t2.r4.c1: {treatment: N60}}` adds a cell or overrides one.
- `skip: [p6.t2.r3.c2]` leaves a cell out.
- `scope_at: [p5.b9]` cites a sentence that says what the values are means of, when the table's own text doesn't.
- An empty cell is skipped.

Had you left out `n`, check would answer:

```
records/observations/p6.t2.yaml:11  obs-p6-t2-r2-c1.n: a value with a statistic needs n, the replicates it summarises: write n: [4, anchor] citing where the methods or the table give it ('four replicates', 'n = 4'), or n: [null, note: what was looked for]
…
4 blocking · 0 warnings · 12 records · snapshot 3
```

## Values from the text

```yaml
# records/observations/text.yaml
- text: true
  variable: n2o_flux_g_m2
  site: arlington
  method: static_chamber
  scope: treatment_mean
  scope_at: [p8.b2]
  values:
    n2o-peak-N120:
      value: [12.4 g m-2, p8.b2]
      treatment: N120
      time: [July 2019, p8.b2]
```

Record a value the text states about a figure ("peaked at 12.4 g m−2 in July"). When the text restates a table value, keep the table's. A quantity only a figure shows gets its Variable, marked `figure_only`.

## Unresolved

```yaml
# records/unresolved.yaml
soil_N_flux: {cause: figure_only, reason: only Fig. 3 shows it}
results: {cause: ambiguous, reason: the 44% and 39% gains are not tied to one treatment}
```

A target is a record id, a `record_id.field` that is null, a table anchor, or the target of an open issue such as `results`. A field that holds a value can't be marked: fix it, or make it `[null, note: …]`. The causes are in `references/causes.md`.

## Check

Each line is `file:line  target: what is wrong, and what to do`. Blocking lines come first, then warnings, then `N blocking · M warnings · R records`. The exit code is 0 when nothing blocks, 1 when something does, and 2 when a file isn't valid YAML or CSV.
- Fix every blocking line, and the warnings that are real.
- The `results` warning lists Results sentences whose numbers no record cites. Record the experiment values, and mark the rest in `unresolved.yaml`.
- When the same error keeps surviving your edits of its file, SAGE marks its target `repeated_error` and stops reporting it. Move on; the mark lifts once the error is gone.
- When a table and the text disagree, use the table's value and put the conflict in a note.

`done` runs the check once more and is refused while anything blocks. For how runs, snapshots, statuses and the live UI work, read `references/internals.md`.
