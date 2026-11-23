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
