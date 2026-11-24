"""Checks on planner outputs (Phase 3): PAB reconciliation, shortages vs PAB, cause evidence, planned orders,
capacity ledger, production/BOM and the shortage lifecycle. Every check recomputes from the written tables;
none reads ground truth. The quality gate is re-run (it is deterministic) to obtain firm receipts.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner.plan import balance, quality_gate
from planner.plan.shortages import CRIT_WEIGHT, runs
from planner.schema import LIFECYCLES

from .checks import CheckResult, _blank, _result

EVIDENCE_PK = {
    "forecast_demand": "demand_id", "purchase_order": "po_id", "planned_order": "planned_order_id",
    "supplier_capacity": "capacity_id", "lead_time": "lead_time_id", "inventory": "inventory_id",
    "production_plan": "prod_plan_id", "bom": "bom_id",
}


def _on_hand(t) -> pd.Series:
    inv = t["inventory"]
    return inv[inv["inventory_status"] == "ON_HAND"].set_index(["sku_id", "location_id"])["qty_ea"]


def check_pab_reconciles(t) -> CheckResult:
    pab = t["projected_balance"].sort_values(["sku_id", "location_id", "week"]).reset_index(drop=True)
    bad = balance.reconcile(pab, _on_hand(t))
    return _result("pab_reconciles", "projected_balance", len(pab), bad.to_numpy(), pab,
                   ["sku_id", "location_id", "week"],
                   "closing = opening + firm + planned + production - demand; opening(W01) = ON_HAND; "
                   "opening(w) = closing(w-1); status consistent")


def check_pab_coverage(t, weeks: int) -> CheckResult:
    """One PAB row per SKU-location and week; demand equals forecast for finished SKUs; firm receipts equal open PO qty."""
    pab, sl, fd = t["projected_balance"], t["sku_location"], t["forecast_demand"]
    n_bad = int(len(pab) != len(sl) * weeks)
    m = pab.merge(fd[["sku_id", "location_id", "week", "forecast_qty_ea"]], on=["sku_id", "location_id", "week"], how="inner")
    bad_d = (m["demand_qty"] != m["forecast_qty_ea"]).to_numpy()
    firm_total = int(pab["firm_receipts"].sum())
    gate = quality_gate.run(t, weeks)
    bad_f = np.array([firm_total != int(gate.firm_receipts["qty_ea"].sum())])
    bad = np.concatenate([[n_bad > 0], bad_d, bad_f])
    return _result("pab_inputs_tie_out", "projected_balance", len(bad), bad, None, None,
                   "one row per SKU-location-week; finished demand = forecast; firm receipts = quality-gated open POs")


def check_shortages_match_pab(t, cfg) -> CheckResult:
    pab = t["projected_balance"].sort_values(["sku_id", "location_id", "week"])
    keys = pab[["sku_id", "location_id"]].drop_duplicates().reset_index(drop=True)
    W = cfg.weeks
    closing = pab["closing_qty"].to_numpy().reshape(len(keys), W)
    ss = pab["safety_stock_qty"].to_numpy().reshape(len(keys), W)[:, 0]
    expect = {}
    sku = t["sku"].set_index("sku_id")
    for i, s, e in runs(closing, ss):
        seg = closing[i, s:e + 1]
        k = (keys.at[i, "sku_id"], keys.at[i, "location_id"], cal.week_label(s + 1))
        qty = max(0, -int(seg.min()))
        var = round(qty * float(sku.at[k[0], "unit_price_inr"]), 2)
        expect[k] = (cal.week_label(e + 1), cal.week_label(s + 1 + int(np.argmin(seg))),
                     "STOCKOUT" if seg.min() < 0 else "BELOW_SAFETY_STOCK", qty, int(np.max(ss[i] - seg)), var,
                     round(var * CRIT_WEIGHT[sku.at[k[0], "criticality"]] / math.sqrt(1 + s), 4))
    sh = t["shortage"]
    bad = []
    for r in sh.itertuples(index=False):
        ex = expect.pop((r.sku_id, r.location_id, r.start_week), None)
        got = (r.end_week, r.peak_week, r.shortage_type, int(r.shortage_qty_ea), int(r.below_ss_qty_ea),
               round(float(r.value_at_risk_inr), 2), round(float(r.priority_score), 4))
        bad.append(ex is None or ex[:6] != got[:6] or abs(ex[6] - got[6]) > 1e-3)
    bad += [True] * len(expect)          # runs in the PAB with no shortage row
    order = sh.sort_values(["priority_score", "shortage_id"], ascending=[False, True])["priority_rank"].to_numpy()
    bad.append(not np.array_equal(order, np.arange(1, len(sh) + 1)))
    numbering = sh.sort_values(["start_week", "sku_id", "location_id"])["shortage_id"].tolist() == sorted(sh["shortage_id"])
    bad.append(not numbering)
    return _result("shortages_match_pab", "shortage", len(bad), np.array(bad, dtype=bool), None, None,
                   "each shortage = maximal run with closing < SS; type, qty, below-SS, value at risk, priority "
                   "recomputed from PAB; rank by score; IDs numbered by (start_week, sku, location)")


def check_shortage_causes(t) -> list[CheckResult]:
    sh, sc = t["shortage"], t["shortage_cause"]
    n_causes = sc.groupby("shortage_id").size().reindex(sh["shortage_id"], fill_value=0)
    has_primary = pd.Series([(s, c) for s, c in zip(sh["shortage_id"], sh["primary_cause"])]).isin(
        set(zip(sc["shortage_id"], sc["cause_code"])))
    bad1 = (n_causes.to_numpy() == 0) | ~has_primary.to_numpy() | _blank(sh["explanation"]).to_numpy()
    r1 = _result("every_shortage_explained", "shortage", len(sh), bad1, sh, ["shortage_id"],
                 ">= 1 shortage_cause row; primary_cause among them; explanation present")
    ok = np.zeros(len(sc), dtype=bool)
    for et, pk in EVIDENCE_PK.items():
        m = (sc["evidence_type"] == et).to_numpy()
        if m.any() and et in t:
            ok[m] = sc.loc[m, "evidence_id"].isin(set(t[et][pk])).to_numpy()
    r2 = _result("cause_evidence_resolves", "shortage_cause", len(sc), ~ok, sc, ["shortage_id", "evidence_id"],
                 "evidence_id resolves to a row of evidence_type")
    # explanation names every evidence id of its main clauses: at least the shortage's sku, location and a week
    sk = sh.apply(lambda r: r["sku_id"] in r["explanation"] and r["location_id"] in r["explanation"]
                  and r["start_week"] in r["explanation"], axis=1)
    r3 = _result("explanation_names_ids", "shortage", len(sh), ~sk.to_numpy(), sh, ["shortage_id"],
                 "explanation names the SKU, location and start week")
    return [r1, r2, r3]


def _lane_lookup(t):
    lt = t["lead_time"]
    std = lt[~lt["is_expedite_lane"]]
    return {k: g.to_dict("records") for k, g in std.groupby(["supplier_id", "dest_region"])}


def check_planned_orders(t, cfg) -> CheckResult:
    po = t["planned_order"]
    if len(po) == 0:
        return CheckResult("planned_order_rules", "planned_order", 0, 0, "no planned orders")
    ss = t["supplier_sku"].set_index("supplier_sku_id")
    lt = t["lead_time"].set_index("lead_time_id")
    region = t["location"].set_index("location_id")["region"]
    rel = po["release_week"].map(cal.week_num)
    rec = po["receipt_week"].map(cal.week_num)
    L = po["lead_time_id"].map(lt["total_lead_weeks"])
    s = po["supplier_sku_id"]
    conv = s.map(ss["uom_conversion"])
    moq, mult = s.map(ss["moq"]), s.map(ss["order_multiple"])
    start = rel.map(lambda n: cal.week_start(int(n)).isoformat())
    eff_ok = [(r["effective_from"] <= d) and (r["effective_to"] is None or isinstance(r["effective_to"], float)
                                                or r["effective_to"] >= d)
              for r, d in zip((lt.loc[x] for x in po["lead_time_id"]), start)]
    lane_ok = ((po["lead_time_id"].map(lt["supplier_id"]) == po["supplier_id"])
               & (po["lead_time_id"].map(lt["dest_region"]) == po["location_id"].map(region))
               & ~po["lead_time_id"].map(lt["is_expedite_lane"]).astype(bool))
    bad = ((rel < 1) | (rec != rel + L) | ~np.array(eff_ok) | ~lane_ok
           | (po["order_qty"] < moq) | (po["order_qty"] % mult != 0)
           | (po["qty_base_ea"] != po["order_qty"] * conv) | (po["order_uom"] != s.map(ss["purchase_uom"]))
           | (po["capacity_cut_ea"] != po["requested_qty_ea"] - po["qty_base_ea"])
           | ~s.map(ss["is_primary"]).astype(bool) | (s.map(ss["approval_status"]) != "APPROVED")
           | (s.map(ss["supplier_id"]) != po["supplier_id"]) | (s.map(ss["sku_id"]) != po["sku_id"]))
    return _result("planned_order_rules", "planned_order", len(po), bad.to_numpy(), po, ["planned_order_id"],
                   "release >= W01 (lead-time fence); receipt = release + lead time of the standard lane effective at "
                   "release; qty >= MOQ and multiple of order_multiple; qty_base = qty x conversion; primary APPROVED source")


def check_capacity_ledger(t, cfg) -> CheckResult:
    """Firm (quality-gated) + planned receipts per supplier-week never exceed supplier capacity."""
    W = cfg.weeks
    gate = quality_gate.run(t, W)
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])["capacity_qty_ea"]
    fr = gate.firm_receipts.assign(week=gate.firm_receipts["w"].map(cal.week_label))
    load = fr.groupby(["supplier_id", "week"])["qty_ea"].sum()
    pl = t["planned_order"].groupby(["supplier_id", "receipt_week"])["qty_base_ea"].sum()
    pl.index.names = ["supplier_id", "week"]
    tot = load.add(pl, fill_value=0)
    c = cap.reindex(tot.index)
    bad = (c.isna() | (tot > c)).to_numpy()
    return _result("capacity_ledger_non_negative", "supplier_capacity", len(tot), bad, None, None,
                   "capacity - firm receipts - planned receipts >= 0 for every supplier-week")


def check_production_and_components(t) -> list[CheckResult]:
    pp, cap = t["production_plan"], t["production_capacity"].set_index(["plant_location_id", "week"])["capacity_qty_ea"]
    load = pp.groupby(["plant_location_id", "week"])["capacity_used_ea"].sum()
    over = (load > cap.reindex(load.index)).to_numpy()
    rows = ((pp["requested_qty_ea"] - pp["capacity_cut_ea"] - pp["component_cut_ea"] != pp["planned_qty_ea"])
            | (pp["capacity_used_ea"] != pp["planned_qty_ea"])
            | ((pp["status"] == "FIRM") != (pp["week"].map(cal.week_num) <= 4))).to_numpy()
    r1 = _result("production_reconciles_and_fits", "production_plan", len(rows) + len(over),
                 np.concatenate([rows, over]), None, None,
                 "requested - capacity cut - component cut = planned = capacity used; plant load <= capacity; FIRM only in firm weeks")
    pab = t["projected_balance"]
    sku = t["sku"].set_index("sku_id")
    prod = pp.groupby(["sku_id", "plant_location_id", "week"])["planned_qty_ea"].sum()
    prod.index.names = ["sku_id", "location_id", "week"]
    m = pab.set_index(["sku_id", "location_id", "week"])
    make = m.index.get_level_values("sku_id").map(sku["sourcing_type"]) == "MAKE"
    got = m.loc[make, "production_receipts"]
    bad_prod = (got != prod.reindex(got.index, fill_value=0)).to_numpy()
    # dependent demand = production x qty_per; component balance never negative
    bom = t["bom"]
    dep = pp.merge(bom[["parent_sku_id", "component_sku_id", "qty_per"]], left_on="sku_id", right_on="parent_sku_id")
    dep["d"] = dep["planned_qty_ea"] * dep["qty_per"]
    dep = dep.groupby(["component_sku_id", "plant_location_id", "week"])["d"].sum()
    dep.index.names = ["sku_id", "location_id", "week"]
    comp = m.index.get_level_values("sku_id").map(sku["sku_type"]) == "COMPONENT"
    cd = m.loc[comp, "demand_qty"]
    bad_dep = (cd != dep.reindex(cd.index, fill_value=0)).to_numpy()
    bad_neg = (m.loc[comp, "closing_qty"] < 0).to_numpy()
    r2 = _result("bom_dependent_demand", "projected_balance", len(bad_prod) + len(bad_dep),
                 np.concatenate([bad_prod, bad_dep, bad_neg]), None, None,
                 "MAKE production receipts = production plan; component demand = production x qty_per; "
                 "component balance never negative (parents cut instead)")
    return [r1, r2]


def check_shortage_lifecycle(t) -> CheckResult:
    sh, ev = t["shortage"], t["shortage_event"].sort_values(["shortage_id", "seq"])
    lc = LIFECYCLES["shortage"]
    frm = ev["from_status"].where(~_blank(ev["from_status"]), None)
    prev_to = ev.groupby("shortage_id")["to_status"].shift(1)
    prev_ts = ev.groupby("shortage_id")["event_ts"].shift(1)
    trans = np.array([lc.allowed(f if isinstance(f, str) else None, s) for f, s in zip(frm, ev["to_status"])])
    chain = (prev_to.isna() & frm.isna()) | (prev_to == frm)
    ts = prev_ts.isna() | (ev["event_ts"] > prev_ts)
    last = ev.groupby("shortage_id")["to_status"].last().reindex(sh["shortage_id"])
    bad = np.concatenate([~(trans & chain & ts).to_numpy(), (last.to_numpy() != sh["status"].to_numpy())])
    return _result("shortage_lifecycle", "shortage_event", len(bad), bad, None, None,
                   "allowed transitions, chained from_status, increasing timestamps; status = last event")


def run_plan_checks(t, cfg) -> list[CheckResult]:
    out = [check_pab_reconciles(t), check_pab_coverage(t, cfg.weeks), check_shortages_match_pab(t, cfg),
           check_planned_orders(t, cfg), check_capacity_ledger(t, cfg), check_shortage_lifecycle(t)]
    out += check_shortage_causes(t)
    out += check_production_and_components(t)
    return out
