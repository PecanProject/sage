"""Tests for pipeline/corrections_store.py -- the scientist review log,
kept separate from ir-store/ so the original extraction stays immutable."""

import pytest

from corrections_store import (
    VALID_ACTIONS,
    append_correction,
    latest_action_for_field,
    read_all,
)


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setenv("IR_CORRECTIONS_ROOT", str(tmp_path))
    return tmp_path


def test_append_and_read_all_round_trip(isolated_root):
    entry = append_correction("pecan", "Citation", "pecan", action="approve")
    assert entry["action"] == "approve"
    assert entry["field_name"] is None
    all_entries = read_all("pecan")
    assert len(all_entries) == 1
    assert all_entries[0]["record_id"] == "pecan"


def test_rejects_unknown_action(isolated_root):
    with pytest.raises(ValueError):
        append_correction("pecan", "Citation", "pecan", action="delete_everything")


def test_valid_actions_match_review_action_spec():
    assert VALID_ACTIONS == {
        "approve", "correct_value", "relocate_evidence",
        "relink_entity", "confirm_unresolved", "note",
    }


def test_append_never_touches_other_papers(isolated_root):
    append_correction("pecan", "Citation", "pecan", action="approve")
    assert read_all("other-paper") == []


def test_latest_action_for_field_returns_most_recent(isolated_root):
    append_correction("pecan", "Citation", "pecan", action="correct_value", field_name="title", payload={"new_value": "A"})
    append_correction("pecan", "Citation", "pecan", action="correct_value", field_name="title", payload={"new_value": "B"})
    latest = latest_action_for_field("pecan", "Citation", "pecan", "title")
    assert latest["payload"]["new_value"] == "B"


def test_latest_action_for_field_none_when_no_corrections(isolated_root):
    assert latest_action_for_field("pecan", "Citation", "pecan", "title") is None


def test_correction_never_writes_to_ir_store(isolated_root, tmp_path, monkeypatch):
    ir_store_root = tmp_path / "ir-store-should-stay-empty"
    monkeypatch.setenv("IR_STORE_ROOT", str(ir_store_root))
    append_correction("pecan", "Citation", "pecan", action="approve")
    assert not ir_store_root.exists() or list(ir_store_root.iterdir()) == []


def test_relocate_evidence_payload_shape(isolated_root):
    entry = append_correction(
        "pecan", "Site", "pecan_site", action="relocate_evidence", field_name="name",
        payload={"new_locators": [{"kind": "text", "block_anchor": "b:0099"}]},
    )
    assert entry["payload"]["new_locators"][0]["block_anchor"] == "b:0099"


def test_reviewer_field_defaults(isolated_root):
    entry = append_correction("pecan", "Citation", "pecan", action="approve")
    assert entry["reviewer"] == "scientist"
