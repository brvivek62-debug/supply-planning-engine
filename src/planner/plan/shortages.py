"""Shortages, rule-based causes, explanations and priority (MASTER_PROMPT 10.5) plus the opening
shortage lifecycle event (10.6; later events are added by decide).

A shortage is a maximal run of consecutive weeks with closing_qty < safety_stock_qty for one SKU-location.
Cause rules (each hit is one shortage_cause row pointing at its evidence record):

DEMAND_SPIKE        PROMO / SEASONAL_PEAK week in the run with forecast >= 1.5x baseline -> demand_id
PO_DELAYED          open PO of this SKU-location due (expected) on/before the run end and delayed to
                    a revised week on/after the run start -> po_id
SUPPLIER_CAPACITY   planned order received in the run or up to cover-weeks before it with capacity_cut_ea > 0
                    -> planned_order_id; firm PO receipt pushed by the quality gate from on/before the run end
                    into/after the run start -> capacity_id of the overloaded supplier-week
LEAD_TIME_FENCE     BUY: run starts before the first week a new order released in W01 can arrive -> lead_time_id
LOW_OPENING_STOCK   run starts inside the replenishment lead time (BUY lane lead time; MAKE firm horizon) and
                    ON_HAND covers fewer weeks of mean demand than that lead time -> inventory_id
COMPONENT_SHORTAGE  MAKE production cut for lack of a component in the run or up to firm-weeks before it
                    -> prod_plan_id (+ bom_id of the limiting component)
PRODUCTION_CAPACITY MAKE production cut by plant capacity in the run or up to firm-weeks before it -> prod_plan_id
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids
from planner.schema import CAUSE_CODES

from .quality_gate import GateResult
from .replenish import MrpResult

CRIT_WEIGHT = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
SPIKE_RATIO = 1.5
# evidence types that count towards a cause's total contribution (bom rows only identify the component)
MAIN_EVIDENCE = {"COMPONENT_SHORTAGE": "production_plan"}
# causes whose unmet quantity is carried forward and re-requested each week: contribution = largest cut, not sum
BACKLOG_CAUSES = ("SUPPLIER_CAPACITY", "COMPONENT_SHORTAGE", "PRODUCTION_CAPACITY")


def runs(closing: np.ndarray, ss: np.ndarray) -> list[tuple[int, int, int]]:
    """(row, start, end) of every maximal run with closing < safety stock, 0-based inclusive weeks."""
    below = (closing < ss[:, None]).astype(np.int8)
    pad = np.zeros((below.shape[0], 1), dtype=np.int8)
    d = np.diff(np.concatenate([pad, below, pad], axis=1), axis=1)
    sr, sc = np.nonzero(d == 1)
    er, ec = np.nonzero(d == -1)
    return [(int(r), int(s), int(e) - 1) for r, s, e in zip(sr, sc, ec)]   # nonzero is row-major: pairs align


def _wk(i: int) -> str:
    return cal.week_label(i + 1)


def _span(s: int, e: int) -> str:
    return _wk(s) if s == e else f"{_wk(s)}–W{e + 1:02d}"


def _n(x) -> str:
    return f"{int(round(x)):,}"


class _Evidence:
    def __init__(self, t: dict[str, pd.DataFrame], gate: GateResult, mrp: MrpResult, cfg):
        g = mrp.grid
        self.g, self.mrp, self.cfg = g, mrp, cfg
        self.W = g.W
        self.cover = int(cfg["policy"]["planned_order_cover_weeks"])
        self.firm_weeks = int(cfg["production"]["firm_weeks"])
        key = pd.MultiIndex.from_frame(g.keys)
        self.row = pd.Series(np.arange(len(g.keys)), index=key)
        weeks = cal.week_labels(self.W)
        widx = {w: i for i, w in enumerate(weeks)}

        fd = t["forecast_demand"]
        spike = fd[fd["demand_driver"].isin(["PROMO", "SEASONAL_PEAK"])
                   & (fd["forecast_qty_ea"] >= SPIKE_RATIO * fd["baseline_qty_ea"]) & (fd["baseline_qty_ea"] >= 0)]
        self.spikes = defaultdict(list)
        for r in spike.itertuples(index=False):
            self.spikes[int(self.row[(r.sku_id, r.location_id)])].append(
                (widx[r.week], r.demand_id, r.demand_driver, int(r.forecast_qty_ea), int(r.baseline_qty_ea)))

        po = gate.po
        op = po[po["status"].isin(["CREATED", "CONFIRMED", "SHIPPED", "IN_TRANSIT", "DELAYED"]) & (po["delay_weeks"] > 0)]
        self.delayed = defaultdict(list)
        for r in op.itertuples(index=False):
            i = self.row.get((r.sku_id, r.ship_to_location_id))
            if i is not None:
                self.delayed[int(i)].append((r.po_id, cal.label_offset(r.expected_receipt_week) - 1,
                                             cal.label_offset(r.revised_receipt_week) - 1, int(r.qty_base_ea),
                                             int(r.delay_weeks)))
        self.pushed = defaultdict(list)
        fr = gate.firm_receipts
        for r in fr[fr["pushed_from"].notna()].itertuples(index=False):
            self.pushed[int(self.row[(r.sku_id, r.location_id)])].append(
                (r.po_id, int(r.pushed_from) - 1, int(r.w) - 1, int(r.qty_ea), r.capacity_id, r.supplier_id))

        self.cut_orders = defaultdict(list)
        pl = mrp.planned_orders.rename(columns={"_row": "row", "_q": "q"})
        for r in pl[pl["capacity_cut_ea"] > 0].itertuples(index=False):
            self.cut_orders[int(r.row)].append((r.planned_order_id, int(r.q), int(r.capacity_cut_ea),
                                                r.supplier_id, r.capacity_id, int(r.requested_qty_ea)))

        self.misses = defaultdict(list)
        for r in mrp.capacity_misses.itertuples(index=False):
            self.misses[int(r.row)].append((int(r.q), int(r.cut_ea), r.capacity_id, r.supplier_id))

        self.prod_cuts = defaultdict(list)
        pp = mrp.production_plan
        for r in pp[(pp["capacity_cut_ea"] > 0) | (pp["component_cut_ea"] > 0)].itertuples(index=False):
            self.prod_cuts[int(r.row)].append((r.prod_plan_id, int(r.w), int(r.capacity_cut_ea),
                                               int(r.component_cut_ea), mrp.limiting_bom.get((int(r.row), int(r.w)))))
        bom = t["bom"].set_index("bom_id")
        self.bom_component = bom["component_sku_id"].to_dict()

        # replenishment lead time and mean demand per row
        n = len(g.keys)
        self.fence = np.zeros(n, dtype=np.int64)       # first week index a W01 release can arrive (BUY)
        self.fence_lt = np.empty(n, dtype=object)
        self.fence_mode = np.empty(n, dtype=object)
        self.lead = np.full(n, self.firm_weeks, dtype=np.int64)
        for i in np.flatnonzero(g.is_buy):
            li = g.lane_idx[i]
            self.fence[i] = g.lane_L[li, 0]
            self.lead[i] = g.lane_L[li, 0]
            self.fence_lt[i], self.fence_mode[i] = g.lane_lt[li, 0], g.lane_mode[li, 0]
        self.mean = mrp.demand.mean(axis=1)


def _causes(ev: _Evidence, i: int, s: int, e: int, closing: np.ndarray, ss: int, on_hand: int):
    g = ev.g
    out = []   # (cause, evidence_type, evidence_id, qty, clause, sort_key)
    for w, did, drv, f, b in sorted(ev.spikes.get(i, [])):
        if s <= w <= e:
            pct = (f / b - 1) * 100 if b > 0 else float("inf")
            out.append(("DEMAND_SPIKE", "forecast_demand", did, f - b,
                        f"{drv} demand +{pct:.0f}% vs baseline in {_wk(w)} ({did})"))
    for pid, exp, rev, q, dly in sorted(ev.delayed.get(i, [])):
        if exp <= e and rev >= s and rev > exp:
            out.append(("PO_DELAYED", "purchase_order", pid, q,
                        f"{pid} ({_n(q)} EA) delayed {dly} wk from {cal.offset_label(exp + 1)} to {cal.offset_label(rev + 1)}"))
    for pid, frm, to, q, cap_id, sup in sorted(ev.pushed.get(i, [])):
        if frm <= e and to >= s:
            out.append(("SUPPLIER_CAPACITY", "supplier_capacity", cap_id, q,
                        f"{_n(q)} EA of {pid} pushed from {_wk(frm)} to {_wk(to)} by {sup} capacity ({cap_id})"))
    for plid, q, cut, sup, cap_id, req in sorted(ev.cut_orders.get(i, [])):
        if s - ev.cover <= q <= e:
            out.append(("SUPPLIER_CAPACITY", "planned_order", plid, cut,
                        f"{sup} capacity cut planned order {plid} by {_n(cut)} of {_n(req)} EA for {_wk(q)} ({cap_id})"))
    miss = sorted(m for m in ev.misses.get(i, []) if s - ev.cover <= m[0] <= e)
    if miss:
        q, cut, cap_id, sup = max(miss, key=lambda m: (m[1], -m[0]))
        weeks = sorted({m[0] for m in miss})
        out.append(("SUPPLIER_CAPACITY", "supplier_capacity", cap_id, cut,
                    f"{sup} had no spare capacity for this SKU in {_span(weeks[0], weeks[-1])} ({len(weeks)} wk); "
                    f"unmet requirement reached {_n(cut)} EA by {_wk(q)} ({cap_id})"))
    if g.is_buy[i] and s < ev.fence[i]:
        hi = min(e, ev.fence[i] - 1)
        gap = int(np.max(ss - closing[s:hi + 1]))
        out.append(("LEAD_TIME_FENCE", "lead_time", ev.fence_lt[i], gap,
                    f"no new order can land before {_wk(int(ev.fence[i]))} ({ev.fence_lt[i]}, "
                    f"{int(ev.fence[i])}-week {ev.fence_mode[i]} lane)"))
    lead = int(ev.lead[i])
    mean = float(ev.mean[i])
    if s < lead and mean > 0 and on_hand < lead * mean:
        cover = on_hand / mean
        what = f"{lead}-week lead time" if g.is_buy[i] else f"{lead}-week firm production horizon"
        out.append(("LOW_OPENING_STOCK", "inventory", g.on_hand_id[i], int(math.ceil(lead * mean - on_hand)),
                    f"opening stock {_n(on_hand)} EA covers {cover:.1f} weeks vs {what} ({g.on_hand_id[i]})"))
    comp_rows, cap_rows = defaultdict(list), []
    for ppid, w, capcut, compcut, bom_id in sorted(ev.prod_cuts.get(i, [])):
        if s - ev.firm_weeks <= w <= e:
            if compcut > 0:
                comp_rows[bom_id].append((w, compcut, ppid))
            if capcut > 0:
                cap_rows.append((w, capcut, ppid))
    for bom_id, rows in sorted(comp_rows.items()):
        pw, pq, pp = max(rows, key=lambda x: (x[1], -x[0]))
        comp = ev.bom_component.get(bom_id, "?")
        for w, q, ppid in rows:
            clause = None
            if ppid == pp:
                clause = (f"production cut in {_span(rows[0][0], rows[-1][0])} ({len(rows)} wk, up to {_n(pq)} EA in "
                          f"{_wk(pw)}) because component {comp} was short ({pp}, {bom_id})")
            out.append(("COMPONENT_SHORTAGE", "production_plan", ppid, q, clause))
        out.append(("COMPONENT_SHORTAGE", "bom", bom_id, pq, None))
    if cap_rows:
        pw, pq, pp = max(cap_rows, key=lambda x: (x[1], -x[0]))
        for w, q, ppid in cap_rows:
            clause = None
            if ppid == pp:
                clause = (f"plant capacity cut production in {_span(cap_rows[0][0], cap_rows[-1][0])} ({len(cap_rows)} wk, "
                          f"up to {_n(pq)} EA in {_wk(pw)}) ({pp})")
            out.append(("PRODUCTION_CAPACITY", "production_plan", ppid, q, clause))
    return out


def build(t: dict[str, pd.DataFrame], gate: GateResult, mrp: MrpResult, cfg):
    g = mrp.grid
    ev = _Evidence(t, gate, mrp, cfg)
    sku = t["sku"].set_index("sku_id")
    closing = mrp.closing
    found = runs(closing, g.ss)
    keys_sku = g.keys["sku_id"].to_numpy()
    keys_loc = g.keys["location_id"].to_numpy()
    found.sort(key=lambda x: (x[1], keys_sku[x[0]], keys_loc[x[0]]))
    shortages, causes = [], []
    for n, (i, s, e) in enumerate(found, start=1):
        sid = ids.shortage_id(n)
        seg = closing[i, s:e + 1]
        ss = int(g.ss[i])
        mn = int(seg.min())
        peak = s + int(np.argmin(seg))
        qty = max(0, -mn)
        below = int(np.max(ss - seg))
        stype = "STOCKOUT" if mn < 0 else "BELOW_SAFETY_STOCK"
        sk = keys_sku[i]
        price = float(sku.at[sk, "unit_price_inr"])
        var = round(qty * price, 2)
        urgency = 1.0 / math.sqrt(1 + s)
        score = var * CRIT_WEIGHT[sku.at[sk, "criticality"]] * urgency
        found_c = _causes(ev, i, s, e, closing[i], ss, int(g.opening[i]))
        if not found_c:
            raise AssertionError(f"shortage {sid} ({sk}/{keys_loc[i]} {_span(s, e)}) has no cause")
        totals = defaultdict(int)
        for c, et, _, q, _ in found_c:
            if MAIN_EVIDENCE.get(c, et) != et:
                continue
            # capacity / component cuts are re-requested every week (backlog), so the largest cut is the contribution
            totals[c] = max(totals[c], q) if c in BACKLOG_CAUSES else totals[c] + q
        primary = sorted(totals, key=lambda c: (-totals[c], CAUSE_CODES.index(c)))[0]
        clauses = defaultdict(list)
        for c, et, _, q, cl in found_c:
            if cl:
                clauses[c].append((q, cl))
        parts = []
        for c in sorted(totals, key=lambda c: (-totals[c], CAUSE_CODES.index(c))):
            items = sorted(clauses[c], key=lambda x: -x[0])
            txt = items[0][1]
            if len(items) > 1:
                txt += f" (+{len(items) - 1} more)"
            parts.append(txt)
        head = (f"Stockout of {_n(qty)} EA" if stype == "STOCKOUT" else f"Below safety stock by {_n(below)} EA")
        explanation = (f"{head} for {sk} at {keys_loc[i]} in {_span(s, e)} (low point {_n(mn)} EA in {_wk(peak)}, "
                       f"safety stock {_n(ss)} EA): " + "; ".join(parts) + ".")
        shortages.append({"shortage_id": sid, "sku_id": sk, "location_id": keys_loc[i], "start_week": _wk(s),
                          "end_week": _wk(e), "peak_week": _wk(peak), "shortage_type": stype, "shortage_qty_ea": qty,
                          "below_ss_qty_ea": below, "value_at_risk_inr": var, "primary_cause": primary,
                          "explanation": explanation, "priority_score": round(score, 4), "status": "OPEN",
                          "_row": i, "_s": s, "_e": e})
        seen = set()
        for c, et, eid, q, _ in found_c:
            k = (c, eid)
            if k in seen:          # same evidence twice for one cause -> sum
                for r in causes:
                    if r["shortage_id"] == sid and r["cause_code"] == c and r["evidence_id"] == eid:
                        r["contribution_qty_ea"] += int(q)
                continue
            seen.add(k)
            causes.append({"shortage_id": sid, "cause_code": c, "evidence_type": et, "evidence_id": eid,
                           "contribution_qty_ea": int(q)})
    cols = ["shortage_id", "sku_id", "location_id", "start_week", "end_week", "peak_week", "shortage_type",
            "shortage_qty_ea", "below_ss_qty_ea", "value_at_risk_inr", "primary_cause", "explanation",
            "priority_score", "priority_rank", "status", "_row", "_s", "_e"]
    sh = pd.DataFrame(shortages, columns=[c for c in cols if c != "priority_rank"])
    if len(sh):
        order = sh.sort_values(["priority_score", "shortage_id"], ascending=[False, True], kind="mergesort").index
        sh.loc[order, "priority_rank"] = np.arange(1, len(sh) + 1)
        sh["priority_rank"] = sh["priority_rank"].astype(np.int64)
    else:
        sh["priority_rank"] = pd.Series(dtype=np.int64)
    sc = pd.DataFrame(causes, columns=["shortage_id", "cause_code", "evidence_type", "evidence_id", "contribution_qty_ea"])
    return sh[cols], sc


def opening_events(sh: pd.DataFrame, as_of) -> pd.DataFrame:
    """One OPEN event per shortage at the planning run (as-of date 06:00), raised by the PLANNER."""
    ts = datetime.combine(as_of, time(6, 0)).isoformat(timespec="seconds")
    ev = pd.DataFrame({
        "event_id": [ids.shortage_event_id(k + 1) for k in range(len(sh))],
        "shortage_id": sh.sort_values("shortage_id")["shortage_id"].to_numpy(),
        "seq": 1, "from_status": None, "to_status": "OPEN", "event_ts": ts, "actor_role": "PLANNER",
        "reason_code": "PAB_BELOW_SAFETY_STOCK", "mitigation_id": None,
    })
    return ev
