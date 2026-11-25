"""Phase 4: mitigation options, policy, capacity ledger, scoring and decisions (test scale)."""
import numpy as np
import pandas as pd
import pytest

from planner import plan
from planner.plan import mitigations
from planner.validate import plan_checks


@pytest.fixture(scope="session")
def mit_planned(planned):
    return planned


def _copy(t):
    return {k: v.copy() for k, v in t.items()}


def test_all_mitigation_checks_pass(mit_planned, test_cfg):
    _, t = mit_planned
    bad = [r.as_dict() for r in plan_checks.run_mitigation_checks(t, test_cfg) if r.failures]
    assert bad == []


def test_every_shortage_has_options_and_one_recommendation(mit_planned):
    _, t = mit_planned
    mo, sh = t["mitigation_option"], t["shortage"]
    assert set(mo["shortage_id"]) == set(sh["shortage_id"])
    rec = mo[mo["is_recommended"]]
    assert rec["shortage_id"].is_unique and len(rec) == len(sh)
    # BACKORDER is always offered
    assert set(mo.loc[mo["option_type"] == "BACKORDER", "shortage_id"]) == set(sh["shortage_id"])


def test_no_recommended_violation(mit_planned):
    _, t = mit_planned
    mo = t["mitigation_option"]
    assert not (mo["is_recommended"] & (mo["policy_status"] == "VIOLATION")).any()
    v = mo[mo["policy_status"] == "VIOLATION"]
    assert v["policy_violation_reason"].notna().all()


def test_capacity_ledger_never_negative_after_mitigation(mit_planned, test_cfg):
    res, t = mit_planned
    L = res.mit.ledger
    assert (L.sup_rem >= 0).all() and (L.plant_rem >= 0).all()
    for r in plan_checks.check_mitigation_ledger(t, test_cfg):
        assert r.failures == 0, r.as_dict()


def test_mix_uses_several_levers_and_mostly_avoids_backorder(mit_planned):
    _, t = mit_planned
    rec = t["mitigation_option"].query("is_recommended")
    assert rec["option_type"].nunique() >= 3
    assert (rec["option_type"] != "BACKORDER").mean() >= 0.5


def test_alternatives_show_cost_vs_service(mit_planned):
    """Shortages with several options carry different cost / coverage per option."""
    _, t = mit_planned
    mo = t["mitigation_option"]
    multi = mo.groupby("shortage_id").filter(lambda g: len(g) >= 3)
    assert multi["shortage_id"].nunique() > 0
    spread = multi.groupby("shortage_id").agg(c=("incremental_cost_inr", "nunique"), v=("coverage_pct", "nunique"))
    assert ((spread["c"] > 1) | (spread["v"] > 1)).all()
    assert mo[["incremental_cost_inr", "coverage_pct", "fill_rate_after", "score"]].notna().all().all()


def test_fill_rate_does_not_drop(mit_planned):
    res, _ = mit_planned
    assert res.mit.fill_after >= res.mit.fill_before


def test_decision_check_catches_violation_recommended(mit_planned, test_cfg):
    _, t0 = mit_planned
    t = _copy(t0)
    mo = t["mitigation_option"]
    v = mo.index[mo["policy_status"] == "VIOLATION"]
    if len(v) == 0:   # make one: a non-recommended option turned into a recommended violation
        v = mo.index[~mo["is_recommended"]][:1]
        mo.loc[v, ["policy_status", "policy_violation_reason"]] = ["VIOLATION", "test"]
    mo.loc[v[:1], "is_recommended"] = True
    assert plan_checks.check_mitigation_options(t, test_cfg).failures > 0


def test_ledger_check_catches_overbooking(mit_planned, test_cfg):
    _, t0 = mit_planned
    t = _copy(t0)
    led = t["mitigation_ledger"]
    sup = led.index[led["resource_type"] == "SUPPLIER_CAPACITY"]
    if len(sup):
        led.loc[sup[:1], "qty_ea"] += 10 ** 9
    else:
        st = led.index[(led["resource_type"] == "STOCK") & (led["qty_ea"] < 0)]
        led.loc[st[:1], "qty_ea"] -= 10 ** 9
    assert plan_checks.check_mitigation_ledger(t, test_cfg)[0].failures > 0


def test_manual_unapproved_substitute_flagged(baseline, planned, test_cfg):
    """A MANUAL planner proposal with a PENDING/REJECTED substitute is a VIOLATION, never recommended, and raises
    an UNAPPROVED_SUBSTITUTE exception (Phase 5 injects these from ground-truth-labelled proposals)."""
    _, t_in = baseline
    shs = plan.plan(_copy(t_in), test_cfg).outputs["shortage"].sort_values("shortage_id")
    sub = t_in["sku_substitute"]
    bad = sub[sub["approval_status"] != "APPROVED"]
    hit = shs[shs["sku_id"].isin(bad["sku_id"])]
    sh = (hit if len(hit) else shs).iloc[0]
    rules = bad[bad["sku_id"] == sh["sku_id"]]
    bad_sub = (rules if len(rules) else bad).iloc[0]
    prop = pd.DataFrame([{"mitigation_id": f"MIT-{sh['shortage_id']}-91", "shortage_id": sh["shortage_id"],
                          "option_seq": 91, "option_type": "SUBSTITUTE_SKU", "ref_substitute_id": bad_sub["substitute_id"],
                          "ref_substitute_sku_id": bad_sub["substitute_sku_id"],
                          "qty_proposed_ea": int(sh["below_ss_qty_ea"]), "created_by_role": "PLANNER"}])
    tables = _copy(t_in)
    tables["mitigation_proposal"] = prop
    res = plan.plan(tables, test_cfg)
    mo = res.outputs["mitigation_option"].set_index("mitigation_id")
    m = mo.loc[prop.at[0, "mitigation_id"]]
    assert m["source"] == "MANUAL" and m["policy_status"] == "VIOLATION" and not m["is_recommended"]
    exc = res.outputs["exception"]
    hit = exc[exc["issue_type"] == "UNAPPROVED_SUBSTITUTE"]
    assert list(hit["record_id"]) == [prop.at[0, "mitigation_id"]]
    assert hit.iloc[0]["action_taken"] == "FLAGGED"


def test_draw_respects_safety_stock(mit_planned, test_cfg):
    res, _ = mit_planned
    ctx = mitigations.Context(res.tables, res.gate, res.mrp, test_cfg)
    L = mitigations.Ledger(res.mrp.supplier_remaining.copy(), res.mrp.plant_remaining.copy(), res.mrp.closing.copy())
    g = res.mrp.grid
    for j in np.flatnonzero(g.is_buy)[:50]:
        q, bf = mitigations.draw(ctx, L, int(j), 0, 10 ** 9)
        hi = bf[1] if bf else g.W
        if q > 0:
            assert (L.bal[j, 0:hi] - q >= g.ss[j]).all()


# --- minimum coverage rule (policy.min_coverage_pct) -------------------------------------------------------------

def test_min_coverage_rule_holds(mit_planned, test_cfg):
    """No recommended non-backorder option below the minimum coverage; MITIGATION_SELECTED never below it; a
    BACKORDER is recommended only when no COMPLIANT non-backorder option reaches it, and its final status follows
    value at risk vs the approval threshold."""
    _, t = mit_planned
    m = float(test_cfg["policy"]["min_coverage_pct"])
    thr = float(test_cfg["policy"]["approval_cost_threshold_inr"])
    assert m == 0.25
    mo, sh = t["mitigation_option"], t["shortage"].set_index("shortage_id")
    rec = mo[mo["is_recommended"]].set_index("shortage_id")
    non_bo = rec[rec["option_type"] != "BACKORDER"]
    assert (non_bo["coverage_pct"] >= m).all()
    selected = sh.index[sh["status"] == "MITIGATION_SELECTED"]
    assert (rec.loc[selected, "coverage_pct"] >= m).all()
    reach = set(mo[(mo["policy_status"] == "COMPLIANT") & (mo["option_type"] != "BACKORDER")
                   & (mo["coverage_pct"] >= m)]["shortage_id"])
    bo = rec[rec["option_type"] == "BACKORDER"]
    assert not (set(bo.index) & reach)
    want = np.where(sh.loc[bo.index, "value_at_risk_inr"] > thr, "ESCALATED", "BACKORDER_ACCEPTED")
    assert (sh.loc[bo.index, "status"].to_numpy() == want).all()
    assert plan_checks.check_min_coverage(t, test_cfg).failures == 0


def test_recommend_skips_option_below_min_coverage(test_cfg):
    """Unit test of decide.recommend: a COMPLIANT option covering 20% loses to BACKORDER; at 25% it wins."""
    from types import SimpleNamespace
    from planner.plan import decide
    ctx = SimpleNamespace(cfg=test_cfg, policy=test_cfg["policy"])
    sh = SimpleNamespace(below=100, base_value=1e6)

    def opts(covered):
        return [{"option_type": "TRANSFER", "policy_status": "COMPLIANT", "qty_covered_ea": covered, "weeks_late": 0,
                 "incremental_cost_inr": 10.0, "option_seq": 1},
                {"option_type": "BACKORDER", "policy_status": "COMPLIANT", "qty_covered_ea": 0, "weeks_late": 3,
                 "incremental_cost_inr": 0.0, "option_seq": 2}]
    o = opts(20)
    assert o[decide.recommend(ctx, sh, o)]["option_type"] == "BACKORDER"
    o = opts(25)
    assert o[decide.recommend(ctx, sh, o)]["option_type"] == "TRANSFER"
    thr = float(test_cfg["policy"]["approval_cost_threshold_inr"])
    assert decide.final_status("BACKORDER", False, thr + 1, thr)[0] == "ESCALATED"
    assert decide.final_status("BACKORDER", True, thr, thr)[0] == "BACKORDER_ACCEPTED"
    assert decide.final_status("TRANSFER", False, thr + 1, thr)[0] == "MITIGATION_SELECTED"


def test_min_coverage_check_catches_low_coverage_selection(mit_planned, test_cfg):
    """Mutation: select a low-coverage option for one shortage -> the check fails."""
    _, t = mit_planned
    t2 = _copy(t)
    mo = t2["mitigation_option"]
    m = float(test_cfg["policy"]["min_coverage_pct"])
    low = mo[(mo["option_type"] != "BACKORDER") & (mo["coverage_pct"] < m) & ~mo["is_recommended"]]
    if len(low) == 0:                                    # fabricate one from any non-backorder option
        low = mo[mo["option_type"] != "BACKORDER"].head(1)
        mo.loc[low.index, "coverage_pct"] = 0.1
    k = low.index[0]
    sid = mo.at[k, "shortage_id"]
    mo.loc[mo["shortage_id"] == sid, "is_recommended"] = False
    mo.at[k, "is_recommended"] = True
    t2["shortage"].loc[t2["shortage"]["shortage_id"] == sid, "status"] = "MITIGATION_SELECTED"
    assert plan_checks.check_min_coverage(t2, test_cfg).failures >= 1
