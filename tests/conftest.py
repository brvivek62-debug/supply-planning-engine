import pytest

from planner import generate, tableio
from planner.config import load_config


@pytest.fixture(scope="session")
def test_cfg():
    return load_config("test")


@pytest.fixture(scope="session")
def generated(tmp_path_factory, test_cfg):
    """Generate the test scale once, write it, and read it back through the CSV round trip."""
    out = tmp_path_factory.mktemp("gen_test")
    generate.run(test_cfg, out)
    tables = tableio.read_all(out, tableio.fmt_of(test_cfg), list(generate.INPUT_TABLES))
    return out, tables
