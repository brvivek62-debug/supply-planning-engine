"""Validation checks. Generic checks are driven entirely by planner.schema; domain checks encode
business rules (ledger reconciliation, PO lifecycle, lead-time arithmetic, capacity...).

Every check returns a CheckResult with rows checked and failures; nothing here reads ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids
from planner.schema import BY_NAME, LIFECYCLES, Table


@dataclass
class CheckResult:
    check: str
    table: str
    rows_checked: int
    failures: int
    detail: str = ""
    examples: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "PASS" if self.failures == 0 else "FAIL"

    def as_dict(self) -> dict:
        return {"check": self.check, "table": self.table, "rows_checked": self.rows_checked,
                "failures": self.failures, "status": self.status, "detail": self.detail,
                "examples": self.examples[:5]}


def _result(check: str, table: str, n: int, bad: pd.Series | np.ndarray, df: pd.DataFrame | None = None,
            key: list[str] | None = None, detail: str = "") -> CheckResult:
    bad = np.asarray(bad, dtype=bool)
    ex: list[str] = []
    if df is not None and bad.any():
        cols = key or [c for c in df.columns[:1]]
        ex = ["/".join(str(v) for v in row) for row in df.loc[bad, cols].head(5).itertuples(index=False)]
    return CheckResult(check, table, n, int(bad.sum()), detail, ex)


def _blank(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype(str) == "")


# --------------------------------------------------------------------------------------------
# Generic, schema-driven checks
# --------------------------------------------------------------------------------------------

def check_pk(df: pd.DataFrame, t: Table) -> CheckResult:
    pk = list(t.pk)
    bad = df.duplicated(pk, keep=False) | df[pk].isna().any(axis=1)
    return _result("pk_unique", t.name, len(df), bad, df, pk, f"PK ({', '.join(pk)}) unique and non-null")


def check_required(df: pd.DataFrame, t: Table) -> list[CheckResult]:
    out = []
    req = [f.name for f in t.fields if f.required]
    bad = np.zeros(len(df), dtype=bool)
    for c in req:
        bad |= _blank(df[c]).to_numpy()
    out.append(_result("required_not_null", t.name, len(df), bad, df, list(t.pk), "required fields populated"))
    return out


def check_enums(df: pd.DataFrame, t: Table) -> CheckResult:
    bad = np.zeros(len(df), dtype=bool)
    for f in t.fields:
        if f.allowed is None:
            continue
        s = df[f.name]
        present = ~_blank(s) if f.dtype != "boolean" else s.notna()
        ok = s.isin(list(f.allowed))
        bad |= (present & ~ok).to_numpy()
    return _result("enum_allowed", t.name, len(df), bad, df, list(t.pk), "values within allowed enums")


def check_id_formats(df: pd.DataFrame, t: Table) -> CheckResult:
    bad = np.zeros(len(df), dtype=bool)
    for f in t.fields:
        if not f.id_kind:
            continue
        s = df[f.name]
        present = ~_blank(s)
        ok = s.astype(str).str.fullmatch(ids.PATTERNS[f.id_kind])
        bad |= (present & ~ok).to_numpy()
    return _result("id_format", t.name, len(df), bad, df, list(t.pk), "ID columns match their regex")


def check_bounds(df: pd.DataFrame, t: Table) -> CheckResult:
    bad = np.zeros(len(df), dtype=bool)
    for f in t.fields:
        if f.ge is None and f.le is None and f.gt is None:
            continue
        v = pd.to_numeric(df[f.name], errors="coerce").astype(float)
        present = v.notna()
        b = pd.Series(False, index=df.index)
        if f.ge is not None:
            b |= v < f.ge
        if f.gt is not None:
            b |= v <= f.gt
        if f.le is not None:
            b |= v > f.le
        bad |= (present & b).to_numpy()
    return _result("value_bounds", t.name, len(df), bad, df, list(t.pk), "numeric fields within bounds")


def check_fks(tables: dict[str, pd.DataFrame], t: Table) -> list[CheckResult]:
    out = []
    df = tables[t.name]
    for k in t.fks:
        if k.ref_table not in tables:
            continue
        parent = tables[k.ref_table]
        cols, refs = list(k.columns), list(k.ref_columns)
        present = ~df[cols].isna().any(axis=1)
        if len(cols) == 1:
            ok = df[cols[0]].isin(set(parent[refs[0]]))
        else:
            pk = pd.MultiIndex.from_frame(parent[refs])
            ok = pd.Series(pd.MultiIndex.from_frame(df[cols].astype(object)).isin(pk), index=df.index)
        bad = present & ~ok
        out.append(_result("fk_integrity", t.name, int(present.sum()), bad, df, cols,
                           f"{','.join(cols)} -> {k.ref_table}.{','.join(refs)}"))
    return out


def check_natural_key(df: pd.DataFrame, t: Table) -> CheckResult | None:
    if not t.natural_key:
        return None
    nk = list(t.natural_key)
    bad = df.duplicated(nk, keep=False)
    return _result("duplicate_natural_key", t.name, len(df), bad, df, nk, f"natural key ({', '.join(nk)}) unique")


def check_effective_dates(df: pd.DataFrame, t: Table) -> CheckResult | None:
    if "effective_from" not in df.columns or "effective_to" not in df.columns:
        return None
    et = df["effective_to"]
    present = ~_blank(et)
    bad = present & (et.astype(str) < df["effective_from"].astype(str))
    return _result("effective_date_order", t.name, len(df), bad, df, list(t.pk), "effective_from <= effective_to")


def run_generic(tables: dict[str, pd.DataFrame]) -> list[CheckResult]:
    out: list[CheckResult] = []
    for name, df in tables.items():
        t = BY_NAME[name]
        out.append(check_pk(df, t))
        out += check_required(df, t)
        out.append(check_enums(df, t))
        out.append(check_id_formats(df, t))
        out.append(check_bounds(df, t))
        out += check_fks(tables, t)
        for r in (check_natural_key(df, t), check_effective_dates(df, t)):
            if r is not None:
                out.append(r)
    return out


# --------------------------------------------------------------------------------------------
# Domain checks on generated inputs
# --------------------------------------------------------------------------------------------

def check_on_hand_equals_ledger(tables) -> CheckResult:
    inv, txn = tables["inventory"], tables["inventory_txn"]
    oh = inv[inv["inventory_status"] == "ON_HAND"].set_index(["sku_id", "location_id"])["qty_ea"]
    led = txn.groupby(["sku_id", "location_id"])["qty_ea"].sum().reindex(oh.index, fill_value=0)
    bad = (oh.astype(int) != led.astype(int)).to_numpy()
    dup = oh.index.duplicated()
    return _result("on_hand_equals_ledger", "inventory", len(oh), bad | dup, oh.reset_index(), ["sku_id", "location_id"],
                   "ON_HAND qty = sum(inventory_txn.qty_ea) per SKU-location (one ON_HAND row each)")


def check_ledger_signs(tables) -> CheckResult:
    txn = tables["inventory_txn"]
    q, ty = txn["qty_ea"], txn["txn_type"]
    bad = ((ty.isin(["OPENING", "RECEIPT"]) & (q <= 0)) | ((ty == "ISSUE") & (q >= 0)) | ((ty == "ADJUSTMENT") & (q == 0))
           | ((ty == "RECEIPT") & _blank(txn["ref_id"])) | ((ty != "RECEIPT") & ~_blank(txn["ref_id"])))
    return _result("ledger_sign_and_ref", "inventory_txn", len(txn), bad, txn, ["txn_id"],
                   "OPENING/RECEIPT > 0, ISSUE < 0, ADJUSTMENT != 0, ref_id iff RECEIPT")


def check_receipts_match_received_pos(tables) -> CheckResult:
    txn, po = tables["inventory_txn"], tables["purchase_order"]
    rec = txn[txn["txn_type"] == "RECEIPT"].set_index("ref_id")
    recv = po[po["status"] == "RECEIVED"].set_index("po_id")
    idx = recv.index.union(rec.index)
    a = rec["qty_ea"].reindex(idx)
    b = recv["qty_base_ea"].reindex(idx)
    bad = (a != b) | a.isna() | b.isna()
    bad |= pd.Series(rec.index.duplicated(keep=False), index=rec.index).reindex(idx, fill_value=False)
    return _result("receipts_match_received_pos", "inventory_txn", len(idx), bad.to_numpy(),
                   pd.DataFrame({"po_id": idx}), ["po_id"], "one RECEIPT per RECEIVED PO with qty_base_ea")


def check_ledger_dates(tables, as_of: date, hist_start: str) -> CheckResult:
    txn = tables["inventory_txn"]
    bad = (txn["txn_date"] < hist_start) | (txn["txn_date"] >= as_of.isoformat())
    return _result("ledger_dates_in_window", "inventory_txn", len(txn), bad, txn, ["txn_id"],
                   f"{hist_start} <= txn_date < as-of")


def check_po_quantities(tables) -> CheckResult:
    po, ss = tables["purchase_order"], tables["supplier_sku"].set_index("supplier_sku_id")
    moq = po["supplier_sku_id"].map(ss["moq"])
    om = po["supplier_sku_id"].map(ss["order_multiple"])
    conv = po["supplier_sku_id"].map(ss["uom_conversion"])
    uom = po["supplier_sku_id"].map(ss["purchase_uom"])
    bad = ((po["qty_base_ea"] != po["order_qty"] * po["uom_conversion"]) | (po["order_qty"] < moq)
           | (po["order_qty"] % om != 0) | (po["uom_conversion"] != conv) | (po["order_uom"] != uom))
    return _result("po_qty_uom_moq", "purchase_order", len(po), bad, po, ["po_id"],
                   "qty_base_ea = order_qty x uom_conversion; order_qty >= MOQ and multiple of order_multiple; UoM matches supplier_sku")


def check_po_sourcing(tables) -> CheckResult:
    po = tables["purchase_order"]
    ss = tables["supplier_sku"].set_index("supplier_sku_id")
    sup = tables["supplier"].set_index("supplier_id")
    lt = tables["lead_time"].set_index("lead_time_id")
    region = tables["location"].set_index("location_id")["region"]
    od = po["order_date"]
    s_from = po["supplier_sku_id"].map(ss["effective_from"])
    s_to = po["supplier_sku_id"].map(ss["effective_to"])
    l_from = po["lead_time_id"].map(lt["effective_from"])
    l_to = po["lead_time_id"].map(lt["effective_to"])
    bad = ((po["supplier_sku_id"].map(ss["approval_status"]) != "APPROVED")
           | (po["supplier_sku_id"].map(ss["supplier_id"]) != po["supplier_id"])
           | (po["supplier_sku_id"].map(ss["sku_id"]) != po["sku_id"])
           | (po["supplier_id"].map(sup["status"]) != "ACTIVE")
           | (s_from > od) | (~_blank(s_to) & (s_to.astype(str) < od))
           | (po["lead_time_id"].map(lt["supplier_id"]) != po["supplier_id"])
           | (po["lead_time_id"].map(lt["dest_region"]) != po["ship_to_location_id"].map(region))
           | (po["lead_time_id"].map(lt["is_expedite_lane"]) != False)  # noqa: E712
           | (l_from > od) | (~_blank(l_to) & (l_to.astype(str) < od)))
    return _result("po_sourcing_valid", "purchase_order", len(po), bad, po, ["po_id"],
                   "APPROVED + effective supplier_sku of an ACTIVE supplier; standard lane of that supplier to the ship-to region, effective on order date")


def check_po_dates(tables, as_of: date) -> CheckResult:
    po = tables["purchase_order"]
    lt = tables["lead_time"].set_index("lead_time_id")
    L = po["lead_time_id"].map(lt["total_lead_weeks"]).astype(int)
    order_off = po["order_date"].map(lambda s: cal.week_offset(date.fromisoformat(s)))
    exp = po["expected_receipt_week"].map(cal.label_offset)
    rev = po["revised_receipt_week"].map(cal.label_offset)
    bad = ((exp != order_off + L) | (rev != exp + po["delay_weeks"]) | (po["order_date"] >= as_of.isoformat())
           | (exp <= order_off))
    return _result("po_receipt_week_arithmetic", "purchase_order", len(po), bad, po, ["po_id"],
                   "expected = order week + total_lead_weeks; revised = expected + delay; order_date < as-of; order < receipt")


def check_po_lifecycle(tables, as_of: date) -> list[CheckResult]:
    po, ev = tables["purchase_order"], tables["purchase_order_event"]
    lc = LIFECYCLES["purchase_order"]
    ev = ev.sort_values(["po_id", "seq"])
    prev_to = ev.groupby("po_id")["to_status"].shift(1)
    prev_ts = ev.groupby("po_id")["event_ts"].shift(1)
    prev_seq = ev.groupby("po_id")["seq"].shift(1)
    frm = ev["from_status"].where(~_blank(ev["from_status"]), None)
    trans_ok = np.array([lc.allowed(None if (f is None or (isinstance(f, float) and f != f)) else f, t)
                         for f, t in zip(frm, ev["to_status"])])
    chain_ok = (prev_to.isna() & frm.isna()) | (prev_to == frm)
    seq_ok = (prev_seq.isna() & (ev["seq"] == 1)) | (ev["seq"] == prev_seq + 1)
    ts_ok = prev_ts.isna() | (ev["event_ts"] > prev_ts)
    after = ev["event_ts"].str[:10] >= as_of.isoformat()
    r1 = _result("lifecycle_transitions", "purchase_order_event", len(ev), ~(trans_ok & chain_ok & seq_ok), ev,
                 ["event_id"], "allowed transitions, from_status = previous to_status, seq consecutive from 1")
    r2 = _result("event_ts_increasing", "purchase_order_event", len(ev), ~ts_ok, ev, ["event_id"],
                 "event timestamps strictly increase per PO")
    r3 = _result("no_event_after_as_of", "purchase_order_event", len(ev), after, ev, ["event_id"],
                 "no event dated on/after the as-of date")
    last = ev.groupby("po_id")["to_status"].last()
    first_ts = ev.groupby("po_id")["event_ts"].first().str[:10]
    st = po.set_index("po_id")
    bad = (st["status"] != last.reindex(st.index)) | (first_ts.reindex(st.index) != st["order_date"])
    r4 = _result("status_equals_last_event", "purchase_order", len(po), bad.to_numpy(), po, ["po_id"],
                 "PO status = last event's to_status; first event on order_date")
    return [r1, r2, r3, r4]


def firm_po_load(po: pd.DataFrame, weeks: int) -> pd.DataFrame:
    """Open (non-cancelled, not received) PO quantity per supplier and revised receipt week in the horizon."""
    open_po = po[~po["status"].isin(["RECEIVED", "CANCELLED"])].copy()
    open_po["w"] = open_po["revised_receipt_week"].map(cal.label_offset)
    open_po = open_po[(open_po["w"] >= 1) & (open_po["w"] <= weeks)]
    open_po["week"] = open_po["w"].map(cal.week_label)
    return open_po.groupby(["supplier_id", "week"], as_index=False)["qty_base_ea"].sum()


def check_supplier_capacity(tables, weeks: int) -> CheckResult:
    load = firm_po_load(tables["purchase_order"], weeks)
    cap = tables["supplier_capacity"].set_index(["supplier_id", "week"])["capacity_qty_ea"]
    c = cap.reindex(pd.MultiIndex.from_frame(load[["supplier_id", "week"]])).to_numpy()
    bad = np.isnan(c.astype(float)) | (load["qty_base_ea"].to_numpy() > c)
    return _result("firm_po_within_supplier_capacity", "purchase_order", len(load), bad, load, ["supplier_id", "week"],
                   "open PO qty per supplier and receipt week <= capacity_qty_ea")


def check_in_transit(tables) -> CheckResult:
    inv, po = tables["inventory"], tables["purchase_order"]
    it = inv[inv["inventory_status"] == "IN_TRANSIT"]
    shipped = po[po["status"].isin(["SHIPPED", "IN_TRANSIT", "DELAYED"])].set_index("po_id")
    idx = shipped.index.union(pd.Index(it["po_id"].dropna()))
    cnt = it["po_id"].value_counts().reindex(idx, fill_value=0)
    q = it.groupby("po_id")["qty_ea"].sum().reindex(idx)
    bad = (cnt != 1) | (q != shipped["qty_base_ea"].reindex(idx)) | ~idx.isin(shipped.index)
    bad_on_hand = (inv["inventory_status"] == "ON_HAND") & ~_blank(inv["po_id"])
    return _result("in_transit_mirrors_open_pos", "inventory", len(idx) + len(inv), np.concatenate([bad.to_numpy(), bad_on_hand.to_numpy()]),
                   None, None, "exactly one IN_TRANSIT row per SHIPPED/IN_TRANSIT/DELAYED PO with its qty; no po_id on ON_HAND")


def check_lead_time(tables) -> list[CheckResult]:
    lt = tables["lead_time"]
    tot = np.ceil((lt["production_days"] + lt["transit_days"]) / 7).astype(int)
    bad = (tot != lt["total_lead_weeks"]) | ((lt["transport_mode"] == "AIR") != lt["is_expedite_lane"])
    r1 = _result("lead_time_arithmetic", "lead_time", len(lt), bad, lt, ["lead_time_id"],
                 "total_lead_weeks = ceil((production_days + transit_days)/7); AIR <=> expedite")
    # Versions of one lane must not overlap.
    s = lt.sort_values(["supplier_id", "dest_region", "is_expedite_lane", "effective_from"])
    g = s.groupby(["supplier_id", "dest_region", "is_expedite_lane"])
    prev_to = g["effective_to"].shift(1)
    first = prev_to.isna() & g.cumcount().eq(0)
    overlap = ~first & (prev_to.isna() | (prev_to.astype(str) >= s["effective_from"]))
    r2 = _result("lead_time_versions_disjoint", "lead_time", len(lt), overlap.to_numpy(), s, ["lead_time_id"],
                 "lane versions have non-overlapping effective ranges")
    return [r1, r2]


def check_demand(tables) -> CheckResult:
    fd = tables["forecast_demand"]
    sku = tables["sku"].set_index("sku_id")
    expect_id = "DEM-" + fd["sku_id"] + "-" + fd["location_id"] + "-" + fd["week"]
    starts = {w: cal.week_start_of(w).isoformat() for w in fd["week"].unique()}
    bad = ((fd["demand_id"] != expect_id) | (fd["sku_id"].map(sku["sku_type"]) != "FINISHED")
           | (fd["week_start_date"] != fd["week"].map(starts))
           | ((fd["demand_driver"] == "PROMO") & (fd["forecast_qty_ea"] < 2 * fd["baseline_qty_ea"])))
    return _result("demand_derivations", "forecast_demand", len(fd), bad, fd, ["demand_id"],
                   "demand_id composed of its keys; FINISHED only; week_start_date = ISO Monday; PROMO >= 2x baseline")


def check_master_rules(tables) -> CheckResult:
    sku, sl, loc = tables["sku"], tables["sku_location"], tables["location"]
    bad_sku = (sku["unit_price_inr"] <= sku["unit_cost_inr"]) | ((sku["sku_type"] == "COMPONENT") & (sku["sourcing_type"] != "BUY"))
    lt = sl["location_id"].map(loc.set_index("location_id")["location_type"])
    s = sku.set_index("sku_id")
    typ, src = sl["sku_id"].map(s["sku_type"]), sl["sku_id"].map(s["sourcing_type"])
    plant_only = (typ == "COMPONENT") | (src == "MAKE")
    bad_sl = (plant_only & (lt != "PLANT")) | (~plant_only & (lt == "PLANT"))
    bad = np.concatenate([bad_sku.to_numpy(), bad_sl.to_numpy()])
    return _result("assortment_and_price_rules", "sku/sku_location", len(bad), bad, None, None,
                   "price > cost; COMPONENT is BUY; COMPONENT/MAKE only at plants; BUY FINISHED only at DC/STORE")


def check_bridges(tables) -> CheckResult:
    ss, sub, bom, sku = tables["supplier_sku"], tables["sku_substitute"], tables["bom"], tables["sku"].set_index("sku_id")
    buy = sku.index[sku["sourcing_type"] == "BUY"]
    prim = ss[ss["is_primary"]].groupby("sku_id").size().reindex(buy, fill_value=0)
    b1 = (prim != 1).to_numpy()
    b2 = ((sub["sku_id"] == sub["substitute_sku_id"])
          | (sub["sku_id"].map(sku["category"]) != sub["substitute_sku_id"].map(sku["category"]))).to_numpy()
    b3 = ((bom["parent_sku_id"].map(sku["sourcing_type"]) != "MAKE")
          | (bom["component_sku_id"].map(sku["sku_type"]) != "COMPONENT")).to_numpy()
    b4 = ((ss["moq"] % ss["order_multiple"]) != 0).to_numpy()
    bad = np.concatenate([b1, b2, b3, b4])
    return _result("bridge_rules", "supplier_sku/sku_substitute/bom", len(bad), bad, None, None,
                   "one primary per BUY SKU; substitute != itself and same category; BOM parent MAKE, child COMPONENT; MOQ multiple of order_multiple")


def check_safety_stock(tables) -> CheckResult:
    sl, fd, sku, bom = tables["sku_location"], tables["forecast_demand"], tables["sku"].set_index("sku_id"), tables["bom"]
    mean = fd.groupby(["sku_id", "location_id"])["forecast_qty_ea"].mean()
    home = sl[sl["sku_id"].map(sku["sourcing_type"]) == "MAKE"].set_index("sku_id")["location_id"]
    dep = bom.assign(location_id=bom["parent_sku_id"].map(home))
    dep["m"] = [mean.get((p, l), 0.0) * q for p, l, q in dep[["parent_sku_id", "location_id", "qty_per"]].itertuples(index=False)]
    dep = dep.groupby(["component_sku_id", "location_id"])["m"].sum()
    dep.index.names = ["sku_id", "location_id"]
    m = mean.combine_first(dep).reindex(pd.MultiIndex.from_frame(sl[["sku_id", "location_id"]]), fill_value=0.0).to_numpy()
    exp = np.floor(sl["sku_id"].map(sku["safety_stock_weeks"]).to_numpy() * m + 0.5)
    bad = exp != sl["safety_stock_qty"].to_numpy()
    return _result("safety_stock_derived", "sku_location", len(sl), bad, sl, ["sku_id", "location_id"],
                   "safety_stock_qty = round(safety_stock_weeks x mean weekly (dependent) demand)")


def check_production_plan(tables) -> CheckResult:
    pp, cap = tables["production_plan"], tables["production_capacity"].set_index(["plant_location_id", "week"])["capacity_qty_ea"]
    load = pp.groupby(["plant_location_id", "week"])["capacity_used_ea"].sum()
    over = (load > cap.reindex(load.index)).to_numpy()
    bad_rows = ((pp["requested_qty_ea"] - pp["capacity_cut_ea"] - pp["component_cut_ea"] != pp["planned_qty_ea"])
                | (pp["capacity_used_ea"] != pp["planned_qty_ea"])).to_numpy()
    bad = np.concatenate([bad_rows, over])
    return _result("production_reconciles_and_fits", "production_plan", len(bad), bad, None, None,
                   "requested - cuts = planned = capacity_used; plant load <= production capacity")


def run_generated_domain(tables, cfg) -> list[CheckResult]:
    as_of = cfg.as_of_date
    out = [
        check_master_rules(tables), check_bridges(tables), check_safety_stock(tables),
        check_on_hand_equals_ledger(tables), check_ledger_signs(tables), check_receipts_match_received_pos(tables),
        check_ledger_dates(tables, as_of, str(cfg["history_start_date"])),
        check_po_quantities(tables), check_po_sourcing(tables), check_po_dates(tables, as_of),
        check_supplier_capacity(tables, cfg.weeks), check_in_transit(tables), check_demand(tables),
        check_production_plan(tables),
    ]
    out += check_po_lifecycle(tables, as_of)
    out += check_lead_time(tables)
    return out
