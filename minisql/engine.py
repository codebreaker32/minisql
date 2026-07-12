"""
engine.py — Top-level entry point: Engine.execute_sql(sql) -> rows.

Wires everything together:
  text -> lexer/parser -> AST -> (DDL/DML dispatch, or planner -> executor)

Indexes are kept in memory (BTree objects) and rebuilt by scanning the heap
file the first time a table/column's index is needed after a fresh process
start; this is a common trade-off in small/embedded databases (SQLite's
in-memory temp indexes work similarly) — it avoids having to serialize a
whole tree data structure to disk on every insert.
"""

from __future__ import annotations
from .parser import parse
from .ast_nodes import (
    CreateTableStmt, CreateIndexStmt, InsertStmt, SelectStmt, ExplainStmt,
    UpdateStmt, DeleteStmt, TransactionStmt, BinOp,
)
from .catalog import Catalog
from .storage.heap import HeapFile
from .storage.btree import BTree
from .planner import build_plan, explain as explain_plan, _split_conjuncts, _is_indexable
from .executor import execute, _eval, _qualify


class Engine:
    def __init__(self, data_dir: str = "data"):
        self.catalog = Catalog(data_dir)
        self._heaps: dict[str, HeapFile] = {}
        self._indexes: dict[tuple[str, str], BTree] = {}
        self._in_transaction = False
        # Undo log for the current transaction: list of ("insert", table, rowid)
        # or ("delete", table, rowid). Rolling back replays these in reverse.
        # This is a small, real instance of undo-log-based atomicity — one of
        # the two classical techniques (the other being shadow paging) real
        # databases use to implement ROLLBACK.
        self._undo_log: list[tuple[str, str, int]] = []

    # ---------------- storage accessors ----------------

    def get_heap(self, table: str) -> HeapFile:
        if table not in self._heaps:
            self._heaps[table] = HeapFile(self.catalog.heap_path(table))
        return self._heaps[table]

    def get_index(self, table: str, column: str) -> BTree:
        key = (table, column)
        if key not in self._indexes:
            bt = BTree(min_degree=32)
            heap = self.get_heap(table)
            for rowid, row in heap.scan():
                bt.insert(row[column], rowid)
            self._indexes[key] = bt
        return self._indexes[key]

    def _update_indexes_on_insert(self, table: str, row: dict, rowid: int) -> None:
        schema = self.catalog.get_table(table)
        for col in schema.indexes:
            key = (table, col)
            if key in self._indexes:  # only touch already-loaded indexes
                self._indexes[key].insert(row[col], rowid)

    def _do_insert(self, table: str, heap: HeapFile, row: dict) -> int:
        """Insert a row and, if inside a transaction, log how to undo it.
        Every INSERT and every UPDATE's "insert the new version" step goes
        through here so rollback has no blind spots."""
        rowid = heap.insert(row)
        self._update_indexes_on_insert(table, row, rowid)
        if self._in_transaction:
            self._undo_log.append(("insert", table, rowid))
        return rowid

    def _do_delete(self, table: str, heap: HeapFile, rowid: int) -> None:
        """Tombstone a row and, if inside a transaction, log how to undo it.
        Note: we deliberately do NOT remove the row's entry from any loaded
        B-tree index here (this BTree has no delete operation — see
        README). That's what makes rollback's index story simple: since the
        index was never touched, resurrecting a row via undelete() makes it
        immediately findable via the index again, with zero extra code."""
        heap.delete(rowid)
        if self._in_transaction:
            self._undo_log.append(("delete", table, rowid))

    def _rollback(self) -> None:
        for action, table, rowid in reversed(self._undo_log):
            heap = self.get_heap(table)
            if action == "insert":
                heap.delete(rowid)      # tombstone the row this transaction inserted
            elif action == "delete":
                heap.undelete(rowid)    # resurrect the row this transaction deleted

    def _match_rows(self, table: str, where):
        """Yield (rowid, row) for every live row matching `where` (or every
        row if `where` is None). Reuses the same index-eligibility logic as
        the SELECT planner, so `UPDATE ... WHERE indexed_col = x` is just as
        fast as the equivalent SELECT — it does not fall back to a full
        table scan just because it's a mutation rather than a read."""
        heap = self.get_heap(table)
        if where is None:
            yield from heap.scan()
            return

        conjuncts = _split_conjuncts(where)
        chosen = None
        for c in conjuncts:
            hit = _is_indexable(c, table, self.catalog)
            if hit is not None:
                chosen = (c, hit)
                break

        if chosen is None:
            for rowid, row in heap.scan():
                if _eval(where, _qualify(table, row)):
                    yield rowid, row
            return

        used_conjunct, (col, op, val) = chosen
        index = self.get_index(table, col)
        if op == "=":
            rowids = index.search(val)
        elif op in (">", ">="):
            rowids = index.range_search(low=val, low_inclusive=(op == ">="))
        elif op in ("<", "<="):
            rowids = index.range_search(high=val, high_inclusive=(op == "<="))
        else:  # !=
            rowids = (rid for k, rid in index.inorder() if k != val)

        remaining = None
        for c in conjuncts:
            if c is not used_conjunct:
                remaining = c if remaining is None else BinOp(remaining, "AND", c)

        for rowid in rowids:
            row = heap.read(rowid)
            if row is None:            # stale index entry pointing at a
                continue                # tombstoned/updated-away row
            if remaining is None or _eval(remaining, _qualify(table, row)):
                yield rowid, row

    # ---------------- statement dispatch ----------------

    def execute_sql(self, sql: str):
        """Returns:
          - list[dict] of result rows for SELECT
          - str for EXPLAIN
          - int (rows affected) for UPDATE/DELETE
          - None for DDL/INSERT/BEGIN/COMMIT/ROLLBACK, which mutate state
        """
        stmt = parse(sql)

        if isinstance(stmt, CreateTableStmt):
            # DDL is always immediate/autocommit, even inside a transaction
            # block — the same simplification MySQL historically made for
            # many DDL statements (Postgres, notably, does NOT make this
            # simplification and supports fully transactional DDL).
            cols = [{"name": c.name, "type": c.type, "primary_key": c.primary_key}
                    for c in stmt.columns]
            self.catalog.create_table(stmt.table_name, cols)
            return None

        if isinstance(stmt, CreateIndexStmt):
            self.catalog.add_index(stmt.table_name, stmt.column)
            self.get_index(stmt.table_name, stmt.column)  # build it now
            return None

        if isinstance(stmt, TransactionStmt):
            if stmt.kind == "BEGIN":
                if self._in_transaction:
                    raise ValueError("Already inside a transaction (nested transactions not supported)")
                self._in_transaction = True
                self._undo_log = []
                return None
            if stmt.kind == "COMMIT":
                if not self._in_transaction:
                    raise ValueError("No transaction is in progress")
                self._in_transaction = False
                self._undo_log = []
                return None
            if stmt.kind == "ROLLBACK":
                if not self._in_transaction:
                    raise ValueError("No transaction is in progress")
                self._rollback()
                self._in_transaction = False
                self._undo_log = []
                return None

        if isinstance(stmt, InsertStmt):
            schema = self.catalog.get_table(stmt.table_name)
            col_names = stmt.columns or schema.column_names()
            if len(col_names) != len(stmt.values):
                raise ValueError("Column count does not match value count")
            row = dict(zip(col_names, stmt.values))
            heap = self.get_heap(stmt.table_name)
            self._do_insert(stmt.table_name, heap, row)
            return None

        if isinstance(stmt, ExplainStmt):
            plan = build_plan(stmt.inner, self.catalog)
            return explain_plan(plan)

        if isinstance(stmt, UpdateStmt):
            schema = self.catalog.get_table(stmt.table_name)
            for col in stmt.assignments:
                if col not in schema.column_names():
                    raise ValueError(f"Column {col!r} not found on table {stmt.table_name!r}")
            heap = self.get_heap(stmt.table_name)
            # Materialize matches first: we're about to mutate the heap file
            # (tombstone + append) while iterating, and that must not affect
            # which rows this statement considers "matched".
            matches = list(self._match_rows(stmt.table_name, stmt.where))
            count = 0
            for rowid, row in matches:
                new_row = {**row, **stmt.assignments}
                # MVCC-style update: never edit bytes in place. Tombstone the
                # old row version and append the new one as a fresh row with
                # a fresh rowid — the same technique Postgres uses (an UPDATE
                # is internally a DELETE + INSERT of a new tuple version).
                self._do_delete(stmt.table_name, heap, rowid)
                self._do_insert(stmt.table_name, heap, new_row)
                count += 1
            return count

        if isinstance(stmt, DeleteStmt):
            heap = self.get_heap(stmt.table_name)
            matches = list(self._match_rows(stmt.table_name, stmt.where))
            for rowid, _row in matches:
                self._do_delete(stmt.table_name, heap, rowid)
            return len(matches)

        if isinstance(stmt, SelectStmt):
            plan = build_plan(stmt, self.catalog)
            return list(execute(plan, self))

        raise ValueError(f"Don't know how to execute {stmt!r}")
