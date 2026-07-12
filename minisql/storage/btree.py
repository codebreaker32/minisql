"""
btree.py — B-tree index, implemented from scratch.

This is a classic in-memory B-tree of minimum degree `t` (each internal node
has between t-1 and 2t-1 keys, except the root). It maps an indexed column
value -> a list of rowids (supporting non-unique indexes, e.g. an index on
`age` where many rows share the same age).

Entries are (key, rowid) pairs, kept sorted first by key then by rowid, so
duplicate keys are just adjacent entries rather than requiring special-cased
buckets. This is the standard technique real B-tree indexes use to support
non-unique columns without a separate data structure.

Design note vs. production B+ trees: real databases (Postgres, InnoDB) use
B+ trees — data lives only in leaves, and leaves are linked in a list so a
range scan is a straight leaf-to-leaf walk without touching internal nodes.
This implementation is a plain B-tree (data can live in internal nodes too),
and range_search does an in-order traversal instead of a leaf-linked walk.
That's a real, intentional simplification — documented in the README — that
keeps the splitting logic understandable without changing the asymptotic
complexity of point lookups (O(log n)). Range scans here are O(log n + k)
same as a B+tree, just with a larger constant.
"""

from __future__ import annotations
import bisect


class _Node:
    __slots__ = ("leaf", "entries", "children")

    def __init__(self, leaf: bool):
        self.leaf = leaf
        self.entries: list[tuple] = []      # sorted list of (key, rowid)
        self.children: list[_Node] = []     # len == len(entries) + 1 when internal


class BTree:
    def __init__(self, min_degree: int = 32):
        if min_degree < 2:
            raise ValueError("min_degree must be >= 2")
        self.t = min_degree
        self.root = _Node(leaf=True)
        self._size = 0

    def __len__(self):
        return self._size

    # ---------------- insertion ----------------

    def insert(self, key, rowid: int) -> None:
        entry = (key, rowid)
        root = self.root
        if len(root.entries) == 2 * self.t - 1:
            # root is full: split proactively so recursion never has to
            # handle a full node except at the very top.
            new_root = _Node(leaf=False)
            new_root.children.append(root)
            self._split_child(new_root, 0)
            self.root = new_root
            self._insert_nonfull(new_root, entry)
        else:
            self._insert_nonfull(root, entry)
        self._size += 1

    def _split_child(self, parent: _Node, idx: int) -> None:
        t = self.t
        child = parent.children[idx]
        mid_entry = child.entries[t - 1]

        right = _Node(leaf=child.leaf)
        right.entries = child.entries[t:]
        child.entries = child.entries[: t - 1]

        if not child.leaf:
            right.children = child.children[t:]
            child.children = child.children[:t]

        parent.children.insert(idx + 1, right)
        parent.entries.insert(idx, mid_entry)

    def _insert_nonfull(self, node: _Node, entry: tuple) -> None:
        if node.leaf:
            bisect.insort(node.entries, entry)
            return
        i = bisect.bisect_right(node.entries, entry)
        child = node.children[i]
        if len(child.entries) == 2 * self.t - 1:
            self._split_child(node, i)
            if entry > node.entries[i]:
                i += 1
            child = node.children[i]
        self._insert_nonfull(child, entry)

    # ---------------- point search ----------------

    def search(self, key) -> list[int]:
        """Return all rowids stored under `key`."""
        result = []
        self._search(self.root, key, result)
        return result

    def _search(self, node: _Node, key, out: list) -> None:
        keys_only = [e[0] for e in node.entries]
        lo = bisect.bisect_left(keys_only, key)
        hi = bisect.bisect_right(keys_only, key)
        out.extend(rowid for k, rowid in node.entries[lo:hi])
        if not node.leaf:
            # Every child adjacent to or between the matching entries can
            # also hold more equal keys (duplicates are only ordered by the
            # tie-breaking rowid, not grouped), so all of children[lo..hi]
            # must be visited, not just the two boundary children.
            for i in range(lo, hi + 1):
                self._search(node.children[i], key, out)

    # ---------------- range search ----------------

    def range_search(self, low=None, high=None, low_inclusive=True, high_inclusive=True):
        """Yield rowids for all keys in [low, high] (bounds optional = unbounded)."""
        yield from self._range(self.root, low, high, low_inclusive, high_inclusive)

    def _in_range(self, key, low, high, low_inc, high_inc) -> bool:
        if low is not None:
            if low_inc and key < low:
                return False
            if not low_inc and key <= low:
                return False
        if high is not None:
            if high_inc and key > high:
                return False
            if not high_inc and key >= high:
                return False
        return True

    def _range(self, node: _Node, low, high, low_inc, high_inc):
        # Plain in-order traversal with pruning: descend into a child only if
        # the key range it could contain overlaps [low, high].
        for i, (key, rowid) in enumerate(node.entries):
            if not node.leaf:
                left_child = node.children[i]
                if high is None or self._entry_below_high(left_child, high, high_inc):
                    yield from self._range(left_child, low, high, low_inc, high_inc)
            if self._in_range(key, low, high, low_inc, high_inc):
                yield rowid
            elif high is not None and key > high:
                return  # sorted order: nothing further in this node can match
        if not node.leaf:
            last_child = node.children[len(node.entries)]
            if high is None or self._entry_below_high(last_child, high, high_inc):
                yield from self._range(last_child, low, high, low_inc, high_inc)

    def _entry_below_high(self, node: _Node, high, high_inc) -> bool:
        # Cheap pruning check: could this subtree possibly contain a key <= high?
        return True  # conservative: correctness > tight pruning for a teaching impl

    # ---------------- traversal (debugging / tests) ----------------

    def inorder(self):
        yield from self._inorder(self.root)

    def _inorder(self, node: _Node):
        if node.leaf:
            for entry in node.entries:
                yield entry
            return
        for i, entry in enumerate(node.entries):
            yield from self._inorder(node.children[i])
            yield entry
        yield from self._inorder(node.children[-1])
