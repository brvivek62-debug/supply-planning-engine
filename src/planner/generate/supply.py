"""Step 3: lead_time, supplier_capacity, production_capacity."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids
from planner.schema import REGIONS

from .common import Ctx

ADJACENT = {frozenset(p) for p in [("NORTH", "CENTRAL"), ("NORTH", "WEST"), ("NORTH", "EAST"), ("SOUTH", "CENTRAL"),
                                    ("SOUTH", "WEST"), ("EAST", "CENTRAL"), ("WEST", "CENTRAL")]}
LANE_START = "2024-01-01"
MODE_BAND = {"ROAD": (1, 3), "RAIL": (2, 4), "SEA": (5, 9)}   # typical lead-time weeks per mode


def distance_band(a: str, b: str) -> int:
    """0 = same region, 1 = adjacent, 2 = far."""
    if a == b:
        return 0
    return 1 if frozenset((a, b)) in ADJACENT else 2


def _days_for_weeks(rng: np.random.Generator, weeks: int, min_prod: int = 1) -> tuple[int, int]:
    total = int(rng.integers(max(3, (weeks - 1) * 7 + 1), weeks * 7 + 1))
    prod = int(rng.integers(min_prod, max(min_prod + 1, min(total - 1, 14) + 1)))
    prod = min(prod, total - 1)
    return prod, total - prod


def gen_lead_time(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("lead_time")
    sup = ctx["supplier"]
    importer = ctx.aux["importer"]
    sc = ctx.cfg["supply"]
    change_week = int(sc["lead_time_change_week"])
    change_from = cal.week_start(change_week)
    rows = []
    for sid, home in sup[["supplier_id", "home_region"]].itertuples(index=False):
        for reg in REGIONS:
            band = distance_band(home, reg)
            if importer[sid]:
                mode, weeks = "SEA", int(rng.integers(5, 10))
            elif band == 0:
                mode, weeks = "ROAD", int(rng.integers(1, 3))
            elif band == 1:
                mode = "ROAD" if rng.random() < 0.5 else "RAIL"
                weeks = int(rng.integers(2, 4))
            else:
                mode, weeks = "RAIL", int(rng.integers(3, 5))
            prod, transit = _days_for_weeks(rng, weeks)
            std = dict(supplier_id=sid, dest_region=reg, transport_mode=mode, is_expedite_lane=False,
                       production_days=prod, transit_days=transit)
            air = dict(supplier_id=sid, dest_region=reg, transport_mode="AIR", is_expedite_lane=True,
                       production_days=min(prod, 8), transit_days=int(rng.integers(1, 4)))
            if rng.random() < sc["lead_time_change_share"]:
                lo_b, hi_b = MODE_BAND[mode]
                new_weeks = int(np.clip(weeks + int(rng.choice([1, 1, 2, -1])), lo_b, hi_b))
                if new_weeks == weeks:  # at the edge of the band: move the other way
                    new_weeks = weeks - 1 if weeks > lo_b else weeks + 1
                target = (new_weeks - 1) * 7 + int(rng.integers(1, 8))
                p2 = min(prod, max(1, target - 1))
                rows.append({**std, "effective_from": LANE_START,
                             "effective_to": (change_from - timedelta(days=1)).isoformat()})
                rows.append({**std, "production_days": p2, "transit_days": target - p2,
                             "effective_from": change_from.isoformat(), "effective_to": None})
            else:
                rows.append({**std, "effective_from": LANE_START, "effective_to": None})
            rows.append({**air, "effective_from": LANE_START, "effective_to": None})
    df = pd.DataFrame(rows)
    df["total_lead_weeks"] = np.ceil((df["production_days"] + df["transit_days"]) / 7).astype(int)
    df = df.sort_values(["supplier_id", "dest_region", "is_expedite_lane", "effective_from"], kind="mergesort")
    df.insert(0, "lead_time_id", [ids.lead_time_id(i + 1) for i in range(len(df))])
    return df.reset_index(drop=True)


def gen_supplier_capacity(ctx: Ctx) -> pd.DataFrame:
    """Capacity = expected weekly load of the supplier's primary SKUs x headroom (x weekly noise)."""
    rng = ctx.rngs("supplier_capacity")
    sup, ss = ctx["supplier"], ctx["supplier_sku"]
    sc = ctx.cfg["supply"]
    W = ctx.cfg.weeks
    exp_sku = ctx.aux["exp_rate"].groupby(level="sku_id").sum()
    prim = ss[ss["is_primary"]]
    load = prim.assign(r=prim["sku_id"].map(exp_sku).fillna(0.0)).groupby("supplier_id")["r"].sum()
    load = load.reindex(sup["supplier_id"], fill_value=0.0)
    positive = load[load > 0]
    floor_load = float(positive.median()) if len(positive) else 100.0
    constrained = ctx.aux["constrained"].reindex(sup["supplier_id"]).to_numpy()
    lo_n, hi_n = sc["headroom_normal"]
    lo_c, hi_c = sc["headroom_constrained"]
    head = np.where(constrained, rng.uniform(lo_c, hi_c, len(sup)), rng.uniform(lo_n, hi_n, len(sup)))
    base_load = np.where(load.to_numpy() > 0, load.to_numpy(), floor_load * rng.uniform(0.2, 0.6, len(sup)))
    base = base_load * head
    noise = rng.uniform(0.92, 1.08, (len(sup), W))
    cap = np.maximum(1, np.floor(base[:, None] * noise + 0.5)).astype(np.int64)
    df = pd.DataFrame({
        "supplier_id": np.repeat(sup["supplier_id"].to_numpy(), W),
        "week": np.tile(cal.week_labels(W), len(sup)),
        "capacity_qty_ea": cap.ravel(),
    })
    df.insert(0, "capacity_id", [ids.capacity_id(i + 1) for i in range(len(df))])
    return df


def gen_production_capacity(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("production_capacity")
    loc, sku = ctx["location"], ctx["sku"]
    W = ctx.cfg.weeks
    plants = loc.loc[loc["location_type"] == "PLANT", "location_id"].to_numpy()
    exp = ctx.aux["exp_rate"]
    make = set(sku.loc[sku["sourcing_type"] == "MAKE", "sku_id"])
    e = exp[[s in make for s in exp.index.get_level_values("sku_id")]]
    load = e.groupby(level="location_id").sum().reindex(plants, fill_value=0.0)
    base = np.where(load.to_numpy() > 0, load.to_numpy(), 100.0) * rng.uniform(1.1, 1.4, len(plants))
    noise = rng.uniform(0.95, 1.05, (len(plants), W))
    maint = rng.integers(0, W, len(plants))            # one maintenance week per plant at 50%
    noise[np.arange(len(plants)), maint] *= 0.5
    cap = np.maximum(1, np.floor(base[:, None] * noise + 0.5)).astype(np.int64)
    return pd.DataFrame({
        "plant_location_id": np.repeat(plants, W),
        "week": np.tile(cal.week_labels(W), len(plants)),
        "capacity_qty_ea": cap.ravel(),
    })
