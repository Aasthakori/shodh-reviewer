"""Parser for the supported source-requirement language (controlled English).

Supported sentence shapes (case-insensitive, trailing period optional):

    If <cond> then route to <route>.
    If <cond> then route to <route>, except when <cond>.
    <cond> -> <route>.
    Otherwise, <cond> -> <route>.          ("Otherwise," only restates order)
    All remaining tickets -> <route>.       (default route)
    Otherwise route to <route>.             (default route)

<cond> is built from clauses joined by "and" / "or" ("and" binds tighter),
with optional parentheses and a "not" prefix. Clauses:

    <field> is missing | missing <field> | no <field>
    <field> is present
    <field> is <value> | <field> is not <value>
    <field> is one of [v1, v2, ...]
    <bool-field> ticket                       e.g. "urgent ticket"
    <field> <op> <number> [unit]              op: >= <= > < == !=
    <field> at least|at most|more than|less than|over|under|exactly <number> [unit]
    <field> <number> [unit] or more|or less

Anything else is returned as UNSUPPORTED with a reason; it is never dropped.
Words that signal vague policy ("about", "around", "within", "recently",
"soon", "upset", "maybe") are rejected explicitly as ambiguous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .conditions import UNIT_FACTORS

AMBIGUOUS = ("about", "around", "approximately", "roughly", "within", "recently", "soon",
             "upset", "angry", "maybe", "probably", "usually", "reasonable", "important")

WORD_OPS = [
    ("greater than or equal to", ">="), ("less than or equal to", "<="),
    ("at least", ">="), ("at most", "<="), ("no more than", "<="), ("no less than", ">="),
    ("more than", ">"), ("greater than", ">"), ("over", ">"), ("above", ">"),
    ("less than", "<"), ("fewer than", "<"), ("under", "<"), ("below", "<"),
    ("exactly", "=="), ("equal to", "=="),
]
SYM_OPS = (">=", "<=", "==", "!=", ">", "<", "=")
NUM = r"(-?\d+(?:\.\d+)?)"
UNIT = r"(?:\s*(" + "|".join(sorted(UNIT_FACTORS, key=len, reverse=True)) + r"))?"


class ProseError(ValueError):
    pass


@dataclass
class ParsedRule:
    kind: str                 # "rule" | "default"
    cond: dict | None         # raw (un-normalized) condition, None for default
    route: str


class Vocabulary:
    """Maps surface words to declared field and route names."""

    def __init__(self, fields: dict, routes: dict):
        self.fields = fields
        self.field_alias: dict[str, str] = {}
        for name, spec in fields.items():
            for alias in [name, name.replace("_", " ")] + list(spec.get("aliases", [])):
                self.field_alias[alias.lower()] = name
        self.route_alias: dict[str, str] = {}
        for name, aliases in routes.items():
            for alias in [name, name.replace("_", " ")] + list(aliases):
                self.route_alias[alias.lower()] = name

    def field(self, words: str) -> str:
        w = words.strip().lower()
        if w.startswith("the "):
            w = w[4:]
        if w in self.field_alias:
            return self.field_alias[w]
        raise ProseError(f"unknown field phrase {words.strip()!r}")

    def route(self, words: str) -> str:
        w = words.strip().lower().rstrip(".").strip()
        for prefix in ("the ", "a "):
            if w.startswith(prefix):
                w = w[len(prefix):]
        if w in self.route_alias:
            return self.route_alias[w]
        raise ProseError(f"unknown route phrase {words.strip()!r}")


def _split_top(text: str, word: str) -> list[str]:
    parts, depth, buf, i = [], 0, "", 0
    token = f" {word} "
    low = text.lower()
    while i < len(text):
        ch = text[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if depth == 0 and low.startswith(token, i):
            parts.append(buf)
            buf = ""
            i += len(token)
            continue
        buf += ch
        i += 1
    parts.append(buf)
    return [p.strip() for p in parts]


def _strip_parens(text: str) -> str:
    t = text.strip()
    while t.startswith("(") and t.endswith(")"):
        depth = 0
        for i, ch in enumerate(t):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(t) - 1:
                return t
        t = t[1:-1].strip()
    return t


def _value(word: str, field: str, vocab: Vocabulary) -> Any:
    spec = vocab.fields[field]
    w = word.strip().strip("'\"").lower()
    if spec["type"] == "bool":
        if w in ("true", "yes"):
            return True
        if w in ("false", "no"):
            return False
        raise ProseError(f"{word!r} is not a boolean")
    if spec["type"] == "enum":
        values = [v.lower() for v in spec.get("values", [])]
        if w not in values:
            raise ProseError(f"{word!r} is not a declared value of {field}")
        return spec["values"][values.index(w)]
    raise ProseError(f"cannot use {word!r} as a value of numeric field {field}")


def parse_condition(text: str, vocab: Vocabulary) -> dict:
    t = _strip_parens(text)
    ors = _split_top(t, "or")
    if len(ors) > 1:
        return {"any": [parse_condition(p, vocab) for p in ors]}
    ands = _split_top(t, "and")
    if len(ands) > 1:
        return {"all": [parse_condition(p, vocab) for p in ands]}
    return _clause(t, vocab)


def _clause(text: str, vocab: Vocabulary) -> dict:
    t = _strip_parens(text).strip()
    low = t.lower()
    if low.startswith("not "):
        return {"not": parse_condition(t[4:], vocab)}
    for word in AMBIGUOUS:
        if re.search(rf"\b{word}\b", low):
            raise ProseError(f"ambiguous wording {word!r} in {t!r}")
    m = re.fullmatch(r"(?:missing|no)\s+(.+)", low)
    if m:
        return {"missing": vocab.field(m.group(1))}
    m = re.fullmatch(r"(.+?)\s+is\s+(missing|absent|empty)", low)
    if m:
        return {"missing": vocab.field(m.group(1))}
    m = re.fullmatch(r"(.+?)\s+is\s+present", low)
    if m:
        return {"present": vocab.field(m.group(1))}
    m = re.fullmatch(r"(.+?)\s+is\s+(not\s+)?one\s+of\s+\[(.+)\]", low)
    if m:
        f = vocab.field(m.group(1))
        vals = [_value(v, f, vocab) for v in m.group(3).split(",")]
        cond = {"field": f, "op": "in", "value": vals}
        return {"not": cond} if m.group(2) else cond
    # numeric with symbol operator
    m = re.fullmatch(r"(.+?)\s*(>=|<=|==|!=|>|<|=)\s*" + NUM + UNIT, low)
    if m:
        f = vocab.field(m.group(1))
        op = "==" if m.group(2) == "=" else m.group(2)
        return _num(f, op, m.group(3), m.group(4), vocab)
    # numeric with word operator
    for phrase, op in WORD_OPS:
        m = re.fullmatch(r"(.+?)\s+(?:is\s+)?" + re.escape(phrase) + r"\s+" + NUM + UNIT, low)
        if m:
            return _num(vocab.field(m.group(1)), op, m.group(2), m.group(3), vocab)
    m = re.fullmatch(r"(.+?)\s+(?:is\s+)?" + NUM + UNIT + r"\s+or\s+(more|longer|less|fewer|shorter)", low)
    if m:
        op = ">=" if m.group(4) in ("more", "longer") else "<="
        return _num(vocab.field(m.group(1)), op, m.group(2), m.group(3), vocab)
    m = re.fullmatch(r"(.+?)\s+is\s+(not\s+)?(.+)", low)
    if m:
        f = vocab.field(m.group(1))
        return {"field": f, "op": "!=" if m.group(2) else "==", "value": _value(m.group(3), f, vocab)}
    m = re.fullmatch(r"(.+?)\s+tickets?", low)
    if m:
        f = vocab.field(m.group(1))
        if vocab.fields[f]["type"] != "bool":
            raise ProseError(f"'{m.group(1)} ticket' only works for boolean fields")
        return {"field": f, "op": "==", "value": True}
    raise ProseError(f"unsupported clause {t!r}")


def _num(field: str, op: str, number: str, unit: str | None, vocab: Vocabulary) -> dict:
    spec = vocab.fields[field]
    if spec["type"] != "number":
        raise ProseError(f"numeric comparison on non-numeric field {field}")
    if spec.get("unit") and not unit:
        raise ProseError(f"missing time unit for {field} (declared unit: {spec['unit']})")
    cond = {"field": field, "op": op, "value": number}
    if unit:
        cond["unit"] = unit
    return cond


def parse_rule(text: str, vocab: Vocabulary) -> ParsedRule:
    """Parse one requirement sentence. Raises ProseError if unsupported."""
    t = " ".join(text.strip().split())
    t = re.sub(r"^[A-Za-z]*\d+[A-Za-z0-9_-]*\s*[:.)-]\s+", "", t)  # leading "R3:" label
    t = t.rstrip(".").strip()
    t = re.sub(r"^otherwise,?\s+(?=.*->)", "", t, flags=re.I)
    low = t.lower()
    m = re.fullmatch(r"(?:all\s+)?(?:remaining|other)\s+tickets?\s*->\s*(.+)", low) or \
        re.fullmatch(r"otherwise,?\s+(?:route\s+)?to\s+(.+)", low) or \
        re.fullmatch(r"otherwise,?\s+route\s+(?:to\s+)?(.+)", low)
    if m:
        return ParsedRule("default", None, vocab.route(m.group(1)))
    m = re.fullmatch(r"if\s+(.+?)\s*,?\s+then\s+route\s+(?:it\s+)?to\s+(.+?)(?:\s*,?\s+except\s+when\s+(.+))?", t, flags=re.I)
    if m:
        cond = parse_condition(m.group(1), vocab)
        if m.group(3):
            cond = {"all": [cond, {"not": parse_condition(m.group(3), vocab)}]}
        return ParsedRule("rule", cond, vocab.route(m.group(2)))
    if "->" in t:
        lhs, rhs = t.rsplit("->", 1)
        return ParsedRule("rule", parse_condition(lhs, vocab), vocab.route(rhs))
    raise ProseError(f"sentence shape not supported: {text.strip()!r}")
