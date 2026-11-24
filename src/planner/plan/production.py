"""Production and BOM helpers (MASTER_PROMPT 10.4). Kept deliberately simple:

* Production lead time is zero: a quantity produced in week w is available in week w.
* FIRM weeks (the first ``production.firm_weeks``) are taken from the input plan; PLANNED weeks are
  lot-for-lot up to safety stock: requested = max(0, safety stock + demand(w) - opening(w)).
* Order of cuts: component availability first, then plant capacity on the buildable quantity, so each cut
  is attributed to the constraint that actually binds.
* Plant capacity: if the requested load of a plant-week exceeds capacity, every MAKE SKU at that plant is
  scaled down by the same factor (floored to whole units) -> ``capacity_cut_ea``.
* Components: dependent demand = production x qty_per. If a component's availability in week w
  (opening + receipts) is below the requested dependent demand, every parent using it is cut to
  floor(requested x available / needed); a parent with several short components takes the tightest one
  -> ``component_cut_ea``, cause COMPONENT_SHORTAGE. Integer arithmetic guarantees consumption <= availability.
"""
from __future__ import annotations

import numpy as np


def cap_to_plant_capacity(requested: np.ndarray, make_rows: np.ndarray, plant_idx: np.ndarray,
                          plant_rem_w: np.ndarray) -> np.ndarray:
    granted = requested.copy()
    if len(make_rows) == 0:
        return granted
    p = plant_idx[make_rows]
    load = np.bincount(p, weights=requested[make_rows], minlength=len(plant_rem_w)).astype(np.int64)
    cap = np.maximum(plant_rem_w, 0)
    over = load > cap
    if over.any():
        rr = make_rows[over[p]]
        pp = plant_idx[rr]
        granted[rr] = (requested[rr] * cap[pp]) // load[pp]
    return granted


def component_limited(granted: np.ndarray, need: np.ndarray, avail: np.ndarray, bp: np.ndarray, bc: np.ndarray,
                      bq: np.ndarray, eff: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Parent production after component availability, and the index of the limiting BOM line per parent."""
    final = granted.copy()
    lim = np.full(len(granted), -1, dtype=np.int64)
    links = np.flatnonzero(eff & (granted[bp] > 0) & (bq > 0))
    for k in links:                      # BOM lines are few (2-4 per MAKE SKU); sorted by bom_id
        c, p = bc[k], bp[k]
        if need[c] > max(avail[c], 0):
            cand = (granted[p] * max(int(avail[c]), 0)) // int(need[c])
            if cand < final[p]:
                final[p], lim[p] = cand, k
    return final, lim
