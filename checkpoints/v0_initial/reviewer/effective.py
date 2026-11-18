"""Derive effective requirements from the source policy + approval records.

Rules (documented in docs/ENGINEERING_NOTE.md):
  * Only top-level approval records are trusted. Approval claims inside prose
    or code comments are reported and ignored.
  * Revision scope: a record applies only if target.artifact/version equal
    the policy artifact id and version under review. Other records are
    reported as out of scope.
  * Ordering: applicable approved records are applied in ascending `seq`.
  * Two approved records that change the same rule conflict unless the later
    one lists the earlier in `supersedes`. Equal `seq` with different content
    is always a conflict. Conflicts are never resolved silently: every
    resolution is kept as a candidate and the reviewer must find the same
    defect under all candidates to block, otherwise the result is INCOMPLETE.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .conditions import ConditionError, normalize
from .loader import Bundle
from .prose import ParsedRule, ProseError, Vocabulary, parse_rule

MAX_CANDIDATES = 8
APPROVAL_CLAIM = re.compile(r"\b(approved|approval|signed[- ]off|authori[sz]ed)\b", re.I)


@dataclass
class EffectiveRule:
    id: str
    order: int
    text: str
    anchor: dict
    provenance: list[str]
    kind: str = "rule"                 # rule | default
    cond: dict | None = None           # normalized
    route: str | None = None
    unsupported: str | None = None     # reason, if not in supported language

    def location(self) -> str:
        a = self.anchor
        return f"{a['file']}#L{a['line']}" if "line" in a else a.get("location", "?")


@dataclass
class Effective:
    candidates: list[list[EffectiveRule]]
    applied: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)       # unapproved / out of scope
    conflicts: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    prose_claims: list[dict] = field(default_factory=list)
    original_ids: set[str] = field(default_factory=set)

    @property
    def primary(self) -> list[EffectiveRule]:
        return self.candidates[0]

    def all_ids(self) -> set[str]:
        ids = set(self.original_ids)
        for c in self.candidates:
            ids |= {r.id for r in c}
        return ids


def _parse(rule: EffectiveRule, vocab: Vocabulary, fields: dict) -> EffectiveRule:
    try:
        pr: ParsedRule = parse_rule(rule.text, vocab)
        rule.kind = pr.kind
        rule.route = pr.route
        rule.cond = normalize(pr.cond, fields) if pr.cond is not None else None
    except (ProseError, ConditionError) as exc:
        rule.unsupported = str(exc)
    return rule


def derive(b: Bundle) -> Effective:
    vocab = Vocabulary(b.fields, b.routes)
    base = [EffectiveRule(r["id"], r["order"], r["text"], r["anchor"], []) for r in b.policy_records]
    eff = Effective(candidates=[], original_ids={r.id for r in base})

    for note in b.policy_notes:
        if APPROVAL_CLAIM.search(note["text"]):
            eff.prose_claims.append(note)

    records = (b.approvals or {}).get("records", [])
    in_scope: list[dict] = []
    seen_ids: dict[str, dict] = {}
    for i, rec in enumerate(records):
        rec = dict(rec, _index=i)
        prev = seen_ids.get(rec["change_id"])
        if prev is not None:
            same = {k: v for k, v in prev.items() if k != "_index"} == {k: v for k, v in rec.items() if k != "_index"}
            if not same:
                eff.conflicts.append({"rule": rec["target"]["rule"], "records": [prev, rec],
                                      "reason": f"change_id {rec['change_id']} appears twice with different content"})
            continue
        seen_ids[rec["change_id"]] = rec
        if rec["status"] != "approved":
            eff.ignored.append({"record": rec, "reason": f"status is {rec['status']}, not approved"})
            continue
        t = rec["target"]
        if t["artifact"] != b.policy_id or t["version"] != b.policy_version:
            eff.ignored.append({"record": rec, "reason": f"targets {t['artifact']} v{t['version']}, "
                                f"reviewing {b.policy_id} v{b.policy_version} (out of revision scope)"})
            continue
        if rec["action"] in ("replace", "remove") and t["rule"] not in eff.original_ids:
            eff.invalid.append({"record": rec, "reason": f"targets unknown rule {t['rule']}"})
            continue
        if rec["action"] in ("replace", "add") and not rec.get("new_text"):
            eff.invalid.append({"record": rec, "reason": "approved change has no new_text"})
            continue
        in_scope.append(rec)

    # group by target rule; detect conflicts
    by_rule: dict[str, list[dict]] = {}
    for rec in sorted(in_scope, key=lambda r: (r["seq"], r["_index"])):
        key = rec.get("new_rule_id") if rec["action"] == "add" else rec["target"]["rule"]
        by_rule.setdefault(key, []).append(rec)
    choices: list[list[list[dict]]] = []   # per conflicting rule: list of alternative change chains
    fixed: list[dict] = []
    for rule_id, recs in by_rule.items():
        chain_ok = all(
            recs[j]["seq"] > recs[j - 1]["seq"] and recs[j - 1]["change_id"] in recs[j].get("supersedes", [])
            for j in range(1, len(recs)))
        if chain_ok:
            fixed.extend(recs)
        else:
            eff.conflicts.append({"rule": rule_id, "records": recs,
                                  "reason": "multiple approved changes to the same rule without a supersedes chain"})
            choices.append([[r] for r in recs])

    combos = list(itertools.islice(itertools.product(*choices), MAX_CANDIDATES)) if choices else [()]
    for combo in combos:
        chosen = sorted(fixed + [r for alt in combo for r in alt], key=lambda r: (r["seq"], r["_index"]))
        rules = [EffectiveRule(r.id, r.order, r.text, dict(r.anchor), []) for r in base]
        for rec in chosen:
            loc = {"artifact": b.approvals["artifact_id"], "version": b.approvals["version"],
                   "location": f"{b.manifest['artifacts']['approvals']}#/records/{rec['_index']}"}
            if rec["action"] == "replace":
                for r in rules:
                    if r.id == rec["target"]["rule"]:
                        r.text = rec["new_text"]
                        r.provenance.append(rec["change_id"])
                        r.anchor = dict(loc, replaced=r.anchor)
            elif rec["action"] == "remove":
                rules = [r for r in rules if r.id != rec["target"]["rule"]]
            elif rec["action"] == "add":
                pos = next((i for i, r in enumerate(rules) if r.id == rec["target"]["rule"]), len(rules) - 1)
                rules.insert(pos + 1, EffectiveRule(rec.get("new_rule_id") or rec["change_id"], 0,
                                                    rec["new_text"], loc, [rec["change_id"]]))
        for i, r in enumerate(rules, start=1):
            r.order = i
            _parse(r, vocab, b.fields)
        eff.candidates.append(rules)
        if not eff.applied:
            eff.applied = [{"change_id": r["change_id"], "rule": r["target"]["rule"], "action": r["action"]}
                           for r in fixed]
    return eff
