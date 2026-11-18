from common import *
import copy, json
NOAPP = approvals([])

# H01 valid: B01 with every ID renamed and serialized records reordered (semantic order preserved)
pol = BASE_POLICY.replace("R1:", "POL-A:").replace("R2:", "POL-B:").replace("R3:", "POL-C:").replace("DEFAULT:", "FALLBACK:")
sr = copy.deepcopy(BASE_SPEC_RULES)
for r, (nid, ref) in zip(sr, [("RULE-M", "POL-A"), ("RULE-U", "POL-B"), ("RULE-W", "POL-C")]):
    r["id"], r["implements"] = nid, [ref]
sr.reverse()
n = base_nodes()
ren = {"n_category": "chk_cat", "n_urgent": "chk_urg", "n_wait": "chk_wait", "q_manual": "to_manual", "q_priority": "to_prio", "q_escalation": "to_esc", "q_normal": "to_norm"}
refs = {"n_category": "POL-A", "n_urgent": "POL-B", "n_wait": "POL-C"}
nn = {}
for k in reversed(list(n)):
    v = copy.deepcopy(n[k])
    for e in ("on_true", "on_false"):
        if e in v: v[e] = ren[v[e]]
    if k in refs: v["implements"] = [refs[k]]
    nn[ren[k]] = v
code = base_code().replace("rule: R1", "rule: POL-A").replace("rule: R2", "rule: POL-B").replace("rule: R3", "rule: POL-C")
write("H01_valid_rename_reorder", {"manifest.json": manifest("H01_valid_rename_reorder", "Pair of B01: IDs renamed everywhere, spec rules and workflow nodes serialized in reverse (order fields kept)."),
  "policy.md": pol, "approvals.json": NOAPP, "spec.json": spec(sr, default_impl=("FALLBACK",)), "workflow.json": wf(nn, start="chk_cat"), "router.py": code})

# H02 defect: two individually plausible spec rules interact badly
sr2 = copy.deepcopy(BASE_SPEC_RULES)
sr2[1]["when"] = {"all": [{"field": "urgent", "op": "==", "value": True}, {"field": "wait_minutes", "op": "<", "value": 30}]}
sr2[1]["note"] = "urgent and still fresh"
sr2[2]["note"] = "anything waiting 30+ escalates"
write("H02_defect_multi_rule_interaction", {"manifest.json": manifest("H02_defect_multi_rule_interaction", "Spec S2 (urgent and wait<30 -> priority) and S3 (wait>=30 -> escalation) each look plausible; together urgent tickets waiting 30+ go to escalation."),
  "policy.md": BASE_POLICY, "approvals.json": NOAPP, "spec.json": spec(sr2), "workflow.json": wf(base_nodes()), "router.py": base_code()})

# H03 defect: workflow decision missing its false branch
n3 = base_nodes(); del n3["n_wait"]["on_false"]; del n3["q_normal"]
write("H03_defect_workflow_missing_branch", {"manifest.json": manifest("H03_defect_workflow_missing_branch", "Workflow wait decision has no false branch; short-wait tickets get no route."),
  "policy.md": BASE_POLICY, "approvals.json": NOAPP, "spec.json": spec(BASE_SPEC_RULES), "workflow.json": wf(n3), "router.py": base_code()})

# H04 incomplete: router uses constructs outside the restricted subset
code4 = '''"""Router that computes wait time itself."""
import datetime


def route(ticket):
    # rule: R1
    if ticket.category is None:
        return "manual_review"
    # rule: R2
    if ticket.urgent:
        return "priority"
    waited = (datetime.datetime.now() - ticket.created_at).total_seconds() / 60
    # rule: R3
    if waited >= 30:
        return "escalation"
    return "normal"
'''
write("H04_incomplete_unsupported_code", {"manifest.json": manifest("H04_incomplete_unsupported_code", "Router imports datetime and computes its own wait; not verifiable in the restricted subset."),
  "policy.md": BASE_POLICY, "approvals.json": NOAPP, "spec.json": spec(BASE_SPEC_RULES), "workflow.json": wf(base_nodes()), "router.py": code4})

# H05 defect: units - spec in hours (correct), code forgets the conversion
sr5 = copy.deepcopy(BASE_SPEC_RULES); sr5[2]["when"] = {"field": "wait_minutes", "op": ">=", "value": 0.5, "unit": "hours"}
code5 = base_code().replace("ticket.wait_minutes >= 30", "ticket.wait_minutes >= 0.5  # half an hour")
write("H05_defect_code_unit_error", {"manifest.json": manifest("H05_defect_code_unit_error", "Spec states 0.5 hours correctly; code compares minutes against 0.5."),
  "policy.md": BASE_POLICY, "approvals.json": NOAPP, "spec.json": spec(sr5), "workflow.json": wf(base_nodes()), "router.py": code5})

# H06 valid: B01 with the policy paraphrased inside the supported language
pol6 = """<!-- artifact: routing-policy, version: 1.0 -->
# Ticket routing policy

Rules are applied top to bottom.

- R1: If category is missing then route to manual review.
- R2: If urgent is true then route to priority queue.
- R3: If waiting time is at least 30 minutes then route to escalation queue.
- DEFAULT: Otherwise route to normal queue.
"""
write("H06_valid_paraphrase", {"manifest.json": manifest("H06_valid_paraphrase", "Pair of B01: policy paraphrased within the supported language."),
  "policy.md": pol6, "approvals.json": NOAPP, "spec.json": spec(BASE_SPEC_RULES), "workflow.json": wf(base_nodes()), "router.py": base_code()})

# H07 defect: pair of B01 that changes behavior - code sends urgent tickets to the wrong queue
write("H07_defect_wrong_destination", {"manifest.json": manifest("H07_defect_wrong_destination", "Pair of B01: router sends urgent tickets to escalation instead of priority; metadata correct."),
  "policy.md": BASE_POLICY, "approvals.json": NOAPP, "spec.json": spec(BASE_SPEC_RULES), "workflow.json": wf(base_nodes()),
  "router.py": base_code().replace('return "priority"', 'return "escalation"')})
print("held-out written")
