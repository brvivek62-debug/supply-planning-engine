# Business rules

Every rule the generator and planner apply, numbered, with the actor who **owns** it (the person accountable
for the rule in a real organisation), the code that **enforces** it and the check that **verifies** it.
Check names are the rows of `reports/validation_summary.md` (run `python -m planner validate`).

Actors (from the brief, Section 2): **Supplier**, **Planner**, **Carrier / warehouse operator**,
**Logistics analyst**, **Operations manager**. The same roles appear in the data as
`purchase_order_event.actor_role` (PLANNER, SUPPLIER, CARRIER, WAREHOUSE_OPERATOR, LOGISTICS_ANALYST) and
`shortage_event.actor_role` (PLANNER, OPERATIONS_MANAGER).

## Owner summary

| Actor | Owns |
|---|---|
| Supplier | Supplier terms (approval, MOQ, multiple, UoM conversion), capacity, PO confirmation and dispatch: BR-07–BR-10, BR-20, BR-22, BR-34 |
| Planner | Master data and assortment, demand, PAB, MRP, production, shortages, causes, priority, option generation, recommendation: BR-01–BR-06, BR-11, BR-14, BR-15, BR-19, BR-23–BR-31, BR-33, BR-36–BR-42 |
| Carrier / warehouse operator | Shipment events, receipts, inventory ledger, in-transit records: BR-16–BR-18, BR-21, BR-32 |
| Logistics analyst | Lanes and lead times, delay clearance, expedite lanes, transfers: BR-12, BR-13, BR-35, BR-43, BR-44 |
| Operations manager | Substitute approval, overtime production, approval thresholds and escalations, backorder sign-off: BR-45–BR-49 |

## 1. Master data and assortment

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-01 | IDs follow the fixed patterns (`SKU-000001`, `PO-2026-000001`, `SHORT-2026-000001`, `MIT-<shortage>-01`, …), are zero-padded, never reused; injected records continue the counter | Planner | `src/planner/ids.py` | `id_format` (every table), `tests/test_ids.py` |
| BR-02 | `unit_price_inr` > `unit_cost_inr`; COMPONENT SKUs are BUY and stocked only at plants; MAKE finished SKUs are stocked only at their home plant | Planner | `generate/master.py`, `generate/bridges.py` | `assortment_and_price_rules` |
| BR-03 | A BUY finished SKU is stocked at 6–10 DC/STORE locations; every demand, inventory, PO, plan and shortage row refers to a stocked SKU-location | Planner | `generate/bridges.py`, composite FKs in `schema.py` | `fk_integrity` on `(sku_id, location_id) → sku_location`, `sqlite_fk_check` |
| BR-04 | `safety_stock_qty` = round(safety_stock_weeks × mean weekly forecast); components use dependent demand (parent forecast × qty_per) | Planner | `generate/demand.py` | `safety_stock_derived` |
| BR-05 | `effective_from` ≤ `effective_to` on every effective-dated row; lane versions for one supplier/region/lane type never overlap | Planner | `schema.py`, `generate/supply.py` | `effective_date_order`, `lead_time_versions_disjoint` |
| BR-06 | A substitute is in the same category and never the SKU itself; a BOM links a MAKE parent to 2–4 COMPONENT SKUs | Planner | `generate/bridges.py` | `bridge_rules` |
| BR-07 | Each BUY SKU has exactly one primary supplier line and 0–2 alternates | Supplier | `generate/bridges.py` | `bridge_rules` |
| BR-08 | `uom_conversion` = case_pack_qty when `purchase_uom` = CASE, else 1; MOQ is a multiple of the order multiple | Supplier | `generate/bridges.py` | `bridge_rules`, `po_qty_uom_moq` |

## 2. Supply, demand and inventory inputs

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-09 | Supplier capacity per receipt week = seasonal expected load × headroom 1.15–1.3 × ±8% noise; ~10% of suppliers get one temporary 4–8 week disruption at 0.5–0.7× (no flag stored) | Supplier | `generate/supply.py` | `reports/data_profile.md` (utilisation) |
| BR-10 | Firm PO load per supplier per receipt week ≤ `capacity_qty_ea` | Supplier | `generate/orders.py` | `firm_po_within_supplier_capacity` |
| BR-11 | Forecast exists for FINISHED SKUs only; `demand_id` = `DEM-<sku>-<loc>-2026Wnn`; `demand_driver` PROMO ⇔ promo lift, SEASONAL_PEAK ⇔ seasonal factor ≥ 1.25; one row per SKU-location-week | Planner | `generate/demand.py` | `demand_derivations`, `duplicate_natural_key` |
| BR-12 | `total_lead_weeks` = ceil((production_days + transit_days) / 7); every supplier has one standard lane and one AIR expedite lane per region | Logistics analyst | `generate/supply.py` | `lead_time_arithmetic` |
| BR-13 | ~5% of lanes change lead time from 2026W27 (two versioned rows); planner uses the row effective at release | Logistics analyst | `generate/supply.py`, `plan/replenish.py` | `lead_time_versions_disjoint`, `planned_order_rules` |
| BR-14 | A PO comes from an APPROVED supplier line effective on its order date, on that supplier's lane to the ship-to region | Planner | `generate/orders.py` | `po_sourcing_valid` |
| BR-15 | `qty_base_ea` = order_qty × uom_conversion; order_qty ≥ MOQ and a multiple of the order multiple | Planner | `generate/orders.py` | `po_qty_uom_moq` |
| BR-16 | `expected_receipt_week` = order week + total_lead_weeks; `revised_receipt_week` = expected + delay_weeks; order date < receipt | Carrier / warehouse operator | `generate/orders.py` | `po_receipt_week_arithmetic` |
| BR-17 | Inventory ledger is append-only (OPENING, RECEIPT, ISSUE, ADJUSTMENT) between 2025-09-01 and the as-of date; RECEIPT rows = received POs; signs follow txn_type | Carrier / warehouse operator | `generate/inventory.py` | `ledger_sign_and_ref`, `receipts_match_received_pos`, `ledger_dates_in_window` |
| BR-18 | ON_HAND = sum of the ledger per SKU-location | Carrier / warehouse operator | `generate/inventory.py` | `on_hand_equals_ledger` |
| BR-19 | FIRM production (W01–W04) fits plant capacity; requested − capacity cut = planned | Planner | `generate/production.py` | `production_reconciles_and_fits` |

## 3. PO lifecycle (event history)

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-20 | CREATED (Planner) → CONFIRMED → SHIPPED (Supplier); CREATED/CONFIRMED → CANCELLED (Planner or Supplier) | Supplier | `generate/orders.py` | `lifecycle_transitions` |
| BR-21 | SHIPPED → IN_TRANSIT → RECEIVED (Carrier, then Warehouse operator); IN_TRANSIT → DELAYED (Carrier) | Carrier / warehouse operator | `generate/orders.py` | `lifecycle_transitions` |
| BR-22 | `status` = last event's `to_status`; event timestamps increase per PO; no event after the as-of date 2025-12-29 | Supplier | `generate/orders.py` | `status_equals_last_event`, `event_ts_increasing`, `no_event_after_as_of` |

## 4. Planning (PAB, MRP, production)

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-23 | opening(W01) = ON_HAND; opening(w) = closing(w−1); closing = opening + firm + planned + production receipts − demand (backlog semantics, integer EA) | Planner | `plan/balance.py` | `pab_reconciles`, `pab_inputs_tie_out` |
| BR-24 | Firm receipts come from open, non-cancelled POs at their revised receipt week, after quality-gate corrections | Planner | `plan/balance.py`, `plan/quality_gate.py` | `pab_inputs_tie_out` |
| BR-25 | When projected closing < safety stock, raise a planned order for the SS gap + 4 weeks of demand, rounded up to MOQ and multiple in purchase UoM, on the primary supplier's standard lane | Planner | `plan/replenish.py` | `planned_order_rules` |
| BR-26 | Lead-time fence: release week = receipt − lead time; no planned order if release would be before W01 | Planner | `plan/replenish.py` | `planned_order_rules` |
| BR-27 | Supplier capacity is allocated per receipt week in priority order (criticality, ABC, sku_id); MRP may pull a receipt up to 8 weeks earlier into spare capacity; the rest is `capacity_cut_ea` | Planner | `plan/replenish.py` | `capacity_ledger_non_negative` |
| BR-28 | MAKE SKUs: PLANNED production covers net requirements within plant capacity, plus up to 1 week build-ahead; component demand = production × qty_per; a component shortage cuts the parent proportionally | Planner | `plan/production.py`, `plan/replenish.py` | `bom_dependent_demand` |

## 5. Quality gate (exception path)

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-29 | Every quality-gate hit is written to `exception` with issue_type, record_id, related records, detection rule and action | Planner | `plan/quality_gate.py` | `reports/metrics.md` precision/recall |
| BR-30 | Planner reads only `data/`, never `ground_truth/` | Planner | `plan/`, `validate/` | `tests/test_plan.py::test_plan_and_validate_never_reference_ground_truth` |
| BR-31 | UOM_MISMATCH: qty_base_ea ≠ order_qty × conversion → CORRECTED with the conversion | Planner | `plan/quality_gate.py` | `po_qty_uom_moq` (EXPECTED_FAIL, explained) |
| BR-32 | DUPLICATE_INBOUND: > 1 IN_TRANSIT row per PO or qty ≠ PO qty → receipt counted once from the PO, duplicate EXCLUDED | Carrier / warehouse operator | `plan/quality_gate.py` | `in_transit_mirrors_open_pos` (EXPECTED_FAIL, explained) |
| BR-33 | LEAD_TIME_IGNORED: expected receipt < order week + lane lead time → receipt week CORRECTED, PAB recomputed | Planner | `plan/quality_gate.py` | `po_receipt_week_arithmetic` (EXPECTED_FAIL, explained) |
| BR-34 | SUPPLIER_CAPACITY_EXCEEDED: firm PO load > supplier-week capacity → overflow pushed to the next week with spare capacity (CAPPED) | Supplier | `plan/quality_gate.py` | `firm_po_within_supplier_capacity` (EXPECTED_FAIL, explained) |

## 6. Shortages

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-35 | A delayed PO counts as PO_DELAYED evidence when it was due in or before the run and lands in or after it; delays are cleared by the Logistics analyst (DELAYED → IN_TRANSIT) | Logistics analyst | `generate/orders.py`, `plan/shortages.py` | `cause_evidence_resolves` |
| BR-36 | A shortage is a contiguous run of weeks with closing < SS; STOCKOUT if closing < 0 anywhere in the run, else BELOW_SAFETY_STOCK; shortage_qty = max(0, −min closing); VaR = shortage_qty × unit price; numbered by (start_week, sku, location) | Planner | `plan/shortages.py` | `shortages_match_pab` |
| BR-37 | Each shortage has ≥ 1 rule-based cause (DEMAND_SPIKE, PO_DELAYED, SUPPLIER_CAPACITY, LEAD_TIME_FENCE, LOW_OPENING_STOCK, COMPONENT_SHORTAGE, PRODUCTION_CAPACITY) pointing at a real evidence record | Planner | `plan/shortages.py` | `every_shortage_explained`, `cause_evidence_resolves` |
| BR-38 | `explanation` is one sentence naming quantities, weeks and evidence IDs | Planner | `plan/shortages.py` | `explanation_names_ids` |
| BR-39 | priority_score = VaR × criticality weight (3/2/1) × 1/√(1 + weeks to start); rank by score | Planner | `plan/shortages.py` | `shortages_match_pab` |
| BR-40 | Shortage lifecycle: OPEN → MITIGATION_PROPOSED → MITIGATION_SELECTED / ESCALATED / BACKORDER_ACCEPTED; append-only; status = last event | Planner | `plan/shortages.py`, `plan/decide.py` | `shortage_lifecycle` |

## 7. Mitigations and decisions

| # | Rule | Owner | Enforced in | Verified by |
|---|---|---|---|---|
| BR-41 | Shortages are mitigated in priority-rank order against a capacity ledger (supplier capacity, plant capacity, stock); capacity used by one shortage is not available to the next; the ledger never goes negative | Planner | `plan/mitigations.py` | `mitigation_capacity_ledger` |
| BR-42 | Every option records coverage_pct, weeks_late, cost, fill_rate_after and score = 0.6 coverage + 0.25 timeliness − 0.15 normalised cost; coverage ≤ requirement; every alternative is kept | Planner | `plan/mitigations.py`, `plan/decide.py` | `mitigation_option_rules`, `mitigation_coverage_recomputes` |
| BR-43 | EXPEDITE_PO: an open PO not yet SHIPPED switches to the AIR lane and must arrive by the shortage start; cost = qty × air premium | Logistics analyst | `plan/mitigations.py`, `plan/decide.py` | `mitigation_policy` |
| BR-44 | TRANSFER: the source location stays ≥ SS in every week after the draw (or until its backfill order lands); 1 week same region, 2 weeks cross region; cost by distance band | Logistics analyst | `plan/mitigations.py` | `mitigation_capacity_ledger` |
| BR-45 | ALTERNATE_SUPPLIER: line APPROVED and effective, supplier ACTIVE, release ≥ W01, arrival ≤ end week, MOQ/multiple respected, spare capacity in the ledger; otherwise VIOLATION | Operations manager | `plan/mitigations.py`, `plan/decide.py` | `mitigation_policy` |
| BR-46 | SUBSTITUTE_SKU only with an APPROVED substitute effective in the shortage week; a PENDING/REJECTED/ad-hoc substitute (incl. MANUAL planner proposals) is a VIOLATION and raises UNAPPROVED_SUBSTITUTE (FLAGGED) | Operations manager | `plan/decide.py` | `mitigation_policy`, `reports/metrics.md` |
| BR-47 | INCREASE_PRODUCTION: MAKE SKU, spare plant capacity (overtime may unfreeze FIRM weeks) and component stock above SS in time; cost = qty × overtime premium | Operations manager | `plan/mitigations.py` | `mitigation_capacity_ledger` |
| BR-48 | `requires_approval` when cost > ₹2,00,000 or > 25% of value at risk (SS-gap value when VaR = 0); the shortage is then ESCALATED for Operations manager approval | Operations manager | `plan/decide.py` | `mitigation_policy`, `decision_matches_recommendation` |
| BR-49 | Recommend the best COMPLIANT non-backorder option that covers something; never a VIOLATION; if none exists, BACKORDER (always available, cost = lost margin + service penalty) and the shortage is BACKORDER_ACCEPTED | Operations manager | `plan/decide.py` | `decision_matches_recommendation`, `tests/test_mitigations.py::test_no_recommended_violation` |
