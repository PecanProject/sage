Real artifacts for tests/test_grounding_gate.py (item 13: grounding).

- `felipe_real_failed_excerpts.json`: real `raw_text_excerpt` values Extraction produced for Felipe-2010-Cultivar
  that the pre-item-13 gate rejected (stored run attempts), grouped by what the new rule does with them:
  `ellipsis_now_passes` (literal stretches around an ellipsis, in order), `ellipsis_still_fails` (an ellipsis but not
  every stretch is literal / in order), `other_still_fails` (no ellipsis: annotation, wrong anchor, ...).
- `Felipe-2010-Cultivar/`: the real blocks (unmodified text and anchors) those excerpts cite.
