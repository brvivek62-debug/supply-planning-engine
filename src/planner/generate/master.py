"""Step 1: location -> sku -> supplier."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from planner import ids
from planner.schema import CATEGORIES, REGIONS

from .common import Ctx, exact_split, rand_dates

# Public knowledge (Indian city names) used only for realism.
CITIES: dict[str, list[str]] = {
    "NORTH": ["Delhi", "Gurugram", "Noida", "Chandigarh", "Jaipur", "Lucknow", "Ludhiana", "Kanpur", "Dehradun", "Amritsar"],
    "SOUTH": ["Bengaluru", "Chennai", "Hyderabad", "Kochi", "Coimbatore", "Mysuru", "Visakhapatnam", "Madurai", "Vijayawada", "Thiruvananthapuram"],
    "EAST": ["Kolkata", "Bhubaneswar", "Patna", "Guwahati", "Ranchi", "Siliguri", "Jamshedpur", "Cuttack"],
    "WEST": ["Mumbai", "Pune", "Ahmedabad", "Surat", "Vadodara", "Nashik", "Rajkot", "Panaji"],
    "CENTRAL": ["Bhopal", "Indore", "Nagpur", "Raipur", "Jabalpur", "Gwalior"],
}
TYPE_LABEL = {"PLANT": "Plant", "DC": "DC", "STORE": "Store"}
SIZE_MEDIAN = {"PLANT": 3.0, "DC": 2.0, "STORE": 1.0}
SIZE_SIGMA = {"PLANT": 0.2, "DC": 0.3, "STORE": 0.4}

CATEGORY_COST = {
    "BEVERAGES": 45, "SNACKS": 30, "DAIRY": 55, "STAPLES": 70, "PERSONAL_CARE": 120,
    "HOME_CARE": 95, "BABY_CARE": 220, "HOUSEHOLD_ELECTRICALS": 650,
}
CATEGORY_NOUNS = {
    "BEVERAGES": ["Mango Drink", "Lemon Soda", "Green Tea", "Filter Coffee", "Jeera Soda", "Coconut Water"],
    "SNACKS": ["Masala Chips", "Bhujia", "Cream Biscuit", "Roasted Peanuts", "Khakhra", "Namkeen Mix"],
    "DAIRY": ["Paneer", "Toned Milk", "Curd", "Ghee", "Butter", "Lassi"],
    "STAPLES": ["Basmati Rice", "Toor Dal", "Atta", "Sugar", "Mustard Oil", "Poha"],
    "PERSONAL_CARE": ["Neem Soap", "Herbal Shampoo", "Toothpaste", "Face Wash", "Hair Oil", "Talc"],
    "HOME_CARE": ["Dishwash Bar", "Floor Cleaner", "Detergent Powder", "Toilet Cleaner", "Glass Cleaner", "Agarbatti"],
    "BABY_CARE": ["Baby Wipes", "Diapers", "Baby Lotion", "Baby Powder", "Rash Cream", "Baby Oil"],
    "HOUSEHOLD_ELECTRICALS": ["LED Bulb", "Ceiling Fan", "Extension Board", "Electric Kettle", "Mixer Grinder", "Iron"],
}
COMPONENT_NOUNS = ["PET Preform", "Bottle Cap", "Shrink Film", "Corrugated Carton", "Label Roll", "Laminate Pouch",
                   "Pump Dispenser", "Glass Jar", "Motor Coil", "Switch Assembly", "PCB Board", "Plastic Housing"]
BRANDS = ["Aarav", "Sunrise", "Kesari", "Nirmal", "Utsav", "Vriksha", "Saral", "Prakash", "Ganga", "Tara"]
SIZES = ["100g", "200g", "500g", "1kg", "200ml", "500ml", "1L", "Pack of 4", "Std", "Large"]
SUP_PREFIX = ["Shree", "Sri", "Om", "Jai", "Maa", "New", "Bharat", "Hind", "Deccan", "Coastal", "Northern",
              "Eastern", "Western", "Central", "Royal", "Prime", "Apex", "Vijay", "Lakshmi", "Sai"]
SUP_CORE = ["Ganesh", "Balaji", "Krishna", "Durga", "Ambika", "Shakti", "Sagar", "Kaveri", "Narmada", "Himalaya",
            "Vindhya", "Sahyadri", "Godavari", "Ashoka", "Kalinga"]
SUP_SUFFIX = ["Industries", "Enterprises", "Packaging", "Foods", "Polymers", "Traders"]


def gen_location(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("location")
    mix = ctx.cfg.volumes["location_type_mix"]
    types: list[str] = []
    regions: list[str] = []
    for t in ("PLANT", "DC", "STORE"):
        n = int(mix[t])
        types += [t] * n
        if t == "STORE":
            regions += list(rng.choice(REGIONS, size=n, p=[0.24, 0.24, 0.16, 0.24, 0.12]))
        else:  # spread plants and DCs across regions
            order = list(rng.permutation(REGIONS))
            regions += [order[i % len(order)] for i in range(n)]
    n = len(types)
    cities, names, seen = [], [], {}
    for t, r in zip(types, regions):
        c = str(rng.choice(CITIES[r]))
        seen[(c, t)] = seen.get((c, t), 0) + 1
        cities.append(c)
        names.append(f"{c} {TYPE_LABEL[t]} {seen[(c, t)]}")
    df = pd.DataFrame({
        "location_id": [ids.location_id(i + 1) for i in range(n)],
        "location_name": names, "location_type": types, "city": cities, "region": [str(r) for r in regions],
        "is_active": True,
        "effective_from": rand_dates(rng, date(2012, 1, 1), date(2024, 12, 31), n),
    })
    size = np.array([SIZE_MEDIAN[t] for t in types]) * rng.lognormal(0, [SIZE_SIGMA[t] for t in types])
    ctx.aux["loc_size"] = pd.Series(size, index=df["location_id"])
    return df


def gen_sku(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("sku")
    a = ctx.cfg["assortment"]
    n = int(ctx.cfg.volumes["skus"])
    n_comp = int(round(n * a["component_share"]))
    n_fin = n - n_comp
    n_make = int(round(n_fin * a["make_share_of_finished"]))
    kind = np.array(["COMPONENT"] * n_comp + ["MAKE"] * n_make + ["BUY"] * (n_fin - n_make), dtype=object)
    rng.shuffle(kind)
    fin = kind != "COMPONENT"
    sku_type = np.where(fin, "FINISHED", "COMPONENT")
    sourcing = np.where(kind == "MAKE", "MAKE", "BUY")
    shares = ctx.cfg["demand"]["abc_share"]
    abc = np.empty(n, dtype=object)
    abc[fin] = exact_split(rng, int(fin.sum()), shares)
    abc[~fin] = exact_split(rng, int((~fin).sum()), shares)
    category = rng.choice(CATEGORIES, size=n, p=[0.16, 0.16, 0.12, 0.14, 0.13, 0.11, 0.08, 0.10])
    crit_p = {"A": [0.5, 0.4, 0.1], "B": [0.25, 0.5, 0.25], "C": [0.1, 0.4, 0.5]}
    criticality = np.array([rng.choice(["HIGH", "MEDIUM", "LOW"], p=crit_p[c]) for c in abc], dtype=object)
    lifecycle = rng.choice(["NEW", "ACTIVE", "PHASE_OUT"], size=n, p=[0.08, 0.85, 0.07])
    eff_from = np.where(lifecycle == "NEW",
                        rand_dates(rng, date(2025, 6, 1), date(2025, 12, 1), n),
                        rand_dates(rng, date(2016, 1, 1), date(2024, 12, 31), n))
    eff_to_dates = rand_dates(rng, date(2027, 1, 31), date(2027, 6, 30), n)
    eff_to = [e if lc == "PHASE_OUT" else None for e, lc in zip(eff_to_dates, lifecycle)]
    base_cost = np.array([CATEGORY_COST[c] for c in category], dtype=float)
    cost = base_cost * rng.lognormal(0, 0.45, n) * np.where(fin, 1.0, 0.25)
    cost = np.maximum(1.0, np.round(cost, 2))
    price = np.round(cost * rng.uniform(1.25, 1.9, n), 2)
    price = np.maximum(price, cost + 0.01)
    case_pack = rng.choice([1, 6, 12, 24], size=n, p=[0.2, 0.3, 0.3, 0.2])
    ssw = np.select([abc == "A", abc == "B"], [2, rng.integers(1, 3, n)], 1)
    ssw = np.clip(ssw + (criticality == "HIGH"), 1, 3)
    names = []
    for i in range(n):
        if fin[i]:
            names.append(f"{rng.choice(BRANDS)} {rng.choice(CATEGORY_NOUNS[category[i]])} {rng.choice(SIZES)}")
        else:
            names.append(f"{rng.choice(COMPONENT_NOUNS)} {rng.choice(['Type A', 'Type B', 'Type C', 'Grade 1', 'Grade 2'])}")
    df = pd.DataFrame({
        "sku_id": [ids.sku_id(i + 1) for i in range(n)], "sku_name": names, "category": category,
        "sku_type": sku_type, "sourcing_type": sourcing, "base_uom": "EA", "case_pack_qty": case_pack,
        "unit_cost_inr": cost, "unit_price_inr": price, "abc_class": abc, "criticality": criticality,
        "lifecycle_state": lifecycle, "effective_from": eff_from, "effective_to": eff_to,
        "safety_stock_weeks": ssw.astype(int),
    })
    # Latent weekly rate per location of size 1 (never exported): lognormal by ABC class.
    med = {"A": 150.0, "B": 25.0, "C": 5.0}
    rate = np.array([med[c] for c in abc]) * rng.lognormal(0, 0.5, n)
    rate *= np.select([lifecycle == "PHASE_OUT", lifecycle == "NEW"], [0.6, 0.8], 1.0)
    ctx.aux["sku_rate"] = pd.Series(rate, index=df["sku_id"])
    return df


def gen_supplier(ctx: Ctx) -> pd.DataFrame:
    rng = ctx.rngs("supplier")
    n = int(ctx.cfg.volumes["suppliers"])
    combos = [(p, c, s) for p in SUP_PREFIX for c in SUP_CORE for s in SUP_SUFFIX]
    pick = rng.choice(len(combos), size=n, replace=n > len(combos))
    names = [f"{combos[k][0]} {combos[k][1]} {combos[k][2]}" for k in pick]
    status = rng.choice(["ACTIVE", "ON_HOLD", "INACTIVE"], size=n, p=[0.90, 0.06, 0.04])
    a, b = ctx.cfg["supply"]["on_time_beta"]
    on_time = np.clip(rng.beta(a, b, n), 0.5, 0.995)
    eff_to_dates = rand_dates(rng, date(2025, 1, 1), date(2025, 11, 30), n)
    df = pd.DataFrame({
        "supplier_id": [ids.supplier_id(i + 1) for i in range(n)], "supplier_name": names,
        "home_region": rng.choice(REGIONS, size=n), "status": status, "on_time_rate": np.round(on_time, 4),
        "effective_from": rand_dates(rng, date(2010, 1, 1), date(2023, 12, 31), n),
        "effective_to": [e if s == "INACTIVE" else None for e, s in zip(eff_to_dates, status)],
    })
    share = ctx.cfg["supply"]["constrained_supplier_share"]
    n_con = int(round(n * share))
    constrained = np.zeros(n, dtype=bool)
    constrained[rng.choice(n, size=n_con, replace=False)] = True
    ctx.aux["constrained"] = pd.Series(constrained, index=df["supplier_id"])   # latent, never exported
    ctx.aux["supplier_weight"] = pd.Series(rng.lognormal(0, 1.0, n), index=df["supplier_id"])
    ctx.aux["importer"] = pd.Series(rng.random(n) < 0.15, index=df["supplier_id"])
    return df
