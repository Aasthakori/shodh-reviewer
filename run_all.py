"""One CPU-only, offline command: unit/regression tests + all evaluation runs.

    python run_all.py
"""
import subprocess
import sys

STEPS = [
    [sys.executable, "-m", "pytest", "-q", "tests"],
    [sys.executable, "eval/run_eval.py", "--impl", "checkpoint"],
    [sys.executable, "eval/run_eval.py", "--impl", "final"],
    [sys.executable, "eval/run_eval.py", "--impl", "final", "--ablation", "no_boundary_points"],
    [sys.executable, "eval/summarize.py"],
]
for cmd in STEPS:
    print("\n$ " + " ".join(cmd[1:]), flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(rc)
