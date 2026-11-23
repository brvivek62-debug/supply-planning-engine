# Progress

## Status
| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold | ✅ done |
| 1 | Schema & docs | ✅ done |
| 2 | Generator | ✅ done (test + starter) |
| 3 | Planning | ⏳ next |
| 4 | Mitigations | — |
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
- Starter profile: A-class = 78% of volume; PROMO 3.0% of weeks; delay rate 8.0%; PO status 20% RECEIVED / 4% CANCELLED; firm PO load median 18% of capacity.
- Tests: 85 passing (adds generated-data checks, mutation tests proving each check catches its corruption, byte-identical reproducibility and seed sensitivity).

## Next
- Phase 3: quality gate, PAB, planned orders (MRP with lead-time fence and capacity allocation), production/BOM, shortages, causes, explanations, priority, shortage events. Calibrate shortage rate to 5–12% via `inventory.opening_cover_bias` if needed.

## Open issues
- None.
