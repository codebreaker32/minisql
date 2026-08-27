"""
btree.py — B-tree index, implemented from scratch.

This is a classic in-memory B-tree of minimum degree `t` (each node except
the root has between t-1 and 2t-1 entries). It maps an indexed column
value -> rowids (supporting non-unique indexes, e.g. an index on `age`
where many rows share the same age).

Entries are (key, rowid) pairs, kept sorted first by key then by rowid, so
duplicate keys are just adjacent entries rather than requiring special-cased
buckets. This is the standard technique real B-tree indexes use to support
non-unique columns without a separate data structure.

NULL handling: keys are stored in a normalized form, (0, 0) for NULL and
(1, value) otherwise, so NULLs sort before every real value and never raise
a TypeError. `search(None)` finds NULL entries (used by nothing in SQL, but
it keeps the structure total); `range_search` never yields NULL keys, which
matches SQL semantics where `NULL < x` is unknown rather than true.

Design note vs. production B+ trees: real databases (Postgres, InnoDB) use
B+ trees — data lives only in leaves, and leaves are linked in a list so a
range scan is a straight leaf-to-leaf walk without touching internal nodes.
This implementation is a plain B-tree (data can live in internal nodes too),
and range_search is a pruned in-order traversal: a child is descended into
only if the separator keys around it allow it to contain a key in
[low, high]. That gives O(log n + k) range scans, same as a B+tree, just
with a larger constant.
"""

from __future__ import annotations
import bisect


def _norm(key):
    return (0, 0) if key is None else (1, key)


def _denorm(nk):
    return None if nk[0] == 0 else nk[1]


class _Node:
    __slots__ = ("leaf", "entries", "children")

    def __init__(self, leaf: bool):
        self.leaf = leaf
        self.entries: list[tuple] = []      # sorted list of (normalized key, rowid)
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
        entry = (_norm(key), rowid)
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
        self._search(self.root, _norm(key), result)
        return result

    def _search(self, node: _Node, nkey, out: list) -> None:
        keys_only = [e[0] for e in node.entries]
        lo = bisect.bisect_left(keys_only, nkey)
        hi = bisect.bisect_right(keys_only, nkey)
        out.extend(rowid for _k, rowid in node.entries[lo:hi])
        if not node.leaf:
            # Every child adjacent to or between the matching entries can
            # also hold more equal keys (duplicates are only ordered by the
            # tie-breaking rowid, not grouped), so all of children[lo..hi]
            # must be visited, not just the two boundary children.
            for i in range(lo, hi + 1):
                self._search(node.children[i], nkey, out)

    # ---------------- range search ----------------

    def range_search(self, low=None, high=None, low_inclusive=True, high_inclusive=True):
        """Yield rowids for all non-NULL keys in [low, high] (a bound of
        None means unbounded on that side)."""
        nlow = None if low is None else _norm(low)
        nhigh = None if high is None else _norm(high)
        yield from self._range(self.root, nlow, nhigh, low_inclusive, high_inclusive)

    @staticmethod
    def _below_low(nkey, nlow, low_inc) -> bool:
        """True if nkey is too small to be in range."""
        if nlow is None:
            return False
        return nkey < nlow if low_inc else nkey <= nlow

    @staticmethod
    def _above_high(nkey, nhigh, high_inc) -> bool:
        """True if nkey is too large to be in range."""
        if nhigh is None:
            return False
        return nkey > nhigh if high_inc else nkey >= nhigh

    def _in_range(self, nkey, nlow, nhigh, low_inc, high_inc) -> bool:
        if nkey[0] == 0:            # NULL never satisfies a comparison
            return False
        return not self._below_low(nkey, nlow, low_inc) and \
               not self._above_high(nkey, nhigh, high_inc)

    def _range(self, node: _Node, nlow, nhigh, low_inc, high_inc):
        # In-order traversal with pruning. Child i sits between
        # entries[i-1] and entries[i], so it can only hold matching keys if
        # entries[i] isn't already below the low bound and entries[i-1]
        # isn't already above the high bound.
        n = len(node.entries)
        for i in range(n + 1):
            if not node.leaf:
                sep_right = node.entries[i][0] if i < n else None
                sep_left = node.entries[i - 1][0] if i > 0 else None
                skip = (sep_right is not None and self._below_low(sep_right, nlow, low_inc)) or \
                       (sep_left is not None and self._above_high(sep_left, nhigh, high_inc))
                if not skip:
                    yield from self._range(node.children[i], nlow, nhigh, low_inc, high_inc)
            if i < n:
                nkey, rowid = node.entries[i]
                if self._in_range(nkey, nlow, nhigh, low_inc, high_inc):
                    yield rowid
                elif self._above_high(nkey, nhigh, high_inc):
                    return   # sorted order: nothing further in this node can match

    # ---------------- traversal (debugging / tests) ----------------

    def inorder(self):
        """Yield (key, rowid) in sorted order, NULL keys first."""
        for nkey, rowid in self._inorder(self.root):
            yield _denorm(nkey), rowid

    def _inorder(self, node: _Node):
        if node.leaf:
            for entry in node.entries:
                yield entry
            return
        for i, entry in enumerate(node.entries):
            yield from self._inorder(node.children[i])
            yield entry
        yield from self._inorder(node.children[-1])
