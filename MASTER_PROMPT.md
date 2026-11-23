# MASTER PROMPT — ORYVA AI Assignment: Demand, Inventory & Supply Planning

You are building a complete, graded technical assignment. The original brief is at
`docs/assignment/ORYVA_AI_Assignment.pdf` — read it fully before starting. Where this
prompt and the PDF disagree, the PDF wins. Deadline is tight: aim to finish all
required phases in about 12 working hours.

---

## 0. How to work

- This folder is already a git repo connected to the public GitHub repo
  `brvivek62-debug/supply-planning-engine`.
- Work **phase by phase** (Section 17). After each phase: run `pytest`, fix failures,
  update `PROGRESS.md` (what's done, what's next, any open issues), commit with a clear
  message, run `git push -u origin HEAD`, then stop and report a short summary.
- Before every commit, run `git status` and make sure nothing over 50 MB, no `.venv/`,
  and nothing from `docs/assignment/` is staged.
- In Phase 0, create `CLAUDE.md` containing: "Always follow MASTER_PROMPT.md. Current
  status is in PROGRESS.md. Design decisions are logged in docs/decisions.md."
- Don't stop to ask questions about details. Make a sensible decision, log it in
  `docs/decisions.md` (one line each: decision + reason), and keep going.
- Build the **starter scale end-to-end first**. Run the full scale only once everything
  works on starter scale.
- Never hand-edit generated data. Everything in `data/`, `ground_truth/` and `reports/`
  must come from a command.

## 1. What is being graded (design every choice around this)

| Area | What they check | How we satisfy it |
|---|---|---|
| A | Realistic distributions, meaningful variation | ABC demand skew, seasonality, promo spikes, constrained suppliers, delays, substitutes; `reports/data_profile.md` |
| B | Consistent IDs, valid joins | Exact ID patterns; PK/FK validator; SQLite FK check |
| C | Dependency & lifecycle consistency | Parent tables generated first; event tables with allowed status transitions |
| D | Correct derived values, reconciliation | All balances, statuses, dates and totals computed from source rows; reconciliation checks |
| E | Normal, boundary, failure, exception coverage | Injected anomalies, boundary cases, and an exception table with actions taken |
| F | Separate ground truth, not in input features | `ground_truth/` written only by the injector, read only by `evaluate`; a test enforces this |
| G | Fixed-seed reproducibility, docs, lawful provenance | `repro-check` (two runs, identical SHA-256), README, data dictionary, source manifest |

Required acceptance criteria from the brief:
- Projected available balance reconciles week by week.
- Every shortage has explainable causes.
- Every mitigation respects policy and available capacity.
- Alternatives show cost and service trade-offs.
- Primary keys are unique and every FK join is valid.
- Derived values reconcile; lifecycle transitions and dates are valid.

## 2. Tech stack & constraints

- Python 3.11+, `numpy`, `pandas`, `pyarrow`, `pyyaml`, `pytest`, `matplotlib` (charts
  only). Standard-library `argparse` for the CLI and `sqlite3` for the SQL load check.
- Machine: Apple M4 MacBook Air, 16 GB RAM. Vectorise with pandas/numpy. Don't loop
  over the ~1.2M demand rows in Python. Looping over shortages (a few thousand) is fine.
  Target full-scale `run-all` under 10 minutes.
- Packaging: `pyproject.toml` with a `src/` layout; installable via `pip install -e .`.
- **GitHub file limits:** no committed file over 50 MB. Full-scale large tables are
  written as `.csv.gz` (and optionally `.parquet`). If any file is still over 50 MB,
  don't commit it: commit its checksum and row count in the manifest, and tell readers
  in the README to regenerate it with the command.
- Currency INR; base unit of measure `EA`; weeks are ISO weeks of 2026.

## 3. Repository layout

The repository root is the current folder (`supply-planning-engine`). Don't create a
nested project folder.

```
supply-planning-engine/
├── README.md
├── CLAUDE.md, PROGRESS.md
├── pyproject.toml
├── config/            test.yaml, starter.yaml, full.yaml
├── src/planner/
│   ├── __main__.py    CLI
│   ├── config.py, ids.py, rng.py, calendar.py
│   ├── schema.py      single source of truth for every table (fields, types, enums, PK, FKs, rules)
│   ├── generate/      master.py, bridges.py, supply.py, demand.py, inventory.py, orders.py, production.py
│   ├── inject/        anomalies.py, boundaries.py (the ONLY code that writes ground_truth/)
│   ├── plan/          quality_gate.py, balance.py, replenish.py, production.py, shortages.py, mitigations.py, decide.py
│   ├── validate/      checks.py, report.py
│   ├── evaluate.py    the ONLY code that reads ground_truth/
│   ├── export.py, manifest.py, repro.py, docs_gen.py, profile.py
├── sql/schema.sql     generated DDL with PKs and FKs
├── data/starter/      csv/, json/, sql/seed.sql
├── data/full/         csv.gz (and/or parquet)
├── ground_truth/      starter/ground_truth.csv, full/ground_truth.csv
├── reports/           validation_summary.{md,json}, metrics.md, data_profile.md, reproducibility.md, figures/
├── docs/              data_dictionary.md, erd.md, business_rules.md, source_manifest.md, decisions.md
├── docs/assignment/   assignment PDF (git-ignored)
└── tests/
```

`.gitignore`: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `docs/assignment/`, `tmp/`.

## 4. CLI (document every command in README)

```
python -m planner generate   --scale starter --seed 20261022   # baseline + injected cases + ground truth
python -m planner plan       --scale starter                    # quality gate, PAB, shortages, mitigations, decisions
python -m planner validate   --scale starter                    # all checks -> reports/validation_summary.*
python -m planner evaluate   --scale starter                    # metrics + detection precision/recall vs ground truth
python -m planner export     --scale starter                    # CSV/JSON/SQL + manifest + checksums
python -m planner repro-check --scale starter --seed 20261022   # run twice into tmp dirs, compare row counts + SHA-256
python -m planner docs                                          # data dictionary, schema.sql, ERD from schema.py
python -m planner run-all    --scale starter --seed 20261022    # everything above in order
```

The default seed is **20261022**. Scale comes only from config, so starter and full use
the same generator code.

## 5. Config & scales (`config/*.yaml`)

| Param | test | starter | full |
|---|---|---|---|
| skus | 60 | 300 | **3,000** |
| locations | 6 | 10 | **50** |
| weeks | 52 | 52 | **52** |
| suppliers | 15 | 50 | **500** |
| purchase_orders | 200 | 1,000 | **10,000** |
| anomalies per issue type | 3 | 10 | 40 |
| boundary cases per type | 2 | 5 | 15 |

Also in config: as-of date, assortment size per SKU, promo spike rate, delay rate,
constrained-supplier share, substitute share, policy thresholds and cost parameters.
The `test` scale is for pytest and must run in a few seconds.

## 6. Determinism rules (needed for identical checksums)

- One `numpy.random.SeedSequence(seed)`. Spawn a child generator per table **by a fixed
  table name** (e.g. hash the name into the spawn key), so adding a table never shifts
  the random numbers of other tables.
- No `datetime.now()`, UUIDs, or run timestamps inside data files. The as-of date comes
  from config. Put run metadata only in a non-checksummed `run_info.json`.
- Sort every table by its primary key before writing. Use fixed float rounding
  (2 decimals for money, 4 for rates), `\n` line endings, fixed column order taken
  from `schema.py`, and `gzip` with `mtime=0`.
- Iterate over sorted collections only, never over raw sets.

## 7. IDs (immutable, zero-padded, exactly consistent across files)

| Record | Pattern | Example |
|---|---|---|
| SKU | `SKU-%06d` | SKU-000001 |
| Location | `LOC-%03d` | LOC-001 |
| Supplier | `SUP-%04d` | SUP-0001 |
| Forecast demand | `DEM-<sku_id>-<location_id>-2026W%02d` | DEM-SKU-000001-LOC-001-2026W01 |
| Purchase order | `PO-2026-%06d` | PO-2026-000001 |
| Planned order | `PLN-2026-%06d` | PLN-2026-000001 |
| Shortage | `SHORT-2026-%06d` | SHORT-2026-000001 |
| Mitigation option | `MIT-<shortage_id>-%02d` | MIT-SHORT-2026-000001-01 |
| Others | `LT-%06d`, `CAP-%07d`, `INV-%07d`, `TXN-%08d`, `POE-%08d`, `SHE-%07d`, `PROD-2026-%06d`, `BOM-%05d`, `SSUB-%05d`, `SS-%06d`, `EXC-2026-%06d`, `GT-%05d` | |

- Injected new records continue the counter (e.g. PO-2026-010001). IDs are never reused
  or renumbered.
- Shortages are numbered after sorting by (start_week, sku_id, location_id).
- `ids.py` holds the formatters plus a regex per pattern. The validator checks every ID
  column against its regex.

## 8. Data model

Define every table in `schema.py`: field name, dtype, required/optional, allowed values,
unit, unique flag, validation rule, description, PK, and FKs. Generate
`docs/data_dictionary.md`, `sql/schema.sql` and the validators from it.
`docs/erd.md` holds a Mermaid `erDiagram`, a cardinality table (1:1 / 1:N / N:M, with
optional relationships marked), and the lifecycle state machines.

Use calendar weeks as strings `2026Wnn`. Week start date is
`date.fromisocalendar(2026, n, 1)`; note that 2026W01 starts on 2025-12-29.
As-of date = **2025-12-29** (the planning run happens at the start of 2026W01).

### 8.1 Master tables
- **location**: location_id, location_name, location_type {PLANT, DC, STORE}
  (5 / 10 / 35 at full scale), city (Indian cities), region
  {NORTH, SOUTH, EAST, WEST, CENTRAL}, is_active, effective_from.
- **sku**: sku_id, sku_name, category (about 8), sku_type {FINISHED, COMPONENT},
  sourcing_type {BUY, MAKE}, base_uom {EA}, case_pack_qty {1, 6, 12, 24},
  unit_cost_inr, unit_price_inr (> cost), abc_class {A, B, C}, criticality
  {HIGH, MEDIUM, LOW}, lifecycle_state {NEW, ACTIVE, PHASE_OUT}, effective_from,
  effective_to (optional), safety_stock_weeks.
  About 15% are COMPONENT (BUY, stocked only at plants). About 10% of FINISHED are MAKE
  (stocked only at plants; made from components via BOM).
- **supplier**: supplier_id, supplier_name, home_region, status
  {ACTIVE, ON_HOLD, INACTIVE}, on_time_rate (Beta-distributed, mostly 0.75–0.98),
  effective_from, effective_to. **Do not store an "is_constrained" flag.** Constraint
  must emerge from the capacity numbers.

### 8.2 Bridge tables (many-to-many)
- **sku_location** (assortment): sku_id, location_id, safety_stock_qty (derived =
  safety_stock_weeks × mean weekly forecast, rounded), is_active, effective_from.
  Each BUY FINISHED SKU is stocked at about 6–10 DC/STORE locations. Expect about
  1.2M demand rows at full scale.
- **supplier_sku**: supplier_sku_id, supplier_id, sku_id, is_primary, approval_status
  {APPROVED, PENDING, REJECTED}, unit_cost_inr, purchase_uom {EA, CASE},
  uom_conversion (EA per purchase unit = case_pack_qty when CASE), moq (purchase uom),
  order_multiple, effective_from, effective_to. Each BUY SKU has 1 primary and 0–2
  alternate suppliers.
- **sku_substitute**: substitute_id, sku_id, substitute_sku_id (same category, never
  itself), approval_status {APPROVED, PENDING, REJECTED} (about 70% APPROVED),
  substitution_ratio, price_delta_pct, effective_from, effective_to. About 15% of
  FINISHED SKUs have substitutes.
- **bom**: bom_id, parent_sku_id (MAKE), component_sku_id (COMPONENT), qty_per, uom,
  effective_from, effective_to. 2–4 components per MAKE SKU.

### 8.3 Supply, demand and inventory
- **supplier_capacity**: capacity_id, supplier_id, week, capacity_qty_ea. This is the
  quantity deliverable per receipt week, sized from the supplier's expected load ×
  headroom: 1.1–1.6 normally, 0.6–0.9 for about 10% of suppliers (these are the
  constrained ones).
- **production_capacity**: plant_location_id, week, capacity_qty_ea.
- **lead_time**: lead_time_id, supplier_id, dest_region, transport_mode
  {ROAD, RAIL, SEA, AIR}, is_expedite_lane, production_days, transit_days,
  total_lead_weeks (derived = ceil((production_days + transit_days) / 7)),
  effective_from, effective_to.
  - Every supplier has one standard lane and one AIR expedite lane per region.
    Typical lead times: ROAD 1–3 weeks, RAIL 2–4, SEA 5–9, AIR 1–2 (at a cost premium).
  - About 5% of lanes change lead time from 2026W27. That means two versioned rows with
    effective dates, and the planner must pick the row effective at release date.
- **forecast_demand**: demand_id, sku_id, location_id, week, week_start_date,
  baseline_qty_ea, forecast_qty_ea, demand_driver {BASELINE, SEASONAL_PEAK, PROMO}.
  Generate it this way:
  - Lognormal base rate by ABC class (A ≈ 20% of SKUs, ≈ 75–80% of volume).
  - Location-size multiplier, and category seasonality (sinusoid with a category-specific
    phase).
  - Negative-binomial noise.
  - About 3% PROMO weeks at 2–4× baseline.
  - Forecast demand exists for FINISHED SKUs only. Component demand is dependent demand
    computed from production × BOM.
- **inventory_txn** (event ledger, never overwritten): txn_id, sku_id, location_id,
  txn_type {OPENING, RECEIPT, ISSUE, ADJUSTMENT}, qty_ea (signed), txn_date, ref_id
  (po_id for RECEIPT). It covers 2025-09-01 up to the as-of date:
  - one OPENING row per SKU-location
  - RECEIPT rows from received POs
  - one aggregated ISSUE row per SKU-location for the period
  - occasional cycle-count ADJUSTMENT rows
- **inventory**: inventory_id, sku_id, location_id, inventory_status
  {ON_HAND, IN_TRANSIT}, qty_ea, as_of_date, po_id (required when IN_TRANSIT).
  - ON_HAND **must equal the ledger sum** per SKU-location. Calibrate opening cover at
    roughly (lead time + safety stock weeks) ± noise, so shortages are a minority.
  - IN_TRANSIT rows mirror open POs with status SHIPPED / IN_TRANSIT / DELAYED. They are
    informational; scheduled receipts come from POs (see 10.1).
- **purchase_order**: po_id, supplier_id, sku_id, ship_to_location_id, lead_time_id,
  order_date, order_qty, order_uom, uom_conversion, qty_base_ea (derived),
  expected_receipt_week (derived = order date + lead time), delay_weeks,
  revised_receipt_week (derived), status
  {CREATED, CONFIRMED, SHIPPED, IN_TRANSIT, DELAYED, RECEIVED, CANCELLED}, created_by_role.
  - Order dates run from 2025-09 to 2026-04. About 20% are RECEIVED before as-of, about
    4% CANCELLED, and the rest are open.
  - Order quantities respect MOQ and order multiple.
  - About 8% have transport delays of 1–3 weeks.
  - The PO must come from an APPROVED supplier_sku that is effective on its order date.
- **purchase_order_event**: event_id, po_id, seq, from_status, to_status, event_ts,
  actor_role {PLANNER, SUPPLIER, CARRIER, WAREHOUSE_OPERATOR, LOGISTICS_ANALYST},
  reason_code. No event may be dated after the as-of date.
  Allowed transitions:
  - CREATED→CONFIRMED→SHIPPED→IN_TRANSIT→RECEIVED
  - IN_TRANSIT→DELAYED→IN_TRANSIT
  - CREATED/CONFIRMED→CANCELLED
  Current `status` must equal the last event's `to_status`.
- **production_plan**: prod_plan_id, sku_id, plant_location_id, week, planned_qty_ea,
  status {FIRM, PLANNED}, capacity_used_ea. The first 4 weeks are FIRM (generated
  input); later weeks are PLANNED (created by the planner).

### 8.4 Planner outputs (all derived, never randomly generated)
- **exception**: exception_id, issue_type, entity, record_id, related_record_ids,
  detection_rule, action_taken {EXCLUDED, CORRECTED, CAPPED, FLAGGED}, severity.
- **planned_order**: planned_order_id, supplier_id, sku_id, location_id, lead_time_id,
  release_week, receipt_week, order_qty, order_uom, qty_base_ea, requested_qty_ea,
  capacity_cut_ea.
- **projected_balance**: sku_id, location_id, week, opening_qty, firm_receipts,
  planned_receipts, production_receipts, demand_qty, closing_qty (= PAB),
  safety_stock_qty, balance_status {OK, BELOW_SS, STOCKOUT}.
- **shortage**: shortage_id, sku_id, location_id, start_week, end_week, peak_week,
  shortage_type {STOCKOUT, BELOW_SAFETY_STOCK}, shortage_qty_ea, below_ss_qty_ea,
  value_at_risk_inr, primary_cause, explanation, priority_score, priority_rank, status.
- **shortage_cause** (bridge): shortage_id, cause_code, evidence_type, evidence_id,
  contribution_qty_ea.
- **shortage_event**: event history, see 10.6.
- **mitigation_option**: mitigation_id, shortage_id, option_seq, option_type
  {EXPEDITE_PO, ALTERNATE_SUPPLIER, TRANSFER, SUBSTITUTE_SKU, INCREASE_PRODUCTION, BACKORDER},
  source {SYSTEM, MANUAL}, ref_po_id, ref_supplier_id, ref_from_location_id,
  ref_substitute_sku_id, ref_plant_location_id, qty_covered_ea, coverage_pct,
  arrival_week, weeks_late, incremental_cost_inr, fill_rate_after, score,
  policy_status {COMPLIANT, VIOLATION}, policy_violation_reason, requires_approval,
  is_recommended.

## 9. Generation order (dependency order, parents before children)

1. location → sku → supplier
2. sku_location, supplier_sku, sku_substitute, bom
3. lead_time, supplier_capacity, production_capacity
4. forecast_demand
5. purchase_order and purchase_order_event (status derived from the event sequence)
6. inventory_txn → inventory (ON_HAND from the ledger; IN_TRANSIT from open POs)
7. production_plan (FIRM weeks)
8. Inject boundary cases and anomalies (Section 12), then write ground truth

## 10. Planning engine (`plan`)

### 10.1 Quality gate (the exception path)
Before planning, scan the inputs with detection rules. Write every hit to `exception`
and apply the documented action:

| issue_type | Detection rule | Action |
|---|---|---|
| DUPLICATE_INBOUND | More than one IN_TRANSIT row per po_id, or IN_TRANSIT qty ≠ PO qty_base_ea | Count the receipt once (from the PO) and EXCLUDE the duplicate row |
| SUPPLIER_CAPACITY_EXCEEDED | Firm PO qty per supplier per receipt week > capacity_qty_ea | FLAG the POs; receipts above capacity are pushed to the next week with spare capacity (CAPPED) |
| LEAD_TIME_IGNORED | PO expected_receipt_week < order week + total_lead_weeks of its effective lane | CORRECT the receipt week using the lead time, then recompute PAB |
| UOM_MISMATCH | qty_base_ea ≠ order_qty × uom_conversion (or order_uom inconsistent with supplier_sku) | CORRECT using the conversion |
| UNAPPROVED_SUBSTITUTE | A mitigation option using a substitute whose approval_status ≠ APPROVED or that isn't effective in the shortage week (checked in `decide`, 10.7) | Mark VIOLATION; never recommend it |

The planner must **not read ground_truth/**. It only reads `data/`.

### 10.2 Projected available balance (backlog semantics, documented)
For each SKU-location and week:
- opening(W01) = ON_HAND from inventory
- opening(w) = closing(w−1)
- closing(w) = opening + firm_receipts + planned_receipts + production_receipts − demand_qty

Firm receipts come from open, non-cancelled POs at their revised receipt week, after the
quality-gate corrections. Compute this with groupby + cumsum. It must reconcile exactly
(integer units).

### 10.3 Replenishment (planned orders)
For BUY SKU-locations, generate planned orders like an MRP run:
1. When projected closing < safety stock, create a requirement covering about 4 weeks
   of demand.
2. Round the quantity up to MOQ and order multiple, in purchase UoM.
3. Use the primary supplier's standard lane, with the lead time effective at release.
   Release week = receipt week − lead time.
4. **If release week would be before W01, no planned order is possible (lead-time fence).**
5. Allocate supplier capacity per receipt week in priority order (criticality, then ABC,
   then sku_id). Anything that doesn't fit is `capacity_cut_ea`.
6. Iterate week by week, or use a vectorised equivalent. Keep it deterministic.

### 10.4 Production and BOM
For MAKE SKUs at plants:
- PLANNED production covers net requirements, capped by production_capacity.
- Component dependent demand = production qty × qty_per, shown as demand_qty in the
  component PAB.
- If a component goes short, cut the parent's production proportionally and record the
  cause COMPONENT_SHORTAGE.

Keep this simple and documented.

### 10.5 Shortages, causes and explanations
- A shortage is a contiguous run of weeks with closing < safety stock for one
  SKU-location.
- Its type is STOCKOUT if closing < 0 at any point in the run, otherwise
  BELOW_SAFETY_STOCK.
- shortage_qty_ea = max(0, −min closing) in the run.
- value_at_risk_inr = shortage_qty_ea × unit_price.

Causes are derived by rules. Each cause gets a `shortage_cause` row pointing to the
evidence record:

| Cause code | Rule | Evidence |
|---|---|---|
| DEMAND_SPIKE | PROMO/SEASONAL_PEAK demand ≥ 1.5× baseline in the run | demand_id |
| PO_DELAYED | A PO was due in or before the run but is delayed into or after it | po_id |
| SUPPLIER_CAPACITY | Planned order cut by capacity | planned_order_id or capacity_id |
| LEAD_TIME_FENCE | The run starts before W01 + lead time, so a new order can't arrive in time | lead_time_id |
| LOW_OPENING_STOCK | Opening cover < lead time weeks | inventory_id |
| COMPONENT_SHORTAGE | MAKE production was cut because of a component | prod_plan_id / bom_id |

`explanation` is one generated sentence naming quantities, weeks and IDs, e.g. "Stockout
of 340 EA at LOC-012 in 2026W07–W09: PROMO demand +180% in W07 (DEM-…); PO-2026-004512
delayed 2 weeks; no new order can land before W10 (LT-000231, 9-week SEA lane)."

Priority:
- priority_score = value_at_risk_inr × criticality_weight (HIGH 3, MEDIUM 2, LOW 1) ×
  urgency
- urgency = 1 / sqrt(1 + weeks from as-of to start_week)
- priority_rank orders shortages by that score.

**Calibrate** so that about 5–12% of SKU-locations have at least one shortage. Report
the actual rate in `reports/metrics.md`.

### 10.6 Shortage lifecycle
Write events to `shortage_event` (never overwrite):
- OPEN → MITIGATION_PROPOSED → MITIGATION_SELECTED
- OPEN → MITIGATION_PROPOSED → ESCALATED (needs OPERATIONS_MANAGER approval)
- OPEN → MITIGATION_PROPOSED → BACKORDER_ACCEPTED (when there's no feasible
  non-backorder option)

`shortage.status` must equal the last event.

### 10.7 Mitigations (`mitigations.py` + `decide.py`)
Process shortages in priority-rank order. Keep a **capacity ledger** so spare supplier
capacity, transferable surplus and plant capacity used by one shortage aren't available
to the next. For each shortage, generate every applicable option:

| Option | Policy / feasibility rules | Cost |
|---|---|---|
| EXPEDITE_PO | An open PO for this SKU-location not yet SHIPPED; switching to the AIR lane makes it arrive by the shortage start | qty × air premium per EA |
| ALTERNATE_SUPPLIER | supplier_sku APPROVED and effective; supplier ACTIVE; release ≥ W01 and release + lead time ≤ end_week; qty ≥ MOQ and a multiple of order_multiple; spare capacity in the ledger | qty × (alt unit cost − primary unit cost) + freight |
| TRANSFER | Another location holding this SKU keeps closing ≥ safety stock in every week after the transfer; transfer lead time is 1 week in the same region, 2 weeks across regions | qty × transfer cost per EA by distance band |
| SUBSTITUTE_SKU | sku_substitute APPROVED and effective in the shortage week; the substitute has surplus at the same location (same safety-stock test) | qty × price_delta |
| INCREASE_PRODUCTION | MAKE SKU; plant capacity and component availability in time | qty × overtime premium |
| BACKORDER | Always available | lost margin × uncovered qty + service penalty |

For each option:
- Compute `coverage_pct`, `weeks_late`, `fill_rate_after` and `incremental_cost_inr`.
- score = service gain (coverage, timeliness) − normalised cost. Document the weights.
- Set `requires_approval` = true when cost > config threshold (e.g. ₹2,00,000) or
  > 25% of value at risk.
- **Recommend** the best COMPLIANT option. Never recommend a VIOLATION.
- Keep every option as an alternative so cost vs service trade-offs are visible.

## 11. Business rules doc
Write `docs/business_rules.md` listing every rule above with its number. Map each rule
to the actor who owns it: Supplier, Planner, Carrier / warehouse operator, Logistics
analyst, Operations manager.

## 12. Edge cases, boundary cases & ground truth

Generate a valid baseline first, then inject. Only touch the targeted records, never
unrelated rows. The count per type comes from config.

| issue_type | Injection |
|---|---|
| DUPLICATE_INBOUND | Add a second IN_TRANSIT inventory row (new inventory_id) for an existing open shipped PO |
| UNAPPROVED_SUBSTITUTE | After `plan`, add MANUAL mitigation options (source = MANUAL, created by role PLANNER) proposing a PENDING/REJECTED substitute for real shortages. `decide` must flag them. (Implementation order: generate → plan → inject manual options → decide.) |
| SUPPLIER_CAPACITY_EXCEEDED | Add extra firm POs (continuing PO ids) so one supplier-week exceeds capacity |
| LEAD_TIME_IGNORED | Set expected/revised receipt week = order week on selected POs. A naive calculation would then miss a shortage; this is the brief's "shortage calculated without lead time" case |
| UOM_MISMATCH | On CASE POs, set qty_base_ea = order_qty (conversion skipped) |

Boundary cases are valid and must **not** raise an exception:
- PO qty exactly = MOQ
- a supplier-week load exactly = capacity
- a receipt exactly at order week + lead time

`ground_truth/<scale>/ground_truth.csv` has these columns: gt_id, case_class
{ANOMALY, BOUNDARY}, issue_type, entity, record_id, related_record_ids,
injection_rule, expected_detection_rule, expected_action, expected_outcome. For
example, for LEAD_TIME_IGNORED the expected outcome is "shortage SKU-…/LOC-… starting
2026Wnn".

The ground-truth file is written only by `inject/`. A test fails if any module in
`plan/` or `validate/` references the `ground_truth` path.

## 13. Validation (`validate`)

`reports/validation_summary.md` and `.json` contain one row per check: check name,
table, rows checked, failures, status. The checks are:

1. PK uniqueness (every table)
2. FK integrity (every FK, including bridges and events). Also load the starter scale
   into SQLite with `PRAGMA foreign_keys=ON` and run `PRAGMA foreign_key_check`.
3. Allowed enums
4. ID format regex
5. Date ordering:
   - effective_from ≤ effective_to
   - order_date < receipt
   - event timestamps increase per entity
   - no events after the as-of date
6. Arithmetic reconciliation:
   - PAB week by week
   - ON_HAND = ledger sum
   - qty_base_ea = order_qty × conversion
   - shortage_qty matches the PAB minimum
   - coverage ≤ shortage
   - capacity ledger never negative
7. Lifecycle transitions (PO and shortage events valid; current status = last event)
8. Duplicates (natural keys, e.g. sku + location + week in demand)
9. Policy: every recommended mitigation is COMPLIANT and within capacity

Expected failures (the injected anomalies caught by the quality gate) are reported
separately from **unexpected failures, which must be 0**.

## 14. Evaluation & reports (`evaluate`)

- `reports/metrics.md`:
  - total demand
  - fill rate before and after recommended mitigations
  - shortages by type and cause
  - % of SKU-locations short
  - top 20 shortages by priority, with explanations
  - mitigation mix
  - total and per-unit mitigation cost
  - % fully covered
  - escalations count
  - **anomaly detection precision / recall / F1 per issue_type** (exception table vs
    ground truth, matched on issue_type + record_id)
  - boundary false-positive count (target 0)
- `reports/data_profile.md` plus `reports/figures/*.png`:
  - demand share by ABC class
  - coefficient of variation (CV) distribution
  - promo spike rate
  - seasonality example
  - lead time by mode
  - supplier capacity utilisation
  - delay rate
  - PO status mix
- `reports/reproducibility.md`: a table of file → rows (run 1, run 2) → SHA-256
  (run 1, run 2) → match. Also show that a different seed changes the checksums.

## 15. Exports, manifest & docs

- `data/starter/csv/*.csv`, `data/starter/json/*.json`, `data/starter/sql/seed.sql`
  (INSERTs that respect FK order), and `sql/schema.sql` (DDL).
- `data/full/*.csv.gz`, plus the ground truth.
- `docs/source_manifest.md` and `data/<scale>/manifest.json`: every file with row count,
  SHA-256, generator command, seed, scale and package version.
- **Provenance statement:** all data is synthetic; no external datasets were used.
  Public knowledge (Indian city names, standard transport modes) was used only for
  realism. License: MIT for code.
- `README.md` sections:
  1. Overview
  2. Architecture (Mermaid flow: generate → inject → plan → decide → validate → evaluate)
  3. Quick start (exact commands: venv, `pip install -e .`, `run-all --scale starter`)
  4. Configuration and scales
  5. Generator command and fixed seed
  6. Outputs map
  7. Planning logic summary
  8. Edge cases and ground truth
  9. Validation results (headline numbers)
  10. Metrics (headline numbers)
  11. Reproducibility
  12. Assumptions and limitations
  13. Requirement → evidence checklist (each PDF requirement mapped to the file that
      proves it)

## 16. Tests (`pytest`, using the `test` scale)

- ID formats and regexes
- Same seed gives identical checksums; a different seed gives different ones
- PK/FK integrity
- Enums valid
- PAB reconciles
- ON_HAND = ledger sum
- PO current status = last event, and all transitions are valid
- Every shortage has ≥ 1 cause with resolvable evidence IDs
- No recommended VIOLATION
- Capacity ledger never negative
- Every injected anomaly type is detected (recall = 1.0 at test scale)
- Boundary cases raise no exception
- No ground-truth leakage (static check on imports and paths in `plan/` and `validate/`)

Save the output to `reports/test_results.txt` (`pytest -q | tee`).

## 17. Phases (stop and report after each)

| Phase | Scope | Done when |
|---|---|---|
| 0 Scaffold | pyproject, package, CLI stub, configs, ids.py, rng.py, calendar.py, .gitignore, CLAUDE.md, PROGRESS.md, decisions.md | `pip install -e .`, `python -m planner --help` and `pytest` all pass |
| 1 Schema & docs | schema.py for all tables; `docs` command generates data_dictionary.md, schema.sql, erd.md | Docs generated; every field has type, required, allowed values, unit, unique, rule |
| 2 Generator | Section 9 steps 1–7 at test and starter scale | Starter generate < 30 s; PK/FK/enum/date checks pass; ON_HAND = ledger |
| 3 Planning | Quality gate, PAB, planned orders, production/BOM, shortages, causes, explanations, priority, shortage events | PAB reconciliation test passes; shortage rate 5–12%; every shortage explained |
| 4 Mitigations | Options, capacity ledger, scoring, policy, approval, recommendation | Policy and capacity tests pass; alternatives show cost vs service |
| 5 Edge cases & truth | Anomaly and boundary injection, ground_truth.csv, evaluate | Recall 1.0 and precision ≈ 1.0 on starter; 0 boundary false positives |
| 6 Validate, export, repro | Full validation suite, exports, manifest, repro-check, data profile, figures; **then run the full scale** | Unexpected failures = 0; identical checksums; full run < 10 min; no file > 50 MB |
| 7 README & polish | README, business_rules.md, requirement → evidence checklist; fresh-clone test in /tmp | A fresh clone runs `run-all --scale starter` with only README instructions |
| 8 Optional | Only if everything above is done: a small Streamlit viewer (navy theme) for the top shortages and their mitigation trade-offs | — |

## 18. Final self-check before declaring done

Go through the PDF section by section (1–10). Confirm each requirement in the README
checklist with the file path that proves it. In particular check:
- All 11 core entities exist with PK and FKs.
- Bridge tables are used for N:M relationships.
- Effective dates and lifecycle states are present.
- Event history is kept.
- Exact ID patterns are used.
- 3,000 / 50 / 52 / 500 / 10,000 volumes are met at full scale.
- Demand spikes, constrained suppliers, substitutes and transport delays are present.
- Seed 20261022 is used.
- All 5 edge cases are labelled and traceable.
- Ground truth is separate.
- Validation output, tests output, data exports, data dictionary, generator command,
  seed, source manifest and validation summary are all present.
- Reproducibility is demonstrated.
