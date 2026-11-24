"""Decisions on mitigation options (MASTER_PROMPT 10.7, 10.6, 10.1 UNAPPROVED_SUBSTITUTE).

For every option (SYSTEM or MANUAL):
- policy_status: COMPLIANT unless a policy rule is broken (reason recorded):
    EXPEDITE_PO         the AIR arrival must be on/before the shortage start week
    ALTERNATE_SUPPLIER  supplier_sku APPROVED and effective at release, supplier ACTIVE, release >= W01,
                        receipt <= end week
    SUBSTITUTE_SKU      sku_substitute APPROVED and effective in the shortage start week (else
                        UNAPPROVED_SUBSTITUTE exception, FLAGGED)
- score = w_coverage x coverage_pct + w_timeliness x timeliness - w_cost x normalised cost, where
  timeliness = 1 / (1 + weeks_late) when the option covers anything (else 0) and
  normalised cost = min(1, cost / base value); base value = value at risk, or the SS-gap value
  (below_ss_qty x unit price) for BELOW_SAFETY_STOCK shortages whose value at risk is 0.
- requires_approval = cost > approval_cost_threshold_inr or cost > approval_var_share x base value.
Recommendation: the highest-scoring COMPLIANT non-backorder option that covers something (ties: lower cost,
then option_seq); if there is none, BACKORDER. A VIOLATION is never recommended.
Lifecycle: OPEN -> MITIGATION_PROPOSED -> MITIGATION_SELECTED | ESCALATED (needs OPERATIONS_MANAGER approval)
| BACKORDER_ACCEPTED.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from planner import calendar as cal
from planner import ids

from .replenish import _effective_on

UNAPPROVED_RULE = ("mitigation option uses a sku_substitute whose approval_status != APPROVED or that is not "
                   "effective in the shortage start week")


def _wk(i: int) -> str:
    return cal.week_label(i + 1)


def substitute_ok(ctx, sh, sub_id: str) -> str | None:
    """None if the substitute may be used for this shortage, else the reason."""
    r = ctx.sub_rec.get(sub_id)
    if r is None:
        return f"substitute {sub_id} does not exist"
    if r["approval_status"] != "APPROVED":
        return f"UNAPPROVED_SUBSTITUTE: {sub_id} approval_status {r['approval_status']}"
    if not _effective_on(r["effective_from"], r["effective_to"], ctx.week_start[sh.s]):
        return f"UNAPPROVED_SUBSTITUTE: {sub_id} not effective in {_wk(sh.s)}"
    return None


def _policy_reason(ctx, sh, o: dict) -> str | None:
    t = o["option_type"]
    if t == "EXPEDITE_PO":
        if o["_arrival"] > sh.s:
            return f"AIR arrival {_wk(o['_arrival'])} is after the shortage start {_wk(sh.s)}"
    elif t == "ALTERNATE_SUPPLIER":
        r = ctx.ss_rec[o["_supplier_sku_id"]]
        why = []
        if r["approval_status"] != "APPROVED":
            why.append(f"supplier_sku {o['_supplier_sku_id']} is {r['approval_status']}")
        if not _effective_on(r["effective_from"], r["effective_to"], ctx.week_start[o["_release"]]):
            why.append(f"supplier_sku {o['_supplier_sku_id']} not effective at release {_wk(o['_release'])}")
        status = ctx.sup_status.get(o["ref_supplier_id"])
        if status != "ACTIVE":
            why.append(f"supplier {o['ref_supplier_id']} is {status}")
        if o["_release"] < 0 or o["_arrival"] > sh.e:
            why.append("release before W01 or receipt after the shortage end")
        if why:
            return "; ".join(why)
    elif t == "SUBSTITUTE_SKU":
        return substitute_ok(ctx, sh, o["ref_substitute_id"])
    return None


def check_policy(ctx, sh, o: dict) -> None:
    why = _policy_reason(ctx, sh, o)
    o["policy_status"] = "COMPLIANT" if why is None else "VIOLATION"
    o["policy_violation_reason"] = why


def score(ctx, sh, o: dict) -> float:
    sc = ctx.cfg["scoring"]
    cov = o["qty_covered_ea"] / sh.below if sh.below else 0.0
    timely = 1.0 / (1.0 + o["weeks_late"]) if o["qty_covered_ea"] > 0 else 0.0
    norm = min(1.0, o["incremental_cost_inr"] / sh.base_value) if sh.base_value > 0 else 0.0
    return round(float(sc["w_coverage"]) * cov + float(sc["w_timeliness"]) * timely - float(sc["w_cost"]) * norm, 4)


def needs_approval(ctx, sh, cost: float) -> bool:
    p = ctx.policy
    return bool(cost > float(p["approval_cost_threshold_inr"]) or cost > float(p["approval_var_share"]) * sh.base_value)


def recommend(ctx, sh, opts: list[dict]) -> int:
    for o in opts:
        o["coverage_pct"] = round(o["qty_covered_ea"] / sh.below, 4) if sh.below else 0.0
        o["score"] = score(ctx, sh, o)
        o["requires_approval"] = needs_approval(ctx, sh, o["incremental_cost_inr"])
        o["is_recommended"] = False
    cand = [k for k, o in enumerate(opts) if o["policy_status"] == "COMPLIANT" and o["option_type"] != "BACKORDER"
            and o["qty_covered_ea"] > 0]
    if cand:
        best = min(cand, key=lambda k: (-opts[k]["score"], opts[k]["incremental_cost_inr"], opts[k]["option_seq"]))
    else:
        best = next(k for k, o in enumerate(opts) if o["option_type"] == "BACKORDER")
    opts[best]["is_recommended"] = True
    return best


def manual_option(ctx, L, sh, p) -> dict | None:
    """A MANUAL planner proposal (mitigation_proposal row) turned into an option. Only SUBSTITUTE_SKU proposals
    exist; one that may be used is sized against the ledger, an unusable one is costed as proposed."""
    from .mitigations import substitute_option
    if p.option_type != "SUBSTITUTE_SKU" or p.ref_substitute_id not in ctx.sub_rec:
        return None
    usable = substitute_ok(ctx, sh, p.ref_substitute_id) is None
    o = substitute_option(ctx, L if usable else None, sh, p.ref_substitute_id, int(p.qty_proposed_ea))
    if o is None:
        return None
    o.update({"source": "MANUAL", "created_by_role": p.created_by_role, "option_seq": int(p.option_seq)})
    if not usable:
        o["_effects"] = []          # never booked: it cannot be recommended
    return o


def events(sh_df: pd.DataFrame, decisions: list[tuple[str, dict]], as_of) -> pd.DataFrame:
    t0 = datetime.combine(as_of, time(6, 0))
    ts = [(t0 + timedelta(minutes=10 * k)).isoformat(timespec="seconds") for k in range(3)]
    chosen = dict(decisions)
    rows = []
    for sid in sorted(sh_df["shortage_id"]):
        rows.append((sid, 1, None, "OPEN", ts[0], "PLANNER", "PAB_BELOW_SAFETY_STOCK", None))
        o = chosen.get(sid)
        if o is None:
            continue
        rows.append((sid, 2, "OPEN", "MITIGATION_PROPOSED", ts[1], "PLANNER", "OPTIONS_GENERATED", None))
        if o["option_type"] == "BACKORDER":
            final = ("BACKORDER_ACCEPTED", "NO_FEASIBLE_OPTION")
        elif o["requires_approval"]:
            final = ("ESCALATED", "APPROVAL_REQUIRED")
        else:
            final = ("MITIGATION_SELECTED", "BEST_COMPLIANT_OPTION")
        rows.append((sid, 3, "MITIGATION_PROPOSED", final[0], ts[2], "PLANNER", final[1], o["mitigation_id"]))
    ev = pd.DataFrame(rows, columns=["shortage_id", "seq", "from_status", "to_status", "event_ts", "actor_role",
                                     "reason_code", "mitigation_id"])
    ev.insert(0, "event_id", [ids.shortage_event_id(k + 1) for k in range(len(ev))])
    return ev


def exceptions(opts: pd.DataFrame) -> pd.DataFrame:
    cols = ["issue_type", "entity", "record_id", "related_record_ids", "detection_rule", "action_taken",
            "severity", "detail"]
    if len(opts) == 0:
        return pd.DataFrame(columns=cols)
    bad = opts[(opts["option_type"] == "SUBSTITUTE_SKU")
               & opts["policy_violation_reason"].fillna("").str.startswith("UNAPPROVED_SUBSTITUTE")]
    rows = [{"issue_type": "UNAPPROVED_SUBSTITUTE", "entity": "mitigation_option", "record_id": r.mitigation_id,
             "related_record_ids": f"{r.ref_substitute_id};{r.shortage_id}", "detection_rule": UNAPPROVED_RULE,
             "action_taken": "FLAGGED", "severity": "MEDIUM",
             "detail": f"{r.source} option marked VIOLATION and not recommended: {r.policy_violation_reason}"}
            for r in bad.sort_values("mitigation_id").itertuples(index=False)]
    return pd.DataFrame(rows, columns=cols)
