# AI-assisted workflow reviewer

Checks whether a ticket-routing **spec**, **workflow graph** and linked **Python** implement the written **requirements**, after applying **approved changes**. It returns an evidence-backed JSON report: `PASS`, `CHANGES_REQUIRED` or `INCOMPLETE`.

Shodh AI, AI/ML Engineer Intern take-home. Python, CPU only, offline, synthetic data.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Python 3.11+ (tested on 3.13)
pip install -r requirements.lock.txt                   # pinned: jsonschema 4.23.0, pytest 8.3.3 (+ transitive pins)
python run_all.py                                      # tests + checkpoint/final/ablation evaluation + summary
```

`run_all.py` runs these steps:
* 38 tests,
* the evaluation on all 18 bundles for the v0 checkpoint, the final version, and the ablation,
* `reports/SUMMARY.md`.

No network, GPU or API key is needed.

## Review one bundle

```bash
python -m reviewer review fixtures/bundles/B04_defect_code_boundary --out report.json
python -m reviewer review <bundle> --model fake --model-script fixtures/model_scripts/B02_valid_unusual_formulation.json
python -m reviewer review <bundle> --model ollama --ollama-model qwen2.5:3b   # optional local model (not evaluated)
python -m reviewer review <bundle> --cache                                     # content-addressed cache
```

Exit codes: 0 = PASS, 1 = CHANGES_REQUIRED, 2 = INCOMPLETE.

Example blocker (B04, the code uses `> 30`):

```json
{"severity": "blocker", "kind": "condition_mismatch", "rule_ref": "R3", "artifact": "router", "version": "1.3",
 "location": "router.py#L12",
 "evidence": {"counterexample": {"ticket": {"category": "billing", "urgent": false, "wait_minutes": 30},
              "expected_route": "escalation", "expected_by": "R3 (policy.md#L8)",
              "actual_route": "normal", "actual_by": "router.py#L14"}, "failing_tickets": 3},
 "correction": "... implements R3 exactly: wait_minutes >= 30 -> escalation."}
```

## Results (frozen set, fake model client)

| Run | Correct status | False pass | False block | Defect + location found |
|---|---|---|---|---|
| v0 checkpoint | 16/18 | 0/9 | 0/5 | 7/9 |
| final | 18/18 (held-out 7/7) | 0/9 | 0/5 | 8/9 |
| ablation: no boundary tickets | 17/18 | 1/9 | 0/5 | 7/9 |

See `reports/SUMMARY.md` for the full tables. Real-model performance was **not measured**.

## Repository map

| Path | What |
|---|---|
| `reviewer/` | the reviewer: `loader`, `prose` (requirement language), `conditions` (IR), `effective` (approvals), `workflow`, `pycode` (restricted Python), `semantic` (partition + comparison), `model` (client, prompt, validation), `review` (status, report, cache), CLI |
| `schemas/` | JSON Schemas for manifest, approvals, requirements, spec, workflow, conditions, report |
| `fixtures/bundles/` | 18 synthetic review bundles (B01–B11 dev, H01–H07 held-out) |
| `fixtures/model_scripts/` | fake-model scripts (reviewer inputs, not labels) |
| `fixtures/authoring/` | scripts used to write the fixtures |
| `eval/labels/` | expected outcomes, kept outside reviewer inputs |
| `eval/oracle.py`, `eval/independent.py` | independent oracle and artifact evaluator (do not import `reviewer/`) |
| `eval/FROZEN.json` | hashes of held-out bundles + labels, implementation commit, evaluation config |
| `checkpoints/v0_initial/` | frozen initial working checkpoint (git tag `v0-checkpoint`) |
| `reports/` | machine-readable reports and metrics for each run + `SUMMARY.md` |
| `docs/` | `ENGINEERING_NOTE.md`, `LANGUAGE.md`, `AI_ASSISTANCE.md`, `debugging/` |
| `tests/` | unit + regression tests |
