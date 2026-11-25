"""Command-line interface: ``python -m planner <command> [--scale S] [--seed N]``."""
from __future__ import annotations

import argparse
import sys
from typing import Callable

from planner import DEFAULT_SEED, __version__
from planner.config import SCALES, load_config


def cmd_generate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from planner import generate, tableio
    cfg = load_config(args.scale, args.seed)
    out = Path(args.out) if args.out else tableio.data_dir(cfg.scale)
    counts = generate.run(cfg, out)
    width = max(len(k) for k in counts)
    print(f"[planner] generated scale={cfg.scale} seed={cfg.seed} -> {out}")
    for name, n in counts.items():
        print(f"  {name:<{width}}  {n:>10,}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from pathlib import Path

    from planner import plan
    cfg = load_config(args.scale)
    res = plan.run(cfg, Path(args.data) if args.data else None)
    s = plan.summary(res)
    print(f"[planner] planned scale={cfg.scale} in {s['seconds']} s")
    for k, v in s.items():
        if k != "seconds":
            print(f"  {k:<26} {v}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from planner import evaluate
    cfg = load_config(args.scale)
    r = evaluate.run(cfg, Path(args.data) if args.data else None)
    m = r["planning"]
    print(f"[planner] evaluated scale={cfg.scale}: {m['shortages']} shortages, fill rate "
          f"{m['fill_rate_before']:.4%} -> {m['fill_rate_after']:.4%}, boundary false positives "
          f"{r['boundary_false_positives']}")
    for d in r["detection"]:
        print(f"  {d['issue_type']:<28} injected {d['injected']:>3}  detected {d['detected']:>3}  "
              f"precision {d['precision']:.3f}  recall {d['recall']:.3f}  f1 {d['f1']:.3f}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from planner.validate import report
    cfg = load_config(args.scale)
    s = report.run(cfg, Path(args.data) if args.data else None)["summary"]
    print(f"[planner] validated scale={cfg.scale} in {s['seconds']} s: {s['checks']} checks, {s['passed']} PASS, "
          f"{s['expected_fail_checks']} EXPECTED_FAIL ({s['expected_failures']} expected failures), "
          f"{s['failed_checks']} FAIL, unexpected failures {s['unexpected_failures']}")
    return 0 if s["unexpected_failures"] == 0 else 1


def cmd_export(args: argparse.Namespace) -> int:
    from pathlib import Path

    from planner import export, profile
    cfg = load_config(args.scale)
    base = Path(args.data) if args.data else None
    r = export.run(cfg, base)
    p = profile.run(cfg, base)
    print(f"[planner] exported scale={cfg.scale}: {len(r['written'])} JSON/SQL files, manifest {r['manifest']}, profile {p}")
    return 0


def cmd_repro(args: argparse.Namespace) -> int:
    from planner import repro
    cfg = load_config(args.scale, args.seed)
    r = repro.run(cfg)
    print(f"[planner] repro-check scale={cfg.scale} seed={cfg.seed} in {r['seconds']} s: "
          f"{'IDENTICAL' if r['identical'] else 'MISMATCH'} over {r['files']} files; seed {r['other_seed']} changes "
          f"{r['files_changed_by_other_seed']} of them")
    return 0 if r["identical"] and r["files_changed_by_other_seed"] > 0 else 1


def cmd_run_all(args: argparse.Namespace) -> int:
    import time
    t0 = time.perf_counter()
    rc = 0
    for name, fn in (("generate", cmd_generate), ("plan", cmd_plan), ("validate", cmd_validate),
                     ("evaluate", cmd_evaluate), ("export", cmd_export), ("repro-check", cmd_repro)):
        t1 = time.perf_counter()
        rc |= fn(args)
        print(f"[planner] {name} done in {time.perf_counter() - t1:.1f} s")
    print(f"[planner] run-all scale={args.scale} finished in {time.perf_counter() - t0:.1f} s (exit {rc})")
    return rc


def cmd_docs(args: argparse.Namespace) -> int:
    from planner.docs_gen import write_all
    for path in write_all():
        print(f"[planner] wrote {path}")
    return 0


COMMANDS: dict[str, tuple[str, Callable[[argparse.Namespace], int], bool, bool]] = {
    # name: (help, handler, takes --scale, takes --seed)
    "generate": ("Generate baseline data, inject labelled cases, write ground truth", cmd_generate, True, True),
    "plan": ("Quality gate, PAB, planned orders, shortages, mitigations, decisions", cmd_plan, True, False),
    "validate": ("Run all checks -> reports/validation_summary.{md,json}", cmd_validate, True, False),
    "evaluate": ("Metrics and detection precision/recall vs ground truth", cmd_evaluate, True, False),
    "export": ("CSV/JSON/SQL exports, manifest and checksums", cmd_export, True, False),
    "repro-check": ("Run twice into temp dirs and compare row counts + SHA-256", cmd_repro, True, True),
    "docs": ("Generate data dictionary, sql/schema.sql and ERD from schema.py", cmd_docs, False, False),
    "run-all": ("Run every step above in order", cmd_run_all, True, True),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="planner", description="Demand, inventory & supply planning engine.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>", required=True)
    for name, (help_, handler, scale, seed) in COMMANDS.items():
        sp = sub.add_parser(name, help=help_, description=help_)
        if scale:
            sp.add_argument("--scale", choices=SCALES, default="starter")
        if seed:
            sp.add_argument("--seed", type=int, default=DEFAULT_SEED)
        if name == "run-all":
            sp.set_defaults(out=None, data=None)
        if name == "generate":
            sp.add_argument("--out", default=None, help="output directory (default data/<scale>)")
        if name in ("plan", "evaluate", "validate", "export"):
            sp.add_argument("--data", default=None, help="data directory (default data/<scale>)")
        sp.set_defaults(func=handler)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "scale", None):
        load_config(args.scale, getattr(args, "seed", None))  # fail fast on bad config
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
