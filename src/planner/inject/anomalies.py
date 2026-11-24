"""Injected anomalies (MASTER_PROMPT 12). Each must be caught by the quality gate (or decide).

UOM_MISMATCH                open CASE PO: qty_base_ea set to order_qty (conversion skipped)
LEAD_TIME_IGNORED           open PO: expected / revised receipt week set to the order week. Targets are preferably
                            POs whose naive early receipt would hide a real shortage (the corrected plan shows it)
DUPLICATE_INBOUND           open shipped PO: a second IN_TRANSIT inventory row (new inventory_id)
SUPPLIER_CAPACITY_EXCEEDED  an extra CONFIRMED PO (continuing PO ids, with its event history) on a primary
                            supplier_sku so its supplier-week load exceeds capacity by 10-30%; drawn from the
                            least-spare supplier-weeks so the pushed overflow disturbs the plan little
UNAPPROVED_SUBSTITUTE       after a first planning pass: a MANUAL mitigation proposal (planner) for a real
                            shortage using a PENDING / REJECTED / not-effective substitute
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids
from planner.plan import quality_gate
from planner.plan.decide import UNAPPROVED_RULE
from planner.plan.replenish import _effective_on, lot_units

from .common import Book, open_pos

RULES = {**quality_gate.RULES, "UNAPPROVED_SUBSTITUTE": UNAPPROVED_RULE}


def _gt(issue, entity, record, related, rule, action, outcome):
    return {"case_class": "ANOMALY", "issue_type": issue, "entity": entity, "record_id": record,
            "related_record_ids": related, "injection_rule": rule, "expected_detection_rule": RULES[issue],
            "expected_action": action, "expected_outcome": outcome}


def _next(series: pd.Series) -> int:
    return max((ids.parse_counter(v) for v in series), default=0) + 1


def _uom(t, cfg, rngs, book, n) -> list[dict]:
    po = t["purchase_order"]
    ops = open_pos(t, cfg)
    c = ops[(ops["order_uom"] == "CASE") & (ops["uom_conversion"] > 1)]
    rows = []
    for r in book.pick(rngs("inject.uom_mismatch"), c, n):
        i = po.index[po["po_id"] == r.po_id][0]
        po.loc[i, "qty_base_ea"] = int(r.order_qty)
        rows.append(_gt("UOM_MISMATCH", "purchase_order", r.po_id, r.supplier_sku_id,
                        f"qty_base_ea {r.qty_base_ea} -> {r.order_qty} (order_qty {r.order_qty} CASE, conversion x"
                        f"{r.uom_conversion} skipped)", "CORRECTED",
                        f"qty_base_ea corrected to {r.qty_base_ea} EA; receipt {r.revised_receipt_week} unchanged"))
    return rows


def _lead_time(t, cfg, rngs, book, n, base) -> list[dict]:
    """``base`` is a PlanRun of the baseline: used to find POs whose naive early receipt would hide a shortage."""
    po = t["purchase_order"]
    ops = open_pos(t, cfg)
    ops = ops[ops["exp_w"] > ops["order_w"]]
    g, closing = base.mrp.grid, base.mrp.closing
    row = {k: i for i, k in enumerate(zip(g.keys["sku_id"], g.keys["location_id"]))}
    sh = base.outputs["shortage"]
    by_row: dict[int, list] = {}
    for r in sh.to_dict("records"):
        by_row.setdefault(int(r["_row"]), []).append(r)
    hides = {}
    fr = base.gate.firm_receipts.set_index("po_id")["qty_ea"]
    for r in ops.itertuples(index=False):
        i = row.get((r.sku_id, r.ship_to_location_id))
        q = int(fr.get(r.po_id, 0))
        rev0 = int(r.w) - 1
        for s in sorted(by_row.get(i, []), key=lambda x: x["_s"]):
            a, b = int(s["_s"]), min(int(s["_e"]), rev0 - 1)
            if a < rev0 and (closing[i, a:b + 1] + q >= g.ss[i]).all():
                hides[r.po_id] = s
                break
    pref = ops[ops["po_id"].isin(hides)]
    rng = rngs("inject.lead_time_ignored")
    picked = book.pick(rng, pref, n)
    picked += book.pick(rng, ops[~ops["po_id"].isin(hides)], n - len(picked))
    rows = []
    for r in sorted(picked, key=lambda r: r.po_id):
        i = po.index[po["po_id"] == r.po_id][0]
        naive = cal.offset_label(int(r.order_w))
        po.loc[i, ["expected_receipt_week", "revised_receipt_week"]] = naive
        s = hides.get(r.po_id)
        if s is not None:
            outcome = f"shortage {s['sku_id']}/{s['location_id']} starting {s['start_week']}"
        else:
            outcome = f"receipt corrected to {r.revised_receipt_week}; no shortage hidden"
        rows.append(_gt("LEAD_TIME_IGNORED", "purchase_order", r.po_id, r.lead_time_id,
                        f"expected/revised receipt {r.expected_receipt_week}/{r.revised_receipt_week} -> order week "
                        f"{naive} ({int(r.L)}-week lane ignored)", "CORRECTED", outcome))
    return rows


def _duplicate(t, cfg, rngs, book, n) -> list[dict]:
    inv = t["inventory"]
    ops = open_pos(t, cfg)
    c = ops[ops["has_mirror"]]
    rows, new = [], []
    k = _next(inv["inventory_id"])
    for r in book.pick(rngs("inject.duplicate_inbound"), c, n):
        src = inv[(inv["inventory_status"] == "IN_TRANSIT") & (inv["po_id"] == r.po_id)].iloc[0].copy()
        src["inventory_id"] = ids.inventory_id(k)
        k += 1
        new.append(src)
        rows.append(_gt("DUPLICATE_INBOUND", "inventory", src["inventory_id"], r.po_id,
                        f"second IN_TRANSIT row for {r.po_id} ({src['qty_ea']} EA)", "EXCLUDED",
                        f"duplicate excluded; {r.po_id} received once ({r.qty_base_ea} EA in {cal.week_label(int(r.w))})"))
    if new:
        t["inventory"] = pd.concat([inv, pd.DataFrame(new)], ignore_index=True)
    return rows


def _capacity(t, cfg, rngs, book, n) -> list[dict]:
    W = cfg.weeks
    rng = rngs("inject.supplier_capacity_exceeded")
    gate = quality_gate.run(t, W)
    rem = gate.supplier_remaining
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])
    region = t["location"].set_index("location_id")["region"]
    sku = t["sku"].set_index("sku_id")
    sst = t["supplier_sku"]
    prim = sst[sst["is_primary"].astype(bool) & (sst["approval_status"] == "APPROVED")]
    prim = prim[sku.loc[prim["sku_id"], "sourcing_type"].to_numpy() == "BUY"]
    locs = t["sku_location"].sort_values(["sku_id", "location_id"]).groupby("sku_id")["location_id"].apply(list)
    lt = t["lead_time"]
    lanes = {k: g.sort_values(["effective_from", "lead_time_id"]) for k, g in
             lt[~lt["is_expedite_lane"].astype(bool)].groupby(["supplier_id", "dest_region"])}
    as_of = cfg.as_of_date
    cands = []
    for p in prim.sort_values("supplier_sku_id").itertuples(index=False):
        for loc in locs.get(p.sku_id, []):
            g = lanes.get((p.supplier_id, region[loc]))
            lane = None if g is None else next(
                (x for x in g.itertuples(index=False) if _effective_on(x.effective_from, x.effective_to, as_of.isoformat())), None)
            if lane is None:
                continue
            for w in range(1, min(int(lane.total_lead_weeks), W) + 1):
                wk = cal.week_label(w)
                cands.append({"po_id": f"{p.supplier_sku_id}|{loc}|{w}", "supplier_id": p.supplier_id, "w": w,
                              "ss": p, "loc": loc, "lane": lane,
                              "spare_frac": rem.at[p.supplier_id, wk] / max(1, cap.at[(p.supplier_id, wk), "capacity_qty_ea"])})
    c = pd.DataFrame(cands)
    if len(c) == 0:
        return []
    po, ev = t["purchase_order"], t["purchase_order_event"]
    k_po, k_ev = _next(po["po_id"]), _next(ev["event_id"])
    new_po, new_ev, rows = [], [], []
    chosen = book.pick(rng, c[["po_id", "supplier_id", "w", "spare_frac"]], n, prefer="spare_frac")
    info = c.set_index("po_id")
    for r in chosen:
        x = info.loc[r.po_id]
        p, loc, lane, w = x["ss"], x["loc"], x["lane"], int(r.w)
        wk = cal.week_label(w)
        capacity = int(cap.at[(p.supplier_id, wk), "capacity_qty_ea"])
        spare = int(rem.at[p.supplier_id, wk])
        over = int(np.ceil(rng.uniform(0.10, 0.30) * capacity))
        conv = int(p.uom_conversion)
        units = lot_units(spare + over, conv, int(p.moq), int(p.order_multiple))
        order_off = w - int(lane.total_lead_weeks)
        day = cal.date_of_offset(order_off) + timedelta(days=int(rng.integers(0, 5)))
        day = min(day, as_of - timedelta(days=1))
        pid = ids.po_id(k_po)
        k_po += 1
        new_po.append({"po_id": pid, "supplier_id": p.supplier_id, "sku_id": p.sku_id, "ship_to_location_id": loc,
                       "supplier_sku_id": p.supplier_sku_id, "lead_time_id": lane.lead_time_id,
                       "order_date": day.isoformat(), "order_qty": units, "order_uom": p.purchase_uom,
                       "uom_conversion": conv, "qty_base_ea": units * conv, "expected_receipt_week": wk,
                       "delay_weeks": 0, "revised_receipt_week": wk, "status": "CONFIRMED",
                       "created_by_role": "PLANNER"})
        t0 = datetime.combine(day, time(9, 0))
        for seq, (frm, to, ts, actor, reason) in enumerate(
                [(None, "CREATED", t0, "PLANNER", "PO_CREATED"),
                 ("CREATED", "CONFIRMED", t0 + timedelta(hours=6), "SUPPLIER", "SUPPLIER_CONFIRMED")], 1):
            new_ev.append({"event_id": ids.po_event_id(k_ev), "po_id": pid, "seq": seq, "from_status": frm,
                           "to_status": to, "event_ts": ts.isoformat(timespec="seconds"), "actor_role": actor,
                           "reason_code": reason})
            k_ev += 1
        load = capacity - spare + units * conv
        rows.append(_gt("SUPPLIER_CAPACITY_EXCEEDED", "purchase_order", pid, cap.at[(p.supplier_id, wk), "capacity_id"],
                        f"extra CONFIRMED PO {units} {p.purchase_uom} ({units * conv} EA) of {p.sku_id} to {loc} due {wk}; "
                        f"{p.supplier_id} load {load} EA > capacity {capacity} EA", "CAPPED",
                        f"{load - capacity} EA of {pid} pushed from {wk} to later weeks with spare capacity"))
    if new_po:
        t["purchase_order"] = pd.concat([po, pd.DataFrame(new_po)], ignore_index=True)
        t["purchase_order_event"] = pd.concat([ev, pd.DataFrame(new_ev)], ignore_index=True)
    return rows


def inject_inputs(t: dict[str, pd.DataFrame], cfg, rngs, book: Book, base) -> list[dict]:
    n = int(cfg["injection"]["anomalies_per_type"])
    rows = _uom(t, cfg, rngs, book, n)
    rows += _lead_time(t, cfg, rngs, book, n, base)
    rows += _duplicate(t, cfg, rngs, book, n)
    rows += _capacity(t, cfg, rngs, book, n)
    return rows


def inject_proposals(t: dict[str, pd.DataFrame], res, cfg, rngs) -> tuple[pd.DataFrame, list[dict]]:
    """MANUAL planner proposals for real shortages (from planning run ``res``) using unusable substitutes: first a
    PENDING / REJECTED / not-effective sku_substitute rule of the short SKU; if there are too few of those, an
    ad-hoc substitute (same category, stocked at the same location) for which no sku_substitute rule exists."""
    n = int(cfg["injection"]["anomalies_per_type"])
    sh = res.outputs["shortage"].sort_values("shortage_id")
    sub = t["sku_substitute"].sort_values("substitute_id")
    sku = t["sku"].set_index("sku_id")
    rng = rngs("inject.unapproved_substitute")
    rule_c, adhoc_c = [], []
    pairs = set(zip(sub["sku_id"], sub["substitute_sku_id"]))
    stocked = t["sku_location"].groupby("location_id")["sku_id"].apply(lambda s: sorted(s))
    for s in sh.itertuples(index=False):
        day = cal.week_start_of(s.start_week).isoformat()
        bad = [x for x in sub[sub["sku_id"] == s.sku_id].itertuples(index=False)
               if x.approval_status != "APPROVED" or not _effective_on(x.effective_from, x.effective_to, day)]
        if bad:
            x = bad[0]
            why = (f"approval_status {x.approval_status}" if x.approval_status != "APPROVED"
                   else f"not effective in {s.start_week}")
            rule_c.append((s, x.substitute_id, x.substitute_sku_id, f"via {x.substitute_id} ({why})"))
            continue
        cat = sku.at[s.sku_id, "category"]
        alt = [k for k in stocked.get(s.location_id, []) if k != s.sku_id and (s.sku_id, k) not in pairs
               and sku.at[k, "category"] == cat and sku.at[k, "sku_type"] == "FINISHED"]
        if alt:
            k = alt[int(rng.integers(0, len(alt)))]
            adhoc_c.append((s, None, k, "ad hoc (no sku_substitute rule exists)"))

    def draw(c, m):
        return [c[i] for i in sorted(rng.choice(len(c), size=min(m, len(c)), replace=False).tolist())] if c and m > 0 else []

    chosen = draw(rule_c, n)
    used = {c[0].shortage_id for c in chosen}
    chosen += draw([c for c in adhoc_c if c[0].shortage_id not in used], n - len(chosen))
    props, rows = [], []
    for s, sub_id, sub_sku, why in sorted(chosen, key=lambda c: c[0].shortage_id):
        mid = ids.mitigation_id(s.shortage_id, 91)
        props.append({"mitigation_id": mid, "shortage_id": s.shortage_id, "option_seq": 91,
                      "option_type": "SUBSTITUTE_SKU", "ref_substitute_id": sub_id, "ref_substitute_sku_id": sub_sku,
                      "qty_proposed_ea": int(s.below_ss_qty_ea), "created_by_role": "PLANNER"})
        rows.append(_gt("UNAPPROVED_SUBSTITUTE", "mitigation_option", mid,
                        ";".join(x for x in (sub_id, sub_sku, s.shortage_id) if x),
                        f"MANUAL planner proposal: substitute {sub_sku} for {s.sku_id} at {s.location_id} {why}",
                        "FLAGGED",
                        f"{mid} is VIOLATION and never recommended; {s.shortage_id} keeps a compliant recommendation"))
    cols = ["mitigation_id", "shortage_id", "option_seq", "option_type", "ref_substitute_id", "ref_substitute_sku_id",
            "qty_proposed_ea", "created_by_role"]
    return pd.DataFrame(props, columns=cols), rows
