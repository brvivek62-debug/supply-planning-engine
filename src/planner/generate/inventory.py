"""Step 6: inventory_txn (event ledger) -> inventory (ON_HAND from the ledger, IN_TRANSIT from open POs)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids

from .common import Ctx, rand_dates, round_half_up

TXN_ORDER = {"OPENING": 0, "RECEIPT": 1, "ADJUSTMENT": 2, "ISSUE": 3}


def _lead_weeks(ctx: Ctx, sl: pd.DataFrame) -> np.ndarray:
    """Primary standard-lane lead time to each SKU-location's region (1 week for MAKE)."""
    sku = ctx["sku"].set_index("sku_id")
    region = ctx["location"].set_index("location_id")["region"]
    ss = ctx["supplier_sku"]
    prim = ss[ss["is_primary"]].set_index("sku_id")["supplier_id"]
    lt = ctx["lead_time"]
    as_of = ctx.as_of.isoformat()
    std = lt[(~lt["is_expedite_lane"]) & (lt["effective_from"] <= as_of)
             & (lt["effective_to"].isna() | (lt["effective_to"].fillna("9999") >= as_of))]
    lw = std.set_index(["supplier_id", "dest_region"])["total_lead_weeks"]
    out = []
    for s, l in sl[["sku_id", "location_id"]].itertuples(index=False):
        if sku.at[s, "sourcing_type"] == "MAKE":
            out.append(1)
        else:
            out.append(int(lw[(prim[s], region[l])]))
    return np.array(out)


def gen_inventory(ctx: Ctx) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = ctx.rngs("inventory_txn")
    ic = ctx.cfg["inventory"]
    sl = ctx["sku_location"][["sku_id", "location_id"]].reset_index(drop=True)
    n = len(sl)
    mean = ctx.aux["mean_demand"].reindex(pd.MultiIndex.from_frame(sl)).to_numpy()
    ssw = sl["sku_id"].map(ctx["sku"].set_index("sku_id")["safety_stock_weeks"]).to_numpy()
    lead = _lead_weeks(ctx, sl)
    ctx.aux["primary_lead_weeks"] = pd.Series(lead, index=pd.MultiIndex.from_frame(sl))
    noise = float(ic["opening_cover_noise"])
    cover = (lead + ssw) * rng.uniform(1 - noise, 1 + noise, n) * float(ic.get("opening_cover_bias", 1.0))

    po = ctx["purchase_order"]
    # Just-in-time cover: for a share of SKU-locations with an open, delayed PO the opening stock was planned to
    # last only until the PO's original expected week (SS + demand up to it + a fraction of a week), so the delay
    # lands in thin stock. Own stream: the other inventory draws are unchanged.
    trng = ctx.rngs("inventory_jit_cover")
    dl = po[(po["delay_weeks"] > 0) & ~po["status"].isin(["RECEIVED", "CANCELLED"])].copy()
    dl["exp"] = dl["expected_receipt_week"].map(cal.label_offset)
    dl = dl[dl["exp"] >= 1].groupby(["sku_id", "ship_to_location_id"])["exp"].min()
    pos = pd.Series(np.arange(n), index=pd.MultiIndex.from_frame(sl))
    hit = pos.reindex(dl.index).dropna().astype(int)
    pick = trng.random(len(hit)) < float(ic.get("delayed_po_thin_share", 0.0))
    lo_f, hi_f = ic.get("delayed_po_thin_cover", [0.2, 0.7])
    frac = trng.uniform(lo_f, hi_f, len(hit))
    # components: the plant has already run its line-side buffer (SS) down waiting for the late PO
    is_comp = sl["sku_id"].map(ctx["sku"].set_index("sku_id")["sku_type"]).eq("COMPONENT").to_numpy()
    rows_all = hit.to_numpy()
    pick |= is_comp[rows_all]
    rows = rows_all[pick]
    cover[rows] = np.where(is_comp[rows], 0.0, ssw[rows]) + (dl.loc[hit.index].to_numpy()[pick] - 1) + frac[pick]
    target = round_half_up(cover * mean)

    recv = po[po["status"] == "RECEIVED"].copy()
    recv["txn_date"] = recv["po_id"].map(ctx.aux["po_receipt_date"])
    rsum = recv.groupby(["sku_id", "ship_to_location_id"])["qty_base_ea"].sum()
    rsum.index.names = ["sku_id", "location_id"]
    receipts = rsum.reindex(pd.MultiIndex.from_frame(sl), fill_value=0).to_numpy()

    hist_weeks = int(ic.get("history_weeks", 17))
    hist = round_half_up(mean * hist_weeks * rng.uniform(0.9, 1.1, n))
    has_adj = rng.random(n) < float(ic["adjustment_rate"])
    adj_mag = np.maximum(1, round_half_up(rng.uniform(0.01, 0.05, n) * np.maximum(target, 1)))
    adj = np.where(has_adj, np.where(rng.random(n) < 0.5, -1, 1) * adj_mag, 0)
    opening = target + hist - receipts - adj
    issue = -hist
    low = opening < 1
    opening = np.where(low, 1, opening)
    issue = np.where(low, target - 1 - receipts - adj, issue)

    hist_start = date.fromisoformat(str(ctx.cfg["history_start_date"]))
    issue_date = (ctx.as_of - timedelta(days=1)).isoformat()
    adj_dates = rand_dates(rng, hist_start + timedelta(days=1), ctx.as_of - timedelta(days=2), n)
    skus, locs = sl["sku_id"].to_numpy(dtype=object), sl["location_id"].to_numpy(dtype=object)
    parts = [
        pd.DataFrame({"sku_id": skus, "location_id": locs, "txn_type": "OPENING", "qty_ea": opening,
                      "txn_date": hist_start.isoformat(), "ref_id": None}),
        pd.DataFrame({"sku_id": recv["sku_id"].to_numpy(), "location_id": recv["ship_to_location_id"].to_numpy(),
                      "txn_type": "RECEIPT", "qty_ea": recv["qty_base_ea"].to_numpy(),
                      "txn_date": recv["txn_date"].to_numpy(), "ref_id": recv["po_id"].to_numpy()}),
    ]
    m = issue != 0
    parts.append(pd.DataFrame({"sku_id": skus[m], "location_id": locs[m], "txn_type": "ISSUE", "qty_ea": issue[m],
                               "txn_date": issue_date, "ref_id": None}))
    parts.append(pd.DataFrame({"sku_id": skus[has_adj], "location_id": locs[has_adj], "txn_type": "ADJUSTMENT",
                               "qty_ea": adj[has_adj], "txn_date": np.array(adj_dates, dtype=object)[has_adj],
                               "ref_id": None}))
    txn = pd.concat(parts, ignore_index=True)
    txn["_o"] = txn["txn_type"].map(TXN_ORDER)
    txn = txn.sort_values(["sku_id", "location_id", "txn_date", "_o", "ref_id"], kind="mergesort", na_position="first")
    txn = txn.drop(columns="_o").reset_index(drop=True)
    txn.insert(0, "txn_id", [ids.txn_id(i + 1) for i in range(len(txn))])
    txn["qty_ea"] = txn["qty_ea"].astype(np.int64)

    # ON_HAND is derived from the ledger, never generated independently.
    on_hand = txn.groupby(["sku_id", "location_id"])["qty_ea"].sum().reindex(pd.MultiIndex.from_frame(sl), fill_value=0)
    oh = sl.assign(inventory_status="ON_HAND", qty_ea=on_hand.to_numpy(), po_id=None)
    open_ship = po[po["status"].isin(["SHIPPED", "IN_TRANSIT", "DELAYED"])].sort_values("po_id")
    it = pd.DataFrame({"sku_id": open_ship["sku_id"].to_numpy(), "location_id": open_ship["ship_to_location_id"].to_numpy(),
                       "inventory_status": "IN_TRANSIT", "qty_ea": open_ship["qty_base_ea"].to_numpy(),
                       "po_id": open_ship["po_id"].to_numpy()})
    inv = pd.concat([oh, it], ignore_index=True)
    inv.insert(0, "inventory_id", [ids.inventory_id(i + 1) for i in range(len(inv))])
    inv["as_of_date"] = ctx.as_of.isoformat()
    return txn, inv
