# Progress

## Status
| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold | ✅ done |
| 1 | Schema & docs | ✅ done |
| 2 | Generator | ✅ done (test + starter) |
| 3 | Planning | ✅ done (test + starter) |
| 4 | Mitigations | ✅ done (test + starter) |
| 5 | Edge cases & ground truth | ✅ done (test + starter) |
| 6 | Validate, export, repro, full scale | ⏳ next |
| 7 | README & polish | — |

## Phase 0 — done
- `pyproject.toml` (src layout), `pip install -e ".[dev]"` works.
- `src/planner/`: `config.py`, `ids.py` (formatters + regexes), `rng.py` (name-keyed streams), `calendar.py` (ISO weeks), CLI stub `__main__.py` with all 8 commands.
- `config/test.yaml`, `starter.yaml`, `full.yaml`.
- `CLAUDE.md`, `PROGRESS.md`, `docs/decisions.md`, `LICENSE`.
- Tests: 14 passing (ids, calendar, rng, config, CLI).

## Phase 1 — done
- `src/planner/schema.py`: 24 tables (11 core entities + bridges, events, planner outputs, ground truth) with dtype, required, allowed values, unit, unique, rule, description, PK, natural key, FKs (incl. composite FKs to `sku_location`), polymorphic refs, lifecycles.
- `python -m planner docs` writes `docs/data_dictionary.md`, `sql/schema.sql` (SQLite DDL with PK/FK/UNIQUE/CHECK), `docs/erd.md` (Mermaid erDiagram, cardinality tables incl. N:M bridges and 1:0..1, effective-dated entities, lifecycle state diagrams).
- Tests: 69 passing (adds schema completeness, FK ordering, SQLite load with FK + CHECK enforcement, deterministic docs, committed docs up to date).

## Phase 2 — done (test + starter; full scale deferred to Phase 6)
- `python -m planner generate --scale {test,starter} [--seed N] [--out DIR]` builds Section 9 steps 1–7 in dependency order and writes `data/<scale>/csv/*.csv` (+ `run_info.json`).
- Modules: `generate/{master,bridges,supply,demand,orders,inventory,production}.py`, schema-driven writer/reader `tableio.py` (PK sort, fixed rounding, `\n`, gzip mtime=0).
- `validate/checks.py`: generic schema checks (PK, required, enum, ID regex, bounds, FK incl. composite, natural keys, effective dates) + domain checks (ON_HAND = ledger, ledger signs, receipts = received POs, PO qty/UoM/MOQ, sourcing approval/effectivity, receipt-week arithmetic, PO lifecycle, firm load ≤ capacity, IN_TRANSIT mirrors open POs, lead-time arithmetic and version overlap, demand derivations, safety stock derivation, production reconciliation).
- Starter: 146 checks, 0 failures; SQLite load with `PRAGMA foreign_keys=ON` has 0 FK violations; generate takes ~0.3 s.
- Starter profile: A-class = 78% of volume; PROMO 3.0% of weeks; delay rate 8.0%; PO status 20% RECEIVED / 4% CANCELLED; firm PO load median 17% of capacity (after Phase 3 seasonal capacity sizing).
- Tests: 85 passing (adds generated-data checks, mutation tests proving each check catches its corruption, byte-identical reproducibility and seed sensitivity).

## Phase 3 — done (test + starter)
- `python -m planner plan --scale {test,starter} [--data DIR]` runs quality gate -> PAB + MRP -> shortages and writes `exception`, `planned_order`, `projected_balance`, `production_plan` (FIRM + PLANNED), `shortage`, `shortage_cause`, `shortage_event` into `data/<scale>/csv/`.
- Modules: `plan/quality_gate.py` (UOM_MISMATCH, LEAD_TIME_IGNORED, DUPLICATE_INBOUND, SUPPLIER_CAPACITY_EXCEEDED with documented actions), `plan/balance.py` (PAB + reconciliation), `plan/replenish.py` (integrated weekly MRP: lead-time fence, lane version at release, MOQ/multiple, priority capacity allocation with pre-build, BOM dependent demand), `plan/production.py` (plant capacity, proportional component cuts), `plan/shortages.py` (runs, 7 rule-based causes with evidence, explanation sentence, priority, OPEN event).
- `validate/plan_checks.py`: PAB reconciles, PAB ties out to inputs, shortages match PAB, planned-order rules, capacity ledger >= 0, production/BOM, cause evidence resolves, explanations, shortage lifecycle. Starter: 11 checks, 0 failures; generic PK/FK/enum/ID checks also pass on all output tables.
- Starter (after realism tuning): 1,777 SKU-locations, 205 short (11.5%), 245 shortages (66 STOCKOUT, 179 BELOW_SAFETY_STOCK); episode length median 1 wk, max 5 wk, none caused by a permanent constraint; primary causes SUPPLIER_CAPACITY 76 (31%), DEMAND_SPIKE 50, LEAD_TIME_FENCE 48, PRODUCTION_CAPACITY 40, LOW_OPENING_STOCK 16, PO_DELAYED 15; COMPONENT_SHORTAGE appears as a contributing cause on 3 shortages (never primary); top 10 has no SKU more than twice; 0 exceptions on clean baseline. Test scale: 10.7% short.
- Realism tuning: temporary supplier disruptions (10% of suppliers, 4-8 wk at 0.5-0.7x) replace permanent 0.6-0.9x constraints; normal headroom 1.15-1.3 on forecast-level-corrected seasonal load; opening_cover_bias 1.5 (noise 0.20); just-in-time opening stock behind open delayed POs; component POs 8x delay weight; PLANNED production build-ahead 1 week; LOW_OPENING_STOCK threshold includes SS (see decisions).
- Tests: 108 passing (adds PAB reconciliation + independent recompute + corruption detection, shortage/cause/explanation checks, planned-order policy, capacity ledger, production/BOM, determinism, quality-gate mutation tests per issue type, boundary no-exception test, no ground-truth reference in plan/ or validate/).

## Phase 4 — done (test + starter)
- `plan` now also writes `mitigation_option`, `mitigation_ledger` and the full `shortage_event` history; `shortage.status` = last event.
- Modules: `plan/mitigations.py` (option generators + capacity ledger in priority-rank order, backfilled stock draws), `plan/decide.py` (policy, score, approval, recommendation, lifecycle events, UNAPPROVED_SUBSTITUTE exceptions incl. MANUAL proposals).
- `validate/plan_checks.py`: mitigation_option_rules, mitigation_policy (recomputed from master data), mitigation_capacity_ledger (supplier / plant / stock recomputed from written tables), mitigation_coverage_recomputes, decision_matches_recommendation. Starter: 16 plan checks, 0 failures; generic PK/FK/enum/ID checks pass on the new tables.
- Starter: 1,019 options for 245 shortages (TRANSFER 615, BACKORDER 245, ALTERNATE_SUPPLIER 68, INCREASE_PRODUCTION 58, SUBSTITUTE_SKU 21, EXPEDITE_PO 12; 29 VIOLATION alternatives). Recommended: TRANSFER 125, INCREASE_PRODUCTION 56, BACKORDER 42, ALTERNATE_SUPPLIER 10, SUBSTITUTE_SKU 7, EXPEDITE_PO 5. 82.9% get a COMPLIANT non-backorder recommendation, 63.7% fully covered; cost INR 246,101.92 (238,666.92 non-backorder); 177 SELECTED, 26 ESCALATED, 42 BACKORDER_ACCEPTED; fill rate 99.9833% -> 99.9931%.
- Tests: 121 passing (adds tests/test_mitigations.py: checks pass, one recommendation per shortage, no recommended VIOLATION, ledger never negative, alternatives differ in cost/coverage, fill rate does not drop, mutation tests for recommended violation and ledger overbooking, MANUAL unapproved substitute flagged, draws keep SS).

## Phase 5 — done (test + starter)
- `generate` now ends with step 8 (`planner.inject`): boundary cases and anomalies on the baseline, a planning pass, MANUAL proposals (`mitigation_proposal`) for real shortages, and `ground_truth/<scale>/ground_truth.csv` (gt_id, case_class, issue_type, entity, record_id, related_record_ids, injection_rule, expected_detection_rule, expected_action, expected_outcome).
- Modules: `inject/{__init__,common,boundaries,anomalies}.py`, `evaluate.py` (only ground-truth reader) -> `reports/metrics.md` + `metrics.json`; CLI `python -m planner evaluate --scale starter`.
- Starter: 10 injected per anomaly type and 5 per boundary type (65 labels). Detection precision = recall = F1 = 1.0 for all 5 types, expected action 10/10 each, 0 boundary false positives, 20/20 machine-checkable expected outcomes hold. Test scale: 3 per type, same result.
- Starter after injection: 11.71% SKU-locations short, 233 shortages; 79.8% COMPLIANT non-backorder recommendations; cost INR 244,068.55; 25 escalated; fill rate 99.9839% -> 99.9926%; 0 recommended VIOLATION.
- All plan checks, generic PK/FK/enum/ID checks and the mitigation ledger pass on the injected data; raw input domain checks fail only on the injected records (po_qty_uom_moq, po_receipt_week_arithmetic, firm_po_within_supplier_capacity, in_transit_mirrors_open_pos) — Phase 6 reports those as expected failures.
- Tests: 133 passing (adds tests/test_inject_evaluate.py: label counts, recall/precision 1.0, action as expected, 0 boundary FP, outcomes, only targeted records change, only expected raw checks fail, outputs valid on injected data, MANUAL proposals are VIOLATIONs, ground-truth isolation). Output in `reports/test_results.txt`.

## Next
- Phase 6: full validation suite (`validate` -> reports/validation_summary.{md,json} with expected vs unexpected failures, SQLite FK check), exports (CSV/JSON/SQL seed), manifest + checksums, repro-check, data profile + figures; then full scale.

## Open issues
- Remaining backorders are mostly W01-W03 shortages inside the lead-time fence and MAKE shortages without spare plant capacity before the run end.
- One recommended option per shortage; combinations (two transfers) are not proposed.
- Headroom is 1.15-1.3, above the requested 1.0-1.15 (see Phase 3 decisions).
- At starter the PO_QTY_EQUALS_MOQ picks are POs already at MOQ (label-only, stated in the label).
- Full scale not run yet (Phase 6); `generate` now plans twice in memory (about 4 s at starter).
