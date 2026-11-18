# Engineering note: AI-assisted workflow reviewer

**Aastha Kori · Shodh AI, AI/ML Engineer Intern take-home · November 2026**

## 1. What it does and supported scope

The reviewer takes a bundle and checks whether every artifact implements the effective requirements. A bundle contains a versioned prose policy, approval records, a structured spec, a workflow graph, and Python snippets.

It returns a JSON report with:
* status (`PASS`, `CHANGES_REQUIRED` or `INCOMPLETE`),
* a summary,
* findings, each with severity, rule ref, artifact, version, location, evidence and a correction,
* coverage,
* check results,
* artifact hashes and model provenance.

Scope is declared in `docs/LANGUAGE.md`:
* a controlled-English requirement language,
* a JSON condition language (nested AND/OR/NOT, missing/present, `== != < <= > >= in`, units),
* a restricted Python subset.

Content outside this scope is reported as unreviewed. **Arbitrary prose and arbitrary Python are not verified.**

## 2. Design choices

**One intermediate representation.** Every artifact is translated into the same condition language. The policy goes through `prose.py`, spec and workflow through `conditions.normalize`, and Python through `pycode.py`. Each artifact then becomes a routing function over a typed ticket domain declared in the manifest. Comparing behavior, not text, is what lets the reviewer accept equivalent formulations and reject look-alikes. Examples:
* `not (wait < 0.5 h)`, `wait > 30 or wait == 1800 s` and `ticket.wait_minutes * 60 >= 1800` all count as `wait >= 30`.
* Matching IDs or numeric substrings is never used as evidence.

**Effective requirements** (`effective.py`):
* Only top-level approval records carry authority. Approval claims in prose or code comments are reported and ignored.
* A record applies only if it targets the policy artifact *and version* under review. Other records are reported as "out of revision scope".
* Applicable approved records apply in ascending `seq`.
* Two approved records that change the same rule conflict unless the later one names the earlier in `supersedes`. Equal `seq` with different content, or a reused `change_id`, is always a conflict.
* Conflicts are never resolved by picking one. Each admissible reading becomes a *candidate* effective policy (capped at 8).

**Status rules** (`review.py`):
* A blocker needs a concrete ticket whose route differs from the expected route **under every candidate reading**.
* If a mismatch exists under only some readings, the result is INCOMPLETE.
* `PASS` requires all of the following:
  * every effective requirement compared exhaustively against every declared artifact,
  * no unreviewed scope,
  * no failed required check.
* Unsupported requirements, unsupported code, schema errors, missing or oversized files, approval conflicts, sampled domains and a failed *required* model call all add to `coverage.unreviewed`. That gives INCOMPLETE unless a blocker exists. In that case the result is CHANGES_REQUIRED and the unreviewed scope is still listed.
* Oversized inputs are reported, never truncated.

**Code handling.** Syntax checking (`ast.parse`), inspection (AST whitelist) and interpretation (our own evaluator) are separate checks. The snippet is never executed or imported. The interpreter keeps Python semantics: `None >= 30` gives `RUNTIME_ERROR`, falling off the end gives `NO_ROUTE`.

**Caching.** The cache key is the SHA-256 of every byte in the bundle, plus the review config and the reviewer version. A changed artifact or approval cannot reuse a stale report. `test_cache_never_reuses_stale_pass` withdraws an approval and checks that the cached PASS becomes CHANGES_REQUIRED.

## 3. Semantic checks: why they are exhaustive within scope

In the supported language every atomic condition compares one field with a constant. Linear code expressions are solved for the field. So each condition's truth value is constant on the regions between the constants.

`semantic.partition` collects every constant used by *any* artifact or candidate requirement. It then builds one ticket per region:
* each constant,
* the midpoint between neighbours,
* one value below and one above, clipped to `min`/`max`,
* every bool and enum value,
* `None` for nullable fields.

If two routing functions agree on all these tickets, they agree on the whole domain for this language. So the comparison is an equivalence proof, not a sample. The base scenario needs 24 tickets. Above 20,000 the reviewer uses a seeded sample, labels coverage `sampled`, and cannot PASS.

For each mismatch, `classify` uses trace refs and the artifact's own implementation of the rule to name the defect:
* `omitted_requirement`: no element implements the rule. The location points at the source anchor, even when the ID is absent from the artifact.
* `precedence`: the artifact's implementation of the rule matches the ticket on its own but is pre-empted by a later rule.
* `condition_mismatch`: threshold, boundary, unit, or an overly narrow/broad condition, including interactions between rules.
* `missing_branch` (`NO_ROUTE`) and `code_runtime_error`.

Each blocker carries:
* a concrete ticket,
* the expected route and the rule that sets it (with its anchor),
* the actual route and the element that produced it,
* the number of failing partition tickets.

If the artifact's behavior matches an *unapproved* change, the evidence says so (B05 cites CH-201). Structural checks add warnings for dangling or missing trace refs, unconnected or unreachable workflow nodes, and embedded instructions.

## 4. Model integration

`model.py` defines:
* a `ModelClient` protocol,
* a reviewer system prompt (artifacts are wrapped as data, and instructions inside them must be reported, not followed),
* `FakeModelClient`, scripted per test,
* an optional `OllamaClient` for a local CPU model.

**What the model adds:** advisory findings about vague or ambiguous wording, and intent the controlled language cannot express. These are `info`/`warning` findings with `source: "model"`. **The model cannot create a blocker, approve a change, or turn a result into PASS.**

Every claim is validated before it is accepted:
* the JSON parses (malformed or truncated output is retried),
* the rule ref exists (invented refs are rejected),
* the cited artifact and location exist,
* the evidence quote appears verbatim,
* an `intent_mismatch` on a rule already verified exhaustively is rejected as a conflict.

**Bounds:** up to 3 attempts, then the call is marked failed. An "unavailable" error stops immediately. Prompts over 60k characters are skipped, never truncated. If `review_policy.model_required` is set and the model fails, the result is INCOMPLETE.

Provenance records model id, temperature, seed, max tokens, timeout, prompt version, prompt hash, and per-attempt latency, usage, response hash and error.

**No real model was run. All model results are synthetic and say nothing about real-model quality.**

## 5. Evaluation

**Data.** 18 hand-written bundles: 5 valid, 9 defective, 4 needing INCOMPLETE.
* Dev set B01–B11 was inspected during development.
* Held-out set H01–H07 was written after the final implementation commit (`final-impl`). Its labels and settings were frozen in `eval/FROZEN.json` before the reviewer first ran on them.

Coverage:
* approved, unapproved and out-of-scope changes; an approval conflict
* missing requirement, boundaries, units (hours/seconds), precedence
* wrong and missing branches, code disagreement despite correct metadata
* injected instructions in prose and code
* unsupported prose and code; model timeout, malformed, truncated and invented-reference responses
* a valid unusual formulation (B02) and a multi-rule interaction defect (H02)
* paired variants of B01: rename + reorder only (H01), paraphrase only (H06), behavior change (H07)

**Labels** (`eval/labels/`, never passed to the reviewer) were written by manual reasoning. Each expected finding has a witness ticket. Before every run, `run_eval.py` checks each witness against two independent sources:
* a hand-written oracle (`eval/oracle.py`),
* an independent artifact evaluator (`eval/independent.py`), which executes our own trusted fixture code.

Neither imports `reviewer/`. Every reviewer counterexample is re-checked the same way.

**Results** (`reports/SUMMARY.md`; same frozen inputs and settings for every run):

| Run | Correct status (all / held-out) | False pass / defective | False block / valid | INCOMPLETE on non-incomplete cases | Defect+location found | Counterexamples verified | Unsupported blockers |
|---|---|---|---|---|---|---|---|
| v0 checkpoint | 16/18 · 6/7 | 0/9 | 0/5 | 2 (B02, H01) | 7/9 | 11/11 | 1/11 |
| **final** | **18/18 · 7/7** | **0/9** | **0/5** | **0** | **8/9** | **9/9** | **0/9** |
| ablation: no boundary tickets | 17/18 · 7/7 | 1/9 | 0/5 | 0 | 7/9 | 8/8 | 0/8 |

The one finding the final reviewer misses is H07. The router sends urgent tickets to the wrong queue. The reviewer reports the right artifact, rule, line and counterexample, but names it `condition_mismatch`. The label, fixed before the run, says `wrong_route`. The classifier has no "wrong destination" category for a branch whose condition is right but whose route is wrong. This is a real gap, left as found.

**Resources:**
* final reviewer time for all 18 bundles: about 0.15 s (in-process); about 2.4 s wall time including one subprocess per bundle, on CPU
* 22 fake-model calls, 4 retries
* real model: not measured

**Ablation (boundary tickets off, everything else fixed).** The partition keeps only the points between and outside the constants, not the constants themselves.
* B04 (`> 30` vs `>= 30`) becomes a **false PASS**. The two functions differ only at exactly 30, and with no ticket at 30 they look the same.
* B05 is still caught, but the counterexample moves from 25 to 27.5, with 3 failing tickets instead of 6.

Every other outcome is unchanged. So the boundary points are needed for inclusive/exclusive defects. They add nothing for defects that differ over a whole interval.

## 6. Debugging examples

Full details, raw outputs and regression tests are in `docs/debugging/DEBUGGING.md`. Both examples are reproduced by the frozen checkpoint.

1. **"30 minutes or more" was read as a disjunction.**
   * Symptom: valid B02 came back INCOMPLETE, and R3 was hidden from coverage.
   * Cause: the top-level `or` split ran before the clause regex.
   * Fix: protect `or more/less/...` before splitting.
   * Test: `test_regression_or_more_is_not_a_disjunction`.
2. **A trace tag above a second comment line was lost.**
   * Symptom: B05 blamed DEFAULT instead of R3 and lost the "matches unapproved CH-201" evidence.
   * Cause: the tag lookup checked only one line above the `if`, and the injected comment sat there.
   * Fix: the whole comment block above a statement belongs to it.
   * Test: `test_regression_trace_tag_above_comment_block`.

Four smaller real fixes are listed in the same file:
* duplicate blockers for one omission,
* a required model check satisfied by a default fake,
* `is None` treated as an enum value,
* rename-only IDs not recognised.

## 7. Limitations

* **Language scope.** Only single-field comparisons with linear arithmetic. No cross-field arithmetic (`wait > sla`), no dates or time-of-day, no string patterns, no prose outside the listed shapes.
* **Interpretation.** Python is interpreted, not executed. Behavior that depends on real runtime objects is out of scope.
* **Classification.** Defect kinds are heuristic: H07 above. Correction text names the element that produced the wrong route. For a fall-through or interaction defect (B04, H02), that is not always the element to edit. The finding's `location` and explanation point at the right one.
* **Equal routes.** Two rules with the same route are not told apart. Swapping them is not reported because behavior is identical.
* **Small dataset.** One domain, written by one person who also built the reviewer. Held-out cases were written knowing the design, so they test generalisation to new IDs, wording and defects, not to an unknown author's style.
* **Model.** The prompt and interface exist, but semantic model performance was not measured. Model output is advisory only.
