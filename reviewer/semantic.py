"""Deterministic semantic review.

Core idea: every artifact is a routing function over a small typed ticket
domain. All conditions in the supported language are comparisons of one
field against constants (after solving linear expressions in code). So the
truth value of every condition is constant on each region between those
constants. Picking one ticket per region (each constant itself, a point
between neighbours, one point below and one above, plus None for nullable
fields and every enum/bool value) gives a finite *partition* of the domain.
Comparing the effective requirements against an artifact on every partition
ticket is therefore an exhaustive equivalence check for the supported
language, not a sample. If the partition would exceed MAX_TICKETS we fall
back to a seeded sample and report coverage as "sampled" (never PASS).
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable

from . import pycode
from .conditions import ConditionError, constants, evaluate, jsonable, normalize, to_text
from .effective import Effective, EffectiveRule
from .loader import Bundle
from .workflow import Workflow

MAX_TICKETS = 20_000
UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# artifact adapters: one uniform interface over spec / workflow / code
# --------------------------------------------------------------------------
@dataclass
class Outcome:
    route: str
    location: str
    refs: list[str]
    detail: str = ""


@dataclass
class ArtifactModel:
    name: str
    kind: str                       # spec | workflow | code
    version: str
    file: str
    run: Callable[[dict], Outcome] | None
    consts: list[tuple[str, Any]] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)   # (location, reason)
    has_refs: bool = False
    all_refs: set[str] = field(default_factory=set)
    # implementation lookup for precedence diagnosis: rule id -> [(location, order, matcher)]
    impls: dict[str, list[tuple[str, int, Callable[[dict], bool | None], str]]] = field(default_factory=dict)
    syntax: str | None = None       # code only
    raw: Any = None

    @property
    def usable(self) -> bool:
        return self.run is not None and not self.unsupported


def spec_model(b: Bundle) -> ArtifactModel:
    s = b.spec
    file = b.manifest["artifacts"]["spec"]
    m = ArtifactModel("spec", "spec", s["version"], file, None, raw=s)
    index = {r["id"]: i for i, r in enumerate(s["rules"])}
    rules = sorted(s["rules"], key=lambda r: r["order"])
    norm: list[tuple[dict, dict]] = []
    for r in rules:
        loc = f"{file}#/rules/{index[r['id']]} ({r['id']})"
        try:
            c = normalize(r["when"], b.fields)
        except ConditionError as exc:
            m.unsupported.append((loc, str(exc)))
            continue
        norm.append((r, c))
        m.consts += list(constants(c))
        for ref in r.get("implements", []):
            m.has_refs = True
            m.all_refs.add(ref)
            m.impls.setdefault(ref, []).append((loc, r["order"], (lambda c: lambda t: evaluate(c, t))(c), to_text(c)))
    for ref in s["default"].get("implements", []):
        m.has_refs = True
        m.all_refs.add(ref)
    if m.unsupported:
        return m

    def run(t: dict) -> Outcome:
        for r, c in norm:
            if evaluate(c, t):
                return Outcome(r["route"], f"{file}#/rules/{index[r['id']]} ({r['id']})",
                               list(r.get("implements", [])), to_text(c))
        return Outcome(s["default"]["route"], f"{file}#/default", list(s["default"].get("implements", [])), "default")
    m.run = run
    return m


def workflow_model(b: Bundle) -> ArtifactModel:
    data = b.workflow
    file = b.manifest["artifacts"]["workflow"]
    wf = Workflow(data, b.fields)
    m = ArtifactModel("workflow", "workflow", data["version"], file, None, raw=wf)
    m.unsupported = [(f"{file}#/nodes/{n}", msg) for n, msg in wf.errors]
    for nid, c in wf.conds.items():
        m.consts += list(constants(c))
        node = wf.nodes[nid]
        for ref in node.get("implements", []):
            m.has_refs = True
            m.all_refs.add(ref)
            order = list(wf.nodes).index(nid)
            m.impls.setdefault(ref, []).append(
                (f"{file}#/nodes/{nid} (rev {node.get('rev')})", order, (lambda c: lambda t: evaluate(c, t))(c), to_text(c)))
    if not wf.usable:
        if wf.start not in wf.nodes:
            m.unsupported.append((f"{file}#/start", f"start node {wf.start!r} missing"))
        return m

    def run(t: dict) -> Outcome:
        r = wf.run(t)
        if r.decided_by:
            node = wf.nodes[r.decided_by]
            loc = f"{file}#/nodes/{r.decided_by} (rev {node.get('rev')})"
        else:
            loc = f"{file}#/nodes/{wf.start}"
        detail = " -> ".join(r.path) + (f"; {r.problem}" if r.problem else "")
        return Outcome(r.route, loc, r.refs, detail)
    m.run = run
    return m


def code_model(b: Bundle, item: dict) -> ArtifactModel:
    meta, src = item["meta"], item["source"]
    cm = pycode.load(meta["path"], src, meta.get("entrypoint", "route"))
    m = ArtifactModel(meta["artifact_id"], "code", meta["version"], meta["path"], None, raw=cm)
    m.syntax = cm.syntax_error
    if cm.syntax_error:
        m.unsupported.append((meta["path"], f"syntax error: {cm.syntax_error}"))
        return m
    m.unsupported = [(f"{meta['path']}#L{ln}" if ln else meta["path"], msg) for msg, ln in cm.unsupported]
    for refs in cm.tags.values():
        m.has_refs = True
        m.all_refs |= set(refs)
    if m.unsupported:
        return m
    for f, v in pycode.thresholds(cm):
        if f in b.fields:
            m.consts.append((f, v))
    for ref in m.all_refs:
        for ln in pycode.tagged_if_lines(cm, ref):
            m.impls.setdefault(ref, []).append(
                (f"{meta['path']}#L{ln}", ln, (lambda ln: lambda t: pycode.test_at(cm, ln, t))(ln),
                 src.splitlines()[ln - 1].strip()))
    lines = src.splitlines()

    def run(t: dict) -> Outcome:
        r = pycode.run(cm, t)
        ln = r.line or r.branch_line
        loc = f"{meta['path']}#L{ln}" if ln else meta["path"]
        detail = lines[ln - 1].strip() if ln else ""
        if r.error:
            detail = f"{detail} -> {r.error}"
        return Outcome(r.route, loc, r.refs, detail)
    m.run = run
    return m


def build_models(b: Bundle) -> list[ArtifactModel]:
    models = []
    if b.spec is not None:
        models.append(spec_model(b))
    if b.workflow is not None:
        models.append(workflow_model(b))
    for item in b.code:
        models.append(code_model(b, item))
    return models


# --------------------------------------------------------------------------
# partition of the ticket domain
# --------------------------------------------------------------------------
@dataclass
class Domain:
    tickets: list[dict]
    mode: str                    # exhaustive | sampled
    points: dict[str, list]
    unknown_values: list[tuple[str, Any]]


def partition(fields: dict, consts: list[tuple[str, Any]], boundary_points: bool = True,
              seed: int = 7) -> Domain:
    points: dict[str, list] = {}
    unknown_values = []
    for f, spec in fields.items():
        cs = [v for (ff, v) in consts if ff == f]
        if spec["type"] == "number":
            lo = Fraction(str(spec["min"])) if "min" in spec else None
            hi = Fraction(str(spec["max"])) if "max" in spec else None
            ts = sorted({Fraction(v) for v in cs if isinstance(v, (Fraction, int, float)) and not isinstance(v, bool)})
            ts_in = [t for t in ts if (lo is None or t >= lo) and (hi is None or t <= hi)]
            pts: set[Fraction] = set()
            if not ts_in:
                pts.add(lo if lo is not None else Fraction(0))
            else:
                if boundary_points:
                    pts |= set(ts_in)
                for a, c in zip(ts_in, ts_in[1:]):
                    pts.add((a + c) / 2)
                below = ts_in[0] - 1
                if lo is not None and below < lo:
                    below = lo if lo < ts_in[0] else None
                if below is not None:
                    pts.add(below)
                above = ts_in[-1] + 1
                if hi is not None and above > hi:
                    above = hi if hi > ts_in[-1] else None
                if above is not None:
                    pts.add(above)
            vals: list = sorted(pts)
        elif spec["type"] == "bool":
            vals = [False, True]
        else:
            declared = list(spec.get("values", []))
            for v in cs:
                if v not in declared:
                    unknown_values.append((f, v))
            vals = declared
        if spec.get("nullable"):
            vals = [None] + vals
        points[f] = vals
    names = list(fields)
    total = 1
    for n in names:
        total *= max(1, len(points[n]))
    if total <= MAX_TICKETS:
        tickets = [dict(zip(names, combo)) for combo in itertools.product(*(points[n] for n in names))]
        mode = "exhaustive"
    else:
        rng = random.Random(seed)
        tickets = [{n: rng.choice(points[n]) for n in names} for _ in range(MAX_TICKETS)]
        mode = "sampled"
    return Domain(tickets, mode, points, unknown_values)


# --------------------------------------------------------------------------
# expected routing from effective requirements
# --------------------------------------------------------------------------
def expected(rules: list[EffectiveRule], t: dict) -> tuple[str, EffectiveRule | None]:
    for r in rules:
        if r.unsupported:
            return UNKNOWN, r
        if r.kind == "default":
            return r.route, r
        if evaluate(r.cond, t):
            return r.route, r
    return UNKNOWN, None


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------
@dataclass
class Mismatch:
    artifact: ArtifactModel
    ticket: dict
    exp_route: str
    exp_rule: EffectiveRule
    act: Outcome


@dataclass
class SemanticResult:
    domain: Domain
    models: list[ArtifactModel]
    mismatches: list[Mismatch]                 # established under every candidate
    candidate_only: int                        # mismatches under some but not all candidates
    unknown_tickets: dict[str, int]            # artifact -> tickets with unknown expectation
    per_rule: dict[str, dict[str, str]]        # rule id -> artifact -> consistent|mismatch|unreviewed
    reached: dict[str, set[str]]               # artifact -> locations reached by some ticket


def compare(b: Bundle, eff: Effective, models: list[ArtifactModel], boundary_points: bool = True) -> SemanticResult:
    consts: list[tuple[str, Any]] = []
    for cand in eff.candidates:
        for r in cand:
            if r.cond is not None:
                consts += list(constants(r.cond))
    for m in models:
        consts += m.consts
    dom = partition(b.fields, consts, boundary_points=boundary_points)
    primary = eff.primary
    mismatches: list[Mismatch] = []
    candidate_only = 0
    unknown: dict[str, int] = {}
    per_rule: dict[str, dict[str, str]] = {r.id: {} for r in primary}
    reached: dict[str, set[str]] = {m.name: set() for m in models}
    exp_cache = [[expected(c, t) for t in dom.tickets] for c in eff.candidates]
    for m in models:
        if not m.usable:
            for r in primary:
                per_rule[r.id][m.name] = "unreviewed"
            continue
        for i, t in enumerate(dom.tickets):
            act = m.run(t)
            reached[m.name].add(act.location)
            exps = [ec[i] for ec in exp_cache]
            p_route, p_rule = exps[0]
            if any(e[0] == UNKNOWN for e in exps):
                unknown[m.name] = unknown.get(m.name, 0) + 1
                if p_rule is not None and p_rule.id in per_rule:
                    per_rule[p_rule.id][m.name] = "unreviewed"
                continue
            bad = [e[0] != act.route for e in exps]
            if all(bad):
                mismatches.append(Mismatch(m, t, p_route, p_rule, act))
                per_rule[p_rule.id][m.name] = "mismatch"
            else:
                if any(bad):
                    candidate_only += 1
                    per_rule[p_rule.id].setdefault(m.name, "unreviewed")
                else:
                    per_rule[p_rule.id].setdefault(m.name, "consistent")
    for r in primary:
        for m in models:
            per_rule[r.id].setdefault(m.name, "consistent" if m.usable else "unreviewed")
    return SemanticResult(dom, models, mismatches, candidate_only, unknown, per_rule, reached)


def classify(mm: Mismatch, rules: list[EffectiveRule]) -> tuple[str, str, str]:
    """Return (kind, rule_ref, explanation) for a behavioral mismatch."""
    m, req, act = mm.artifact, mm.exp_rule, mm.act
    by_id = {r.id: r for r in rules}
    if act.route == "NO_ROUTE":
        return "missing_branch", req.id, f"no route produced ({act.detail})"
    if act.route == "RUNTIME_ERROR":
        return "code_runtime_error", req.id, f"code raises at runtime ({act.detail})"
    act_rule = next((by_id[r] for r in act.refs if r in by_id), None)
    if req.kind == "default":
        if act_rule is not None:
            return ("condition_mismatch", act_rule.id,
                    f"{act.location} (traced to {act_rule.id}) matches a ticket that {act_rule.id} does not cover; "
                    f"the requirements send it to the default route")
        return "wrong_route", req.id, f"default route differs: {act.location} sends to {act.route}"
    if m.has_refs and req.id not in m.all_refs:
        return "omitted_requirement", req.id, f"no element of {m.name} implements {req.id}"
    if act_rule is not None and act_rule.id == req.id:
        return "condition_mismatch", req.id, f"{act.location} implements {req.id} but routes this ticket to {act.route}"
    if act_rule is not None and act_rule.order > req.order:
        # did the artifact's own implementation of req match, but get pre-empted?
        for loc, order, matcher, text in m.impls.get(req.id, []):
            if matcher(mm.ticket):
                return ("precedence", req.id,
                        f"{loc} implements {req.id} and matches this ticket, but {act.location} "
                        f"(traced to {act_rule.id}) is evaluated first")
    if act_rule is not None and act_rule.order < req.order:
        return ("condition_mismatch", act_rule.id,
                f"{act.location} (traced to {act_rule.id}) also matches this ticket, which "
                f"{act_rule.id} does not cover; the requirements route it by {req.id}")
    impl = m.impls.get(req.id, [])
    if impl:
        return ("condition_mismatch", req.id,
                f"implementation of {req.id} at {impl[0][0]} ({impl[0][3]}) does not match this ticket; "
                f"it falls through to {act.location}")
    return "wrong_route", req.id, f"{act.location} sends the ticket to {act.route}"


def ticket_json(t: dict) -> dict:
    return jsonable(t)
