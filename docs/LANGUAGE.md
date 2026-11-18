# Supported language and contracts

Everything outside this document is reported as **unsupported** and appears in
`coverage.unreviewed`. It is never dropped from coverage.

## 1. Bundle layout

```
<bundle>/
  manifest.json     bundle id, typed ticket domain, artifact list, review policy
  policy.md         anchored source requirements (versioned prose)
  approvals.json    approval records (the only source of approval authority)
  spec.json         structured specification
  workflow.json     versioned workflow graph
  router.py         linked Python snippet(s)
```

JSON Schemas for every artifact and for the report are in `schemas/`.

### Ticket domain (`manifest.json → domain`)

```json
"fields": {
  "category": {"type": "enum", "values": ["billing", "technical", "account"], "nullable": true},
  "urgent": {"type": "bool"},
  "wait_minutes": {"type": "number", "unit": "minutes", "min": 0, "aliases": ["wait", "waiting"]}
},
"routes": {"manual_review": ["manual review"], "priority": ["priority queue"], "...": []}
```

New fields, values, routes and IDs are accepted through this contract. Nothing is hard-coded to the example scenario.

## 2. Source requirements (`policy.md`)

* Header: `<!-- artifact: routing-policy, version: 1.0 -->` (artifact id + version).
* A requirement is a markdown list item `- <ID>: <sentence>`. Any ID token works (`R3`, `POL-A`, `rule_7`).
* **List order is precedence.** Every other line is a note. Notes are scanned for approval claims and embedded instructions. They never change behavior.
* Anchor = `file#Lline`, plus artifact id and version.

Supported sentence shapes (case-insensitive):

| Shape | Example |
|---|---|
| `<cond> -> <route>` | `Missing category -> manual review.` |
| `Otherwise, <cond> -> <route>` | `Otherwise, urgent ticket -> priority queue.` |
| `If <cond> then route to <route>` | `If wait is 30 minutes or more then route to escalation queue.` |
| `... , except when <cond>` | `If urgent is true then route to priority queue, except when category is billing.` |
| default | `All remaining tickets -> normal queue.` / `Otherwise route to normal queue.` |

Conditions: `and` binds tighter than `or`. Parentheses and a `not` prefix are supported. Clauses:

| Clause | Meaning |
|---|---|
| `<f> is missing`, `missing <f>`, `no <f>` | value is absent |
| `<f> is present` | value is not absent |
| `<f> is <v>`, `<f> is not <v>` | equality (bool / enum) |
| `<f> is one of [a, b]` | membership |
| `<bool-f> ticket` | `urgent ticket` means `urgent == true` |
| `<f> >= 30 minutes` (`>= <= > < == !=`) | numeric comparison |
| `<f> at least / at most / more than / less than / over / under / exactly N <unit>` | numeric comparison |
| `<f> is N <unit> or more / or less` | inclusive comparison |

Units: seconds, minutes, hours. They are converted exactly with rational arithmetic (0.5 hours = 30 minutes = 1800 seconds). A numeric clause on a field that declares a unit **must** state a unit.

Vague words are rejected as ambiguous: *about, around, approximately, roughly, within, recently, soon, upset, angry, maybe, probably, usually, reasonable, important*.

## 3. Structured condition language (spec + workflow)

```json
{"missing": "category"}            {"present": "category"}         {"const": true}
{"field": "wait_minutes", "op": ">=", "value": 0.5, "unit": "hours"}
{"field": "category", "op": "in", "value": ["billing", "account"]}
{"all": [ ... ]}   {"any": [ ... ]}   {"not": { ... }}
```

**Missing values:** any comparison on a missing value is false. Route missing values explicitly with `missing`.

## 4. Spec and workflow

* **Spec:** `rules[]` with `id`, `order` (precedence), `implements` (trace refs to requirement IDs), `when`, `route`, plus a `default`. Serialized order does not matter. `order` does.
* **Workflow:** `start` plus `nodes`. A decision node has `condition`, `on_true`, `on_false`, `rev` and `implements`. A route node has `route`. `implements` describes the *true* branch.
  * A missing or dangling edge gives `NO_ROUTE`.
  * A cycle gives `NO_ROUTE`.
  * Unconnected nodes and nodes no ticket can reach are reported.

## 5. Restricted Python subset (`router.py`)

These are three separate checks:
1. **Syntax check:** `ast.parse`.
2. **Inspection:** an AST whitelist.
3. **Interpretation:** our own evaluator walks the accepted AST.

The snippet is never passed to `exec`, `eval` or `import`.

* **Module level:** a docstring, `NAME = <literal>`, and one `def <entrypoint>(ticket):`.
* **Statements:** `if / elif / else`, `return <string literal or module constant>`, `pass`.
* **Expressions:**
  * `and / or / not`
  * chained comparisons, `in` / `not in` with literal collections, `is None` / `is not None`
  * `+ - * /` on numbers
  * `ticket.f`, `ticket["f"]`, `ticket.get("f")`
  * module constants and `True / False / None`
* **Python semantics are kept:**
  * Ordering a missing value (`None >= 30`) gives `RUNTIME_ERROR`.
  * Falling off the end gives `NO_ROUTE`.
* **Trace tags:** `# rule: R3` on the `if` line, or in the comment block directly above it.
* Comments never change behavior. Comments that look like instructions or approval claims are reported.

Anything else gives `unsupported_artifact_content` with a line number, and the artifact is marked unreviewed. For example: imports, calls, loops, assignments inside the function, attribute access on anything other than `ticket`.

## 6. Verification claim

Within this language every condition compares one field with constants. For code, linear expressions such as `wait * 60 >= 1800` are solved for the field. The reviewer builds one ticket per region between all constants used by any artifact, plus every bool/enum value and `None` for nullable fields. Comparing routes on those tickets is an **exhaustive** equivalence check for this language.

If the product of field values exceeds 20,000 tickets, the reviewer uses a seeded sample instead. The report then says `sampled` and can never return PASS.

Arbitrary prose and arbitrary Python are **not** verified.
