"""
executor.py — Executes a plan tree, Volcano/iterator style.

Every node type is executed by a generator function: each operator pulls
rows from its child generator(s) one at a time and yields transformed rows
upward, rather than materializing intermediate results in bulk. This is
the same "pull-based, one-tuple-at-a-time" model Postgres's executor uses
(the classic 1994 Volcano paper). SortNode is the one necessary exception —
sorting fundamentally requires seeing every input row before it can produce
its first output row, which is true in real engines too (they spill to disk
for large sorts; MiniSQL just holds it in memory).

Row representation: every row flowing through the executor is a plain dict
containing BOTH a bare key per column ("age") AND a table-qualified key
("users.age"). This lets Filter/Sort/Project resolve a column reference
regardless of whether the query wrote it qualified or not, and lets a join
combine two tables' rows without silently losing one side's column when
names collide (documented limitation: an unqualified name shared by both
tables in a join resolves to the right-hand table's value, since it is
merged in last — queries against ambiguous columns should qualify them).
"""

from __future__ import annotations
from collections import defaultdict
from .planner import (
    SeqScanNode, IndexScanNode, NestedLoopJoinNode, FilterNode,
    SortNode, ProjectNode, AggregateNode, LimitNode,
)
from .ast_nodes import BinOp, ColumnRef, Literal, AggExpr


def _qualify(table: str, row: dict) -> dict:
    out = dict(row)
    for k, v in row.items():
        out[f"{table}.{k}"] = v
    return out


def execute(plan, engine) -> "Iterator[dict]":
    if isinstance(plan, SeqScanNode):
        heap = engine.get_heap(plan.table)
        for _rowid, row in heap.scan():
            yield _qualify(plan.table, row)
        return

    if isinstance(plan, IndexScanNode):
        heap = engine.get_heap(plan.table)
        index = engine.get_index(plan.table, plan.column)
        if plan.op == "=":
            rowids = index.search(plan.value)
        elif plan.op in (">", ">="):
            rowids = index.range_search(low=plan.value, low_inclusive=(plan.op == ">="))
        elif plan.op in ("<", "<="):
            rowids = index.range_search(high=plan.value, high_inclusive=(plan.op == "<="))
        elif plan.op == "!=":
            rowids = (rid for k, rid in index.inorder() if k != plan.value)
        else:
            raise ValueError(f"Unsupported index op {plan.op!r}")
        for rowid in rowids:
            row = heap.read(rowid)
            if row is not None:
                yield _qualify(plan.table, row)
        return

    if isinstance(plan, NestedLoopJoinNode):
        # Classic nested-loop join: materialize the (smaller, ideally)
        # right side once, then stream the left side against it. O(n*m)
        # time — MiniSQL does not implement hash join or merge join, which
        # is the main thing a production optimizer would pick instead when
        # there's no index to support an index-nested-loop join.
        right_rows = list(execute(plan.right, engine))
        for left_row in execute(plan.left, engine):
            lval = left_row.get(plan.left_key)
            for right_row in right_rows:
                rval = right_row.get(plan.right_key)
                if lval == rval:
                    merged = {**right_row, **left_row}
                    yield merged
        return

    if isinstance(plan, FilterNode):
        for row in execute(plan.child, engine):
            if _eval(plan.predicate, row):
                yield row
        return

    if isinstance(plan, SortNode):
        rows = list(execute(plan.child, engine))
        rows.sort(key=lambda r: r.get(plan.key), reverse=plan.desc)
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


def _agg_display_name(agg: AggExpr) -> str:
    return f"{agg.func}({agg.arg})"


def _compute_agg_row(rows: list[dict], columns: list, group_col=None, group_value=None) -> dict:
    """Compute one output row of aggregate results over `rows` (all the rows
    in a single group, or all matching rows if there's no GROUP BY). This is
    the one "blocking" step in an otherwise all-generator executor: you
    cannot know COUNT/SUM/AVG/MIN/MAX until every row in the group has been
    seen, so — like Sort — it must materialize its input first."""
    out = {}
    for col in columns:
        if isinstance(col, AggExpr):
            values = [r.get(col.arg) for r in rows if col.arg == "*" or r.get(col.arg) is not None]
            name = _agg_display_name(col)
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
        lval, rval = _eval(expr.left, row), _eval(expr.right, row)
        if expr.op == "=":
            return lval == rval
        if expr.op == "!=":
            return lval != rval
        if expr.op == "<":
            return lval is not None and rval is not None and lval < rval
        if expr.op == "<=":
            return lval is not None and rval is not None and lval <= rval
        if expr.op == ">":
            return lval is not None and rval is not None and lval > rval
        if expr.op == ">=":
            return lval is not None and rval is not None and lval >= rval
    raise ValueError(f"Cannot evaluate expression: {expr!r}")
