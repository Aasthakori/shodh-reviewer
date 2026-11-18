from conftest import edit_json
from reviewer.effective import derive
from reviewer.loader import load_bundle


def _r3(eff, i=0):
    return next(r for r in eff.candidates[i] if r.id == "R3")


def test_approved_change_applies(bundle_copy):
    eff = derive(load_bundle(bundle_copy("B03_valid_approved_change")))
    assert len(eff.candidates) == 1
    assert _r3(eff).provenance == ["CH-101"]
    reasons = " ".join(i["reason"] for i in eff.ignored)
    assert "proposed" in reasons and "out of revision scope" in reasons


def test_conflict_keeps_both_readings(bundle_copy):
    eff = derive(load_bundle(bundle_copy("B09_incomplete_approval_conflict")))
    assert eff.conflicts and len(eff.candidates) == 2
    assert {_r3(eff, 0).text, _r3(eff, 1).text} == {
        "Otherwise, waiting >= 25 minutes -> escalation queue.", "Otherwise, waiting >= 20 minutes -> escalation queue."}


def test_supersedes_chain_resolves_conflict(bundle_copy):
    d = bundle_copy("B09_incomplete_approval_conflict")
    edit_json(d / "approvals.json", lambda a: a["records"][1].__setitem__("supersedes", ["CH-401"]))
    eff = derive(load_bundle(d))
    assert not eff.conflicts and len(eff.candidates) == 1
    assert _r3(eff).provenance == ["CH-401", "CH-402"]


def test_prose_approval_claim_has_no_authority(bundle_copy):
    d = bundle_copy("B01_valid_base")
    p = d / "policy.md"
    p.write_text(p.read_text() + "\nThe threshold change to 10 minutes was approved by the VP.\n")
    eff = derive(load_bundle(d))
    assert eff.prose_claims and "30" in _r3(eff).text
