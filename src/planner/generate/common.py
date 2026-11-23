"""Shared generator context and small helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from planner.config import Config
from planner.rng import RngFactory


@dataclass
class Ctx:
    """Holds config, RNG factory, generated tables and latent (never exported) parameters."""
    cfg: Config
    rngs: RngFactory
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    aux: dict[str, Any] = field(default_factory=dict)

    @property
    def as_of(self) -> date:
        return self.cfg.as_of_date

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.tables[name]


def rand_dates(rng: np.random.Generator, start: date, end: date, n: int) -> list[str]:
    """``n`` ISO dates uniformly in [start, end]."""
    span = (end - start).days
    offs = rng.integers(0, span + 1, size=n)
    return [(start + timedelta(days=int(o))).isoformat() for o in offs]


def round_half_up(x) -> np.ndarray:
    return np.floor(np.asarray(x, dtype=float) + 0.5).astype(np.int64)


def iso(d: date | datetime) -> str:
    return d.isoformat(timespec="seconds") if isinstance(d, datetime) else d.isoformat()


def d(s: str) -> date:
    return date.fromisoformat(s)


def is_blank(v) -> bool:
    """True for None / NaN / empty string (pandas turns None into NaN in object columns)."""
    return v is None or (isinstance(v, float) and v != v) or v == ""


def exact_split(rng: np.random.Generator, n: int, shares: dict[str, float]) -> np.ndarray:
    """Array of ``n`` labels with counts as close as possible to ``shares``, shuffled."""
    keys = list(shares)
    raw = np.array([shares[k] for k in keys], dtype=float) * n
    counts = np.floor(raw).astype(int)
    for i in np.argsort(-(raw - counts), kind="stable")[: n - counts.sum()]:
        counts[i] += 1
    out = np.repeat(np.array(keys, dtype=object), counts)
    rng.shuffle(out)
    return out
