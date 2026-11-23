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
