# Progress

## Status
| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold | ✅ done |
| 1 | Schema & docs | ✅ done |
| 2 | Generator | ⏳ next |
| 3 | Planning | — |
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

## Next
- Phase 2: generator (Section 9 steps 1–7) at test and starter scale; starter generate < 30 s; PK/FK/enum/date checks; ON_HAND = ledger.

## Open issues
- None.
