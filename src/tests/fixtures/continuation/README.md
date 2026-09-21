Real Marker-derived fixtures for the table-continuation rule
(`content_reader.table_continuation_map`). Block text and anchors are copied
unmodified from `src/paper/<paper>/content.md`; provenance keeps only
`block_type`, `page_id` and `section_path` (plus the raw TableCell entries of
Daren's Table 2, needed for the numeric-token sanity check). They are trimmed
to CONTIGUOUS anchor ranges, because the rule depends on exactly which
rendered blocks sit between two tables -- dropping blocks between a pair would
manufacture a false "adjacent" pair.

- Daren-1997-Canopy: the only paper on disk with genuine page-split tables
  (b:0119->b:0178, b:0350->b:0367, b:0607->b:0656).
- Berntson-1997-Regenerating: b:0053 -> b:0059 are on consecutive pages with the
  same width but are DIFFERENT tables (Table 2's caption is a separate block).
- Nutrient-cycling: Table 3 / Table 4 headers are `#### Table N` SectionHeaders.
- Kathryn-2020-Winter: b:0044 -> b:0101 carry no label between them but are
  separated by 17 substantive blocks.
