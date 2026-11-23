"""ID formatters and validation regexes. IDs are immutable and zero-padded."""
from __future__ import annotations

import re

YEAR = 2026

# --- formatters --------------------------------------------------------------------------

def sku_id(n: int) -> str: return f"SKU-{n:06d}"
def location_id(n: int) -> str: return f"LOC-{n:03d}"
def supplier_id(n: int) -> str: return f"SUP-{n:04d}"
def po_id(n: int) -> str: return f"PO-{YEAR}-{n:06d}"
def planned_order_id(n: int) -> str: return f"PLN-{YEAR}-{n:06d}"
def shortage_id(n: int) -> str: return f"SHORT-{YEAR}-{n:06d}"
def prod_plan_id(n: int) -> str: return f"PROD-{YEAR}-{n:06d}"
def exception_id(n: int) -> str: return f"EXC-{YEAR}-{n:06d}"
def lead_time_id(n: int) -> str: return f"LT-{n:06d}"
def capacity_id(n: int) -> str: return f"CAP-{n:07d}"
def inventory_id(n: int) -> str: return f"INV-{n:07d}"
def txn_id(n: int) -> str: return f"TXN-{n:08d}"
def po_event_id(n: int) -> str: return f"POE-{n:08d}"
def shortage_event_id(n: int) -> str: return f"SHE-{n:07d}"
def bom_id(n: int) -> str: return f"BOM-{n:05d}"
def substitute_id(n: int) -> str: return f"SSUB-{n:05d}"
def supplier_sku_id(n: int) -> str: return f"SS-{n:06d}"
def gt_id(n: int) -> str: return f"GT-{n:05d}"


def demand_id(sku: str, loc: str, week: str) -> str:
    """``DEM-SKU-000001-LOC-001-2026W01``; ``week`` is a ``2026Wnn`` label."""
    return f"DEM-{sku}-{loc}-{week}"


def mitigation_id(short_id: str, seq: int) -> str:
    return f"MIT-{short_id}-{seq:02d}"


# --- regexes -----------------------------------------------------------------------------

_SKU = r"SKU-\d{6}"
_LOC = r"LOC-\d{3}"
_SHORT = rf"SHORT-{YEAR}-\d{{6}}"

PATTERNS: dict[str, str] = {
    "sku": _SKU,
    "location": _LOC,
    "supplier": r"SUP-\d{4}",
    "demand": rf"DEM-{_SKU}-{_LOC}-{YEAR}W(0[1-9]|[1-4]\d|5[0-3])",
    "purchase_order": rf"PO-{YEAR}-\d{{6}}",
    "planned_order": rf"PLN-{YEAR}-\d{{6}}",
    "shortage": _SHORT,
    "mitigation": rf"MIT-{_SHORT}-\d{{2}}",
    "production_plan": rf"PROD-{YEAR}-\d{{6}}",
    "exception": rf"EXC-{YEAR}-\d{{6}}",
    "lead_time": r"LT-\d{6}",
    "capacity": r"CAP-\d{7}",
    "inventory": r"INV-\d{7}",
    "txn": r"TXN-\d{8}",
    "po_event": r"POE-\d{8}",
    "shortage_event": r"SHE-\d{7}",
    "bom": r"BOM-\d{5}",
    "substitute": r"SSUB-\d{5}",
    "supplier_sku": r"SS-\d{6}",
    "gt": r"GT-\d{5}",
    "week": rf"{YEAR}W(0[1-9]|[1-4]\d|5[0-3])",
}

REGEX: dict[str, re.Pattern[str]] = {k: re.compile(rf"^{v}$") for k, v in PATTERNS.items()}


def is_valid(kind: str, value: str) -> bool:
    return bool(REGEX[kind].match(str(value)))


def parse_counter(value: str) -> int:
    """Trailing numeric counter of a counter-style ID (``PO-2026-000123`` -> 123)."""
    return int(str(value).rsplit("-", 1)[1])
