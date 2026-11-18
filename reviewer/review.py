"""Orchestrates a full review and assembles the JSON report.

Status rules:
  PASS              every effective requirement was compared exhaustively
                    against every declared artifact, no blocker, nothing
                    unreviewed, and every required check succeeded.
  CHANGES_REQUIRED  at least one evidenced blocker (a concrete ticket whose
                    route differs under every admissible reading of the
                    requirements). Unreviewed scope is still disclosed.
  INCOMPLETE        no blocker established, but missing evidence, ambiguity,
                    unsupported content or a failed required check prevents
                    a full review.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .conditions import jsonable, to_text
from .effective import Effective, derive
from .loader import Bundle, load_bundle
from .model import FakeModelClient, ModelClient, review_with_model
from .pycode import suspicious_comments
from .semantic import UNKNOWN, ArtifactModel, build_models, classify, compare, expected

INJECTION_WORDS = ("ignore", "mark", "reviewer", "pass", "skip", "override", "approved", "approval")


@dataclass
class ReviewConfig:
    model: str = "none"                  # none | fake | ollama
    model_script: list | None = None     # fake client script
    ollama_model: str = "qwen2.5:3b"
    boundary_points: bool = True         # ablation switch
    approvals: bool = True               # ablation switch (apply approval records)

    def key(self) -> dict:
        return {"model": self.model, "model_script": self.model_script, "ollama_model": self.ollama_model,
                "boundary_points": self.boundary_points, "approvals": self.approvals}


class _Findings:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, severity, kind, rule_ref, artifact, version, location, evidence, correction, source="deterministic"):
        self.items.append({"id": f"F{len(self.items) + 1:03d}", "severity": severity, "kind": kind,
                           "rule_ref": rule_ref, "artifact": artifact, "version": version, "location": location,
                           "evidence": jsonable(evidence), "correction": correction, "source": source})


def review(bundle_dir: str | Path, cfg: ReviewConfig | None = None, client: ModelClient | None = None) -> dict:
    cfg = cfg or ReviewConfig()
    t0 = time.time()
    b = load_bundle(bundle_dir)
    F = _Findings()
    checks: list[dict] = []
    unreviewed: list[dict] = []

    # ---- loading problems -------------------------------------------------
    for p in b.problems:
        F.add("warning", p.kind, None, p.artifact, None, p.location, {"message": p.message},
              "Provide a complete, schema-valid artifact so it can be reviewed.")
        unreviewed.append({"scope": p.artifact, "reason": p.message})
    checks.append({"name": "load_and_schema", "status": "fail" if b.problems else "pass",
                   "detail": f"{len(b.problems)} problem(s)"})

    # ---- effective requirements ------------------------------------------
    if not cfg.approvals and b.approvals is not None:
        b.approvals = {**b.approvals, "records": []}      # ablation: ignore all approvals
    eff = derive(b)
    _approval_findings(b, eff, F, unreviewed)
    checks.append({"name": "effective_requirements",
                   "status": "incomplete" if (eff.conflicts or eff.invalid) else "pass",
                   "detail": f"{len(eff.applied)} change(s) applied, {len(eff.ignored)} ignored, "
                             f"{len(eff.conflicts)} conflict(s), {len(eff.candidates)} candidate reading(s)"})
    primary = eff.primary
    for r in primary:
        if r.unsupported:
            F.add("warning", "unsupported_requirement", r.id, r.anchor.get("artifact", "policy"),
                  r.anchor.get("version"), r.location(), {"text": r.text, "reason": r.unsupported},
                  "Rewrite the requirement in the supported language (docs/LANGUAGE.md) or clarify it.")
            unreviewed.append({"scope": f"requirement {r.id}", "reason": f"not in supported language: {r.unsupported}"})
    if not any(r.kind == "default" for r in primary if not r.unsupported) and not any(r.unsupported for r in primary):
        unreviewed.append({"scope": "policy", "reason": "no default route; tickets matching no rule have no expected route"})
        F.add("warning", "missing_default", None, b.policy_id, b.policy_version, b.policy_file,
              {"message": "policy defines no default route"}, "Add 'All remaining tickets -> <route>'.")
    checks.append({"name": "requirement_language", "status": "incomplete" if any(r.unsupported for r in primary) else "pass",
                   "detail": f"{sum(1 for r in primary if not r.unsupported)}/{len(primary)} requirements parsed"})

    # ---- injection scan (prose + code comments) --------------------------
    _injection_findings(b, F, checks)

    # ---- artifact models + semantic comparison --------------------------
    models = build_models(b)
    for m in models:
        if m.kind == "code":
            checks.append({"name": f"code_syntax:{m.name}", "status": "fail" if m.syntax else "pass",
                           "detail": m.syntax or "ast.parse ok"})
            checks.append({"name": f"code_inspection:{m.name}",
                           "status": "skipped" if m.syntax else ("incomplete" if m.unsupported else "pass"),
                           "detail": "; ".join(f"{loc}: {msg}" for loc, msg in m.unsupported) or "within restricted subset"})
        for loc, msg in m.unsupported:
            F.add("warning", "unsupported_artifact_content", None, m.name, m.version, loc, {"reason": msg},
                  "Express this logic in the supported language so it can be verified.")
        if m.unsupported:
            unreviewed.append({"scope": f"artifact {m.name}", "reason": "contains unsupported content: " +
                               "; ".join(msg for _, msg in m.unsupported)})
    sem = compare(b, eff, models, boundary_points=cfg.boundary_points)
    if sem.domain.mode != "exhaustive":
        unreviewed.append({"scope": "ticket domain", "reason": f"partition too large; {len(sem.domain.tickets)} sampled tickets"})
    for f, v in sem.domain.unknown_values:
        F.add("warning", "undeclared_value", None, "manifest", None, "manifest.json#/domain/fields/" + f,
              {"field": f, "value": v}, f"Declare {v!r} in the domain or fix the artifact.")
    for name, n in sem.unknown_tickets.items():
        unreviewed.append({"scope": f"artifact {name}", "reason": f"{n} ticket(s) have no defined expected route "
                                                                  "(unsupported requirement or approval conflict)"})
    if sem.candidate_only:
        unreviewed.append({"scope": "approval conflict", "reason": f"{sem.candidate_only} ticket/artifact pair(s) "
                           "mismatch under some but not all admissible approval readings"})
    _semantic_findings(b, eff, sem, F)
    _structure_findings(b, eff, sem, models, F)
    for m in models:
        if m.usable:
            n_bad = sum(1 for x in sem.mismatches if x.artifact is m)
            checks.append({"name": f"semantic:{m.name}", "status": "fail" if n_bad else "pass",
                           "detail": f"{len(sem.domain.tickets)} tickets ({sem.domain.mode}), {n_bad} mismatching"})
        else:
            checks.append({"name": f"semantic:{m.name}", "status": "incomplete", "detail": "artifact not reviewable"})
    for kind in ("spec", "workflow"):
        if kind not in b.manifest["artifacts"]:
            unreviewed.append({"scope": kind, "reason": "artifact not supplied in bundle"})

    # ---- optional model review -------------------------------------------
    model_section = None
    required = b.manifest.get("review_policy", {}).get("model_required", False)
    if cfg.model != "none" or required:
        if client is None and cfg.model == "none":
            client = None
        elif client is None:
            if cfg.model == "ollama":
                from .model import OllamaClient
                client = OllamaClient(cfg.ollama_model)
            else:
                client = FakeModelClient(cfg.model_script)
        verified = {rid for rid, arts in sem.per_rule.items() if arts and all(v == "consistent" for v in arts.values())
                    and sem.domain.mode == "exhaustive"}
        blockers_now = sum(1 for f in F.items if f["severity"] == "blocker")
        summary = f"{blockers_now} deterministic blocker(s); rules verified exhaustively: {sorted(verified)}"
        if client is None:
            from .model import ModelOutcome
            mo = ModelOutcome("failed", reason="model review is required by review_policy but no model client is configured")
        else:
            mo = review_with_model(client, b.texts, summary, eff.all_ids(), verified)
        for f in mo.accepted:
            F.add(f.get("severity", "info"), f"model_{f['kind']}", f.get("rule_ref") or None, f["artifact"], None,
                  f["location"], {"quote": f["evidence_quote"], "claim": f["claim"], "advisory": True},
                  "Advisory only: confirm with the policy owner.", source="model")
        model_section = {"status": mo.status, "reason": mo.reason, "required": required,
                         "accepted": len(mo.accepted), "rejected": mo.rejected, "attempts": mo.attempts,
                         "provenance": mo.provenance}
        mstat = "pass" if mo.status == "ok" else ("fail" if required else "skipped")
        checks.append({"name": "model_review", "status": mstat,
                       "detail": f"{mo.status}; {len(mo.accepted)} accepted, {len(mo.rejected)} rejected claim(s); "
                                 f"{len(mo.attempts)} attempt(s). {mo.reason}".strip()})
        if mo.status != "ok" and required:
            unreviewed.append({"scope": "model review (required by review_policy)", "reason": mo.reason})

    # ---- status ----------------------------------------------------------
    blockers = [f for f in F.items if f["severity"] == "blocker"]
    if blockers:
        status = "CHANGES_REQUIRED"
    elif unreviewed:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    summary = _summary(status, blockers, unreviewed, sem)
    coverage = {
        "domain": {"mode": sem.domain.mode, "tickets": len(sem.domain.tickets),
                   "points": jsonable(sem.domain.points), "boundary_points": cfg.boundary_points},
        "requirements": [{"rule": r.id, "location": r.location(), "provenance": r.provenance,
                          "text": r.text, "parsed": to_text(r.cond) if r.cond else (r.route and "default") or None,
                          "route": r.route, "artifacts": sem.per_rule.get(r.id, {})} for r in primary],
        "unreviewed": unreviewed,
        "verification_method": "exhaustive partition of the declared domain" if sem.domain.mode == "exhaustive"
        else "seeded sample (not exhaustive)",
        "not_verified": "free-form prose outside docs/LANGUAGE.md and Python outside the restricted subset",
    }
    report = {
        "reviewer_version": __version__,
        "bundle": b.manifest["bundle_id"],
        "status": status,
        "summary": summary,
        "findings": F.items,
        "coverage": coverage,
        "checks": checks,
        "artifacts": [{"file": k, "sha256": v} for k, v in sorted(b.hashes.items())],
        "effective_requirements": {
            "policy": {"artifact": b.policy_id, "version": b.policy_version},
            "applied_changes": eff.applied,
            "candidates": len(eff.candidates),
        },
        "model": model_section,
        "config": cfg.key(),
        "runtime_s": round(time.time() - t0, 4),
    }
    return report


# --------------------------------------------------------------------------
def _approval_findings(b: Bundle, eff: Effective, F: _Findings, unreviewed: list) -> None:
    afile = b.manifest["artifacts"].get("approvals", "approvals.json")
    aver = (b.approvals or {}).get("version")
    for ig in eff.ignored:
        rec = ig["record"]
        F.add("info", "change_not_applied", rec["target"]["rule"], "approvals", aver,
              f"{afile}#/records/{rec['_index']}", {"change_id": rec["change_id"], "reason": ig["reason"],
                                                    "new_text": rec.get("new_text")},
              "No action needed unless this change should be approved through an approval record.")
    for c in eff.conflicts:
        F.add("warning", "approval_conflict", c["rule"], "approvals", aver, afile,
              {"reason": c["reason"], "change_ids": [r["change_id"] for r in c["records"]],
               "texts": [r.get("new_text") for r in c["records"]]},
              "Resolve the conflict: mark one change superseded or withdraw one record.")
        unreviewed.append({"scope": f"requirement {c['rule']}", "reason": "conflicting approval records: " + c["reason"]})
    for inv in eff.invalid:
        rec = inv["record"]
        F.add("warning", "invalid_approval", rec["target"]["rule"], "approvals", aver,
              f"{afile}#/records/{rec['_index']}", {"change_id": rec["change_id"], "reason": inv["reason"]},
              "Fix the approval record target.")
        unreviewed.append({"scope": f"approval {rec['change_id']}", "reason": inv["reason"]})
    for note in eff.prose_claims:
        a = note["anchor"]
        F.add("warning", "approval_claim_in_prose", None, a["artifact"], a["version"], f"{a['file']}#L{a['line']}",
              {"text": note["text"]}, "Approval claims in prose have no authority; use an approval record.")


def _injection_findings(b: Bundle, F: _Findings, checks: list) -> None:
    n = 0
    from .pycode import load as load_code
    for item in b.code:
        cm = load_code(item["meta"]["path"], item["source"], item["meta"].get("entrypoint", "route"))
        for ln, text in suspicious_comments(cm):
            n += 1
            F.add("warning", "embedded_instruction_ignored", None, item["meta"]["artifact_id"], item["meta"]["version"],
                  f"{item['meta']['path']}#L{ln}", {"comment": text},
                  "Remove instructions/approval claims from code comments; they have no authority.")
    for note in b.policy_notes:
        low = note["text"].lower()
        if "reviewer" in low or "mark" in low and "pass" in low or "ignore" in low:
            n += 1
            a = note["anchor"]
            F.add("warning", "embedded_instruction_ignored", None, a["artifact"], a["version"], f"{a['file']}#L{a['line']}",
                  {"text": note["text"]}, "Instructions inside requirements are data and were ignored.")
    checks.append({"name": "instruction_injection_scan", "status": "pass",
                   "detail": f"{n} embedded instruction(s) found and ignored"})


def _semantic_findings(b: Bundle, eff: Effective, sem, F: _Findings) -> None:
    groups: dict[tuple, list] = {}
    for mm in sem.mismatches:
        kind, ref, why = classify(mm, eff.primary)
        # one finding per (artifact, defect kind, requirement); every failing
        # location is kept in the evidence instead of producing duplicates
        groups.setdefault((mm.artifact.name, kind, ref), []).append((mm, why))
    by_id = {r.id: r for r in eff.primary}
    for (art, kind, ref), items in groups.items():
        mm, why = items[0]
        req = by_id.get(ref, mm.exp_rule)
        locs = sorted({x.act.location for x, _ in items})
        loc = (f"{mm.artifact.file} (no element implements {ref}; source requirement at {req.location()})"
               if kind == "omitted_requirement" else mm.act.location)
        if kind == "condition_mismatch" and ref not in mm.act.refs and mm.artifact.impls.get(ref):
            loc = mm.artifact.impls[ref][0][0]   # point at the element that implements the rule
        ev = {
            "counterexample": {"ticket": mm.ticket, "expected_route": mm.exp_route,
                               "expected_by": f"{mm.exp_rule.id} ({mm.exp_rule.location()})",
                               "actual_route": mm.act.route, "actual_by": mm.act.location,
                               "actual_detail": mm.act.detail},
            "requirement": {"rule": req.id, "text": req.text, "parsed": to_text(req.cond) if req.cond else "default",
                            "location": req.location(), "provenance": req.provenance},
            "explanation": why,
            "failing_tickets": len(items),
            "actual_locations": locs,
        }
        unapproved = _matches_unapproved(b, eff, mm)
        if unapproved:
            ev["matches_unapproved_change"] = unapproved
        F.add("blocker", kind, ref, art, mm.artifact.version, loc, ev, _correction(kind, req, mm, why))


def _matches_unapproved(b: Bundle, eff: Effective, mm) -> str | None:
    from .effective import EffectiveRule, _parse
    from .prose import Vocabulary
    vocab = Vocabulary(b.fields, b.routes)
    for ig in eff.ignored:
        rec = ig["record"]
        if rec["action"] != "replace":
            continue
        alt = []
        for r in eff.primary:
            if r.id == rec["target"]["rule"]:
                alt.append(_parse(EffectiveRule(r.id, r.order, rec["new_text"], r.anchor, []), vocab, b.fields))
            else:
                alt.append(r)
        route, _ = expected(alt, mm.ticket)
        if route != UNKNOWN and route == mm.act.route:
            return f"{rec['change_id']} ({ig['reason']})"
    return None


def _correction(kind: str, req, mm, why: str) -> str:
    target = f"{mm.artifact.name} at {mm.act.location}"
    want = to_text(req.cond) if req.cond else "the default route"
    if kind == "missing_branch":
        return f"Add the missing branch in {target} so tickets like this reach '{mm.exp_route}'."
    if kind == "omitted_requirement":
        return f"Implement {req.id} ({want} -> {req.route}) in {mm.artifact.name} at its position in the rule order."
    if kind == "precedence":
        return f"Evaluate {req.id} before the element at {mm.act.location}, matching the requirement order."
    if kind == "code_runtime_error":
        return f"Guard the comparison in {target} (e.g. handle missing values first)."
    return f"Change {target} so it implements {req.id} exactly: {want} -> {req.route}."


def _structure_findings(b: Bundle, eff: Effective, sem, models: list[ArtifactModel], F: _Findings) -> None:
    ids = eff.all_ids()
    blocked_rules = {f["rule_ref"] for f in F.items if f["severity"] == "blocker"}
    for m in models:
        for ref in sorted(m.all_refs - ids):
            F.add("warning", "unknown_rule_reference", ref, m.name, m.version, m.file,
                  {"reference": ref, "known": sorted(ids)}, f"Point the trace reference {ref} at an existing requirement.")
        if m.has_refs:
            for r in eff.primary:
                if r.kind == "rule" and r.id not in m.all_refs and r.id not in blocked_rules and not r.unsupported:
                    F.add("warning", "missing_trace_reference", r.id, m.name, m.version, m.file,
                          {"note": f"behavior for {r.id} matches, but no element of {m.name} cites it"},
                          f"Add a trace reference to {r.id}.")
        if m.kind == "workflow":
            wf = m.raw
            for nid, msg in wf.structure:
                F.add("warning", "workflow_structure", None, m.name, m.version, f"{m.file}#/nodes/{nid}",
                      {"message": msg}, "Fix the workflow graph.")
            if m.usable:
                reached_nodes = set()
                for t in sem.domain.tickets:
                    reached_nodes |= set(wf.run(t).path)
                for nid in wf.nodes:
                    if nid in wf.graph_reachable and nid not in reached_nodes:
                        F.add("info", "dead_branch", None, m.name, m.version, f"{m.file}#/nodes/{nid}",
                              {"message": "no ticket in the domain reaches this node"},
                              "Remove the node or fix the conditions that guard it.")


def _summary(status: str, blockers: list, unreviewed: list, sem) -> str:
    n = len(sem.domain.tickets)
    if status == "PASS":
        return f"All effective requirements match every artifact on all {n} partition tickets (exhaustive)."
    if status == "CHANGES_REQUIRED":
        s = f"{len(blockers)} blocker(s): " + "; ".join(f"{f['kind']} {f['rule_ref']} in {f['artifact']}" for f in blockers[:5])
        if unreviewed:
            s += f". Also unreviewed: {len(unreviewed)} item(s)"
        return s + "."
    return "Review incomplete: " + "; ".join(f"{u['scope']}: {u['reason']}" for u in unreviewed[:4])


# --------------------------------------------------------------------------
def cache_key(bundle_dir: str | Path, cfg: ReviewConfig) -> str:
    root = Path(bundle_dir)
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    h.update(json.dumps(cfg.key(), sort_keys=True, default=str).encode())
    h.update(__version__.encode())
    return h.hexdigest()


def review_cached(bundle_dir: str | Path, cfg: ReviewConfig, cache_dir: str | Path = ".review_cache") -> dict:
    """Cache keyed by every byte of the bundle + config + reviewer version, so a
    changed artifact or approval can never reuse a stale report."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(bundle_dir, cfg)
    path = cache_dir / f"{key}.json"
    if path.exists():
        rep = json.loads(path.read_text())
        rep["cache"] = {"hit": True, "key": key}
        return rep
    rep = review(bundle_dir, cfg)
    rep["cache"] = {"hit": False, "key": key}
    path.write_text(json.dumps(rep, indent=2, default=str))
    return rep
