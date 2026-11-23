"""Command-line interface: ``python -m planner <command> [--scale S] [--seed N]``."""
from __future__ import annotations

import argparse
import sys
from typing import Callable

from planner import DEFAULT_SEED, __version__
from planner.config import SCALES, load_config


def _not_yet(name: str, phase: int) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        print(f"[planner] '{name}' is implemented in Phase {phase}; not available yet.")
        return 2
    return run


def cmd_docs(args: argparse.Namespace) -> int:
    from planner.docs_gen import write_all
    for path in write_all():
        print(f"[planner] wrote {path}")
    return 0


COMMANDS: dict[str, tuple[str, Callable[[argparse.Namespace], int], bool, bool]] = {
    # name: (help, handler, takes --scale, takes --seed)
    "generate": ("Generate baseline data, inject labelled cases, write ground truth", _not_yet("generate", 2), True, True),
    "plan": ("Quality gate, PAB, planned orders, shortages, mitigations, decisions", _not_yet("plan", 3), True, False),
    "validate": ("Run all checks -> reports/validation_summary.{md,json}", _not_yet("validate", 6), True, False),
    "evaluate": ("Metrics and detection precision/recall vs ground truth", _not_yet("evaluate", 5), True, False),
    "export": ("CSV/JSON/SQL exports, manifest and checksums", _not_yet("export", 6), True, False),
    "repro-check": ("Run twice into temp dirs and compare row counts + SHA-256", _not_yet("repro-check", 6), True, True),
    "docs": ("Generate data dictionary, sql/schema.sql and ERD from schema.py", cmd_docs, False, False),
    "run-all": ("Run every step above in order", _not_yet("run-all", 6), True, True),
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
        sp.set_defaults(func=handler)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "scale", None):
        load_config(args.scale, getattr(args, "seed", None))  # fail fast on bad config
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
