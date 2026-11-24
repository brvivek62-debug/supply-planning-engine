"""Projected available balance (MASTER_PROMPT 10.2), backlog semantics.

opening(W01) = ON_HAND; opening(w) = closing(w-1);
closing(w) = opening + firm_receipts + planned_receipts + production_receipts - demand_qty.
Unmet demand is carried forward as a negative balance (backlog), never dropped. Integer units only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from planner import calendar as cal


def closing_matrix(opening: np.ndarray, firm: np.ndarray, planned: np.ndarray, prod: np.ndarray,
                   demand: np.ndarray) -> np.ndarray:
    net = firm + planned + prod - demand
    return opening[:, None] + np.cumsum(net, axis=1)


def balance_status(closing: np.ndarray, ss: np.ndarray) -> np.ndarray:
    return np.where(closing < 0, "STOCKOUT", np.where(closing < ss[:, None], "BELOW_SS", "OK"))


def to_frame(keys: pd.DataFrame, opening: np.ndarray, firm: np.ndarray, planned: np.ndarray, prod: np.ndarray,
             demand: np.ndarray, ss: np.ndarray) -> pd.DataFrame:
    """Long projected_balance table (one row per SKU-location-week)."""
    n, w = demand.shape
    closing = closing_matrix(opening, firm, planned, prod, demand)
    open_w = np.concatenate([opening[:, None], closing[:, :-1]], axis=1)
    return pd.DataFrame({
        "sku_id": np.repeat(keys["sku_id"].to_numpy(dtype=object), w),
        "location_id": np.repeat(keys["location_id"].to_numpy(dtype=object), w),
        "week": np.tile(np.array(cal.week_labels(w), dtype=object), n),
        "opening_qty": open_w.ravel().astype(np.int64),
        "firm_receipts": firm.ravel().astype(np.int64),
        "planned_receipts": planned.ravel().astype(np.int64),
        "production_receipts": prod.ravel().astype(np.int64),
        "demand_qty": demand.ravel().astype(np.int64),
        "closing_qty": closing.ravel().astype(np.int64),
        "safety_stock_qty": np.repeat(ss.astype(np.int64), w),
        "balance_status": balance_status(closing, ss).ravel(),
    })


def reconcile(pab: pd.DataFrame, on_hand: pd.Series) -> pd.Series:
    """Boolean mask of rows that break the PAB identity (expects rows sorted by sku, location, week).

    Checks: closing = opening + receipts - demand; opening(W01) = ON_HAND; opening(w) = closing(w-1);
    balance_status consistent with closing vs safety stock.
    """
    p = pab
    ident = p["closing_qty"] != (p["opening_qty"] + p["firm_receipts"] + p["planned_receipts"]
                                 + p["production_receipts"] - p["demand_qty"])
    first = p["week"] == cal.week_label(1)
    key = pd.MultiIndex.from_frame(p[["sku_id", "location_id"]])
    oh = pd.Series(on_hand.reindex(key).to_numpy(), index=p.index)
    bad_open1 = first & (p["opening_qty"] != oh)
    prev_close = p.groupby(["sku_id", "location_id"], sort=False)["closing_qty"].shift(1)
    bad_chain = ~first & (p["opening_qty"] != prev_close)
    exp_status = np.where(p["closing_qty"] < 0, "STOCKOUT",
                          np.where(p["closing_qty"] < p["safety_stock_qty"], "BELOW_SS", "OK"))
    bad_status = p["balance_status"] != exp_status
    return ident | bad_open1 | bad_chain | bad_status
