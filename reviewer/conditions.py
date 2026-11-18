"""Structured condition language (the reviewer's intermediate representation).

Every artifact (prose requirements, spec, workflow, Python) is translated into
this small language before it is compared. A condition is a JSON object:

    {"missing": "category"}                     field value is absent (None)
    {"present": "category"}                     field value is not absent
    {"field": "wait_minutes", "op": ">=", "value": 30, "unit": "minutes"}
    {"field": "category", "op": "==", "value": "billing"}
    {"field": "category", "op": "in", "value": ["billing", "tech"]}
    {"all": [c1, c2, ...]}                      nested AND
    {"any": [c1, c2, ...]}                      nested OR
    {"not": c}
    {"const": true}

Semantics (documented in docs/LANGUAGE.md):
  * Any comparison on a missing value is False ("unknown is not a match").
    Use {"missing": f} to route missing values explicitly.
  * Numeric values are converted to the field's declared unit with exact
    rational arithmetic (fractions.Fraction), so 0.5 hours == 30 minutes.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable

CMP_OPS = ("==", "!=", "<", "<=", ">", ">=")
UNIT_FACTORS = {  # factor to convert into minutes
    "seconds": Fraction(1, 60), "second": Fraction(1, 60), "secs": Fraction(1, 60), "sec": Fraction(1, 60),
    "minutes": Fraction(1), "minute": Fraction(1), "mins": Fraction(1), "min": Fraction(1),
    "hours": Fraction(60), "hour": Fraction(60), "hrs": Fraction(60), "hr": Fraction(60),
}


class ConditionError(ValueError):
    """Raised when a condition is malformed or refers to an unknown field."""


def to_fraction(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise ConditionError(f"boolean {value!r} used as a number")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    if isinstance(value, str):
        return Fraction(value)
    raise ConditionError(f"not a number: {value!r}")


def convert_units(value: Any, from_unit: str | None, to_unit: str | None) -> Fraction:
    v = to_fraction(value)
    if from_unit is None or to_unit is None or from_unit == to_unit:
        if from_unit is not None and to_unit is None:
            raise ConditionError(f"unit {from_unit!r} given for a unitless field")
        return v
    if from_unit not in UNIT_FACTORS or to_unit not in UNIT_FACTORS:
        raise ConditionError(f"unsupported unit conversion {from_unit!r} -> {to_unit!r}")
    return v * UNIT_FACTORS[from_unit] / UNIT_FACTORS[to_unit]


def normalize(cond: dict, fields: dict) -> dict:
    """Validate a condition and convert numeric constants to field units.

    Returns a new condition whose numeric values are Fractions in the field's
    declared unit and which carries no "unit" key.
    """
    if not isinstance(cond, dict) or len(cond) == 0:
        raise ConditionError(f"condition must be a non-empty object: {cond!r}")
    if "const" in cond:
        return {"const": bool(cond["const"])}
    if "missing" in cond or "present" in cond:
        key = "missing" if "missing" in cond else "present"
        f = cond[key]
        if f not in fields:
            raise ConditionError(f"unknown field {f!r}")
        return {key: f}
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        items = cond[key]
        if not isinstance(items, list) or not items:
            raise ConditionError(f"{key} needs a non-empty list")
        return {key: [normalize(c, fields) for c in items]}
    if "not" in cond:
        return {"not": normalize(cond["not"], fields)}
    if "field" in cond:
        f = cond["field"]
        if f not in fields:
            raise ConditionError(f"unknown field {f!r}")
        spec = fields[f]
        op = cond.get("op")
        val = cond.get("value")
        ftype = spec["type"]
        if op == "in":
            if not isinstance(val, list) or not val:
                raise ConditionError("'in' needs a non-empty list")
            if ftype == "number":
                vals = [convert_units(v, cond.get("unit", spec.get("unit")), spec.get("unit")) for v in val]
            else:
                vals = list(val)
            return {"field": f, "op": "in", "value": vals}
        if op not in CMP_OPS:
            raise ConditionError(f"unsupported operator {op!r}")
        if ftype == "number":
            v = convert_units(val, cond.get("unit", spec.get("unit")), spec.get("unit"))
        elif ftype == "bool":
            if op not in ("==", "!="):
                raise ConditionError(f"operator {op} not valid for boolean field {f}")
            if not isinstance(val, bool):
                raise ConditionError(f"boolean field {f} compared with {val!r}")
            v = val
        elif ftype == "enum":
            if op not in ("==", "!="):
                raise ConditionError(f"operator {op} not valid for enum field {f}")
            v = val
        else:
            raise ConditionError(f"unknown field type {ftype!r}")
        return {"field": f, "op": op, "value": v}
    raise ConditionError(f"unrecognised condition {cond!r}")


def _cmp(a: Any, op: str, b: Any) -> bool:
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    raise ConditionError(op)


def evaluate(cond: dict, ticket: dict) -> bool:
    """Evaluate a *normalized* condition on a ticket (dict of field -> value)."""
    if "const" in cond:
        return cond["const"]
    if "missing" in cond:
        return ticket.get(cond["missing"]) is None
    if "present" in cond:
        return ticket.get(cond["present"]) is not None
    if "all" in cond:
        return all(evaluate(c, ticket) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, ticket) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], ticket)
    value = ticket.get(cond["field"])
    if value is None:
        return False  # comparisons on missing values never match
    if cond["op"] == "in":
        return value in cond["value"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = to_fraction(value)
    return _cmp(value, cond["op"], cond["value"])


def constants(cond: dict) -> Iterable[tuple[str, Any]]:
    """Yield (field, constant) pairs referenced by a normalized condition."""
    if "all" in cond or "any" in cond:
        for c in cond.get("all", cond.get("any")):
            yield from constants(c)
    elif "not" in cond:
        yield from constants(cond["not"])
    elif "field" in cond:
        if cond["op"] == "in":
            for v in cond["value"]:
                yield cond["field"], v
        else:
            yield cond["field"], cond["value"]


def to_text(cond: dict) -> str:
    """Human-readable rendering used in findings."""
    if "const" in cond:
        return "always" if cond["const"] else "never"
    if "missing" in cond:
        return f"{cond['missing']} is missing"
    if "present" in cond:
        return f"{cond['present']} is present"
    if "all" in cond:
        return "(" + " AND ".join(to_text(c) for c in cond["all"]) + ")"
    if "any" in cond:
        return "(" + " OR ".join(to_text(c) for c in cond["any"]) + ")"
    if "not" in cond:
        return f"NOT {to_text(cond['not'])}"
    v = cond["value"]
    if isinstance(v, Fraction):
        v = fmt_num(v)
    elif isinstance(v, list):
        v = "[" + ", ".join(fmt_num(x) if isinstance(x, Fraction) else repr(x) for x in v) + "]"
    else:
        v = repr(v)
    return f"{cond['field']} {cond['op']} {v}"


def fmt_num(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    return str(float(v))


def jsonable(obj: Any) -> Any:
    """Convert Fractions (and nested containers) to JSON-safe values."""
    if isinstance(obj, Fraction):
        return int(obj) if obj.denominator == 1 else float(obj)
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj
