"""Mitigation options (MASTER_PROMPT 10.7) with a capacity ledger.

Shortages are processed in priority-rank order. For each one every applicable option is generated against
the *current* ledger, ``decide`` scores them and picks the recommendation, and only the recommended option's
effects are booked into the ledger, so spare supplier capacity, transferable surplus and plant capacity used
by one shortage are not available to the next. Escalated recommendations reserve capacity too (pending
approval).

Ledger (all integer EA):
- ``sup_rem[S, W]``  supplier capacity left after firm receipts + planned orders (from MRP)
- ``plant_rem[P, W]`` plant capacity left after production (from MRP)
- ``bal[N, W]``       projected closing balance; transfers / substitutions / component use draw it down,
                      receipts add to it. A source may only give what keeps it >= safety stock in every
                      later week.

Requirement and service metrics use the shortage's own PAB (the planner's closing balance):
- requirement      = below_ss_qty_ea (largest gap to safety stock in the run)
- qty_covered_ea   = requirement - largest gap left after the option (so a late arrival covers less)
- coverage_pct     = qty_covered_ea / requirement
- fill_rate_after  = 1 - sum(min(demand, backlog)) / sum(demand) over the run weeks, with the option applied

Option rules (each generated option is then policy-checked in ``decide``):
EXPEDITE_PO         open PO for this SKU-location still CREATED/CONFIRMED, single firm receipt, the supplier's
                    AIR lane brings it in earlier (arrival = max(W01, order week + AIR lead)) and by the run end;
                    needs spare supplier capacity in the new week (the old week is released).
                    Cost = PO qty x air premium.
ALTERNATE_SUPPLIER  every non-primary supplier_sku of the SKU; standard lane effective at release; release >= W01;
                    receipt = first reachable week >= start with spare capacity, <= end; qty = requirement rounded
                    up to MOQ / multiple, shrunk to the capacity left (still >= MOQ).
                    Cost = qty x max(0, alt - primary unit cost) + qty x freight.
TRANSFER            every other location stocking the SKU with surplus: ship week t = arrival - transfer lead
                    (1 wk same region, 2 wk cross region), arrival = max(start, lead); qty = min(requirement,
                    min over w >= t of (bal - SS)). Cost = qty x cost per EA of the distance band.
SUBSTITUTE_SKU      APPROVED sku_substitute effective in the start week whose substitute is stocked at the same
                    location with surplus from the start week; substitute EA = covered EA x substitution_ratio.
                    Cost = covered x unit price x |price_delta_pct| / 100 (price given away or revenue lost).
INCREASE_PRODUCTION MAKE SKU: one production week w in W01..end with spare plant capacity and components above
                    their SS from w on; w maximises coverage (ties: latest week <= start, then earliest).
                    Cost = qty x overtime premium.
BACKORDER           always available: cost = stockout qty x (lost margin + service penalty), covers nothing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from planner import calendar as cal
from planner import ids

from . import decide
from .quality_gate import GateResult
from .replenish import MrpResult, _effective_on, fit_units, lot_units

TYPE_ORDER = ("EXPEDITE_PO", "ALTERNATE_SUPPLIER", "TRANSFER", "SUBSTITUTE_SKU", "INCREASE_PRODUCTION", "BACKORDER")
UNSHIPPED = ("CREATED", "CONFIRMED")

COLUMNS = [
    "mitigation_id", "shortage_id", "option_seq", "option_type", "source", "created_by_role",
    "ref_po_id", "ref_supplier_id", "ref_lead_time_id", "ref_from_location_id", "ref_substitute_id",
    "ref_substitute_sku_id", "ref_plant_location_id", "action_qty_ea", "qty_covered_ea", "coverage_pct",
    "arrival_week", "weeks_late", "incremental_cost_inr", "fill_rate_after", "score", "policy_status",
    "policy_violation_reason", "requires_approval", "is_recommended",
]


@dataclass
class Ledger:
    sup_rem: np.ndarray
    plant_rem: np.ndarray
    bal: np.ndarray

    def surplus(self, row: int, frm: int, ss: int) -> int:
        """Largest qty row can give from week frm on and still keep closing >= SS in every later week."""
        return int((self.bal[row, frm:] - ss).min()) if frm < self.bal.shape[1] else 0

    def apply(self, effects: list[tuple]) -> None:
        for e in effects:
            if e[0] == "sup":
                _, s, w, q = e
                self.sup_rem[s, w] -= q
            elif e[0] == "plant":
                _, p, w, q = e
                self.plant_rem[p, w] -= q
            else:                                   # ("bal", row, lo, hi, qty): bal[row, lo:hi] += qty
                _, r, lo, hi, q = e
                self.bal[r, lo:hi] += q
        if (self.sup_rem < 0).any() or (self.plant_rem < 0).any():
            raise AssertionError("capacity ledger went negative")

    def fits(self, effects: list[tuple]) -> bool:
        """True if the summed capacity effects fit what is left."""
        sup, plant = defaultdict(int), defaultdict(int)
        for e in effects:
            if e[0] == "sup":
                sup[(e[1], e[2])] += e[3]
            elif e[0] == "plant":
                plant[(e[1], e[2])] += e[3]
        return (all(self.sup_rem[k] >= q for k, q in sup.items())
                and all(self.plant_rem[k] >= q for k, q in plant.items()))


class Context:
    """Static lookups shared by every shortage."""

    def __init__(self, t: dict[str, pd.DataFrame], gate: GateResult, mrp: MrpResult, cfg):
        g = mrp.grid
        self.g, self.mrp, self.cfg, self.W = g, mrp, cfg, g.W
        self.costs, self.policy = cfg["costs"], cfg["policy"]
        self.keys_sku = g.keys["sku_id"].to_numpy(dtype=object)
        self.keys_loc = g.keys["location_id"].to_numpy(dtype=object)
        self.row = {(s, l): i for i, (s, l) in enumerate(zip(self.keys_sku, self.keys_loc))}
        self.rows_by_sku = defaultdict(list)
        for i, s in enumerate(self.keys_sku):
            self.rows_by_sku[s].append(i)
        loc = t["location"].set_index("location_id")
        self.region = loc["region"].to_dict()
        sku = t["sku"].set_index("sku_id")
        self.price = sku["unit_price_inr"].astype(float).to_dict()
        self.unit_cost = sku["unit_cost_inr"].astype(float).to_dict()
        self.week_start = [cal.week_start(w + 1).isoformat() for w in range(self.W)]
        self.as_of = cfg.as_of_date.isoformat()

        # sourcing
        sst = t["supplier_sku"].sort_values("supplier_sku_id")
        self.ss_rec = sst.set_index("supplier_sku_id").to_dict("index")
        self.alts = defaultdict(list)
        for r in sst[~sst["is_primary"].astype(bool)].itertuples(index=False):
            self.alts[r.sku_id].append(r.supplier_sku_id)
        self.primary_cost = {}
        for i in np.flatnonzero(g.is_buy):
            sid = g.ss_id[i]
            if isinstance(sid, str):
                self.primary_cost[self.keys_sku[i]] = float(self.ss_rec[sid]["unit_cost_inr"])
        self.sup_status = t["supplier"].set_index("supplier_id")["status"].to_dict()
        self.sidx = {s: k for k, s in enumerate(g.supplier_ids)}
        lt = t["lead_time"].sort_values(["supplier_id", "dest_region", "effective_from", "lead_time_id"])
        self.lanes = defaultdict(list)       # (supplier, region, expedite) -> lane records
        for r in lt.itertuples(index=False):
            self.lanes[(r.supplier_id, r.dest_region, bool(r.is_expedite_lane))].append(r)
        self.lt_rec = lt.set_index("lead_time_id").to_dict("index")

        # open, not yet shipped POs with a single firm receipt (after the quality gate)
        fr = gate.firm_receipts
        n_rec = fr.groupby("po_id").size()
        one = fr[fr["po_id"].map(n_rec) == 1].set_index("po_id")
        self.unshipped = defaultdict(list)
        for r in gate.po[gate.po["status"].isin(UNSHIPPED)].sort_values("po_id").itertuples(index=False):
            if r.po_id in one.index:
                i = self.row.get((r.sku_id, r.ship_to_location_id))
                if i is not None:
                    self.unshipped[i].append((r.po_id, r.supplier_id, r.order_date, int(one.at[r.po_id, "w"]) - 1,
                                              int(one.at[r.po_id, "qty_ea"])))

        # substitutes
        sub = t["sku_substitute"].sort_values("substitute_id")
        self.sub_rec = sub.set_index("substitute_id").to_dict("index")
        self.subs = defaultdict(list)
        for r in sub.itertuples(index=False):
            self.subs[r.sku_id].append(r.substitute_id)

        # BOM per parent row: (comp_row, qty_per, bom index)
        self.bom_of = defaultdict(list)
        for k, (p, c, q) in enumerate(g.bom[["parent_row", "comp_row", "qty_per"]].itertuples(index=False)):
            self.bom_of[int(p)].append((int(c), int(q), k))

    def lane_at(self, sup: str, region: str, w: int, expedite: bool):
        day = self.week_start[min(max(w, 0), self.W - 1)] if w >= 0 else self.as_of
        for r in self.lanes.get((sup, region, expedite), []):
            if _effective_on(r.effective_from, r.effective_to, day):
                return r
        return None


@dataclass
class Shortage:
    shortage_id: str
    row: int
    s: int
    e: int
    sku: str
    loc: str
    ss: int
    below: int
    stockout: int
    var: float
    price: float

    @property
    def base_value(self) -> float:
        """Value used for cost normalisation and approval: value at risk, or the SS-gap value when VaR is 0."""
        return self.var if self.var > 0 else self.below * self.price


def evaluate(ctx: Context, sh: Shortage, delta: np.ndarray) -> tuple[int, float]:
    """(qty_covered_ea, fill_rate_after) when ``delta`` (EA added to closing, per run week) is applied."""
    seg = ctx.mrp.closing[sh.row, sh.s:sh.e + 1] + delta
    gap = max(0, int((sh.ss - seg).max()))
    d = ctx.mrp.demand[sh.row, sh.s:sh.e + 1]
    return sh.below - gap, fill_rate(seg, d)


def fill_rate(closing: np.ndarray, demand: np.ndarray) -> float:
    tot = int(demand.sum())
    if tot <= 0:
        return 1.0
    return 1.0 - float(np.minimum(demand, np.maximum(0, -closing)).sum()) / tot


def _step(sh: Shortage, lo: int, hi: int, q: int) -> np.ndarray:
    """Run-length delta: +q for absolute weeks lo <= w < hi."""
    d = np.zeros(sh.e - sh.s + 1, dtype=np.int64)
    a, b = max(lo, sh.s) - sh.s, min(hi, sh.e + 1) - sh.s
    if b > a:
        d[a:b] = q
    return d


def _opt(sh: Shortage, otype: str, arrival: int | None, qty: int, cost: float, delta: np.ndarray,
         effects: list, ctx: Context, **refs) -> dict:
    covered, fill = evaluate(ctx, sh, delta) if qty > 0 else (0, fill_rate(
        ctx.mrp.closing[sh.row, sh.s:sh.e + 1], ctx.mrp.demand[sh.row, sh.s:sh.e + 1]))
    o = {c: None for c in COLUMNS}
    o.update(refs)
    o.update({"shortage_id": sh.shortage_id, "option_type": otype, "source": "SYSTEM", "created_by_role": "SYSTEM",
              "action_qty_ea": int(qty), "qty_covered_ea": int(max(0, covered)),
              "arrival_week": cal.week_label(arrival + 1) if arrival is not None else None,
              "weeks_late": (sh.e - sh.s + 1) if arrival is None else max(0, arrival - sh.s),
              "incremental_cost_inr": round(max(0.0, float(cost)), 2), "fill_rate_after": round(fill, 4),
              "_effects": effects, "_arrival": arrival})
    return o


# --------------------------------------------------------------------------------------------
# drawing stock from another SKU-location (transfer source, substitute, component)
# --------------------------------------------------------------------------------------------

def draw(ctx: Context, L: Ledger, j: int, t: int, want: int) -> tuple[int, tuple | None]:
    """Largest qty <= want that row j can give from week t while its closing stays >= SS.

    Either permanently (closing - SS >= qty in every week to W52), or with a backfill: the source re-orders the
    qty on its primary standard lane at t, so it only has to stay >= SS until that order lands in week
    b = t + lead time, which needs spare primary-supplier capacity in week b. The larger of the two is used.
    Returns (qty, backfill) with backfill = (supplier index, b, lead_time_id) or None.
    """
    g, W = ctx.g, ctx.W
    if want <= 0 or t >= W:
        return 0, None
    ss = int(g.ss[j])
    best = (max(0, min(want, L.surplus(j, t, ss))), None)
    s = int(g.sup_idx[j])
    if g.is_buy[j] and s >= 0:
        lane = ctx.lane_at(g.supplier_ids[s], ctx.region[ctx.keys_loc[j]], t, False)
        if lane is not None:
            b = t + int(lane.total_lead_weeks)
            if b < W:
                q = max(0, min(want, int((L.bal[j, t:b] - ss).min()), int(L.sup_rem[s, b])))
                if q > best[0]:
                    best = (q, (s, b, lane.lead_time_id))
    return best


def draw_effects(ctx: Context, j: int, t: int, q: int, backfill: tuple | None) -> list[tuple]:
    if backfill is None:
        return [("bal", j, t, ctx.W, -q)]
    s, b, _ = backfill
    return [("bal", j, t, b, -q), ("sup", s, b, q)]


# --------------------------------------------------------------------------------------------
# option generators
# --------------------------------------------------------------------------------------------

def _expedite(ctx: Context, L: Ledger, sh: Shortage) -> list[dict]:
    out = []
    region = ctx.region[sh.loc]
    for po_id, sup, order_date, fw, qty in ctx.unshipped.get(sh.row, []):
        air = ctx.lane_at(sup, region, -1, True)
        if air is None or sup not in ctx.sidx:
            continue
        order_w = cal.week_offset(pd.Timestamp(order_date).date())          # 1 = W01
        arrival = max(0, order_w + int(air.total_lead_weeks) - 1)
        if arrival >= fw or arrival > sh.e:
            continue                                                         # not earlier / too late to help
        s = ctx.sidx[sup]
        if L.sup_rem[s, arrival] < qty:
            continue                                                         # no capacity in the new week
        effects = [("sup", s, arrival, qty), ("bal", sh.row, arrival, fw, qty)]
        if fw < ctx.W:
            effects.append(("sup", s, fw, -qty))                             # old receipt week is released
        out.append(_opt(sh, "EXPEDITE_PO", arrival, qty, qty * float(ctx.costs["air_premium_per_ea"]),
                        _step(sh, arrival, fw, qty), effects, ctx, ref_po_id=po_id, ref_supplier_id=sup,
                        ref_lead_time_id=air.lead_time_id))
    return out


def _alternate(ctx: Context, L: Ledger, sh: Shortage) -> list[dict]:
    out = []
    region = ctx.region[sh.loc]
    prim = ctx.primary_cost.get(sh.sku)
    for ssid in ctx.alts.get(sh.sku, []):
        r = ctx.ss_rec[ssid]
        sup = r["supplier_id"]
        if sup not in ctx.sidx:
            continue
        s = ctx.sidx[sup]
        conv, moq, mult = int(r["uom_conversion"]), int(r["moq"]), int(r["order_multiple"])
        units = lot_units(sh.below, conv, moq, mult)
        best = None
        for rel in range(0, sh.e + 1):
            lane = ctx.lane_at(sup, region, rel, False)
            if lane is None:
                continue
            q = rel + int(lane.total_lead_weeks)
            if q < sh.s:
                continue
            if q > sh.e:
                break
            got = fit_units(int(L.sup_rem[s, q]), units, conv, moq, mult)
            if got > 0:
                best = (rel, q, got, lane)
                break
        if best is None:
            continue                                                         # unreachable or no capacity in time
        rel, q, got, lane = best
        qty = got * conv
        diff = float(r["unit_cost_inr"]) - (prim if prim is not None else float(r["unit_cost_inr"]))
        cost = qty * max(0.0, diff) + qty * float(ctx.costs["alt_supplier_freight_per_ea"])
        o = _opt(sh, "ALTERNATE_SUPPLIER", q, qty, cost, _step(sh, q, ctx.W, qty),
                 [("sup", s, q, qty), ("bal", sh.row, q, ctx.W, qty)], ctx,
                 ref_supplier_id=sup, ref_lead_time_id=lane.lead_time_id)
        o["_supplier_sku_id"], o["_release"] = ssid, rel
        out.append(o)
    return out


def _transfer(ctx: Context, L: Ledger, sh: Shortage) -> list[dict]:
    out = []
    g = ctx.g
    for j in ctx.rows_by_sku[sh.sku]:
        if j == sh.row:
            continue
        frm = ctx.keys_loc[j]
        same = ctx.region[frm] == ctx.region[sh.loc]
        lead = int(ctx.policy["transfer_lead_weeks_same_region" if same else "transfer_lead_weeks_cross_region"])
        arrival = max(sh.s, lead)
        if arrival > sh.e:
            continue
        t = arrival - lead
        qty, bf = draw(ctx, L, j, t, sh.below)
        if qty <= 0:
            continue
        rate = float(ctx.costs["transfer_cost_per_ea_same_region" if same else "transfer_cost_per_ea_cross_region"])
        out.append(_opt(sh, "TRANSFER", arrival, qty, qty * rate, _step(sh, arrival, ctx.W, qty),
                        draw_effects(ctx, j, t, qty, bf) + [("bal", sh.row, arrival, ctx.W, qty)], ctx,
                        ref_from_location_id=frm, ref_lead_time_id=bf[2] if bf else None))
    return out


def substitute_option(ctx: Context, L: Ledger | None, sh: Shortage, sub_id: str, want_ea: int | None = None) -> dict | None:
    """Option using sku_substitute ``sub_id`` (system or manual). With a ledger the qty is limited to the
    substitute's surplus; without one (a hypothetical / non-compliant proposal) the requested qty is used."""
    r = ctx.sub_rec[sub_id]
    j = ctx.row.get((r["substitute_sku_id"], sh.loc))
    ratio = float(r["substitution_ratio"])
    need_sub = int(math.ceil((want_ea if want_ea is not None else sh.below) * ratio - 1e-9))
    bf = None
    if L is not None:
        if j is None:
            return None
        need_sub, bf = draw(ctx, L, j, sh.s, need_sub)
        if need_sub <= 0:
            return None
    covered_orig = int(math.floor(need_sub / ratio + 1e-9)) if ratio > 0 else 0
    if covered_orig <= 0:
        return None
    cost = covered_orig * ctx.price[sh.sku] * abs(float(r["price_delta_pct"])) / 100.0
    effects = [("bal", sh.row, sh.s, ctx.W, covered_orig)]
    if j is not None:
        effects = draw_effects(ctx, j, sh.s, need_sub, bf) + effects
    o = _opt(sh, "SUBSTITUTE_SKU", sh.s, need_sub, cost, _step(sh, sh.s, ctx.W, covered_orig), effects, ctx,
             ref_substitute_id=sub_id, ref_substitute_sku_id=r["substitute_sku_id"],
             ref_lead_time_id=bf[2] if bf else None)
    return o


def _substitutes(ctx: Context, L: Ledger, sh: Shortage) -> list[dict]:
    out = []
    day = ctx.week_start[sh.s]
    for sub_id in ctx.subs.get(sh.sku, []):
        r = ctx.sub_rec[sub_id]
        # the system only proposes substitutes it may use; anything else is a manual proposal checked in decide
        if r["approval_status"] != "APPROVED" or not _effective_on(r["effective_from"], r["effective_to"], day):
            continue
        o = substitute_option(ctx, L, sh, sub_id)
        if o is not None:
            out.append(o)
    return out


def _production(ctx: Context, L: Ledger, sh: Shortage) -> list[dict]:
    g = ctx.g
    if not g.is_make[sh.row]:
        return []
    p = int(g.plant_idx[sh.row])
    eff = g.bom_eff
    best = None
    for w in range(0, sh.e + 1):
        q = min(sh.below, int(L.plant_rem[p, w]))
        comps = [(c, qp) for c, qp, k in ctx.bom_of.get(sh.row, []) if eff[k, w]]
        for c, qp in comps:
            if q <= 0:
                break
            q = min(q, draw(ctx, L, c, w, q * qp)[0] // qp)
        if q <= 0:
            continue
        effects = [("plant", p, w, q), ("bal", sh.row, w, ctx.W, q)]
        for c, qp in comps:
            got, bf = draw(ctx, L, c, w, q * qp)
            effects += draw_effects(ctx, c, w, q * qp, bf)
        if not L.fits(effects):
            continue                                   # two components backfilled from one supplier-week
        covered, _ = evaluate(ctx, sh, _step(sh, w, ctx.W, q))
        key = (covered, w <= sh.s, w if w <= sh.s else -w)
        if best is None or key > best[0]:
            best = (key, w, q, effects)
    if best is None:
        return []
    _, w, q, effects = best
    return [_opt(sh, "INCREASE_PRODUCTION", w, q, q * float(ctx.costs["overtime_premium_per_ea"]),
                 _step(sh, w, ctx.W, q), effects, ctx, ref_plant_location_id=sh.loc)]


def _backorder(ctx: Context, sh: Shortage) -> dict:
    margin = ctx.price[sh.sku] - ctx.unit_cost[sh.sku]
    cost = sh.stockout * (max(0.0, margin) + float(ctx.costs["backorder_service_penalty_per_ea"]))
    return _opt(sh, "BACKORDER", None, 0, cost, np.zeros(sh.e - sh.s + 1, dtype=np.int64), [], ctx)


def generate(ctx: Context, L: Ledger, sh: Shortage) -> list[dict]:
    return (_expedite(ctx, L, sh) + _alternate(ctx, L, sh) + _transfer(ctx, L, sh) + _substitutes(ctx, L, sh)
            + _production(ctx, L, sh) + [_backorder(ctx, sh)])


# --------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------

@dataclass
class MitigationResult:
    options: pd.DataFrame
    ledger_entries: pd.DataFrame    # mitigation_ledger rows
    shortage: pd.DataFrame          # with status updated from the last event
    events: pd.DataFrame
    exceptions: pd.DataFrame        # UNAPPROVED_SUBSTITUTE hits (no exception_id yet)
    ledger: Ledger
    fill_before: float
    fill_after: float


def _shortages(sh_df: pd.DataFrame, ctx: Context) -> list[Shortage]:
    out = []
    for r in sh_df.sort_values("priority_rank").to_dict("records"):
        s = Shortage(r["shortage_id"], int(r["_row"]), int(r["_s"]), int(r["_e"]), r["sku_id"], r["location_id"],
                     int(ctx.g.ss[int(r["_row"])]), int(r["below_ss_qty_ea"]), int(r["shortage_qty_ea"]),
                     float(r["value_at_risk_inr"]), ctx.price[r["sku_id"]])
        out.append(s)
    return out


def run(t: dict[str, pd.DataFrame], gate: GateResult, mrp: MrpResult, sh_df: pd.DataFrame, cfg,
        proposals: pd.DataFrame | None = None) -> MitigationResult:
    ctx = Context(t, gate, mrp, cfg)
    L = Ledger(mrp.supplier_remaining.copy(), mrp.plant_remaining.copy(), mrp.closing.copy())
    manual = defaultdict(list)
    if proposals is not None and len(proposals):
        for r in proposals.sort_values("mitigation_id").itertuples(index=False):
            manual[r.shortage_id].append(r)
    rows, decisions, entries = [], [], []
    for sh in _shortages(sh_df, ctx):
        opts = generate(ctx, L, sh)
        opts.sort(key=lambda o: TYPE_ORDER.index(o["option_type"]))
        for k, o in enumerate(opts, start=1):
            o["option_seq"] = k
        for p in manual.get(sh.shortage_id, []):
            o = decide.manual_option(ctx, L, sh, p)
            if o is not None:
                opts.append(o)
        for o in opts:
            o["mitigation_id"] = ids.mitigation_id(sh.shortage_id, int(o["option_seq"]))
            decide.check_policy(ctx, sh, o)
        rec = decide.recommend(ctx, sh, opts)
        if not L.fits(opts[rec]["_effects"]):
            raise AssertionError(f"recommended option for {sh.shortage_id} does not fit the ledger")
        L.apply(opts[rec]["_effects"])
        entries.extend(_entries(ctx, opts[rec]))
        decisions.append((sh.shortage_id, opts[rec]))
        rows.extend(opts)
    opt_df = pd.DataFrame(rows)
    if len(opt_df) == 0:
        opt_df = pd.DataFrame(columns=COLUMNS + ["_effects", "_arrival"])
    events = decide.events(sh_df, decisions, cfg.as_of_date)
    last = events.sort_values(["shortage_id", "seq"]).groupby("shortage_id")["to_status"].last()
    sh_out = sh_df.copy()
    sh_out["status"] = sh_out["shortage_id"].map(last).fillna(sh_out["status"])
    exc = decide.exceptions(opt_df)
    fin = ~ctx.g.is_comp & (ctx.g.demand.sum(axis=1) > 0)
    before = fill_rate(mrp.closing[fin].ravel(), mrp.demand[fin].ravel())
    after = fill_rate(L.bal[fin].ravel(), mrp.demand[fin].ravel())
    led = pd.DataFrame(entries, columns=["mitigation_id", "entry_seq", "resource_type", "supplier_id",
                                         "plant_location_id", "sku_id", "location_id", "week", "qty_ea"])
    return MitigationResult(opt_df.sort_values("mitigation_id").reset_index(drop=True), led, sh_out, events, exc, L,
                            before, after)


def _entries(ctx: Context, o: dict) -> list[dict]:
    rows = []

    def add(kind, w, q, sup=None, plant=None, row=None):
        if q == 0:
            return
        rows.append({"mitigation_id": o["mitigation_id"], "entry_seq": len(rows) + 1, "resource_type": kind,
                     "supplier_id": sup, "plant_location_id": plant,
                     "sku_id": ctx.keys_sku[row] if row is not None else None,
                     "location_id": ctx.keys_loc[row] if row is not None else None,
                     "week": cal.week_label(w + 1), "qty_ea": int(q)})

    for e in o["_effects"]:
        if e[0] == "sup":
            add("SUPPLIER_CAPACITY", e[2], e[3], sup=ctx.g.supplier_ids[e[1]])
        elif e[0] == "plant":
            add("PLANT_CAPACITY", e[2], e[3], plant=ctx.g.plants[e[1]])
        else:
            _, r, lo, hi, q = e
            add("STOCK", lo, q, row=r)
            if hi < ctx.W:
                add("STOCK", hi, -q, row=r)
    return rows
