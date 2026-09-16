from __future__ import annotations

import marker_pipeline
from pipeline import results_store


def _write_result(results_root, paper_id: str, entity_type: str, status: str):
    record_id = f"{paper_id}_{entity_type.lower()}_1"
    data = {"paper_id": paper_id, "entity_type": entity_type, "record_id": record_id, "status": status}
    if entity_type in results_store.MULTI_RECORD_ENTITY_TYPES:
        results_store.save_multi_entity_results(paper_id, entity_type, [data])
    else:
        results_store.save_entity_result(paper_id, entity_type, data)


def test_no_results_needs_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    assert marker_pipeline._needs_extraction("SomePaper") is True


def test_only_error_results_needs_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    _write_result(tmp_path, "SomePaper", "Citation", "error")
    _write_result(tmp_path, "SomePaper", "Site", "error")
    assert marker_pipeline._needs_extraction("SomePaper") is True


def test_only_blocked_results_needs_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    _write_result(tmp_path, "SomePaper", "Study", "blocked")
    _write_result(tmp_path, "SomePaper", "Treatment", "blocked")
    assert marker_pipeline._needs_extraction("SomePaper") is True


def test_genuinely_successful_extraction_does_not_need_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    _write_result(tmp_path, "SomePaper", "Citation", "ready")
    _write_result(tmp_path, "SomePaper", "Site", "unresolved")
    _write_result(tmp_path, "SomePaper", "TreatmentPair", "blocked")
    assert marker_pipeline._needs_extraction("SomePaper") is False


def test_mixed_successful_and_error_results_needs_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_RESULTS_ROOT", str(tmp_path))
    _write_result(tmp_path, "SomePaper", "Citation", "ready")
    _write_result(tmp_path, "SomePaper", "Site", "error")
    assert marker_pipeline._needs_extraction("SomePaper") is True


def test_run_marker_for_paper_errors_cleanly_when_no_pdf(tmp_path, monkeypatch):
    import sage_paths

    monkeypatch.setattr(sage_paths, "PDF_DIR", tmp_path / "docproc" / "paper")
    (tmp_path / "docproc" / "paper").mkdir(parents=True)

    result = marker_pipeline.run_marker_for_paper("never_uploaded")

    assert result["ok"] is False
    assert "no PDF on disk" in result["error"]


def test_run_marker_for_paper_never_passes_skip_existing(tmp_path, monkeypatch):
    import sage_paths

    pdf_dir = tmp_path / "docproc" / "paper"
    marker_dir = tmp_path / "docproc" / "marker_json"
    pdf_dir.mkdir(parents=True)
    (pdf_dir / "pecan.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr(sage_paths, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(sage_paths, "MARKER_JSON_DIR", marker_dir)

    captured_cmd = {}

    class _FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeCompletedProcess()

    monkeypatch.setattr(marker_pipeline.subprocess, "run", fake_run)

    result = marker_pipeline.run_marker_for_paper("pecan")

    assert result["ok"] is True
    assert "--skip_existing" not in captured_cmd["cmd"]


def test_run_conversion_for_paper_errors_cleanly_when_no_marker_output(tmp_path, monkeypatch):
    import sage_paths

    monkeypatch.setattr(sage_paths, "MARKER_JSON_DIR", tmp_path / "docproc" / "marker_json")
    (tmp_path / "docproc" / "marker_json").mkdir(parents=True)

    result = marker_pipeline.run_conversion_for_paper("never_converted")

    assert result["ok"] is False
    assert "no Marker output found" in result["error"]


def test_run_conversion_for_paper_is_unaffected_by_an_unrelated_failing_paper(tmp_path, monkeypatch):
    import sage_paths
    import prepare_papers

    marker_dir = tmp_path / "docproc" / "marker_json"
    paper_dir = tmp_path / "paper"
    monkeypatch.setattr(sage_paths, "MARKER_JSON_DIR", marker_dir)
    monkeypatch.setattr(sage_paths, "PAPER_DIR", paper_dir)
    (marker_dir / "good_paper").mkdir(parents=True)
    (marker_dir / "unrelated_bad_paper").mkdir(parents=True)

    seen_marker_roots = []

    def fake_prepare_papers(marker_output_dir, output_dir):
        seen_marker_roots.append(sorted(p.name for p in marker_output_dir.iterdir()))
        paper_id = sorted(p.name for p in marker_output_dir.iterdir())[0]
        (output_dir / paper_id).mkdir(parents=True, exist_ok=True)
        (output_dir / paper_id / "content.md").write_text("scoped content")
        return {
            "adapter_results": [{"paper_id": paper_id, "marker_json_resolved": True}],
            "adapter_failures": [],
            "qc": {"papers_found": 1, "papers_passed": 1, "papers_failed": 0, "per_paper": []},
        }

    monkeypatch.setattr(prepare_papers, "prepare_papers", fake_prepare_papers)

    result = marker_pipeline.run_conversion_for_paper("good_paper")

    assert result["ok"] is True
    assert seen_marker_roots == [["good_paper"]]
    assert (paper_dir / "good_paper" / "content.md").read_text() == "scoped content"



def test_poll_new_completions_reports_each_record_once(tmp_path, monkeypatch):
    from pipeline import run_store

    monkeypatch.setenv("IR_RUNS_ROOT", str(tmp_path))
    run_store.save_final("run1", "Observation__obs_a", {"status": "ready"})
    run_store.save_final("run1", "Observation__enumeration", {"status": "n/a"})

    seen = set()
    first_pass = marker_pipeline._poll_new_completions("run1", seen)
    assert any("obs_a" in m and "ready" in m for m in first_pass)
    assert any("enumeration complete" in m for m in first_pass)

    # A record with no final.json yet must not be reported.
    run_store.save_stage_attempt("run1", "Observation__obs_b", "extraction", 1, {})
    second_pass = marker_pipeline._poll_new_completions("run1", seen)
    assert second_pass == []  # obs_b has no final.json; obs_a/enumeration already seen

    # Once obs_b actually finishes, it's reported exactly once.
    run_store.save_final("run1", "Observation__obs_b", {"status": "unresolved"})
    third_pass = marker_pipeline._poll_new_completions("run1", seen)
    assert any("obs_b" in m and "unresolved" in m for m in third_pass)
    assert marker_pipeline._poll_new_completions("run1", seen) == []



def test_classify_run_outcome_success_when_nothing_errored():
    records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Site": [{"status": "ready", "record_id": "p_site_a"}],
        "TreatmentPair": [{"status": "blocked", "record_id": "p_treatmentpair"}],
    }
    outcome, errors = marker_pipeline._classify_run_outcome(records)
    assert outcome == "success"
    assert errors == []


def test_classify_run_outcome_names_every_error_record_not_just_a_count():
    records = {
        "Citation": {"status": "ready", "record_id": "p"},
        "Variable": [
            {"status": "ready", "record_id": "p_variable_a"},
            {"status": "error", "record_id": "p_variable_b"},
        ],
        "Treatment": [{"status": "error", "record_id": "p_treatment_c"}],
    }
    outcome, errors = marker_pipeline._classify_run_outcome(records)
    assert outcome == "completed_with_errors"
    assert set(errors) == {("Variable", "p_variable_b"), ("Treatment", "p_treatment_c")}


def test_run_extraction_with_ui_progress_maps_success_to_done(monkeypatch):
    def fake_gen(paper_id, model):
        yield {"ok": True, "step": "extraction", "run_id": "r1", "statuses": {}, "run_outcome": "success", "error_records": []}

    monkeypatch.setattr(marker_pipeline, "_run_extraction_for_paper", fake_gen)
    events = list(marker_pipeline._run_extraction_with_ui_progress("SomePaper", "test-model"))
    assert len(events) == 1
    assert events[0]["status"] == "done"


def test_run_extraction_with_ui_progress_maps_isolated_errors_to_done_with_errors_not_error(monkeypatch):
    def fake_gen(paper_id, model):
        yield {
            "ok": False, "step": "extraction", "run_id": "r1", "statuses": {},
            "run_outcome": "completed_with_errors",
            "error_records": [("Variable", "p_variable_b"), ("Treatment", "p_treatment_c")],
        }

    monkeypatch.setattr(marker_pipeline, "_run_extraction_for_paper", fake_gen)
    events = list(marker_pipeline._run_extraction_with_ui_progress("SomePaper", "test-model"))
    assert len(events) == 1
    assert events[0]["status"] == "done_with_errors"  # NOT "error" -- the core regression this fixes
    assert "Variable/p_variable_b" in events[0]["message"]
    assert "Treatment/p_treatment_c" in events[0]["message"]


def test_run_extraction_with_ui_progress_still_maps_catastrophic_failure_to_error(monkeypatch):
    def fake_gen(paper_id, model):
        yield {"ok": False, "step": "extraction", "error": "ConnectionError: ir_service unreachable", "run_outcome": "failed"}

    monkeypatch.setattr(marker_pipeline, "_run_extraction_for_paper", fake_gen)
    events = list(marker_pipeline._run_extraction_with_ui_progress("SomePaper", "test-model"))
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert "ir_service unreachable" in events[0]["message"]
