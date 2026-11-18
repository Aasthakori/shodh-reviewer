import pytest
from fractions import Fraction

from reviewer.conditions import normalize, to_text
from reviewer.prose import ProseError, Vocabulary, parse_rule

FIELDS = {"category": {"type": "enum", "values": ["billing", "technical", "account"], "nullable": True},
          "urgent": {"type": "bool"},
          "wait_minutes": {"type": "number", "unit": "minutes", "min": 0, "aliases": ["wait", "waiting"]}}
ROUTES = {"manual_review": ["manual review"], "priority": ["priority queue"], "escalation": ["escalation queue"],
          "normal": ["normal queue"]}
V = Vocabulary(FIELDS, ROUTES)


def parsed(text):
    r = parse_rule(text, V)
    return r.kind, (to_text(normalize(r.cond, FIELDS)) if r.cond else None), r.route


@pytest.mark.parametrize("text,expect", [
    ("R1: Missing category -> manual review.", ("rule", "category is missing", "manual_review")),
    ("Otherwise, urgent ticket -> priority queue.", ("rule", "urgent == True", "priority")),
    ("Otherwise, waiting >= 30 minutes -> escalation.", ("rule", "wait_minutes >= 30", "escalation")),
    ("All remaining tickets -> normal queue.", ("default", None, "normal")),
    ("If wait is at least 0.5 hours then route to escalation queue.", ("rule", "wait_minutes >= 30", "escalation")),
    ("If category is one of [billing, account] and urgent is false then route to normal queue.",
     ("rule", "(category in ['billing', 'account'] AND urgent == False)", "normal")),
    ("If urgent is true then route to priority queue, except when category is billing.",
     ("rule", "(urgent == True AND NOT category == 'billing')", "priority")),
])
def test_supported_patterns(text, expect):
    assert parsed(text) == expect


def test_regression_or_more_is_not_a_disjunction():
    # Debugging example 1: "30 minutes or more" was split on " or " and failed to parse.
    assert parsed("R3: If wait is 30 minutes or more then route to escalation queue.") == \
        ("rule", "wait_minutes >= 30", "escalation")
    assert parsed("If wait is 10 minutes or less or urgent is true then route to priority queue.") == \
        ("rule", "(wait_minutes <= 10 OR urgent == True)", "priority")


@pytest.mark.parametrize("text", [
    "If the customer seems upset then route to escalation queue.",
    "If waiting about 30 minutes then route to escalation queue.",
    "Waiting >= 30 -> escalation.",                 # missing declared time unit
    "If priority is high then route to escalation queue.",  # unknown field
    "Escalate tickets that look stuck.",
])
def test_unsupported_is_rejected_not_dropped(text):
    with pytest.raises(ProseError):
        parse_rule(text, V)


def test_units_are_exact():
    c = normalize({"field": "wait_minutes", "op": ">=", "value": 1800, "unit": "seconds"}, FIELDS)
    assert c["value"] == Fraction(30)
