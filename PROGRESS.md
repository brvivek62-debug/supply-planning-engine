# Progress

## Status
| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold | ✅ done |
| 1 | Schema & docs | ⏳ next |
| 2 | Generator | — |
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

## Next
- Phase 1: `schema.py` for every table; `python -m planner docs` generates `docs/data_dictionary.md`, `sql/schema.sql`, `docs/erd.md`.

## Open issues
- None.
