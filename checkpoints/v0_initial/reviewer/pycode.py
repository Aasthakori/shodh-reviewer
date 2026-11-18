"""Restricted-Python inspection and interpretation for linked routing snippets.

Three separate steps, reported as separate checks:

1. syntax check      -> ast.parse() only (no import, no exec)
2. code inspection   -> walk the AST and reject anything outside the subset
3. interpretation    -> evaluate the *accepted* AST on synthetic tickets with
                        our own small interpreter. The snippet is never passed
                        to exec()/eval()/import, so untrusted code cannot run.

Supported subset (see docs/LANGUAGE.md):
  module level : docstring, NAME = <number|string|bool|None constant>, one def
  function     : def <entrypoint>(ticket): with if / elif / else, return
                 <string constant or module constant>, pass
  expressions  : and / or / not, chained comparisons (== != < <= > >= in,
                 not in, is None, is not None), + - * / on numbers,
                 ticket.f, ticket["f"], ticket.get("f"), tuple/list/set
                 literals of constants, True/False/None, module constants.
Comments are read with `tokenize` only to collect `# rule: R3` trace tags and
to scan for embedded instructions; comments never affect behavior.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .conditions import to_fraction

NO_ROUTE = "NO_ROUTE"
RUNTIME_ERROR = "RUNTIME_ERROR"
RULE_TAG = re.compile(r"#\s*(?:rule|implements)\s*:\s*([A-Za-z0-9_.,\s-]+)", re.I)


class Unsupported(Exception):
    def __init__(self, msg: str, line: int | None):
        super().__init__(msg)
        self.line = line


@dataclass
class CodeModel:
    path: str
    source: str
    func: ast.FunctionDef | None = None
    constants: dict[str, Any] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    tags: dict[int, list[str]] = field(default_factory=dict)   # line -> rule refs
    syntax_error: str | None = None
    unsupported: list[tuple[str, int | None]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.syntax_error is None and not self.unsupported and self.func is not None


def load(path: str, source: str, entrypoint: str = "route") -> CodeModel:
    cm = CodeModel(path=path, source=source)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        cm.syntax_error = f"line {exc.lineno}: {exc.msg}"
        return cm
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                cm.comments.append((tok.start[0], tok.string))
                m = RULE_TAG.search(tok.string)
                if m:
                    refs = [r.strip() for r in m.group(1).split(",") if r.strip()]
                    cm.tags[tok.start[0]] = refs
    except tokenize.TokenError:
        pass
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # docstring
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                cm.constants[node.targets[0].id] = _const_value(node.value)
            except Unsupported as exc:
                cm.unsupported.append((str(exc), exc.line))
            continue
        if isinstance(node, ast.FunctionDef) and node.name == entrypoint:
            if cm.func is not None:
                cm.unsupported.append((f"duplicate definition of {entrypoint}", node.lineno))
            cm.func = node
            continue
        cm.unsupported.append((f"unsupported top-level statement {type(node).__name__}", getattr(node, "lineno", None)))
    if cm.func is None:
        cm.unsupported.append((f"entrypoint function {entrypoint!r} not found", None))
    else:
        _inspect_function(cm)
    return cm


def _const_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool, type(None))):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value
    raise Unsupported("module constants must be literal numbers, strings, booleans or None",
                      getattr(node, "lineno", None))


def _inspect_function(cm: CodeModel) -> None:
    fn = cm.func
    args = fn.args
    if (len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs
            or fn.decorator_list):
        cm.unsupported.append(("entrypoint must take exactly one positional argument and no decorators", fn.lineno))
        return
    param = args.args[0].arg

    def stmt(s: ast.stmt) -> None:
        if isinstance(s, ast.If):
            expr(s.test)
            for b in s.body + s.orelse:
                stmt(b)
        elif isinstance(s, ast.Return):
            if s.value is None or not (
                    (isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
                    or (isinstance(s.value, ast.Name) and isinstance(cm.constants.get(s.value.id), str))):
                cm.unsupported.append(("return value must be a string constant", s.lineno))
        elif isinstance(s, ast.Pass):
            pass
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            pass  # docstring
        else:
            cm.unsupported.append((f"unsupported statement {type(s).__name__}", s.lineno))

    def expr(e: ast.expr) -> None:
        if isinstance(e, ast.BoolOp):
            for v in e.values:
                expr(v)
        elif isinstance(e, ast.UnaryOp) and isinstance(e.op, (ast.Not, ast.USub)):
            expr(e.operand)
        elif isinstance(e, ast.Compare):
            expr(e.left)
            for op, c in zip(e.ops, e.comparators):
                if not isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot)):
                    cm.unsupported.append((f"unsupported comparison {type(op).__name__}", e.lineno))
                if isinstance(op, (ast.Is, ast.IsNot)) and not (isinstance(c, ast.Constant) and c.value is None):
                    cm.unsupported.append(("'is' is only supported with None", e.lineno))
                expr(c)
        elif isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            expr(e.left)
            expr(e.right)
        elif isinstance(e, ast.Constant):
            if not isinstance(e.value, (int, float, str, bool, type(None))):
                cm.unsupported.append(("unsupported constant", e.lineno))
        elif isinstance(e, ast.Name):
            if e.id not in cm.constants and e.id not in ("True", "False", "None"):
                cm.unsupported.append((f"unknown name {e.id!r}", e.lineno))
        elif isinstance(e, (ast.Tuple, ast.List, ast.Set)):
            for elt in e.elts:
                if not isinstance(elt, ast.Constant) and not (isinstance(elt, ast.Name) and elt.id in cm.constants):
                    cm.unsupported.append(("collections may contain only constants", e.lineno))
        elif _field_name(e, param) is not None:
            pass
        else:
            cm.unsupported.append((f"unsupported expression {type(e).__name__}: {ast.unparse(e)}", e.lineno))

    for s in fn.body:
        stmt(s)
    cm.constants.setdefault("__param__", param)


def _field_name(e: ast.AST, param: str) -> str | None:
    """Return the ticket field accessed by `e`, or None if `e` is not a field access."""
    if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id == param:
        return e.attr
    if (isinstance(e, ast.Subscript) and isinstance(e.value, ast.Name) and e.value.id == param
            and isinstance(e.slice, ast.Constant) and isinstance(e.slice.value, str)):
        return e.slice.value
    if (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "get"
            and isinstance(e.func.value, ast.Name) and e.func.value.id == param
            and len(e.args) == 1 and not e.keywords
            and isinstance(e.args[0], ast.Constant) and isinstance(e.args[0].value, str)):
        return e.args[0].value
    return None


# --------------------------------------------------------------------------
# interpretation
# --------------------------------------------------------------------------
class _RuntimeFault(Exception):
    pass


@dataclass
class CodeResult:
    route: str
    line: int | None           # line of the return statement that produced the route
    branch_line: int | None    # line of the innermost `if` that led to it
    refs: list[str]
    error: str | None = None


def _make_ev(cm: CodeModel, ticket: dict):
    param = cm.constants["__param__"]

    def ev(e: ast.expr) -> Any:
        if isinstance(e, ast.Constant):
            v = e.value
            return to_fraction(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        if isinstance(e, ast.Name):
            if e.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[e.id]
            v = cm.constants[e.id]
            return to_fraction(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        f = _field_name(e, param)
        if f is not None:
            if isinstance(e, ast.Subscript) and f not in ticket:
                raise _RuntimeFault(f"KeyError: {f!r}")
            v = ticket.get(f)
            return to_fraction(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        if isinstance(e, ast.BoolOp):
            if isinstance(e.op, ast.And):
                result = True
                for v in e.values:
                    result = ev(v)
                    if not result:
                        return result
                return result
            result = False
            for v in e.values:
                result = ev(v)
                if result:
                    return result
            return result
        if isinstance(e, ast.UnaryOp):
            v = ev(e.operand)
            if isinstance(e.op, ast.Not):
                return not v
            if v is None or isinstance(v, (str, bool)):
                raise _RuntimeFault("TypeError: bad operand for unary -")
            return -v
        if isinstance(e, ast.BinOp):
            a, b = ev(e.left), ev(e.right)
            if not all(isinstance(x, Fraction) for x in (a, b)):
                raise _RuntimeFault(f"TypeError: arithmetic on {type(a).__name__} and {type(b).__name__}")
            if isinstance(e.op, ast.Add):
                return a + b
            if isinstance(e.op, ast.Sub):
                return a - b
            if isinstance(e.op, ast.Mult):
                return a * b
            if b == 0:
                raise _RuntimeFault("ZeroDivisionError")
            return a / b
        if isinstance(e, (ast.Tuple, ast.List, ast.Set)):
            return [ev(x) for x in e.elts]
        if isinstance(e, ast.Compare):
            left = ev(e.left)
            for op, comp in zip(e.ops, e.comparators):
                right = ev(comp)
                if not _compare(left, op, right):
                    return False
                left = right
            return True
        raise _RuntimeFault(f"unsupported expression at line {e.lineno}")

    return ev


def test_at(cm: CodeModel, line: int, ticket: dict) -> bool | None:
    """Evaluate, in isolation, the test of the `if` statement at `line`.
    Returns None when the test cannot be evaluated (runtime fault)."""
    for node in ast.walk(cm.func):
        if isinstance(node, ast.If) and node.lineno == line:
            try:
                return bool(_make_ev(cm, ticket)(node.test))
            except _RuntimeFault:
                return None
    return None


def tagged_if_lines(cm: CodeModel, ref: str) -> list[int]:
    """Lines of `if` statements whose trace tag (same or previous line) names `ref`."""
    out = []
    for node in ast.walk(cm.func):
        if isinstance(node, ast.If):
            for cand in (node.lineno, node.lineno - 1):
                if ref in cm.tags.get(cand, []):
                    out.append(node.lineno)
                    break
    return sorted(out)


def run(cm: CodeModel, ticket: dict, field_units: dict[str, str | None] | None = None) -> CodeResult:
    """Interpret the accepted snippet on a ticket. Requires cm.usable."""
    assert cm.usable, "run() called on unsupported code"
    path: list[int] = []
    ev = _make_ev(cm, ticket)

    def ex(stmts: list[ast.stmt]) -> tuple[str, int] | None:
        for s in stmts:
            if isinstance(s, ast.If):
                path.append(s.lineno)
                branch = s.body if ev(s.test) else s.orelse
                r = ex(branch)
                if r is not None:
                    return r
                path.pop()
            elif isinstance(s, ast.Return):
                v = s.value.value if isinstance(s.value, ast.Constant) else cm.constants[s.value.id]
                return v, s.lineno
        return None

    try:
        r = ex(cm.func.body)
    except _RuntimeFault as exc:
        return CodeResult(RUNTIME_ERROR, None, path[-1] if path else None, _refs_for(cm, path), str(exc))
    if r is None:
        return CodeResult(NO_ROUTE, None, path[-1] if path else None, _refs_for(cm, path))
    route, line = r
    return CodeResult(route, line, path[-1] if path else None, _refs_for(cm, path, line))


def _refs_for(cm: CodeModel, path: list[int], ret_line: int | None = None) -> list[str]:
    lines = ([ret_line] if ret_line else []) + list(reversed(path))
    for ln in lines:
        for cand in (ln, ln - 1):
            if cand in cm.tags:
                return cm.tags[cand]
    return []


def _compare(a: Any, op: ast.cmpop, b: Any) -> bool:
    if isinstance(op, ast.Is):
        return a is None if b is None else a is b
    if isinstance(op, ast.IsNot):
        return a is not None if b is None else a is not b
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    if isinstance(op, (ast.In, ast.NotIn)):
        if not isinstance(b, list):
            raise _RuntimeFault("TypeError: 'in' needs a collection")
        return (a in b) == isinstance(op, ast.In)
    # ordering: mimic Python, which raises TypeError for None / mixed types
    if a is None or b is None or isinstance(a, str) != isinstance(b, str) or isinstance(a, bool) or isinstance(b, bool):
        raise _RuntimeFault(f"TypeError: '{type(op).__name__}' not supported between {type(a).__name__} and {type(b).__name__}")
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Gt):
        return a > b
    return a >= b


# --------------------------------------------------------------------------
# constants for the partition (solve linear comparisons for the field)
# --------------------------------------------------------------------------
def thresholds(cm: CodeModel) -> list[tuple[str, Any]]:
    """Collect (field, value) boundary points from comparisons in the snippet.

    For comparisons of the form  a*field + b  <op>  c  the boundary is
    (c - b) / a, so `ticket.wait_minutes * 60 >= 1800` yields wait_minutes=30.
    """
    if cm.func is None:
        return []
    param = cm.constants.get("__param__", "ticket")
    out: list[tuple[str, Any]] = []

    def lin(e: ast.expr):
        """Return (field, a, b) for a*field+b, ('const', value) or None."""
        f = _field_name(e, param)
        if f is not None:
            return (f, Fraction(1), Fraction(0))
        if isinstance(e, ast.Constant):
            v = e.value
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return ("const", to_fraction(v))
            return ("const", v)
        if isinstance(e, ast.Name) and e.id in cm.constants:
            v = cm.constants[e.id]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return ("const", to_fraction(v))
            return ("const", v)
        if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.USub):
            r = lin(e.operand)
            if r and r[0] == "const" and isinstance(r[1], Fraction):
                return ("const", -r[1])
            if r and r[0] != "const":
                return (r[0], -r[1], -r[2])
            return None
        if isinstance(e, ast.BinOp):
            a, b = lin(e.left), lin(e.right)
            if not a or not b:
                return None
            if a[0] == "const" and b[0] == "const":
                if not all(isinstance(x[1], Fraction) for x in (a, b)):
                    return None
                return ("const", _arith(e.op, a[1], b[1]))
            if a[0] != "const" and b[0] == "const" and isinstance(b[1], Fraction):
                f, k, c = a
                if isinstance(e.op, ast.Mult):
                    return (f, k * b[1], c * b[1])
                if isinstance(e.op, ast.Div) and b[1] != 0:
                    return (f, k / b[1], c / b[1])
                if isinstance(e.op, ast.Add):
                    return (f, k, c + b[1])
                if isinstance(e.op, ast.Sub):
                    return (f, k, c - b[1])
            if b[0] != "const" and a[0] == "const" and isinstance(a[1], Fraction):
                f, k, c = b
                if isinstance(e.op, ast.Mult):
                    return (f, k * a[1], c * a[1])
                if isinstance(e.op, ast.Add):
                    return (f, k, c + a[1])
                if isinstance(e.op, ast.Sub):
                    return (f, -k, a[1] - c)
            return None
        if isinstance(e, (ast.Tuple, ast.List, ast.Set)):
            vals = [lin(x) for x in e.elts]
            if all(v and v[0] == "const" for v in vals):
                return ("const_list", [v[1] for v in vals])
        return None

    for node in ast.walk(cm.func):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left] + list(node.comparators)
        for left, right in zip(operands, operands[1:]):
            l, r = lin(left), lin(right)
            if not l or not r:
                continue
            if l[0] not in ("const", "const_list") and r[0] == "const_list":
                out += [(l[0], v) for v in r[1]]
                continue
            for side, other in ((l, r), (r, l)):
                if side[0] not in ("const", "const_list") and other[0] == "const":
                    f, k, c = side
                    if isinstance(other[1], Fraction) and k != 0:
                        out.append((f, (other[1] - c) / k))
                    elif not isinstance(other[1], Fraction):
                        out.append((f, other[1]))
    return out


def _arith(op: ast.operator, a: Fraction, b: Fraction) -> Fraction:
    if isinstance(op, ast.Add):
        return a + b
    if isinstance(op, ast.Sub):
        return a - b
    if isinstance(op, ast.Mult):
        return a * b
    return a / b if b != 0 else Fraction(0)


INJECTION = re.compile(
    r"\b(approved|approval|ignore (?:all|previous|the above)|mark (?:this|it|bundle)?\s*(?:as )?pass|"
    r"skip (?:the )?review|reviewer[:,]|override|do not report|status\s*[:=]\s*pass)\b", re.I)


def suspicious_comments(cm: CodeModel) -> list[tuple[int, str]]:
    return [(ln, text) for ln, text in cm.comments if INJECTION.search(text)]
