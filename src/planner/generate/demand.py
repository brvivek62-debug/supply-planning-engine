"""Step 4: forecast_demand (FINISHED SKUs only), then derived safety stock on sku_location."""
from __future__ import annotations

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner.schema import CATEGORIES

from .common import Ctx, round_half_up


def gen_forecast_demand(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("forecast_demand")
    dc = ctx.cfg["demand"]
    W = ctx.cfg.weeks
    sku = ctx["sku"].set_index("sku_id")
    sl = ctx["sku_location"]
    fin = sl[sl["sku_id"].map(sku["sku_type"]) == "FINISHED"][["sku_id", "location_id"]].reset_index(drop=True)
    n_pairs = len(fin)
    # Category seasonality: sinusoid with category-specific phase and amplitude (drawn in a fixed order).
    srng = ctx.rngs("seasonality")
    amp_lo, amp_hi = dc["seasonality_amplitude"]
    phase = {c: srng.uniform(0, 2 * np.pi) for c in CATEGORIES}
    amp = {c: srng.uniform(amp_lo, amp_hi) for c in CATEGORIES}
    ctx.aux["seasonality"] = {"phase": phase, "amp": amp}

    rate = ctx.aux["exp_rate"].reindex(pd.MultiIndex.from_frame(fin)).to_numpy()
    cat = fin["sku_id"].map(sku["category"]).to_numpy()
    wk = np.arange(1, W + 1)
    ph = np.array([phase[c] for c in cat])
    am = np.array([amp[c] for c in cat])
    season = 1.0 + am[:, None] * np.sin(2 * np.pi * (wk[None, :] - 1) / 52.0 + ph[:, None])   # (pairs, W)

    rate2 = np.repeat(rate[:, None], W, axis=1)
    promo = rng.random((n_pairs, W)) < dc["promo_rate"]
    lo, hi = dc["promo_multiplier"]
    mult = np.where(promo, rng.uniform(lo, hi, (n_pairs, W)), 1.0)
    mean = rate2 * season * mult
    size = float(dc["nb_dispersion"])
    p = size / (size + mean)
    noisy = rng.negative_binomial(size, p)
    baseline = round_half_up(rate2)
    forecast = np.where(promo, np.maximum(noisy, np.ceil(2 * baseline)), noisy).astype(np.int64)
    driver = np.where(promo, "PROMO", np.where(season >= dc["seasonal_peak_threshold"], "SEASONAL_PEAK", "BASELINE"))

    labels = np.array(cal.week_labels(W), dtype=object)
    starts = np.array([cal.week_start(i).isoformat() for i in wk], dtype=object)
    sku_col = np.repeat(fin["sku_id"].to_numpy(dtype=object), W)
    loc_col = np.repeat(fin["location_id"].to_numpy(dtype=object), W)
    week_col = np.tile(labels, n_pairs)
    df = pd.DataFrame({
        "demand_id": "DEM-" + pd.Series(sku_col) + "-" + pd.Series(loc_col) + "-" + pd.Series(week_col),
        "sku_id": sku_col, "location_id": loc_col, "week": week_col,
        "week_start_date": np.tile(starts, n_pairs),
        "baseline_qty_ea": baseline.ravel(), "forecast_qty_ea": forecast.ravel(), "demand_driver": driver.ravel(),
    })
    return df


def mean_weekly_demand(ctx: Ctx) -> pd.Series:
    """Mean weekly forecast per FINISHED SKU-location, plus dependent mean for components at plants."""
    fd = ctx["forecast_demand"]
    fin = fd.groupby(["sku_id", "location_id"])["forecast_qty_ea"].mean()
    bom = ctx["bom"]
    hp = ctx.aux["home_plant"]
    dep = bom.assign(location_id=bom["parent_sku_id"].map(hp))
    dep["m"] = [fin.get((p, l), 0.0) * q for p, l, q in dep[["parent_sku_id", "location_id", "qty_per"]].itertuples(index=False)]
    dep = dep.groupby(["component_sku_id", "location_id"])["m"].sum()
    dep.index.names = ["sku_id", "location_id"]
    sl = ctx["sku_location"]
    idx = pd.MultiIndex.from_frame(sl[["sku_id", "location_id"]])
    return fin.combine_first(dep).reindex(idx, fill_value=0.0)


def fill_safety_stock(ctx: Ctx) -> None:
    """safety_stock_qty = round(safety_stock_weeks x mean weekly (dependent) demand)."""
    sl = ctx["sku_location"]
    mean = mean_weekly_demand(ctx)
    ctx.aux["mean_demand"] = mean
    ssw = sl["sku_id"].map(ctx["sku"].set_index("sku_id")["safety_stock_weeks"]).to_numpy()
    sl["safety_stock_qty"] = round_half_up(ssw * mean.to_numpy())
