"""Quality gate (MASTER_PROMPT 10.1): scan inputs before planning, log every hit to ``exception`` and
apply the documented action. Works only on ``data/`` tables; never reads ground truth.

Order of rules matters and is fixed:
1. UOM_MISMATCH        -> CORRECTED  (qty_base_ea = order_qty x supplier_sku conversion)
2. LEAD_TIME_IGNORED   -> CORRECTED  (receipt week = order week + lane lead time [+ delay])
3. DUPLICATE_INBOUND   -> EXCLUDED   (extra / wrong-qty IN_TRANSIT rows; receipt counted once from the PO)
4. SUPPLIER_CAPACITY_EXCEEDED -> CAPPED (POs allocated in po_id order; the PO that overflows gets the exception,
   the other POs of the supplier-week are listed in related_record_ids; overflow pushed to the next week with
   spare capacity)

UNAPPROVED_SUBSTITUTE is checked later on mitigation options (decide, Phase 4).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from planner import calendar as cal

OPEN_STATUS = ("CREATED", "CONFIRMED", "SHIPPED", "IN_TRANSIT", "DELAYED")

RULES = {
    "UOM_MISMATCH": "qty_base_ea != order_qty x supplier_sku.uom_conversion, or order_uom/uom_conversion != supplier_sku",
    "LEAD_TIME_IGNORED": "expected_receipt_week < week(order_date) + lead_time.total_lead_weeks",
    "DUPLICATE_INBOUND": "more than one IN_TRANSIT row per po_id, or IN_TRANSIT qty_ea != PO qty_base_ea",
    "SUPPLIER_CAPACITY_EXCEEDED": "open PO qty_base_ea per supplier and receipt week > supplier_capacity.capacity_qty_ea",
}


@dataclass
class GateResult:
    exceptions: pd.DataFrame          # exception rows (without exception_id; numbered by the caller)
    po: pd.DataFrame                  # all POs after corrections
    inventory: pd.DataFrame           # inventory without EXCLUDED rows
    firm_receipts: pd.DataFrame       # po_id, supplier_id, sku_id, location_id, w (1-based), qty_ea, pushed_from, capacity_id
    supplier_remaining: pd.DataFrame  # supplier x week matrix of capacity left after firm receipts (>= 0)
    capacity_ids: pd.DataFrame        # supplier x week matrix of capacity_id


def _exc(issue, entity, record, related, action, severity, detail):
    return {"issue_type": issue, "entity": entity, "record_id": record, "related_record_ids": related,
            "detection_rule": RULES[issue], "action_taken": action, "severity": severity, "detail": detail}


def _uom(po: pd.DataFrame, ss: pd.DataFrame, out: list) -> pd.DataFrame:
    s = ss.set_index("supplier_sku_id")
    uom = po["supplier_sku_id"].map(s["purchase_uom"])
    conv = po["supplier_sku_id"].map(s["uom_conversion"]).astype(np.int64)
    expect = po["order_qty"].astype(np.int64) * conv
    bad = (po["order_uom"] != uom) | (po["uom_conversion"] != conv) | (po["qty_base_ea"] != expect)
    for i in np.flatnonzero(bad.to_numpy()):
        r = po.iloc[i]
        out.append(_exc("UOM_MISMATCH", "purchase_order", r["po_id"], r["supplier_sku_id"], "CORRECTED", "MEDIUM",
                        f"qty_base_ea {r['qty_base_ea']} -> {expect.iloc[i]} ({r['order_qty']} {uom.iloc[i]} x {conv.iloc[i]})"))
    po = po.copy()
    po["order_uom"], po["uom_conversion"], po["qty_base_ea"] = uom, conv, expect
    return po


def _lead_time(po: pd.DataFrame, lt: pd.DataFrame, out: list) -> pd.DataFrame:
    lw = po["lead_time_id"].map(lt.set_index("lead_time_id")["total_lead_weeks"]).astype(np.int64)
    order_w = po["order_date"].map(lambda s: cal.week_offset(pd.Timestamp(s).date()))
    exp_w = po["expected_receipt_week"].map(cal.label_offset)
    min_w = order_w + lw
    bad = exp_w < min_w
    po = po.copy()
    for i in np.flatnonzero(bad.to_numpy()):
        r = po.iloc[i]
        new_exp = cal.offset_label(int(min_w.iloc[i]))
        new_rev = cal.offset_label(int(min_w.iloc[i]) + int(r["delay_weeks"]))
        out.append(_exc("LEAD_TIME_IGNORED", "purchase_order", r["po_id"], r["lead_time_id"], "CORRECTED", "HIGH",
                        f"expected_receipt_week {r['expected_receipt_week']} -> {new_exp} "
                        f"(order week {cal.offset_label(int(order_w.iloc[i]))} + {lw.iloc[i]} wk); revised -> {new_rev}"))
        po.iat[i, po.columns.get_loc("expected_receipt_week")] = new_exp
        po.iat[i, po.columns.get_loc("revised_receipt_week")] = new_rev
    return po


def _duplicates(inv: pd.DataFrame, po: pd.DataFrame, out: list) -> pd.DataFrame:
    it = inv[inv["inventory_status"] == "IN_TRANSIT"].sort_values("inventory_id", kind="mergesort")
    rank = it.groupby("po_id").cumcount()
    po_qty = it["po_id"].map(po.set_index("po_id")["qty_base_ea"])
    dup = rank > 0
    drop = []
    for (iid, pid, q, pq, is_dup) in zip(it["inventory_id"], it["po_id"], it["qty_ea"], po_qty, dup):
        if is_dup:
            detail = f"second IN_TRANSIT row for {pid} ({q} EA); receipt counted once from the PO"
        elif q != pq:
            detail = f"IN_TRANSIT qty {q} EA != PO qty_base_ea {pq}; receipt taken from the PO"
        else:
            continue
        out.append(_exc("DUPLICATE_INBOUND", "inventory", iid, pid, "EXCLUDED", "MEDIUM", detail))
        drop.append(iid)
    return inv[~inv["inventory_id"].isin(drop)].reset_index(drop=True)


def _capacity(po: pd.DataFrame, cap: pd.DataFrame, weeks: int, out: list):
    """Firm receipts at revised week; overflow above capacity pushed to the next week with spare capacity."""
    op = po[po["status"].isin(OPEN_STATUS)].copy()
    op["w"] = op["revised_receipt_week"].map(cal.label_offset).clip(lower=1)  # overdue open POs land in W01
    capm = cap.pivot(index="supplier_id", columns="week", values="capacity_qty_ea").sort_index()
    cid = cap.pivot(index="supplier_id", columns="week", values="capacity_id").sort_index()
    week_cols = cal.week_labels(weeks)
    capm, cid = capm[week_cols], cid[week_cols]
    rem = capm.to_numpy(dtype=np.int64).copy()
    sidx = {s: i for i, s in enumerate(capm.index)}
    inh = op[op["w"] <= weeks].sort_values(["supplier_id", "w", "po_id"], kind="mergesort")
    load = np.zeros_like(rem)
    np.add.at(load, (inh["supplier_id"].map(sidx).to_numpy(), inh["w"].to_numpy() - 1), inh["qty_base_ea"].to_numpy())
    rem = rem - load   # negative where exceeded
    receipts = []
    for sup, g in inh.groupby("supplier_id", sort=True):
        s = sidx[sup]
        for w, gw in g.groupby("w", sort=True):
            wi = int(w) - 1
            capacity = int(capm.iat[s, wi])
            over = rem[s, wi] < 0
            used = 0
            for r in gw.itertuples(index=False):
                q = int(r.qty_base_ea)
                fit = q if not over else max(0, min(q, capacity - used))
                used += fit
                base = {"po_id": r.po_id, "supplier_id": sup, "sku_id": r.sku_id, "location_id": r.ship_to_location_id}
                if fit > 0:
                    receipts.append({**base, "w": int(w), "qty_ea": fit, "pushed_from": None, "capacity_id": None})
                if not over:
                    continue
                left = q - fit
                if left == 0:
                    continue                    # fits: listed as related on the overflowing PO's exception
                moved = []
                for w2 in range(wi + 1, weeks):
                    if left == 0:
                        break
                    take = min(left, max(0, int(rem[s, w2])))
                    if take > 0:
                        rem[s, w2] -= take
                        left -= take
                        receipts.append({**base, "w": w2 + 1, "qty_ea": take, "pushed_from": int(w),
                                         "capacity_id": cid.iat[s, wi]})
                        moved.append(f"{take} EA to {cal.week_label(w2 + 1)}")
                if left > 0:
                    moved.append(f"{left} EA beyond the horizon")
                others = [p for p in gw["po_id"] if p != r.po_id]
                out.append(_exc("SUPPLIER_CAPACITY_EXCEEDED", "purchase_order", r.po_id,
                                ";".join([cid.iat[s, wi]] + others), "CAPPED",
                                "HIGH", f"{sup} load {capacity - int(rem[s, wi])} EA > capacity {capacity} EA in "
                                f"{cal.week_label(w)}; {fit} EA fit, pushed " + ", ".join(moved)))
            if over:
                rem[s, wi] = 0
    rec = pd.DataFrame(receipts, columns=["po_id", "supplier_id", "sku_id", "location_id", "w", "qty_ea",
                                          "pushed_from", "capacity_id"])
    remaining = pd.DataFrame(np.maximum(rem, 0), index=capm.index, columns=week_cols)
    return rec, remaining, cid


def run(tables: dict[str, pd.DataFrame], weeks: int) -> GateResult:
    out: list[dict] = []
    po = tables["purchase_order"].sort_values("po_id").reset_index(drop=True)
    po = _uom(po, tables["supplier_sku"], out)
    po = _lead_time(po, tables["lead_time"], out)
    inv = _duplicates(tables["inventory"], po, out)
    rec, remaining, cid = _capacity(po, tables["supplier_capacity"], weeks, out)
    exc = pd.DataFrame(out, columns=["issue_type", "entity", "record_id", "related_record_ids", "detection_rule",
                                     "action_taken", "severity", "detail"])
    return GateResult(exc, po, inv, rec, remaining, cid)
