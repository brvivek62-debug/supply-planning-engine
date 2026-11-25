"""Exports (MASTER_PROMPT 15): ``data/<scale>/json/*.json`` and ``data/<scale>/sql/seed.sql`` from the written
CSVs, then the manifest. JSON and SQL seed are produced for test/starter only; the full scale ships the
``csv.gz`` tables (GitHub file limit) and its manifest.
"""
from __future__ import annotations

import json
from pathlib import Path

from planner import manifest, tableio
from planner.config import Config
from planner.schema import BY_NAME
from planner.sqlio import DATA_TABLES, _py_values, read_text, seed_sql_lines

TEXT_EXPORT_SCALES = ("test", "starter")


def write_json(base: Path, fmt: str) -> list[Path]:
    out = []
    folder = Path(base) / "json"
    folder.mkdir(parents=True, exist_ok=True)
    for name in DATA_TABLES:
        if not tableio.exists(name, base, fmt):
            continue
        t = BY_NAME[name]
        cols = t.columns
        recs = [dict(zip(cols, r)) for r in _py_values(read_text(name, base, fmt), t)]
        for r in recs:   # booleans back to JSON true/false
            for f in t.fields:
                if f.dtype == "boolean" and r[f.name] is not None:
                    r[f.name] = bool(r[f.name])
        p = folder / f"{name}.json"
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(recs, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("\n")
        out.append(p)
    return out


def write_seed_sql(base: Path, fmt: str) -> Path:
    p = Path(base) / "sql" / "seed.sql"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for chunk in seed_sql_lines(base, fmt):
            fh.write(chunk)
    return p


def run(cfg: Config, base: Path | None = None) -> dict:
    base = Path(base) if base else tableio.data_dir(cfg.scale)
    fmt = tableio.fmt_of(cfg)
    written = []
    if cfg.scale in TEXT_EXPORT_SCALES:
        written += write_json(base, fmt)
        written.append(write_seed_sql(base, fmt))
    m = manifest.write(cfg, base)
    if Path(base).resolve() == tableio.data_dir(cfg.scale).resolve():
        manifest.write_source_manifest()
    return {"written": [str(p) for p in written], "manifest": m}
