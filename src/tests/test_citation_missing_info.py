"""Citation: front matter supplied, missing information made explicit (NOT WRITTEN), and a stall told apart from a
provider failure.

Real evidence (run 20260923T040931_c10b6234, Philippe-2007-Six): the paper prints no DOI and no journal name. In every
Citation round the extractor read the first page, went looking for them with tools that do not exist (`find_issues`,
`find`) and `read_section("Journal")` / `("doi")`, and ended its turn with no answer text. That was classified
`provider_empty` and retried with the identical prompt five times (15 calls, ~9 minutes of cooldowns); Citation ended in
error and blocked eight downstream entity types.

Fixtures (real): tests/fixtures/citation_missing/ -- the trimmed front matter of Philippe-2007-Six (no DOI, no journal)
and Kathryn-2020-Winter (DOI and journal written in b:0004), and the five recorded Philippe Citation rounds.
The extractor/converter ANSWERS below are scripted (canned responses), as in every orchestrator test.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline import orchestrator, run_store, store
from test_orchestrator import env, make_invoke_sequence  # noqa: F401  (env is a pytest fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "citation_missing"
PHILIPPE = "Philippe-2007-Six"
KATHRYN = "Kathryn-2020-Winter"
KATHRYN_DOI = "10.1371/journal.pone.0228677"
RECORDED = json.loads((FIXTURES / "philippe_citation_rounds_20260923T040931_c10b6234.json").read_text())


@pytest.fixture(autouse=True)
def _fresh_provider_log():
    """The provider-failure log is process-global and keyed by run id; other modules also use "run1"."""
    orchestrator._PROVIDER_FAILURE_LOG.clear()


@pytest.fixture()
def papers(env):
    for pid in (PHILIPPE, KATHRYN):
        shutil.copytree(FIXTURES / pid, env["papers_root"] / pid)
    return env


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


def _answer(agent: str, parsed: dict) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent=agent, model="test-model", prompt="prompt", returncode=0, stdout="{}", stderr="",
        final_text=json.dumps(parsed), parsed_json=parsed, parse_error=None,
    )


def _stream(tools: list[tuple[str, dict]], *, error: bool = False) -> str:
    """An `opencode run --format json` stream: each tool completes, then the turn ends with no text part."""
    lines = []
    for tool, tool_input in tools:
        lines.append({"type": "step_start", "part": {"type": "step-start"}})
        lines.append({"type": "tool_use", "part": {"type": "tool", "tool": tool, "state": {"status": "completed", "input": tool_input, "output": "..."}}})
        lines.append({"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}})
    lines.append({"type": "step_start", "part": {"type": "step-start"}})
    if error:
        lines.append({"type": "error", "error": {"name": "UnknownError", "data": {"message": "litellm.BadRequestError: Hosted_vllmException"}}})
    else:
        lines.append({"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}})
    return "\n".join(json.dumps(line) for line in lines)


def _no_answer(stdout: str, returncode: int = 0) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(
        agent="extractor", model="test-model", prompt="prompt", returncode=returncode, stdout=stdout, stderr="",
        final_text=None, parsed_json=None, parse_error="no final assistant text found in agent output",
    )


def _recorded(key: str) -> orchestrator.AgentInvocation:
    return orchestrator.AgentInvocation(**RECORDED[key])


SEARCHING = [  # the Philippe pattern: read the first page, then hunt for a journal/DOI that is not written
    ("read_document_start", {"paper_id": PHILIPPE, "max_lines": 100}),
    ("read_section", {"paper_id": PHILIPPE, "section_name": "Journal"}),
    ("invalid", {"tool": "find_issues", "error": "Model tried to call unavailable tool 'find_issues'."}),
    ("read_section", {"paper_id": PHILIPPE, "section_name": "doi"}),
]


def _fact(name, value, excerpt, anchors, notes=None):
    return {"field_name": name, "raw_value": value, "raw_text_excerpt": excerpt, "anchors": anchors, "notes": notes}


def philippe_extraction(**overrides) -> dict:
    facts = [
        _fact("title", "Six-year time course of light-use efficiency, carbon gain and growth of beech saplings",
              "Six-year time course of light-use efficiency, carbon gain and growth of beech saplings", ["b:0003"]),
        _fact("author", "Philippe Balandier", "PHILIPPE BALANDIER", ["b:0004"]),
        _fact("year", "2007", "published online May 1, 2007", ["b:0005"]),
        _fact("journal", None, "", ["b:0003", "b:0004", "b:0005"], "NOT WRITTEN in the paper"),
        _fact("persistent_identifier", None, "", ["b:0003", "b:0004", "b:0005"], "NOT WRITTEN in the paper"),
    ]
    for name, fact in overrides.items():
        facts = [fact if f["field_name"] == name else f for f in facts]
    return {"paper_id": PHILIPPE, "entity_type": "Citation", "record_id": PHILIPPE, "facts": facts}


def _src(paper_id, anchor):
    return {"source_document_id": paper_id, "section_path": [], "locators": [{"kind": "text", "block_anchor": anchor}]}


def philippe_payload(pid: dict | None = None) -> dict:
    return {
        "id": PHILIPPE,
        "title": {"value": "Six-year time course of light-use efficiency, carbon gain and growth of beech saplings",
                  "provenance_label": "EXTRACTED", "source": _src(PHILIPPE, "b:0003")},
        "author": {"value": "Philippe Balandier", "provenance_label": "EXTRACTED", "source": _src(PHILIPPE, "b:0004")},
        "year": {"value": 2007, "provenance_label": "EXTRACTED", "source": _src(PHILIPPE, "b:0005")},
        "persistent_identifier": pid or {
            "value": None, "provenance_label": "UNRESOLVED",
            "unresolved_reason": orchestrator.CITATION_NOT_WRITTEN_REASON, "source": _src(PHILIPPE, "b:0005"),
        },
    }


def kathryn_extraction(*, with_doi: bool = True) -> dict:
    facts = [
        _fact("title", "Winter cover crops increase readily decomposable soil carbon",
              "Winter cover crops increase readily decomposable soil carbon", ["b:0004"]),
        _fact("author", "White KE", "White KE, Brennan EB, Cavigelli MA, Smith RF", ["b:0004"]),
        _fact("year", "2020", "Published: February 6, 2020", ["b:0007"]),
        _fact("journal", "PLoS ONE", "PLoS ONE 15(2): e0228677", ["b:0004"]),
        _fact("persistent_identifier", KATHRYN_DOI if with_doi else None,
              f"https://doi.org/{KATHRYN_DOI}" if with_doi else "", ["b:0004"]),
    ]
    return {"paper_id": KATHRYN, "entity_type": "Citation", "record_id": KATHRYN, "facts": facts}


def kathryn_payload() -> dict:
    return {
        "id": KATHRYN,
        "title": {"value": "Winter cover crops increase readily decomposable soil carbon", "provenance_label": "EXTRACTED", "source": _src(KATHRYN, "b:0004")},
        "author": {"value": "White KE", "provenance_label": "EXTRACTED", "source": _src(KATHRYN, "b:0004")},
        "year": {"value": 2020, "provenance_label": "EXTRACTED", "source": _src(KATHRYN, "b:0007")},
        "persistent_identifier": {"value": KATHRYN_DOI, "provenance_label": "EXTRACTED", "source": _src(KATHRYN, "b:0004")},
    }


def _run(env, paper_id, invoke):
    return orchestrator.run_record(
        run_id="run1", paper_id=paper_id, entity_type="Citation", record_id=paper_id, model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=False,
    )


def _recording(sequence):
    """make_invoke_sequence that also records (agent, prompt) of every call."""
    inner = make_invoke_sequence(sequence)
    calls: list[tuple[str, str]] = []

    def invoke(agent, model, prompt, timeout=300):
        calls.append((agent, prompt))
        return inner(agent, model, prompt, timeout)

    return invoke, calls


def _manifest(paper_id):
    return run_store.load_json(run_store.record_dir("run1", f"Citation__{paper_id}") / "record_manifest.json")


def _attempt(paper_id, n):
    return run_store.load_json(run_store.record_dir("run1", f"Citation__{paper_id}") / "extraction" / f"attempt{n}.json")


def _stored_payload(paper_id):
    return [e for e in store.read_all(paper_id) if e["entity_type"] == "Citation"][-1]


# --------------------------------------------------------------------- #
# front matter and the DOI check (deterministic)
# --------------------------------------------------------------------- #


def test_front_matter_is_the_first_blocks_up_to_the_body_heading(papers):
    front, hits = orchestrator._citation_front_matter(PHILIPPE)
    assert [a for a, _ in front] == ["b:0003", "b:0004", "b:0005", "b:0006", "b:0007"]      # stops at "#### Introduction"
    assert front[0][1].startswith("# Six-year time course") and hits == []                  # the title heading does not stop it


def test_the_doi_check_finds_a_first_page_doi_but_never_a_reference_list_doi(papers):
    assert orchestrator._citation_front_matter(KATHRYN)[1] == [("b:0004", KATHRYN_DOI)]
    # a DOI on a later page (the reference list: other papers' DOIs) is never taken as this paper's
    prov_path = papers["papers_root"] / PHILIPPE / "provenance.json"
    prov = json.loads(prov_path.read_text())
    content = (papers["papers_root"] / PHILIPPE / "content.md").read_text()
    (papers["papers_root"] / PHILIPPE / "content.md").write_text(content + "\nSmith J (2001) doi:10.1111/j.1234.x\n⟦b:0009⟧\n")
    prov["b:0009"] = {"block_type": "ListItem", "page_id": "page_23", "section_path": ["References"]}
    prov_path.write_text(json.dumps(prov))
    assert orchestrator._citation_front_matter(PHILIPPE)[1] == []


def test_the_citation_prompt_carries_the_front_matter_and_the_not_written_rules(papers):
    note = orchestrator._citation_extraction_note(PHILIPPE)
    assert "[b:0003] # Six-year time course" in note and "[b:0005] Received June 6, 2006" in note
    assert "no DOI" in note and "persistent_identifier is NOT WRITTEN -- do not search for it" in note
    assert "NOT WRITTEN is not evidence" in note and "at most 3 further reads" in note and "no find or search tool" in note
    kathryn = orchestrator._citation_extraction_note(KATHRYN)
    assert f"'{KATHRYN_DOI}' in block b:0004" in kathryn and "do not search for it" not in kathryn


def test_other_entity_types_prompts_are_unchanged():
    base = orchestrator._extraction_prompt("p", "Site", "p_site")
    assert base == orchestrator._extraction_prompt("p", "Site", "p_site", supplied_context=None, final_answer_nudge=False)
    assert "CITATION FRONT MATTER" not in base and orchestrator.CITATION_FINAL_ANSWER_NUDGE not in base


# --------------------------------------------------------------------- #
# A: DOI and journal absent -> a valid Citation, fields unresolved, no provider failure, no retry
# --------------------------------------------------------------------- #


def test_A_absent_doi_and_journal_give_a_ready_citation_with_them_not_written(papers):
    invoke, calls = _recording([("extractor", _answer("extractor", philippe_extraction())),
                                ("converter", _answer("converter", philippe_payload()))])
    result = _run(papers, PHILIPPE, invoke)

    assert result.status == "ready"
    assert [a for a, _ in calls] == ["extractor", "converter"]                                      # no retry at all
    assert "CITATION FRONT MATTER" in calls[0][1] and "[b:0004] PHILIPPE BALANDIER" in calls[0][1]
    assert [e["field"] for e in result.detail["not_written"]] == ["journal", "persistent_identifier"]
    assert result.detail["not_written"][0]["checked_anchors"] == ["b:0003", "b:0004", "b:0005", "b:0006", "b:0007"]
    attempts = _manifest(PHILIPPE)["attempts"]
    assert attempts["extraction"] == 1 and "provider_failure_rounds" not in attempts and "final_answer_retries" not in attempts
    assert orchestrator.summarize_provider_failures("run1")["total_rounds"] == 0
    # Conversion is told the fields are not written -- as a pipeline determination, not as source text
    assert "orchestrator_not_written" in calls[1][1] and orchestrator.CITATION_NOT_WRITTEN_REASON in calls[1][1]
    # the committed record: title/author/year extracted, the DOI UNRESOLVED with a reason and no value
    stored = _stored_payload(PHILIPPE)["payload"]
    assert stored["year"]["value"] == 2007 and stored["title"]["provenance_label"] == "EXTRACTED"
    pid = stored["persistent_identifier"]
    assert pid["provenance_label"] == "UNRESOLVED" and pid["value"] is None and pid["unresolved_reason"]
    assert "NOT WRITTEN" not in json.dumps(stored)                                                  # never written as a value


def test_A_not_written_is_never_evidence_the_null_facts_do_not_reach_conversion_as_facts(papers):
    invoke, calls = _recording([("extractor", _answer("extractor", philippe_extraction())),
                                ("converter", _answer("converter", philippe_payload()))])
    result = _run(papers, PHILIPPE, invoke)
    # the model's null facts (empty excerpts) are dropped by the existing null/blank-fact rule, logged as before
    assert sorted(d["field_name"] for d in result.detail["dropped_ungrounded_facts"]) == ["journal", "persistent_identifier"]
    raw = json.loads(calls[1][1].split("RAW_EVIDENCE:\n```json\n", 1)[1].split("\n```", 1)[0])
    assert [f["field_name"] for f in raw["facts"]] == ["title", "author", "year"]
    assert [e["field"] for e in raw["orchestrator_not_written"]] == ["journal", "persistent_identifier"]


# --------------------------------------------------------------------- #
# B / C: DOI and journal written -> extracted and grounded normally
# --------------------------------------------------------------------- #


def test_B_C_a_written_doi_and_journal_are_extracted_and_grounded(papers):
    invoke, calls = _recording([("extractor", _answer("extractor", kathryn_extraction())),
                                ("converter", _answer("converter", kathryn_payload()))])
    result = _run(papers, KATHRYN, invoke)
    assert result.status == "ready" and "not_written" not in result.detail
    assert f"'{KATHRYN_DOI}' in block b:0004" in calls[0][1]
    raw = json.loads(calls[1][1].split("RAW_EVIDENCE:\n```json\n", 1)[1].split("\n```", 1)[0])
    journal = next(f for f in raw["facts"] if f["field_name"] == "journal")
    assert journal["raw_value"] == "PLoS ONE" and "orchestrator_not_written" not in raw          # C: journal kept, grounded
    stored = _stored_payload(KATHRYN)["payload"]["persistent_identifier"]
    assert stored["source"]["locators"] == [{"kind": "text", "block_anchor": "b:0004"}]
    assert stored["provenance_label"] == "EXTRACTED" and stored["value"] == KATHRYN_DOI            # B: DOI, grounded


def test_B_a_written_doi_the_extraction_left_out_is_sent_back_never_marked_not_written(papers):
    invoke, calls = _recording([("extractor", _answer("extractor", kathryn_extraction(with_doi=False))),
                                ("extractor", _answer("extractor", kathryn_extraction())),
                                ("converter", _answer("converter", kathryn_payload()))])
    result = _run(papers, KATHRYN, invoke)
    assert result.status == "ready" and "not_written" not in result.detail
    first = _attempt(KATHRYN, 1)
    assert first["validation_errors"][0]["field"] == "persistent_identifier" and "a DOI is written" in first["validation_errors"][0]["message"]
    assert "a DOI is written on the first page" in calls[1][1]                                       # the retry carries the feedback


# --------------------------------------------------------------------- #
# D: the model keeps searching and never answers -> a targeted final-answer retry, not the same prompt
# --------------------------------------------------------------------- #


def test_D_a_stall_gets_a_targeted_retry_and_the_citation_succeeds(papers):
    invoke, calls = _recording([("extractor", _no_answer(_stream(SEARCHING))),
                                ("extractor", _answer("extractor", philippe_extraction())),
                                ("converter", _answer("converter", philippe_payload()))])
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "ready"
    first_prompt, retry_prompt = calls[0][1], calls[1][1]
    assert orchestrator.CITATION_FINAL_ANSWER_NUDGE not in first_prompt
    assert orchestrator.CITATION_FINAL_ANSWER_NUDGE in retry_prompt and retry_prompt != first_prompt
    stall = _attempt(PHILIPPE, 1)
    assert stall["failure_class"] == "no_final_answer" and stall["failure_kind"] == "extraction"
    assert stall["numbered_attempt"] is None and [t["tool"] for t in stall["tool_calls"]] == [
        "read_document_start", "read_section", "invalid:find_issues", "read_section"]
    attempts = _manifest(PHILIPPE)["attempts"]
    assert attempts["final_answer_retries"] == 1 and "provider_failure_rounds" not in attempts
    assert orchestrator.summarize_provider_failures("run1")["total_rounds"] == 0                    # not a provider failure
    assert [e["field"] for e in result.detail["not_written"]] == ["journal", "persistent_identifier"]


def test_D_stalls_are_bounded_and_end_as_the_models_failure_not_the_providers(papers):
    sequence = [("extractor", _no_answer(_stream(SEARCHING)))] * (orchestrator.MAX_CITATION_FINAL_ANSWER_RETRIES + 1)
    invoke, calls = _recording(sequence)
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "error" and len(calls) == orchestrator.MAX_CITATION_FINAL_ANSWER_RETRIES + 1
    assert result.detail["failure_class"] == "no_final_answer"
    assert all(orchestrator.CITATION_FINAL_ANSWER_NUDGE in p for _, p in calls[1:])
    assert orchestrator.summarize_provider_failures("run1")["total_rounds"] == 0


def test_D_a_stall_on_another_entity_type_gets_the_same_handling(env):
    """Extended from Citation to every entity type (Felipe run 20260923T132453_7595c3bf: 12 of 19 no-answer rounds were
    Variable/Coverage/Crop stalls). The classification itself is unchanged; the loop treats the stall as the model's."""
    stall = _no_answer(_stream(SEARCHING))
    assert orchestrator.classify_invocation_failure(stall) == "provider_empty"                       # classification unchanged
    invoke, calls = _recording([("extractor", stall)] * (orchestrator.MAX_FINAL_ANSWER_RETRIES + 1))
    result = orchestrator.run_record(
        run_id="run1", paper_id="orch_test_paper", entity_type="Site", record_id="orch_test_paper_site", model="test-model",
        client=env["client"], invoke=invoke, enable_ai_validation=False,
    )
    assert result.status == "error" and result.detail["failure_class"] == "no_final_answer"
    assert "CITATION FRONT MATTER" not in calls[0][1] and orchestrator._final_answer_nudge("extraction", "Site") in calls[1][1]
    summary = orchestrator.summarize_provider_failures("run1")
    assert summary["total_rounds"] == 0 and summary["model_no_answer"]["total_rounds"] == len(calls)


# --------------------------------------------------------------------- #
# E: invented values are still rejected by grounding
# --------------------------------------------------------------------- #


def test_E_an_invented_doi_in_the_raw_evidence_is_still_rejected(papers):
    invented = _fact("persistent_identifier", "10.1093/treephys/27.6.817", "doi:10.1093/treephys/27.6.817", ["b:0005"])
    bad = philippe_extraction(persistent_identifier=invented)
    invoke, calls = _recording([("extractor", _answer("extractor", bad))] * orchestrator.MAX_EXTRACTION_ATTEMPTS)
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "error"                                                                  # never laundered
    errors = _attempt(PHILIPPE, 1)["validation_errors"]
    assert any("persistent_identifier" in e["message"] and "raw_text_excerpt" in e["field"] for e in errors)
    assert orchestrator.summarize_provider_failures("run1")["total_rounds"] == 0


def test_E_an_invented_doi_from_conversion_is_rejected_by_provenance_validation(papers):
    invented = {"value": "10.1093/treephys/27.6.817", "provenance_label": "EXTRACTED", "source": _src(PHILIPPE, "b:0005")}
    invoke, calls = _recording([("extractor", _answer("extractor", philippe_extraction())),
                                ("converter", _answer("converter", philippe_payload(pid=invented))),
                                ("converter", _answer("converter", philippe_payload()))])
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "ready"
    first = run_store.load_json(run_store.record_dir("run1", f"Citation__{PHILIPPE}") / "conversion_validation" / "attempt1.json")
    assert first["valid"] is False and any(e.get("code") == "provenance_value_mismatch" for e in first["errors"])
    assert _stored_payload(PHILIPPE)["payload"]["persistent_identifier"]["value"] is None


# --------------------------------------------------------------------- #
# F: a genuine provider failure stays a provider failure
# --------------------------------------------------------------------- #


def test_F_an_empty_response_before_any_content_is_a_provider_failure(papers):
    invoke, calls = _recording([("extractor", _no_answer("")),
                                ("extractor", _answer("extractor", philippe_extraction())),
                                ("converter", _answer("converter", philippe_payload()))])
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "ready"
    assert _attempt(PHILIPPE, 1)["failure_class"] == "provider_empty"
    assert orchestrator.CITATION_FINAL_ANSWER_NUDGE not in calls[1][1]                               # same prompt, as before
    assert _manifest(PHILIPPE)["attempts"]["provider_failure_rounds"] == 1


def test_F_a_provider_error_event_after_tool_use_stays_a_provider_failure():
    """The real Philippe round 4: tools ran, then the provider returned a litellm BadRequest -- not a stall."""
    recorded = _recorded("attempt4")
    assert '"type": "error"' in recorded.stdout or '"type":"error"' in recorded.stdout
    assert not orchestrator._ended_without_answer_after_tools(recorded)
    assert orchestrator._provider_failure(recorded) in orchestrator.PROVIDER_FAILURE_CLASSES
    synthetic = _no_answer(_stream(SEARCHING, error=True))
    assert not orchestrator._ended_without_answer_after_tools(synthetic)


# --------------------------------------------------------------------- #
# G: replay of the real Philippe Citation rounds (run 20260923T040931_c10b6234)
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["attempt1", "attempt2", "attempt3", "attempt5"])
def test_G_every_recorded_searching_round_is_a_stall_not_a_provider_failure(key):
    recorded = _recorded(key)
    assert orchestrator.classify_invocation_failure(recorded) == "provider_empty"                   # what it was counted as
    assert orchestrator._ended_without_answer_after_tools(recorded)                                 # what it is


def test_G_replay_the_real_philippe_round_now_yields_a_valid_citation(papers):
    """Before: five provider rounds x 3 identical calls, ~9 min of cooldowns, Citation error, 8 entity types blocked.
    Now: the recorded round is a stall, the targeted retry is answered, Citation is ready."""
    invoke, calls = _recording([("extractor", _recorded("attempt1")),
                                ("extractor", _answer("extractor", philippe_extraction())),
                                ("converter", _answer("converter", philippe_payload()))])
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "ready"
    assert [a for a, _ in calls] == ["extractor", "extractor", "converter"]
    assert orchestrator.CITATION_FINAL_ANSWER_NUDGE in calls[1][1]
    assert orchestrator.summarize_provider_failures("run1")["total_rounds"] == 0
    assert _stored_payload(PHILIPPE)["payload"]["persistent_identifier"]["provenance_label"] == "UNRESOLVED"
    assert [e["field"] for e in result.detail["not_written"]] == ["journal", "persistent_identifier"]


def test_G_replaying_all_five_recorded_rounds_is_bounded_without_touching_the_provider_budget(papers):
    """Even if the model never answered the targeted retry, the old 15-call/9-minute provider spend does not recur:
    the four searching rounds are stalls (bounded), and the one real provider error spends one provider round."""
    sequence = [("extractor", _recorded(k)) for k in ("attempt1", "attempt2", "attempt3", "attempt4", "attempt5")]
    invoke, calls = _recording(sequence)
    result = _run(papers, PHILIPPE, invoke)
    assert result.status == "error" and result.detail["failure_class"] == "no_final_answer"
    assert len(calls) == 5
    summary = orchestrator.summarize_provider_failures("run1")
    assert summary["total_rounds"] == 1 and summary["terminal"] == []
