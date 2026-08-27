"""
test_correctness.py — Cross-checks MiniSQL against sqlite3's stdlib module.

The idea: build the same schema and data in both engines, run the same
queries, and diff the results. sqlite3 is used purely as a ground-truth
oracle here (not as MiniSQL's storage backend) — this is a standard
technique for validating a new query engine's semantics without having to
hand-derive "expected" results for every test by eye.
"""

import os
import shutil
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minisql.engine import Engine

TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "_tmp_test_data")


class CorrectnessTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.row_factory = sqlite3.Row

        self.engine.execute_sql("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
        self.sqlite.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")

        self.engine.execute_sql("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount REAL)")
        self.sqlite.execute("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount REAL)")

        users = [
            (1, "Alice", 30), (2, "Bob", 25), (3, "Carol", 35),
            (4, "Dave", 25), (5, "Eve", 41), (6, "Frank", 19),
        ]
        for u in users:
            self.engine.execute_sql(f"INSERT INTO users VALUES ({u[0]}, '{u[1]}', {u[2]})")
            self.sqlite.execute("INSERT INTO users VALUES (?, ?, ?)", u)

        orders = [
            (100, 1, 49.99), (101, 2, 12.50), (102, 1, 5.00),
            (103, 3, 100.00), (104, 4, 8.25),
        ]
        for o in orders:
            self.engine.execute_sql(f"INSERT INTO orders VALUES ({o[0]}, {o[1]}, {o[2]})")
            self.sqlite.execute("INSERT INTO orders VALUES (?, ?, ?)", o)

        self.sqlite.commit()

    def tearDown(self):
        self.engine.close()
        self.sqlite.close()
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)

    def _compare(self, minisql_query, sqlite_query, sort_key):
        got = self.engine.execute_sql(minisql_query)
        cur = self.sqlite.execute(sqlite_query)
        expected = [dict(row) for row in cur.fetchall()]

        got_sorted = sorted(got, key=lambda r: r[sort_key])
        expected_sorted = sorted(expected, key=lambda r: r[sort_key])
        self.assertEqual(len(got_sorted), len(expected_sorted),
                          f"row count mismatch: {len(got_sorted)} vs {len(expected_sorted)}")
        for g, e in zip(got_sorted, expected_sorted):
            for k in e:
                self.assertEqual(g.get(k), e[k], f"mismatch on column {k!r}: {g} vs {e}")

    def test_seq_scan_simple_filter(self):
        self._compare(
            "SELECT id, name, age FROM users WHERE age > 25",
            "SELECT id, name, age FROM users WHERE age > 25",
            "id",
        )

    def test_and_filter(self):
        self._compare(
            "SELECT id, name FROM users WHERE age >= 25 AND age < 35",
            "SELECT id, name FROM users WHERE age >= 25 AND age < 35",
            "id",
        )

    def test_or_filter(self):
        self._compare(
            "SELECT id, name FROM users WHERE age = 25 OR age = 41",
            "SELECT id, name FROM users WHERE age = 25 OR age = 41",
            "id",
        )

    def test_order_by_desc(self):
        self._compare(
            "SELECT id, name, age FROM users ORDER BY age DESC",
            "SELECT id, name, age FROM users ORDER BY age DESC",
            "id",
        )

    def test_limit(self):
        got = self.engine.execute_sql("SELECT id FROM users ORDER BY age LIMIT 3")
        cur = self.sqlite.execute("SELECT id FROM users ORDER BY age LIMIT 3")
        expected = [dict(row) for row in cur.fetchall()]
        self.assertEqual([r["id"] for r in got], [r["id"] for r in expected])

    def test_index_scan_matches_seq_scan_semantics(self):
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        # equality
        self._compare(
            "SELECT id, name FROM users WHERE age = 25",
            "SELECT id, name FROM users WHERE age = 25",
            "id",
        )
        # range
        self._compare(
            "SELECT id, name FROM users WHERE age >= 25",
            "SELECT id, name FROM users WHERE age >= 25",
            "id",
        )
        # inequality
        self._compare(
            "SELECT id, name FROM users WHERE age != 25",
            "SELECT id, name FROM users WHERE age != 25",
            "id",
        )

    def test_index_scan_plus_extra_and_clause(self):
        # Only `age = 25` is index-eligible; `name = 'Dave'` must still be
        # applied as a Filter on top for correctness.
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        self._compare(
            "SELECT id, name FROM users WHERE age = 25 AND name = 'Dave'",
            "SELECT id, name FROM users WHERE age = 25 AND name = 'Dave'",
            "id",
        )

    def test_inner_join(self):
        got = self.engine.execute_sql(
            "SELECT * FROM users JOIN orders ON users.id = orders.user_id WHERE amount > 10"
        )
        cur = self.sqlite.execute(
            "SELECT users.id as uid, orders.amount as amt FROM users "
            "JOIN orders ON users.id = orders.user_id WHERE amount > 10"
        )
        expected = [dict(row) for row in cur.fetchall()]
        got_pairs = sorted((r["users.id"], r["orders.amount"]) for r in got)
        exp_pairs = sorted((r["uid"], r["amt"]) for r in expected)
        self.assertEqual(got_pairs, exp_pairs)

    def test_no_rows_match(self):
        self._compare(
            "SELECT id FROM users WHERE age > 999",
            "SELECT id FROM users WHERE age > 999",
            "id",
        )

    def test_update_seq_scan(self):
        self.engine.execute_sql("UPDATE users SET age = 99 WHERE name = 'Bob'")
        self.sqlite.execute("UPDATE users SET age = 99 WHERE name = 'Bob'")
        self._compare(
            "SELECT id, name, age FROM users",
            "SELECT id, name, age FROM users",
            "id",
        )

    def test_update_uses_index_and_stays_consistent(self):
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        self.engine.execute_sql("UPDATE users SET age = 100 WHERE age = 25")
        self.sqlite.execute("UPDATE users SET age = 100 WHERE age = 25")
        # old value should no longer be found (via the index path)
        self._compare(
            "SELECT id FROM users WHERE age = 25",
            "SELECT id FROM users WHERE age = 25",
            "id",
        )
        # new value should be found (via the index path)
        self._compare(
            "SELECT id FROM users WHERE age = 100",
            "SELECT id FROM users WHERE age = 100",
            "id",
        )
        # full table state should match too
        self._compare(
            "SELECT id, name, age FROM users",
            "SELECT id, name, age FROM users",
            "id",
        )

    def test_update_multiple_columns(self):
        self.engine.execute_sql("UPDATE users SET age = 50, name = 'Zed' WHERE id = 3")
        self.sqlite.execute("UPDATE users SET age = 50, name = 'Zed' WHERE id = 3")
        self._compare(
            "SELECT id, name, age FROM users",
            "SELECT id, name, age FROM users",
            "id",
        )

    def test_update_no_where_affects_all_rows(self):
        n = self.engine.execute_sql("UPDATE users SET age = 0")
        self.sqlite.execute("UPDATE users SET age = 0")
        self.assertEqual(n, 6)
        self._compare(
            "SELECT id, age FROM users",
            "SELECT id, age FROM users",
            "id",
        )

    def test_delete_seq_scan(self):
        n = self.engine.execute_sql("DELETE FROM users WHERE age < 25")
        cur = self.sqlite.execute("SELECT COUNT(*) as c FROM users WHERE age < 25")
        expected_n = cur.fetchone()["c"]
        self.sqlite.execute("DELETE FROM users WHERE age < 25")
        self.assertEqual(n, expected_n)
        self._compare(
            "SELECT id, name, age FROM users",
            "SELECT id, name, age FROM users",
            "id",
        )

    def test_delete_uses_index_and_stays_consistent(self):
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        self.engine.execute_sql("DELETE FROM users WHERE age = 25")
        self.sqlite.execute("DELETE FROM users WHERE age = 25")
        self._compare(
            "SELECT id, name FROM users",
            "SELECT id, name FROM users",
            "id",
        )
        # deleted rows must not resurface via a stale index entry
        self._compare(
            "SELECT id FROM users WHERE age = 25",
            "SELECT id FROM users WHERE age = 25",
            "id",
        )

    def test_delete_all_rows(self):
        n = self.engine.execute_sql("DELETE FROM users")
        self.assertEqual(n, 6)
        self.assertEqual(self.engine.execute_sql("SELECT id FROM users"), [])

    def test_update_then_reselect_via_seq_scan_matches_index_scan(self):
        # Cross-check MiniSQL against itself: the same query pre- and
        # post-index-creation must return identical rows after a mutation,
        # since the planner's choice of scan strategy must never change
        # query semantics, only speed.
        self.engine.execute_sql("UPDATE users SET age = 77 WHERE name = 'Eve'")
        no_index_result = sorted(
            self.engine.execute_sql("SELECT id FROM users WHERE age = 77"),
            key=lambda r: r["id"],
        )
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        with_index_result = sorted(
            self.engine.execute_sql("SELECT id FROM users WHERE age = 77"),
            key=lambda r: r["id"],
        )
        self.assertEqual(no_index_result, with_index_result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
