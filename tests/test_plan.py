import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from planner import calendar as cal
from planner import plan, schema, tableio
from planner.plan import balance, quality_gate, replenish
from planner.validate import checks, plan_checks

SRC = Path(__file__).resolve().parents[1] / "src" / "planner"


@pytest.fixture(scope="session")
def planned(generated, test_cfg):
    """Plan the generated test scale and read every table (inputs + outputs) back from CSV."""
    out, _ = generated
    res = plan.run(test_cfg, out)
    tables = tableio.read_all(out, tableio.fmt_of(test_cfg))
    return res, tables


def _copy(t):
    return {k: v.copy() for k, v in t.items()}


# --- PAB ------------------------------------------------------------------------------------

def test_pab_reconciles_week_by_week(planned):
    _, t = planned
    r = plan_checks.check_pab_reconciles(t)
    assert r.rows_checked == len(t["sku_location"]) * 52
    assert r.failures == 0, r.as_dict()


def test_pab_independent_recompute(planned):
    """Recompute closing from opening ON_HAND and the flow columns with a plain cumsum."""
    _, t = planned
    pab = t["projected_balance"].sort_values(["sku_id", "location_id", "week"])
    oh = t["inventory"].query("inventory_status == 'ON_HAND'").set_index(["sku_id", "location_id"])["qty_ea"]
    net = pab["firm_receipts"] + pab["planned_receipts"] + pab["production_receipts"] - pab["demand_qty"]
    start = oh.reindex(pd.MultiIndex.from_frame(pab[["sku_id", "location_id"]])).to_numpy()
    closing = start + net.groupby([pab["sku_id"], pab["location_id"]]).cumsum().to_numpy()
    assert np.array_equal(closing, pab["closing_qty"].to_numpy())


def test_pab_reconcile_detects_corruption(planned):
    _, t = planned
    pab = t["projected_balance"].sort_values(["sku_id", "location_id", "week"]).reset_index(drop=True)
    pab.loc[10, "closing_qty"] += 1
    oh = t["inventory"].query("inventory_status == 'ON_HAND'").set_index(["sku_id", "location_id"])["qty_ea"]
    assert balance.reconcile(pab, oh).sum() >= 2   # the row itself and the next week's opening


def test_all_plan_checks_pass(planned, test_cfg):
    _, t = planned
    bad = [r.as_dict() for r in plan_checks.run_plan_checks(t, test_cfg) if r.failures]
    assert bad == []


def test_outputs_pass_schema_checks(planned):
    _, t = planned
    outs = {n: t[n] for n in plan.OUTPUT_TABLES}
    bad = [r.as_dict() for r in checks.run_generic({**t, **outs}) if r.failures]
    assert bad == []


# --- shortages ------------------------------------------------------------------------------

def test_shortage_rate_in_target_band(planned):
    res, _ = planned
    pct = plan.summary(res)["pct_sku_locations_short"]
    assert 5.0 <= pct <= 12.0, pct


def test_every_shortage_has_resolvable_cause(planned):
    _, t = planned
    sh, sc = t["shortage"], t["shortage_cause"]
    assert len(sh) > 0
    assert set(sh["shortage_id"]) == set(sc["shortage_id"])
    for r in plan_checks.check_shortage_causes(t):
        assert r.failures == 0, r.as_dict()


def test_explanations_name_quantities_weeks_and_evidence(planned):
    _, t = planned
    sh, sc = t["shortage"], t["shortage_cause"]
    main = sc[sc["evidence_type"] != "bom"]
    for r in sh.itertuples(index=False):
        assert re.search(r"\d+ EA", r.explanation) and r.start_week in r.explanation
        ev = main[main["shortage_id"] == r.shortage_id]
        top = ev.sort_values("contribution_qty_ea", ascending=False).groupby("cause_code").head(1)
        assert any(e in r.explanation for e in top["evidence_id"]), r.explanation


def test_shortage_types_and_quantities(planned):
    _, t = planned
    sh = t["shortage"]
    so = sh["shortage_type"] == "STOCKOUT"
    assert (sh.loc[so, "shortage_qty_ea"] > 0).all()
    assert (sh.loc[~so, "shortage_qty_ea"] == 0).all()
    assert (sh["below_ss_qty_ea"] > 0).all()
    assert sorted(sh["priority_rank"]) == list(range(1, len(sh) + 1))


def test_lead_time_fence_cause_only_inside_fence(planned):
    res, t = planned
    sc, sh = t["shortage_cause"], t["shortage"].set_index("shortage_id")
    lt = t["lead_time"].set_index("lead_time_id")["total_lead_weeks"]
    f = sc[sc["cause_code"] == "LEAD_TIME_FENCE"]
    for r in f.itertuples(index=False):
        assert cal.week_num(sh.at[r.shortage_id, "start_week"]) <= lt[r.evidence_id]


def test_shortage_events_open(planned):
    _, t = planned
    ev, sh = t["shortage_event"], t["shortage"]
    assert len(ev) == len(sh)
    assert (ev["to_status"] == "OPEN").all() and (sh["status"] == "OPEN").all()
    assert plan_checks.check_shortage_lifecycle(t).failures == 0


# --- replenishment / production -------------------------------------------------------------

def test_planned_orders_respect_policy(planned, test_cfg):
    _, t = planned
    assert len(t["planned_order"]) > 0
    assert (t["planned_order"]["qty_base_ea"] > 0).all()
    assert plan_checks.check_planned_orders(t, test_cfg).failures == 0


def test_capacity_ledger_never_negative(planned, test_cfg):
    res, t = planned
    assert (res.mrp.supplier_remaining >= 0).all()
    assert (res.mrp.plant_remaining >= 0).all()
    assert plan_checks.check_capacity_ledger(t, test_cfg).failures == 0


def test_production_and_components(planned):
    _, t = planned
    for r in plan_checks.check_production_and_components(t):
        assert r.failures == 0, r.as_dict()
    pp = t["production_plan"]
    assert (pp["status"] == "PLANNED").any() and (pp["status"] == "FIRM").any()


def test_lot_sizing_helpers():
    assert replenish.lot_units(1, 12, 4, 2) == 4         # MOQ
    assert replenish.lot_units(100, 12, 4, 2) == 10      # ceil(100/12)=9 -> multiple of 2 -> 10
    assert replenish.fit_units(100, 10, 12, 4, 2) == 8   # 100 EA fits 8 cases
    assert replenish.fit_units(40, 10, 12, 4, 2) == 0    # 2 cases < MOQ


def test_plan_is_deterministic(generated, test_cfg):
    _, t = generated
    a = plan.plan({k: v.copy() for k, v in t.items()}, test_cfg)
    b = plan.plan({k: v.copy() for k, v in t.items()}, test_cfg)
    for name in plan.OUTPUT_TABLES:
        ta = tableio.to_text(a.outputs[name], schema.BY_NAME[name])
        tb = tableio.to_text(b.outputs[name], schema.BY_NAME[name])
        assert ta == tb, name


# --- quality gate ---------------------------------------------------------------------------

def _gate(t, cfg):
    return quality_gate.run(t, cfg.weeks)


def test_quality_gate_clean_baseline(generated, test_cfg):
    _, t = generated
    assert len(_gate(t, test_cfg).exceptions) == 0


def _open_po(t, statuses=quality_gate.OPEN_STATUS, nth=0, **filt):
    po = t["purchase_order"]
    m = po["status"].isin(statuses)
    for k, v in filt.items():
        m &= po[k] == v
    return po[m].sort_values("po_id").iloc[nth]


def test_gate_uom_mismatch_corrected(generated, test_cfg):
    _, t0 = generated
    t = _copy(t0)
    p = _open_po(t, order_uom="CASE")
    i = t["purchase_order"].index[t["purchase_order"]["po_id"] == p["po_id"]][0]
    t["purchase_order"].loc[i, "qty_base_ea"] = p["order_qty"]
    g = _gate(t, test_cfg)
    e = g.exceptions
    assert list(e["issue_type"]) == ["UOM_MISMATCH"] and e.iloc[0]["record_id"] == p["po_id"]
    assert e.iloc[0]["action_taken"] == "CORRECTED"
    assert g.po.set_index("po_id").at[p["po_id"], "qty_base_ea"] == p["qty_base_ea"]


def test_gate_lead_time_ignored_corrected(generated, test_cfg):
    _, t0 = generated
    t = _copy(t0)
    p = _open_po(t)
    order_week = cal.offset_label(cal.week_offset(pd.Timestamp(p["order_date"]).date()))
    po = t["purchase_order"]
    i = po.index[po["po_id"] == p["po_id"]][0]
    po.loc[i, ["expected_receipt_week", "revised_receipt_week"]] = order_week
    g = _gate(t, test_cfg)
    e = g.exceptions
    assert list(e["issue_type"]) == ["LEAD_TIME_IGNORED"] and e.iloc[0]["action_taken"] == "CORRECTED"
    fixed = g.po.set_index("po_id").loc[p["po_id"]]
    assert fixed["expected_receipt_week"] == p["expected_receipt_week"]
    assert fixed["revised_receipt_week"] == p["revised_receipt_week"]


def test_gate_duplicate_inbound_excluded(generated, test_cfg):
    _, t0 = generated
    t = _copy(t0)
    inv = t["inventory"]
    row = inv[inv["inventory_status"] == "IN_TRANSIT"].iloc[0].copy()
    row["inventory_id"] = "INV-9999999"
    t["inventory"] = pd.concat([inv, row.to_frame().T], ignore_index=True)
    g = _gate(t, test_cfg)
    e = g.exceptions
    assert list(e["issue_type"]) == ["DUPLICATE_INBOUND"] and e.iloc[0]["record_id"] == "INV-9999999"
    assert e.iloc[0]["action_taken"] == "EXCLUDED"
    assert "INV-9999999" not in set(g.inventory["inventory_id"])
    # the receipt is still counted exactly once (from the PO)
    base = _gate(t0, test_cfg).firm_receipts["qty_ea"].sum()
    assert g.firm_receipts["qty_ea"].sum() == base


def test_gate_capacity_exceeded_capped(generated, test_cfg):
    _, t0 = generated
    t = _copy(t0)
    p = _open_po(t)
    w = p["revised_receipt_week"]
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])["capacity_qty_ea"][(p["supplier_id"], w)]
    extra = p.copy()
    extra["po_id"] = "PO-2026-999999"
    extra["qty_base_ea"] = int(cap)
    extra["order_qty"] = int(cap) // int(p["uom_conversion"])
    extra["qty_base_ea"] = extra["order_qty"] * int(p["uom_conversion"])
    t["purchase_order"] = pd.concat([t["purchase_order"], extra.to_frame().T], ignore_index=True)
    g = _gate(t, test_cfg)
    e = g.exceptions
    assert set(e["issue_type"]) == {"SUPPLIER_CAPACITY_EXCEEDED"}
    assert "CAPPED" in set(e["action_taken"])
    fr = g.firm_receipts
    wk = fr[(fr["supplier_id"] == p["supplier_id"]) & (fr["w"] == cal.label_offset(w))]["qty_ea"].sum()
    assert wk <= cap
    assert fr["pushed_from"].notna().any()
    assert (g.supplier_remaining.to_numpy() >= 0).all()


def test_gate_boundaries_raise_nothing(generated, test_cfg):
    """Load exactly = capacity, PO qty exactly = MOQ, receipt exactly = order week + lead time: no exception."""
    _, t0 = generated
    t = _copy(t0)
    po = t["purchase_order"]
    unshipped = ("CREATED", "CONFIRMED")        # no IN_TRANSIT mirror row to keep in sync
    p = _open_po(t, unshipped)
    cap = t["supplier_capacity"].set_index(["supplier_id", "week"])["capacity_qty_ea"]
    load = checks.firm_po_load(po, test_cfg.weeks).set_index(["supplier_id", "week"])["qty_base_ea"]
    key = (p["supplier_id"], p["revised_receipt_week"])
    spare = int(cap[key] - load[key])
    i = po.index[po["po_id"] == p["po_id"]][0]
    conv = int(p["uom_conversion"])
    if conv == 1 and spare > 0:          # top the PO up so the supplier-week is exactly full
        po.loc[i, "order_qty"] = int(p["order_qty"]) + spare
        po.loc[i, "qty_base_ea"] = int(p["qty_base_ea"]) + spare
    ss = t["supplier_sku"].set_index("supplier_sku_id")
    q = _open_po(t, unshipped, nth=1)
    j = po.index[po["po_id"] == q["po_id"]][0]
    po.loc[j, "order_qty"] = int(ss.at[q["supplier_sku_id"], "moq"])
    po.loc[j, "qty_base_ea"] = int(ss.at[q["supplier_sku_id"], "moq"]) * int(q["uom_conversion"])
    assert len(_gate(t, test_cfg).exceptions) == 0


# --- isolation ------------------------------------------------------------------------------

def test_plan_and_validate_never_reference_ground_truth():
    for sub in ("plan", "validate"):
        for f in (SRC / sub).rglob("*.py"):
            assert "ground_truth" not in f.read_text(encoding="utf-8"), f
