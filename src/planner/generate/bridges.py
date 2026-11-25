"""Step 2: bom, sku_location, supplier_sku, sku_substitute (bridge tables)."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from planner import ids

from .common import Ctx, rand_dates


def gen_bom(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("bom")
    sku = ctx["sku"]
    make = sku.loc[sku["sourcing_type"] == "MAKE", "sku_id"].tolist()
    comps = sku.loc[sku["sku_type"] == "COMPONENT", "sku_id"].to_numpy()
    lo, hi = ctx.cfg["assortment"]["components_per_make"]
    k = rng.integers(lo, hi + 1, size=len(make))
    # Every component is used at least once while slots allow: fill first slots from a permutation.
    pool = list(rng.permutation(comps))
    rows = []
    for parent, kk in zip(make, k):
        chosen: list[str] = []
        while len(chosen) < min(kk, len(comps)):
            c = pool.pop() if pool else str(rng.choice(comps))
            if c not in chosen:
                chosen.append(str(c))
        for c in sorted(chosen):
            rows.append((parent, c))
    n = len(rows)
    df = pd.DataFrame(rows, columns=["parent_sku_id", "component_sku_id"])
    df.insert(0, "bom_id", [ids.bom_id(i + 1) for i in range(n)])
    df["qty_per"] = rng.choice([1, 1, 1, 2, 2, 3, 4], size=n)
    df["uom"] = "EA"
    df["effective_from"] = rand_dates(rng, date(2024, 1, 1), date(2025, 6, 30), n)
    df["effective_to"] = None
    return df


def gen_sku_location(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("sku_location")
    sku, loc, bom = ctx["sku"], ctx["location"], ctx["bom"]
    size = ctx.aux["loc_size"]
    plants = loc.loc[loc["location_type"] == "PLANT", "location_id"].to_numpy()
    sellers = loc.loc[loc["location_type"] != "PLANT", "location_id"].to_numpy()
    w = size.loc[sellers].to_numpy()
    w = w / w.sum()
    lo, hi = ctx.cfg["assortment"]["locations_per_finished_sku"]
    pairs: list[tuple[str, str]] = []
    home_plant: dict[str, str] = {}
    for s, typ, src in sku[["sku_id", "sku_type", "sourcing_type"]].itertuples(index=False):
        if typ == "FINISHED" and src == "BUY":
            k = min(int(rng.integers(lo, hi + 1)), len(sellers))
            for l in rng.choice(sellers, size=k, replace=False, p=w):
                pairs.append((s, str(l)))
        elif typ == "FINISHED":  # MAKE: stocked at its home plant only
            p = str(rng.choice(plants))
            home_plant[s] = p
            pairs.append((s, p))
    comp_locs: dict[str, set[str]] = {}
    for parent, comp in bom[["parent_sku_id", "component_sku_id"]].itertuples(index=False):
        comp_locs.setdefault(comp, set()).add(home_plant[parent])
    for s in sku.loc[sku["sku_type"] == "COMPONENT", "sku_id"]:
        for p in sorted(comp_locs.get(s, {str(rng.choice(plants))})):
            pairs.append((s, p))
    df = pd.DataFrame(pairs, columns=["sku_id", "location_id"]).sort_values(["sku_id", "location_id"]).reset_index(drop=True)
    eff = pd.concat([
        df["sku_id"].map(sku.set_index("sku_id")["effective_from"]),
        df["location_id"].map(loc.set_index("location_id")["effective_from"]),
    ], axis=1).max(axis=1)
    df["safety_stock_qty"] = 0          # derived after demand (see demand.fill_safety_stock)
    df["is_active"] = True
    df["effective_from"] = eff.to_numpy()
    ctx.aux["home_plant"] = home_plant
    _expected_rates(ctx, df)
    return df


def _expected_rates(ctx: Ctx, sl: pd.DataFrame) -> None:
    """Latent expected weekly demand per SKU-location (independent + dependent), used for sizing."""
    sku = ctx["sku"].set_index("sku_id")
    rate = ctx.aux["sku_rate"]
    size = ctx.aux["loc_size"]
    fin = sl[sl["sku_id"].map(sku["sku_type"]) == "FINISHED"]
    exp = pd.Series(rate.loc[fin["sku_id"]].to_numpy() * size.loc[fin["location_id"]].to_numpy(),
                    index=pd.MultiIndex.from_frame(fin[["sku_id", "location_id"]]))
    bom = ctx["bom"]
    dep = bom.assign(location_id=bom["parent_sku_id"].map(ctx.aux["home_plant"]))
    dep["rate"] = [exp.get((p, l), 0.0) * q for p, l, q in dep[["parent_sku_id", "location_id", "qty_per"]].itertuples(index=False)]
    dep = dep.groupby(["component_sku_id", "location_id"])["rate"].sum()
    dep.index.names = ["sku_id", "location_id"]
    allidx = pd.MultiIndex.from_frame(sl[["sku_id", "location_id"]])
    ctx.aux["exp_rate"] = exp.combine_first(dep).reindex(allidx, fill_value=0.0)


def gen_supplier_sku(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("supplier_sku")
    sku, sup = ctx["sku"], ctx["supplier"]
    sc = ctx.cfg["supply"]
    lo_alt, hi_alt = sc["alternates_per_buy_sku"]
    weight = ctx.aux["supplier_weight"]
    active = sup.loc[sup["status"] == "ACTIVE", "supplier_id"].to_numpy()
    all_sup = sup["supplier_id"].to_numpy()
    wa = weight.loc[active].to_numpy(); wa = wa / wa.sum()
    wall = weight.loc[all_sup].to_numpy()
    exp = ctx.aux["exp_rate"]
    mean_loc = exp.groupby(level="sku_id").mean()
    rows = []
    buy = sku[sku["sourcing_type"] == "BUY"]
    for s, cp, cost in buy[["sku_id", "case_pack_qty", "unit_cost_inr"]].itertuples(index=False):
        prim = str(rng.choice(active, p=wa))
        n_alt = int(rng.choice(np.arange(lo_alt, hi_alt + 1), p=[0.45, 0.35, 0.20][: hi_alt - lo_alt + 1]))
        mask = all_sup != prim
        ww = wall[mask] / wall[mask].sum()
        alts = list(rng.choice(all_sup[mask], size=min(n_alt, mask.sum()), replace=False, p=ww))
        for j, sid in enumerate([prim] + [str(a) for a in alts]):
            primary = j == 0
            uom = "CASE" if (cp > 1 and rng.random() < 0.7) else "EA"
            conv = int(cp) if uom == "CASE" else 1
            target_ea = rng.uniform(0.5, 2.0) * max(mean_loc.get(s, 1.0), 1.0) * (1.0 if primary else rng.uniform(1.0, 2.0))
            target_units = max(1.0, target_ea / conv)
            if uom == "CASE":
                om = int(rng.choice([1, 1, 2, 4]))
            else:
                om = max([m for m in (1, 5, 10, 20, 50, 100) if m <= max(1.0, target_units / 3)])
            moq = max(om, int(math.ceil(target_units / om)) * om)
            unit_cost = cost * (rng.uniform(0.97, 1.03) if primary else rng.uniform(1.02, 1.20))
            if primary:
                approval, ef, et = "APPROVED", rand_dates(rng, date(2023, 1, 1), date(2025, 6, 30), 1)[0], None
            else:
                approval = str(rng.choice(["APPROVED", "PENDING", "REJECTED"], p=[0.75, 0.15, 0.10]))
                u = rng.random()
                if u < 0.10:   # becomes effective only during the plan year
                    ef = rand_dates(rng, date(2026, 1, 15), date(2026, 6, 30), 1)[0]
                else:
                    ef = rand_dates(rng, date(2023, 1, 1), date(2025, 6, 30), 1)[0]
                # an expiring line ends 2026-03-31..09-30 and at least 4 weeks after it became effective
                et_lo = max(date(2026, 3, 31), date.fromisoformat(ef) + timedelta(days=28))
                et = rand_dates(rng, et_lo, date(2026, 9, 30), 1)[0] if rng.random() < 0.08 else None
            rows.append(dict(supplier_id=sid, sku_id=s, is_primary=primary, approval_status=approval,
                             unit_cost_inr=round(float(unit_cost), 2), purchase_uom=uom, uom_conversion=conv,
                             moq=int(moq), order_multiple=int(om), effective_from=ef, effective_to=et))
    df = pd.DataFrame(rows).sort_values(["sku_id", "is_primary", "supplier_id"], ascending=[True, False, True])
    df.insert(0, "supplier_sku_id", [ids.supplier_sku_id(i + 1) for i in range(len(df))])
    return df.reset_index(drop=True)


def gen_sku_substitute(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("sku_substitute")
    sku = ctx["sku"]
    sc = ctx.cfg["supply"]
    fin = sku[sku["sku_type"] == "FINISHED"].reset_index(drop=True)
    locs = ctx["sku_location"].groupby("sku_id")["location_id"].apply(frozenset)
    price = fin.set_index("sku_id")["unit_price_inr"]
    n_pick = int(round(len(fin) * sc["substitute_share"]))
    picked = sorted(rng.choice(fin["sku_id"].to_numpy(), size=n_pick, replace=False))
    by_cat = fin.groupby("category")["sku_id"].apply(list)
    rows = []
    for s in picked:
        cat = fin.loc[fin["sku_id"] == s, "category"].iloc[0]
        p = price[s]
        cands = [c for c in by_cat[cat] if c != s and abs(price[c] - p) / p <= 0.5]
        shared = [c for c in cands if locs[c] & locs[s]]
        pool = shared or cands
        if not pool:
            continue
        k = min(len(pool), 1 if rng.random() < 0.7 else 2)
        for c in sorted(rng.choice(pool, size=k, replace=False)):
            c = str(c)
            approval = str(rng.choice(["APPROVED", "PENDING", "REJECTED"],
                                      p=[sc["substitute_approved_share"], 0.20, 1 - sc["substitute_approved_share"] - 0.20]))
            ratio = 1.0 if rng.random() < 0.8 else round(float(rng.uniform(0.8, 1.25)), 2)
            et = rand_dates(rng, date(2026, 3, 31), date(2026, 9, 30), 1)[0] if rng.random() < 0.10 else None
            rows.append(dict(sku_id=s, substitute_sku_id=c, approval_status=approval, substitution_ratio=ratio,
                             price_delta_pct=round((price[c] - p) / p * 100, 4),
                             effective_from=rand_dates(rng, date(2025, 1, 1), date(2025, 12, 1), 1)[0],
                             effective_to=et))
    df = pd.DataFrame(rows, columns=["sku_id", "substitute_sku_id", "approval_status", "substitution_ratio",
                                     "price_delta_pct", "effective_from", "effective_to"])
    df.insert(0, "substitute_id", [ids.substitute_id(i + 1) for i in range(len(df))])
    return df
