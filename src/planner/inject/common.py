"""Shared helpers for the injectors: candidate POs and a reservation book that keeps targets disjoint."""
from __future__ import annotations

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner.plan import quality_gate


class Book:
    """Records / supplier-weeks already used by an injected case."""

    def __init__(self):
        self.records: set[str] = set()
        self.supplier_weeks: set[tuple[str, int]] = set()

    def free(self, r) -> bool:
        return r.po_id not in self.records and (r.supplier_id, int(r.w)) not in self.supplier_weeks

    def take(self, r) -> None:
        self.records.add(r.po_id)
        self.supplier_weeks.add((r.supplier_id, int(r.w)))

    def pick(self, rng: np.random.Generator, cand: pd.DataFrame, n: int, prefer: str | None = None) -> list:
        """Up to n random candidate rows (sorted by po_id first, so the draw is reproducible), each in a
        supplier-week no other case uses. With ``prefer`` the draw is limited to the 3n free candidates with the
        smallest value of that column (e.g. least spare capacity, so a case disturbs the plan as little as possible)."""
        cand = cand.sort_values("po_id", kind="mergesort")
        if prefer is not None:
            free = cand[[self.free(r) for r in cand.itertuples(index=False)]]
            free = free.sort_values([prefer, "po_id"], kind="mergesort").drop_duplicates(["supplier_id", "w"])
            cand = free.head(3 * n).sort_values("po_id", kind="mergesort")
        rows = list(cand.itertuples(index=False))
        out = []
        for k in rng.permutation(len(rows)):
            r = rows[int(k)]
            if len(out) >= n:
                break
            if self.free(r):
                self.take(r)
                out.append(r)
        return sorted(out, key=lambda r: r.po_id)


def open_pos(t: dict[str, pd.DataFrame], cfg) -> pd.DataFrame:
    """Open POs with a single in-horizon firm receipt, plus order/expected weeks, lane lead time and the spare
    capacity of their supplier-week (as the quality gate sees it)."""
    W = cfg.weeks
    gate = quality_gate.run(t, W)
    fr = gate.firm_receipts
    n = fr.groupby("po_id").size()
    one = fr[fr["po_id"].map(n) == 1].set_index("po_id")
    po = t["purchase_order"]
    po = po[po["status"].isin(quality_gate.OPEN_STATUS) & po["po_id"].isin(one.index)].copy()
    po["w"] = po["po_id"].map(one["w"]).astype(int)
    lt = t["lead_time"].set_index("lead_time_id")
    po["L"] = po["lead_time_id"].map(lt["total_lead_weeks"]).astype(int)
    po["order_w"] = po["order_date"].map(lambda s: cal.week_offset(pd.Timestamp(s).date()))
    po["exp_w"] = po["expected_receipt_week"].map(cal.label_offset)
    po["rev_w"] = po["revised_receipt_week"].map(cal.label_offset)
    rem = gate.supplier_remaining
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])
    weeks = po["w"].map(cal.week_label)
    po["spare"] = [int(rem.at[s, wk]) for s, wk in zip(po["supplier_id"], weeks)]
    po["capacity"] = [int(cap.at[(s, wk), "capacity_qty_ea"]) for s, wk in zip(po["supplier_id"], weeks)]
    po["capacity_id"] = [cap.at[(s, wk), "capacity_id"] for s, wk in zip(po["supplier_id"], weeks)]
    inv = t["inventory"]
    po["has_mirror"] = po["po_id"].isin(set(inv.loc[inv["inventory_status"] == "IN_TRANSIT", "po_id"]))
    return po[(po["w"] >= 1) & (po["w"] <= W) & (po["rev_w"] >= 1)].reset_index(drop=True)
