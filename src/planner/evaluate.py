"""Evaluation (MASTER_PROMPT 14): planning metrics and anomaly-detection quality against ground truth.

This is the ONLY module that reads ``ground_truth/``. Detection is matched on (issue_type, record_id) between the
planner's ``exception`` table and the ANOMALY labels; a BOUNDARY label hit by any exception (as record_id or in
related_record_ids) is a boundary false positive. Writes ``reports/metrics.md`` and ``reports/metrics.json``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import tableio
from planner.config import Config, project_root
from planner.inject import GT_FILE, ground_truth_dir
from planner.schema import BY_NAME, ISSUE_TYPES


def read_ground_truth(folder: Path) -> pd.DataFrame:
    raw = pd.read_csv(Path(folder) / GT_FILE, dtype=str, keep_default_na=False)
    return tableio.from_text_frame(raw, BY_NAME["ground_truth"])


def reports_dir(scale: str, base: Path | None = None) -> Path:
    default = tableio.data_dir(scale)
    if base is None or Path(base).resolve() == default.resolve():
        return project_root() / "reports"
    return Path(base) / "reports"


def _related(s) -> set[str]:
    return set(str(s).split(";")) if isinstance(s, str) and s else set()


# --------------------------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------------------------

def detection(gt: pd.DataFrame, exc: pd.DataFrame) -> pd.DataFrame:
    a = gt[gt["case_class"] == "ANOMALY"]
    rows = []
    for it in ISSUE_TYPES:
        g = a[a["issue_type"] == it]
        e = exc[exc["issue_type"] == it]
        G, E = set(g["record_id"]), set(e["record_id"])
        tp, fp, fn = len(G & E), len(E - G), len(G - E)
        p = tp / (tp + fp) if tp + fp else (1.0 if not G else 0.0)
        r = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        act = e.set_index("record_id")["action_taken"]
        action_ok = sum(1 for x in g.itertuples(index=False) if act.get(x.record_id) == x.expected_action)
        rows.append({"issue_type": it, "injected": len(G), "detected": len(E), "tp": tp, "fp": fp, "fn": fn,
                     "precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
                     "action_as_expected": action_ok})
    return pd.DataFrame(rows)


def boundary_false_positives(gt: pd.DataFrame, exc: pd.DataFrame) -> pd.DataFrame:
    b = gt[gt["case_class"] == "BOUNDARY"]
    hit_ids = set(exc["record_id"]) | set().union(*[_related(s) for s in exc["related_record_ids"]]) if len(exc) else set()
    rows = []
    for it in sorted(b["issue_type"].unique()):
        g = b[b["issue_type"] == it]
        fp = sorted(set(g["record_id"]) & hit_ids)
        rows.append({"issue_type": it, "cases": len(g), "false_positives": len(fp), "examples": ";".join(fp[:5])})
    return pd.DataFrame(rows, columns=["issue_type", "cases", "false_positives", "examples"])


def outcomes(gt: pd.DataFrame, t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Check each label's expected_outcome against the planner outputs where it is machine-checkable."""
    sh, mo = t["shortage"], t["mitigation_option"]
    short = set(zip(sh["sku_id"], sh["location_id"], sh["start_week"]))
    mo_i = mo.set_index("mitigation_id")
    rec = mo[mo["is_recommended"].astype(bool)].set_index("shortage_id")
    rows = []
    for x in gt.itertuples(index=False):
        ok = None
        m = re.match(r"shortage (SKU-\d+)/(LOC-\d+) starting (2026W\d\d)", x.expected_outcome)
        if m:
            ok = (m.group(1), m.group(2), m.group(3)) in short
        elif x.issue_type == "UNAPPROVED_SUBSTITUTE":
            o = mo_i.loc[x.record_id] if x.record_id in mo_i.index else None
            ok = (o is not None and o["policy_status"] == "VIOLATION" and not bool(o["is_recommended"])
                  and rec.loc[o["shortage_id"], "policy_status"] == "COMPLIANT")
        rows.append({"gt_id": x.gt_id, "issue_type": x.issue_type, "checked": ok is not None, "ok": bool(ok)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# planning metrics
# --------------------------------------------------------------------------------------------

def _fill(closing: np.ndarray, demand: np.ndarray) -> float:
    tot = demand.sum()
    return 1.0 - float(np.minimum(demand, np.maximum(0, -closing)).sum()) / tot if tot else 1.0


def planning_metrics(t: dict[str, pd.DataFrame], cfg: Config) -> dict:
    W = cfg.weeks
    sku = t["sku"].set_index("sku_id")
    pab = t["projected_balance"].sort_values(["sku_id", "location_id", "week"])
    keys = pab[["sku_id", "location_id"]].drop_duplicates().reset_index(drop=True)
    kidx = {k: i for i, k in enumerate(zip(keys["sku_id"], keys["location_id"]))}
    closing = pab["closing_qty"].to_numpy().reshape(len(keys), W)
    demand = pab["demand_qty"].to_numpy().reshape(len(keys), W)
    flow = np.zeros_like(closing)
    led = t["mitigation_ledger"]
    for r in led[led["resource_type"] == "STOCK"].itertuples(index=False):
        flow[kidx[(r.sku_id, r.location_id)], cal.week_num(r.week) - 1] += r.qty_ea
    after = closing + np.cumsum(flow, axis=1)
    fin = (keys["sku_id"].map(sku["sku_type"]) == "FINISHED").to_numpy()
    sh, mo = t["shortage"], t["mitigation_option"]
    rec = mo[mo["is_recommended"].astype(bool)]
    non_bo = rec[rec["option_type"] != "BACKORDER"]
    covered = int(non_bo["qty_covered_ea"].sum())
    n = max(len(sh), 1)
    return {
        "sku_locations": len(keys),
        "total_demand_ea": int(demand[fin].sum()),
        "fill_rate_before": round(_fill(closing[fin], demand[fin]), 6),
        "fill_rate_after": round(_fill(after[fin], demand[fin]), 6),
        "shortages": len(sh),
        "sku_locations_short": int(sh[["sku_id", "location_id"]].drop_duplicates().shape[0]),
        "pct_sku_locations_short": round(100.0 * sh[["sku_id", "location_id"]].drop_duplicates().shape[0] / len(keys), 2),
        "shortages_by_type": sh["shortage_type"].value_counts().sort_index().to_dict(),
        "shortages_by_primary_cause": sh["primary_cause"].value_counts().sort_index().to_dict(),
        "shortages_with_cause": t["shortage_cause"].groupby("cause_code")["shortage_id"].nunique().sort_index().to_dict(),
        "mitigation_options": len(mo),
        "options_by_type": mo["option_type"].value_counts().sort_index().to_dict(),
        "option_violations": int((mo["policy_status"] == "VIOLATION").sum()),
        "recommended_by_type": rec["option_type"].value_counts().sort_index().to_dict(),
        "pct_compliant_non_backorder": round(100.0 * len(non_bo) / n, 2),
        "pct_fully_covered": round(100.0 * float((rec["coverage_pct"] >= 1.0).sum()) / n, 2),
        "mitigation_cost_inr": round(float(rec["incremental_cost_inr"].sum()), 2),
        "mitigation_cost_non_backorder_inr": round(float(non_bo["incremental_cost_inr"].sum()), 2),
        "covered_ea": covered,
        "cost_per_covered_ea_inr": round(float(non_bo["incremental_cost_inr"].sum()) / covered, 2) if covered else 0.0,
        "status_mix": sh["status"].value_counts().sort_index().to_dict(),
        "escalated": int((sh["status"] == "ESCALATED").sum()),
        "recommended_violations": int((rec["policy_status"] == "VIOLATION").sum()),
    }


def top_shortages(t: dict[str, pd.DataFrame], k: int = 20) -> pd.DataFrame:
    sh, mo = t["shortage"], t["mitigation_option"]
    rec = mo[mo["is_recommended"].astype(bool)].set_index("shortage_id")
    top = sh.sort_values("priority_rank").head(k).copy()
    top["recommended"] = [f"{rec.at[s, 'option_type']} ({rec.at[s, 'coverage_pct']:.0%}, INR {rec.at[s, 'incremental_cost_inr']:,.0f})"
                          for s in top["shortage_id"]]
    return top


# --------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------

def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in df.itertuples(index=False):
        out.append("| " + " | ".join(str(v).replace("|", "/") for v in r) + " |")
    return "\n".join(out)


def _kv(d: dict) -> str:
    return ", ".join(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v}" for k, v in d.items())


def render(cfg: Config, m: dict, det: pd.DataFrame, bfp: pd.DataFrame, out: pd.DataFrame, top: pd.DataFrame) -> str:
    ob = out[out["checked"]]
    lines = [
        f"# Metrics — scale `{cfg.scale}`, seed {cfg.seed}",
        "",
        "Generated by `python -m planner evaluate`. Planning metrics come from the planner outputs in `data/`;",
        "detection metrics compare the `exception` table with `ground_truth/` (matched on issue_type + record_id).",
        "",
        "## Planning",
        "",
        "| Metric | Value |", "|---|---|",
        f"| SKU-locations | {m['sku_locations']:,} |",
        f"| Total demand (finished SKUs, 52 wk) | {m['total_demand_ea']:,} EA |",
        f"| SKU-locations with >= 1 shortage | {m['sku_locations_short']:,} ({m['pct_sku_locations_short']}%) |",
        f"| Shortages | {m['shortages']:,} ({_kv(m['shortages_by_type'])}) |",
        f"| Primary cause | {_kv(m['shortages_by_primary_cause'])} |",
        f"| Shortages with cause (any) | {_kv(m['shortages_with_cause'])} |",
        f"| Fill rate before mitigation | {m['fill_rate_before']:.4%} |",
        f"| Fill rate after recommended mitigations | {m['fill_rate_after']:.4%} |",
        "",
        "Fill rate = 1 - sum(min(demand, backlog)) / sum(demand) per SKU-location-week (backlog semantics).",
        "",
        "## Mitigations",
        "",
        "| Metric | Value |", "|---|---|",
        f"| Options generated | {m['mitigation_options']:,} ({_kv(m['options_by_type'])}) |",
        f"| VIOLATION alternatives (never recommended) | {m['option_violations']:,} |",
        f"| Recommended mix | {_kv(m['recommended_by_type'])} |",
        f"| Shortages with a COMPLIANT non-backorder recommendation | {m['pct_compliant_non_backorder']}% |",
        f"| Fully covered (coverage 100%) | {m['pct_fully_covered']}% |",
        f"| Total mitigation cost | INR {m['mitigation_cost_inr']:,.2f} (non-backorder INR {m['mitigation_cost_non_backorder_inr']:,.2f}) |",
        f"| Cost per covered EA (non-backorder) | INR {m['cost_per_covered_ea_inr']:,.2f} over {m['covered_ea']:,} EA |",
        f"| Shortage status | {_kv(m['status_mix'])} |",
        f"| Escalated for OPERATIONS_MANAGER approval | {m['escalated']:,} |",
        f"| Recommended VIOLATION options | {m['recommended_violations']} |",
        "",
        "## Anomaly detection (exception table vs ground truth)",
        "",
        _md_table(det),
        "",
        f"Boundary false positives (target 0): **{int(bfp['false_positives'].sum())}**",
        "",
        _md_table(bfp),
        "",
        f"Expected outcomes verified: {int(ob['ok'].sum())} / {len(ob)} machine-checkable labels "
        "(LEAD_TIME_IGNORED: corrected plan shows the shortage; UNAPPROVED_SUBSTITUTE: VIOLATION, not recommended, "
        "shortage keeps a compliant recommendation).",
        "",
        "## Top 20 shortages by priority",
        "",
        _md_table(top[["priority_rank", "shortage_id", "shortage_type", "shortage_qty_ea", "value_at_risk_inr",
                       "primary_cause", "status", "recommended", "explanation"]]),
        "",
    ]
    return "\n".join(lines)


def run(cfg: Config, base: Path | None = None, out_dir: Path | None = None) -> dict:
    base = Path(base) if base else tableio.data_dir(cfg.scale)
    fmt = tableio.fmt_of(cfg)
    t = tableio.read_all(base, fmt)
    gt = read_ground_truth(ground_truth_dir(cfg.scale, base))
    exc = t["exception"]
    det, bfp, out = detection(gt, exc), boundary_false_positives(gt, exc), outcomes(gt, t)
    m = planning_metrics(t, cfg)
    top = top_shortages(t)
    folder = Path(out_dir) if out_dir else reports_dir(cfg.scale, base)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "metrics.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(cfg, m, det, bfp, out, top))
    result = {"scale": cfg.scale, "seed": cfg.seed, "planning": m, "detection": det.to_dict("records"),
              "boundary_false_positives": int(bfp["false_positives"].sum()), "boundary": bfp.to_dict("records"),
              "outcomes_checked": int(out["checked"].sum()), "outcomes_ok": int(out[out["checked"]]["ok"].sum()),
              "ground_truth_rows": len(gt)}
    with open(folder / "metrics.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return result
