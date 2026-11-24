"""Replenishment planning (MASTER_PROMPT 10.3) integrated with production/BOM (10.4).

One deterministic forward pass over the weeks, vectorised over SKU-locations. In each week w:

1. MAKE production request: FIRM weeks use the input plan; later weeks plan lot-for-lot up to safety
   stock (``production.py``).
2. Component dependent demand = requested production x BOM qty_per.
3. BUY MRP (finished BUY SKUs and components): if projected closing < safety stock, raise a planned
   order covering the gap plus ``planned_order_cover_weeks`` of demand, rounded up to MOQ / order
   multiple in purchase UoM, on the primary supplier's standard lane with the lead time effective at
   release. The receipt lands in the latest week <= w reachable with release >= W01; if none exists the
   requirement is inside the lead-time fence and no order is possible. Supplier capacity is allocated per
   receipt week in priority order (criticality, ABC, sku_id, location_id); the rest is ``capacity_cut_ea``.
4. Components that still cannot cover the requested production cut the parents proportionally
   (COMPONENT_SHORTAGE); then plant capacity caps what is buildable (PRODUCTION_CAPACITY). Component
   consumption never exceeds availability.
5. Closing balance for week w.

The PAB table is then recomputed from the recorded flows with cumsum and must equal the loop state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids

from . import balance
from .production import cap_to_plant_capacity, component_limited
from .quality_gate import GateResult

CRIT_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
ABC_ORDER = {"A": 0, "B": 1, "C": 2}


def _effective_on(ef, et, day: str) -> bool:
    return str(ef) <= day and (et is None or (isinstance(et, float) and et != et) or str(et) >= day)


@dataclass
class Grid:
    keys: pd.DataFrame                 # sku_id, location_id (sorted); row i of every matrix
    W: int
    ss: np.ndarray
    opening: np.ndarray
    on_hand_id: np.ndarray             # inventory_id of the ON_HAND row
    is_buy: np.ndarray
    is_make: np.ndarray
    is_comp: np.ndarray
    demand: np.ndarray                 # independent demand (finished SKUs)
    lookahead: np.ndarray              # demand used for lot sizing (estimated dependent demand for components)
    firm: np.ndarray                   # firm PO receipts after the quality gate
    # BUY sourcing
    sup_idx: np.ndarray
    lane_idx: np.ndarray
    ss_id: np.ndarray
    supplier_ids: list[str]
    uom: np.ndarray
    conv: np.ndarray
    moq: np.ndarray
    mult: np.ndarray
    prio: np.ndarray                   # allocation order (0 = first)
    # lanes (per unique supplier x region): by release index r
    lane_L: np.ndarray                 # total_lead_weeks [lanes, W]
    lane_lt: np.ndarray                # lead_time_id [lanes, W]
    lane_mode: np.ndarray              # transport_mode [lanes, W]
    rel_for_receipt: np.ndarray        # [lanes, W] release index for receipt index q (-1 unreachable)
    best_receipt: np.ndarray           # [lanes, W] latest reachable receipt <= w (-1 = fence)
    # production
    plant_idx: np.ndarray
    plants: list[str]
    firm_req: np.ndarray               # FIRM requested (after FIRM capacity cut) [N, W]
    firm_capcut: np.ndarray
    firm_requested: np.ndarray
    firm_ids: dict = field(default_factory=dict)   # (row, w) -> prod_plan_id
    plant_cap: np.ndarray | None = None
    bom: pd.DataFrame | None = None    # parent_row, comp_row, qty_per, bom_id, eff_lo, eff_hi


def build_grid(t: dict[str, pd.DataFrame], gate: GateResult, cfg) -> Grid:
    W = cfg.weeks
    weeks = cal.week_labels(W)
    sku = t["sku"].set_index("sku_id")
    loc = t["location"].set_index("location_id")
    sl = t["sku_location"].sort_values(["sku_id", "location_id"]).reset_index(drop=True)
    keys = sl[["sku_id", "location_id"]].copy()
    N = len(sl)
    row = pd.Series(np.arange(N), index=pd.MultiIndex.from_frame(keys))
    src = sl["sku_id"].map(sku["sourcing_type"]).to_numpy()
    typ = sl["sku_id"].map(sku["sku_type"]).to_numpy()
    is_buy, is_make, is_comp = src == "BUY", src == "MAKE", typ == "COMPONENT"

    inv = gate.inventory
    oh = inv[inv["inventory_status"] == "ON_HAND"].set_index(["sku_id", "location_id"])
    opening = oh["qty_ea"].reindex(row.index).fillna(0).to_numpy(dtype=np.int64)
    on_hand_id = oh["inventory_id"].reindex(row.index).to_numpy(dtype=object)
    ss = sl["safety_stock_qty"].to_numpy(dtype=np.int64)

    fd = t["forecast_demand"]
    wi = fd["week"].map({w: i for i, w in enumerate(weeks)}).to_numpy()
    ri = row.reindex(pd.MultiIndex.from_frame(fd[["sku_id", "location_id"]])).to_numpy()
    demand = np.zeros((N, W), dtype=np.int64)
    demand[ri, wi] = fd["forecast_qty_ea"].to_numpy()

    # BOM links (parent at its home plant -> component at the same plant)
    bom = t["bom"].copy()
    home = sl[is_make].set_index("sku_id")["location_id"]
    bom["plant"] = bom["parent_sku_id"].map(home)
    bom = bom[bom["plant"].notna()]
    bom["parent_row"] = row.reindex(pd.MultiIndex.from_arrays([bom["parent_sku_id"], bom["plant"]])).to_numpy()
    bom["comp_row"] = row.reindex(pd.MultiIndex.from_arrays([bom["component_sku_id"], bom["plant"]])).to_numpy()
    starts = [cal.week_start(i + 1).isoformat() for i in range(W)]
    bom = bom.sort_values("bom_id").reset_index(drop=True)
    eff = np.array([[_effective_on(ef, et, d) for d in starts] for ef, et in zip(bom["effective_from"], bom["effective_to"])],
                   dtype=bool).reshape(len(bom), W)
    bom = bom[["bom_id", "parent_row", "comp_row", "qty_per"]].astype({"parent_row": np.int64, "comp_row": np.int64})
    lookahead = demand.copy()
    for k, (p, c, q) in enumerate(bom[["parent_row", "comp_row", "qty_per"]].itertuples(index=False)):
        lookahead[c] += np.where(eff[k], demand[p] * int(q), 0)

    # firm receipts
    firm = np.zeros((N, W), dtype=np.int64)
    fr = gate.firm_receipts
    if len(fr):
        r_idx = row.reindex(pd.MultiIndex.from_frame(fr[["sku_id", "location_id"]])).to_numpy()
        np.add.at(firm, (r_idx, fr["w"].to_numpy() - 1), fr["qty_ea"].to_numpy())

    # sourcing: primary APPROVED supplier_sku effective at the as-of date
    as_of = cfg.as_of_date.isoformat()
    ss_tab = t["supplier_sku"]
    prim = ss_tab[ss_tab["is_primary"] & (ss_tab["approval_status"] == "APPROVED")
                  & np.array([_effective_on(a, b, as_of) for a, b in zip(ss_tab["effective_from"], ss_tab["effective_to"])], dtype=bool)]
    prim = prim.sort_values("supplier_sku_id").drop_duplicates("sku_id").set_index("sku_id")
    supplier_ids = sorted(t["supplier_capacity"]["supplier_id"].unique())
    sidx = {s: i for i, s in enumerate(supplier_ids)}
    p = prim.reindex(sl["sku_id"])
    sup = p["supplier_id"].to_numpy(dtype=object)
    sup_idx = np.array([sidx.get(s, -1) if b else -1 for s, b in zip(sup, is_buy)], dtype=np.int64)
    region = sl["location_id"].map(loc["region"]).to_numpy(dtype=object)
    lt = t["lead_time"]
    std = lt[~lt["is_expedite_lane"]].sort_values(["supplier_id", "dest_region", "effective_from"])
    lane_keys = sorted({(s, r) for s, r, b in zip(sup, region, is_buy) if b})
    lane_of = {k: i for i, k in enumerate(lane_keys)}
    lane_idx = np.array([lane_of[(s, r)] if b else -1 for s, r, b in zip(sup, region, is_buy)], dtype=np.int64)
    nl = len(lane_keys)
    lane_L = np.zeros((nl, W), dtype=np.int64)
    lane_lt = np.empty((nl, W), dtype=object)
    lane_mode = np.empty((nl, W), dtype=object)
    groups = {k: g for k, g in std.groupby(["supplier_id", "dest_region"])}
    for li, k in enumerate(lane_keys):
        g = groups[k]
        for r in range(W):
            hit = [x for x in g.itertuples(index=False) if _effective_on(x.effective_from, x.effective_to, starts[r])]
            if not hit:
                raise LookupError(f"no standard lane effective for {k} at {starts[r]}")
            lane_L[li, r], lane_lt[li, r], lane_mode[li, r] = hit[0].total_lead_weeks, hit[0].lead_time_id, hit[0].transport_mode
    rel_for_receipt = np.full((nl, W), -1, dtype=np.int64)
    for r in range(W):
        q = r + lane_L[:, r]
        ok = q < W
        rel_for_receipt[np.flatnonzero(ok), q[ok]] = r      # later releases overwrite -> max release
    reach_q = np.where(rel_for_receipt >= 0, np.arange(W)[None, :], -1)
    best_receipt = np.maximum.accumulate(reach_q, axis=1) if W else reach_q

    crit = sl["sku_id"].map(sku["criticality"]).map(CRIT_ORDER).to_numpy()
    abc = sl["sku_id"].map(sku["abc_class"]).map(ABC_ORDER).to_numpy()
    order = np.lexsort((keys["location_id"].to_numpy(), keys["sku_id"].to_numpy(), abc, crit))
    prio = np.empty(N, dtype=np.int64)
    prio[order] = np.arange(N)

    # production
    plants = sorted(loc.index[loc["location_type"] == "PLANT"])
    pidx = {pl: i for i, pl in enumerate(plants)}
    plant_idx = np.array([pidx.get(l, -1) for l in keys["location_id"]], dtype=np.int64)
    pc = t["production_capacity"].pivot(index="plant_location_id", columns="week", values="capacity_qty_ea")
    plant_cap = pc.reindex(index=plants, columns=weeks).fillna(0).to_numpy(dtype=np.int64)
    pp = t["production_plan"]
    pp = pp[pp["status"] == "FIRM"]
    firm_req = np.zeros((N, W), dtype=np.int64)
    firm_capcut = np.zeros((N, W), dtype=np.int64)
    firm_requested = np.zeros((N, W), dtype=np.int64)
    firm_ids = {}
    for r in pp.itertuples(index=False):
        i, w = int(row[(r.sku_id, r.plant_location_id)]), weeks.index(r.week)
        firm_requested[i, w] = r.requested_qty_ea
        firm_capcut[i, w] = r.capacity_cut_ea
        firm_req[i, w] = r.requested_qty_ea - r.capacity_cut_ea
        firm_ids[(i, w)] = r.prod_plan_id

    g = Grid(keys=keys, W=W, ss=ss, opening=opening, on_hand_id=on_hand_id, is_buy=is_buy, is_make=is_make,
             is_comp=is_comp, demand=demand, lookahead=lookahead, firm=firm, sup_idx=sup_idx, lane_idx=lane_idx,
             ss_id=p["supplier_sku_id"].to_numpy(dtype=object), supplier_ids=supplier_ids,
             uom=p["purchase_uom"].to_numpy(dtype=object), conv=p["uom_conversion"].fillna(1).to_numpy(dtype=np.int64),
             moq=p["moq"].fillna(1).to_numpy(dtype=np.int64), mult=p["order_multiple"].fillna(1).to_numpy(dtype=np.int64),
             prio=prio, lane_L=lane_L, lane_lt=lane_lt, lane_mode=lane_mode, rel_for_receipt=rel_for_receipt,
             best_receipt=best_receipt, plant_idx=plant_idx, plants=plants, firm_req=firm_req,
             firm_capcut=firm_capcut, firm_requested=firm_requested, firm_ids=firm_ids, plant_cap=plant_cap, bom=bom)
    g.bom_eff = eff  # type: ignore[attr-defined]
    return g


def lot_units(req_ea: int, conv: int, moq: int, mult: int) -> int:
    """Requirement in EA -> purchase units, rounded up to MOQ and order multiple."""
    units = max(-(-req_ea // conv), moq)
    return -(-units // mult) * mult


def fit_units(avail_ea: int, want: int, conv: int, moq: int, mult: int) -> int:
    """Largest valid purchase quantity (multiple of mult, >= MOQ) <= want that fits in avail_ea; else 0."""
    got = min(want, (max(avail_ea, 0) // conv) // mult * mult)
    return got if got >= moq else 0


@dataclass
class MrpResult:
    grid: Grid
    planned: np.ndarray            # planned receipts [N, W]
    production: np.ndarray         # production receipts [N, W]
    demand: np.ndarray             # demand incl. dependent component demand [N, W]
    closing: np.ndarray
    planned_orders: pd.DataFrame
    production_plan: pd.DataFrame
    supplier_remaining: np.ndarray  # capacity ledger after firm + planned receipts [S, W]
    plant_remaining: np.ndarray     # [P, W]
    limiting_bom: dict              # (parent_row, w) -> bom_id of the limiting component
    capacity_misses: pd.DataFrame   # requirements that found no capacity at all (no order created)


def run_mrp(g: Grid, gate: GateResult, cfg) -> MrpResult:
    W, N = g.W, len(g.keys)
    cover = int(cfg["policy"]["planned_order_cover_weeks"])
    prebuild = int(cfg["policy"].get("prebuild_weeks", cover))
    firm_weeks = int(cfg["production"]["firm_weeks"])
    sup_rem = gate.supplier_remaining.reindex(g.supplier_ids).fillna(0).to_numpy(dtype=np.int64).copy()
    cap_ids = gate.capacity_ids.reindex(g.supplier_ids).to_numpy(dtype=object)
    plant_rem = g.plant_cap.copy()
    la_cum = np.concatenate([np.zeros((N, 1), dtype=np.int64), np.cumsum(g.lookahead, axis=1)], axis=1)

    D = g.demand.copy()
    Rp = np.zeros((N, W), dtype=np.int64)
    Rm = np.zeros((N, W), dtype=np.int64)
    prev = g.opening.copy()
    make_rows = np.flatnonzero(g.is_make)
    comp_rows = np.flatnonzero(g.is_comp)
    buy_rows_by_prio = np.flatnonzero(g.is_buy)[np.argsort(g.prio[g.is_buy], kind="stable")]
    bp, bc, bq = (g.bom["parent_row"].to_numpy(), g.bom["comp_row"].to_numpy(), g.bom["qty_per"].to_numpy(np.int64))
    bids = g.bom["bom_id"].to_numpy(dtype=object)
    orders: list[list] = []
    misses: list[tuple] = []
    prod_rows: list[dict] = []
    limiting: dict = {}

    for w in range(W):
        # 1. production request (FIRM: input plan after its capacity cut; PLANNED: lot-for-lot to safety stock)
        firm_w = w < firm_weeks
        if firm_w:
            base = g.firm_req[:, w].copy()
            requested = g.firm_requested[:, w].copy()
        else:
            requested = np.zeros(N, dtype=np.int64)
            requested[make_rows] = np.maximum(0, g.ss[make_rows] + D[make_rows, w] - prev[make_rows])
            base = requested.copy()
        # 2. dependent demand at the requested production
        eff = g.bom_eff[:, w]
        need = np.zeros(N, dtype=np.int64)
        np.add.at(need, bc[eff], base[bp[eff]] * bq[eff])
        D[comp_rows, w] = need[comp_rows]
        # 3. BUY MRP
        proj = prev + g.firm[:, w] + Rp[:, w] - D[:, w]
        trig = buy_rows_by_prio[proj[buy_rows_by_prio] < g.ss[buy_rows_by_prio]]
        for i in trig:
            li = g.lane_idx[i]
            q = int(g.best_receipt[li, w])
            if q < 0 or g.sup_idx[i] < 0:
                continue                                   # inside the lead-time fence
            req = int(g.ss[i] - proj[i] + la_cum[i, min(W, w + 1 + cover)] - la_cum[i, w + 1])
            conv, moq, mult = int(g.conv[i]), int(g.moq[i]), int(g.mult[i])
            s = int(g.sup_idx[i])
            units = lot_units(req, conv, moq, mult)
            left = units
            main = None
            # receipt week q first, then pull in (pre-build) to earlier reachable weeks with spare capacity
            for qq in range(q, max(-1, q - prebuild - 1), -1):
                if left <= 0:
                    break
                rr = int(g.rel_for_receipt[li, qq])
                if rr < 0:
                    continue
                got = fit_units(int(sup_rem[s, qq]), left, conv, moq, mult)
                if got == 0:
                    continue
                qty = got * conv
                sup_rem[s, qq] -= qty
                Rp[i, qq] += qty
                if qq < w:
                    prev[i] += qty
                left -= got
                o = [i, rr, qq, got, qty, qty, s, str(g.lane_lt[li, rr]), cap_ids[s, qq]]
                orders.append(o)
                if qq == q:
                    main = o
            if left > 0:                       # what still does not fit is cut
                cut_ea = left * conv
                if main is not None:
                    main[5] += cut_ea          # requested_qty_ea on the receipt-week order
                else:                          # nothing fits at all: no zero-qty order, keep the evidence
                    misses.append((i, w, q, cut_ea, cap_ids[s, q], g.supplier_ids[s]))
        # 4. component availability -> proportional parent cut, then plant capacity on what is buildable
        avail_c = prev + g.firm[:, w] + Rp[:, w]
        feasible, lim = component_limited(base, need, avail_c, bp, bc, bq, eff)
        compcut = base - feasible
        for i in np.flatnonzero(compcut > 0):
            limiting[(int(i), w)] = bids[lim[i]]
        if firm_w:
            final, capcut = feasible, g.firm_capcut[:, w].copy()
        else:
            final = cap_to_plant_capacity(feasible, make_rows, g.plant_idx, plant_rem[:, w])
            capcut = feasible - final
        plant_rem[:, w] -= np.bincount(g.plant_idx[make_rows], weights=final[make_rows],
                                       minlength=len(g.plants)).astype(np.int64)
        need_final = np.zeros(N, dtype=np.int64)
        np.add.at(need_final, bc[eff], final[bp[eff]] * bq[eff])
        D[comp_rows, w] = need_final[comp_rows]
        Rm[make_rows, w] = final[make_rows]
        for i in make_rows:
            if requested[i] > 0 or (i, w) in g.firm_ids:
                prod_rows.append({"row": int(i), "w": w, "requested_qty_ea": int(requested[i]),
                                  "capacity_cut_ea": int(capcut[i]), "component_cut_ea": int(compcut[i]),
                                  "planned_qty_ea": int(final[i]), "status": "FIRM" if w < firm_weeks else "PLANNED",
                                  "prod_plan_id": g.firm_ids.get((int(i), w))})
        # 5. close the week
        prev = prev + g.firm[:, w] + Rp[:, w] + Rm[:, w] - D[:, w]

    closing = balance.closing_matrix(g.opening, g.firm, Rp, Rm, D)
    if not np.array_equal(closing[:, -1], prev):
        raise AssertionError("PAB recomputed with cumsum does not match the MRP loop state")
    po_df = _planned_orders_frame(g, orders)
    pp_df = _production_frame(g, prod_rows)
    miss_df = pd.DataFrame(misses, columns=["row", "w", "q", "cut_ea", "capacity_id", "supplier_id"])
    return MrpResult(g, Rp, Rm, D, closing, po_df, pp_df, sup_rem, plant_rem, limiting, miss_df)


def _planned_orders_frame(g: Grid, orders: list[tuple]) -> pd.DataFrame:
    cols = ["planned_order_id", "supplier_id", "sku_id", "location_id", "supplier_sku_id", "lead_time_id",
            "capacity_id", "release_week", "receipt_week", "order_qty", "order_uom", "qty_base_ea",
            "requested_qty_ea", "capacity_cut_ea", "_row", "_r", "_q"]
    if not orders:
        return pd.DataFrame(columns=cols)
    o = pd.DataFrame(orders, columns=["_row", "_r", "_q", "order_qty", "qty_base_ea", "requested_qty_ea", "_s",
                                      "lead_time_id", "capacity_id"])
    o["sku_id"] = g.keys["sku_id"].to_numpy()[o["_row"]]
    o["location_id"] = g.keys["location_id"].to_numpy()[o["_row"]]
    o["supplier_id"] = np.array(g.supplier_ids, dtype=object)[o["_s"]]
    o["supplier_sku_id"] = g.ss_id[o["_row"]]
    o["order_uom"] = g.uom[o["_row"]]
    o["release_week"] = [cal.week_label(r + 1) for r in o["_r"]]
    o["receipt_week"] = [cal.week_label(q + 1) for q in o["_q"]]
    o["capacity_cut_ea"] = o["requested_qty_ea"] - o["qty_base_ea"]
    o = o.sort_values(["_r", "sku_id", "location_id", "_q"], kind="mergesort").reset_index(drop=True)
    o["planned_order_id"] = [ids.planned_order_id(k + 1) for k in range(len(o))]
    return o[cols]


def _production_frame(g: Grid, rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["sku_id"] = g.keys["sku_id"].to_numpy()[df["row"]]
    df["plant_location_id"] = g.keys["location_id"].to_numpy()[df["row"]]
    df["week"] = [cal.week_label(w + 1) for w in df["w"]]
    df["capacity_used_ea"] = df["planned_qty_ea"]
    firm_max = max((ids.parse_counter(v) for v in g.firm_ids.values()), default=0)
    df = df.sort_values(["w", "sku_id", "plant_location_id"], kind="mergesort").reset_index(drop=True)
    new = df["prod_plan_id"].isna()
    df.loc[new, "prod_plan_id"] = [ids.prod_plan_id(firm_max + k + 1) for k in range(int(new.sum()))]
    return df
