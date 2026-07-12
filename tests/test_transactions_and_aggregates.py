"""
test_transactions_and_aggregates.py — Tests for BEGIN/COMMIT/ROLLBACK and
COUNT/SUM/AVG/MIN/MAX/GROUP BY, including cross-checks against SQLite where
applicable.
"""

import os
import shutil
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minisql.engine import Engine

TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "_tmp_tx_test_data")


class TransactionTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.engine.execute_sql("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
        self.engine.execute_sql("INSERT INTO users VALUES (1, 'Alice', 30)")
        self.engine.execute_sql("INSERT INTO users VALUES (2, 'Bob', 25)")

    def tearDown(self):
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)

    def _ids(self):
        return sorted(r["id"] for r in self.engine.execute_sql("SELECT id FROM users"))

    def test_commit_persists_insert(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (3, 'Carol', 40)")
        self.engine.execute_sql("COMMIT")
        self.assertEqual(self._ids(), [1, 2, 3])

    def test_rollback_undoes_insert(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (3, 'Carol', 40)")
        self.assertEqual(self._ids(), [1, 2, 3])  # visible mid-transaction
        self.engine.execute_sql("ROLLBACK")
        self.assertEqual(self._ids(), [1, 2])

    def test_rollback_undoes_delete(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM users WHERE id = 2")
        self.assertEqual(self._ids(), [1])
        self.engine.execute_sql("ROLLBACK")
        self.assertEqual(self._ids(), [1, 2])
        row = self.engine.execute_sql("SELECT name, age FROM users WHERE id = 2")
        self.assertEqual(row, [{"name": "Bob", "age": 25}])

    def test_rollback_undoes_update_and_stays_index_consistent(self):
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("UPDATE users SET age = 99 WHERE id = 1")
        self.assertEqual(
            self.engine.execute_sql("SELECT id FROM users WHERE age = 99"),
            [{"id": 1}],
        )
        self.engine.execute_sql("ROLLBACK")
        self.assertEqual(self.engine.execute_sql("SELECT id FROM users WHERE age = 99"), [])
        self.assertEqual(
            self.engine.execute_sql("SELECT id FROM users WHERE age = 30"),
            [{"id": 1}],
        )

    def test_multiple_statements_roll_back_together(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (3, 'Carol', 40)")
        self.engine.execute_sql("DELETE FROM users WHERE id = 2")
        self.engine.execute_sql("UPDATE users SET age = 100 WHERE id = 1")
        self.engine.execute_sql("ROLLBACK")
        # Every statement in the transaction must be undone, not just the last one.
        result = self.engine.execute_sql("SELECT id, name, age FROM users ORDER BY id")
        self.assertEqual(result, [{"id": 1, "name": "Alice", "age": 30},
                                   {"id": 2, "name": "Bob", "age": 25}])

    def test_commit_without_begin_raises(self):
        with self.assertRaises(ValueError):
            self.engine.execute_sql("COMMIT")

    def test_rollback_without_begin_raises(self):
        with self.assertRaises(ValueError):
            self.engine.execute_sql("ROLLBACK")

    def test_nested_begin_raises(self):
        self.engine.execute_sql("BEGIN")
        with self.assertRaises(ValueError):
            self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("ROLLBACK")  # clean up

    def test_statements_outside_transaction_are_immediate(self):
        # No BEGIN: every statement should take effect right away (autocommit).
        self.engine.execute_sql("INSERT INTO users VALUES (3, 'Carol', 40)")
        self.assertEqual(self._ids(), [1, 2, 3])


class AggregateTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.row_factory = sqlite3.Row
        self.engine.execute_sql("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
        self.sqlite.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
        users = [(1, "Alice", 30), (2, "Bob", 25), (3, "Carol", 35),
                  (4, "Dave", 25), (5, "Eve", 41)]
        for u in users:
            self.engine.execute_sql(f"INSERT INTO users VALUES ({u[0]}, '{u[1]}', {u[2]})")
            self.sqlite.execute("INSERT INTO users VALUES (?, ?, ?)", u)
        self.sqlite.commit()

    def tearDown(self):
        self.sqlite.close()
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)

    def test_count_star(self):
        got = self.engine.execute_sql("SELECT COUNT(*) FROM users")
        self.assertEqual(got, [{"COUNT(*)": 5}])

    def test_count_with_filter(self):
        got = self.engine.execute_sql("SELECT COUNT(*) FROM users WHERE age > 26")
        exp = self.sqlite.execute("SELECT COUNT(*) as c FROM users WHERE age > 26").fetchone()["c"]
        self.assertEqual(got[0]["COUNT(*)"], exp)

    def test_sum_avg_min_max(self):
        got = self.engine.execute_sql("SELECT SUM(age), AVG(age), MIN(age), MAX(age) FROM users")[0]
        row = self.sqlite.execute(
            "SELECT SUM(age) as s, AVG(age) as a, MIN(age) as mn, MAX(age) as mx FROM users"
        ).fetchone()
        self.assertEqual(got["SUM(age)"], row["s"])
        self.assertAlmostEqual(got["AVG(age)"], row["a"])
        self.assertEqual(got["MIN(age)"], row["mn"])
        self.assertEqual(got["MAX(age)"], row["mx"])

    def test_group_by(self):
        got = self.engine.execute_sql("SELECT age, COUNT(*) FROM users GROUP BY age")
        got_map = {r["age"]: r["COUNT(*)"] for r in got}
        rows = self.sqlite.execute(
            "SELECT age, COUNT(*) as c FROM users GROUP BY age"
        ).fetchall()
        exp_map = {r["age"]: r["c"] for r in rows}
        self.assertEqual(got_map, exp_map)

    def test_aggregate_on_empty_result_set(self):
        got = self.engine.execute_sql("SELECT COUNT(*), SUM(age) FROM users WHERE age > 999")
        self.assertEqual(got, [{"COUNT(*)": 0, "SUM(age)": None}])

    def test_group_by_with_order_and_limit(self):
        got = self.engine.execute_sql(
            "SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY age DESC LIMIT 2"
        )
        ages = [r["age"] for r in got]
        self.assertEqual(ages, sorted(set(u[2] for u in [(1, "", 30), (2, "", 25), (3, "", 35), (4, "", 25), (5, "", 41)]), reverse=True)[:2])

    def test_non_grouped_plain_column_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.execute_sql("SELECT name, COUNT(*) FROM users GROUP BY age")


if __name__ == "__main__":
    unittest.main(verbosity=2)
