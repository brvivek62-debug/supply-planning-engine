import pytest

from planner import generate, plan, tableio
from planner.config import load_config


@pytest.fixture(scope="session")
def test_cfg():
    return load_config("test")


@pytest.fixture(scope="session")
def baseline(tmp_path_factory, test_cfg):
    """Clean baseline (no injected cases) at test scale, read back through the CSV round trip."""
    out = tmp_path_factory.mktemp("gen_baseline")
    generate.run(test_cfg, out, inject=False)
    tables = tableio.read_all(out, tableio.fmt_of(test_cfg), list(generate.INPUT_TABLES))
    return out, tables


@pytest.fixture(scope="session")
def generated(tmp_path_factory, test_cfg):
    """Full generate (baseline + injected cases + ground truth) at test scale, read back from CSV."""
    out = tmp_path_factory.mktemp("gen_test")
    generate.run(test_cfg, out)
    tables = tableio.read_all(out, tableio.fmt_of(test_cfg), list(generate.INPUT_TABLES))
    return out, tables


@pytest.fixture(scope="session")
def planned(generated, test_cfg):
    """Plan the generated test scale and read every table (inputs + outputs) back from CSV."""
    out, _ = generated
    res = plan.run(test_cfg, out)
    tables = tableio.read_all(out, tableio.fmt_of(test_cfg))
    return res, tables
