"""Load a review bundle from disk, validate it against the JSON schemas and
record artifact versions + content hashes for provenance and caching.

Nothing is silently dropped: missing, oversized or schema-invalid artifacts
are recorded in `Bundle.problems` and later surface as INCOMPLETE coverage.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
MAX_FILE_BYTES = 200_000          # oversized artifacts are reported, never truncated
POLICY_HEADER = re.compile(r"<!--\s*artifact:\s*([\w.-]+)\s*,\s*version:\s*([\w.-]+)\s*-->")
POLICY_LINE = re.compile(r"^\s*(?:[-*]\s*)?\**([A-Za-z][A-Za-z0-9_.-]*)\**\s*:\s*(.+?)\s*$")


@dataclass
class Problem:
    artifact: str
    location: str
    message: str
    kind: str          # missing_artifact | oversized | schema_error | parse_error


@dataclass
class Bundle:
    root: Path
    manifest: dict
    fields: dict
    routes: dict
    policy_id: str = "policy"
    policy_version: str | None = None
    policy_file: str = ""
    policy_records: list[dict] = field(default_factory=list)
    policy_notes: list[dict] = field(default_factory=list)
    approvals: dict | None = None
    spec: dict | None = None
    workflow: dict | None = None
    code: list[dict] = field(default_factory=list)      # {meta, source}
    hashes: dict[str, str] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)  # artifact file -> raw text
    problems: list[Problem] = field(default_factory=list)


def _registry() -> Registry:
    reg = Registry()
    for p in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(p.read_text())
        reg = reg.with_resource(schema["$id"], Resource.from_contents(schema))
    return reg


_REG = None


def validate(obj: dict, schema_name: str) -> list[str]:
    global _REG
    if _REG is None:
        _REG = _registry()
    schema = json.loads((SCHEMA_DIR / f"{schema_name}.schema.json").read_text())
    v = jsonschema.Draft202012Validator(schema, registry=_REG)
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in v.iter_errors(obj)]


def _read(bundle: Bundle, rel: str, artifact: str) -> str | None:
    path = bundle.root / rel
    if not path.is_file():
        bundle.problems.append(Problem(artifact, rel, f"artifact file {rel} not found", "missing_artifact"))
        return None
    raw = path.read_bytes()
    bundle.hashes[rel] = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_FILE_BYTES:
        bundle.problems.append(Problem(artifact, rel, f"{rel} is {len(raw)} bytes (limit {MAX_FILE_BYTES}); "
                                       "not reviewed rather than truncated", "oversized"))
        return None
    text = raw.decode("utf-8")
    bundle.texts[rel] = text
    return text


def _json(bundle: Bundle, rel: str, artifact: str, schema: str) -> dict | None:
    text = _read(bundle, rel, artifact)
    if text is None:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        bundle.problems.append(Problem(artifact, rel, f"invalid JSON: {exc}", "parse_error"))
        return None
    errs = validate(obj, schema)
    if errs:
        for e in errs[:10]:
            bundle.problems.append(Problem(artifact, f"{rel}#{e.split(':')[0]}", e, "schema_error"))
        return None
    return obj


def load_bundle(root: str | Path) -> Bundle:
    root = Path(root)
    mpath = root / "manifest.json"
    manifest = json.loads(mpath.read_text())
    errs = validate(manifest, "manifest")
    if errs:
        raise ValueError(f"invalid manifest {mpath}: {errs}")
    b = Bundle(root=root, manifest=manifest, fields=manifest["domain"]["fields"],
               routes=manifest["domain"]["routes"])
    b.hashes["manifest.json"] = hashlib.sha256(mpath.read_bytes()).hexdigest()
    arts = manifest["artifacts"]

    # policy (anchored prose requirements)
    b.policy_file = arts["policy"]
    text = _read(b, arts["policy"], "policy")
    if text is not None:
        _parse_policy(b, text)

    if "approvals" in arts:
        b.approvals = _json(b, arts["approvals"], "approvals", "approvals")
    for name in ("spec", "workflow"):
        if name in arts:
            setattr(b, name, _json(b, arts[name], name, name))
    for meta in arts.get("code", []):
        src = _read(b, meta["path"], meta["artifact_id"])
        if src is not None:
            b.code.append({"meta": meta, "source": src})
    return b


def _parse_policy(b: Bundle, text: str) -> None:
    lines = text.splitlines()
    for ln in lines[:5]:
        m = POLICY_HEADER.search(ln)
        if m:
            b.policy_id, b.policy_version = m.group(1), m.group(2)
            break
    if b.policy_version is None:
        b.problems.append(Problem("policy", f"{b.policy_file}#L1",
                                  "policy has no '<!-- artifact: id, version: v -->' header", "parse_error"))
        b.policy_version = "unversioned"
    order = 0
    seen = set()
    for i, ln in enumerate(lines, start=1):
        if not ln.strip() or ln.strip().startswith("#") or POLICY_HEADER.search(ln):
            continue
        m = POLICY_LINE.match(ln)
        anchor = {"artifact": b.policy_id, "version": b.policy_version, "file": b.policy_file, "line": i}
        if m and (re.fullmatch(r"[A-Za-z]+\d+[A-Za-z0-9_.-]*", m.group(1)) or m.group(1).upper() == "DEFAULT"):
            rid = m.group(1)
            if rid in seen:
                b.problems.append(Problem("policy", f"{b.policy_file}#L{i}", f"duplicate requirement id {rid}", "parse_error"))
                continue
            seen.add(rid)
            order += 1
            rec = {"id": rid, "order": order, "text": m.group(2), "anchor": anchor}
            errs = validate(rec, "requirement")
            if errs:
                b.problems.append(Problem("policy", f"{b.policy_file}#L{i}", "; ".join(errs), "schema_error"))
                continue
            b.policy_records.append(rec)
        else:
            b.policy_notes.append({"text": ln.strip(), "anchor": anchor})
