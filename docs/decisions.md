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
- (Superseded by the realism tuning below) Supplier capacity = primary-SKU expected weekly load × headroom (1.1–1.6, or 0.6–0.9 for 10% of suppliers) × ±8% weekly noise; suppliers with no primary SKUs get 20–60% of the median load — gives alternates spare capacity.
- ~15% of suppliers are importers whose standard lanes are SEA (5–9 wk); others use ROAD for same region, ROAD/RAIL for adjacent, RAIL for far regions; lane changes from 2026W27 stay inside the mode's typical band.
- BOM is generated before sku_location (still inside step 2) — component stocking plants follow their parents' home plants. Every MAKE SKU is stocked at exactly one home plant.
- `sku_location.safety_stock_qty` is filled after demand: round-half-up(safety_stock_weeks × mean weekly forecast), with dependent demand (parent forecast × qty_per) for components — it is a derived value.
- Opening cover = (primary lead weeks + SS weeks) × U(0.65, 1.35) × `opening_cover_bias`; the ledger is back-solved so OPENING + RECEIPTs + ISSUE + ADJUSTMENT = target, and ON_HAND is then computed from the ledger sum.
- ISSUE row omitted for SKU-locations with zero history demand (rule says ISSUE < 0) — avoids zero-quantity ledger rows.
- FIRM production = forecast × U(0.95, 1.15) for W01–W04, scaled down proportionally where a plant-week exceeds capacity (cut recorded in `capacity_cut_ea`).
- Added `production_plan.capacity_cut_ea` — makes requested − cuts = planned reconcilable.
- Demand rows at full scale will be ~0.97M, not ~1.2M — 6–10 DC/STORE locations per BUY FINISHED SKU (as specified) × 2,295 SKUs × 52 weeks; kept the specified assortment range rather than inflating it.
- Data files go to `data/<scale>/csv/<table>.csv` (test/starter) or `.csv.gz` (full); `data/test/` and `run_info.json` are git-ignored (run_info holds timing, so it is not reproducible by design).

## Phase 3
- Generator (Phase 2 revisit): supplier and plant capacity are sized per week on the *seasonal* expected load incl. expected promo uplift (1 + promo_rate x (mean multiplier - 1)), x headroom — sizing on the mean load let seasonal peaks and promos eat the 1.1x low-end headroom, so "normal" suppliers behaved as constrained. Seasonality is drawn once in a shared helper from its own named stream, so forecast_demand is byte-identical.
- (Superseded by the realism tuning below) Calibration: `inventory.opening_cover_bias` = 2.0 (opening cover = (lead + SS weeks) x U(0.65, 1.35) x 2.0) — at 1.0 about half of SKU-locations fall below SS inside the lead-time fence and the catch-up wave saturates suppliers; 2.0 gives 9.7% short at starter, 8.6% at test (target 5-12%).
- Planner outputs are written next to inputs in `data/<scale>/csv/`; `production_plan` is rewritten as FIRM + PLANNED rows, and plan reads FIRM rows from requested/capacity_cut (not planned_qty), so re-running plan is idempotent.
- One deterministic forward pass over weeks handles BUY MRP, MAKE production and component cuts together — dependent demand and component availability interact within the same week.
- Planned order qty = (SS - projected closing) + next `planned_order_cover_weeks` (4) of demand, rounded up to MOQ and order multiple in purchase UoM — "covers about 4 weeks"; component lot sizing uses parent forecast x qty_per as look-ahead.
- Receipt lands in the latest week <= need week reachable with release >= W01 on the lane version effective at release — handles the W27 lane change (an unreachable week just lands one week early); no reachable week = lead-time fence, no order.
- Capacity-constrained MRP pre-builds: before cutting, the remainder is pulled into up to `policy.prebuild_weeks` (8) earlier reachable weeks with spare supplier capacity — standard practice and removes artificial cuts from lumpy MOQ lots.
- A requirement that finds no capacity at all creates no zero-qty planned order; it is kept as SUPPLIER_CAPACITY evidence pointing at the supplier_capacity row — avoids thousands of empty orders re-raised every week.
- Supplier capacity allocation order within a receipt week: criticality, ABC, sku_id, location_id (components compete with finished SKUs on equal terms) — the brief's priority order.
- Production: zero lead time, PLANNED weeks lot-for-lot to safety stock, FIRM weeks frozen but still subject to component availability; cut order is component availability first, then plant capacity on the buildable qty — each cut is attributed to the constraint that binds.
- Quality gate: open POs with revised week before W01 land in W01, after W52 are outside the horizon; capacity overflow allocated by po_id, the overflow is pushed to the next week with spare capacity (CAPPED), POs in an overloaded week that still fit are FLAGGED; for duplicate IN_TRANSIT rows the lowest inventory_id is kept.
- Added cause code PRODUCTION_CAPACITY (evidence = production_plan row) — MAKE shortages from plant capacity had no honest code among the brief's six.
- Cause windows: planned-order / capacity cuts count if received in the run or up to cover weeks before; production cuts up to firm-weeks before; LOW_OPENING_STOCK only when the run starts inside the replenishment lead time (BUY lane lead time at W01, MAKE firm horizon).
- primary_cause = largest contribution; for backlog causes (SUPPLIER_CAPACITY, COMPONENT_SHORTAGE, PRODUCTION_CAPACITY) the contribution is the largest single cut, since the unmet qty is re-requested every week; ties broken by cause-code order.
- value_at_risk uses shortage_qty_ea as specified, so BELOW_SAFETY_STOCK shortages have VaR 0 and priority 0 (ranked after stockouts by shortage_id); urgency weeks = start week index - 1 (W01 -> 0).
- One OPEN shortage_event per shortage at the as-of date 06:00 by PLANNER (reason PAB_BELOW_SAFETY_STOCK); decide (Phase 4) appends the rest.
- Phase 3 output checks live in `validate/plan_checks.py` and re-run the deterministic quality gate to get firm receipts for the capacity ledger.

## Phase 3 realism tuning (starter)
- Supplier constraints are temporary instead of permanent: 10% of suppliers get one 4-8 week window at 0.5-0.7x, all other capacity = seasonal load x U(1.15, 1.3) — the 0.6-0.9x permanent constraint starved low-priority SKUs from mid-year to W52 with growing backlog (108 of 259 episodes ran to W52).
- Normal headroom 1.15-1.3, not the requested 1.0-1.15 — capacity/demand averaged 1.08 at 1.0-1.15, leaving no slack to rebuild safety stock: 25-30% of SKU-locations short, episodes up to 44 weeks; 1.15-1.3 is the lowest band that met 5-12% short.
- Capacity is generated after forecast_demand and level-corrected per SKU (plant: per SKU-location) to the published mean forecast, keeping the latent seasonal shape — the latent rate ran a few % under realized demand (NB noise, promo floor, rounding), so "1.15x" was really ~1.07x.
- Disrupted suppliers are drawn only from suppliers with primary load; one is drawn from component suppliers (weighted by component count) and starts at W02-W04, already under way at the planning run; others start after W02 and end >= 8 weeks before W52 — so component outages exist and every episode can recover in the horizon.
- Removed the latent `constrained` supplier flag from master generation; the latent `disrupted` flag lives in `ctx.aux` only.
- `opening_cover_bias` 2.0 -> 1.5 and `opening_cover_noise` 0.35 -> 0.20 — the brief asks for ~1.3-1.5; with noise 0.35 about 10% of SKU-locations opened below lead + SS cover and early blips dominated.
- Just-in-time opening stock for SKU-locations with an open delayed PO (50% of finished, 100% of components): cover = SS weeks (0 for components) + weeks to the PO's expected week + U(0.2, 0.7), own RNG stream — makes delays land in thin stock so PO_DELAYED and LOW_OPENING_STOCK appear (previously 0 and 0 primary).
- Component POs are 8x as likely to receive the open-PO transport delay (`component_delay_weight`) — gives the plant-side delay scenario that produces COMPONENT_SHORTAGE evidence; overall delay rate stays 8%.
- LOW_OPENING_STOCK now fires when the run starts inside the lead time and ON_HAND < lead x mean weekly demand + SS (was < lead x mean) — the old threshold ignored safety stock and never fired once opening cover included SS.
- PLANNED production builds ahead up to `build_ahead_weeks` (1) of next demand with spare plant capacity and component stock above SS, without counting as a cut — lot-for-lot with no build-ahead made every promo or maintenance week a PRODUCTION_CAPACITY shortage (~20% of all shortages); 2 weeks removed them entirely, 1 keeps some.
- Explanation test accepts any evidence row tied for the largest contribution of its cause — ties (e.g. a 72 EA planned-order cut and a 72 EA capacity miss) are legitimately quoted either way.

## Phase 4
- Mitigation requirement = `below_ss_qty_ea` (largest gap to SS in the run); `qty_covered_ea` = gap closed by the option on the shortage's own PAB, so a late arrival covers less — one formula for every lever, and coverage <= requirement by construction.
- Fill rate = 1 - sum(min(demand, backlog)) / sum(demand) (period fill rate under backlog semantics); global before/after uses finished-SKU demand over the horizon, "after" applies every recommended option's ledger effects.
- Requirement and coverage use the planner's PAB, not the ledger-updated balance — a second shortage on the same SKU-location is sized as planned even if an earlier mitigation over-delivered (e.g. MOQ overshoot).
- Only the recommended option books the ledger; escalated recommendations reserve capacity pending approval — otherwise an approval would find the capacity gone.
- Ledger written as `mitigation_ledger` (SUPPLIER_CAPACITY / PLANT_CAPACITY consumed per week, STOCK as signed flows) — validation recomputes supplier, plant and stock limits from written tables instead of trusting the planner.
- Stock drawn from another SKU-location (transfer source, substitute, component for extra production) is backfilled: the source re-orders on its primary lane at the draw week and only has to stay >= SS until that order lands, using its primary supplier's spare capacity; a permanent draw (>= SS to W52) is used when larger — MRP keeps every balance just above SS, so a whole-year surplus test found almost nothing (51% backorders); with backfill 17%.
- EXPEDITE_PO: only CREATED/CONFIRMED POs with one firm receipt; AIR arrival = max(W01, order week + AIR lead) since the AIR lead includes production; arrivals after the shortage start are kept as VIOLATION alternatives (rule: arrive by the start); the old receipt week's supplier capacity is released.
- ALTERNATE_SUPPLIER options are generated for PENDING/REJECTED/not-effective lines and ON_HOLD suppliers too, as VIOLATION alternatives with the reason — shows the policy filter; capacity- or lead-time-infeasible options are not generated (they cannot be executed at all).
- The system only proposes APPROVED, effective substitutes; anything else can only come from a MANUAL planner proposal (`mitigation_proposal`, Phase 5) and is flagged UNAPPROVED_SUBSTITUTE — keeps exception precision exact.
- Substitute cost = covered EA x original unit price x |price_delta_pct| (price given away when the substitute is dearer, revenue lost when cheaper); substitute EA = covered x substitution_ratio.
- INCREASE_PRODUCTION uses one production week (the one with the best coverage, preferring the latest week <= start) in spare plant capacity incl. FIRM weeks (overtime unfreezes them) — a single week keeps the option and its ledger rows simple.
- BACKORDER cost = stockout EA x (unit margin + service penalty); BELOW_SAFETY_STOCK shortages have 0 stockout so a backorder there costs 0 but scores <= 0.
- Cost normalisation and the 25%-of-VaR approval rule use value at risk, or the SS-gap value (below_ss x price) when VaR is 0 — otherwise every option on a BELOW_SAFETY_STOCK shortage would need approval.
- Score = 0.6 coverage + 0.25 timeliness (1/(1+weeks_late), 0 if nothing covered) - 0.15 min(1, cost / base value); recommendation = best COMPLIANT non-backorder option covering > 0 (ties: cost, seq), else BACKORDER — matches the lifecycle rule "BACKORDER_ACCEPTED when no feasible non-backorder option".
- One option per shortage is recommended (no combinations of e.g. two transfers) — keeps the decision and the event history one-to-one; partial coverage is visible in coverage_pct.
- Added `mitigation_option.action_qty_ea` (physical qty moved/ordered/produced) next to `qty_covered_ea` — they differ for MOQ overshoot, substitution ratio and late arrivals.
- Shortage events: OPEN 06:00, MITIGATION_PROPOSED 06:10, final 06:20 of the as-of date, all by PLANNER (ESCALATED hands over to OPERATIONS_MANAGER); event IDs numbered by (shortage_id, seq).

## Phase 5
- `generate` runs step 8 itself: baseline written and read back -> one in-memory planning pass -> boundary cases + input anomalies -> written and read back -> second planning pass -> MANUAL proposals for real shortages -> ground truth. `plan` then decides with the proposals — the brief's "generate -> plan -> inject manual options -> decide" order, but the CLI stays generate -> plan.
- MANUAL proposals live in a new input table `mitigation_proposal` (planner-entered, option_seq 91) instead of being appended to the planner's own `mitigation_option` output — inputs and outputs never mix, and re-running `plan` is idempotent.
- Quality gate: only the PO that actually overflows a supplier-week gets the SUPPLIER_CAPACITY_EXCEEDED exception (CAPPED); the other POs of that week are listed in `related_record_ids` instead of separate FLAGGED rows — they did nothing wrong, and the exception count per injected case stays exactly 1 (precision 1.0).
- Injected records are disjoint: a `Book` reserves every targeted PO and its supplier-week, so no record or supplier-week carries two cases (a boundary week can never be overloaded by an anomaly).
- SUPPLIER_CAPACITY_EXCEEDED adds a new CONFIRMED PO (continuing PO ids and POE event ids, CREATED -> CONFIRMED history, valid primary line and lane, receipt = order week + lead) sized to the week's spare + 10-30% of capacity; drawn from the least-spare supplier-weeks — the pushed overflow then disturbs the plan least.
- LEAD_TIME_IGNORED targets preferably open POs whose naive receipt at the order week would lift the PAB above SS for the weeks before the true receipt, i.e. the naive plan misses a real shortage; expected_outcome names that shortage and `evaluate` checks it exists in the corrected plan (10/10 at starter).
- SUPPLIER_LOAD_EQUALS_CAPACITY sets one supplier_capacity row to exactly its firm load, in a week whose spare the baseline MRP left unused — topping a PO up instead created 9 -> 5,020 EA orders and new SUPPLIER_CAPACITY shortages (rate 12.6%); this way the plan is unchanged.
- PO_QTY_EQUALS_MOQ is drawn from the open unshipped POs closest to their MOQ (with MOQ a multiple of the order multiple); cutting e.g. 1,600 -> 200 EA created shortages that are not part of the case. At starter all 5 picks are already at MOQ (label-only, stated in injection_rule); RECEIPT_EQUALS_LEAD_TIME is label-only by nature (the generator places receipts at order week + lead).
- UNAPPROVED_SUBSTITUTE: first a PENDING / REJECTED / not-effective rule of the short SKU; if too few exist (starter: 7), an ad-hoc same-category substitute stocked at the location with no sku_substitute rule at all — decide flags both (no approved rule). `mitigation_proposal.ref_substitute_id` is optional, `ref_substitute_sku_id` required.
- Boundary false positive = a BOUNDARY record named by any exception as record_id or in related_record_ids (stricter than record_id only).
- `evaluate.py` holds the only ground-truth reader; `inject/` holds the only writer and the path helper; a test scans the source tree for both, and that plan/ and validate/ neither mention ground truth nor import inject/evaluate.
- Ground truth goes to `ground_truth/<scale>/` for the default data folder, `<out>/ground_truth/` for a custom `--out` (tests use temp folders).
- Test fixtures: `baseline` (inject=False) for input-generator and gate unit tests, `generated` (full pipeline) for planning, decisions and evaluation — gate unit tests need a clean baseline to assert exact exception lists.
- `reports/test_results.txt` is written with `pytest -o addopts="" -q` — `addopts = -q` plus `-q` hides the pass-count line.

## Phase 6
- Expected vs unexpected failures: a failing record counts as expected only if the planner's own `exception` table names it under the issue type that explains that check (po_qty_uom_moq <- UOM_MISMATCH; po_receipt_week_arithmetic <- LEAD_TIME_IGNORED; firm_po_within_supplier_capacity <- SUPPLIER_CAPACITY_EXCEEDED via capacity_id; in_transit_mirrors_open_pos <- DUPLICATE_INBOUND or UOM_MISMATCH). Validation never reads ground truth, and an unkeyed or un-named failure is always unexpected.
- in_transit_mirrors_open_pos also fails for UOM_MISMATCH POs: their correct IN_TRANSIT row no longer equals the corrupted raw qty_base_ea, so that issue type explains it as well.
- Fixed in_transit_mirrors_open_pos counting a duplicated PO twice (Index.union keeps duplicates).
- The SQLite check loads every data table into an in-memory DB built from sql/schema.sql, with `PRAGMA foreign_keys = ON` (and CHECK constraints) at both scales, then runs `PRAGMA foreign_key_check`.
- Reports: starter writes to `reports/`, full to `reports/full/` (and other scales to `reports/<scale>/`), so a full run never overwrites the starter reports.
- JSON and seed.sql are exported for test/starter only. Full scale ships `csv.gz` and its manifest, because full JSON/SQL would exceed the 50 MB file limit and add nothing.
- seed.sql values are copied from the deterministic CSV text (booleans as 1/0), with 500 rows per INSERT in one transaction, so it is byte-stable.
- repro-check runs generate + plan twice with the seed and once with seed + 1 into temporary folders, comparing every CSV and the ground truth (run_info.json excluded).
- New metrics. Shortage units = sum(below_ss_qty_ea), the mitigation requirement, and the covered share = sum(recommended qty_covered_ea) / that. VaR recovered = VaR before - sum(unit price x stockout depth left in the run on the post-mitigation balance). The short-only fill rate is computed over finished SKU-locations with >= 1 shortage across all 52 weeks.
- run-all = generate -> plan -> validate -> evaluate -> export (+ data profile) -> repro-check; it exits non-zero on any unexpected failure or repro mismatch.
- supplier_sku: an expiring alternate line now ends >= 4 weeks after its effective_from (lower bound max(2026-03-31, effective_from + 28 d)). Full scale had 2 lines that became effective in 2026 with an earlier effective_to; the draw count is unchanged, so no other table shifts.

## Phase 7
- Business-rule owners follow who is accountable in practice: supplier terms and capacity -> Supplier; lanes, delays, expedite, transfers -> Logistics analyst; receipts, ledger, in-transit -> Carrier / warehouse operator; approvals, substitutes, overtime, escalation policy -> Operations manager; everything computed by the plan -> Planner.
- Quick start uses `python3 -m venv` + `pip install -e .` exactly as the brief asks; tests need `pip install -e ".[dev]"`, documented separately.
- README cites real check names and test node IDs as evidence so each requirement can be verified by running one command.
