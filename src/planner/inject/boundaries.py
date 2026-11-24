"""Boundary cases (valid; the quality gate must NOT raise an exception on them).

PO_QTY_EQUALS_MOQ              an open, not yet shipped PO is set to exactly its supplier_sku MOQ (MOQ is a multiple
                               of the order multiple, so the PO stays valid); drawn from the POs closest to their MOQ
SUPPLIER_LOAD_EQUALS_CAPACITY  supplier_capacity.capacity_qty_ea of a supplier-week with firm load is set to exactly
                               that load (the gate's rule is load > capacity); drawn from supplier-weeks whose spare
                               capacity the baseline MRP did not use, so the plan is unchanged
RECEIPT_EQUALS_LEAD_TIME       an open PO whose expected receipt week is exactly order week + total_lead_weeks
                               (the gate's rule is expected < order week + lead time)
"""
from __future__ import annotations

import pandas as pd

from planner import calendar as cal

from .common import Book, open_pos

UNSHIPPED = ("CREATED", "CONFIRMED")


def _gt(issue, entity, record, related, rule, outcome):
    return {"case_class": "BOUNDARY", "issue_type": issue, "entity": entity, "record_id": record,
            "related_record_ids": related, "injection_rule": rule, "expected_detection_rule": "none",
            "expected_action": "none", "expected_outcome": outcome}


def inject(t: dict[str, pd.DataFrame], cfg, rngs, book: Book, base) -> list[dict]:
    """``base`` is a PlanRun of the baseline (its MRP shows which supplier capacity is unused)."""
    n = int(cfg["injection"]["boundary_per_type"])
    po = t["purchase_order"]
    ss = t["supplier_sku"].set_index("supplier_sku_id")
    ops = open_pos(t, cfg)
    rows: list[dict] = []

    # --- PO qty exactly = MOQ
    c = ops[ops["status"].isin(UNSHIPPED)].copy()
    c["moq"] = c["supplier_sku_id"].map(ss["moq"]).astype(int)
    c["mult"] = c["supplier_sku_id"].map(ss["order_multiple"]).astype(int)
    c = c[(c["moq"] % c["mult"] == 0) & (c["order_qty"] >= c["moq"])].assign(ratio=lambda d: d["order_qty"] / d["moq"])
    for r in book.pick(rngs("inject.boundary.moq"), c, n, prefer="ratio"):
        i = po.index[po["po_id"] == r.po_id][0]
        po.loc[i, "order_qty"] = int(r.moq)
        po.loc[i, "qty_base_ea"] = int(r.moq) * int(r.uom_conversion)
        change = (f"order_qty {r.order_qty} -> MOQ {r.moq} {r.order_uom}" if r.order_qty != r.moq
                  else f"selected PO already at MOQ {r.moq} {r.order_uom} (no change needed)")
        rows.append(_gt("PO_QTY_EQUALS_MOQ", "purchase_order", r.po_id, r.supplier_sku_id,
                        f"{change} (qty_base_ea {int(r.moq) * int(r.uom_conversion)})",
                        "no exception; PO planned at exactly MOQ"))

    # --- supplier-week load exactly = capacity (capacity set to the firm load of a week the MRP left unused)
    cap = t["supplier_capacity"]
    mrp_rem = pd.DataFrame(base.mrp.supplier_remaining, index=base.mrp.grid.supplier_ids,
                           columns=cal.week_labels(cfg.weeks))
    c = ops[(ops["spare"] > 0) & (ops["spare"] < ops["capacity"])].copy()
    c["unused"] = [int(mrp_rem.at[s, cal.week_label(int(w))]) == sp
                   for s, w, sp in zip(c["supplier_id"], c["w"], c["spare"])]
    c = c[c["unused"]].assign(spare_frac=lambda d: d["spare"] / d["capacity"])
    for r in book.pick(rngs("inject.boundary.capacity"), c, n, prefer="spare_frac"):
        wk = cal.week_label(int(r.w))
        load = int(r.capacity) - int(r.spare)
        i = cap.index[cap["capacity_id"] == r.capacity_id][0]
        cap.loc[i, "capacity_qty_ea"] = load
        rows.append(_gt("SUPPLIER_LOAD_EQUALS_CAPACITY", "supplier_capacity", r.capacity_id, r.supplier_id,
                        f"capacity_qty_ea {int(r.capacity)} -> {load} EA = firm PO load of {r.supplier_id} in {wk} "
                        f"(spare unused by the baseline MRP)", f"no exception; {r.supplier_id} {wk} exactly full"))

    # --- receipt exactly at order week + lead time
    c = ops[ops["exp_w"] == ops["order_w"] + ops["L"]]
    for r in book.pick(rngs("inject.boundary.lead_time"), c, n):
        rows.append(_gt("RECEIPT_EQUALS_LEAD_TIME", "purchase_order", r.po_id, r.lead_time_id,
                        f"selected PO with expected_receipt_week {r.expected_receipt_week} = order week "
                        f"{cal.offset_label(int(r.order_w))} + {int(r.L)} wk exactly (no change needed)",
                        "no exception; receipt kept at the stated week"))
    return rows
