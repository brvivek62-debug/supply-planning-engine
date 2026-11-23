from datetime import date

import numpy as np
import pytest

from planner import calendar as cal
from planner.config import load_config
from planner.rng import stream


def test_week_one_starts_2025_12_29():
    assert cal.week_start(1) == date(2025, 12, 29)
    assert cal.week_start_of("2026W27") == date.fromisocalendar(2026, 27, 1)
    assert cal.weeks_in_year(2026) == 53
    assert cal.week_labels(52)[-1] == "2026W52"


def test_week_offset_roundtrip_and_history():
    assert cal.week_offset(date(2025, 12, 29)) == 1
    assert cal.week_offset(date(2026, 1, 4)) == 1
    assert cal.week_offset(date(2026, 1, 5)) == 2
    assert cal.week_offset(date(2025, 12, 28)) == 0
    for off in (-20, 0, 1, 27, 52, 60):
        assert cal.week_offset(cal.date_of_offset(off)) == off


def test_lead_weeks_ceiling():
    assert cal.lead_weeks(7, 0) == 1
    assert cal.lead_weeks(7, 1) == 2
    assert cal.lead_weeks(20, 30) == 8


def test_streams_are_reproducible_and_independent():
    a1 = stream(20261022, "sku").integers(0, 1_000_000, 10)
    a2 = stream(20261022, "sku").integers(0, 1_000_000, 10)
    b = stream(20261022, "supplier").integers(0, 1_000_000, 10)
    c = stream(1, "sku").integers(0, 1_000_000, 10)
    assert np.array_equal(a1, a2)
    assert not np.array_equal(a1, b)
    assert not np.array_equal(a1, c)


@pytest.mark.parametrize("scale,skus", [("test", 60), ("starter", 300), ("full", 3000)])
def test_configs_load(scale, skus):
    cfg = load_config(scale)
    assert cfg.seed == 20261022
    assert cfg.volumes["skus"] == skus
    assert cfg.weeks == 52
    assert cfg.as_of_date == date(2025, 12, 29)
    mix = cfg.volumes["location_type_mix"]
    assert sum(mix.values()) == cfg.volumes["locations"]


def test_full_scale_volumes_match_brief():
    v = load_config("full").volumes
    assert (v["skus"], v["locations"], v["weeks"], v["suppliers"], v["purchase_orders"]) == (3000, 50, 52, 500, 10000)
