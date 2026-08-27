"""
test_constraints_and_recovery.py — Tests for the "make it a real database"
layer: constraint enforcement (types, PRIMARY KEY, NOT NULL), SQL NULL
semantics, name resolution (unknown / ambiguous columns, reserved words),
index selection under joins, statement-level atomicity, and crash recovery
from the on-disk undo journal.
"""

import os
import shutil
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minisql.engine import Engine
from minisql.parser import ParseError
from minisql.storage.btree import BTree
from minisql.storage.heap import HeapFile
from minisql.storage.journal import UndoJournal

TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "_tmp_constraints_test_data")


class _Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.row_factory = sqlite3.Row
        for ddl in (
            "CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)",
            "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount REAL)",
        ):
            self.engine.execute_sql(ddl)
            self.sqlite.execute(ddl)
        self.both("INSERT INTO users VALUES (1, 'Alice', 30)")
        self.both("INSERT INTO users VALUES (2, 'Bob', 25)")
        self.both("INSERT INTO users VALUES (3, 'Carol', NULL)")
        self.both("INSERT INTO users VALUES (4, NULL, 25)")
        self.both("INSERT INTO orders VALUES (100, 1, 49.99)")
        self.both("INSERT INTO orders VALUES (101, 2, 12.5)")
        self.both("INSERT INTO orders VALUES (102, 1, 5)")
        self.both("INSERT INTO orders VALUES (103, 9, 7.25)")

    def tearDown(self):
        self.engine.close()
        self.sqlite.close()
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)

    def both(self, sql):
        self.engine.execute_sql(sql)
        self.sqlite.execute(sql)

    def assert_same(self, sql, sqlite_sql=None, ordered=False):
        got = self.engine.execute_sql(sql)
        exp = [dict(r) for r in self.sqlite.execute(sqlite_sql or sql).fetchall()]
        got = [tuple(r.values()) for r in got]
        exp = [tuple(r.values()) for r in exp]
        if not ordered:
            got, exp = sorted(got, key=repr), sorted(exp, key=repr)
        self.assertEqual(got, exp)


class ConstraintTest(_Base):
    def test_duplicate_primary_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate PRIMARY KEY"):
            self.engine.execute_sql("INSERT INTO users VALUES (1, 'Dup', 99)")
        self.assertEqual(self.engine.execute_sql("SELECT COUNT(*) FROM users"), [{"COUNT(*)": 4}])

    def test_primary_key_reusable_after_delete(self):
        self.engine.execute_sql("DELETE FROM users WHERE id = 1")
        self.engine.execute_sql("INSERT INTO users VALUES (1, 'Again', 1)")  # stale index entry must not block
        self.assertEqual(self.engine.execute_sql("SELECT name FROM users WHERE id = 1"), [{"name": "Again"}])

    def test_primary_key_not_null(self):
        with self.assertRaisesRegex(ValueError, "cannot be NULL"):
            self.engine.execute_sql("INSERT INTO users VALUES (NULL, 'x', 1)")

    def test_update_to_duplicate_primary_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate PRIMARY KEY"):
            self.engine.execute_sql("UPDATE users SET id = 1 WHERE id = 2")
        self.assertEqual(sorted(r["id"] for r in self.engine.execute_sql("SELECT id FROM users")), [1, 2, 3, 4])

    def test_update_primary_key_to_itself_is_fine(self):
        self.assertEqual(self.engine.execute_sql("UPDATE users SET id = 1, name = 'A2' WHERE id = 1"), 1)
        self.assertEqual(self.engine.execute_sql("SELECT name FROM users WHERE id = 1"), [{"name": "A2"}])

    def test_type_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "is INT"):
            self.engine.execute_sql("INSERT INTO users VALUES (5, 'x', 'young')")
        with self.assertRaisesRegex(ValueError, "is TEXT"):
            self.engine.execute_sql("UPDATE users SET name = 42 WHERE id = 1")
        with self.assertRaisesRegex(ValueError, "is INT"):
            self.engine.execute_sql("INSERT INTO users VALUES (5, 'x', 1.5)")

    def test_int_widened_to_real(self):
        self.assert_same("SELECT id, amount FROM orders WHERE id = 102")
        self.assertIsInstance(self.engine.execute_sql("SELECT amount FROM orders WHERE id = 102")[0]["amount"], float)

    def test_insert_column_subset_fills_null(self):
        self.both("INSERT INTO users (id, name) VALUES (5, 'Dan')")
        self.assert_same("SELECT id, name, age FROM users WHERE id = 5")
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")   # NULL in the indexed column is fine
        self.assert_same("SELECT id FROM users WHERE age IS NULL")

    def test_unknown_column_errors(self):
        for sql in ("INSERT INTO users (id, nmae) VALUES (9, 'x')",
                    "SELECT nope FROM users",
                    "SELECT id FROM users WHERE nope = 1",
                    "UPDATE users SET nope = 1",
                    "DELETE FROM users WHERE nope = 1",
                    "SELECT id FROM users ORDER BY nope"):
            with self.assertRaisesRegex(ValueError, "not found|No such column", msg=sql):
                self.engine.execute_sql(sql)

    def test_multiple_primary_keys_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.execute_sql("CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)")


class NullSemanticsTest(_Base):
    def test_comparisons_exclude_null(self):
        self.assert_same("SELECT id FROM users WHERE age > 20")
        self.assert_same("SELECT id FROM users WHERE age = NULL")
        self.assert_same("SELECT id FROM users WHERE age != 25")
        self.assert_same("SELECT id FROM users WHERE name = 'Bob' OR age < 100")

    def test_is_null_and_is_not_null(self):
        self.assert_same("SELECT id FROM users WHERE age IS NULL")
        self.assert_same("SELECT id FROM users WHERE name IS NOT NULL AND age IS NOT NULL")

    def test_index_paths_respect_null(self):
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        self.assertIn("IndexScan", self.engine.execute_sql("EXPLAIN SELECT id FROM users WHERE age < 100"))
        self.assert_same("SELECT id FROM users WHERE age < 100")
        self.assert_same("SELECT id FROM users WHERE age >= 25")
        self.assert_same("SELECT id FROM users WHERE age = NULL")
        self.assertEqual(self.engine.execute_sql("DELETE FROM users WHERE age = NULL"), 0)

    def test_order_by_with_nulls_matches_sqlite(self):
        self.assert_same("SELECT id, age FROM users ORDER BY age", ordered=True)
        self.assert_same("SELECT id, age FROM users ORDER BY age DESC", ordered=True)

    def test_aggregates_skip_null(self):
        self.assert_same("SELECT COUNT(*), COUNT(age), SUM(age), AVG(age), MIN(age), MAX(age) FROM users")
        self.assert_same("SELECT age, COUNT(*) FROM users GROUP BY age")

    def test_join_never_matches_on_null(self):
        self.both("INSERT INTO orders VALUES (104, NULL, 1)")
        self.assert_same(
            "SELECT users.id, orders.id FROM users JOIN orders ON users.id = orders.user_id",
            "SELECT users.id AS uid, orders.id AS oid FROM users JOIN orders ON users.id = orders.user_id",
        )


class NameResolutionTest(_Base):
    def test_reserved_words_as_column_names(self):
        self.both("CREATE TABLE kv (key TEXT PRIMARY KEY, count INT, text TEXT)")
        self.both("INSERT INTO kv VALUES ('a', 1, 'x')")
        self.both("INSERT INTO kv VALUES ('b', 2, 'y')")
        self.assert_same("SELECT key, count FROM kv WHERE count > 1")
        self.assert_same("SELECT key, count, text FROM kv ORDER BY count DESC", ordered=True)
        self.assert_same("SELECT COUNT(*) FROM kv")

    def test_quoted_identifiers(self):
        self.both('CREATE TABLE "select" ("from" INT PRIMARY KEY)')
        self.both('INSERT INTO "select" VALUES (7)')
        self.assert_same('SELECT "from" FROM "select"')

    def test_ambiguous_column_in_join_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.engine.execute_sql("SELECT id FROM users JOIN orders ON users.id = orders.user_id")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.engine.execute_sql("SELECT users.id FROM users JOIN orders ON users.id = orders.user_id WHERE id = 1")

    def test_unambiguous_unqualified_columns_in_join(self):
        # `id` is in both tables and must be qualified; name/amount/user_id are not
        self.assert_same("SELECT name, amount FROM users JOIN orders ON users.id = user_id WHERE amount > 10")

    def test_join_requires_equality(self):
        with self.assertRaises(ParseError):
            self.engine.execute_sql("SELECT * FROM users JOIN orders ON users.id < orders.user_id")

    def test_index_used_under_join(self):
        self.engine.execute_sql("CREATE INDEX idx_amount ON orders (amount)")
        plan = self.engine.execute_sql(
            "EXPLAIN SELECT name, amount FROM users JOIN orders ON users.id = orders.user_id "
            "WHERE amount > 10 AND users.id = 1"
        )
        self.assertIn("IndexScan(table=users, id = 1)", plan)
        self.assertIn("IndexScan(table=orders, amount > 10)", plan)
        self.assertNotIn("Filter", plan)
        self.assert_same(
            "SELECT name, amount FROM users JOIN orders ON users.id = orders.user_id "
            "WHERE amount > 10 AND users.id = 1"
        )

    def test_not_equal_never_uses_index(self):
        self.engine.execute_sql("CREATE INDEX idx_age ON users (age)")
        self.assertIn("SeqScan", self.engine.execute_sql("EXPLAIN SELECT id FROM users WHERE age != 25"))

    def test_primary_key_lookup_uses_index(self):
        self.assertIn("IndexScan(table=users, id = 2)",
                      self.engine.execute_sql("EXPLAIN SELECT name FROM users WHERE id = 2"))


class AggregateSyntaxTest(_Base):
    def test_order_by_aggregate(self):
        self.assert_same("SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY COUNT(*) DESC")
        got = self.engine.execute_sql("SELECT user_id, SUM(amount) FROM orders GROUP BY user_id ORDER BY SUM(amount) DESC")
        self.assertEqual([r["user_id"] for r in got], [1, 2, 9])

    def test_group_by_without_aggregate_is_distinct(self):
        self.assert_same("SELECT age FROM users GROUP BY age")

    def test_order_by_non_output_column_in_aggregate_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.execute_sql("SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY name")


class AtomicityAndRecoveryTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.engine.execute_sql("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
        self.engine.execute_sql("INSERT INTO users VALUES (1, 'Alice', 30)")
        self.engine.execute_sql("INSERT INTO users VALUES (2, 'Bob', 25)")
        self.engine.execute_sql("INSERT INTO users VALUES (3, 'Carol', 25)")

    def tearDown(self):
        self.engine.close()
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)

    def snapshot(self, engine=None):
        engine = engine or self.engine
        return engine.execute_sql("SELECT id, name, age FROM users ORDER BY id")

    def test_failed_statement_is_fully_undone(self):
        before = self.snapshot()
        # Both rows with age 25 would get id 9: the second one violates the
        # PK, so the whole statement must be undone, including the first row.
        with self.assertRaisesRegex(ValueError, "Duplicate PRIMARY KEY"):
            self.engine.execute_sql("UPDATE users SET id = 9 WHERE age = 25")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.engine.execute_sql("SELECT id FROM users WHERE id = 9"), [])

    def test_failed_statement_inside_transaction_keeps_earlier_work(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (4, 'Dan', 40)")
        with self.assertRaises(ValueError):
            # Bob (2) -> 9 is applied, then Carol (3) -> 9 duplicates: the
            # statement must undo Bob's change but leave Dan's insert alone.
            self.engine.execute_sql("UPDATE users SET id = 9 WHERE age = 25")
        self.assertEqual([r["id"] for r in self.snapshot()], [1, 2, 3, 4])   # Bob restored, Dan survived
        self.assertEqual(self.engine.execute_sql("SELECT name FROM users WHERE id = 2"), [{"name": "Bob"}])
        self.engine.execute_sql("ROLLBACK")
        self.assertEqual([r["id"] for r in self.snapshot()], [1, 2, 3])

    def test_uncommitted_transaction_is_undone_on_restart(self):
        before = self.snapshot()
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (4, 'Dan', 40)")
        self.engine.execute_sql("DELETE FROM users WHERE id = 2")
        self.engine.execute_sql("UPDATE users SET age = 99 WHERE id = 1")
        self.assertNotEqual(self.snapshot(), before)
        # Simulate a crash: drop the engine without COMMIT/ROLLBACK, reopen.
        self.engine.close()
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.engine._journal.entries(), [])
        # and the recovered database is fully usable, index included
        self.engine.execute_sql("INSERT INTO users VALUES (4, 'Dan', 40)")
        self.assertEqual(self.engine.execute_sql("SELECT name FROM users WHERE id = 4"), [{"name": "Dan"}])

    def test_torn_write_is_discarded_on_restart(self):
        before = self.snapshot()
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (4, 'Dan', 40)")
        self.engine.close()
        # Simulate the process dying halfway through the heap append: chop
        # the last few bytes off the record the journal knows about.
        path = os.path.join(TEST_DATA_DIR, "users.tbl")
        with open(path, "r+b") as f:
            f.truncate(os.path.getsize(path) - 3)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.snapshot(), before)

    def _record_sync_order(self):
        """Wrap HeapFile.sync and UndoJournal.rewrite so the order in which
        they run is observable, without changing what they do."""
        events = []
        real_sync, real_rewrite = HeapFile.sync, UndoJournal.rewrite

        def sync(heap):
            events.append("heap-fsync")
            real_sync(heap)

        def rewrite(journal, entries):
            events.append("journal-rewrite")
            real_rewrite(journal, entries)

        patches = [mock.patch.object(HeapFile, "sync", sync),
                   mock.patch.object(UndoJournal, "rewrite", rewrite)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return events

    def _assert_heap_synced_before_journal(self, events):
        # The journal may only stop describing an undo once that undo is on
        # disk: every journal rewrite must be preceded by a heap fsync.
        self.assertIn("journal-rewrite", events)
        self.assertIn("heap-fsync", events)
        self.assertLess(events.index("heap-fsync"), events.index("journal-rewrite"),
                        f"journal was rewritten before the heap was fsync'd: {events}")

    def test_failed_statement_syncs_heap_before_shrinking_journal(self):
        events = self._record_sync_order()
        with self.assertRaises(ValueError):
            self.engine.execute_sql("UPDATE users SET id = 9 WHERE age = 25")
        self._assert_heap_synced_before_journal(events)

    def test_explicit_rollback_syncs_heap_before_clearing_journal(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM users WHERE id = 2")
        events = self._record_sync_order()
        self.engine.execute_sql("ROLLBACK")
        self._assert_heap_synced_before_journal(events)

    def test_committed_data_survives_restart(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO users VALUES (4, 'Dan', 40)")
        self.engine.execute_sql("COMMIT")
        after = self.snapshot()
        self.engine.close()
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.snapshot(), after)


class BTreeNullAndPruningTest(unittest.TestCase):
    def test_null_keys_sort_first_and_are_excluded_from_ranges(self):
        bt = BTree(min_degree=2)
        for i, k in enumerate([5, None, 3, None, 8]):
            bt.insert(k, i)
        self.assertEqual([k for k, _ in bt.inorder()], [None, None, 3, 5, 8])
        self.assertEqual(sorted(bt.search(None)), [1, 3])
        self.assertEqual(sorted(bt.range_search(high=100)), [0, 2, 4])
        self.assertEqual(sorted(bt.range_search(low=4)), [0, 4])

    def test_range_search_prunes_subtrees(self):
        bt = BTree(min_degree=3)
        for i in range(10000):
            bt.insert(i, i)
        visited = []
        original = bt._range

        def counting(node, *args):
            visited.append(node)
            return original(node, *args)

        bt._range = counting
        self.assertEqual(sorted(bt.range_search(low=9990)), list(range(9990, 10000)))
        low_only = len(visited)
        visited.clear()
        self.assertEqual(sorted(bt.range_search(high=9)), list(range(0, 10)))
        high_only = len(visited)
        visited.clear()
        self.assertEqual(sorted(bt.range_search(5000, 5010)), list(range(5000, 5011)))
        both = len(visited)
        # A 10k-key tree at t=3 has thousands of nodes; a tight range should
        # touch only a path's worth of them.
        for n in (low_only, high_only, both):
            self.assertLess(n, 40, f"visited {n} nodes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
