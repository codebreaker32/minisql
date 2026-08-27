# MiniSQL

A SQL query engine built from scratch in Python: lexer → parser → AST →
query planner → Volcano-model executor, backed by a custom heap-file
storage layer, a hand-written B-tree index, and an on-disk undo journal
for crash-safe transactions.

No SQLite, no ORM, no existing query engine used anywhere in the execution
path. SQLite is used **only** in the test suite, as a ground-truth oracle to
verify MiniSQL's query results are correct.

```
SQL text
   │
   ▼
 lexer.py        tokenize()          text -> list[Token]
   │
   ▼
 parser.py       parse()             tokens -> AST (ast_nodes.py)
   │
   ▼
 planner.py      build_plan()        AST + catalog -> plan tree
   │                                 (resolves column names, chooses IndexScan vs SeqScan)
   ▼
 executor.py     execute()           plan tree -> generator of row dicts
   │
   ├── storage/heap.py     HeapFile     append-only row storage on disk
   ├── storage/btree.py    BTree        B-tree index: column value -> rowids
   └── storage/journal.py  UndoJournal  write-ahead undo log for ROLLBACK + crash recovery
```

## Quick start

```bash
# run the test suite (72 tests: B-tree, SQLite cross-checks, constraints,
# NULL semantics, transactions, crash recovery, aggregates)
python3 -m unittest discover tests -v

# run the interactive REPL
python3 repl.py

# run the performance benchmark (SeqScan vs IndexScan, 1k-200k rows)
python3 benchmark.py
```

Example REPL session:

```sql
minisql> CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT);
OK
minisql> INSERT INTO users VALUES (1, 'Alice', 30);
OK (1 row affected)
minisql> INSERT INTO users VALUES (1, 'Dup', 40);
Error: Duplicate PRIMARY KEY value 1 for users.id
minisql> CREATE INDEX idx_age ON users (age);
OK
minisql> EXPLAIN SELECT name FROM users WHERE age > 25;
Project(name)
  IndexScan(table=users, age > 25)
minisql> SELECT name FROM users WHERE age > 25;
name
------
Alice
(1 row)
```

## Module-by-module

| File | Responsibility |
|---|---|
| `minisql/lexer.py` | Regex-based tokenizer. SQL text → `Token` list. Supports `"quoted identifiers"`. |
| `minisql/ast_nodes.py` | Dataclasses for every statement and expression type. |
| `minisql/parser.py` | Hand-written recursive-descent parser. Tokens → AST. |
| `minisql/catalog.py` | Table schemas + index metadata, persisted as JSON. |
| `minisql/storage/heap.py` | Append-only row storage: `HeapFile`. Rowid = byte offset. Tombstone deletes. |
| `minisql/storage/btree.py` | From-scratch B-tree: insert (with node splitting), point search, pruned range search, NULL-safe keys. |
| `minisql/storage/journal.py` | On-disk undo journal: written before every heap change, cleared at commit. |
| `minisql/planner.py` | Semantic analysis (unknown/ambiguous columns) + AST → plan tree. Decides IndexScan vs SeqScan per table, including under a join. Powers `EXPLAIN`. |
| `minisql/executor.py` | Interprets the plan tree, Volcano/iterator style (generators). SQL NULL semantics. |
| `minisql/engine.py` | Top-level `Engine.execute_sql()`: constraint enforcement, write path, transactions, recovery. |
| `repl.py` | Interactive shell for live demos. |
| `benchmark.py` | SeqScan vs IndexScan timing across table sizes 1k–200k rows. |
| `tests/test_btree.py` | B-tree correctness in isolation (stress test vs brute force). |
| `tests/test_correctness.py` | Full-engine correctness, cross-checked against real SQLite. |
| `tests/test_transactions_and_aggregates.py` | BEGIN/COMMIT/ROLLBACK semantics; COUNT/SUM/AVG/MIN/MAX/GROUP BY, cross-checked against SQLite. |
| `tests/test_constraints_and_recovery.py` | PRIMARY KEY / type / NOT NULL enforcement, NULL semantics, name resolution, index use under joins, statement atomicity, crash recovery, B-tree pruning. |

## Development

Code style is enforced with [black](https://github.com/psf/black) (formatting) and
[ruff](https://github.com/astral-sh/ruff) (linting + import sorting). Config lives in
`pyproject.toml`.

```bash
pip install -r requirements-dev.txt

black .
ruff check .
```

## What's implemented

- DDL: `CREATE TABLE` (with `PRIMARY KEY`, always index-backed), `CREATE INDEX`
- DML: `INSERT` (full or partial column list), `SELECT`, `UPDATE`, `DELETE`
- Constraints, enforced on every write: column types (`INT`/`INTEGER`,
  `REAL`/`FLOAT` with int→real widening, `TEXT`/`VARCHAR`), `PRIMARY KEY`
  uniqueness and `NOT NULL`
- `WHERE` with `=, !=, <>, <, <=, >, >=, AND, OR, IS NULL, IS NOT NULL`,
  with SQL NULL semantics (a comparison with NULL is never true)
- `GROUP BY`, aggregates: `COUNT(*)`, `COUNT(col)`, `SUM`, `AVG`, `MIN`, `MAX`;
  `ORDER BY` an aggregate (`ORDER BY COUNT(*) DESC`)
- `ORDER BY ... ASC/DESC` (NULLs first, as in SQLite), `LIMIT`
- `INNER JOIN ... ON` (equi-join, nested loop); unqualified column names are
  resolved to their table and rejected if ambiguous
- `BEGIN` / `COMMIT` / `ROLLBACK` — atomic transactions via an on-disk undo
  journal, with automatic recovery of an interrupted transaction on restart
- Statement-level atomicity: a statement that fails halfway (e.g. an
  `UPDATE` whose third row would violate the primary key) is undone completely
- `EXPLAIN` (prints the plan tree without executing it)
- Non-unique B-tree indexes (duplicate keys and NULLs supported)
- Automatic index-vs-scan selection in the planner — for each table of a
  join, and for `UPDATE`/`DELETE`, not just `SELECT`
- Reserved words usable as names where unambiguous (`key`, `count`, `text`,
  ...) and `"double-quoted"` identifiers for everything else

### How transactions work (undo journal + write-ahead ordering)

There are two classical techniques real databases use to implement atomic
`ROLLBACK`: **shadow paging** (never overwrite a page in place; commit by
swapping a pointer to the new version) and **undo/redo logging** (write
changes immediately, but log enough information to undo them if the
transaction aborts). MiniSQL implements a small, real version of the
second technique, on disk:

- Before every heap mutation, one line is appended to `undo.journal`:
  `insert <table> <rowid>` or `delete <table> <rowid>`. In the default
  durable mode the journal is fsync'd before the heap is touched — the
  **write-ahead rule**: the disk always knows how to undo anything that may
  have happened.
- `ROLLBACK` replays the journal in reverse: undo an `insert` by
  tombstoning that rowid; undo a `delete` by resurrecting it
  (`HeapFile.undelete`).
- The **commit point** — `COMMIT`, or the end of every autocommit statement —
  fsyncs the heap files and *then* clears the journal. That order matters:
  clearing first could lose a committed write with nothing left to recover from.
- **Recovery**: if the engine starts and the journal is non-empty, the
  previous process died mid-transaction. It undoes the journaled changes in
  reverse; an interrupted `insert` is undone by truncating the heap back to
  that offset (journaled inserts are always the file's tail, since the
  journal is cleared at every commit point), which also discards a torn,
  half-written record.
- A statement that raises (constraint violation, bad column) is rolled
  back to the position the journal was at when the statement began, so an
  enclosing transaction's earlier statements survive.

No index bookkeeping is needed on rollback: since a loaded B-tree index is
never modified by delete/undelete (this B-tree has no delete operation —
see Limitations), resurrecting a row via `undelete()` makes it findable via
the index again automatically, because every index read path already
filters on `heap.read(rowid) is not None`. Live rollback tombstones rather
than truncates for the same reason: a loaded index may still point at that
offset, and an offset must never be reused while it does.

`Engine(data_dir, sync=False)` keeps the journal but skips the fsyncs
(the same knob as SQLite's `PRAGMA synchronous=OFF`) — that's what the
benchmark uses for bulk loading.

Limitations, stated up front: no nested transactions or savepoints, DDL is
always autocommit (can't be rolled back — matching MySQL's historical
behavior for many DDL statements, unlike Postgres which supports
transactional DDL), and there is no isolation between concurrent
transactions because there is no concurrency at all — MiniSQL is
single-threaded, single-writer.

### How GROUP BY / aggregates work

`AggregateNode` is the one other "blocking" operator in the executor
besides `Sort` — you can't know `COUNT`/`SUM`/`AVG`/`MIN`/`MAX` for a group
until every row in that group has been seen, so it materializes its input
before producing any output. Grouping is a plain Python dict keyed by the
`GROUP BY` column's value; this is O(n) rather than requiring the input to
already be sorted by the group key (a "hash aggregate", in real-optimizer
terms, as opposed to a "sort-then-group" aggregate). Only single-column
`GROUP BY` is supported, and every non-aggregated column in the `SELECT`
list must be the `GROUP BY` column itself — this is checked explicitly at
plan time rather than left to produce a confusing runtime value, same rule
real engines enforce.

### How UPDATE works

`UPDATE` never rewrites bytes in place. It tombstones the old row version and
appends the new one as a fresh row with a fresh rowid — mirroring how
Postgres actually implements `UPDATE` internally (a new tuple version,
the old one marked dead for a later `VACUUM`). `DELETE` just tombstones.
Matching rows are materialized before any mutation so the statement can't
re-match the rows it just appended (the "Halloween problem").

One direct consequence, deliberately not hidden: a B-tree index entry
pointing at a since-updated-or-deleted row is left in place rather than
removed. When that entry is looked up later, `heap.read(rowid)` returns
`None` for a tombstoned row, and every index read path — `IndexScan`, the
`UPDATE`/`DELETE` row matcher, and the primary-key uniqueness check —
filters those `None`s out. So results are always correct — but, exactly
like a real un-vacuumed Postgres table, the heap file and index accumulate
dead entries over time.

## What's deliberately NOT implemented

- **No concurrency.** Transactions give you atomicity (all-or-nothing) and
  durability but not isolation between simultaneous transactions, because
  there's only ever one writer. Real MVCC (Postgres) or 2PL (traditional
  locking) solve a fundamentally harder problem: multiple transactions in
  flight at once.
- **No `VACUUM` / compaction.** Tombstoned rows and their stale index
  entries are never physically reclaimed, so the heap file and B-tree only
  grow. A production engine periodically compacts; this one doesn't.
- **The B-tree has no delete operation.** Removing a key from a B-tree
  correctly (rebalancing after underflow, merging sibling nodes) is
  meaningfully more code than insertion. Rather than half-implement it,
  `UPDATE`/`DELETE`/`ROLLBACK` all route around the gap via tombstoning +
  filtering stale reads.
- **No nested transactions / savepoints.** One level of `BEGIN` only.
- **No hash join or merge join**, only nested-loop join. A cost-based
  optimizer would pick between join algorithms based on table size and
  available indexes; MiniSQL always nested-loops.
- **B-tree, not B+tree.** Real database indexes (Postgres, InnoDB) use
  B+trees, where data lives only in leaf nodes and leaves are linked for
  fast range scans. MiniSQL's B-tree stores data in internal nodes too, and
  range scans do a pruned in-order traversal instead of a leaf-to-leaf walk.
  Same O(log n + k) complexity, weaker constant factor.
- **In-memory indexes, rebuilt on demand.** Indexes aren't serialized to
  disk; they're rebuilt by scanning the heap file the first time they're
  needed after a process restart. Fine for a teaching/demo engine, not for
  a real database with fast-restart requirements.
- **No query cost estimation.** The planner always uses an index if one
  exists on an eligible column (`= < <= > >=`; never `!=`, which matches
  almost everything) — it has no row-count statistics or selectivity
  estimates to decide an index scan might actually be worse.
- **Rows are pickled.** A real engine packs typed columns with fixed
  layouts so it can read one column without decoding the row.
- **GROUP BY is single-column only**, there's no `HAVING`, `ORDER BY`
  takes one key, and there are no arithmetic expressions (`age + 1`).
