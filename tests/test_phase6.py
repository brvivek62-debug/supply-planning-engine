"""Phase 6: validation suite (expected vs unexpected), SQLite FK load, exports, manifest, profile, new metrics."""
import json
import sqlite3

import pytest

from planner import evaluate, export, manifest, profile, tableio
from planner.docs_gen import schema_sql
from planner.validate import report


@pytest.fixture(scope="module")
def validated(planned, generated, test_cfg, tmp_path_factory):
    out, _ = generated
    return report.run(test_cfg, out, tmp_path_factory.mktemp("val"))


def test_unexpected_failures_zero(validated):
    s = validated["summary"]
    assert s["unexpected_failures"] == 0, [c for c in validated["checks"] if c["unexpected_failures"]]
    assert s["failed_checks"] == 0


def test_injected_anomalies_are_expected_failures(validated):
    exp = {c["check"] for c in validated["checks"] if c["status"] == "EXPECTED_FAIL"}
    assert exp == {"po_qty_uom_moq", "po_receipt_week_arithmetic", "firm_po_within_supplier_capacity",
                   "in_transit_mirrors_open_pos"}


def test_sqlite_fk_check_passes(validated):
    r = [c for c in validated["checks"] if c["check"] == "sqlite_fk_check"]
    assert len(r) == 1 and r[0]["failures"] == 0 and r[0]["rows_checked"] > 0


def test_unexplained_failure_is_unexpected(planned, test_cfg):
    _, t = planned
    t = {k: v.copy() for k, v in t.items()}
    t["purchase_order"].loc[t["purchase_order"].index[0], "order_qty"] = -5   # not named by any exception
    from planner.validate import checks
    rows = report.classify(checks.run_generated_domain(t, test_cfg), t)
    assert sum(r["unexpected_failures"] for r in rows) >= 1


def test_export_json_sql_and_manifest(planned, generated, test_cfg):
    out, _ = generated
    r = export.run(test_cfg, out)
    fmt = tableio.fmt_of(test_cfg)
    recs = json.loads((out / "json" / "sku.json").read_text())
    assert len(recs) == len(tableio.read_table("sku", out, fmt))
    con = sqlite3.connect(":memory:")
    con.executescript(schema_sql())
    con.executescript((out / "sql" / "seed.sql").read_text())
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    n = con.execute("SELECT COUNT(*) FROM projected_balance").fetchone()[0]
    assert n == len(tableio.read_table("projected_balance", out, fmt))
    m = json.loads(open(r["manifest"]).read())
    assert m["seed"] == test_cfg.seed and m["files"]
    for f in m["files"]:
        assert len(f["sha256"]) == 64 and not f["over_50mb"]
    assert any(f["path"].endswith("ground_truth.csv") for f in m["files"])
    # export is deterministic
    first = {f["path"]: f["sha256"] for f in m["files"]}
    export.run(test_cfg, out)
    again = {f["path"]: f["sha256"] for f in manifest.build(test_cfg, out)["files"]}
    assert first == again


def test_profile_written(planned, generated, test_cfg, tmp_path):
    out, _ = generated
    p = profile.run(test_cfg, out, tmp_path)
    assert p.read_text().startswith("# Data profile")
    assert len(list((tmp_path / "figures").glob("*.png"))) == 7


def test_recovery_metrics(planned, generated, test_cfg, tmp_path):
    out, _ = generated
    m = evaluate.run(test_cfg, out, tmp_path)["planning"]
    assert 0 <= m["pct_shortage_units_covered"] <= 100
    assert 0 <= m["value_at_risk_recovered_inr"] <= m["value_at_risk_inr"]
    assert m["fill_rate_short_after"] >= m["fill_rate_short_before"]
    assert m["fill_rate_short_before"] <= m["fill_rate_before"]
