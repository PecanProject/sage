"""Item 8: free-form / table Treatment candidate deduplication.

Real evidence: run 20260919T085532_98015a8a (Daren-1997-Canopy) produced 32 free-form
Treatment candidates ("Trailblazer population at Ames, IA in the vegetative sward
maturity", ...) that duplicated Table 6's own table-derived candidates. They cited
`b:0606` -- the table's CAPTION block, not its table block -- and carried no links, so
neither the anchor-based nor the link-based drop removed them. Item 8 compares the two
sources by the same canonical identity instead; free-form candidates declare
`dimensions`, and the comparison is semantic, never a count and never "delete all
free-form when tables exist".

Fixtures: tests/fixtures/item8/ (real artifacts, see its README). Tests marked
SYNTHETIC use invented conditions to pin rules the real case does not exercise. No test
treats an expected record count as the criterion: every expectation is derived from the
candidates' own levels.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store
from pipeline.raw_schema import (
    CandidateDimension, EnumerationCandidate, TableClassification, TableFactor, TableRowGroup, TableValueColumn,
)

FIXTURES = Path(__file__).parent / "fixtures" / "item8"
SITE_POOL = [{"slug": "ames_ia", "name": "Ames, IA", "record_id": "p_site_ames_ia"},
             {"slug": "mead_ne", "name": "Mead, NE", "record_id": "p_site_mead_ne"}]
_DESCRIPTION = re.compile(r"^(.+?) (?:population|line) at (.+?) in the (\w+) sward maturity\.$")


def _real_freeform() -> list[EnumerationCandidate]:
    return [EnumerationCandidate.model_validate(c) for c in json.loads((FIXTURES / "085532_freeform_treatment_candidates.json").read_text())["candidates"]]


def _real_legacy_tables() -> list[TableClassification]:
    return [TableClassification.model_validate(c) for c in json.loads((FIXTURES / "085532_table6_legacy_classifications.json").read_text())]


def _real_declared_table() -> TableClassification:
    return TableClassification.model_validate(json.loads((FIXTURES / "live_gpt_oss_table6_declared_classification.json").read_text()))


def _declare(candidate: EnumerationCandidate, mode: str) -> EnumerationCandidate:
    """What a compliant model would declare for a real candidate, read off its own description."""
    population, site, maturity = _DESCRIPTION.match(candidate.description).groups()
    if mode == "as_treatment":  # the legacy reading: population and maturity identify the treatment
        dims = [CandidateDimension(name="Entry", dimension="treatment", level=population),
                CandidateDimension(name="Stage", dimension="treatment", level=maturity),
                CandidateDimension(name="Location", dimension="site", level=site)]
    else:  # protocol Section 6.3 (decision Q1): population=crop, maturity=time, site=site
        dims = [CandidateDimension(name="Population", dimension="crop", level=population),
                CandidateDimension(name="Maturity", dimension="time", level=maturity),
                CandidateDimension(name="Location", dimension="site", level=site)]
    return candidate.model_copy(update={"dimensions": dims})


def _dedup(freeform, tables, pool=SITE_POOL, dimension_pools=None):
    return orchestrator._drop_freeform_treatments_covered_by_tables(freeform, tables, pool, dimension_pools)


def _decisions(decisions):
    return {d["candidate_id"]: d["decision"] for d in decisions}


def _dim(name, dimension, level):
    return CandidateDimension(name=name, dimension=dimension, level=level)


def _candidate(cid, *dims, anchors=("b:0001",), description=None, **kw):
    return EnumerationCandidate(candidate_id=cid, description=description or cid, anchors=list(anchors), dimensions=list(dims), **kw)


# --------------------------------------------------------------------- #
# the real 085532 case
# --------------------------------------------------------------------- #


def test_real_085532_free_form_candidates_cited_the_caption_so_neither_old_drop_removed_them():
    freeform = _real_freeform()
    tables = _real_legacy_tables()
    table_candidates, covered = orchestrator._table_classifications_to_treatment_candidates(tables, {"site_id": SITE_POOL})
    assert {a for c in freeform for a in c.anchors} == {"b:0606"} and "b:0606" not in covered
    assert orchestrator._drop_candidates_covered_by_tables(freeform, covered) == freeform          # the anchor rule missed all of them
    assert orchestrator._drop_freeform_candidates_subsumed_by_tables(freeform, table_candidates) == freeform  # they carried no links


def test_real_085532_candidates_without_declared_dimensions_are_kept_and_flagged_never_guessed():
    freeform = _real_freeform()
    assert all(c.dimensions == [] for c in freeform)  # recorded before the field existed
    table_candidates, _ = orchestrator._table_classifications_to_treatment_candidates(_real_legacy_tables(), {"site_id": SITE_POOL})
    kept, decisions = _dedup(freeform, table_candidates)
    assert kept == freeform and set(_decisions(decisions).values()) == {"kept_unresolved"}


def test_real_085532_duplicates_are_resolved_once_the_free_form_candidates_declare_their_dimensions():
    """Legacy reading (population + maturity identify the treatment): every free-form candidate
    is the same condition a table candidate already reports."""
    freeform = [_declare(c, "as_treatment") for c in _real_freeform()]
    table_candidates, _ = orchestrator._table_classifications_to_treatment_candidates(_real_legacy_tables(), {"site_id": SITE_POOL})
    kept, decisions = _dedup(freeform, table_candidates)
    by_id = {t.candidate_id: t for t in table_candidates}
    assert kept == [] and set(_decisions(decisions).values()) == {"dropped_covered"}
    for decision in decisions:  # each drop is independently justified by the matched table candidate's own levels
        population, site, maturity = _DESCRIPTION.match(decision["description"]).groups()
        matched = {d.dimension: d.level for d in by_id[decision["matched_table_candidate"]].dimensions}
        assert orchestrator._normalize_for_matching(matched["treatment"]) in {
            orchestrator._normalize_for_matching(population), orchestrator._normalize_for_matching(maturity)}
        assert orchestrator._normalize_for_matching(matched["site"]) in orchestrator._normalize_for_matching(site)


def test_real_daren_population_maturity_site_never_become_a_treatment_identity():
    """Decision Q1: the live Step B classification of Table 6 declares crop/time/site only. It yields no
    Treatment candidate, and free-form candidates that also declare crop/time/site are not Treatments."""
    table = _real_declared_table()
    assert {f.dimension for f in table.factors} == {"crop", "time", "site"}
    table_candidates, covered = orchestrator._table_classifications_to_treatment_candidates([table], {"site_id": SITE_POOL})
    assert table_candidates == [] and covered == set(table.table_anchors)  # covered, and yields none
    freeform = [_declare(c, "as_crop_time_site") for c in _real_freeform()]
    kept, decisions = _dedup(freeform, table_candidates)
    assert kept == [] and set(_decisions(decisions).values()) == {"dropped_no_treatment_dimension"}


def test_real_daren_a_population_mis_declared_as_a_treatment_is_reconciled_to_crop_and_dropped():
    """A model that files the population under `treatment` is corrected when the level is EXACTLY a ready
    Crop record of this run (the same rule `_reconcile_factor_dimensions` applies to table factors)."""
    pools = {"crop": [{"slug": "trailblazer", "name": "Trailblazer"}, {"slug": "pathfinder", "name": "Pathfinder"}], "site": SITE_POOL}
    trailblazer = next(c for c in _real_freeform() if c.description.startswith("Trailblazer population at Ames, IA in the vegetative"))
    population, site, maturity = _DESCRIPTION.match(trailblazer.description).groups()
    misdeclared = trailblazer.model_copy(update={"dimensions": [
        _dim("Entry", "treatment", population), _dim("Stage", "time", maturity), _dim("Location", "site", site)]})
    assert [(d.name, d.dimension) for d in orchestrator._reconcile_candidate_dimensions(misdeclared, pools)] == [
        ("Entry", "crop"), ("Stage", "time"), ("Location", "site")]
    kept, decisions = _dedup([misdeclared], [], dimension_pools=pools)
    assert kept == [] and decisions[0]["decision"] == "dropped_no_treatment_dimension"
    # without the exact Crop record the declaration is taken as given -- never guessed
    kept, decisions = _dedup([misdeclared], [], dimension_pools={"crop": [], "site": SITE_POOL})
    assert kept == [misdeclared] and decisions[0]["decision"] == "kept_new"


# --------------------------------------------------------------------- #
# SYNTHETIC rules
# --------------------------------------------------------------------- #

TABLE_A = _candidate("mustard_ames", _dim("Cover crop", "treatment", "Mustard"), _dim("Site", "site", "Ames"), anchors=("b:0069",))
TABLE_B = _candidate("fallow_ames", _dim("Cover crop", "treatment", "Fallow"), _dim("Site", "site", "Ames"), anchors=("b:0069",))


def test_synthetic_A_a_free_form_candidate_identical_to_a_table_candidate_is_dropped_as_covered():
    free = _candidate("f1", _dim("Cover crop", "treatment", "Mustard"), _dim("Site", "site", "Ames, IA"), anchors=("b:0200",))
    kept, decisions = _dedup([free], [TABLE_A, TABLE_B])
    assert kept == [] and decisions[0]["decision"] == "dropped_covered" and decisions[0]["matched_table_candidate"] == "mustard_ames"


def test_synthetic_B_other_factor_names_other_order_and_other_spelling_are_the_same_identity():
    free = _candidate("f1", _dim("Site", "site", "Ames"), _dim("Winter crop", "treatment", "mustard"))  # order + key name + case differ
    assert _dedup([free], [TABLE_A])[1][0]["decision"] == "dropped_covered"
    two = _candidate("t", _dim("Tillage", "treatment", "No-till"), _dim("Cover", "treatment", "Mustard"), _dim("Site", "site", "Ames"))
    renamed = _candidate("f", _dim("Practice", "treatment", "no till"), _dim("Crop", "treatment", "MUSTARD"), _dim("Location", "site", "Ames, IA"))
    assert _dedup([renamed], [two])[1][0]["decision"] == "dropped_covered"


def test_synthetic_C_a_free_form_candidate_with_a_genuinely_new_level_or_dimension_survives_untouched():
    new_level = _candidate("nl", _dim("Cover crop", "treatment", "Rye"), _dim("Site", "site", "Ames"), anchors=("b:0300",))
    extra_dim = _candidate("xd", _dim("Cover crop", "treatment", "Mustard"), _dim("Irrigation", "treatment", "Drip"), _dim("Site", "site", "Ames"))
    narrative = _candidate("nar", _dim("Nitrogen rate", "treatment", "100 kg N/ha"), anchors=("b:0400",))
    kept, decisions = _dedup([new_level, extra_dim, narrative], [TABLE_A, TABLE_B])
    assert kept == [new_level, extra_dim, narrative]  # the very same objects: dedup never edits a survivor
    assert set(_decisions(decisions).values()) == {"kept_new"}


def test_synthetic_D_an_ambiguous_site_is_kept_and_flagged_not_merged():
    both = SITE_POOL + [{"slug": "ames_farm", "name": "Ames Farm", "record_id": "p_site_ames_farm"}]
    table = _candidate("t", _dim("Cover", "treatment", "Mustard"), _dim("Site", "site", "Ames, IA"))
    tie = _candidate("f", _dim("Cover", "treatment", "Mustard"), _dim("Site", "site", "Ames"))  # "Ames" fits two ready Sites
    kept, decisions = _dedup([tie], [table], pool=both)
    assert kept == [tie] and decisions[0]["decision"] == "kept_unresolved" and "ambiguous" in decisions[0]["reason"]


def test_synthetic_E_an_unresolved_site_is_never_merged_because_the_names_look_similar():
    table = _candidate("t", _dim("Cover", "treatment", "Mustard"), _dim("Site", "site", "Ames, IA"))
    lookalike = _candidate("f", _dim("Cover", "treatment", "Mustard"), _dim("Site", "site", "Ames Research Farm"))
    kept, decisions = _dedup([lookalike], [table])
    assert kept == [lookalike] and decisions[0]["decision"] == "kept_unresolved"
    # and with no site pool at all, two different sites in play are not treated as one
    kept, decisions = _dedup([lookalike], [table], pool=None)
    assert kept == [lookalike] and decisions[0]["decision"] == "kept_unresolved"


def test_synthetic_F_the_same_level_under_a_different_dimension_or_factor_never_merges():
    table_fallow = _candidate("t", _dim("Cover", "treatment", "Fallow"), _dim("Site", "site", "Ames"))
    as_time = _candidate("a", _dim("Phase", "time", "Fallow"), _dim("Site", "site", "Ames"))       # 'Fallow' as a time is no Treatment
    as_crop = _candidate("b", _dim("Crop", "crop", "Fallow"), _dim("Site", "site", "Ames"))
    kept, decisions = _dedup([as_time, as_crop], [table_fallow])
    assert kept == [] and set(_decisions(decisions).values()) == {"dropped_no_treatment_dimension"}  # not "covered"
    # two treatment factors that swap their levels are different conditions, not one
    table = _candidate("t2", _dim("Cover", "treatment", "none"), _dim("Tillage", "treatment", "till"), _dim("Site", "site", "Ames"))
    swapped = _candidate("s", _dim("Cover", "treatment", "till"), _dim("Tillage", "treatment", "none"), _dim("Site", "site", "Ames"))
    kept, decisions = _dedup([swapped], [table])
    assert kept == [swapped] and decisions[0]["decision"] == "kept_unresolved"
    # and one factor whose two levels coincide keeps its names in the identity
    same_level = _candidate("sl", _dim("Cover", "treatment", "none"), _dim("Tillage", "treatment", "none"))
    other_names = _candidate("on", _dim("A", "treatment", "none"), _dim("B", "treatment", "none"))
    assert _dedup([other_names], [same_level])[1][0]["decision"] == "kept_new"


def test_synthetic_a_candidate_with_no_declared_dimensions_is_kept_and_flagged():
    bare = EnumerationCandidate(candidate_id="bare", description="Mustard at Ames", anchors=["b:0001"])
    kept, decisions = _dedup([bare], [TABLE_A])
    assert kept == [bare] and decisions[0]["decision"] == "kept_unresolved"


def test_synthetic_a_candidate_declaring_only_crop_time_site_is_not_a_treatment_even_next_to_treatment_tables():
    pop = _candidate("p", _dim("Population", "crop", "Trailblazer"), _dim("Site", "site", "Ames"))
    kept, decisions = _dedup([pop], [TABLE_A, TABLE_B])
    assert kept == [] and decisions[0]["decision"] == "dropped_no_treatment_dimension"


def test_synthetic_decisions_never_depend_on_how_many_candidates_there_are():
    free = [_candidate(f"f{i}", _dim("Cover", "treatment", "Mustard"), _dim("Site", "site", "Ames")) for i in range(5)]
    kept, decisions = _dedup(free, [TABLE_A])
    assert kept == [] and {d["decision"] for d in decisions} == {"dropped_covered"}  # every equal candidate, whatever the total
    assert _dedup(free[:1], [TABLE_A])[0] == []


# --------------------------------------------------------------------- #
# grounding
# --------------------------------------------------------------------- #


def test_dedup_never_edits_a_survivor_or_a_table_candidate_and_transfers_nothing():
    table = TABLE_A.model_copy(update={"known_value": "12±1", "anchors": ["b:0069"]})
    covered = _candidate("cov", _dim("Cover crop", "treatment", "Mustard"), _dim("Site", "site", "Ames"), anchors=("b:0999",))
    new = _candidate("new", _dim("Cover crop", "treatment", "Rye"), anchors=("b:0500",))
    before = (table.model_dump(), new.model_dump())
    kept, _ = _dedup([covered, new], [table])
    assert kept == [new] and (table.model_dump(), new.model_dump()) == before
    assert table.anchors == ["b:0069"] and table.known_value == "12±1"  # the dropped candidate's anchor was not merged in
    assert new.known_value is None  # and a table's known value was not copied onto a survivor


def test_declared_levels_must_be_supported_by_the_candidates_own_description_or_cited_blocks():
    blocks = {"b:0001": "Plots were sown to mustard in the fall.", "b:0002": "Unrelated text."}
    ok = _candidate("ok", _dim("Cover", "treatment", "mustard"), anchors=("b:0001",), description="Mustard cover")
    invented = _candidate("bad", _dim("Cover", "treatment", "Fallow"), anchors=("b:0002",), description="Something else")
    empty = EnumerationCandidate(candidate_id="none", description="x", anchors=["b:0001"])
    errors, bad = orchestrator._candidate_dimension_errors([ok, invented, empty], blocks)
    assert bad == {"bad", "none"} and len(errors) == 2
    assert "appears neither in its description nor in the blocks it cites" in errors[0]["message"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    (papers / "syn").mkdir(parents=True)
    (papers / "syn" / "content.md").write_text("Plots were sown to mustard or left fallow.\n⟦b:0001⟧\n\nOther text.\n⟦b:0002⟧\n", encoding="utf-8")
    (papers / "syn" / "provenance.json").write_text(json.dumps({
        "b:0001": {"block_type": "Text", "page_id": "page_1", "section_path": []},
        "b:0002": {"block_type": "Text", "page_id": "page_1", "section_path": []}}), encoding="utf-8")
    monkeypatch.setenv("IR_PAPERS_ROOT", str(papers))
    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path / "runs"))
    return tmp_path


def _enumeration_invoke(payloads):
    calls = []
    it = iter(payloads)

    def invoke(agent, model, prompt, timeout=300):
        calls.append(prompt)
        payload = next(it)
        return orchestrator.AgentInvocation(agent=agent, model=model, prompt=prompt, returncode=0, stdout="{}", stderr="",
                                            final_text=json.dumps(payload), parsed_json=payload, parse_error=None)
    return invoke, calls


def _cand_payload(cid, level, anchors=("b:0001",), dims=True):
    c = {"candidate_id": cid, "description": f"{level} plots", "anchors": list(anchors), "linked_candidates": {}}
    if dims:
        c["dimensions"] = [{"name": "Cover", "dimension": "treatment", "level": level}]
    return c


def test_missing_dimensions_are_retried_and_the_retry_message_says_what_to_declare(env):
    invoke, calls = _enumeration_invoke([
        {"entity_type": "Treatment", "candidates": [_cand_payload("m", "mustard", dims=False)]},
        {"entity_type": "Treatment", "candidates": [_cand_payload("m", "mustard")]},
    ])
    candidates, error = orchestrator.run_enumeration(
        run_id="r", paper_id="syn", entity_type="Treatment", model="m", invoke=invoke, declare_dimensions=True,
    )
    assert error is None and len(calls) == 2 and candidates[0].dimensions[0].level == "mustard"
    assert "declares no dimensions" in calls[1]


def test_on_the_final_attempt_invalid_dimensions_are_cleared_not_the_candidate_lost(env):
    bad = {"entity_type": "Treatment", "candidates": [_cand_payload("m", "irrigated")]}  # 'irrigated' is not in text or description... it is in the description
    bad["candidates"][0]["description"] = "Mustard plots"  # so the declared level is unsupported
    invoke, calls = _enumeration_invoke([bad] * orchestrator.MAX_ENUMERATION_ATTEMPTS)
    candidates, error = orchestrator.run_enumeration(
        run_id="r", paper_id="syn", entity_type="Treatment", model="m", invoke=invoke, declare_dimensions=True,
    )
    assert error is None and len(calls) == orchestrator.MAX_ENUMERATION_ATTEMPTS
    assert [c.candidate_id for c in candidates] == ["m"] and candidates[0].dimensions == []  # kept, identity unresolved


def test_a_nonexistent_anchor_is_still_rejected_when_dimensions_are_required(env):
    invoke, _ = _enumeration_invoke([{"entity_type": "Treatment", "candidates": [_cand_payload("m", "mustard", anchors=("b:9999",))]}]
                                    * orchestrator.MAX_ENUMERATION_ATTEMPTS)
    candidates, error = orchestrator.run_enumeration(
        run_id="r", paper_id="syn", entity_type="Treatment", model="m", invoke=invoke, declare_dimensions=True,
    )
    assert candidates == [] and "invalid anchors" in error  # dimensions never substitute for grounding


def test_the_enumeration_prompt_is_unchanged_without_dimensions_and_carries_the_block_with_them():
    base = orchestrator._enumeration_prompt("p", "Treatment")
    assert orchestrator._enumeration_prompt("p", "Treatment", declare_dimensions=False, covered_conditions=None) == base
    assert "dimensions" not in base
    with_block = orchestrator._enumeration_prompt("p", "Treatment", covered_conditions=["Experimental condition: Cover=Mustard"], declare_dimensions=True)
    assert with_block.startswith(base) and "Experimental condition: Cover=Mustard" in with_block
    assert "cultivar/population is 'crop'" in with_block and "Something with no treatment-dimension level is not a Treatment" in with_block
    big = orchestrator._enumeration_prompt("p", "Treatment", covered_conditions=[f"c{i}" for i in range(100)], declare_dimensions=True)
    assert "... and 60 more" in big


# --------------------------------------------------------------------- #
# the pipeline path (_run_multi_record_entity)
# --------------------------------------------------------------------- #


@pytest.fixture()
def run_env(env, monkeypatch):
    monkeypatch.setattr(orchestrator, "_resolve_known_refs", lambda entity_type, records: ({}, None))
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {"site_id": SITE_POOL})
    monkeypatch.setattr(orchestrator, "_apply_candidate_links", lambda paper_id, entity_type, this_run_records, known_refs, candidate: known_refs)
    processed = []

    def fake_run_record(*, entity_type, record_id, extraction_context=None, **kwargs):
        processed.append(record_id)
        return orchestrator.RecordResult(status="ready", entity_type=entity_type, record_id=record_id, detail={"payload": {}, "ai_validation": None})

    monkeypatch.setattr(orchestrator, "run_record", fake_run_record)
    return processed


def _run(run_id="run1", entity="Treatment"):
    return orchestrator._run_multi_record_entity(
        run_id=run_id, paper_id="syn", entity_type=entity, model="m", client=None,
        invoke=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call expected")), enable_ai_validation=False, this_run_records={},
    )


def _treatment_table() -> TableClassification:
    return TableClassification(
        table_role="treatment_response", table_anchors=["b:0001"],
        factors=[TableFactor(name="Cover crop", dimension="treatment", encoding="columns"), TableFactor(name="Variable", dimension="variable", encoding="rows")],
        value_columns=[TableValueColumn(value_column_id="f", variable_name_hint="v", factor_levels={"Cover crop": "Fallow"}),
                       TableValueColumn(value_column_id="m", variable_name_hint="v", factor_levels={"Cover crop": "Mustard"})],
        row_groups=[TableRowGroup(row_group_id="r", factor_values={"Variable": "biomass"}, source_table_anchor="b:0001", cells={"f": "1", "m": "2"})],
    )


def test_the_pipeline_drops_a_covered_free_form_treatment_keeps_a_new_one_and_logs_every_decision(run_env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0001": _treatment_table()})
    seen = {}

    def fake_enumeration(**kwargs):
        seen.update(kwargs)
        return [
            _candidate("dup", _dim("Winter treatment", "treatment", "mustard"), anchors=("b:0002",), description="Mustard"),
            _candidate("new", _dim("Nitrogen", "treatment", "100 kg N/ha"), anchors=("b:0002",), description="Nitrogen 100 kg N/ha"),
        ], None

    monkeypatch.setattr(orchestrator, "run_enumeration", fake_enumeration)
    infos = _run()
    assert seen["declare_dimensions"] is True and len(seen["covered_conditions"]) == 2  # the free-form pass is told what is covered
    assert sorted(run_env) == ["syn_treatment_fallow", "syn_treatment_mustard", "syn_treatment_new"]
    log = json.loads((run_store.record_dir("run1", "Treatment__enumeration") / "freeform_dedup" / "attempt1.json").read_text())
    assert log["counts"] == {"dropped_covered": 1, "kept_new": 1}
    assert {d["candidate_id"]: d["decision"] for d in log["decisions"]} == {"dup": "dropped_covered", "new": "kept_new"}
    assert len(infos) == 3


def test_synthetic_G_a_paper_with_no_applicable_table_keeps_the_existing_free_form_behavior(run_env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {})
    seen = {}

    def fake_enumeration(**kwargs):
        seen.update(kwargs)
        return [EnumerationCandidate(candidate_id="a", description="Mustard", anchors=["b:0001"]),
                _candidate("b", _dim("Population", "crop", "Trailblazer"), anchors=("b:0001",))], None

    monkeypatch.setattr(orchestrator, "run_enumeration", fake_enumeration)
    _run()
    assert seen["declare_dimensions"] is False and seen["covered_conditions"] is None and seen["excluded_table_anchors"] is None
    assert sorted(run_env) == ["syn_treatment_a", "syn_treatment_b"]  # nothing dropped, not even the crop-only one
    assert not (run_store.record_dir("run1", "Treatment__enumeration") / "freeform_dedup").exists()


def test_synthetic_H_when_table_classification_fails_the_free_form_fallback_still_runs(run_env, monkeypatch):
    """Step B produced nothing for the table (a provider failure): free-form must not be suppressed merely
    because a table exists -- no dimensions are demanded, no anchors excluded, nothing is dropped."""
    monkeypatch.setattr(orchestrator.content_reader, "list_tables", lambda *a, **k: {"found": True, "tables": [
        {"table_anchor": "b:0001", "page": "page_1", "section_path": [], "continuation_of": None}]})
    monkeypatch.setattr(orchestrator, "run_table_classification",
                        lambda **k: (None, "attempt 3: no final assistant text found in agent output"))
    seen = {}

    def fake_enumeration(**kwargs):
        seen.update(kwargs)
        return [_candidate("m", _dim("Cover", "treatment", "mustard"), description="Mustard plots"),
                EnumerationCandidate(candidate_id="bare", description="Fallow plots", anchors=["b:0001"])], None

    monkeypatch.setattr(orchestrator, "run_enumeration", fake_enumeration)
    infos = _run()
    assert seen["declare_dimensions"] is False and seen["excluded_table_anchors"] is None and seen["covered_conditions"] is None
    assert sorted(run_env) == ["syn_treatment_bare", "syn_treatment_m"] and len(infos) == 2


def test_daren_style_crop_time_site_tables_yield_no_treatment_and_no_free_form_treatment_survives(run_env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0607": _real_declared_table()})
    free = [_declare(c, "as_crop_time_site") for c in _real_freeform()]
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: (free, None))
    assert _run() == [] and run_env == []
    log = json.loads((run_store.record_dir("run1", "Treatment__enumeration") / "freeform_dedup" / "attempt1.json").read_text())
    assert log["counts"] == {"dropped_no_treatment_dimension": len(free)}


def test_other_entity_types_keep_the_anchor_and_link_based_drops(run_env, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_table_enumeration", lambda **k: ([_candidate("t", anchors=("b:0001",))], {"b:0001"}))
    seen = {}

    def fake_enumeration(**kwargs):
        seen.update(kwargs)
        return [EnumerationCandidate(candidate_id="covered", description="d", anchors=["b:0001"])], None

    monkeypatch.setattr(orchestrator, "run_enumeration", fake_enumeration)
    _run(entity="Observation")
    assert seen["declare_dimensions"] is False and run_env == ["syn_observation_t"]  # Observation: covered-anchor drop as before


def test_single_site_paper_a_free_form_candidate_naming_the_site_is_still_covered_by_the_table_candidate(run_env, monkeypatch):
    """Regression (found in the Felipe live validation): with ONE ready Site the site is implicit, so a free-form
    candidate that names it must compare equal to an identical table candidate that carries no site. The wiring
    used to hand the comparison a one-entry site pool, which made only the free-form side carry a site."""
    monkeypatch.setattr(orchestrator, "_multi_record_link_pools", lambda *a, **k: {})  # one Site -> no site_id link pool
    monkeypatch.setattr(orchestrator, "run_table_classification_pass", lambda **k: {"b_0001": _treatment_table()})
    monkeypatch.setattr(orchestrator, "run_enumeration", lambda **k: ([
        _candidate("dup", _dim("Winter treatment", "treatment", "mustard"), _dim("Farm", "site", "Rominger Brothers Farms"),
                   anchors=("b:0002",), description="Mustard at Rominger Brothers Farms"),
    ], None))
    site = {"entity_type": "Site", "record_id": "syn_site_rominger", "status": "ready",
            "detail": {"payload": {"name": {"value": "Rominger Brothers Farms"}}}}
    orchestrator._run_multi_record_entity(
        run_id="run1", paper_id="syn", entity_type="Treatment", model="m", client=None,
        invoke=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call expected")),
        enable_ai_validation=False, this_run_records={"Site": [site]},
    )
    log = json.loads((run_store.record_dir("run1", "Treatment__enumeration") / "freeform_dedup" / "attempt1.json").read_text())
    assert log["counts"] == {"dropped_covered": 1}
    assert sorted(run_env) == ["syn_treatment_fallow", "syn_treatment_mustard"]
