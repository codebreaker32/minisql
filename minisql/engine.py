"""
engine.py — Top-level entry point: Engine.execute_sql(sql) -> rows.

Wires everything together:
  text -> lexer/parser -> AST -> (DDL/DML dispatch, or planner -> executor)

Responsibilities that live here rather than in the planner/executor:
  - constraint enforcement on writes: column existence, column types (with
    the usual int -> REAL widening), PRIMARY KEY uniqueness and NOT NULL;
  - the write path (`_do_insert` / `_do_delete`), which journals every heap
    mutation before performing it;
  - transactions: BEGIN / COMMIT / ROLLBACK, plus statement-level atomicity
    (a statement that fails halfway — e.g. an UPDATE that would create a
    duplicate key on its third row — is undone completely before the error
    is raised, like a real engine);
  - crash recovery on startup, from the same undo journal.

Indexes are kept in memory (BTree objects) and rebuilt by scanning the heap
file the first time a table/column's index is needed after a fresh process
start; this is a common trade-off in small/embedded databases — it avoids
having to serialize a whole tree data structure to disk on every insert.
The PRIMARY KEY column always gets an index, which is what makes the
uniqueness check O(log n) instead of a table scan.
"""

from __future__ import annotations
from .parser import parse
from .ast_nodes import (
    CreateTableStmt, CreateIndexStmt, InsertStmt, SelectStmt, ExplainStmt,
    UpdateStmt, DeleteStmt, TransactionStmt,
)
from .catalog import Catalog, TableSchema
from .storage.heap import HeapFile
from .storage.btree import BTree
from .storage.journal import UndoJournal
from .planner import (
    build_plan, explain as explain_plan, validate_where,
    _split_conjuncts, pick_index_conjunct, _and_together,
)
from .executor import execute, index_lookup, _eval, _qualify


class Engine:
    def __init__(self, data_dir: str = "data", sync: bool = True):
        """`sync=True` fsyncs the journal before every heap write and the heap
        files at every commit point, so committed data survives power loss.
        `sync=False` still journals (so a process crash is recoverable) but
        leaves flushing to the OS — the same knob as SQLite's
        PRAGMA synchronous=OFF, used by the benchmark for bulk loading."""
        self.catalog = Catalog(data_dir)
        self.sync = sync
        self._heaps: dict[str, HeapFile] = {}
        self._indexes: dict[tuple[str, str], BTree] = {}
        self._in_transaction = False
        # In-memory mirror of the on-disk undo journal for the current
        # transaction (or current autocommit statement): a list of
        # ("insert", table, rowid) / ("delete", table, rowid).
        self._undo_log: list[tuple[str, str, int]] = []
        self._journal = UndoJournal(self.catalog.journal_path(), sync=sync)
        self._recover()

    def close(self) -> None:
        for heap in self._heaps.values():
            heap.close()
        self._journal.close()

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
                bt.insert(row.get(column), rowid)
            self._indexes[key] = bt
        return self._indexes[key]

    def _update_indexes_on_insert(self, table: str, row: dict, rowid: int) -> None:
        schema = self.catalog.get_table(table)
        for col in schema.indexes:
            key = (table, col)
            if key in self._indexes:  # only touch already-loaded indexes
                self._indexes[key].insert(row.get(col), rowid)

    # ---------------- write path + undo journal ----------------

    def _log(self, action: str, table: str, rowid: int) -> None:
        # Write-ahead: the undo record is on disk before the heap changes.
        self._journal.append(action, table, rowid)
        self._undo_log.append((action, table, rowid))

    def _do_insert(self, table: str, heap: HeapFile, row: dict) -> int:
        """Insert a row, journaling how to undo it first. Every INSERT and
        every UPDATE's "insert the new version" step goes through here."""
        rowid = heap.next_rowid()
        self._log("insert", table, rowid)
        actual = heap.insert(row)
        assert actual == rowid
        self._update_indexes_on_insert(table, row, rowid)
        return rowid

    def _do_delete(self, table: str, heap: HeapFile, rowid: int) -> None:
        """Tombstone a row, journaling how to undo it first.
        We deliberately do NOT remove the row's entry from any loaded B-tree
        index (this BTree has no delete operation — see README). That's what
        makes rollback's index story simple: since the index was never
        touched, resurrecting a row via undelete() makes it immediately
        findable via the index again, with zero extra code."""
        self._log("delete", table, rowid)
        heap.delete(rowid)

    def _rollback_to(self, mark: int) -> None:
        """Undo every journaled change after position `mark`, newest first."""
        touched: dict[str, HeapFile] = {}
        for action, table, rowid in reversed(self._undo_log[mark:]):
            heap = self.get_heap(table)
            touched[table] = heap
            if action == "insert":
                heap.delete(rowid)      # tombstone (never reuse the offset: loaded
                                        # indexes may still point at it)
            elif action == "delete":
                heap.undelete(rowid)    # resurrect
        # Same ordering rule as _commit_point: the undo writes must be on
        # disk *before* the journal stops describing how to redo them.
        # Otherwise a crash between the two leaves a heap that still shows
        # the rolled-back rows and a journal that says there's nothing to fix.
        if self.sync:
            for heap in touched.values():
                heap.sync()
        del self._undo_log[mark:]
        self._journal.rewrite(self._undo_log)

    def _commit_point(self) -> None:
        """Make everything journaled so far permanent: heap files to disk
        first, then discard the journal. (The other order could lose a
        committed write with nothing left to recover from.)"""
        if self.sync:
            for heap in self._heaps.values():
                heap.sync()
        self._undo_log = []
        self._journal.clear()

    def _recover(self) -> None:
        """Startup: if the journal is non-empty, the previous process died
        mid-statement or mid-transaction. Undo its heap changes in reverse.
        An interrupted insert is undone by truncating the heap back to that
        offset — journaled inserts are always the tail of the file, since
        the journal is cleared at every commit point — which also discards a
        torn, half-written record."""
        entries = self._journal.entries()
        if not entries:
            return
        for action, table, rowid in reversed(entries):
            heap = self.get_heap(table)
            if action == "insert":
                heap.truncate(rowid)
            elif action == "delete":
                heap.undelete(rowid)
        self._commit_point()

    # ---------------- constraints ----------------

    @staticmethod
    def _coerce(schema: TableSchema, col: str, value):
        """Check `value` against the declared type of `col`, widening int to
        REAL. Raises ValueError on a mismatch instead of storing garbage."""
        if value is None:
            return None
        ctype = schema.column_type(col)
        if ctype in ("INT", "INTEGER"):
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
        elif ctype in ("REAL", "FLOAT"):
            if isinstance(value, bool):
                return float(value)
            if isinstance(value, (int, float)):
                return float(value)
        elif ctype in ("TEXT", "VARCHAR"):
            if isinstance(value, str):
                return value
        raise ValueError(f"Column {col!r} is {ctype}; cannot store {value!r}")

    def _check_primary_key(self, table: str, schema: TableSchema, row: dict,
                           exclude_rowid: int | None = None) -> None:
        pk = schema.pk_column()
        if pk is None:
            return
        value = row.get(pk)
        if value is None:
            raise ValueError(f"PRIMARY KEY column {pk!r} cannot be NULL")
        heap = self.get_heap(table)
        for rowid in self.get_index(table, pk).search(value):
            if rowid != exclude_rowid and heap.read(rowid) is not None:
                raise ValueError(f"Duplicate PRIMARY KEY value {value!r} for {table}.{pk}")

    def _build_row(self, schema: TableSchema, col_names: list[str], values: list) -> dict:
        names = schema.column_names()
        if len(col_names) != len(values):
            raise ValueError("Column count does not match value count")
        if len(set(col_names)) != len(col_names):
            raise ValueError("A column is listed more than once")
        for c in col_names:
            if c not in names:
                raise ValueError(f"Column {c!r} not found on table {schema.name!r}")
        row = {name: None for name in names}      # unspecified columns are NULL
        for c, v in zip(col_names, values):
            row[c] = self._coerce(schema, c, v)
        return row

    # ---------------- row matching for UPDATE / DELETE ----------------

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
        validate_where(where, table, self.catalog)

        conjuncts = _split_conjuncts(where)
        chosen = pick_index_conjunct(conjuncts, table, self.catalog)
        if chosen is None:
            for rowid, row in heap.scan():
                if _eval(where, _qualify(table, row)):
                    yield rowid, row
            return

        used, (col, op, val) = chosen
        remaining = _and_together([c for c in conjuncts if c is not used])
        index = self.get_index(table, col)
        for rowid in index_lookup(index, op, val):
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
          - int (rows affected) for INSERT/UPDATE/DELETE
          - None for DDL/BEGIN/COMMIT/ROLLBACK
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
            pk = self.catalog.get_table(stmt.table_name).pk_column()
            if pk is not None:
                # A PRIMARY KEY is always backed by an index (as in every
                # real engine) — it's what makes the uniqueness check cheap.
                self.catalog.add_index(stmt.table_name, pk)
                self.get_index(stmt.table_name, pk)
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
                return None
            if stmt.kind == "COMMIT":
                if not self._in_transaction:
                    raise ValueError("No transaction is in progress")
                self._commit_point()
                self._in_transaction = False
                return None
            if stmt.kind == "ROLLBACK":
                if not self._in_transaction:
                    raise ValueError("No transaction is in progress")
                self._rollback_to(0)
                self._commit_point()      # the rollback itself is now durable
                self._in_transaction = False
                return None

        if isinstance(stmt, ExplainStmt):
            plan = build_plan(stmt.inner, self.catalog)
            return explain_plan(plan)

        if isinstance(stmt, SelectStmt):
            plan = build_plan(stmt, self.catalog)
            return list(execute(plan, self))

        if isinstance(stmt, (InsertStmt, UpdateStmt, DeleteStmt)):
            # Statement-level atomicity: if anything below raises, undo just
            # this statement's journaled changes (leaving an enclosing
            # transaction's earlier work intact) and re-raise.
            mark = len(self._undo_log)
            try:
                result = self._execute_dml(stmt)
            except Exception:
                self._rollback_to(mark)
                raise
            if not self._in_transaction:
                self._commit_point()      # autocommit
            return result

        raise ValueError(f"Don't know how to execute {stmt!r}")

    def _execute_dml(self, stmt) -> int:
        if isinstance(stmt, InsertStmt):
            schema = self.catalog.get_table(stmt.table_name)
            row = self._build_row(schema, stmt.columns or schema.column_names(), stmt.values)
            self._check_primary_key(stmt.table_name, schema, row)
            heap = self.get_heap(stmt.table_name)
            self._do_insert(stmt.table_name, heap, row)
            return 1

        if isinstance(stmt, UpdateStmt):
            schema = self.catalog.get_table(stmt.table_name)
            assignments = {}
            for col, val in stmt.assignments.items():
                if col not in schema.column_names():
                    raise ValueError(f"Column {col!r} not found on table {stmt.table_name!r}")
                assignments[col] = self._coerce(schema, col, val)
            heap = self.get_heap(stmt.table_name)
            # Materialize matches first: we're about to mutate the heap file
            # (tombstone + append) while iterating, and that must not affect
            # which rows this statement considers "matched" (the classic
            # "Halloween problem").
            matches = list(self._match_rows(stmt.table_name, stmt.where))
            pk_changes = schema.pk_column() in assignments
            for rowid, row in matches:
                new_row = {**row, **assignments}
                if pk_changes:
                    self._check_primary_key(stmt.table_name, schema, new_row, exclude_rowid=rowid)
                # MVCC-style update: never edit bytes in place. Tombstone the
                # old row version and append the new one as a fresh row with
                # a fresh rowid — the same technique Postgres uses (an UPDATE
                # is internally a DELETE + INSERT of a new tuple version).
                self._do_delete(stmt.table_name, heap, rowid)
                self._do_insert(stmt.table_name, heap, new_row)
            return len(matches)

        if isinstance(stmt, DeleteStmt):
            heap = self.get_heap(stmt.table_name)
            matches = list(self._match_rows(stmt.table_name, stmt.where))
            for rowid, _row in matches:
                self._do_delete(stmt.table_name, heap, rowid)
            return len(matches)

        raise ValueError(f"Don't know how to execute {stmt!r}")
