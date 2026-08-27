"""
engine.py — Top-level entry point: Engine.execute_sql(sql) -> rows.

Wires everything together:
  text -> lexer/parser -> AST -> (DDL/DML dispatch, or planner -> executor)

Responsibilities that live here rather than in the planner/executor:
  - constraint enforcement on writes: column existence, column types (with
    the usual int -> REAL widening), PRIMARY KEY uniqueness and NOT NULL;
  - the write path (`_do_insert` / `_do_delete`) and the page-journaling
    hook the heap calls before it modifies any page: the page's pre-image
    goes to the on-disk rollback journal (once per transaction) and to an
    in-memory statement journal (once per statement) — SQLite's design;
  - transactions: BEGIN / COMMIT / ROLLBACK, plus statement-level atomicity
    (a statement that fails halfway — e.g. an UPDATE that would create a
    duplicate key on its third row — is undone completely before the error
    is raised, like a real engine);
  - crash recovery on startup, from the same rollback journal.

Rollback (of a statement or a transaction) restores page images, which can
free slots that loaded B-tree indexes still point at; those indexes are
therefore dropped and rebuilt lazily from the heap on next use.

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
from .storage.journal import UndoJournal, PRE_IMAGE, NEW_PAGE
from .planner import (
    build_plan, explain as explain_plan, validate_where,
    _split_conjuncts, pick_index_conjunct, _and_together,
)
from .executor import execute, index_lookup, _eval, _qualify


class Engine:
    def __init__(self, data_dir: str = "data", sync: bool = True):
        """`sync=True` fsyncs the journal before every page modification and
        the heap files at every commit point, so committed data survives
        power loss. `sync=False` still journals (so a process crash is
        recoverable) but leaves flushing to the OS — the same knob as
        SQLite's PRAGMA synchronous=OFF, used by the benchmark for bulk
        loading."""
        self.catalog = Catalog(data_dir)
        self.sync = sync
        self._heaps: dict[str, HeapFile] = {}
        self._indexes: dict[tuple[str, str], BTree] = {}
        self._in_transaction = False
        # Transaction-level rollback journal (on disk, mirrored in memory):
        # one entry per page first touched in the transaction —
        # (kind, table, page_no, pre-image or None for a new page).
        self._txn_entries: list[tuple[int, str, int, bytes | None]] = []
        self._txn_saved: set[tuple[str, int]] = set()
        # Statement-level journal (memory only): pre-image of every page
        # first touched by the *current statement*, used to undo just that
        # statement on failure without disturbing the rest of the transaction.
        self._stmt_images: dict[tuple[str, int], bytes | None] = {}
        self._journal = UndoJournal(self.catalog.journal_path(), sync=sync)
        self._recover()

    def close(self) -> None:
        for heap in self._heaps.values():
            heap.close()
        self._journal.close()

    # ---------------- storage accessors ----------------

    def get_heap(self, table: str) -> HeapFile:
        if table not in self._heaps:
            self._heaps[table] = HeapFile(
                self.catalog.heap_path(table),
                on_page_write=lambda page_no, old, t=table: self._before_page_write(t, page_no, old),
            )
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

    # ---------------- write path + rollback journal ----------------

    def _before_page_write(self, table: str, page_no: int, old_image: bytes | None) -> None:
        """Called by a HeapFile just before it modifies a page (old_image is
        the page's current bytes) or allocates one (old_image is None).
        Write-ahead: the pre-image is on disk before the page changes."""
        key = (table, page_no)
        if key not in self._stmt_images:
            self._stmt_images[key] = old_image
        if key not in self._txn_saved:
            kind = NEW_PAGE if old_image is None else PRE_IMAGE
            self._journal.append(kind, table, page_no, old_image)
            self._txn_entries.append((kind, table, page_no, old_image))
            self._txn_saved.add(key)

    def _do_insert(self, table: str, heap: HeapFile, row: dict) -> int:
        """Insert a row (the heap journals the pages it touches) and update
        every loaded index. Every INSERT and every UPDATE's "insert the new
        version" step goes through here."""
        rowid = heap.insert(row)
        self._update_indexes_on_insert(table, row, rowid)
        return rowid

    def _do_delete(self, table: str, heap: HeapFile, rowid: int) -> None:
        """Tombstone a row. The B-tree entry is deliberately left in place
        (this BTree has no delete operation): every index read path filters
        rowids whose heap.read() is None, so a stale entry is harmless."""
        heap.delete(rowid)

    def _apply_undo(self, entries) -> set[str]:
        """Put pages back: pre-images are restored, new pages truncated
        away. Newest first, so a page journaled several times ends at its
        oldest image. Returns the tables touched."""
        touched: set[str] = set()
        lowest_new: dict[str, int] = {}
        for kind, table, page_no, image in reversed(entries):
            touched.add(table)
            if kind == PRE_IMAGE:
                self.get_heap(table).restore_page(page_no, image)
            else:
                lowest_new[table] = min(page_no, lowest_new.get(table, page_no))
        for table, page_no in lowest_new.items():
            self.get_heap(table).truncate_pages(page_no)
        # Restored pages may have freed slots that loaded indexes still
        # point at; those rowids could be reused, so drop the indexes and
        # let them rebuild lazily from the (now correct) heap.
        self._indexes = {k: v for k, v in self._indexes.items() if k[0] not in touched}
        return touched

    def _rollback_statement(self) -> None:
        """Undo only the current statement, from the in-memory statement
        journal. The transaction journal keeps its entries: they are still
        valid pre-images for a later full ROLLBACK or crash recovery."""
        entries = [(PRE_IMAGE if img is not None else NEW_PAGE, t, p, img)
                   for (t, p), img in self._stmt_images.items()]
        self._apply_undo(entries)
        self._stmt_images = {}

    def _rollback_transaction(self) -> None:
        self._apply_undo(self._txn_entries)
        self._stmt_images = {}
        self._commit_point()          # the rollback itself is now durable

    def _commit_point(self) -> None:
        """Make everything journaled so far permanent: dirty pages to the
        heap files (and to disk, in durable mode) *first*, then discard the
        journal. The other order could lose a committed write with nothing
        left to recover from."""
        for heap in self._heaps.values():
            if self.sync:
                heap.sync()
            else:
                heap.flush()
        self._journal.clear()
        self._txn_entries = []
        self._txn_saved = set()
        self._stmt_images = {}

    def _recover(self) -> None:
        """Startup: a non-empty journal means the previous process died
        mid-statement or mid-transaction. Copy every pre-image back over
        its page and truncate away pages that were new — that also repairs
        a torn (half-written) page, which a rowid-level undo never could."""
        if self._journal.size() == 0:
            return
        entries = self._journal.entries()   # [] if only a torn first entry exists
        if entries:
            self._apply_undo(entries)
        # Always reach a commit point: it clears the journal, so a torn entry
        # can't stay at the head and hide the next transaction's pre-images.
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
                self._rollback_transaction()
                self._in_transaction = False
                return None

        if isinstance(stmt, ExplainStmt):
            plan = build_plan(stmt.inner, self.catalog)
            return explain_plan(plan)

        if isinstance(stmt, SelectStmt):
            plan = build_plan(stmt, self.catalog)
            return list(execute(plan, self))

        if isinstance(stmt, (InsertStmt, UpdateStmt, DeleteStmt)):
            # Statement-level atomicity: if anything below raises, put back
            # the pages this statement touched (leaving an enclosing
            # transaction's earlier work intact) and re-raise.
            self._stmt_images = {}
            try:
                result = self._execute_dml(stmt)
            except Exception:
                self._rollback_statement()
                if not self._in_transaction:
                    self._commit_point()  # autocommit: the no-op is now durable
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
