"""CLI: python -m reviewer review <bundle_dir> [--model none|fake|ollama] [--out FILE] [--cache]"""
from __future__ import annotations

import argparse
import json
import sys

from .review import ReviewConfig, review, review_cached


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reviewer", description="Review a ticket-routing bundle.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review", help="review one bundle directory")
    r.add_argument("bundle")
    r.add_argument("--model", choices=["none", "fake", "ollama"], default="none")
    r.add_argument("--model-script", help="JSON file with a fake-client script")
    r.add_argument("--ollama-model", default="qwen2.5:3b")
    r.add_argument("--no-boundary-points", action="store_true", help="ablation: drop boundary tickets")
    r.add_argument("--cache", action="store_true", help="use the content-addressed report cache")
    r.add_argument("--out", help="write report JSON here (default: stdout)")
    a = ap.parse_args(argv)
    script = json.load(open(a.model_script)) if a.model_script else None
    cfg = ReviewConfig(model=a.model, model_script=script, ollama_model=a.ollama_model,
                       boundary_points=not a.no_boundary_points)
    rep = review_cached(a.bundle, cfg) if a.cache else review(a.bundle, cfg)
    text = json.dumps(rep, indent=2, default=str)
    if a.out:
        open(a.out, "w").write(text)
        print(f"{rep['status']}: {rep['summary']}")
    else:
        print(text)
    return {"PASS": 0, "CHANGES_REQUIRED": 1, "INCOMPLETE": 2}[rep["status"]]


if __name__ == "__main__":
    sys.exit(main())
