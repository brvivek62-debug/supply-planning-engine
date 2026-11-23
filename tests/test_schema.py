import sqlite3

import pytest

from planner import docs_gen, ids, schema
from planner.schema import BY_NAME, DTYPES, TABLES


def test_core_entities_present():
    core = {t.core_entity for t in TABLES if t.core_entity}
    assert set(schema.CORE_ENTITIES) <= core


def test_table_names_unique():
    assert len(BY_NAME) == len(TABLES)


@pytest.mark.parametrize("t", TABLES, ids=lambda t: t.name)
def test_table_definition_is_complete(t):
    names = t.columns
    assert len(names) == len(set(names)), "duplicate field"
    assert t.pk and all(c in names for c in t.pk)
    if t.natural_key:
        assert all(c in names for c in t.natural_key)
    for c in t.polymorphic_refs:
        assert c in names
    for f in t.fields:
        assert f.dtype in DTYPES, f.name
        assert f.description, f.name
        assert f.full_rule, f.name
        if f.id_kind:
            assert f.id_kind in ids.REGEX, f.name
        if f.allowed is not None:
            assert len(f.allowed) > 0
    for c in t.pk:
        assert t.field(c).required, f"PK column {c} must be required"


@pytest.mark.parametrize("t", TABLES, ids=lambda t: t.name)
def test_foreign_keys_resolve_to_earlier_unique_keys(t):
    order = {x.name: i for i, x in enumerate(TABLES)}
    for k in t.fks:
        assert k.ref_table in BY_NAME, k
        parent = BY_NAME[k.ref_table]
        assert order[parent.name] < order[t.name], f"{t.name} must come after {parent.name}"
        assert all(c in t.columns for c in k.columns)
        assert all(c in parent.columns for c in k.ref_columns)
        assert len(k.columns) == len(k.ref_columns)
        assert tuple(k.ref_columns) == parent.pk or tuple(k.ref_columns) == parent.natural_key, k
        for c in k.columns:
            assert t.field(c).required != k.optional or c in t.pk, (t.name, c)
        assert k.cardinality in ("1:N", "1:0..1", "1:1")


def test_bridge_tables_cover_many_to_many():
    for _, _, bridge, _ in schema.MANY_TO_MANY:
        assert bridge in BY_NAME


def test_lifecycles():
    po = schema.LIFECYCLES["purchase_order"]
    assert po.allowed(None, "CREATED")
    assert po.allowed("IN_TRANSIT", "DELAYED") and po.allowed("DELAYED", "IN_TRANSIT")
    assert not po.allowed("SHIPPED", "CANCELLED")
    assert not po.allowed("RECEIVED", "IN_TRANSIT")
    sh = schema.LIFECYCLES["shortage"]
    assert sh.allowed("MITIGATION_PROPOSED", "ESCALATED")
    assert not sh.allowed("OPEN", "MITIGATION_SELECTED")
    for lc in schema.LIFECYCLES.values():
        for a, b in lc.transitions:
            assert a in lc.states and b in lc.states
        for t in (x for x in TABLES if x.lifecycle):
            assert t.lifecycle in schema.LIFECYCLES


def test_schema_sql_loads_in_sqlite_with_foreign_keys():
    con = sqlite3.connect(":memory:")
    con.executescript(docs_gen.schema_sql())
    con.execute("PRAGMA foreign_keys = ON")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(BY_NAME)
    # FK enforcement works: an orphan child row is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO sku_location VALUES ('SKU-000001','LOC-001',5,1,'2025-01-01')")
    # Enum CHECK constraint works.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO location VALUES ('LOC-001','x','WAREHOUSE','Pune','WEST',1,'2025-01-01')")
    con.execute("INSERT INTO location VALUES ('LOC-001','x','DC','Pune','WEST',1,'2025-01-01')")


def test_docs_generation(tmp_path):
    paths = docs_gen.write_all(tmp_path)
    assert len(paths) == 3 and all(p.exists() and p.stat().st_size > 0 for p in paths)
    dd = (tmp_path / "docs" / "data_dictionary.md").read_text()
    for t in TABLES:
        assert f"## {t.name}" in dd
    erd = (tmp_path / "docs" / "erd.md").read_text()
    assert "erDiagram" in erd and "stateDiagram-v2" in erd and "N:M" in erd
    # deterministic
    again = tmp_path / "again"
    docs_gen.write_all(again)
    for p in paths:
        rel = p.relative_to(tmp_path)
        assert p.read_bytes() == (again / rel).read_bytes()


def test_committed_docs_are_up_to_date():
    from planner.config import project_root
    root = project_root()
    assert (root / "docs" / "data_dictionary.md").read_text().rstrip("\n") == docs_gen.data_dictionary().rstrip("\n")
    assert (root / "sql" / "schema.sql").read_text().rstrip("\n") == docs_gen.schema_sql().rstrip("\n")
    assert (root / "docs" / "erd.md").read_text().rstrip("\n") == docs_gen.erd().rstrip("\n")
