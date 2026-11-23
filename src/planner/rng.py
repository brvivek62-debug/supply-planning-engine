"""Deterministic random streams.

One root ``SeedSequence(seed)``. Each table / purpose gets its own child generator whose
spawn key is derived from a stable hash of its *name*, so adding a new stream never shifts
the random numbers of any other stream.
"""
from __future__ import annotations

import hashlib

import numpy as np


def name_key(name: str) -> int:
    """Stable 32-bit integer derived from a stream name (independent of PYTHONHASHSEED)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4], "big")


def stream(seed: int, name: str) -> np.random.Generator:
    """Return an independent, reproducible generator for ``name`` under ``seed``."""
    ss = np.random.SeedSequence(entropy=int(seed), spawn_key=(name_key(name),))
    return np.random.Generator(np.random.PCG64(ss))


class RngFactory:
    """Convenience wrapper: ``rngs = RngFactory(seed); rng = rngs("sku")``."""

    def __init__(self, seed: int):
        self.seed = int(seed)

    def __call__(self, name: str) -> np.random.Generator:
        return stream(self.seed, name)
