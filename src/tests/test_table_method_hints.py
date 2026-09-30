"""Correction pass, Fix 1: grounded method hints for table variables (Felipe Table 1).

Real evidence (run felipe_smoke_20260920T132343, Felipe-2010-Cultivar): Step B reconstructed Table 1 perfectly on the
first attempt -- 22 of 22 cells matched the source, pooling over "cultivar mixture" was detected -- but the model made
only two tool calls (read_table, read_nearby), never read Methods, and returned `method_hint=None` for all 6 variables.
All 22 table Observation candidates then reached extraction and died at "method_id is ambiguous among 15 equally-valid
candidates": none reached Conversion.

The propagation code (`_effective_method_hint` -> `_match_method_hint`) was never the problem: with a hint it links.
The information was lost at Step B, where the Methods text was never in front of the model. The fix supplies the
paper's Methods prose to the Step B prompt and keeps a hint only when that prose supports it. The approved matcher is
NOT touched: these tests pin that it still refuses ambiguous and ungrounded hints.

Fixtures (real): tests/fixtures/pass2/Felipe-2010-Cultivar (trimmed real content/provenance), felipe_table1_classification.json
(the real Step B result for Table 1) and felipe_method_pool.json (the 15 real Methods of that run).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import orchestrator
from pipeline.raw_schema import TableClassification

FIXTURES = Path(__file__).parent / "fixtures" / "pass2"
PAPER = "Felipe-2010-Cultivar"

PAR_HINT = "portable-tube solarimeter photosynthetically active radiation"   # Methods, b:0041
N_HINT = "total N Nitrogen Gas Analyzer"                                       # Methods, b:0038 (real wording)
UNGROUNDED_HINT = "Kjeldahl digestion titration"                               # the paper never says this
INCIDENTAL_HINT = "measured using standard methods"                            # one incidental word, no method


@pytest.fixture()
def felipe(monkeypatch):
    monkeypatch.setenv("IR_PAPERS_ROOT", str(FIXTURES))


def _pool() -> list[dict]:
    return json.loads((FIXTURES / "felipe_method_pool.json").read_text())


def _classification(hints: dict[str, str]) -> TableClassification:
    data = json.loads((FIXTURES / "felipe_table1_classification.json").read_text())
    for variable in data["variables"]:
        variable["method_hint"] = hints.get(variable["label"])
    return TableClassification.model_validate(data)


def _linked(classification: TableClassification) -> dict[str, set]:
    """{variable label hint: {linked method slug or None}} over every table candidate."""
    out: dict[str, set] = {}
    for c in orchestrator._table_classification_to_candidates(classification, {"method_id": _pool()}):
        out.setdefault(c.variable_name_hint, set()).add(c.linked_candidates.get("method_id"))
    return out


# --------------------------------------------------------------------- #
# the Methods text handed to Step B
# --------------------------------------------------------------------- #


def test_methods_context_is_the_real_methods_prose_by_position(felipe):
    ctx = orchestrator._methods_context(PAPER)
    assert ctx.count("[b:") == 11
    assert "portable" in ctx and "solarimeter" in ctx and "Nitrogen Gas Analyzer" in ctx
    # Marker's section_path does NOT mention methods for these blocks; the span is positional
    provenance = json.loads((FIXTURES / PAPER / "provenance.json").read_text())
    assert not any("method" in seg.lower() for seg in provenance["b:0041"]["section_path"])
    # nothing from Results, and no table block
    assert "[b:0049]" not in ctx and "[b:0069]" not in ctx and "Table 1" not in ctx


def test_methods_context_is_empty_without_a_methods_header(tmp_path, monkeypatch):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "content.md").write_text("Some prose about tomatoes.\n⟦b:0001⟧\n", encoding="utf-8")
    (tmp_path / "p" / "provenance.json").write_text(json.dumps(
        {"b:0001": {"block_type": "Text", "page_id": "page_0", "section_path": ["T"], "rendered_in_content_md": True}}), encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(tmp_path))
    assert orchestrator._methods_context("p") == ""


def test_prompt_carries_the_methods_text_only_when_there_is_some(felipe):
    with_ctx = orchestrator._table_classification_prompt(PAPER, "b:0069", [], methods_context=orchestrator._methods_context(PAPER))
    assert "METHODS TEXT" in with_ctx and "[b:0041]" in with_ctx and "discarded" in with_ctx
    without = orchestrator._table_classification_prompt(PAPER, "b:0069", [])
    assert "METHODS TEXT" not in without
    assert with_ctx.replace(with_ctx[with_ctx.index("METHODS TEXT"):with_ctx.index("Output ONLY a TableClassification")], "") == without


# --------------------------------------------------------------------- #
# grounding: what is kept, what is withheld
# --------------------------------------------------------------------- #


def test_grounded_hints_are_kept_and_ungrounded_or_incidental_ones_withheld_with_flags(felipe):
    tc = _classification({
        "PAR intercepted (%)": PAR_HINT, "Aboveground N (g N m -2 )": N_HINT,
        "Shoot biomass (g m -2 )": UNGROUNDED_HINT, "Harvest index": INCIDENTAL_HINT,
    })
    out = orchestrator._withhold_ungrounded_method_hints(tc, PAPER)
    kept = {v.label: v.method_hint for v in out.variables}
    assert kept["PAR intercepted (%)"] == PAR_HINT and kept["Aboveground N (g N m -2 )"] == N_HINT
    assert kept["Shoot biomass (g m -2 )"] is None and kept["Harvest index"] is None
    flags = {f.key: f for f in out.method_hint_flags}
    assert set(flags) == {"Shoot biomass (g m -2 )", "Harvest index"}
    assert flags["Shoot biomass (g m -2 )"].method_hint == UNGROUNDED_HINT and flags["Shoot biomass (g m -2 )"].scope == "variable"


def test_a_single_distinctive_word_needs_the_exact_word_in_the_prose(felipe):
    prose, texts = orchestrator._prose_token_sets(PAPER), orchestrator._prose_normalized_texts(PAPER)
    assert orchestrator._method_hint_grounded("colorimeter", prose, texts)        # the word is in Methods
    assert not orchestrator._method_hint_grounded("spectrophotometer", prose, texts)
    assert not orchestrator._method_hint_grounded("hand method measurement", prose, texts)  # only generic words


def test_model_supplied_flags_are_discarded(felipe):
    tc = _classification({}).model_copy(update={"method_hint_flags": [
        {"scope": "variable", "key": "x", "method_hint": "made up", "reason": "made up"}]})
    assert orchestrator._withhold_ungrounded_method_hints(tc, PAPER).method_hint_flags == []


# --------------------------------------------------------------------- #
# grounded table variable -> method hint -> conservative matcher -> correct link
# --------------------------------------------------------------------- #


def test_no_hint_reproduces_the_felipe_failure_every_cell_is_unresolved(felipe):
    linked = _linked(_classification({}))
    assert len(linked) == 6 and all(v == {None} for v in linked.values())


def test_grounded_hints_link_the_right_methods_through_the_unchanged_matcher(felipe):
    tc = orchestrator._withhold_ungrounded_method_hints(_classification({
        "PAR intercepted (%)": PAR_HINT, "Aboveground N (g N m -2 )": N_HINT,
        "Shoot biomass (g m -2 )": UNGROUNDED_HINT, "Harvest index": INCIDENTAL_HINT,
    }), PAPER)
    linked = _linked(tc)
    assert linked["PAR intercepted"] == {"canopy_par_interception"}
    assert linked["aboveground N"] == {"tomato_total_nitrogen"}
    # variables without a grounded hint stay unresolved -- nothing is guessed, no Method is invented
    assert linked["shoot biomass"] == {None} and linked["harvest index"] == {None}
    assert linked["total fruit"] == {None} and linked["harvestable fruit"] == {None}


def test_an_ambiguous_grounded_hint_still_resolves_nothing(felipe):
    """The matcher's tie rule is untouched: a hint several Methods' descriptions all contain is never resolved."""
    pool = _pool()
    for h in ("fruit color firmness", "soluble solids titratable acidity pH", "soil samples colorimetric"):
        assert orchestrator._match_method_hint({"Method": h}, h, pool) is None


def test_step_b_end_to_end_records_flags_and_prompts_with_the_methods_text(felipe, tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    data = json.loads((FIXTURES / "felipe_table1_classification.json").read_text())
    for variable in data["variables"]:
        variable["method_hint"] = {"PAR intercepted (%)": PAR_HINT, "Shoot biomass (g m -2 )": UNGROUNDED_HINT}.get(variable["label"])
    prompts = []

    def invoke(agent, model, prompt, timeout=300):
        prompts.append(prompt)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="", stderr="",
                                            final_text=json.dumps(data), parsed_json=data)

    tc, error = orchestrator.run_table_classification(
        run_id="r1", paper_id=PAPER, seed_table_anchor="b:0069", other_tables=[], model="m", invoke=invoke)
    assert error is None and "METHODS TEXT" in prompts[0]
    assert {v.label: v.method_hint for v in tc.variables}["PAR intercepted (%)"] == PAR_HINT
    assert [f.key for f in tc.method_hint_flags] == ["Shoot biomass (g m -2 )"]
    # the flag is disclosed in the run's table summary (cached classification carries it)
    cached = orchestrator._load_cached_table_classification("r1", "table_classification__b_0069")
    assert [f.method_hint for f in cached.method_hint_flags] == [UNGROUNDED_HINT]
