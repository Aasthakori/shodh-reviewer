# Material AI assistance and how it was verified

**Tool used:** Claude (Anthropic), through a chat-based coding assistant that could run code in a sandbox.

**What the AI did (material):**
* Proposed the architecture: a shared condition IR, a boundary-partition equivalence check, candidate effective policies for approval conflicts, and a validated advisory-only model layer.
* Wrote most of the code in `reviewer/`, the JSON schemas, the fixtures and their authoring scripts (`fixtures/authoring/`), the tests, the evaluation harness, and first drafts of these documents.
* Ran the code during development. The bugs in `docs/debugging/DEBUGGING.md` surfaced in those runs and were then fixed.

**How correctness was checked (not by trusting the AI's word):**
1. **Tests.** 38 unit and regression tests (`pytest`), one per fixed bug. They also cover cache invalidation, injection resistance, bounded retries, oversized input, paired rename/reorder/behavior variants, and the report schema.
2. **Independent labels.** Expected outcomes were written by reasoning about each case, not by running the reviewer. Every witness ticket is checked before each evaluation run against:
   * a separate hand-written oracle (`eval/oracle.py`),
   * a separate artifact evaluator (`eval/independent.py`).
   Neither imports the reviewer. This check caught a real fixture bug (H05).
3. **Every reviewer counterexample** in the evaluation is re-checked against the same oracle and evaluator. Result: 9/9 verified in the final run.
4. **Held-out set.** Frozen with file hashes and the implementation commit before the first run (`eval/FROZEN.json`). Results on it are reported as they came out, including the H07 kind mismatch.
5. **Git history.** Tags `v0-checkpoint` → `final-impl` → `frozen-eval` let anyone re-run the checkpoint and the final version on the same data.

**Not verified:** real-model behavior. Only the fake client was run.
