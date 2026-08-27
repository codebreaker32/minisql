"""
planner.py — Turns a SelectStmt AST into a plan tree.

The plan tree is a set of small dataclasses (SeqScanNode, IndexScanNode, ...)
completely decoupled from execution — building a plan does no I/O. This
mirrors real query engines: planning is a pure function from
(AST, catalog) -> plan, and a separate executor interprets the plan. It's
also what makes EXPLAIN possible: we can print the plan tree without
running it.

Planning has two halves:

1. Semantic analysis (`validate_select` / `validate_where`): every column
   reference is checked against the catalog. Unknown columns are an error;
   in a join, an unqualified name that exists in both tables is rejected as
   ambiguous, and every other unqualified reference is resolved to the
   table that owns it (the ColumnRef gets its `table` filled in). This is
   what lets the rest of the planner treat "which table does this predicate
   belong to?" as already answered.

2. Index selection (deliberately simple, documented as such):
   - The WHERE clause is split into AND-ed conjuncts (an OR is opaque).
   - For each scanned table, the first conjunct of the form
     `column OP literal` (or `literal OP column`) where OP is one of
     = < <= > >= and the column has an index becomes an IndexScan for that
     table. `!=` is never indexed: it matches almost everything, so an index
     would only add random heap reads on top of touching every entry.
   - Every conjunct not consumed by a scan becomes a Filter on top (above
     the join, if there is one).
   A real optimizer would also estimate selectivity (e.g. via histograms)
   before deciding a scan is worthwhile; MiniSQL always prefers an index
   when one is available on an eligible column, since it has no statistics
   to reason about cost otherwise.
"""

from __future__ import annotations
from dataclasses import dataclass
from .ast_nodes import SelectStmt, BinOp, ColumnRef, Literal, AggExpr
from .catalog import Catalog

INDEXABLE_OPS = {"=", "<", "<=", ">", ">="}


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
    key: str        # a row-dict key: column name, or an aggregate's display name
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


# ---------------- Semantic analysis ----------------

class _Scope:
    """The tables visible to a statement, and how to resolve names in them."""

    def __init__(self, catalog: Catalog, tables: list[str]):
        self.tables = tables
        self.schemas = {t: catalog.get_table(t) for t in tables}

    def resolve(self, name: str, table: str | None = None) -> str:
        """Return the owning table of `name`, raising on unknown/ambiguous."""
        if table is not None:
            if table not in self.schemas:
                raise ValueError(f"No such table in query: {table!r}")
            if name not in self.schemas[table].column_names():
                raise ValueError(f"No such column: {table}.{name}")
            return table
        owners = [t for t in self.tables if name in self.schemas[t].column_names()]
        if not owners:
            raise ValueError(f"No such column: {name!r}")
        if len(owners) > 1:
            raise ValueError(
                f"Column {name!r} is ambiguous (exists in {' and '.join(owners)}); qualify it"
            )
        return owners[0]

    def resolve_str(self, ref: str) -> None:
        """Validate a 'col' or 'table.col' string from the select list."""
        if "." in ref:
            table, col = ref.split(".", 1)
            self.resolve(col, table)
        else:
            self.resolve(ref)


def _walk_columns(expr):
    if isinstance(expr, ColumnRef):
        yield expr
    elif isinstance(expr, BinOp):
        yield from _walk_columns(expr.left)
        yield from _walk_columns(expr.right)


def _resolve_expr(expr, scope: _Scope, qualify: bool) -> None:
    for col in _walk_columns(expr):
        owner = scope.resolve(col.name, col.table)
        if qualify:
            col.table = owner


def validate_where(where, table: str, catalog: Catalog) -> None:
    """Semantic check for a single-table WHERE (UPDATE / DELETE)."""
    if where is not None:
        _resolve_expr(where, _Scope(catalog, [table]), qualify=False)


def validate_select(stmt: SelectStmt, catalog: Catalog) -> None:
    tables = [stmt.table] + ([stmt.join.table] if stmt.join else [])
    if stmt.join and stmt.join.table == stmt.table:
        raise ValueError("Self-joins are not supported")
    scope = _Scope(catalog, tables)
    has_join = stmt.join is not None

    if stmt.join:
        for col in (stmt.join.left_col, stmt.join.right_col):
            col.table = scope.resolve(col.name, col.table)
    if stmt.where is not None:
        # In a join, fill in each ColumnRef's owning table so index selection
        # knows which scan a predicate belongs to.
        _resolve_expr(stmt.where, scope, qualify=has_join)
    for c in stmt.columns:
        if isinstance(c, AggExpr):
            if c.arg != "*":
                scope.resolve_str(c.arg)
        elif c != "*":
            scope.resolve_str(c)
    if stmt.group_by is not None:
        scope.resolve_str(stmt.group_by)
    if isinstance(stmt.order_by, str):
        scope.resolve_str(stmt.order_by)
    elif isinstance(stmt.order_by, AggExpr) and stmt.order_by.arg != "*":
        scope.resolve_str(stmt.order_by.arg)


# ---------------- Index selection ----------------

def _split_conjuncts(expr) -> list:
    """Flatten a left-leaning AND-chain into a flat list of conjuncts.
    Stops flattening at an OR (OR is not decomposable this way)."""
    if isinstance(expr, BinOp) and expr.op == "AND":
        return _split_conjuncts(expr.left) + _split_conjuncts(expr.right)
    return [expr]


def _is_indexable(cond, table: str, catalog: Catalog):
    """If `cond` is a simple `column OP literal` (or `literal OP column`)
    against `table`'s own column and that column has an index, return
    (column, op, value). Otherwise return None."""
    if not (isinstance(cond, BinOp) and cond.op in INDEXABLE_OPS):
        return None
    left, right = cond.left, cond.right
    if isinstance(left, ColumnRef) and isinstance(right, Literal):
        col, op, val = left, cond.op, right.value
    elif isinstance(right, ColumnRef) and isinstance(left, Literal):
        # normalize "25 < age" into "age > 25"
        flip = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
        col, op, val = right, flip[cond.op], left.value
    else:
        return None
    if col.table is not None and col.table != table:
        return None
    if val is None:
        return None          # `col = NULL` matches nothing; leave it to the Filter
    if not catalog.has_index(table, col.name):
        return None
    return col.name, op, val


def pick_index_conjunct(conjuncts: list, table: str, catalog: Catalog):
    """First conjunct usable as an IndexScan on `table`, as
    (conjunct, (column, op, value)) — or None."""
    for c in conjuncts:
        hit = _is_indexable(c, table, catalog)
        if hit is not None:
            return c, hit
    return None


def _choose_scan(table: str, conjuncts: list, catalog: Catalog):
    chosen = pick_index_conjunct(conjuncts, table, catalog)
    if chosen is None:
        return SeqScanNode(table), None
    used, (col, op, val) = chosen
    return IndexScanNode(table, col, op, val), used


def _and_together(conjuncts: list):
    expr = None
    for c in conjuncts:
        expr = c if expr is None else BinOp(expr, "AND", c)
    return expr


def _resolve_columns(stmt: SelectStmt, catalog: Catalog) -> list[str]:
    if stmt.columns != ["*"]:
        return stmt.columns
    base_cols = catalog.get_table(stmt.table).column_names()
    if stmt.join is None:
        return base_cols
    join_cols = catalog.get_table(stmt.join.table).column_names()
    return [f"{stmt.table}.{c}" for c in base_cols] + \
           [f"{stmt.join.table}.{c}" for c in join_cols]


def output_name(item) -> str:
    """Row-dict key / display name of a select-list or ORDER BY item."""
    if isinstance(item, AggExpr):
        return f"{item.func}({item.arg})"
    return item


# ---------------- Planning ----------------

def build_plan(stmt: SelectStmt, catalog: Catalog):
    validate_select(stmt, catalog)
    conjuncts = _split_conjuncts(stmt.where) if stmt.where is not None else []
    used = []

    # 1. base scan
    plan, u = _choose_scan(stmt.table, conjuncts, catalog)
    used.append(u)

    # 2. join (the right side gets its own index-vs-scan decision)
    if stmt.join is not None:
        right, u = _choose_scan(stmt.join.table, conjuncts, catalog)
        used.append(u)
        left_key = f"{stmt.join.left_col.table}.{stmt.join.left_col.name}"
        right_key = f"{stmt.join.right_col.table}.{stmt.join.right_col.name}"
        plan = NestedLoopJoinNode(plan, right, left_key, right_key)

    # 3. whatever the scans didn't consume becomes a Filter
    remaining = _and_together([c for c in conjuncts if not any(c is u for u in used)])
    if remaining is not None:
        plan = FilterNode(plan, remaining)

    has_agg = any(isinstance(c, AggExpr) for c in stmt.columns)
    is_agg_query = has_agg or stmt.group_by is not None
    order_key = output_name(stmt.order_by) if stmt.order_by is not None else None

    if is_agg_query:
        # Every non-aggregate column in the select list must be the GROUP BY
        # column itself — the same rule real engines enforce (a plain column
        # not in GROUP BY has no single well-defined value per group).
        for c in stmt.columns:
            if not isinstance(c, AggExpr) and c != stmt.group_by:
                raise ValueError(
                    f"Column {c!r} must appear in GROUP BY or be wrapped in "
                    f"an aggregate function"
                )
        plan = AggregateNode(plan, stmt.group_by, stmt.columns)
        # 4. sort — on the aggregated rows, so the key must be an output column
        if order_key is not None:
            if order_key not in {output_name(c) for c in stmt.columns}:
                raise ValueError(
                    f"ORDER BY {order_key} must appear in the SELECT list of an aggregate query"
                )
            plan = SortNode(plan, order_key, stmt.order_desc)
        # No separate Project step — AggregateNode already emits exactly
        # the requested output columns in order.
    else:
        if isinstance(stmt.order_by, AggExpr):
            raise ValueError(f"ORDER BY {order_key} requires an aggregate query")
        # 4. sort
        if order_key is not None:
            plan = SortNode(plan, order_key, stmt.order_desc)
        # 5. project
        plan = ProjectNode(plan, _resolve_columns(stmt, catalog))

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
        cols_str = ", ".join(output_name(c) for c in plan.columns)
        gb = f", GROUP BY {plan.group_by}" if plan.group_by else ""
        return f"{pad}Aggregate({cols_str}{gb})\n{explain(plan.child, indent + 1)}"
    if isinstance(plan, LimitNode):
        return f"{pad}Limit({plan.n})\n{explain(plan.child, indent + 1)}"
    return f"{pad}Unknown({plan!r})"


def _expr_str(expr) -> str:
    if isinstance(expr, Literal):
        return "NULL" if expr.value is None else repr(expr.value)
    if isinstance(expr, ColumnRef):
        return f"{expr.table}.{expr.name}" if expr.table else expr.name
    if isinstance(expr, BinOp):
        if expr.op in ("IS", "IS NOT"):
            return f"({_expr_str(expr.left)} {expr.op} NULL)"
        return f"({_expr_str(expr.left)} {expr.op} {_expr_str(expr.right)})"
    return str(expr)
