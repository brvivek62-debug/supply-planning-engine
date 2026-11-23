"""Step 7: production_plan FIRM weeks (input). PLANNED weeks are created later by the planner."""
from __future__ import annotations

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids

from .common import Ctx, round_half_up


def gen_production_plan(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("production_plan")
    firm = int(ctx.cfg["production"]["firm_weeks"])
    weeks = cal.week_labels(firm)
    sku = ctx["sku"]
    make = set(sku.loc[sku["sourcing_type"] == "MAKE", "sku_id"])
    fd = ctx["forecast_demand"]
    req = fd[fd["sku_id"].isin(make) & fd["week"].isin(weeks)][["sku_id", "location_id", "week", "forecast_qty_ea"]].copy()
    req = req.sort_values(["sku_id", "location_id", "week"]).reset_index(drop=True)
    req["requested_qty_ea"] = round_half_up(req["forecast_qty_ea"].to_numpy() * rng.uniform(0.95, 1.15, len(req)))
    cap = ctx["production_capacity"].set_index(["plant_location_id", "week"])["capacity_qty_ea"]
    load = req.groupby(["location_id", "week"])["requested_qty_ea"].transform("sum").to_numpy()
    capv = np.array([cap[(l, w)] for l, w in req[["location_id", "week"]].itertuples(index=False)])
    scale = np.where(load > capv, capv / np.maximum(load, 1), 1.0)
    planned = np.floor(req["requested_qty_ea"].to_numpy() * scale).astype(np.int64)
    df = pd.DataFrame({
        "sku_id": req["sku_id"], "plant_location_id": req["location_id"], "week": req["week"],
        "planned_qty_ea": planned, "status": "FIRM", "capacity_used_ea": planned,
        "requested_qty_ea": req["requested_qty_ea"].astype(np.int64),
        "capacity_cut_ea": req["requested_qty_ea"].to_numpy() - planned, "component_cut_ea": 0,
    })
    df.insert(0, "prod_plan_id", [ids.prod_plan_id(i + 1) for i in range(len(df))])
    return df
