import duckdb

from agent.data_agent.engine import DataEngine


def _make_db(path, statements):
    con = duckdb.connect(str(path))
    for sql in statements:
        con.execute(sql)
    con.close()


def test_duckdb_tables_outside_main_schema_are_exposed(tmp_path):
    path = tmp_path / "warehouse.duckdb"
    _make_db(path, [
        "CREATE TABLE orders(id INTEGER)",
        "INSERT INTO orders VALUES (1), (2)",
        "CREATE SCHEMA raw",
        "CREATE TABLE raw.events(id INTEGER)",
        "INSERT INTO raw.events VALUES (1), (2), (3)",
    ])

    engine = DataEngine()
    desc = engine.connect_source(str(path), persist=False)

    # main-schema names are unchanged; other schemas carry the schema name
    assert set(desc["tables"]) == {"warehouse_orders", "warehouse_raw_events"}
    assert engine.run_query("SELECT COUNT(*) FROM warehouse_orders").rows == [[2]]
    assert engine.run_query("SELECT COUNT(*) FROM warehouse_raw_events").rows == [[3]]


def test_duckdb_single_table_in_non_main_schema(tmp_path):
    path = tmp_path / "only_raw.duckdb"
    _make_db(path, [
        "CREATE SCHEMA raw",
        "CREATE TABLE raw.video(id INTEGER)",
        "INSERT INTO raw.video VALUES (7)",
    ])

    engine = DataEngine()
    desc = engine.connect_source(str(path), persist=False)

    assert desc["tables"] == ["video"]
    assert engine.run_query("SELECT id FROM video").rows == [[7]]
