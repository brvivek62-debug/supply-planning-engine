"""Step 5: purchase_order and purchase_order_event.

Each PO is drawn for a BUY SKU-location from an APPROVED supplier_sku of an ACTIVE supplier that is
effective on the order date, on the supplier's standard lane to the ship-to region that is effective on
the order date. Status is never assigned directly: it is the last event of a timeline that is cut off at
the as-of date. Open POs are allocated against supplier capacity at their revised receipt week, so the
baseline never exceeds capacity (exceedances only come from injected anomalies).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids

from .common import Ctx, d, is_blank

ACTOR = {
    "CREATED": ("PLANNER", "PO_CREATED"), "CONFIRMED": ("SUPPLIER", "SUPPLIER_CONFIRMED"),
    "SHIPPED": ("SUPPLIER", "DISPATCHED"), "IN_TRANSIT": ("CARRIER", "HANDED_TO_CARRIER"),
    "DELAYED": ("CARRIER", "TRANSPORT_DELAY"), "RECEIVED": ("WAREHOUSE_OPERATOR", "GOODS_RECEIVED"),
}
CLEARED = ("LOGISTICS_ANALYST", "DELAY_CLEARED")


def _effective(ef: str, et: str | None, on: date) -> bool:
    return d(ef) <= on and (is_blank(et) or on <= d(et))


def _monday(off: int) -> datetime:
    return datetime.combine(cal.date_of_offset(off), time())


class _PODrawer:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.rng = ctx.rngs("purchase_order")
        cfg = ctx.cfg
        self.start = d(str(cfg["po_order_date_start"]))
        self.end = min(d(str(cfg["po_order_date_end"])), ctx.as_of - timedelta(days=1))
        self.start_off = cal.week_offset(self.start)
        sl = ctx["sku_location"]
        sku = ctx["sku"].set_index("sku_id")
        region = ctx["location"].set_index("location_id")["region"]
        buy = sl[sl["sku_id"].map(sku["sourcing_type"]) == "BUY"].reset_index(drop=True)
        mean = ctx.aux["mean_demand"].reindex(pd.MultiIndex.from_frame(buy[["sku_id", "location_id"]])).to_numpy()
        self.pairs = buy[["sku_id", "location_id"]].to_numpy(dtype=object)
        self.pair_region = buy["location_id"].map(region).to_numpy(dtype=object)
        self.pair_mean = np.maximum(mean, 0.0)
        pc = ctx.cfg["purchase_orders"]
        w = np.power(self.pair_mean + 0.05, float(pc.get("pair_weight_power", 0.5)))
        self.w = w / w.sum()
        self.cover = tuple(pc.get("cover_weeks", [1.5, 3.5]))
        sup_status = ctx["supplier"].set_index("supplier_id")["status"]
        ss = ctx["supplier_sku"]
        ok = ss[(ss["approval_status"] == "APPROVED") & (ss["supplier_id"].map(sup_status) == "ACTIVE")]
        self.options: dict[str, list[dict]] = {}
        for r in ok.sort_values(["sku_id", "is_primary", "supplier_sku_id"], ascending=[True, False, True]).to_dict("records"):
            self.options.setdefault(r["sku_id"], []).append(r)
        lt = ctx["lead_time"]
        std = lt[~lt["is_expedite_lane"]]
        self.lanes: dict[tuple[str, str], list[dict]] = {}
        for r in std.to_dict("records"):
            self.lanes.setdefault((r["supplier_id"], r["dest_region"]), []).append(r)

    def lane(self, supplier: str, region: str, on: date) -> dict:
        for r in self.lanes[(supplier, region)]:
            if _effective(r["effective_from"], r["effective_to"], on):
                return r
        raise LookupError((supplier, region, on))

    def _date_between(self, lo: date, hi: date) -> date:
        return lo + timedelta(days=int(self.rng.integers(0, (hi - lo).days + 1)))

    def draw(self, cls: str, delay: int = 0) -> dict:
        rng = self.rng
        for _ in range(1000):
            i = int(rng.choice(len(self.pairs), p=self.w))
            sku_id, loc_id = self.pairs[i]
            region = self.pair_region[i]
            opts = self.options.get(sku_id)
            if not opts:
                continue
            primary = opts[0]
            choice = primary if (len(opts) == 1 or rng.random() < 0.85) else opts[1 + int(rng.integers(0, len(opts) - 1))]
            lane0 = self.lane(choice["supplier_id"], region, self.end)
            L = int(lane0["total_lead_weeks"])
            if cls == "RECEIVED":
                max_off = -(L + delay)
                if max_off < self.start_off:
                    continue
                hi = min(self.end, cal.date_of_offset(max_off) + timedelta(days=6))
                od = self._date_between(self.start, hi)
            elif cls == "OPEN":
                lo = max(self.start, cal.date_of_offset(1 - L))
                od = self._date_between(lo, self.end)
            else:
                od = self._date_between(self.start, self.end - timedelta(days=2))
            if not _effective(choice["effective_from"], choice["effective_to"], od):
                choice = primary
            lane = self.lane(choice["supplier_id"], region, od)
            L = int(lane["total_lead_weeks"])
            order_off = cal.week_offset(od)
            if cls == "RECEIVED" and order_off + L + delay > 0:
                continue
            if cls == "OPEN" and order_off + L < 1:
                continue
            conv, om, moq = int(choice["uom_conversion"]), int(choice["order_multiple"]), int(choice["moq"])
            target_ea = rng.uniform(*self.cover) * max(self.pair_mean[i], 1.0)
            units = max(moq, int(math.ceil(target_ea / conv / om)) * om)
            t0 = datetime.combine(od, time(8)) + timedelta(minutes=int(rng.integers(0, 600)))
            return dict(
                cls=cls, sku_id=sku_id, ship_to_location_id=loc_id, supplier_id=choice["supplier_id"],
                supplier_sku_id=choice["supplier_sku_id"], lead_time_id=lane["lead_time_id"],
                prod=int(lane["production_days"]), transit=int(lane["transit_days"]),
                order_date=od, order_off=order_off, L=L, delay=delay,
                order_qty=units, order_uom=choice["purchase_uom"], uom_conversion=conv, moq=moq, om=om,
                t0=t0, end_jit=timedelta(days=int(rng.integers(0, 5)), hours=9 + int(rng.integers(0, 8)),
                                         minutes=int(rng.integers(0, 60))),
                cancel_confirm=bool(rng.random() < 0.6), cancel_gap_h=int(rng.integers(24, 240)),
                cancel_by_supplier=bool(rng.random() < 0.4), delayed_frac=float(rng.uniform(0.1, 0.9)),
            )
        raise RuntimeError(f"could not draw a feasible {cls} purchase order")


def _timeline(po: dict, cutoff: datetime) -> list[tuple[str | None, str, datetime, str, str]]:
    """Events as (from_status, to_status, ts, actor_role, reason_code), all strictly before cutoff."""
    t0 = po["t0"]
    raw: list[tuple[str, datetime, tuple[str, str]]] = [("CREATED", t0, ACTOR["CREATED"])]
    if po["cls"] == "CANCELLED":
        last = t0
        if po["cancel_confirm"]:
            last = t0 + timedelta(hours=int(4 + po["cancel_gap_h"] % 40))
            raw.append(("CONFIRMED", last, ACTOR["CONFIRMED"]))
        ts = min(last + timedelta(hours=po["cancel_gap_h"]), cutoff - timedelta(hours=1))
        if ts <= last:
            ts = last + (cutoff - last) / 2
        who = ("SUPPLIER", "SUPPLIER_REJECTED") if (po["cancel_by_supplier"] and po["cancel_confirm"]) else ("PLANNER", "DEMAND_CHANGE")
        raw.append(("CANCELLED", ts, who))
    else:
        f_s = min(0.7, max(0.2, po["prod"] / max(1, po["prod"] + po["transit"])))
        f_it = f_s + 0.05
        if po["cls"] == "RECEIVED":
            end = _monday(po["order_off"] + po["L"] + po["delay"]) + po["end_jit"]
        else:
            end = _monday(po["order_off"] + po["L"]) + po["end_jit"]
        span = end - t0
        raw += [("CONFIRMED", t0 + 0.08 * span, ACTOR["CONFIRMED"]),
                ("SHIPPED", t0 + f_s * span, ACTOR["SHIPPED"]),
                ("IN_TRANSIT", t0 + f_it * span, ACTOR["IN_TRANSIT"])]
        if po["cls"] == "RECEIVED":
            if po["delay"] > 0:
                raw += [("DELAYED", t0 + (f_it + 0.25 * (1 - f_it)) * span, ACTOR["DELAYED"]),
                        ("IN_TRANSIT", t0 + (f_it + 0.8 * (1 - f_it)) * span, CLEARED)]
            raw.append(("RECEIVED", end, ACTOR["RECEIVED"]))
        else:
            raw = [e for e in raw if e[1] < cutoff]
            if po["delay"] > 0:
                it = raw[-1][1]
                raw.append(("DELAYED", it + (cutoff - it) * po["delayed_frac"], ACTOR["DELAYED"]))
    out, prev = [], None
    for status, ts, (actor, reason) in raw:
        ts = ts.replace(microsecond=0)
        out.append((prev, status, ts, actor, reason))
        prev = status
    return out


def gen_purchase_orders(ctx: Ctx) -> tuple[pd.DataFrame, pd.DataFrame]:
    cfg = ctx.cfg
    pc = cfg["purchase_orders"]
    N = int(cfg.volumes["purchase_orders"])
    W = cfg.weeks
    drawer = _PODrawer(ctx)
    rng = ctx.rngs("purchase_order_status")
    cutoff = datetime.combine(ctx.as_of, time())
    n_recv = int(round(N * pc["received_share"]))
    n_canc = int(round(N * pc["cancelled_share"]))
    classes = ["RECEIVED"] * n_recv + ["CANCELLED"] * n_canc + ["OPEN"] * (N - n_recv - n_canc)
    dlo, dhi = pc["delay_weeks"]
    rate = float(pc["delay_rate"])
    pos = []
    for cls in classes:
        delay = int(rng.integers(dlo, dhi + 1)) if (cls == "RECEIVED" and rng.random() < rate) else 0
        pos.append(drawer.draw(cls, delay))

    # Delays on open POs are only known once the shipment is in transit before the as-of date.
    def in_transit_before_cutoff(po: dict) -> bool:
        ev = _timeline(po, cutoff)
        return ev[-1][1] == "IN_TRANSIT" and ev[-1][2] < cutoff - timedelta(hours=2)

    eligible = [i for i, po in enumerate(pos) if po["cls"] == "OPEN" and in_transit_before_cutoff(po)]
    need = max(0, int(round(N * rate)) - sum(1 for po in pos if po["delay"] > 0))
    for i in sorted(rng.choice(eligible, size=min(need, len(eligible)), replace=False).tolist()) if eligible else []:
        pos[i]["delay"] = int(rng.integers(dlo, dhi + 1))

    # Allocate open POs against supplier capacity at their revised receipt week.
    cap = ctx["supplier_capacity"]
    remaining = {(s, cal.week_num(w)): int(q) for s, w, q in cap[["supplier_id", "week", "capacity_qty_ea"]].itertuples(index=False)}
    order = sorted((i for i, po in enumerate(pos) if po["cls"] == "OPEN"), key=lambda i: (pos[i]["t0"], i))
    for i in order:
        po = pos[i]
        for _ in range(500):
            wk = po["order_off"] + po["L"] + po["delay"]
            key = (po["supplier_id"], wk)
            if wk > W or key not in remaining:
                break
            need_ea = po["order_qty"] * po["uom_conversion"]
            rem = remaining[key]
            if need_ea <= rem:
                remaining[key] = rem - need_ea
                break
            fit_units = (rem // po["uom_conversion"] // po["om"]) * po["om"]
            if fit_units >= po["moq"] and fit_units > 0:
                po["order_qty"] = int(fit_units)
                remaining[key] = rem - fit_units * po["uom_conversion"]
                break
            po = drawer.draw("OPEN", 0)
        else:
            raise RuntimeError("could not place an open PO within supplier capacity")
        pos[i] = po

    pos.sort(key=lambda p: (p["order_date"], p["t0"], p["sku_id"], p["ship_to_location_id"]))
    po_rows, ev_rows = [], []
    for n, po in enumerate(pos, 1):
        pid = ids.po_id(n)
        events = _timeline(po, cutoff)
        exp_off = po["order_off"] + po["L"]
        po_rows.append(dict(
            po_id=pid, supplier_id=po["supplier_id"], sku_id=po["sku_id"],
            ship_to_location_id=po["ship_to_location_id"], supplier_sku_id=po["supplier_sku_id"],
            lead_time_id=po["lead_time_id"], order_date=po["order_date"].isoformat(),
            order_qty=po["order_qty"], order_uom=po["order_uom"], uom_conversion=po["uom_conversion"],
            qty_base_ea=po["order_qty"] * po["uom_conversion"],
            expected_receipt_week=cal.offset_label(exp_off), delay_weeks=po["delay"],
            revised_receipt_week=cal.offset_label(exp_off + po["delay"]),
            status=events[-1][1], created_by_role="PLANNER",
            _receipt_date=events[-1][2].date().isoformat() if events[-1][1] == "RECEIVED" else None,
        ))
        for seq, (frm, to, ts, actor, reason) in enumerate(events, 1):
            ev_rows.append(dict(po_id=pid, seq=seq, from_status=frm, to_status=to,
                                event_ts=ts.isoformat(timespec="seconds"), actor_role=actor, reason_code=reason))
    po_df = pd.DataFrame(po_rows)
    ctx.aux["po_receipt_date"] = po_df.set_index("po_id")["_receipt_date"]
    po_df = po_df.drop(columns="_receipt_date")
    ev_df = pd.DataFrame(ev_rows)
    ev_df.insert(0, "event_id", [ids.po_event_id(i + 1) for i in range(len(ev_df))])
    return po_df, ev_df
