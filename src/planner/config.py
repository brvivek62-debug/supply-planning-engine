"""Load scale configuration from config/<scale>.yaml.

Scale comes only from config, so the same generator code produces test, starter and full data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SCALES = ("test", "starter", "full")


def project_root() -> Path:
    """Repository root (the folder that holds config/)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config").is_dir() and (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


@dataclass(frozen=True)
class Config:
    scale: str
    seed: int
    raw: dict[str, Any] = field(repr=False)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def volumes(self) -> dict[str, Any]:
        return self.raw["volumes"]

    @property
    def as_of_date(self) -> date:
        return date.fromisoformat(str(self.raw["as_of_date"]))

    @property
    def weeks(self) -> int:
        return int(self.raw["volumes"]["weeks"])


def load_config(scale: str, seed: int | None = None, config_dir: Path | None = None) -> Config:
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; expected one of {SCALES}")
    cdir = config_dir or (project_root() / "config")
    with open(cdir / f"{scale}.yaml", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw.get("scale") != scale:
        raise ValueError(f"config/{scale}.yaml declares scale={raw.get('scale')!r}")
    use_seed = int(seed if seed is not None else raw["seed"])
    raw["seed"] = use_seed
    return Config(scale=scale, seed=use_seed, raw=raw)
