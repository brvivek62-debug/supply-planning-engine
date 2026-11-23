# Design decisions

One line each: decision — reason.

## Phase 0
- Build backend setuptools with `src/` layout, console script `planner` plus `python -m planner` — standard, installable with `pip install -e .`.
- `requires-python >= 3.11`; developed on Python 3.15 in `.venv` — brief asks for 3.11+; no 3.12+ only syntax used.
- Per-stream RNG = `SeedSequence(entropy=seed, spawn_key=(sha256(name)[:4],))` — adding a stream never shifts other streams; independent of `PYTHONHASHSEED`.
- Config files repeat the shared parameter block verbatim; only `volumes` and `injection` differ — makes it obvious scale is the only difference, no inheritance magic.
- Location type mix test 1/2/3, starter 2/3/5, full 5/10/35 (PLANT/DC/STORE) — full mix given by the prompt; smaller scales keep at least one plant.
- `calendar.week_offset(date)` returns the planning-week index (1 = 2026W01, 0 or negative = history) — lets PO order dates in 2025 and receipts in 2026 share one integer axis.
- Default `--scale` for CLI commands is `starter` — matches the documented primary workflow.
- CLI commands not yet built exit with code 2 and a "Phase N" message — keeps `--help` complete from Phase 0.
- Added MIT `LICENSE` file — brief asks for lawful provenance; prompt specifies MIT for code.

## Phase 1
- `schema.py` declares 24 tables in dependency order (each table only references earlier ones) — the SQL DDL, FK-ordered seed inserts and validators can all walk `TABLES` in order.
- Field-level rule text is the custom rule plus auto-derived parts (regex, enum, bounds, rounding) — guarantees every field has a non-empty validation rule in the dictionary.
- Composite FKs `(sku_id, location_id) → sku_location` on demand, inventory, ledger, POs, production, PAB and shortages — enforces that these rows only exist for stocked SKU-locations.
- `sku_location` and `production_capacity` and `projected_balance` and `shortage_cause` use composite PKs — the prompt gives them no surrogate ID pattern.
- Added `supplier_sku_id` to `purchase_order` and `planned_order` — makes "PO comes from an APPROVED, effective supplier_sku" a checkable FK instead of an inferred join.
- Added `capacity_id` to `planned_order` — gives SUPPLIER_CAPACITY causes a concrete evidence record.
- Added `ref_lead_time_id`, `ref_substitute_id` and `created_by_role` to `mitigation_option` — traceability for AIR expedite lanes, substitute approval checks and MANUAL planner proposals.
- Added `requested_qty_ea` and `component_cut_ea` to `production_plan` — makes capacity and COMPONENT_SHORTAGE cuts reconcilable.
- Added optional `detail` to `exception` — human-readable quantities for each exception.
- PO receipt weeks use an `iso_week` pattern (2025–2027) while planning weeks use `2026Wnn` — received POs placed in Sept 2025 have receipt weeks in 2025.
- Polymorphic references (`shortage_cause.evidence_id`, `exception.record_id`, `ground_truth.record_id`) have no SQL FK; they are validated in code against the table named by the type column.
- Natural keys are emitted as SQL `UNIQUE`; inventory has none because a duplicate IN_TRANSIT row per PO is the injected DUPLICATE_INBOUND anomaly (caught by the quality gate).
- SKU lifecycle and supplier status are snapshot states (no event table); PO and shortage lifecycles have append-only event tables — the brief asks for event history on state changes that the workflow performs.
- Booleans stored as `true/false` in CSV/JSON and `1/0` in SQLite — SQLite has no boolean type.
- Shortage event actor roles limited to PLANNER and OPERATIONS_MANAGER — those are the brief's actors who own shortage decisions.

## Phase 2
- PO order dates run 2025-09-01 to 2025-12-28 (not to 2026-04) — the as-of date is 2025-12-29 and no PO event may be dated after it; open-PO receipts still extend into 2026 (up to ~W09 on SEA lanes).
- PO status is never assigned: each PO gets a timeline (CREATED → CONFIRMED → SHIPPED → IN_TRANSIT [→ DELAYED → IN_TRANSIT] → RECEIVED, or → CANCELLED) and status is the last event before the as-of cut-off — status and event history cannot disagree.
- Status classes are fixed first (20% RECEIVED, 4% CANCELLED, rest open) and order dates are drawn so the class is physically consistent with order week + lead time — hits the target mix exactly.
- Delays on open POs are only assigned to POs already IN_TRANSIT before the as-of date — a delay cannot be known before the shipment moves; overall delay rate is topped up to 8%.
- Open POs are allocated against supplier capacity at their revised receipt week (shrunk to a valid MOQ/multiple, or redrawn) — the baseline never exceeds capacity, so SUPPLIER_CAPACITY_EXCEEDED exceptions come only from injected anomalies (precision stays 1.0).
- PO quantity = U(2, 4) weeks of the SKU-location's mean demand; every BUY SKU-location is equally likely to get a PO (`pair_weight_power: 0`) — weighting by demand put early-week PO load at 2× capacity; this keeps firm load ≈ 20% median utilisation and leaves room for planned orders.
- Latent parameters (SKU base rate, location size, seasonality, "constrained" supplier flag, importer flag) live only in memory — constraint emerges from capacity numbers; no such flag is exported.
- Supplier capacity = primary-SKU expected weekly load × headroom (1.1–1.6, or 0.6–0.9 for 10% of suppliers) × ±8% weekly noise; suppliers with no primary SKUs get 20–60% of the median load — gives alternates spare capacity.
- ~15% of suppliers are importers whose standard lanes are SEA (5–9 wk); others use ROAD for same region, ROAD/RAIL for adjacent, RAIL for far regions; lane changes from 2026W27 stay inside the mode's typical band.
- BOM is generated before sku_location (still inside step 2) — component stocking plants follow their parents' home plants. Every MAKE SKU is stocked at exactly one home plant.
- `sku_location.safety_stock_qty` is filled after demand: round-half-up(safety_stock_weeks × mean weekly forecast), with dependent demand (parent forecast × qty_per) for components — it is a derived value.
- Opening cover = (primary lead weeks + SS weeks) × U(0.65, 1.35) × `opening_cover_bias`; the ledger is back-solved so OPENING + RECEIPTs + ISSUE + ADJUSTMENT = target, and ON_HAND is then computed from the ledger sum.
- ISSUE row omitted for SKU-locations with zero history demand (rule says ISSUE < 0) — avoids zero-quantity ledger rows.
- FIRM production = forecast × U(0.95, 1.15) for W01–W04, scaled down proportionally where a plant-week exceeds capacity (cut recorded in `capacity_cut_ea`).
- Added `production_plan.capacity_cut_ea` — makes requested − cuts = planned reconcilable.
- Demand rows at full scale will be ~0.97M, not ~1.2M — 6–10 DC/STORE locations per BUY FINISHED SKU (as specified) × 2,295 SKUs × 52 weeks; kept the specified assortment range rather than inflating it.
- Data files go to `data/<scale>/csv/<table>.csv` (test/starter) or `.csv.gz` (full); `data/test/` and `run_info.json` are git-ignored (run_info holds timing, so it is not reproducible by design).
