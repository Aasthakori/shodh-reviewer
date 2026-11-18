"""Independent routing oracle used ONLY by the evaluation harness.

Written by hand from the policy text and the approval records of each case,
without importing anything from `reviewer/`. Labels are checked against it,
so expected outcomes are not derived from the reviewer itself.
"""


def base_policy(t, threshold=30):
    """R1 missing category -> manual; R2 urgent -> priority;
    R3 wait >= threshold minutes -> escalation; else normal."""
    if t.get("category") is None:
        return "manual_review"
    if t.get("urgent") is True:
        return "priority"
    if t.get("wait_minutes") is not None and t["wait_minutes"] >= threshold:
        return "escalation"
    return "normal"


def policy_for(spec):
    kind = spec["policy"]
    if kind == "base":
        return lambda t: base_policy(t, spec.get("threshold", 30))
    if kind == "undefined":       # expected route cannot be determined (ambiguity / conflict)
        return None
    raise ValueError(kind)
