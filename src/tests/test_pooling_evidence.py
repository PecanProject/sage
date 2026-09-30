"""Deterministic pooling-evidence detection (pipeline/pooling_evidence.py).

Real evidence: Felipe-2010-Cultivar Table 1 reports means per cover-crop
treatment, and the note directly after it says "Data show the mean +/- standard
error for all cultivar mixture treatments, since no differences were observed
amongst them." -- pooling over the cultivar mixtures. The live gpt-oss-120b
read that block (read_nearby after=2) and still returned pooled_factors=[], so
the statement is recognised in code from grounded source text instead.

Fixtures: tests/fixtures/pooling/<paper>/ -- real Marker output for Felipe,
Daren, Kathryn, Berntson and Paul (rebuilt by fixtures/pooling/build_fixtures.py).
Tests marked SYNTHETIC use invented text for frames the real corpus has no
example of; they are NOT evidence about real papers.

No test here expects a count: every number follows from the source text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import content_reader as cr
from pipeline import orchestrator, pooling_evidence as pe
from pipeline.raw_schema import PooledFactor, TableClassification, TableFactor, TableRowGroup, TableValueColumn
from pipeline.validators import _value_supported_by_text

FIXTURES = Path(__file__).parent / "fixtures" / "pooling"
DAREN, FELIPE, KATHRYN, BERNTSON, PAUL = (
    "Daren-1997-Canopy", "Felipe-2010-Cultivar", "Kathryn-2020-Winter", "Berntson-1997-Regenerating", "Paul-1998-Foliar",
)
FELIPE_NOTE_SENTENCE = ("Data show the mean $\\pm$ standard error for all cultivar mixture treatments, "
                        "since no differences were observed amongst them")


def _tables(paper: str) -> list[list[str]]:
    """Every logical table of a fixture paper, as its (continuation) chain."""
    provenance = cr._load_provenance(paper, FIXTURES)
    continuation = cr.table_continuation_map(paper, FIXTURES)
    follower = {prev: cur for cur, prev in continuation.items()}
    chains = []
    for anchor in sorted((a for a, e in provenance.items() if e.get("block_type") == "Table"), key=cr._anchor_sort_key):
        if anchor in continuation:
            continue
        chain = [anchor]
        while chain[-1] in follower:
            chain.append(follower[chain[-1]])
        chains.append(chain)
    return chains


def _accepted(paper: str, chain: list[str]) -> list[dict]:
    return [r for r in pe.detect_pooling_evidence(paper, chain, FIXTURES) if r["status"] == "accepted"]


# --------------------------------------------------------------------- #
# real positives
# --------------------------------------------------------------------- #


def test_felipe_note_after_table_1_is_detected_as_pooling_over_the_factor_it_names():
    records = pe.detect_pooling_evidence(FELIPE, ["b:0069"], FIXTURES)
    assert [(r["status"], r["anchor"], r["position"], r["factor"], r["dimension"], r["pattern"]) for r in records] == [
        ("accepted", "b:0119", "after+1", "cultivar mixture", "treatment", "value_for_all"),
    ]
    excerpt = records[0]["excerpt"]
    assert excerpt == FELIPE_NOTE_SENTENCE
    block = cr._rendered_block_texts(FELIPE, FIXTURES)["b:0119"]
    assert excerpt in " ".join(block.split())  # a literal slice of the source
    assert _value_supported_by_text(excerpt, block)  # and it passes the pipeline's own grounding check


def test_daren_table_7_caption_is_detected_as_pooled_over_locations_and_maturities():
    records = _accepted(DAREN, ["b:0761"])
    assert [(r["anchor"], r["position"], r["factor"], r["dimension"], r["pattern"]) for r in records] == [
        ("b:0760", "own_caption", "locations", "site", "aggregation_verb_across"),
        ("b:0760", "own_caption", "maturities", "time", "aggregation_verb_across"),
    ]
    assert records[0]["excerpt"].startswith("Table 7. Leaf area index") and records[0]["excerpt"].endswith("averaged across locations and maturities.")
    assert _value_supported_by_text(records[0]["excerpt"], cr._rendered_block_texts(DAREN, FIXTURES)["b:0760"])


def test_across_every_real_fixture_table_only_those_two_statements_are_accepted():
    found = {}
    for paper in (DAREN, FELIPE, KATHRYN, BERNTSON, PAUL):
        for chain in _tables(paper):
            for record in _accepted(paper, chain):
                found.setdefault((paper, chain[0]), []).append(record["factor"])
    assert found == {(DAREN, "b:0761"): ["locations", "maturities"], (FELIPE, "b:0069"): ["cultivar mixture"]}


# --------------------------------------------------------------------- #
# real negatives: windows
# --------------------------------------------------------------------- #


def test_table_6_never_reads_table_7s_caption():
    chain = ["b:0607", "b:0656"]
    assert cr.table_continuation_map(DAREN, FIXTURES).get("b:0656") == "b:0607"
    windows = pe.pooling_windows(DAREN, chain, FIXTURES)
    assert ("own_caption", "b:0606") in windows  # its own caption sits before the FIRST block of the chain
    assert "b:0760" not in [a for _, a in windows]  # the next table's caption is a boundary, not a note
    assert _accepted(DAREN, chain) == []


def test_table_4_never_reads_figure_1s_caption():
    chain = ["b:0350", "b:0367"]
    windows = pe.pooling_windows(DAREN, chain, FIXTURES)
    assert "b:0452" not in [a for _, a in windows]  # Figure 1 (the caption says "averaged across three sward maturities")
    assert cr._load_provenance(DAREN, FIXTURES)["b:0452"]["block_type"] == "Figure"
    assert "averaged across three sward maturities" in cr._rendered_block_texts(DAREN, FIXTURES)["b:0452"]
    assert _accepted(DAREN, chain) == []


def test_daren_body_text_averaged_across_locations_is_outside_every_table_window():
    body = ["b:0462", "b:0838", "b:0850"]
    texts = cr._rendered_block_texts(DAREN, FIXTURES)
    assert "Averaged across locations" in texts["b:0462"] and "Averaged across" in texts["b:0838"]
    for chain in _tables(DAREN):
        assert not set(body) & {a for _, a in pe.pooling_windows(DAREN, chain, FIXTURES)}


def test_paul_pooled_across_species_is_outside_every_table_window():
    texts = cr._rendered_block_texts(PAUL, FIXTURES)
    assert "pooled across species" in texts["b:0470"]
    outside = {"b:0006", "b:0422", "b:0470"}
    for chain in _tables(PAUL):
        assert not outside & {a for _, a in pe.pooling_windows(PAUL, chain, FIXTURES)}
        assert _accepted(PAUL, chain) == []


def test_kathryns_means_separation_text_is_not_a_pooling_statement():
    texts = cr._rendered_block_texts(KATHRYN, FIXTURES)
    assert "Means separations for all ANOVAs were performed by Tukey-Kramer test" in texts["b:0131"]
    # not eligible by position (it is ordinary prose two blocks after the table) ...
    assert "b:0131" not in [a for _, a in pe.pooling_windows(KATHRYN, ["b:0101"], FIXTURES)]
    # ... and rejected on its own merits even if it WERE scanned
    scanned = pe.scan_text(texts["b:0131"])
    assert scanned and all(r["status"] == "rejected" for r in scanned)
    assert "statistical-procedure" in scanned[0]["rejection_reason"]
    # the other real "for all data presented in ..." sentence is a clause, not a factor
    other = pe.scan_text(texts["b:0093"])
    assert not [r for r in other if r["status"] == "accepted"]


def test_berntsons_co2_mean_over_mean_text_is_not_a_pooling_statement():
    texts = cr._rendered_block_texts(BERNTSON, FIXTURES)
    assert "ER ratio of elevated $CO_2$ mean over ambient $CO_2$ mean" in texts["b:0058"]
    assert pe.scan_text(texts["b:0058"]) == []  # "mean over" is a ratio, never a frame
    assert "b:0058" not in [a for _, a in pe.pooling_windows(BERNTSON, ["b:0059"], FIXTURES)]  # prose before a table is not its caption


# --------------------------------------------------------------------- #
# SYNTHETIC frames and window mechanics (invented text, not corpus evidence)
# --------------------------------------------------------------------- #


def _synthetic_paper(tmp_path: Path, blocks: list[tuple[str, str, str]], name: str = "syn") -> Path:
    """blocks: (anchor, block_type, text). Everything on page_1 unless the text starts with '@pN '."""
    root = tmp_path / "papers"
    (root / name).mkdir(parents=True)
    provenance, content = {}, ""
    for anchor, block_type, text in blocks:
        page = "page_1"
        if text.startswith("@p"):
            marker, text = text.split(" ", 1)
            page = f"page_{marker[2:]}"
        provenance[anchor] = {"block_type": block_type, "page_id": page, "section_path": [], "rendered_in_content_md": True}
        content += f"{text}\n⟦{anchor}⟧\n\n"
    (root / name / "content.md").write_text(content, encoding="utf-8")
    (root / name / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    return root


TABLE = "| a | 1.0 |\n|---|---|\n| b | 2.0 |"


@pytest.mark.parametrize("sentence, factor, dimension, pattern", [
    ("Data are means averaged across growing seasons.", "growing seasons", "time", "aggregation_verb_across"),
    ("Values were pooled over years.", "years", "time", "aggregation_verb_across"),
    ("Data were combined across sites and years.", "sites", "site", "aggregation_verb_across"),
    ("Values are the mean across all irrigation regimes.", "irrigation regimes", "other", "mean_across"),
    ("Data show means ± SE regardless of sampling depth.", "sampling depth", "other", "regardless_of"),
    ("Values are the mean ± SD for all fertilizer treatments.", "fertilizer", "treatment", "value_for_all"),
])
def test_synthetic_frames_are_recognised_in_the_note_after_a_table(tmp_path, sentence, factor, dimension, pattern):
    root = _synthetic_paper(tmp_path, [("b:0001", "Table", TABLE), ("b:0002", "Text", sentence)])
    accepted = [r for r in pe.detect_pooling_evidence("syn", ["b:0001"], root) if r["status"] == "accepted"]
    assert factor in [r["factor"] for r in accepted]
    [match] = [r for r in accepted if r["factor"] == factor]
    assert (match["dimension"], match["pattern"], match["anchor"]) == (dimension, pattern, "b:0002")
    assert all(r["excerpt"] in sentence for r in accepted)


@pytest.mark.parametrize("sentence, reason_part", [
    ("Values are the mean for all treatments.", "generic"),                       # no specific factor
    ("Data are means across blocks.", "replicate"),                                # replicate pooling is not meaningful
    ("Means separations for all ANOVAs were performed by a Tukey test.", "statistical"),
    ("Samples were pooled across years.", "reported-value subject"),               # no value noun subject
    ("Data are means across the samples that were collected in 1993.", "clause"),  # a clause, not a factor name
])
def test_synthetic_doubtful_statements_are_rejected_with_a_reason_never_guessed(tmp_path, sentence, reason_part):
    root = _synthetic_paper(tmp_path, [("b:0001", "Table", TABLE), ("b:0002", "Text", sentence)])
    records = pe.detect_pooling_evidence("syn", ["b:0001"], root)
    assert records and all(r["status"] == "rejected" for r in records)
    assert reason_part in records[0]["rejection_reason"]


def test_synthetic_a_caption_implies_its_own_value_subject_but_a_note_must_state_one(tmp_path):
    caption = "*Table 1. Yield of four cultivars averaged across sites.*"
    root = _synthetic_paper(tmp_path, [("b:0001", "Caption", caption), ("b:0002", "Table", TABLE)])
    assert [r["factor"] for r in pe.detect_pooling_evidence("syn", ["b:0002"], root)] == ["sites"]


def test_synthetic_window_boundaries(tmp_path):
    pooled = "Data are means pooled across years."
    blocks = [
        ("b:0001", "Table", TABLE),
        ("b:0002", "Footnote", "a Days after planting"),
        ("b:0003", "Text", "First body paragraph."),            # prose: after+2 -> ordinary prose is not directly adjacent
        ("b:0004", "Text", pooled),
        ("b:0010", "Table", TABLE),
        ("b:0011", "Footnote", "Nothing here."),
        ("b:0012", "Footnote", "Table 3. A different table's label."),   # only the label rule stops this (a Footnote is otherwise eligible)
        ("b:0013", "Footnote", pooled),
        ("b:0020", "Table", TABLE),
        ("b:0021", "Text", "@p9 " + pooled),                     # two pages later
        ("b:0030", "Table", TABLE),
        ("b:0031", "Footnote", "note one"),
        ("b:0032", "Footnote", "note two"),
        ("b:0033", "Footnote", "note three"),
        ("b:0034", "Footnote", pooled),                          # the 4th following block: beyond the limit
    ]
    root = _synthetic_paper(tmp_path, blocks)
    assert pe.pooling_windows("syn", ["b:0001"], root) == [("after+1", "b:0002")]
    assert pe.pooling_windows("syn", ["b:0010"], root) == [("after+1", "b:0011")]  # stops at the new "Table N" label
    assert pe.pooling_windows("syn", ["b:0020"], root) == []                       # page gap
    assert [a for _, a in pe.pooling_windows("syn", ["b:0030"], root)] == ["b:0031", "b:0032", "b:0033"]
    assert all(pe.detect_pooling_evidence("syn", [t], root) == [] for t in ("b:0001", "b:0010", "b:0020", "b:0030"))


@pytest.mark.parametrize("stopper_type", ["Caption", "Figure", "Picture", "SectionHeader", "ListItem", "Table"])
def test_synthetic_the_after_window_stops_at_structural_blocks(tmp_path, stopper_type):
    blocks = [("b:0001", "Table", TABLE), ("b:0002", stopper_type, "Data are means pooled across years."),
              ("b:0003", "Footnote", "Data are means pooled across years.")]
    root = _synthetic_paper(tmp_path, blocks)
    assert pe.pooling_windows("syn", ["b:0001"], root) == []


def test_synthetic_a_directly_adjacent_prose_block_is_eligible_a_footnote_may_follow_it(tmp_path):
    blocks = [("b:0001", "Table", TABLE), ("b:0002", "Text", "Note."), ("b:0003", "Footnote", "Data are means pooled across years.")]
    assert [a for _, a in pe.pooling_windows("syn", ["b:0001"], _synthetic_paper(tmp_path, blocks))] == ["b:0002", "b:0003"]


# --------------------------------------------------------------------- #
# schema provenance + backward compatibility
# --------------------------------------------------------------------- #


def test_pooled_factor_origin_and_pattern_default_so_old_classifications_still_load():
    old = {"name": "cultivar mixture", "dimension": "treatment", "evidence_anchor": "b:0119", "evidence_excerpt": "x"}
    pf = PooledFactor.model_validate(old)
    assert (pf.origin, pf.pattern) == ("model", None)
    with pytest.raises(Exception):
        PooledFactor.model_validate({**old, "origin": "someone-else"})


# --------------------------------------------------------------------- #
# Step B integration (real fixtures; the model is a stub)
# --------------------------------------------------------------------- #


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return tmp_path


def _classify(run_id: str, paper: str, seed: str, payload: dict):
    def invoke(agent, model, prompt, timeout=300):
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    return orchestrator.run_table_classification(
        run_id=run_id, paper_id=paper, seed_table_anchor=seed, other_tables=[], model="m", invoke=invoke,
    )


def _felipe_payload(*, treatment_column_hints=True, **extra) -> dict:
    """What the live model actually returned for Felipe Table 1 (treatment only as column hints, no pooling)."""
    columns = [
        {"value_column_id": "fallow", "variable_name_hint": "measurement", "treatment_level_hint": "Fallow" if treatment_column_hints else None},
        {"value_column_id": "mustard", "variable_name_hint": "measurement", "treatment_level_hint": "Mustard" if treatment_column_hints else None},
    ]
    payload = {
        "table_role": "treatment_response", "table_anchors": ["b:0069"],
        "factors": [{"name": "DAP", "dimension": "time", "encoding": "rows"}, {"name": "Variable", "dimension": "variable", "encoding": "rows"}],
        "value_columns": columns,
        "row_groups": [
            {"row_group_id": "par35", "factor_values": {"DAP": "35", "Variable": "PAR intercepted (%)"}, "source_table_anchor": "b:0069",
             "cells": {"fallow": "20±1.0 a", "mustard": "15±0.9 b"}},
            {"row_group_id": "fruit111", "factor_values": {"DAP": "111", "Variable": "Total fruit (g m -2 )"}, "source_table_anchor": "b:0069",
             "cells": {"fallow": "352±15.4 a", "mustard": "252±15.1 b"}},
        ],
    }
    payload.update(extra)
    return payload


def test_deterministic_detection_fills_an_empty_pooled_factors_and_records_it(env):
    result, error = _classify("r1", FELIPE, "b:0069", _felipe_payload())
    assert error is None
    [pooled] = result.pooled_factors
    assert (pooled.name, pooled.dimension, pooled.evidence_anchor, pooled.origin, pooled.pattern) == (
        "cultivar mixture", "treatment", "b:0119", "deterministic", "value_for_all")
    assert pooled.evidence_excerpt == FELIPE_NOTE_SENTENCE
    saved = json.loads((env / "runs" / "r1" / "records" / "table_classification__b_0069" / "final.json").read_text())
    [record] = saved["pooling_evidence"]
    assert (record["status"], record["applied"], record["factor"], record["anchor"], record["pattern"]) == (
        "accepted", True, "cultivar mixture", "b:0119", "value_for_all")
    assert record["excerpt"] == FELIPE_NOTE_SENTENCE
    # the cached copy reloads with its provenance
    assert orchestrator._load_cached_table_classification("r1", "table_classification__b_0069").pooled_factors[0].origin == "deterministic"


def test_felipe_downstream_two_treatments_aggregated_mean_no_mixture_treatments_no_l1(env):
    result, _ = _classify("r1", FELIPE, "b:0069", _felipe_payload())
    assert orchestrator._pooled_representability(result) == (True, None)
    treatments, covered = orchestrator._table_classifications_to_treatment_candidates([result], {})
    assert sorted(t.description for t in treatments) == ["Experimental condition: Treatment=Fallow", "Experimental condition: Treatment=Mustard"]
    assert covered == {"b:0069"}
    assert not any("mixture" in t.description.lower() or "mixture" in t.candidate_id for t in treatments)  # a pooled factor never forms a Treatment
    observations = orchestrator._table_classification_to_candidates(result, {})
    assert len(observations) == 4  # 2 rows x 2 treatment columns
    for candidate in observations:
        assert candidate.context["reported_effect_scope"] == "aggregated_mean"
        assert candidate.context["aggregated_over_factors"] == ["cultivar mixture"]
        assert candidate.context["pooling_evidence"][0]["anchor"] == "b:0119"
    summary = orchestrator.summarize_table_pass("r1", FELIPE, {})
    assert summary["limitations"] == []
    [source] = summary["aggregated_sources"]
    assert source["kind"] == "pooled_treatment_response" and source["representable_in_current_ir"] is True
    assert source["aggregated_over_factors"] == ["cultivar mixture"]


def test_a_model_supplied_pooled_factors_is_preserved_and_the_agreement_is_recorded(env):
    model_pooled = {"name": "cultivar mixtures", "dimension": "treatment", "evidence_anchor": "b:0119",
                    "evidence_excerpt": "for all cultivar mixture treatments", "origin": "deterministic", "pattern": "spoof"}
    result, error = _classify("r1", FELIPE, "b:0069", _felipe_payload(pooled_factors=[model_pooled]))
    assert error is None
    [pooled] = result.pooled_factors
    assert (pooled.name, pooled.evidence_excerpt) == ("cultivar mixtures", "for all cultivar mixture treatments")
    assert (pooled.origin, pooled.pattern) == ("model", None)  # a model can never claim to be the detector
    [record] = orchestrator._cached_pooling_evidence("r1")["table_classification__b_0069"]
    assert (record["status"], record["applied"], record["agreement"]) == ("accepted", False, "agrees")
    assert record["model_pooled_factors"] == ["cultivar mixtures"]


def test_a_model_pooled_factor_that_differs_from_the_detected_one_is_kept_and_the_disagreement_logged(env):
    model_pooled = {"name": "sampling date", "dimension": "time", "evidence_anchor": "b:0119", "evidence_excerpt": "since no differences were observed"}
    result, _ = _classify("r1", FELIPE, "b:0069", _felipe_payload(pooled_factors=[model_pooled]))
    assert [pf.name for pf in result.pooled_factors] == ["sampling date"]
    [record] = orchestrator._cached_pooling_evidence("r1")["table_classification__b_0069"]
    assert (record["factor"], record["agreement"], record["applied"]) == ("cultivar mixture", "disagrees", False)
    assert record["model_pooled_factors"] == ["sampling date"]


def test_a_detected_factor_the_table_itself_reports_is_rejected_not_pooled(env):
    payload = _felipe_payload()
    payload["factors"].append({"name": "Cultivar mixture", "dimension": "treatment", "encoding": "rows"})
    for row in payload["row_groups"]:
        row["factor_values"]["Cultivar mixture"] = "3-cv"
    result, _ = _classify("r1", FELIPE, "b:0069", payload)
    assert result.pooled_factors == []
    [record] = orchestrator._cached_pooling_evidence("r1")["table_classification__b_0069"]
    assert record["status"] == "rejected" and "declared factor" in record["rejection_reason"] and record["applied"] is False


def test_no_pooled_factors_is_ever_added_to_a_table_that_is_not_a_treatment_response(env):
    payload = _felipe_payload(table_role="aggregated_summary", reason="means over the cultivar mixtures")
    result, _ = _classify("r1", FELIPE, "b:0069", payload)
    assert result.pooled_factors == []
    [record] = orchestrator._cached_pooling_evidence("r1")["table_classification__b_0069"]
    assert record["status"] == "accepted" and record["applied"] is False and "informational" in record["note"]


def test_pooled_with_no_retained_treatment_is_L1_and_generates_nothing(env):
    result, _ = _classify("r1", FELIPE, "b:0069", _felipe_payload(treatment_column_hints=False))
    assert [pf.name for pf in result.pooled_factors] == ["cultivar mixture"]
    assert orchestrator._pooled_representability(result) == (False, orchestrator.LIMITATION_L1)
    assert orchestrator._table_classification_to_candidates(result, {}) == []
    assert orchestrator._table_classifications_to_treatment_candidates([result], {})[0] == []


def _daren_table_7_payload(role: str = "treatment_response") -> dict:
    return {
        "table_role": role, "table_anchors": ["b:0761"], "reason": "values averaged across locations and maturities" if role != "treatment_response" else None,
        "factors": [{"name": "Population", "dimension": "crop", "encoding": "rows"}, {"name": "Variable", "dimension": "variable", "encoding": "columns"}],
        "value_columns": [{"value_column_id": "lai", "variable_name_hint": "LAI", "factor_levels": {"Variable": "LAI"}}],
        "row_groups": [{"row_group_id": "tb", "factor_values": {"Population": "Trailblazer"}, "source_table_anchor": "b:0761", "cells": {"lai": "3.9"}}],
    }


def test_daren_table_7_pooled_over_site_is_L2_unrepresentable_and_creates_no_treatment(env):
    result, error = _classify("r1", DAREN, "b:0761", _daren_table_7_payload())
    assert error is None
    assert [(pf.name, pf.dimension, pf.origin) for pf in result.pooled_factors] == [
        ("locations", "site", "deterministic"), ("maturities", "time", "deterministic")]
    assert all(pf.evidence_anchor == "b:0760" for pf in result.pooled_factors)
    assert orchestrator._pooled_representability(result) == (False, orchestrator.LIMITATION_L2)
    assert orchestrator._table_classification_to_candidates(result, {}) == []
    assert orchestrator._table_classifications_to_treatment_candidates([result], {})[0] == []  # Daren stays without a Treatment
    summary = orchestrator.summarize_table_pass("r1", DAREN, {})
    [source] = summary["aggregated_sources"]
    assert source["representable_in_current_ir"] is False and source["why_not_represented"] == orchestrator.LIMITATION_L2
    assert [(e["status"], e["factor"], e["applied"]) for e in summary["pooling_evidence"]] == [
        ("accepted", "locations", True), ("accepted", "maturities", True)]


def test_daren_table_7_as_an_aggregated_summary_gets_the_caption_only_as_informational_evidence(env):
    result, _ = _classify("r1", DAREN, "b:0761", _daren_table_7_payload("aggregated_summary"))
    assert result.table_role == "aggregated_summary" and result.pooled_factors == []
    records = orchestrator.summarize_table_pass("r1", DAREN, {})["pooling_evidence"]
    assert [(r["factor"], r["applied"], r["anchor"], r["position"]) for r in records] == [
        ("locations", False, "b:0760", "own_caption"), ("maturities", False, "b:0760", "own_caption")]
    assert all("informational" in r["note"] for r in records)
    assert all({"status", "anchor", "excerpt", "factor", "pattern"} <= set(r) for r in records)


def test_the_manifest_summary_lists_rejections_with_their_reason(env):
    payload = _felipe_payload()
    payload["factors"].append({"name": "Cultivar mixture", "dimension": "treatment", "encoding": "rows"})
    for row in payload["row_groups"]:
        row["factor_values"]["Cultivar mixture"] = "3-cv"
    _classify("r1", FELIPE, "b:0069", payload)
    [entry] = orchestrator.summarize_table_pass("r1", FELIPE, {})["pooling_evidence"]
    assert entry["status"] == "rejected" and entry["rejection_reason"] and entry["anchor"] == "b:0119"
    assert entry["excerpt"] == FELIPE_NOTE_SENTENCE and entry["factor"] == "cultivar mixture" and entry["table_anchors"] == ["b:0069"]


def test_a_table_with_no_pooling_language_records_nothing_and_old_cached_results_still_summarise(env):
    from pipeline import run_store
    tc = TableClassification(
        table_role="treatment_response", table_anchors=["b:0001"],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="y")],
        row_groups=[TableRowGroup(row_group_id="r", source_table_anchor="b:0001", cells={"y": "1"})],
    )
    run_store.save_final("r1", "table_classification__b_0001", {"status": "success", "classification": tc.model_dump()})  # no pooling_evidence key
    summary = orchestrator.summarize_table_pass("r1", "syn", {})
    assert summary["pooling_evidence"] == [] and summary["tables"][0]["pooled_factors"] == []


def test_a_detected_pooled_name_that_is_a_ready_site_record_is_reconciled_to_site_L2():
    pooled = PooledFactor(name="Ames", dimension="other", evidence_anchor="b:0001", evidence_excerpt="x", origin="deterministic", pattern="p")
    tc = TableClassification(
        table_role="treatment_response", table_anchors=["b:0002"], pooled_factors=[pooled],
        factors=[TableFactor(name="Tillage", dimension="treatment", encoding="rows")],
        value_columns=[TableValueColumn(value_column_id="y", variable_name_hint="y")],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Tillage": "till"}, source_table_anchor="b:0002", cells={"y": "1"})],
    )
    pools = {"crop": [], "site": [{"slug": "ames_ia", "name": "Ames"}]}
    corrected, overrides = orchestrator._reconcile_factor_dimensions(tc, pools)
    assert corrected.pooled_factors[0].dimension == "site" and overrides[0]["pooled"] is True
    assert orchestrator._pooled_representability(corrected) == (False, orchestrator.LIMITATION_L2)
    # a model-supplied pooled factor is never re-dimensioned by this rule
    model_owned = tc.model_copy(update={"pooled_factors": [pooled.model_copy(update={"origin": "model"})]})
    assert orchestrator._reconcile_factor_dimensions(model_owned, pools)[1] == []
