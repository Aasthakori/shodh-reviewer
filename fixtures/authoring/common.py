import json, os
import pathlib
ROOT = str(pathlib.Path(__file__).resolve().parent.parent / "bundles")
DOMAIN = {
  "fields": {
    "category": {"type": "enum", "values": ["billing", "technical", "account"], "nullable": True},
    "urgent": {"type": "bool"},
    "wait_minutes": {"type": "number", "unit": "minutes", "min": 0, "aliases": ["wait", "waiting", "waiting time", "wait time"]}
  },
  "routes": {"manual_review": ["manual review"], "priority": ["priority queue"],
             "escalation": ["escalation queue"], "normal": ["normal queue"]}
}
BASE_POLICY = """<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy

The order of the rules below defines precedence.

- R1: Missing category -> manual review.
- R2: Otherwise, urgent ticket -> priority queue.
- R3: Otherwise, waiting >= 30 minutes -> escalation.
- DEFAULT: All remaining tickets -> normal queue.
"""
def spec(rules, default="normal", version="2.1", default_impl=("DEFAULT",)):
    return {"artifact_id": "routing-spec", "version": version, "rules": rules,
            "default": {"route": default, "implements": list(default_impl)}}
BASE_SPEC_RULES = [
  {"id": "S1", "order": 1, "implements": ["R1"], "when": {"missing": "category"}, "route": "manual_review"},
  {"id": "S2", "order": 2, "implements": ["R2"], "when": {"field": "urgent", "op": "==", "value": True}, "route": "priority"},
  {"id": "S3", "order": 3, "implements": ["R3"], "when": {"field": "wait_minutes", "op": ">=", "value": 30, "unit": "minutes"}, "route": "escalation"},
]
def wf(nodes, start="n_category", version="4"):
    return {"artifact_id": "routing-workflow", "version": version, "start": start, "nodes": nodes}
def base_nodes(th=30, op=">="):
    return {
      "n_category": {"type": "decision", "rev": 1, "implements": ["R1"], "condition": {"missing": "category"}, "on_true": "q_manual", "on_false": "n_urgent"},
      "n_urgent": {"type": "decision", "rev": 1, "implements": ["R2"], "condition": {"field": "urgent", "op": "==", "value": True}, "on_true": "q_priority", "on_false": "n_wait"},
      "n_wait": {"type": "decision", "rev": 2, "implements": ["R3"], "condition": {"field": "wait_minutes", "op": op, "value": th}, "on_true": "q_escalation", "on_false": "q_normal"},
      "q_manual": {"type": "route", "rev": 1, "route": "manual_review"},
      "q_priority": {"type": "route", "rev": 1, "route": "priority"},
      "q_escalation": {"type": "route", "rev": 1, "route": "escalation"},
      "q_normal": {"type": "route", "rev": 1, "route": "normal"},
    }
def base_code(th="30", op=">=", extra_comment=""):
    return f'''"""Ticket router (linked from routing-spec)."""


def route(ticket):
    # rule: R1
    if ticket.category is None:
        return "manual_review"
    # rule: R2
    if ticket.urgent:
        return "priority"
    # rule: R3{extra_comment}
    if ticket.wait_minutes {op} {th}:
        return "escalation"
    return "normal"
'''
def manifest(bid, desc, code=True, approvals=True, policy="policy.md", review_policy=None, code_files=None):
    arts = {"policy": policy, "spec": "spec.json", "workflow": "workflow.json"}
    if approvals: arts["approvals"] = "approvals.json"
    if code:
        arts["code"] = code_files or [{"artifact_id": "router", "path": "router.py", "version": "1.3", "entrypoint": "route", "linked_from": "routing-spec"}]
    m = {"bundle_id": bid, "description": desc, "domain": DOMAIN, "artifacts": arts}
    if review_policy: m["review_policy"] = review_policy
    return m
def approvals(records, version="1"):
    return {"artifact_id": "routing-approvals", "version": version, "records": records}
def write(bid, files):
    d = os.path.join(ROOT, bid); os.makedirs(d, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(content if isinstance(content, str) else json.dumps(content, indent=2) + "\n")
