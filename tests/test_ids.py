from planner import ids


def test_formatters_match_brief_examples():
    assert ids.sku_id(1) == "SKU-000001"
    assert ids.location_id(1) == "LOC-001"
    assert ids.supplier_id(1) == "SUP-0001"
    assert ids.po_id(1) == "PO-2026-000001"
    assert ids.shortage_id(1) == "SHORT-2026-000001"
    assert ids.planned_order_id(1) == "PLN-2026-000001"
    assert ids.demand_id("SKU-000001", "LOC-001", "2026W01") == "DEM-SKU-000001-LOC-001-2026W01"
    assert ids.mitigation_id("SHORT-2026-000001", 1) == "MIT-SHORT-2026-000001-01"


def test_every_formatter_matches_its_regex():
    samples = {
        "sku": ids.sku_id(42), "location": ids.location_id(7), "supplier": ids.supplier_id(500),
        "purchase_order": ids.po_id(10001), "planned_order": ids.planned_order_id(3),
        "shortage": ids.shortage_id(9), "mitigation": ids.mitigation_id(ids.shortage_id(9), 12),
        "production_plan": ids.prod_plan_id(5), "exception": ids.exception_id(5),
        "lead_time": ids.lead_time_id(5), "capacity": ids.capacity_id(5), "inventory": ids.inventory_id(5),
        "txn": ids.txn_id(5), "po_event": ids.po_event_id(5), "shortage_event": ids.shortage_event_id(5),
        "bom": ids.bom_id(5), "substitute": ids.substitute_id(5), "supplier_sku": ids.supplier_sku_id(5),
        "gt": ids.gt_id(5), "week": "2026W52", "iso_week": "2025W40",
        "demand": ids.demand_id(ids.sku_id(3000), ids.location_id(50), "2026W52"),
    }
    assert set(samples) == set(ids.REGEX)
    for kind, value in samples.items():
        assert ids.is_valid(kind, value), (kind, value)


def test_regex_rejects_bad_ids():
    assert not ids.is_valid("sku", "SKU-1")
    assert not ids.is_valid("sku", "SKU-0000001")
    assert not ids.is_valid("purchase_order", "PO-2025-000001")
    assert not ids.is_valid("demand", "DEM-SKU-000001-LOC-001-2026W00")
    assert not ids.is_valid("mitigation", "MIT-SHORT-2026-000001-1")
    assert not ids.is_valid("week", "2026W54")


def test_parse_counter():
    assert ids.parse_counter("PO-2026-010001") == 10001
    assert ids.parse_counter("SKU-000123") == 123
