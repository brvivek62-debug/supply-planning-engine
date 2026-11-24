"""Phase 5: injected anomalies, boundary cases, ground truth and evaluation (test scale)."""
import re
from pathlib import Path

import pandas as pd
import pytest

from planner import evaluate, ids, inject
from planner.schema import BOUNDARY_TYPES, BY_NAME, ISSUE_TYPES
from planner.validate import checks, plan_checks

SRC = Path(__file__).resolve().parents[1] / "src" / "planner"


@pytest.fixture(scope="session")
def gt(generated, test_cfg):
    out, _ = generated
    return evaluate.read_ground_truth(inject.ground_truth_dir(test_cfg.scale, out))


@pytest.fixture(scope="session")
def evaluated(planned, generated, test_cfg, tmp_path_factory):
    out, _ = generated
    return evaluate.run(test_cfg, out, tmp_path_factory.mktemp("reports"))


def test_ground_truth_shape(gt, test_cfg):
    assert list(gt.columns) == BY_NAME["ground_truth"].columns
    assert gt["gt_id"].map(lambda v: ids.is_valid("gt", v)).all() and gt["gt_id"].is_unique
    n_a, n_b = test_cfg["injection"]["anomalies_per_type"], test_cfg["injection"]["boundary_per_type"]
    a = gt[gt["case_class"] == "ANOMALY"]["issue_type"].value_counts()
    b = gt[gt["case_class"] == "BOUNDARY"]["issue_type"].value_counts()
    assert {k: int(a.get(k, 0)) for k in ISSUE_TYPES} == {k: n_a for k in ISSUE_TYPES}
    assert {k: int(b.get(k, 0)) for k in BOUNDARY_TYPES} == {k: n_b for k in BOUNDARY_TYPES}
    assert gt["record_id"].is_unique
    assert (gt.loc[gt["case_class"] == "BOUNDARY", "expected_action"] == "none").all()


def test_every_anomaly_detected_with_exact_precision(evaluated):
    det = pd.DataFrame(evaluated["detection"]).set_index("issue_type")
    assert (det["recall"] == 1.0).all(), det
    assert (det["precision"] == 1.0).all(), det
    assert (det["action_as_expected"] == det["injected"]).all(), det


def test_boundary_cases_raise_no_exception(evaluated):
    assert evaluated["boundary_false_positives"] == 0


def test_expected_outcomes_hold(evaluated):
    assert evaluated["outcomes_checked"] > 0
    assert evaluated["outcomes_ok"] == evaluated["outcomes_checked"]


def test_metrics_report_written(evaluated, generated, tmp_path_factory):
    m = evaluated["planning"]
    assert m["recommended_violations"] == 0
    assert 0 < m["fill_rate_before"] <= m["fill_rate_after"] <= 1


def test_injection_touches_only_targeted_records(baseline, generated, gt):
    """Every row that differs between the clean baseline and the injected inputs is a labelled record."""
    _, b = baseline
    _, g = generated
    labelled = set(gt["record_id"])
    for name in ("purchase_order", "inventory", "supplier_capacity"):
        pk = BY_NAME[name].pk[0]
        x, y = b[name].set_index(pk), g[name].set_index(pk)
        assert set(x.index) <= set(y.index)
        new = set(y.index) - set(x.index)
        common = sorted(set(x.index) & set(y.index))
        xa = x.loc[common].astype(object).where(x.loc[common].notna(), "").astype(str)
        ya = y.loc[common, x.columns].astype(object).where(y.loc[common, x.columns].notna(), "").astype(str)
        changed = set(xa.index[(xa != ya).any(axis=1)])
        assert (new | changed) <= labelled, (name, sorted((new | changed) - labelled)[:5])
    # new PO events belong to injected POs only
    ev_new = set(g["purchase_order_event"]["event_id"]) - set(b["purchase_order_event"]["event_id"])
    ev = g["purchase_order_event"].set_index("event_id")
    assert {ev.at[e, "po_id"] for e in ev_new} <= labelled
    # untouched tables are byte-for-byte equal
    for name in ("sku", "location", "supplier", "sku_location", "supplier_sku", "sku_substitute", "bom",
                 "lead_time", "forecast_demand", "inventory_txn", "production_plan", "production_capacity"):
        pd.testing.assert_frame_equal(b[name], g[name], check_dtype=False)


def test_injected_inputs_fail_only_expected_domain_checks(generated, gt, test_cfg):
    """Raw input checks flag the injected anomalies (that is what the quality gate repairs) and nothing else."""
    _, t = generated
    expected = {"po_qty_uom_moq", "po_receipt_week_arithmetic", "firm_po_within_supplier_capacity",
                "in_transit_mirrors_open_pos"}
    bad = [r for r in checks.run_generated_domain(t, test_cfg) if r.failures]
    assert {r.check for r in bad} <= expected, [r.as_dict() for r in bad]


def test_planner_outputs_valid_on_injected_data(planned, test_cfg):
    _, t = planned
    bad = [r.as_dict() for r in plan_checks.run_plan_checks(t, test_cfg) if r.failures]
    assert bad == []
    bad = [r.as_dict() for r in checks.run_generic(t) if r.failures]
    assert bad == []


def test_manual_proposals_are_violations(planned, gt):
    _, t = planned
    mo = t["mitigation_option"]
    man = mo[mo["source"] == "MANUAL"]
    want = set(gt.loc[gt["issue_type"] == "UNAPPROVED_SUBSTITUTE", "record_id"])
    assert set(man["mitigation_id"]) == want
    assert (man["policy_status"] == "VIOLATION").all() and not man["is_recommended"].any()
    assert (man["created_by_role"] == "PLANNER").all()


def test_ground_truth_isolation():
    """plan/ and validate/ never reference ground truth; only inject/ writes it and only evaluate.py reads it."""
    for sub in ("plan", "validate"):
        for f in (SRC / sub).rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert "ground_truth" not in text, f
            assert not re.search(r"planner\.(inject|evaluate)|from planner import [^\n]*\b(inject|evaluate)\b", text), f
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(SRC).as_posix()
        if "read_ground_truth" in text:
            assert rel in ("evaluate.py",), rel
        if "write_ground_truth" in text:
            assert rel in ("inject/__init__.py", "generate/__init__.py"), rel
