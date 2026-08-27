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

## Supported SQL

This is the complete grammar. If a form isn't listed here, the parser
rejects it with a `ParseError` naming the token it stopped at — nothing is
silently ignored. Keywords are case-insensitive; a trailing `;` is optional;
one statement per call.

### Statements

| Statement | Exact form |
|---|---|
| Create table | `CREATE TABLE t (col TYPE [PRIMARY KEY], col TYPE, ...)` — at most one `PRIMARY KEY`; it is automatically index-backed |
| Create index | `CREATE INDEX name ON t (col)` — single column, non-unique, may contain duplicates and NULLs |
| Insert | `INSERT INTO t VALUES (v, v, ...)` or `INSERT INTO t (col, col) VALUES (v, v)` — one row; unlisted columns become NULL |
| Select | `SELECT items FROM t [JOIN u ON a = b] [WHERE pred] [GROUP BY col] [ORDER BY item [ASC\|DESC]] [LIMIT n]` — clauses in this order |
| Update | `UPDATE t SET col = literal [, col = literal] [WHERE pred]` |
| Delete | `DELETE FROM t [WHERE pred]` |
| Transactions | `BEGIN [TRANSACTION]`, `COMMIT`, `ROLLBACK` — one level, no savepoints |
| Explain | `EXPLAIN SELECT ...` — prints the plan tree, runs nothing |

### Pieces

| Piece | What's accepted |
|---|---|
| Types | `INT` / `INTEGER`, `REAL` / `FLOAT`, `TEXT` / `VARCHAR` — no length or precision (`VARCHAR(30)` is rejected; use `VARCHAR`) |
| Literals | integers `42`, reals `3.14`, strings `'single-quoted'` (escape a quote as `\'`), `TRUE` / `FALSE` (stored as 1 / 0 in an INT column), `NULL` |
| Identifiers | `name`, `table.name`, `"double-quoted"` for any name at all. Type names, aggregate names, `KEY`, `INDEX`, `TRANSACTION` can be used unquoted as column names (`key`, `count`, `text`, ...) |
| Select items | `*`, `col`, `table.col`, `COUNT(*)`, `COUNT(col)`, `SUM(col)`, `AVG(col)`, `MIN(col)`, `MAX(col)` |
| Join | `[INNER] JOIN u ON t.a = u.b` — one join per query, equality only, both sides column references |
| WHERE predicate | comparisons `col OP literal` / `literal OP col` / `col OP col` with `= != <> < <= > >=`; `col IS NULL`; `col IS NOT NULL`; combined with `AND` / `OR` (AND binds tighter, both left-associative). No parentheses, no `NOT` |
| GROUP BY | one column; every non-aggregate select item must be that column; `GROUP BY` with no aggregate returns the distinct values |
| ORDER BY | one key: a column, or an aggregate that appears in the select list (`ORDER BY COUNT(*) DESC`); NULLs sort first ascending (as in SQLite) |
| LIMIT | non-negative integer; no `OFFSET` |

### Semantics you can rely on

- **Constraints are enforced on every write**: values must match the column
  type (an integer is widened to REAL; anything else is an error), the
  primary key is unique and NOT NULL. A violating statement is rejected and
  fully undone, even if it had already changed other rows.
- **NULL** follows SQL: any comparison involving NULL is not true, so
  `WHERE age = NULL` and `WHERE age > 25` both exclude NULL rows; `IS NULL`
  is the only way to find them. Aggregates other than `COUNT(*)` ignore
  NULLs. A NULL join key never matches. `GROUP BY` treats NULL as one group.
- **Names are checked before anything runs**: an unknown column or table is
  an error; an unqualified column that exists in both joined tables is an
  error asking you to qualify it.
- **Indexes are used automatically** when a `WHERE` conjunct is
  `col = / < / <= / > / >= literal` on an indexed column — for each table of
  a join, and for `UPDATE` / `DELETE` as well as `SELECT`. `!=` never uses an
  index. `EXPLAIN` shows the decision.
- **Return values** from `Engine.execute_sql`: `list[dict]` for SELECT,
  `str` for EXPLAIN, `int` rows affected for INSERT / UPDATE / DELETE,
  `None` for DDL and transaction control.

## Not supported — and why

The grammar is deliberately narrow: the goal was depth in the engine layers
(planner, executor, storage, recovery) rather than breadth of SQL. Each row
says what it would take, so the omission is a scoping decision rather than an
unknown.

| Not supported | Why it's out | What it would take |
|---|---|---|
| `VARCHAR(n)`, `DECIMAL(p,s)`, `DATE`, `BOOLEAN` | The type system has three storage classes (int, float, str), like SQLite's; lengths and extra types add checks without teaching anything new about the engine | Parse the `(n)`; enforce `len()` in `_coerce`; a `DATE` needs a canonical encoding to sort correctly in the B-tree |
| `NOT NULL`, `UNIQUE`, `DEFAULT`, `CHECK`, `FOREIGN KEY` | Only `PRIMARY KEY` was needed to demonstrate constraint enforcement through an index; the others are variations on the same check | `NOT NULL` / `DEFAULT`: catalog flag + check in `_build_row`. `UNIQUE`: the PK check on another index. `FOREIGN KEY`: a lookup on the referenced table's PK index at insert, and on delete |
| `ALTER TABLE`, `DROP TABLE`, `DROP INDEX` | DDL is autocommit and not journaled, so a schema change can't be rolled back or recovered; adding more DDL before journaling the catalog would widen that gap | Journal catalog changes; `DROP` = remove from catalog + delete the heap file; `ADD COLUMN` = catalog change only (missing keys read as NULL) |
| Multi-row `INSERT ... VALUES (...), (...)` | Pure parser convenience; the write path already handles one row at a time atomically | Loop in the parser; wrap in the existing statement-level rollback |
| Expressions: arithmetic (`age + 1`), functions (`UPPER`), `SET col = col + 1` | The expression tree has exactly three node types (literal, column, binary op) so the planner's index-eligibility test stays a pattern match; a general expression evaluator is a separate project | A precedence-climbing expression parser and an `_eval` that handles arithmetic; `_is_indexable` would need to recognise `col OP constant-expression` |
| Parentheses and `NOT` in `WHERE` | Three precedence levels were enough to show how precedence falls out of recursive descent; the conjunct splitter relies on a plain AND-chain | `(`…`)` as a primary in `parse_operand`; `NOT` as a unary node; push NOT down (De Morgan) so `_split_conjuncts` still sees ANDs |
| `LIKE`, `IN`, `BETWEEN` | Syntactic sugar over comparisons; none affect the plan shape | `BETWEEN` → two conjuncts (index-eligible for free); `IN` → OR chain or a set membership node; `LIKE` → a pattern-match node, prefix patterns could use the index |
| `DISTINCT`, `HAVING`, multi-column `GROUP BY`, `COUNT(DISTINCT)` | One-column hash aggregate shows the blocking-operator idea; the rest is generalisation | Tuple group keys; `HAVING` = a `Filter` above `Aggregate`; `DISTINCT` = `GROUP BY` all columns |
| Multi-key `ORDER BY`, `OFFSET` | One key suffices to show a blocking sort | Tuple sort key; `OFFSET` = skip n in `LimitNode` |
| Aliases (`AS`), table aliases, self-joins | Name resolution is by real table name; aliases need a scope-renaming layer | An alias map in `_Scope`; row qualification by alias instead of table |
| `LEFT` / `RIGHT` / `FULL OUTER JOIN`, more than one join | Inner nested-loop join demonstrates the join operator; outer joins need NULL-padding and a "matched" flag per row; multiple joins need a join order | Track unmatched left rows and emit them with NULLs; a left-deep chain of `NestedLoopJoinNode`s |
| Subqueries, `UNION` | Would require the planner to nest plans and the executor to materialise intermediate relations | A `SubqueryNode` whose child is a full plan; correlated subqueries need per-row re-execution |
| `SELECT 1` (no `FROM`), scalar functions | Every query is a scan of a table | A one-row `ValuesNode` |
| `--` comments, several statements per call | The lexer and REPL handle one statement string | Skip `--…\n` in the lexer's whitespace group; split on `;` outside strings |

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

## Engine internals deliberately left out — and why

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
