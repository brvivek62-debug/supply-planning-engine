# Progress

## Status
| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold | ✅ done |
| 1 | Schema & docs | ✅ done |
| 2 | Generator | ✅ done (test + starter) |
| 3 | Planning | ✅ done (test + starter) |
| 4 | Mitigations | ⏳ next |
| 5 | Edge cases & ground truth | — |
| 6 | Validate, export, repro, full scale | — |
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
- Starter: 1,777 SKU-locations, 172 short (9.68%), 259 shortages (137 STOCKOUT, 122 BELOW_SAFETY_STOCK); primary causes SUPPLIER_CAPACITY 196, DEMAND_SPIKE 19, PRODUCTION_CAPACITY 19, LEAD_TIME_FENCE 14, COMPONENT_SHORTAGE 11; 14,769 planned orders; 0 exceptions on clean baseline; plan ~1.5 s. Test scale: 8.61% short.
- Generator revisited: capacity sized on seasonal expected load incl. promo uplift; `opening_cover_bias` 2.0 (see decisions).
- Tests: 108 passing (adds PAB reconciliation + independent recompute + corruption detection, shortage/cause/explanation checks, planned-order policy, capacity ledger, production/BOM, determinism, quality-gate mutation tests per issue type, boundary no-exception test, no ground-truth reference in plan/ or validate/).

## Next
- Phase 4: mitigation options (EXPEDITE_PO, ALTERNATE_SUPPLIER, TRANSFER, SUBSTITUTE_SKU, INCREASE_PRODUCTION, BACKORDER) in priority-rank order against a capacity ledger seeded from `MrpResult.supplier_remaining` / `plant_remaining` and PAB surplus; scoring, policy status, approval, recommendation; shortage_event MITIGATION_PROPOSED -> SELECTED / ESCALATED / BACKORDER_ACCEPTED.

## Open issues
- Most SUPPLIER_CAPACITY shortages come from the 4 deliberately constrained suppliers (headroom 0.6-0.9), which starve their lowest-priority SKUs for most of the year; these are the main ALTERNATE_SUPPLIER test cases for Phase 4.
- Full scale not run yet (Phase 6); the MRP loop is Python over triggered rows per week, expected to be a few seconds at full scale.
