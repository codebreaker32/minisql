# MiniSQL

A SQL query engine built from scratch in Python: lexer → parser → AST →
query planner → Volcano-model executor, backed by a custom heap-file
storage layer and a hand-written B-tree index.

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
   │                                 (chooses IndexScan vs SeqScan here)
   ▼
 executor.py     execute()           plan tree -> generator of row dicts
   │
   ├── storage/heap.py    HeapFile   append-only row storage on disk
   └── storage/btree.py   BTree      B-tree index: column value -> rowids
```

## Quick start

```bash
# run the test suite (38 tests: B-tree, SQLite cross-checks, transactions, aggregates)
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
OK
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
| `minisql/lexer.py` | Regex-based tokenizer. SQL text → `Token` list. |
| `minisql/ast_nodes.py` | Dataclasses for every statement and expression type. |
| `minisql/parser.py` | Hand-written recursive-descent parser. Tokens → AST. |
| `minisql/catalog.py` | Table schemas + index metadata, persisted as JSON. |
| `minisql/storage/heap.py` | Append-only row storage: `HeapFile`. Rowid = byte offset. |
| `minisql/storage/btree.py` | From-scratch B-tree: insert (with node splitting), point search, range search. |
| `minisql/planner.py` | AST → plan tree. Decides IndexScan vs SeqScan. Powers `EXPLAIN`. |
| `minisql/executor.py` | Interprets the plan tree, Volcano/iterator style (generators). |
| `minisql/engine.py` | Top-level `Engine.execute_sql()` — wires everything together. |
| `repl.py` | Interactive shell for live demos. |
| `benchmark.py` | SeqScan vs IndexScan timing across table sizes 1k–200k rows. |
| `tests/test_btree.py` | B-tree correctness in isolation (stress test vs brute force). |
| `tests/test_correctness.py` | Full-engine correctness, cross-checked against real SQLite. |
| `tests/test_transactions_and_aggregates.py` | BEGIN/COMMIT/ROLLBACK semantics; COUNT/SUM/AVG/MIN/MAX/GROUP BY, cross-checked against SQLite. |

## What's implemented

- DDL: `CREATE TABLE`, `CREATE INDEX`
- DML: `INSERT`, `SELECT`, `UPDATE`, `DELETE`
- `WHERE` with `=, !=, <, <=, >, >=, AND, OR`
- `GROUP BY`, aggregates: `COUNT(*)`, `COUNT(col)`, `SUM`, `AVG`, `MIN`, `MAX`
- `ORDER BY ... ASC/DESC`, `LIMIT`
- `INNER JOIN ... ON` (nested-loop join)
- `BEGIN` / `COMMIT` / `ROLLBACK` — atomic transactions via an undo log
- `EXPLAIN` (prints the plan tree without executing it)
- Non-unique B-tree indexes (duplicate keys supported)
- Automatic index-vs-scan selection in the planner — including for `UPDATE`/`DELETE`, not just `SELECT`

### How transactions work (undo-log atomicity)

There are two classical techniques real databases use to implement atomic
`ROLLBACK`: **shadow paging** (never overwrite a page in place; commit by
swapping a pointer to the new version) and **undo/redo logging** (write
changes immediately, but log enough information to undo them if the
transaction aborts). MiniSQL implements a small, real version of the
second technique:

- `BEGIN` starts recording an undo log.
- Every `INSERT` (and the "insert the new version" half of every `UPDATE`)
  appends `("insert", table, rowid)` to the log.
- Every `DELETE` (and the "tombstone the old version" half of every
  `UPDATE`) appends `("delete", table, rowid)` to the log.
- `ROLLBACK` replays the log in reverse: undo an `insert` by tombstoning
  that rowid; undo a `delete` by resurrecting it (`HeapFile.undelete`).
- `COMMIT` just discards the log — the writes already happened.

No index bookkeeping is needed on rollback: since a loaded B-tree index is
never modified by delete/undelete (this B-tree has no delete operation —
see Limitations), resurrecting a row via `undelete()` makes it findable via
the index again automatically, because `IndexScan` was already filtering
on `heap.read(rowid) is not None`. Rollback correctness falls out of the
tombstone-filtering design already needed for `UPDATE`/`DELETE`, rather
than needing its own separate mechanism.

Limitations, stated up front: no nested transactions, DDL is always
autocommit (can't be rolled back — matching MySQL's historical behavior
for many DDL statements, unlike Postgres which supports transactional
DDL), and there is no isolation between concurrent transactions because
there is no concurrency at all — MiniSQL is single-threaded, single-writer.

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
real engines enforce (a plain column not in `GROUP BY` has no single
well-defined value per group).

`UPDATE` never rewrites bytes in place. It tombstones the old row version and
appends the new one as a fresh row with a fresh rowid — mirroring how
Postgres actually implements `UPDATE` internally (a new tuple version,
the old one marked dead for a later `VACUUM`). `DELETE` just tombstones.

One direct consequence, deliberately not hidden: a B-tree index entry
pointing at a since-updated-or-deleted row is left in place rather than
removed (this implementation's `BTree` has no delete operation — see
Limitations). When that entry is looked up later, `heap.read(rowid)`
returns `None` for a tombstoned row, and both the planner's `IndexScan`
and the `UPDATE`/`DELETE` row-matching path (`Engine._match_rows`) filter
those `None`s out before they ever reach a user's result set. So results
are always correct — but, exactly like a real un-vacuumed Postgres table,
the heap file and index accumulate dead entries over time. This is a
genuine, explainable trade-off to bring up in an interview, not a bug.

## What's deliberately NOT implemented (and why that's OK to say out loud)

- **No concurrency.** Transactions give you atomicity (all-or-nothing) but
  not isolation between simultaneous transactions, because there's only
  ever one writer. Real MVCC (Postgres) or 2PL (traditional locking) solve
  a fundamentally harder problem: multiple transactions in flight at once.
- **No `VACUUM` / compaction.** Tombstoned rows and their stale index
  entries are never physically reclaimed, so the heap file and B-tree only
  grow. A production engine periodically compacts; this one doesn't.
- **The B-tree has no delete operation.** Removing a key from a B-tree
  correctly (rebalancing after underflow, merging sibling nodes) is
  meaningfully more code than insertion. Rather than half-implement it,
  `UPDATE`/`DELETE`/`ROLLBACK` all route around the gap via tombstoning +
  filtering stale reads — a real trade-off, not an oversight, and worth
  being upfront about if asked "does your B-tree support delete."
- **No nested transactions / savepoints.** One level of `BEGIN` only.
- **No hash join or merge join**, only nested-loop join. A cost-based
  optimizer would pick between join algorithms based on table size and
  available indexes; MiniSQL always nested-loops.
- **B-tree, not B+tree.** Real database indexes (Postgres, InnoDB) use
  B+trees, where data lives only in leaf nodes and leaves are linked for
  fast range scans. MiniSQL's B-tree stores data in internal nodes too,
  and range scans do an in-order traversal instead of a leaf-to-leaf walk.
  Same O(log n) point-lookup complexity, weaker constant factor on ranges.
- **In-memory indexes, rebuilt on demand.** Indexes aren't serialized to
  disk; they're rebuilt by scanning the heap file the first time they're
  needed after a process restart. Fine for a teaching/demo engine, not for
  a real database with fast-restart requirements.
- **No query cost estimation.** The planner always uses an index if one
  exists on an eligible column — it has no row-count statistics or
  selectivity estimates to decide an index scan might actually be worse
  (e.g., if the predicate matches 90% of rows, a seq scan is usually
  faster in a real engine because of I/O locality; MiniSQL doesn't reason
  about that).
- **GROUP BY is single-column only**, and there's no `HAVING` clause.
