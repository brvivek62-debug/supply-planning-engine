"""Data generation in dependency order (MASTER_PROMPT Section 9): baseline steps 1-7, then step 8 (inject
boundary cases, anomalies and MANUAL proposals, write ground truth) via ``planner.inject``."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from planner import __version__, tableio
from planner.config import Config
from planner.rng import RngFactory

from .bridges import gen_bom, gen_sku_location, gen_sku_substitute, gen_supplier_sku
from .common import Ctx
from .demand import fill_safety_stock, gen_forecast_demand
from .inventory import gen_inventory
from .master import gen_location, gen_sku, gen_supplier
from .orders import gen_purchase_orders
from .production import gen_production_plan
from .supply import gen_lead_time, gen_production_capacity, gen_supplier_capacity

INPUT_TABLES = (
    "location", "sku", "supplier",
    "sku_location", "supplier_sku", "sku_substitute", "bom",
    "lead_time", "supplier_capacity", "production_capacity",
    "forecast_demand", "purchase_order", "purchase_order_event",
    "inventory_txn", "inventory", "production_plan", "mitigation_proposal",
)


def build(cfg: Config) -> Ctx:
    """Generate every input table in memory, parents before children."""
    ctx = Ctx(cfg=cfg, rngs=RngFactory(cfg.seed))
    t = ctx.tables
    # 1. master
    t["location"] = gen_location(ctx)
    t["sku"] = gen_sku(ctx)
    t["supplier"] = gen_supplier(ctx)
    # 2. bridges (BOM first: component stocking locations follow the parents' plants)
    t["bom"] = gen_bom(ctx)
    t["sku_location"] = gen_sku_location(ctx)
    t["supplier_sku"] = gen_supplier_sku(ctx)
    t["sku_substitute"] = gen_sku_substitute(ctx)
    # 3. supply
    t["lead_time"] = gen_lead_time(ctx)
    # 4. demand (+ derived safety stock on the assortment bridge)
    t["forecast_demand"] = gen_forecast_demand(ctx)
    fill_safety_stock(ctx)
    # 3b. capacity is sized on the published forecast level (suppliers plan capacity on customer forecasts)
    t["supplier_capacity"] = gen_supplier_capacity(ctx)
    t["production_capacity"] = gen_production_capacity(ctx)
    # 5. purchase orders and their event history
    t["purchase_order"], t["purchase_order_event"] = gen_purchase_orders(ctx)
    # 6. ledger -> inventory snapshot
    t["inventory_txn"], t["inventory"] = gen_inventory(ctx)
    # 7. FIRM production
    t["production_plan"] = gen_production_plan(ctx)
    # no MANUAL proposals in the baseline (step 8 adds them)
    t["mitigation_proposal"] = pd.DataFrame(columns=["mitigation_id", "shortage_id", "option_seq", "option_type",
                                                     "ref_substitute_id", "ref_substitute_sku_id", "qty_proposed_ea",
                                                     "created_by_role"])
    return ctx


def write(tables: dict[str, pd.DataFrame], cfg: Config, out: Path) -> dict[str, int]:
    fmt = tableio.fmt_of(cfg)
    counts = {}
    for name in INPUT_TABLES:
        tableio.write_table(tables[name], name, out, fmt)
        counts[name] = len(tables[name])
    return counts


def inject_cases(ctx: Ctx, out: Path) -> tuple[dict[str, int], int]:
    """Step 8: boundary cases and anomalies on the written baseline, then MANUAL proposals for real shortages
    found by a planning pass. Returns (row counts, ground-truth rows)."""
    from planner import inject, plan
    from planner.inject import anomalies, boundaries
    from planner.inject.common import Book
    cfg, fmt = ctx.cfg, tableio.fmt_of(ctx.cfg)
    t = tableio.read_all(out, fmt, list(INPUT_TABLES))
    book = Book()
    base = plan.plan({k: v.copy() for k, v in t.items()}, cfg)
    gt = boundaries.inject(t, cfg, ctx.rngs, book, base)
    gt += anomalies.inject_inputs(t, cfg, ctx.rngs, book, base)
    write(t, cfg, out)
    t = tableio.read_all(out, fmt, list(INPUT_TABLES))
    res = plan.plan({k: v.copy() for k, v in t.items()}, cfg)
    props, rows = anomalies.inject_proposals(t, res, cfg, ctx.rngs)
    gt += rows
    t["mitigation_proposal"] = props
    counts = write(t, cfg, out)
    inject.write_ground_truth(inject.to_frame(gt), inject.ground_truth_dir(cfg.scale, out))
    return counts, len(gt)


def run(cfg: Config, out: Path, inject: bool = True) -> dict[str, int]:
    t0 = time.perf_counter()
    ctx = build(cfg)
    counts = write(ctx.tables, cfg, out)
    n_gt = 0
    if inject:
        counts, n_gt = inject_cases(ctx, out)
    info = {"command": f"python -m planner generate --scale {cfg.scale} --seed {cfg.seed}",
            "scale": cfg.scale, "seed": cfg.seed, "package_version": __version__,
            "as_of_date": cfg.as_of_date.isoformat(), "row_counts": counts, "injected": inject,
            "ground_truth_rows": n_gt, "generate_seconds": round(time.perf_counter() - t0, 2)}
    with open(Path(out) / "run_info.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(info, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return counts
