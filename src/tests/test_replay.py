"""Evidence replay corpus (`pipeline/replay.py`): the corpus stays true to the papers, and the scorer tells
not-attempted, not-retrieved, retrieved-not-captured and captured apart from recorded run artifacts alone."""

import json
from pathlib import Path

import pytest

from pipeline import replay
from pipeline.replay import Expectation, check_outcome, entity_trace, score_expectation


# --------------------------------------------------------------------------- #
# The real corpus
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("paper_id", replay.corpus_papers())
def test_every_corpus_expectation_is_literally_in_its_cited_blocks(paper_id):
    if not (Path(__file__).resolve().parents[1] / "paper" / paper_id / "content.md").is_file():
        pytest.skip(f"{paper_id} is not prepared on this machine (src/paper is not in git)")
    # Every literal must sit in the content.md block(s) the expectation names; an `absent` expectation's pattern must
    # find nothing. (The PDF side is checked by `python -m pipeline.replay --verify`, which needs pdftotext.)
    assert replay.verify_corpus(paper_id) == []


def test_the_corpus_covers_the_five_regression_papers():
    assert set(replay.corpus_papers()) >= {
        "Daren-1997-Canopy", "Felipe-2010-Cultivar", "Kathryn-2020-Winter", "Philippe-2007-Six", "Smulker-2012-Assessment",
    }


# --------------------------------------------------------------------------- #
# Corpus verification rules
# --------------------------------------------------------------------------- #

BLOCKS = {"b:0001": "Measurements were made near Mead, NE (45°42′ N).", "b:0002": "| Na (g m–2) | 1.1 |"}


def test_a_literal_missing_from_its_block_is_reported():
    exp = Expectation(id="x", entity_type="Site", concept="c", anchors=("b:0001",), literals=("Ames, IA",))
    assert any("not in block" in p for p in replay.verify_expectation(exp, BLOCKS, ""))


def test_an_absent_expectation_whose_pattern_matches_the_document_is_reported():
    exp = Expectation(id="x", entity_type="Citation", concept="doi", status="absent", pattern=r"10\.\d{4,9}/")
    assert replay.verify_expectation(exp, BLOCKS, "see doi 10.1234/abc") != []
    assert replay.verify_expectation(exp, BLOCKS, "no identifier here") == []


def test_upstream_absent_requires_the_pdf_text_that_proves_it():
    exp = Expectation(id="x", entity_type="Citation", concept="doi", status="upstream_absent")
    assert any("pdf_literals" in p for p in replay.verify_expectation(exp, BLOCKS, ""))


def test_pdf_check_tolerates_line_breaks_and_hyphenation_but_not_missing_text():
    exp = Expectation(id="x", entity_type="Site", concept="c", anchors=("b:0001",), literals=("randomized complete block",))
    assert replay.verify_pdf(exp, "a random-\nized complete\nblock design") == []
    assert replay.verify_pdf(exp, "a split-plot design") != []


def test_a_documented_manual_pdf_verification_skips_the_automatic_check():
    exp = Expectation(id="x", entity_type="Site", concept="c", anchors=("b:0001",), literals=("anything",),
                      pdf_verification="visual: rendered page 3")
    assert replay.verify_pdf(exp, "") == []


# --------------------------------------------------------------------------- #
# Scoring a recorded run
# --------------------------------------------------------------------------- #

def _tool_line(output: object) -> str:
    return json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "read_nearby",
                                                     "state": {"status": "completed", "output": json.dumps(output)}}})


def _write_attempt(run_dir: Path, record: str, stage: str, prompt: str = "", outputs: tuple = (), parsed=None) -> None:
    path = run_dir / "records" / record / stage / "attempt1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"prompt": prompt, "stdout": "\n".join(_tool_line(o) for o in outputs),
                                "parsed_json": parsed}), encoding="utf-8")


SITE_COORDS = Expectation(id="site.coordinates", entity_type="Site", concept="coords", anchors=("b:0001",),
                          literals=("45°42′ N",), outcome={"field": "latitude"})


def test_an_entity_with_no_trace_is_not_attempted_and_reports_why(tmp_path):
    blocked = [{"status": "blocked", "reason": "required prerequisite Site is not 'ready' in this run"}]
    row = score_expectation(SITE_COORDS, entity_trace(tmp_path, "Site"), blocked, None)
    assert row["state"] == "not_attempted"
    assert "prerequisite Site" in row["blocked_reason"]


def test_a_literal_returned_by_a_read_tool_counts_as_retrieved_even_when_json_escaped(tmp_path):
    # The tool output is JSON with the non-ASCII characters \\u-escaped; retrieval is judged on the decoded text.
    _write_attempt(tmp_path, "Site__s1", "extraction", prompt="Extract the Site.",
                   outputs=({"blocks": [{"block_anchor": "b:0001", "text": "near Mead (45°42′ N)."}]},))
    row = score_expectation(SITE_COORDS, entity_trace(tmp_path, "Site"), [], None)
    assert row["retrieved"] is True and row["supplied"] is False
    assert row["state"] == "retrieved_not_captured"


def test_a_literal_the_model_never_saw_is_not_retrieved(tmp_path):
    _write_attempt(tmp_path, "Site__s1", "extraction", prompt="Extract the Site.",
                   outputs=({"blocks": [{"block_anchor": "b:0009", "text": "unrelated"}]},))
    row = score_expectation(SITE_COORDS, entity_trace(tmp_path, "Site"), [], None)
    assert row["state"] == "not_retrieved"


def test_a_literal_in_the_orchestrator_prompt_counts_as_supplied(tmp_path):
    _write_attempt(tmp_path, "Site__s1", "extraction", prompt="Front matter: [b:0001] near Mead (45°42′ N).")
    row = score_expectation(SITE_COORDS, entity_trace(tmp_path, "Site"), [], None)
    assert row["supplied"] is True and row["retrieved"] is True


def test_citing_a_cell_of_the_expected_table_counts_as_citing_the_table(tmp_path):
    exp = Expectation(id="v", entity_type="Variable", concept="units", anchors=("b:0050",), literals=("Na (g m–2)",))
    provenance = {"b:0051": {"parent_table_anchor": "b:0050"}}
    _write_attempt(tmp_path, "Variable__na", "extraction", outputs=({"text": "Na (g m–2)"},),
                   parsed={"facts": [{"field_name": "units", "anchors": ["b:0051"]}]})
    row = score_expectation(exp, entity_trace(tmp_path, "Variable"), [], provenance)
    assert row["cited"] is True


def test_only_the_retrieval_stages_count_toward_retrieval(tmp_path):
    # A conversion prompt restates the raw evidence; it is not evidence the extraction stage went and found.
    _write_attempt(tmp_path, "Site__s1", "conversion", prompt="Map raw evidence: 45°42′ N")
    _write_attempt(tmp_path, "Site__s1", "extraction", prompt="Extract the Site.")
    row = score_expectation(SITE_COORDS, entity_trace(tmp_path, "Site"), [], None)
    assert row["retrieved"] is False


# --------------------------------------------------------------------------- #
# Outcome checks
# --------------------------------------------------------------------------- #

def _field(value, label="EXTRACTED"):
    return {"value": value, "provenance_label": label, "source": {"locators": [{"kind": "text", "block_anchor": "b:0001"}]}}


def test_a_value_in_a_record_that_was_not_committed_is_not_captured():
    record = {"status": "unresolved", "payload": {"latitude": _field({"reported_text": "45°42′ N"})}}
    assert check_outcome({"field": "latitude"}, [record]) is False
    assert check_outcome({"field": "latitude"}, [{**record, "status": "ready"}]) is True


def test_an_unresolved_field_is_not_captured_unless_the_expectation_asks_for_unresolved():
    record = {"status": "ready", "payload": {"persistent_identifier": _field(None, "UNRESOLVED")}}
    assert check_outcome({"field": "persistent_identifier"}, [record]) is False
    assert check_outcome({"field": "persistent_identifier", "label_in": ["UNRESOLVED"]}, [record]) is True


def test_where_filters_select_the_intended_record_and_exclude_near_misses():
    increment = {"status": "ready", "payload": {"name": _field("annual basal stem diameter increment"), "units": _field("mm")}}
    diameter = {"status": "ready", "payload": {"name": _field("stem basal diameter"), "units": _field(None, "UNRESOLVED")}}
    outcome = {"field": "units", "contains_any": ["mm"],
               "where": {"field": "name", "contains_any": ["basal diameter", "stem basal diameter"], "not_contains_any": ["increment"]}}
    assert check_outcome(outcome, [increment, diameter]) is False


def test_a_field_list_is_any_of():
    record = {"status": "ready", "payload": {"name": _field("University research center"), "nearest_city": _field("Mead")}}
    assert check_outcome({"field": ["name", "nearest_city"], "contains_any": ["Mead"]}, [record]) is True


def test_packet_recall_reports_supplied_evidence_per_expectation():
    paper_id = "Philippe-2007-Six"
    if not (Path(__file__).resolve().parents[1] / "paper" / paper_id / "content.md").is_file():
        pytest.skip(f"{paper_id} is not prepared on this machine")
    rows = {r["id"]: r for r in replay.packet_recall(paper_id, Path(__file__).resolve().parents[1] / "paper")}
    assert rows["management.thinning"]["supplied"] is True           # never enumerated in two live runs before
    assert rows["variable.table3_leaf_mass_per_area"]["supplied"] is True
    assert rows["citation.title"]["packet"] is None                   # Citation keeps its own front-matter context
