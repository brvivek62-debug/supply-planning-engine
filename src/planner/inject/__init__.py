"""Labelled edge cases (MASTER_PROMPT 12). The ONLY package that writes ground truth.

Flow inside ``generate``:
1. baseline inputs are written and read back, and planned once in memory (to find real shortages)
2. ``boundaries.inject``  valid boundary cases (must raise no exception)
3. ``anomalies.inject_inputs``  DUPLICATE_INBOUND, SUPPLIER_CAPACITY_EXCEEDED, LEAD_TIME_IGNORED, UOM_MISMATCH
4. inputs are written again, read back and planned again in memory
5. ``anomalies.inject_proposals``  MANUAL mitigation proposals with unapproved substitutes (UNAPPROVED_SUBSTITUTE)
6. ground truth -> ``ground_truth/<scale>/ground_truth.csv``

Every injection only touches its targeted records; targets are disjoint (a record or supplier-week used by one
case is never used by another). Counts per type come from config ``injection``.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from planner import ids, tableio
from planner.config import project_root
from planner.schema import BY_NAME

GT_FILE = "ground_truth.csv"


def ground_truth_dir(scale: str, data_out: Path | None = None) -> Path:
    """``ground_truth/<scale>/`` for the default data folder; ``<out>/ground_truth/`` for a custom one."""
    default = tableio.data_dir(scale)
    if data_out is None or Path(data_out).resolve() == default.resolve():
        return project_root() / "ground_truth" / scale
    return Path(data_out) / "ground_truth"


def to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=[c for c in BY_NAME["ground_truth"].columns if c != "gt_id"])
    df.insert(0, "gt_id", [ids.gt_id(k + 1) for k in range(len(df))])
    return df


def write_ground_truth(df: pd.DataFrame, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / GT_FILE
    with open(path, "wb") as fh:
        fh.write(tableio.to_text(df, BY_NAME["ground_truth"]).encode("utf-8"))
    return path
