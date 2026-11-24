"""Baseline data generation in dependency order (MASTER_PROMPT Section 9, steps 1-7)."""
from __future__ import annotations

import json
import time
from pathlib import Path

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
    "inventory_txn", "inventory", "production_plan",
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
    return ctx


def write(ctx: Ctx, out: Path) -> dict[str, int]:
    fmt = tableio.fmt_of(ctx.cfg)
    counts = {}
    for name in INPUT_TABLES:
        tableio.write_table(ctx.tables[name], name, out, fmt)
        counts[name] = len(ctx.tables[name])
    return counts


def run(cfg: Config, out: Path) -> dict[str, int]:
    t0 = time.perf_counter()
    ctx = build(cfg)
    counts = write(ctx, out)
    info = {"command": f"python -m planner generate --scale {cfg.scale} --seed {cfg.seed}",
            "scale": cfg.scale, "seed": cfg.seed, "package_version": __version__,
            "as_of_date": cfg.as_of_date.isoformat(), "row_counts": counts,
            "generate_seconds": round(time.perf_counter() - t0, 2)}
    with open(Path(out) / "run_info.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(info, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return counts
