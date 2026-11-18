"""Evaluation harness.

    python eval/run_eval.py --impl final                      # final implementation
    python eval/run_eval.py --impl checkpoint                 # frozen v0 checkpoint
    python eval/run_eval.py --impl final --ablation no_boundary_points

Each bundle is reviewed in a subprocess against the chosen implementation
with identical inputs and settings (fake model client + per-bundle script).
Labels live in eval/labels/ and are never passed to the reviewer.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from independent import artifact_route  # noqa: E402
from oracle import policy_for  # noqa: E402

BUNDLES = ROOT / "fixtures" / "bundles"
SCRIPTS = ROOT / "fixtures" / "model_scripts"
LABELS = ROOT / "eval" / "labels"
IMPLS = {"final": ROOT, "checkpoint": ROOT / "checkpoints" / "v0_initial"}
TYPE_OF = {"PASS": "valid", "CHANGES_REQUIRED": "defective", "INCOMPLETE": "incomplete"}


def check_labels(labels: list[dict]) -> list[str]:
    """Labels must agree with the independent oracle and with independent
    execution of the artifact; otherwise the label itself is wrong."""
    errors = []
    for lab in labels:
        pol = policy_for(lab["oracle"])
        for f in lab["expected_findings"]:
            w = f["witness"]
            if pol is not None and pol(w["ticket"]) != w["expected_route"]:
                errors.append(f"{lab['bundle']}: oracle disagrees with expected_route {w}")
            act = artifact_route(BUNDLES / lab["bundle"], f["artifact"], w["ticket"])
            if act != w["actual_route"]:
                errors.append(f"{lab['bundle']}: {f['artifact']} actually routes {w['ticket']} to {act}, label says {w['actual_route']}")
    return errors


def run_one(impl_root: Path, bundle: Path, ablation: str, out: Path) -> tuple[dict, float]:
    cmd = [sys.executable, "-m", "reviewer", "review", str(bundle), "--model", "fake", "--out", str(out)]
    script = SCRIPTS / f"{bundle.name}.json"
    if script.exists():
        cmd += ["--model-script", str(script)]
    if ablation == "no_boundary_points":
        cmd.append("--no-boundary-points")
    env = dict(os.environ, PYTHONPATH=str(impl_root))
    t0 = time.time()
    subprocess.run(cmd, cwd=str(impl_root), env=env, capture_output=True, text=True)
    return json.loads(out.read_text()), time.time() - t0


def score(labels: list[dict], reports: dict[str, dict], wall: dict[str, float]) -> dict:
    per_case, unsupported_blockers = [], 0
    dec = {"false_pass": 0, "defective": 0, "false_block": 0, "valid": 0,
           "incomplete_by_type": {"valid": 0, "defective": 0, "incomplete": 0}, "cases_by_type": {"valid": 0, "defective": 0, "incomplete": 0},
           "correct_status": 0, "total": len(labels)}
    fq = {"expected_findings": 0, "matched_defect_and_location": 0, "counterexamples": 0, "counterexamples_verified": 0,
          "unsupported_blockers": 0, "blockers_total": 0}
    res = {"model_calls": 0, "model_retries": 0, "runtime_s_total": 0.0, "wall_s_total": 0.0}
    for lab in labels:
        rep = reports[lab["bundle"]]
        ctype = TYPE_OF[lab["expected_status"]]
        dec["cases_by_type"][ctype] += 1
        if ctype == "defective":
            dec["defective"] += 1
            dec["false_pass"] += rep["status"] == "PASS"
        if ctype == "valid":
            dec["valid"] += 1
            dec["false_block"] += rep["status"] == "CHANGES_REQUIRED"
        if rep["status"] == "INCOMPLETE":
            dec["incomplete_by_type"][ctype] += 1
        dec["correct_status"] += rep["status"] == lab["expected_status"]
        blockers = [f for f in rep["findings"] if f["severity"] == "blocker"]
        fq["blockers_total"] += len(blockers)
        matched = 0
        for ef in lab["expected_findings"]:
            fq["expected_findings"] += 1
            if any(b["artifact"] == ef["artifact"] and b["rule_ref"] == ef["rule_ref"] and b["kind"] in ef["kinds"] for b in blockers):
                matched += 1
        fq["matched_defect_and_location"] += matched
        pol = policy_for(lab["oracle"])
        verified = 0
        for b in blockers:
            if not any(b["artifact"] == ef["artifact"] and b["rule_ref"] == ef["rule_ref"] for ef in lab["expected_findings"]):
                fq["unsupported_blockers"] += 1
            ce = b["evidence"].get("counterexample")
            if ce:
                fq["counterexamples"] += 1
                ok_exp = pol is not None and pol(ce["ticket"]) == ce["expected_route"]
                ok_act = artifact_route(BUNDLES / lab["bundle"], b["artifact"], ce["ticket"]) == ce["actual_route"]
                if ok_exp and ok_act:
                    verified += 1
        fq["counterexamples_verified"] += verified
        m = rep.get("model") or {}
        calls = len(m.get("attempts", []))
        res["model_calls"] += calls
        res["model_retries"] += max(0, calls - 1)
        res["runtime_s_total"] += rep.get("runtime_s", 0)
        res["wall_s_total"] += wall[lab["bundle"]]
        per_case.append({"bundle": lab["bundle"], "split": lab["split"], "expected": lab["expected_status"],
                         "actual": rep["status"], "correct": rep["status"] == lab["expected_status"],
                         "blockers": [(b["kind"], b["rule_ref"], b["artifact"]) for b in blockers],
                         "expected_findings_matched": f"{matched}/{len(lab['expected_findings'])}",
                         "model_calls": calls})
    res = {k: round(v, 4) if isinstance(v, float) else v for k, v in res.items()}
    res["model_client"] = "fake (synthetic, scripted); real-model performance NOT measured"
    return {"decision": dec, "finding_quality": fq, "resources": res, "cases": per_case}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", choices=list(IMPLS), default="final")
    ap.add_argument("--ablation", choices=["none", "no_boundary_points"], default="none")
    ap.add_argument("--split", choices=["all", "dev", "heldout"], default="all")
    ap.add_argument("--name")
    a = ap.parse_args()
    labels = [json.loads(p.read_text()) for p in sorted(LABELS.glob("*.json"))]
    if a.split != "all":
        labels = [l for l in labels if l["split"] == a.split]
    errs = check_labels(labels)
    if errs:
        print("LABEL ERRORS:\n" + "\n".join(errs))
        sys.exit(3)
    name = a.name or f"{a.impl}" + ("" if a.ablation == "none" else f"_ablation_{a.ablation}")
    out_dir = ROOT / "reports" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    reports, wall = {}, {}
    for lab in labels:
        reports[lab["bundle"]], wall[lab["bundle"]] = run_one(IMPLS[a.impl], BUNDLES / lab["bundle"], a.ablation,
                                                               out_dir / f"{lab['bundle']}.json")
    metrics = {"run": name, "impl": a.impl, "ablation": a.ablation, "split": a.split, "labels_checked": len(labels)}
    for split in ("dev", "heldout", "all"):
        subset = [l for l in labels if split == "all" or l["split"] == split]
        if subset:
            metrics[split] = score(subset, reports, wall)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    d, fq = metrics["all"]["decision"], metrics["all"]["finding_quality"]
    print(f"[{name}] correct status {d['correct_status']}/{d['total']} | false pass {d['false_pass']}/{d['defective']} | "
          f"false block {d['false_block']}/{d['valid']} | incomplete by type {d['incomplete_by_type']} | "
          f"defects found {fq['matched_defect_and_location']}/{fq['expected_findings']} | "
          f"counterexamples verified {fq['counterexamples_verified']}/{fq['counterexamples']} | "
          f"unsupported blockers {fq['unsupported_blockers']}")
    for c in metrics["all"]["cases"]:
        flag = "ok " if c["correct"] else "XX "
        print(f"  {flag}{c['split']:8s}{c['bundle']:45s} expected {c['expected']:17s} got {c['actual']}")


if __name__ == "__main__":
    main()
