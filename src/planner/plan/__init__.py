"""Planning engine: quality gate -> PAB + MRP (planned orders, production, BOM) -> shortages.

Reads only ``data/<scale>/`` input tables (never ground truth) and writes planner outputs next to them.
Mitigations and decisions (Phase 4) build on the returned :class:`PlanRun`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from planner import ids, tableio
from planner.config import Config

from . import balance, quality_gate, replenish, shortages

INPUT_TABLES = (
    "location", "sku", "supplier", "sku_location", "supplier_sku", "sku_substitute", "bom",
    "lead_time", "supplier_capacity", "production_capacity", "forecast_demand", "purchase_order",
    "purchase_order_event", "inventory_txn", "inventory", "production_plan",
)
OUTPUT_TABLES = ("exception", "planned_order", "projected_balance", "production_plan",
                 "shortage", "shortage_cause", "shortage_event")


@dataclass
class PlanRun:
    tables: dict[str, pd.DataFrame]     # inputs as read
    gate: quality_gate.GateResult
    mrp: replenish.MrpResult
    outputs: dict[str, pd.DataFrame]    # schema-shaped output tables
    seconds: float


def _number_exceptions(exc: pd.DataFrame, start: int = 1) -> pd.DataFrame:
    order = {k: i for i, k in enumerate(quality_gate.RULES)}
    exc = exc.assign(_o=exc["issue_type"].map(order)).sort_values(["_o", "record_id"], kind="mergesort")
    exc = exc.drop(columns="_o").reset_index(drop=True)
    exc.insert(0, "exception_id", [ids.exception_id(start + k) for k in range(len(exc))])
    return exc


def plan(tables: dict[str, pd.DataFrame], cfg: Config) -> PlanRun:
    t0 = time.perf_counter()
    gate = quality_gate.run(tables, cfg.weeks)
    grid = replenish.build_grid(tables, gate, cfg)
    mrp = replenish.run_mrp(grid, gate, cfg)
    pab = balance.to_frame(grid.keys, grid.opening, grid.firm, mrp.planned, mrp.production, mrp.demand, grid.ss)
    sh, sc = shortages.build(tables, gate, mrp, cfg)
    events = shortages.opening_events(sh, cfg.as_of_date)
    outputs = {
        "exception": _number_exceptions(gate.exceptions),
        "planned_order": mrp.planned_orders,
        "projected_balance": pab,
        "production_plan": mrp.production_plan,
        "shortage": sh,
        "shortage_cause": sc,
        "shortage_event": events,
    }
    return PlanRun(tables, gate, mrp, outputs, time.perf_counter() - t0)


def run(cfg: Config, base: Path | None = None) -> PlanRun:
    base = Path(base) if base else tableio.data_dir(cfg.scale)
    fmt = tableio.fmt_of(cfg)
    if not tableio.exists("sku", base, fmt):
        raise FileNotFoundError(f"no generated data in {base}; run `python -m planner generate --scale {cfg.scale}` first")
    tables = tableio.read_all(base, fmt, list(INPUT_TABLES))
    res = plan(tables, cfg)
    for name in OUTPUT_TABLES:
        tableio.write_table(res.outputs[name], name, base, fmt)
    return res


def summary(res: PlanRun) -> dict:
    sh, sc = res.outputs["shortage"], res.outputs["shortage_cause"]
    n_sl = len(res.mrp.grid.keys)
    short_sl = sh[["sku_id", "location_id"]].drop_duplicates()
    return {
        "sku_locations": n_sl,
        "sku_locations_short": len(short_sl),
        "pct_sku_locations_short": round(100.0 * len(short_sl) / max(n_sl, 1), 2),
        "shortages": len(sh),
        "by_type": sh["shortage_type"].value_counts().sort_index().to_dict(),
        "by_primary_cause": sh["primary_cause"].value_counts().sort_index().to_dict(),
        "shortages_with_cause": sc.groupby("cause_code")["shortage_id"].nunique().sort_index().to_dict(),
        "exceptions": len(res.outputs["exception"]),
        "planned_orders": len(res.outputs["planned_order"]),
        "planned_orders_cut": int((res.outputs["planned_order"]["capacity_cut_ea"] > 0).sum()),
        "seconds": round(res.seconds, 2),
    }
