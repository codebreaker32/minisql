"""
test_btree.py — Unit tests for the B-tree in isolation (no SQL layer involved).
"""

import os
import random
import sys
import unittest
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minisql.storage.btree import BTree


class BTreeTest(unittest.TestCase):
    def test_insert_and_point_search_unique_keys(self):
        bt = BTree(min_degree=4)
        for i in range(1000):
            bt.insert(i, rowid=i * 10)
        self.assertEqual(len(bt), 1000)
        for i in range(0, 1000, 37):
            self.assertEqual(bt.search(i), [i * 10])
        self.assertEqual(bt.search(99999), [])

    def test_duplicate_keys(self):
        bt = BTree(min_degree=3)
        pairs = [(5, 1), (5, 2), (5, 3), (3, 4), (3, 5), (7, 6)]
        for k, rid in pairs:
            bt.insert(k, rid)
        self.assertEqual(sorted(bt.search(5)), [1, 2, 3])
        self.assertEqual(sorted(bt.search(3)), [4, 5])
        self.assertEqual(sorted(bt.search(7)), [6])

    def test_random_stress_matches_brute_force(self):
        random.seed(7)
        bt = BTree(min_degree=3)
        expected = defaultdict(list)
        for i in range(4000):
            k = random.randint(0, 300)
            bt.insert(k, i)
            expected[k].append(i)
        for k in expected:
            self.assertEqual(sorted(bt.search(k)), sorted(expected[k]))

    def test_range_search_bounds(self):
        bt = BTree(min_degree=5)
        for i in range(500):
            bt.insert(i, i)
        self.assertEqual(sorted(bt.range_search(10, 20)), list(range(10, 21)))
        self.assertEqual(
            sorted(bt.range_search(10, 20, low_inclusive=False, high_inclusive=False)),
            list(range(11, 20)),
        )
        self.assertEqual(sorted(bt.range_search(low=495)), list(range(495, 500)))
        self.assertEqual(sorted(bt.range_search(high=4)), list(range(0, 5)))

    def test_inorder_is_sorted(self):
        random.seed(1)
        bt = BTree(min_degree=2)  # smallest legal degree -> lots of splitting
        keys = [random.randint(0, 100) for _ in range(500)]
        for i, k in enumerate(keys):
            bt.insert(k, i)
        result_keys = [k for k, _ in bt.inorder()]
        self.assertEqual(result_keys, sorted(result_keys))
        self.assertEqual(len(result_keys), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
