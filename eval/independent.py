"""Independent evaluation of artifacts on a single ticket, for checking that a
reviewer counterexample is real. Separate, deliberately simple code: it does
not import `reviewer/`. Fixture code is our own synthetic, trusted code, so the
harness may execute it (the reviewer itself never executes code)."""
import json
from pathlib import Path

UNITS = {"seconds": 1 / 60, "minutes": 1, "hours": 60}


def _cond(c, t):
    if "missing" in c:
        return t.get(c["missing"]) is None
    if "present" in c:
        return t.get(c["present"]) is not None
    if "const" in c:
        return c["const"]
    if "all" in c:
        return all(_cond(x, t) for x in c["all"])
    if "any" in c:
        return any(_cond(x, t) for x in c["any"])
    if "not" in c:
        return not _cond(c["not"], t)
    v = t.get(c["field"])
    if v is None:
        return False
    target = c["value"]
    if isinstance(target, (int, float)) and not isinstance(target, bool) and "unit" in c:
        target = target * UNITS[c["unit"]]
    op = c["op"]
    return {"==": v == target, "!=": v != target, "<": v < target if op == "<" else None,
            "<=": v <= target if op == "<=" else None, ">": v > target if op == ">" else None,
            ">=": v >= target if op == ">=" else None, "in": v in target if op == "in" else None}[op]


def spec_route(spec, t):
    for r in sorted(spec["rules"], key=lambda r: r["order"]):
        if _cond(r["when"], t):
            return r["route"]
    return spec["default"]["route"]


def workflow_route(wf, t):
    nid, seen = wf["start"], set()
    while nid in wf["nodes"] and nid not in seen:
        seen.add(nid)
        n = wf["nodes"][nid]
        if n["type"] == "route":
            return n["route"]
        nid = n.get("on_true") if _cond(n["condition"], t) else n.get("on_false")
    return "NO_ROUTE"


class _Ticket(dict):
    __getattr__ = dict.get


def code_route(path, entry, t):
    ns = {}
    exec(compile(Path(path).read_text(), str(path), "exec"), ns)  # trusted fixture code only
    try:
        r = ns[entry](_Ticket(t))
    except Exception:
        return "RUNTIME_ERROR"
    return r if r is not None else "NO_ROUTE"


def artifact_route(bundle_dir, artifact, t):
    b = Path(bundle_dir)
    m = json.loads((b / "manifest.json").read_text())
    if artifact == "spec":
        return spec_route(json.loads((b / m["artifacts"]["spec"]).read_text()), t)
    if artifact == "workflow":
        return workflow_route(json.loads((b / m["artifacts"]["workflow"]).read_text()), t)
    for c in m["artifacts"].get("code", []):
        if c["artifact_id"] == artifact:
            return code_route(b / c["path"], c.get("entrypoint", "route"), t)
    raise KeyError(artifact)
