"""
planner.py — Turns a SelectStmt AST into a plan tree.

The plan tree is a set of small dataclasses (SeqScanNode, IndexScanNode, ...)
completely decoupled from execution — building a plan does no I/O. This
mirrors real query engines: planning is a pure function from
(AST, catalog statistics) -> plan, and a separate executor interprets the
plan. It's also what makes EXPLAIN possible: we can print the plan tree
without running it.

Index-selection strategy (deliberately simple, documented as such):
  - Only equality/range predicates directly on the base (FROM) table's
    column are considered for index use.
  - If WHERE is a single such predicate, or an AND of several where at
    least one conjunct qualifies, the qualifying conjunct becomes an
    IndexScan and every other conjunct is pushed into a Filter on top.
  - OR at the top level, predicates on the joined table, and predicates
    with no matching index all fall back to a SeqScan + Filter.
  A real optimizer would also estimate selectivity (e.g. via histograms)
  before deciding a scan is worthwhile; MiniSQL always prefers an index
  when one is available on an eligible column, since it has no statistics
  to reason about cost otherwise.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from .ast_nodes import SelectStmt, BinOp, ColumnRef, Literal, JoinClause, AggExpr
from .catalog import Catalog

COMPARISON_OPS = {"=", "!=", "<", "<=", ">", ">="}


# ---------------- Plan node types ----------------

@dataclass
class SeqScanNode:
    table: str


@dataclass
class IndexScanNode:
    table: str
    column: str
    op: str
    value: object


@dataclass
class NestedLoopJoinNode:
    left: object
    right: object
    left_key: str    # qualified key, e.g. "users.id"
    right_key: str


@dataclass
class FilterNode:
    child: object
    predicate: BinOp


@dataclass
class SortNode:
    child: object
    key: str
    desc: bool


@dataclass
class ProjectNode:
    child: object
    columns: list[str]   # resolve keys into the row dict; also used as display names


@dataclass
class AggregateNode:
    child: object
    group_by: str | None
    columns: list   # mix of plain column names (str) and AggExpr — defines output order too


@dataclass
class LimitNode:
    child: object
    n: int


# ---------------- Planning ----------------

def _split_conjuncts(expr) -> list:
    """Flatten a right-leaning AND-chain into a flat list of conjuncts.
    Stops flattening at an OR (OR is not decomposable this way)."""
    if isinstance(expr, BinOp) and expr.op == "AND":
        return _split_conjuncts(expr.left) + _split_conjuncts(expr.right)
    return [expr]


def _is_indexable(cond, table: str, catalog: Catalog):
    """If `cond` is a simple `column OP literal` (or `literal OP column`)
    against `table`'s own column and that column has an index, return
    (column, op, value). Otherwise return None."""
    if not (isinstance(cond, BinOp) and cond.op in COMPARISON_OPS):
        return None
    left, right = cond.left, cond.right
    if isinstance(left, ColumnRef) and isinstance(right, Literal):
        col, op, val = left, cond.op, right.value
    elif isinstance(right, ColumnRef) and isinstance(left, Literal):
        # normalize "25 < age" into "age > 25"
        flip = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
        col, op, val = right, flip[cond.op], left.value
    else:
        return None
    if col.table is not None and col.table != table:
        return None
    if not catalog.has_index(table, col.name):
        return None
    return col.name, op, val


def _resolve_columns(stmt: SelectStmt, catalog: Catalog) -> list[str]:
    if stmt.columns != ["*"]:
        return stmt.columns
    base_cols = catalog.get_table(stmt.table).column_names()
    if stmt.join is None:
        return base_cols
    join_cols = catalog.get_table(stmt.join.table).column_names()
    return [f"{stmt.table}.{c}" for c in base_cols] + \
           [f"{stmt.join.table}.{c}" for c in join_cols]


def build_plan(stmt: SelectStmt, catalog: Catalog):
    # 1. base scan (with index selection if there's no join to complicate
    #    which table a predicate belongs to)
    plan = None
    remaining_where = stmt.where

    if stmt.where is not None and stmt.join is None:
        conjuncts = _split_conjuncts(stmt.where)
        chosen = None
        for c in conjuncts:
            hit = _is_indexable(c, stmt.table, catalog)
            if hit is not None:
                chosen = (c, hit)
                break
        if chosen is not None:
            used_conjunct, (col, op, val) = chosen
            plan = IndexScanNode(stmt.table, col, op, val)
            leftover = [c for c in conjuncts if c is not used_conjunct]
            remaining_where = None
            for c in leftover:
                remaining_where = c if remaining_where is None else BinOp(remaining_where, "AND", c)

    if plan is None:
        plan = SeqScanNode(stmt.table)

    # 2. join
    if stmt.join is not None:
        right = SeqScanNode(stmt.join.table)
        left_key = f"{stmt.join.left_col.table or stmt.table}.{stmt.join.left_col.name}"
        right_key = f"{stmt.join.right_col.table or stmt.join.table}.{stmt.join.right_col.name}"
        plan = NestedLoopJoinNode(plan, right, left_key, right_key)

    # 3. remaining filter
    if remaining_where is not None:
        plan = FilterNode(plan, remaining_where)

    has_agg = any(isinstance(c, AggExpr) for c in stmt.columns)

    if has_agg:
        # Validate simple GROUP BY semantics: every non-aggregate column in
        # the select list must be the GROUP BY column itself. Real SQL
        # engines enforce the same rule (a plain column not in GROUP BY has
        # no single well-defined value per group) — MiniSQL just checks it
        # eagerly instead of relying on functional-dependency analysis.
        for c in stmt.columns:
            if not isinstance(c, AggExpr) and c != stmt.group_by:
                raise ValueError(
                    f"Column {c!r} must appear in GROUP BY or be wrapped in "
                    f"an aggregate function"
                )
        plan = AggregateNode(plan, stmt.group_by, stmt.columns)
        # No separate Project step — AggregateNode already emits exactly
        # the requested output columns in order.
    else:
        # 4. sort
        if stmt.order_by is not None:
            plan = SortNode(plan, stmt.order_by, stmt.order_desc)
        # 5. project
        plan = ProjectNode(plan, _resolve_columns(stmt, catalog))

    # Sorting/limiting an aggregated result (e.g. ORDER BY COUNT(*)) operates
    # on the already-computed group rows, so it happens after AggregateNode
    # either way — for the non-aggregate path this was already done above.
    if has_agg and stmt.order_by is not None:
        plan = SortNode(plan, stmt.order_by, stmt.order_desc)

    # 6. limit
    if stmt.limit is not None:
        plan = LimitNode(plan, stmt.limit)

    return plan


def explain(plan, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(plan, SeqScanNode):
        return f"{pad}SeqScan(table={plan.table})"
    if isinstance(plan, IndexScanNode):
        return f"{pad}IndexScan(table={plan.table}, {plan.column} {plan.op} {plan.value!r})"
    if isinstance(plan, NestedLoopJoinNode):
        return (f"{pad}NestedLoopJoin(on {plan.left_key} = {plan.right_key})\n"
                f"{explain(plan.left, indent + 1)}\n"
                f"{explain(plan.right, indent + 1)}")
    if isinstance(plan, FilterNode):
        return f"{pad}Filter({_expr_str(plan.predicate)})\n{explain(plan.child, indent + 1)}"
    if isinstance(plan, SortNode):
        direction = "DESC" if plan.desc else "ASC"
        return f"{pad}Sort(key={plan.key} {direction})\n{explain(plan.child, indent + 1)}"
    if isinstance(plan, ProjectNode):
        return f"{pad}Project({', '.join(plan.columns)})\n{explain(plan.child, indent + 1)}"
    if isinstance(plan, AggregateNode):
        cols_str = ", ".join(_agg_col_str(c) for c in plan.columns)
        gb = f", GROUP BY {plan.group_by}" if plan.group_by else ""
        return f"{pad}Aggregate({cols_str}{gb})\n{explain(plan.child, indent + 1)}"
    if isinstance(plan, LimitNode):
        return f"{pad}Limit({plan.n})\n{explain(plan.child, indent + 1)}"
    return f"{pad}Unknown({plan!r})"


def _agg_col_str(c) -> str:
    if isinstance(c, AggExpr):
        return f"{c.func}({c.arg})"
    return c


def _expr_str(expr) -> str:
    if isinstance(expr, Literal):
        return repr(expr.value)
    if isinstance(expr, ColumnRef):
        return f"{expr.table}.{expr.name}" if expr.table else expr.name
    if isinstance(expr, BinOp):
        return f"({_expr_str(expr.left)} {expr.op} {_expr_str(expr.right)})"
    return str(expr)
