"""
executor.py — Executes a plan tree, Volcano/iterator style.

Every node type is executed by a generator function: each operator pulls
rows from its child generator(s) one at a time and yields transformed rows
upward, rather than materializing intermediate results in bulk. This is
the same "pull-based, one-tuple-at-a-time" model Postgres's executor uses
(the classic 1994 Volcano paper). SortNode and AggregateNode are the two
necessary exceptions — sorting or aggregating fundamentally requires seeing
every input row before producing the first output row, which is true in
real engines too (they spill to disk for large sorts; MiniSQL just holds it
in memory).

Row representation: every row flowing through the executor is a plain dict
containing BOTH a bare key per column ("age") AND a table-qualified key
("users.age"). This lets Filter/Sort/Project resolve a column reference
regardless of whether the query wrote it qualified or not, and lets a join
combine two tables' rows without losing either side's column when names
collide: the qualified keys are always distinct, and the planner rejects an
*unqualified* reference to a column that exists in both joined tables as
ambiguous, so the bare-key collision is never observable.

NULL semantics follow SQL: any comparison involving NULL is false (so
`WHERE age > 25` and `WHERE age = NULL` both exclude NULL rows), `IS NULL` /
`IS NOT NULL` are the only way to test for it, aggregates other than
COUNT(*) skip NULLs, and ORDER BY sorts NULLs first (as SQLite does).
"""

from __future__ import annotations
from collections import defaultdict
from .planner import (
    SeqScanNode, IndexScanNode, NestedLoopJoinNode, FilterNode,
    SortNode, ProjectNode, AggregateNode, LimitNode, output_name,
)
from .ast_nodes import BinOp, ColumnRef, Literal, AggExpr


def _qualify(table: str, row: dict) -> dict:
    out = dict(row)
    for k, v in row.items():
        out[f"{table}.{k}"] = v
    return out


def _sort_key(key: str):
    # (is_not_null, value): NULLs sort first in ASC, last in DESC, and never
    # get compared against a real value (which would raise TypeError).
    def k(row):
        v = row.get(key)
        return (v is not None, v)
    return k


def index_lookup(index, op: str, value):
    """Rowids from a B-tree for `column OP value`. Shared by the SELECT
    executor and the UPDATE/DELETE row matcher so both use identical logic."""
    if value is None:
        return iter(())          # nothing compares equal/less/greater to NULL
    if op == "=":
        return iter(index.search(value))
    if op in (">", ">="):
        return index.range_search(low=value, low_inclusive=(op == ">="))
    if op in ("<", "<="):
        return index.range_search(high=value, high_inclusive=(op == "<="))
    raise ValueError(f"Unsupported index op {op!r}")


def execute(plan, engine) -> "Iterator[dict]":
    if isinstance(plan, SeqScanNode):
        heap = engine.get_heap(plan.table)
        for _rowid, row in heap.scan():
            yield _qualify(plan.table, row)
        return

    if isinstance(plan, IndexScanNode):
        heap = engine.get_heap(plan.table)
        index = engine.get_index(plan.table, plan.column)
        for rowid in index_lookup(index, plan.op, plan.value):
            row = heap.read(rowid)
            if row is not None:      # skip stale entries for tombstoned rows
                yield _qualify(plan.table, row)
        return

    if isinstance(plan, NestedLoopJoinNode):
        # Classic nested-loop join: materialize the right side once, then
        # stream the left side against it. O(n*m) time — MiniSQL does not
        # implement hash join or merge join, which is the main thing a
        # production optimizer would pick instead.
        right_rows = list(execute(plan.right, engine))
        for left_row in execute(plan.left, engine):
            lval = left_row.get(plan.left_key)
            if lval is None:
                continue             # NULL never joins
            for right_row in right_rows:
                if lval == right_row.get(plan.right_key):
                    yield {**right_row, **left_row}
        return

    if isinstance(plan, FilterNode):
        for row in execute(plan.child, engine):
            if _eval(plan.predicate, row):
                yield row
        return

    if isinstance(plan, SortNode):
        rows = list(execute(plan.child, engine))
        rows.sort(key=_sort_key(plan.key), reverse=plan.desc)
        yield from rows
        return

    if isinstance(plan, ProjectNode):
        for row in execute(plan.child, engine):
            yield {col: row.get(col) for col in plan.columns}
        return

    if isinstance(plan, AggregateNode):
        rows = list(execute(plan.child, engine))
        if plan.group_by is None:
            yield _compute_agg_row(rows, plan.columns)
        else:
            groups: dict = defaultdict(list)
            for row in rows:
                groups[row.get(plan.group_by)].append(row)
            for group_value, group_rows in groups.items():
                yield _compute_agg_row(
                    group_rows, plan.columns,
                    group_col=plan.group_by, group_value=group_value,
                )
        return

    if isinstance(plan, LimitNode):
        for i, row in enumerate(execute(plan.child, engine)):
            if i >= plan.n:
                return
            yield row
        return

    raise ValueError(f"Unknown plan node: {plan!r}")


def _compute_agg_row(rows: list[dict], columns: list, group_col=None, group_value=None) -> dict:
    """Compute one output row of aggregate results over `rows` (all the rows
    in a single group, or all matching rows if there's no GROUP BY). This is
    a "blocking" step: you cannot know COUNT/SUM/AVG/MIN/MAX until every row
    in the group has been seen, so — like Sort — it materializes its input."""
    out = {}
    for col in columns:
        if isinstance(col, AggExpr):
            values = [r.get(col.arg) for r in rows if col.arg == "*" or r.get(col.arg) is not None]
            name = output_name(col)
            if col.func == "COUNT":
                out[name] = len(rows) if col.arg == "*" else len(values)
            elif col.func == "SUM":
                out[name] = sum(values) if values else None
            elif col.func == "AVG":
                out[name] = (sum(values) / len(values)) if values else None
            elif col.func == "MIN":
                out[name] = min(values) if values else None
            elif col.func == "MAX":
                out[name] = max(values) if values else None
            else:
                raise ValueError(f"Unknown aggregate function {col.func!r}")
        else:
            # A plain column in an aggregate query is only valid (checked by
            # the planner) if it IS the GROUP BY column.
            out[col] = group_value if col == group_col else None
    return out


def _eval(expr, row: dict):
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, ColumnRef):
        key = f"{expr.table}.{expr.name}" if expr.table else expr.name
        return row.get(key)
    if isinstance(expr, BinOp):
        if expr.op == "AND":
            return bool(_eval(expr.left, row)) and bool(_eval(expr.right, row))
        if expr.op == "OR":
            return bool(_eval(expr.left, row)) or bool(_eval(expr.right, row))
        lval = _eval(expr.left, row)
        if expr.op == "IS":
            return lval is None
        if expr.op == "IS NOT":
            return lval is not None
        rval = _eval(expr.right, row)
        if lval is None or rval is None:
            return False             # SQL: any comparison with NULL is not true
        if expr.op == "=":
            return lval == rval
        if expr.op == "!=":
            return lval != rval
        if expr.op == "<":
            return lval < rval
        if expr.op == "<=":
            return lval <= rval
        if expr.op == ">":
            return lval > rval
        if expr.op == ">=":
            return lval >= rval
    raise ValueError(f"Cannot evaluate expression: {expr!r}")
