"""Deterministic, schema-driven table reading and writing.

Writes: columns in schema order, rows sorted by primary key, fixed decimal rounding,
booleans as ``true``/``false``, empty string for NULL, ``\\n`` line endings, gzip with mtime=0.
"""
from __future__ import annotations

import gzip
import io
from pathlib import Path

import numpy as np
import pandas as pd

from planner.config import Config, project_root
from planner.schema import BY_NAME, Table


def data_dir(scale: str, root: Path | None = None) -> Path:
    return (root or project_root()) / "data" / scale


def table_path(base: Path, table: str, fmt: str) -> Path:
    return Path(base) / "csv" / f"{table}.{fmt}"


def _fmt_column(s: pd.Series, dtype: str, decimals: int | None) -> pd.Series:
    na = s.isna()
    if dtype == "integer":
        out = s.astype("Int64").astype(str)
    elif dtype == "decimal":
        d = 4 if decimals is None else decimals
        vals = np.round(s.astype(float).to_numpy(), d)
        vals = np.where(vals == 0, 0.0, vals)  # avoid "-0.00"
        out = pd.Series([f"{v:.{d}f}" for v in vals], index=s.index)
    elif dtype == "boolean":
        out = s.map(lambda v: "true" if bool(v) else "false")
    else:
        out = s.astype(str)
    return out.mask(na, "")


def to_text(df: pd.DataFrame, table: Table) -> str:
    missing = [c for c in table.columns if c not in df.columns]
    if missing:
        raise ValueError(f"{table.name}: missing columns {missing}")
    df = df[table.columns].sort_values(list(table.pk), kind="mergesort").reset_index(drop=True)
    out = pd.DataFrame({f.name: _fmt_column(df[f.name], f.dtype, f.decimals) for f in table.fields})
    buf = io.StringIO()
    out.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue()


def write_table(df: pd.DataFrame, table: str | Table, base: Path, fmt: str = "csv") -> Path:
    t = BY_NAME[table] if isinstance(table, str) else table
    path = table_path(base, t.name, fmt)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = to_text(df, t).encode("utf-8")
    if fmt == "csv.gz":
        with open(path, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
                gz.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)
    return path


def from_text_frame(raw: pd.DataFrame, t: Table) -> pd.DataFrame:
    out = {}
    for f in t.fields:
        s = raw[f.name] if f.name in raw.columns else pd.Series([""] * len(raw), dtype=object)
        empty = s.eq("")
        if f.dtype == "integer":
            col = pd.to_numeric(s.mask(empty), errors="raise")
            col = col.astype("Int64") if (not f.required or empty.any()) else col.astype("int64")
        elif f.dtype == "decimal":
            col = pd.to_numeric(s.mask(empty), errors="raise").astype(float)
        elif f.dtype == "boolean":
            col = s.map({"true": True, "false": False})
            if not empty.any():
                col = col.astype(bool)
        else:
            col = s.astype(object).mask(empty, None)
        out[f.name] = col
    return pd.DataFrame(out)


def read_table(table: str, base: Path, fmt: str = "csv") -> pd.DataFrame:
    t = BY_NAME[table]
    path = table_path(base, table, fmt)
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, compression="gzip" if fmt == "csv.gz" else None)
    return from_text_frame(raw, t)


def exists(table: str, base: Path, fmt: str = "csv") -> bool:
    return table_path(base, table, fmt).exists()


def read_all(base: Path, fmt: str, tables: list[str] | None = None) -> dict[str, pd.DataFrame]:
    names = tables or [t for t in BY_NAME if exists(t, base, fmt)]
    return {n: read_table(n, base, fmt) for n in names}


def fmt_of(cfg: Config) -> str:
    return str(cfg.get("output", {}).get("format", "csv"))
