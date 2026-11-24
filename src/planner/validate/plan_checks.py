"""Checks on planner outputs (Phases 3-4): PAB reconciliation, shortages vs PAB, cause evidence, planned orders,
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
    if "mitigation_option" in t:
        out += run_mitigation_checks(t, cfg)
    return out


# --------------------------------------------------------------------------------------------
# Phase 4: mitigation options, policy, capacity ledger, decisions
# --------------------------------------------------------------------------------------------

def _eff(ef, et, day) -> bool:
    return str(ef) <= day and (et is None or (isinstance(et, float) and et != et) or str(et) >= day)


def _base_value(sh: pd.DataFrame, sku: pd.DataFrame) -> pd.Series:
    price = sh["sku_id"].map(sku["unit_price_inr"]).astype(float)
    return sh["value_at_risk_inr"].where(sh["value_at_risk_inr"] > 0, sh["below_ss_qty_ea"] * price)


def check_mitigation_options(t, cfg) -> CheckResult:
    mo, sh = t["mitigation_option"], t["shortage"].set_index("shortage_id")
    sku = t["sku"].set_index("sku_id")
    if len(mo) == 0:
        return CheckResult("mitigation_option_rules", "mitigation_option", 0, int(len(sh) > 0), "no options")
    s = mo["shortage_id"]
    below = s.map(sh["below_ss_qty_ea"])
    start = s.map(sh["start_week"]).map(cal.week_num)
    end = s.map(sh["end_week"]).map(cal.week_num)
    arr = mo["arrival_week"].map(lambda w: cal.week_num(w) if isinstance(w, str) else np.nan)
    base = s.map(_base_value(sh.reset_index(), sku).set_axis(sh.index))
    pol = cfg["policy"]
    appr = (mo["incremental_cost_inr"] > float(pol["approval_cost_threshold_inr"])) | (
        mo["incremental_cost_inr"] > float(pol["approval_var_share"]) * base)
    late = np.where(mo["option_type"] == "BACKORDER", end - start + 1, np.maximum(0, arr - start))
    rec = mo["is_recommended"].astype(bool)
    reason = ~_blank(mo["policy_violation_reason"])
    t_ = mo["option_type"]
    refs_ok = (((t_ != "EXPEDITE_PO") | ~_blank(mo["ref_po_id"]))
               & ((t_ != "ALTERNATE_SUPPLIER") | ~_blank(mo["ref_supplier_id"]))
               & ((t_ != "TRANSFER") | (~_blank(mo["ref_from_location_id"])
                                         & (mo["ref_from_location_id"] != s.map(sh["location_id"]))))
               & ((t_ != "SUBSTITUTE_SKU") | (~_blank(mo["ref_substitute_id"]) & ~_blank(mo["ref_substitute_sku_id"])))
               & ((t_ != "INCREASE_PRODUCTION") | ~_blank(mo["ref_plant_location_id"])))
    bad = ((mo["mitigation_id"] != "MIT-" + s + "-" + mo["option_seq"].map(lambda k: f"{int(k):02d}"))
           | (mo["qty_covered_ea"] > below) | ((mo["coverage_pct"] - mo["qty_covered_ea"] / below).abs() > 1e-4)
           | (mo["weeks_late"] != late) | (mo["requires_approval"].astype(bool) != appr)
           | ((mo["policy_status"] == "VIOLATION") != reason) | (rec & (mo["policy_status"] != "COMPLIANT"))
           | ((mo["source"] == "SYSTEM") != (mo["created_by_role"] == "SYSTEM")) | ~refs_ok
           | ((t_ == "BACKORDER") & (mo["qty_covered_ea"] != 0)) | (arr > end)).to_numpy()
    # exactly one recommendation per shortage, and it is the best compliant non-backorder option (or backorder)
    per = []
    cand = mo[(mo["policy_status"] == "COMPLIANT") & (t_ != "BACKORDER") & (mo["qty_covered_ea"] > 0)]
    best = cand.groupby("shortage_id")["score"].max()
    for sid in sh.index:
        r = mo[(s == sid) & rec]
        if len(r) != 1:
            per.append(True)
            continue
        r = r.iloc[0]
        per.append((r["option_type"] == "BACKORDER") == (sid in best.index)
                   or (sid in best.index and r["score"] < best[sid] - 1e-9))
    return _result("mitigation_option_rules", "mitigation_option", len(mo) + len(sh),
                   np.concatenate([bad, np.array(per, dtype=bool)]), None, None,
                   "ID = MIT-<shortage>-<seq>; covered <= below-SS requirement; coverage = covered / requirement; "
                   "weeks_late; approval rule; VIOLATION <=> reason; required refs; exactly one recommendation per "
                   "shortage = best-scoring COMPLIANT non-backorder option, else BACKORDER; never a VIOLATION")


def check_mitigation_policy(t) -> CheckResult:
    """Recompute the policy verdict of every COMPLIANT option from the master data."""
    mo, sh = t["mitigation_option"], t["shortage"].set_index("shortage_id")
    po = t["purchase_order"].set_index("po_id")
    lt = t["lead_time"].set_index("lead_time_id")
    region = t["location"].set_index("location_id")["region"]
    sup_status = t["supplier"].set_index("supplier_id")["status"]
    sst = t["supplier_sku"]
    sub = t["sku_substitute"].set_index("substitute_id")
    exc = t["exception"]
    flagged = set(exc.loc[exc["issue_type"] == "UNAPPROVED_SUBSTITUTE", "record_id"])
    bad = []
    for r in mo.itertuples(index=False):
        s = sh.loc[r.shortage_id]
        day = cal.week_start_of(s["start_week"]).isoformat()
        ok = True
        if r.option_type == "SUBSTITUTE_SKU":
            x = sub.loc[r.ref_substitute_id] if r.ref_substitute_id in sub.index else None
            usable = (x is not None and x["approval_status"] == "APPROVED" and x["sku_id"] == s["sku_id"]
                      and _eff(x["effective_from"], x["effective_to"], day)
                      and x["substitute_sku_id"] == r.ref_substitute_sku_id)
            ok = usable == (r.policy_status == "COMPLIANT")
            ok &= usable or r.mitigation_id in flagged           # every unusable substitute raised an exception
        elif r.policy_status != "COMPLIANT":
            pass
        elif r.option_type == "EXPEDITE_PO":
            p, ln = po.loc[r.ref_po_id], lt.loc[r.ref_lead_time_id]
            ok = (p["status"] in ("CREATED", "CONFIRMED") and p["sku_id"] == s["sku_id"]
                  and p["ship_to_location_id"] == s["location_id"] and bool(ln["is_expedite_lane"])
                  and ln["transport_mode"] == "AIR" and ln["supplier_id"] == p["supplier_id"]
                  and ln["dest_region"] == region[s["location_id"]]
                  and cal.week_num(r.arrival_week) <= cal.week_num(s["start_week"]))
        elif r.option_type == "ALTERNATE_SUPPLIER":
            ln = lt.loc[r.ref_lead_time_id]
            rel = cal.week_num(r.arrival_week) - int(ln["total_lead_weeks"])
            lines = sst[(sst["supplier_id"] == r.ref_supplier_id) & (sst["sku_id"] == s["sku_id"])
                        & ~sst["is_primary"].astype(bool) & (sst["approval_status"] == "APPROVED")]
            rd = cal.week_start(max(rel, 1)).isoformat()
            lines = lines[[_eff(a, b, rd) for a, b in zip(lines["effective_from"], lines["effective_to"])]]
            q_ok = any(r.action_qty_ea % (int(x.uom_conversion) * int(x.order_multiple)) == 0
                       and r.action_qty_ea >= int(x.moq) * int(x.uom_conversion) for x in lines.itertuples())
            ok = (rel >= 1 and len(lines) > 0 and q_ok and sup_status.get(r.ref_supplier_id) == "ACTIVE"
                  and not bool(ln["is_expedite_lane"]) and ln["supplier_id"] == r.ref_supplier_id
                  and ln["dest_region"] == region[s["location_id"]] and _eff(ln["effective_from"], ln["effective_to"], rd)
                  and cal.week_num(r.arrival_week) <= cal.week_num(s["end_week"]))
        bad.append(not ok)
    return _result("mitigation_policy", "mitigation_option", len(mo), np.array(bad, dtype=bool), mo,
                   ["mitigation_id"], "EXPEDITE: unshipped PO, AIR lane, arrives by start; ALTERNATE: APPROVED + "
                   "effective line, ACTIVE supplier, release >= W01, MOQ/multiple, receipt <= end; SUBSTITUTE: "
                   "COMPLIANT <=> APPROVED + effective, otherwise an UNAPPROVED_SUBSTITUTE exception")


def check_mitigation_ledger(t, cfg) -> list[CheckResult]:
    """Capacity ledger never negative after the recommended mitigations, recomputed from the written tables."""
    W = cfg.weeks
    led, mo = t["mitigation_ledger"], t["mitigation_option"]
    rec_ids = set(mo.loc[mo["is_recommended"].astype(bool), "mitigation_id"])
    rt = led["resource_type"]
    shape_bad = (~led["mitigation_id"].isin(rec_ids)
                 | ((rt == "SUPPLIER_CAPACITY") == _blank(led["supplier_id"]))
                 | ((rt == "PLANT_CAPACITY") == _blank(led["plant_location_id"]))
                 | ((rt == "STOCK") == (_blank(led["sku_id"]) | _blank(led["location_id"])))).to_numpy()
    # supplier capacity: firm (quality-gated) + planned + mitigation entries <= capacity
    gate = quality_gate.run(t, W)
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])["capacity_qty_ea"]
    fr = gate.firm_receipts.assign(week=gate.firm_receipts["w"].map(cal.week_label))
    load = fr.groupby(["supplier_id", "week"])["qty_ea"].sum()
    pl = t["planned_order"].groupby(["supplier_id", "receipt_week"])["qty_base_ea"].sum()
    pl.index.names = ["supplier_id", "week"]
    sm = led[rt == "SUPPLIER_CAPACITY"].groupby(["supplier_id", "week"])["qty_ea"].sum()
    tot = load.add(pl, fill_value=0).add(sm, fill_value=0)
    sup_bad = (cap.reindex(tot.index).isna() | (tot > cap.reindex(tot.index))).to_numpy()
    # plant capacity
    pcap = t["production_capacity"].set_index(["plant_location_id", "week"])["capacity_qty_ea"]
    pload = t["production_plan"].groupby(["plant_location_id", "week"])["capacity_used_ea"].sum()
    pm = led[rt == "PLANT_CAPACITY"].groupby(["plant_location_id", "week"])["qty_ea"].sum()
    ptot = pload.add(pm, fill_value=0)
    plant_bad = (pcap.reindex(ptot.index).isna() | (ptot > pcap.reindex(ptot.index))).to_numpy()
    # stock: wherever a SKU-location has given stock away (cumulative entries < 0) it stays >= safety stock
    pab = t["projected_balance"].sort_values(["sku_id", "location_id", "week"])
    keys = pab[["sku_id", "location_id"]].drop_duplicates().reset_index(drop=True)
    kidx = {k: i for i, k in enumerate(zip(keys["sku_id"], keys["location_id"]))}
    closing = pab["closing_qty"].to_numpy().reshape(len(keys), W)
    ss = pab["safety_stock_qty"].to_numpy().reshape(len(keys), W)[:, :1]
    flow = np.zeros_like(closing)
    st = led[rt == "STOCK"]
    for r in st.itertuples(index=False):
        flow[kidx[(r.sku_id, r.location_id)], cal.week_num(r.week) - 1] += r.qty_ea
    net = np.cumsum(flow, axis=1)
    stock_bad = ((net < 0) & (closing + net < ss))[np.abs(net).sum(axis=1) > 0].ravel()
    r1 = _result("mitigation_capacity_ledger", "mitigation_ledger",
                 len(shape_bad) + len(sup_bad) + len(plant_bad) + len(stock_bad),
                 np.concatenate([shape_bad, sup_bad, plant_bad, stock_bad]), None, None,
                 "entries only for recommended options; firm + planned + mitigation load <= supplier capacity; "
                 "production + overtime <= plant capacity; a SKU-location that gives stock stays >= safety stock")
    # coverage of each recommended option recomputed from PAB + its own stock entries
    sh = t["shortage"].set_index("shortage_id")
    rec = mo[mo["is_recommended"].astype(bool) & (mo["option_type"] != "BACKORDER")]
    cov_bad = []
    for r in rec.itertuples(index=False):
        s = sh.loc[r.shortage_id]
        i = kidx[(s["sku_id"], s["location_id"])]
        a, b = cal.week_num(s["start_week"]) - 1, cal.week_num(s["end_week"])
        f = np.zeros(W, dtype=np.int64)
        own = st[(st["mitigation_id"] == r.mitigation_id) & (st["sku_id"] == s["sku_id"])
                 & (st["location_id"] == s["location_id"])]
        for e in own.itertuples(index=False):
            f[cal.week_num(e.week) - 1] += e.qty_ea
        seg = closing[i, a:b] + np.cumsum(f)[a:b]
        gap = max(0, int((ss[i, 0] - seg).max()))
        cov_bad.append(int(s["below_ss_qty_ea"]) - gap != int(r.qty_covered_ea))
    r2 = _result("mitigation_coverage_recomputes", "mitigation_option", len(cov_bad), np.array(cov_bad, dtype=bool),
                 rec, ["mitigation_id"], "qty_covered = below-SS gap closed by the option's own ledger entries")
    return [r1, r2]


def check_decisions(t) -> CheckResult:
    """Final shortage status follows the recommendation; the final event names the recommended option."""
    sh, mo, ev = t["shortage"], t["mitigation_option"], t["shortage_event"]
    rec = mo[mo["is_recommended"].astype(bool)].set_index("shortage_id")
    last = ev.sort_values(["shortage_id", "seq"]).groupby("shortage_id").last()
    bad = []
    for r in sh.itertuples(index=False):
        if r.shortage_id not in rec.index or r.shortage_id not in last.index:
            bad.append(True)
            continue
        o, e = rec.loc[r.shortage_id], last.loc[r.shortage_id]
        want = ("BACKORDER_ACCEPTED" if o["option_type"] == "BACKORDER" else
                "ESCALATED" if bool(o["requires_approval"]) else "MITIGATION_SELECTED")
        bad.append(not (r.status == want == e["to_status"] and e["mitigation_id"] == o["mitigation_id"]))
    return _result("decision_matches_recommendation", "shortage", len(sh), np.array(bad, dtype=bool), sh,
                   ["shortage_id"], "BACKORDER -> BACKORDER_ACCEPTED; approval needed -> ESCALATED; else "
                   "MITIGATION_SELECTED; final event carries the recommended mitigation_id")


def run_mitigation_checks(t, cfg) -> list[CheckResult]:
    return [check_mitigation_options(t, cfg), check_mitigation_policy(t), *check_mitigation_ledger(t, cfg),
            check_decisions(t)]
