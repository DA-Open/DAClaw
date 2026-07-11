import sqlite3

from agent.data_agent.engine import DataEngine


def test_local_sources_restore_after_engine_restart(tmp_path):
    path = tmp_path / "demo.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders(id INTEGER, amount REAL)")
        conn.execute("INSERT INTO orders VALUES (1, 12.5)")

    first = DataEngine()
    first.connect_source(str(path))
    first.con.close()

    restored = DataEngine()
    assert "orders" in restored.list_tables()
    assert restored.run_query("SELECT COUNT(*) AS n FROM orders").rows == [[1]]
