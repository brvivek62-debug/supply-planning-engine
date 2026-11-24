import hashlib
from pathlib import Path

import pandas as pd
import pytest

from planner import generate, schema
from planner.config import load_config
from planner.validate import checks


def _copy(tables):
    return {k: v.copy() for k, v in tables.items()}


def _failing(results, name):
    return sum(r.failures for r in results if r.check == name)


def test_all_input_tables_written(baseline, generated):
    out, tables = baseline
    assert set(tables) == set(generate.INPUT_TABLES)
    for name, df in tables.items():
        assert len(df) > 0 or name == "mitigation_proposal", name
        assert list(df.columns) == schema.BY_NAME[name].columns
    _, inj = generated
    assert set(inj) == set(generate.INPUT_TABLES)
    for name, df in inj.items():
        assert len(df) > 0, name
        assert list(df.columns) == schema.BY_NAME[name].columns


def test_generation_order_respects_foreign_keys():
    pos = {n: i for i, n in enumerate(generate.INPUT_TABLES)}
    for n in generate.INPUT_TABLES:
        for k in schema.BY_NAME[n].fks:
            if n == "mitigation_proposal" and k.ref_table == "shortage":
                continue            # proposals are entered after a first planning pass (generate -> plan -> inject)
            assert pos[k.ref_table] < pos[n], (n, k.ref_table)


def test_generic_checks_pass(baseline):
    _, tables = baseline
    bad = [r.as_dict() for r in checks.run_generic(tables) if r.failures]
    assert bad == []


def test_domain_checks_pass(baseline, test_cfg):
    _, tables = baseline
    bad = [r.as_dict() for r in checks.run_generated_domain(tables, test_cfg) if r.failures]
    assert bad == []


def test_on_hand_equals_ledger(baseline):
    _, t = baseline
    assert checks.check_on_hand_equals_ledger(t).failures == 0


def test_po_status_is_last_event_and_transitions_valid(baseline, test_cfg):
    _, t = baseline
    for r in checks.check_po_lifecycle(t, test_cfg.as_of_date):
        assert r.failures == 0, r.as_dict()


def test_volumes_match_config(baseline, test_cfg):
    _, t = baseline
    v = test_cfg.volumes
    assert len(t["sku"]) == v["skus"] and len(t["location"]) == v["locations"]
    assert len(t["supplier"]) == v["suppliers"] and len(t["purchase_order"]) == v["purchase_orders"]
    assert t["forecast_demand"]["week"].nunique() == v["weeks"]


def test_realistic_variation(baseline):
    _, t = baseline
    fd, sku = t["forecast_demand"], t["sku"].set_index("sku_id")
    vol = fd.groupby(fd["sku_id"].map(sku["abc_class"]))["forecast_qty_ea"].sum()
    assert 0.6 < vol["A"] / vol.sum() < 0.92
    assert 0.01 < (fd["demand_driver"] == "PROMO").mean() < 0.06
    po = t["purchase_order"]
    assert 0.15 <= (po["status"] == "RECEIVED").mean() <= 0.25
    assert 0.02 <= (po["status"] == "CANCELLED").mean() <= 0.06
    assert (po["delay_weeks"] > 0).any()
    assert (t["sku_substitute"]["approval_status"] == "APPROVED").any()
    assert t["lead_time"]["effective_to"].notna().any()     # versioned lanes exist


# --- mutation tests: each check must catch the corruption it is designed for -------------------

def test_pk_check_catches_duplicate(baseline):
    _, t = baseline
    df = pd.concat([t["sku"], t["sku"].head(1)], ignore_index=True)
    assert checks.check_pk(df, schema.BY_NAME["sku"]).failures == 2


def test_fk_check_catches_orphan(baseline):
    _, t = baseline
    tt = _copy(t)
    tt["purchase_order"].loc[0, "supplier_id"] = "SUP-9999"
    assert sum(r.failures for r in checks.check_fks(tt, schema.BY_NAME["purchase_order"])) >= 1


def test_composite_fk_catches_unstocked_pair(baseline):
    _, t = baseline
    tt = _copy(t)
    plant = tt["location"].loc[tt["location"]["location_type"] == "PLANT", "location_id"].iloc[0]
    fin = tt["forecast_demand"]
    buy_sku = fin["sku_id"].iloc[0]
    tt["forecast_demand"].loc[0, "location_id"] = plant if tt["sku_location"].query(
        "sku_id == @buy_sku and location_id == @plant").empty else fin["location_id"].iloc[0]
    res = checks.check_fks(tt, schema.BY_NAME["forecast_demand"])
    assert any(r.failures for r in res if "sku_location" in r.detail)


def test_enum_and_id_checks_catch_bad_values(baseline):
    _, t = baseline
    df = t["location"].copy()
    df.loc[0, "location_type"] = "WAREHOUSE"
    df.loc[1, "location_id"] = "LOC-1"
    tb = schema.BY_NAME["location"]
    assert checks.check_enums(df, tb).failures == 1
    assert checks.check_id_formats(df, tb).failures == 1


def test_ledger_check_catches_mismatch(baseline):
    _, t = baseline
    tt = _copy(t)
    i = tt["inventory"].index[tt["inventory"]["inventory_status"] == "ON_HAND"][0]
    tt["inventory"].loc[i, "qty_ea"] += 1
    assert checks.check_on_hand_equals_ledger(tt).failures == 1


def test_lifecycle_check_catches_bad_status_and_late_event(baseline, test_cfg):
    _, t = baseline
    tt = _copy(t)
    tt["purchase_order"].loc[0, "status"] = "RECEIVED" if tt["purchase_order"].loc[0, "status"] != "RECEIVED" else "CREATED"
    ev = tt["purchase_order_event"]
    ev.loc[ev.index[-1], "event_ts"] = "2026-02-01T10:00:00"
    res = checks.check_po_lifecycle(tt, test_cfg.as_of_date)
    assert _failing(res, "status_equals_last_event") >= 1
    assert _failing(res, "no_event_after_as_of") == 1


def test_qty_and_capacity_checks_catch_violations(baseline, test_cfg):
    _, t = baseline
    tt = _copy(t)
    po = tt["purchase_order"]
    case = po.index[po["uom_conversion"] > 1][0]
    po.loc[case, "qty_base_ea"] = po.loc[case, "order_qty"]          # UoM conversion skipped
    assert checks.check_po_quantities(tt).failures >= 1
    open_i = po.index[po["status"].isin(["CONFIRMED", "IN_TRANSIT", "SHIPPED", "CREATED", "DELAYED"])
                      & (po["revised_receipt_week"] >= "2026W01")][0]
    po.loc[open_i, "qty_base_ea"] = 10**9
    assert checks.check_supplier_capacity(tt, test_cfg.weeks).failures >= 1


# --- reproducibility ---------------------------------------------------------------------------

def _digests(folder: Path) -> dict[str, str]:
    """SHA-256 of every generated input table and the ground truth (planner outputs may sit in the same folder)."""
    files = [folder / "csv" / f"{n}.csv" for n in generate.INPUT_TABLES] + [folder / "ground_truth" / "ground_truth.csv"]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def test_same_seed_identical_bytes_different_seed_differs(tmp_path, test_cfg, generated):
    out1, _ = generated
    out2 = tmp_path / "again"
    generate.run(test_cfg, out2)
    assert _digests(out1) == _digests(out2)
    out3 = tmp_path / "other_seed"
    generate.run(load_config("test", seed=12345), out3)
    d1, d3 = _digests(out1), _digests(out3)
    assert d1.keys() == d3.keys()
    assert sum(d1[k] != d3[k] for k in d1) >= len(d1) - 1   # location master may coincide; data must differ
