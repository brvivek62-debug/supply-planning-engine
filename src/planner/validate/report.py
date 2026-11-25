"""Full validation suite (MASTER_PROMPT 13) -> ``reports/validation_summary.{md,json}``.

Runs, on the written ``data/<scale>/`` tables:
1. generic schema checks on every table (PK, required, enums, ID regex, bounds, FKs incl. composite, natural keys,
   effective-date order)
2. domain checks on the raw inputs (ledger, PO quantities / dates / lifecycle, capacity, lead times, demand, ...)
3. planner-output checks (PAB, shortages, causes, planned orders, capacity ledger, production, mitigations, policy)
4. a SQLite load of every table with ``PRAGMA foreign_keys = ON`` plus ``PRAGMA foreign_key_check``

Expected vs unexpected failures: the raw inputs deliberately contain anomalies. A failing record is EXPECTED only
when the planner's own ``exception`` table names it under the issue type that explains that check (so the quality
gate caught and handled it); every other failure is UNEXPECTED and must be 0. This uses planner outputs only —
nothing here reads ground truth.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from planner import tableio
from planner.config import Config
from planner.sqlio import foreign_key_violations, load_sqlite

from . import checks, plan_checks
from .checks import CheckResult

# check name -> issue type whose exceptions explain its failures, and how a failing key maps to exception ids
EXPLAINED_BY = {
    "po_qty_uom_moq": ("UOM_MISMATCH",),                    # key po_id = exception record_id
    "po_receipt_week_arithmetic": ("LEAD_TIME_IGNORED",),   # key po_id = exception record_id
    # key po_id: a duplicate row (in DUPLICATE_INBOUND related ids) or a PO whose qty_base_ea was mis-converted
    # (UOM_MISMATCH record) so its correct IN_TRANSIT row no longer matches the raw PO qty
    "in_transit_mirrors_open_pos": ("DUPLICATE_INBOUND", "UOM_MISMATCH"),
    "firm_po_within_supplier_capacity": ("SUPPLIER_CAPACITY_EXCEEDED",),  # key supplier/week <- capacity_id in related
}


def _exception_ids(exc: pd.DataFrame, issue: str) -> set[str]:
    e = exc[exc["issue_type"] == issue]
    out = set(e["record_id"])
    for s in e["related_record_ids"].dropna():
        out |= set(str(s).split(";"))
    return out


def classify(results: list[CheckResult], t: dict[str, pd.DataFrame]) -> list[dict]:
    exc = t.get("exception", pd.DataFrame(columns=["issue_type", "record_id", "related_record_ids"]))
    cap = t["supplier_capacity"]
    cap_key = dict(zip(cap["capacity_id"], cap["supplier_id"] + "/" + cap["week"]))
    rows = []
    for r in results:
        d = r.as_dict()
        expected, ids = 0, set()
        issues = EXPLAINED_BY.get(r.check, ()) if r.failures else ()
        if issues:
            ids = set().union(*[_exception_ids(exc, i) for i in issues])
            if r.check == "firm_po_within_supplier_capacity":
                ids = {cap_key[i] for i in ids if i in cap_key}
            expected = sum(1 for k in r.keys if k in ids)   # unkeyed failures can never be explained
        d["expected_failures"] = expected
        d["unexpected_failures"] = r.failures - expected
        d["explained_by"] = " + ".join(issues) if expected else ""
        d["unexpected_examples"] = [k for k in r.keys if k not in ids][:5] if d["unexpected_failures"] else []
        d["status"] = "PASS" if r.failures == 0 else ("EXPECTED_FAIL" if d["unexpected_failures"] == 0 else "FAIL")
        rows.append(d)
    return rows


def sqlite_check(base: Path, fmt: str) -> CheckResult:
    con, loaded, errors = load_sqlite(base, fmt)
    fk = foreign_key_violations(con)
    con.close()
    n = sum(loaded.values())
    ex = [f"{a}: {b}" for a, b in errors[:3]] + [f"{v[0]} rowid {v[1]} -> {v[2]}" for v in fk[:3]]
    return CheckResult("sqlite_fk_check", f"all ({len(loaded)} tables)", n, len(errors) + len(fk),
                       "load sql/schema.sql + every table with PRAGMA foreign_keys = ON (CHECK constraints enforced); "
                       "PRAGMA foreign_key_check returns no rows", ex, ex)


def run(cfg: Config, base: Path | None = None, out_dir: Path | None = None, sqlite: bool = True) -> dict:
    t0 = time.perf_counter()
    base = Path(base) if base else tableio.data_dir(cfg.scale)
    fmt = tableio.fmt_of(cfg)
    t = tableio.read_all(base, fmt)
    groups: list[tuple[str, list[CheckResult]]] = [
        ("schema", checks.run_generic(t)),
        ("input domain", checks.run_generated_domain(t, cfg)),
        ("planner outputs", plan_checks.run_plan_checks(t, cfg)),
    ]
    if sqlite:
        groups.append(("sqlite", [sqlite_check(base, fmt)]))
    rows = []
    for g, res in groups:
        for d in classify(res, t):
            rows.append({"group": g, **d})
    summary = {
        "scale": cfg.scale, "seed": cfg.seed, "checks": len(rows),
        "passed": sum(r["status"] == "PASS" for r in rows),
        "expected_fail_checks": sum(r["status"] == "EXPECTED_FAIL" for r in rows),
        "failed_checks": sum(r["status"] == "FAIL" for r in rows),
        "rows_checked": sum(r["rows_checked"] for r in rows),
        "expected_failures": sum(r["expected_failures"] for r in rows),
        "unexpected_failures": sum(r["unexpected_failures"] for r in rows),
        "exceptions_by_issue": t["exception"]["issue_type"].value_counts().sort_index().to_dict() if "exception" in t else {},
        "seconds": round(time.perf_counter() - t0, 2),
    }
    folder = Path(out_dir) if out_dir else tableio.reports_dir(cfg.scale, base)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "validation_summary.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"summary": {k: v for k, v in summary.items() if k != "seconds"}, "checks": rows}, fh, indent=2)
        fh.write("\n")
    with open(folder / "validation_summary.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(summary, rows))
    return {"summary": summary, "checks": rows}


def render(s: dict, rows: list[dict]) -> str:
    lines = [
        f"# Validation summary — scale `{s['scale']}`, seed {s['seed']}",
        "",
        "Generated by `python -m planner validate`. One row per check. A failure is **expected** only when the",
        "planner's `exception` table names the failing record under the issue type that explains the check (the",
        "quality gate caught and handled an injected anomaly); every other failure is **unexpected**.",
        "",
        "| Result | Value |", "|---|---|",
        f"| Checks run | {s['checks']} |",
        f"| PASS | {s['passed']} |",
        f"| EXPECTED_FAIL (all failures explained by exceptions) | {s['expected_fail_checks']} |",
        f"| FAIL | {s['failed_checks']} |",
        f"| Rows checked | {s['rows_checked']:,} |",
        f"| Expected failures (records) | {s['expected_failures']} |",
        f"| **Unexpected failures** | **{s['unexpected_failures']}** |",
        f"| Exceptions raised by the planner | {', '.join(f'{k} {v}' for k, v in s['exceptions_by_issue'].items())} |",
        "",
    ]
    exp = [r for r in rows if r["status"] != "PASS"]
    if exp:
        lines += ["## Checks with failures", "",
                  "| Check | Table | Failures | Expected | Unexpected | Explained by | Examples |", "|---|---|---|---|---|---|---|"]
        for r in exp:
            lines.append(f"| {r['check']} | {r['table']} | {r['failures']} | {r['expected_failures']} | "
                         f"{r['unexpected_failures']} | {r['explained_by']} | {'; '.join(r['examples'][:3])} |")
        lines.append("")
    lines += ["## All checks", "", "| Group | Check | Table | Rows checked | Failures | Expected | Unexpected | Status | Rule |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['group']} | {r['check']} | {r['table']} | {r['rows_checked']:,} | {r['failures']} | "
                     f"{r['expected_failures']} | {r['unexpected_failures']} | {r['status']} | {r['detail'].replace('|', '/')} |")
    lines.append("")
    return "\n".join(lines)
