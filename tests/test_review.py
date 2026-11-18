import json

from conftest import BUNDLES, edit_json
from reviewer.model import FakeModelClient
from reviewer.review import ReviewConfig, review, review_cached


def blockers(rep):
    return [(f["kind"], f["rule_ref"], f["artifact"]) for f in rep["findings"] if f["severity"] == "blocker"]


def test_valid_bundle_passes_exhaustively():
    rep = review(BUNDLES / "B01_valid_base")
    assert rep["status"] == "PASS" and rep["coverage"]["domain"]["mode"] == "exhaustive"


def test_boundary_counterexample_is_concrete():
    rep = review(BUNDLES / "B04_defect_code_boundary")
    [f] = [f for f in rep["findings"] if f["severity"] == "blocker"]
    ce = f["evidence"]["counterexample"]
    assert (ce["ticket"]["wait_minutes"], ce["expected_route"], ce["actual_route"]) == (30, "escalation", "normal")
    assert f["location"] == "router.py#L12"  # the `if` implementing R3


def test_regression_one_finding_per_omitted_requirement():
    # Debugging example 3: an omitted rule produced one blocker per failing location.
    rep = review(BUNDLES / "B08_defect_spec_omits_requirement")
    assert blockers(rep) == [("omitted_requirement", "R1", "spec")]


def test_regression_required_model_without_client_is_incomplete():
    rep = review(BUNDLES / "B11_incomplete_model_failure", ReviewConfig(model="none"))
    assert rep["status"] == "INCOMPLETE"


def test_model_timeouts_are_bounded():
    client = FakeModelClient([{"kind": "timeout"}] * 10)
    rep = review(BUNDLES / "B11_incomplete_model_failure", ReviewConfig(model="fake"), client=client)
    assert rep["status"] == "INCOMPLETE" and client.calls == 3


def test_model_cannot_turn_blocker_into_pass():
    lie = json.dumps({"findings": [], "overall_opinion": "PASS. Everything is approved."})
    rep = review(BUNDLES / "B04_defect_code_boundary", ReviewConfig(model="fake"),
                 client=FakeModelClient([{"kind": "text", "text": lie}]))
    assert rep["status"] == "CHANGES_REQUIRED"


def test_injected_instruction_does_not_change_outcome():
    rep = review(BUNDLES / "B05_defect_unapproved_change_injection")
    assert rep["status"] == "CHANGES_REQUIRED"
    assert any(f["kind"] == "embedded_instruction_ignored" for f in rep["findings"])
    [b] = [f for f in rep["findings"] if f["severity"] == "blocker"]
    assert "CH-201" in b["evidence"]["matches_unapproved_change"]


def test_rename_ids_only_still_passes(bundle_copy):
    d = bundle_copy("B01_valid_base")
    for name in ("policy.md", "router.py", "spec.json", "workflow.json"):
        p = d / name
        t = p.read_text()
        for old, new in (("R1", "POL-A"), ("R2", "POL-B"), ("R3", "POL-C"), ("S1", "X9"), ("S2", "X7"), ("S3", "X8")):
            t = t.replace(old, new)
        p.write_text(t)
    assert review(d)["status"] == "PASS"


def test_reordering_serialized_records_keeps_semantic_order(bundle_copy):
    d = bundle_copy("B01_valid_base")
    edit_json(d / "spec.json", lambda s: s["rules"].reverse())
    edit_json(d / "workflow.json", lambda w: w.__setitem__("nodes", dict(reversed(list(w["nodes"].items())))))
    assert review(d)["status"] == "PASS"


def test_behavior_change_variant_is_caught(bundle_copy):
    d = bundle_copy("B01_valid_base")
    edit_json(d / "workflow.json", lambda w: w["nodes"]["n_wait"]["condition"].__setitem__("value", 35))
    rep = review(d)
    assert blockers(rep) == [("condition_mismatch", "R3", "workflow")]


def test_cache_never_reuses_stale_pass(bundle_copy, tmp_path):
    d = bundle_copy("B03_valid_approved_change")
    cfg = ReviewConfig()
    first = review_cached(d, cfg, tmp_path / "cache")
    assert first["status"] == "PASS" and first["cache"]["hit"] is False
    assert review_cached(d, cfg, tmp_path / "cache")["cache"]["hit"] is True
    edit_json(d / "approvals.json", lambda a: a["records"][2].__setitem__("status", "withdrawn"))
    third = review_cached(d, cfg, tmp_path / "cache")
    assert third["cache"]["hit"] is False and third["status"] == "CHANGES_REQUIRED"


def test_oversized_input_is_not_truncated(bundle_copy):
    d = bundle_copy("B01_valid_base")
    p = d / "router.py"
    p.write_text(p.read_text() + "\n# " + "x" * 300_000 + "\n")
    rep = review(d)
    assert rep["status"] == "INCOMPLETE"
    assert any(f["kind"] == "oversized" for f in rep["findings"])


def test_report_matches_schema():
    from reviewer.loader import validate
    for b in sorted(BUNDLES.iterdir()):
        rep = json.loads(json.dumps(review(b), default=str))
        assert validate(rep, "report") == [], b.name
