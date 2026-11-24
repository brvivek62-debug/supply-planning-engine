"""Step 3: lead_time, supplier_capacity, production_capacity."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids
from planner.schema import REGIONS

from .common import Ctx
from .demand import promo_uplift, season_factor

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


def expected_sku_load(ctx: Ctx) -> pd.DataFrame:
    """Expected weekly demand per SKU (all locations) incl. category seasonality and expected promo uplift.

    FINISHED: latent rate x category season. COMPONENT: sum over BOM parents of parent load x qty_per.
    """
    sku = ctx["sku"].set_index("sku_id")
    exp_sku = ctx.aux["exp_rate"].groupby(level="sku_id").sum()
    fin = [s for s in exp_sku.index if sku.at[s, "sku_type"] == "FINISHED"]
    m = pd.DataFrame(exp_sku.loc[fin].to_numpy()[:, None] * season_factor(ctx, sku.loc[fin, "category"].to_numpy())
                     * promo_uplift(ctx), index=fin)
    comp = {}
    for p, c, q in ctx["bom"][["parent_sku_id", "component_sku_id", "qty_per"]].itertuples(index=False):
        if p in m.index:
            comp[c] = comp.get(c, 0.0) + m.loc[p].to_numpy() * q
    cm = pd.DataFrame.from_dict(comp, orient="index", columns=m.columns) if comp else None
    return pd.concat([m, cm]) if cm is not None else m


def gen_supplier_capacity(ctx: Ctx) -> pd.DataFrame:
    """Capacity per week = the supplier's expected (seasonal) load of its primary SKUs in that week
    x headroom (x weekly noise). About `disruption_supplier_share` of loaded suppliers get one temporary
    disruption window (`disruption_weeks` long) where capacity drops to `disruption_factor` x normal;
    the window and the disrupted flag are latent (never exported)."""
    rng = ctx.rngs("supplier_capacity")
    sup, ss = ctx["supplier"], ctx["supplier_sku"]
    sc = ctx.cfg["supply"]
    W = ctx.cfg.weeks
    sku_load = expected_sku_load(ctx)
    # level-correct the latent seasonal curve to the published forecast (NB noise, promo floor and rounding
    # put realized demand a few % above the latent rate); the seasonal shape is kept
    real = ctx.aux["mean_demand"].groupby(level="sku_id").sum()
    lvl = (real.reindex(sku_load.index).fillna(0.0) / sku_load.mean(axis=1).clip(lower=1e-9)).clip(0.5, 2.0)
    sku_load = sku_load.mul(lvl, axis=0)
    prim = ss[ss["is_primary"] & ss["sku_id"].isin(sku_load.index)]
    wload = sku_load.loc[prim["sku_id"]].set_index(prim["supplier_id"].to_numpy()).groupby(level=0).sum()
    wload = wload.reindex(sup["supplier_id"], fill_value=0.0)
    load = wload.mean(axis=1)
    shape = (wload.to_numpy() / np.maximum(load.to_numpy()[:, None], 1e-9))
    shape = np.where(load.to_numpy()[:, None] > 0, shape, 1.0)
    positive = load[load > 0]
    floor_load = float(positive.median()) if len(positive) else 100.0
    lo_n, hi_n = sc["headroom_normal"]
    head = rng.uniform(lo_n, hi_n, len(sup))
    base_load = np.where(load.to_numpy() > 0, load.to_numpy(), floor_load * rng.uniform(0.2, 0.6, len(sup)))
    base = base_load * head
    noise = rng.uniform(0.92, 1.08, (len(sup), W))
    # temporary disruptions: only suppliers that carry primary load, window fully inside the horizon
    factor = np.ones((len(sup), W))
    loaded = np.flatnonzero(load.to_numpy() > 0)
    n_dis = int(round(len(sup) * float(sc["disruption_supplier_share"])))
    dlo, dhi = sc["disruption_weeks"]
    flo, fhi = sc["disruption_factor"]
    disrupted = np.zeros(len(sup), dtype=bool)
    # one of the disrupted suppliers is a component supplier (weighted by its primary component count), so
    # MAKE plants see a component disruption; the rest are drawn from all loaded suppliers
    is_comp = ctx["sku"].set_index("sku_id")["sku_type"].eq("COMPONENT")
    ncomp = prim[prim["sku_id"].map(is_comp)].groupby("supplier_id").size().reindex(sup["supplier_id"], fill_value=0)
    ncomp = ncomp.to_numpy()[loaded].astype(float)
    picks = []
    if n_dis > 0 and ncomp.sum() > 0:
        picks.append(int(rng.choice(loaded, p=ncomp / ncomp.sum())))
    rest = np.setdiff1d(loaded, picks)
    picks += rng.choice(rest, size=min(n_dis - len(picks), len(rest)), replace=False).tolist()
    comp_pick = picks[0] if picks and ncomp.sum() > 0 else -1
    for k in sorted(picks):
        dur = int(rng.integers(dlo, dhi + 1))
        # the component disruption is already under way at the planning run (starts W02-W04, inside most lead
        # times, so it cannot be pre-built for); others start after W02 and end >= 8 weeks before W52
        start = int(rng.integers(1, 4)) if k == comp_pick else int(rng.integers(2, W - dur - 8))
        factor[k, start:start + dur] = rng.uniform(flo, fhi)
        disrupted[k] = True
    ctx.aux["disrupted"] = pd.Series(disrupted, index=sup["supplier_id"].to_numpy())   # latent, never exported
    cap = np.maximum(1, np.floor(base[:, None] * shape * noise * factor + 0.5)).astype(np.int64)
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
    # expected (seasonal) weekly load per plant: MAKE rate x its category season
    cats = sku.set_index("sku_id").loc[e.index.get_level_values("sku_id"), "category"].to_numpy()
    wl = e.to_numpy()[:, None] * season_factor(ctx, cats) * promo_uplift(ctx)
    real = ctx.aux["mean_demand"].reindex(e.index).fillna(0.0).to_numpy()   # level-correct as for suppliers
    wl = wl * np.clip(real / np.maximum(wl.mean(axis=1), 1e-9), 0.5, 2.0)[:, None]
    wl = pd.DataFrame(wl, index=e.index.get_level_values("location_id"))
    wl = wl.groupby(level=0).sum().reindex(plants, fill_value=0.0).to_numpy()
    load = wl.mean(axis=1)
    shape = np.where(load[:, None] > 0, wl / np.maximum(load[:, None], 1e-9), 1.0)
    base = np.where(load > 0, load, 100.0) * rng.uniform(1.1, 1.4, len(plants))
    noise = rng.uniform(0.95, 1.05, (len(plants), W))
    maint = rng.integers(0, W, len(plants))            # one maintenance week per plant at 50%
    noise[np.arange(len(plants)), maint] *= 0.5
    cap = np.maximum(1, np.floor(base[:, None] * shape * noise + 0.5)).astype(np.int64)
    return pd.DataFrame({
        "plant_location_id": np.repeat(plants, W),
        "week": np.tile(cal.week_labels(W), len(plants)),
        "capacity_qty_ea": cap.ravel(),
    })
