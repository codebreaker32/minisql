"""
ast_nodes.py — AST node definitions.

Every parsed SQL statement becomes one of these dataclasses. Expressions
(WHERE clauses, join conditions) are represented as a small recursive
expression tree: Literal / ColumnRef are leaves, BinOp is the only internal
node type (it covers comparisons like `age > 25`, null tests like
`age IS NULL`, and boolean logic like `a AND b`, distinguished by `op`).
"""

from __future__ import annotations
from dataclasses import dataclass, field


# ---------- Expressions ----------

class Expr:
    pass


@dataclass
class Literal(Expr):
    value: object  # int, float, str, bool, None


@dataclass
class ColumnRef(Expr):
    name: str
    table: str | None = None  # set when qualified, e.g. t1.col (or resolved by the planner)


@dataclass
class BinOp(Expr):
    left: Expr
    op: str   # one of: = != < <= > >= IS "IS NOT" AND OR
    right: Expr


# ---------- Statements ----------

@dataclass
class ColumnDef:
    name: str
    type: str
    primary_key: bool = False


@dataclass
class CreateTableStmt:
    table_name: str
    columns: list[ColumnDef]


@dataclass
class CreateIndexStmt:
    index_name: str
    table_name: str
    column: str


@dataclass
class InsertStmt:
    table_name: str
    columns: list[str] | None  # None means "all columns, in schema order"
    values: list[object]


@dataclass
class JoinClause:
    table: str
    left_col: ColumnRef
    right_col: ColumnRef


@dataclass
class AggExpr:
    func: str    # COUNT | SUM | AVG | MIN | MAX
    arg: str     # column name, or '*' (only valid for COUNT)


@dataclass
class SelectStmt:
    columns: list          # each item is a column name (str), '*', or an AggExpr
    table: str
    join: JoinClause | None = None
    where: Expr | None = None
    group_by: str | None = None
    order_by: object = None       # column name (str) or AggExpr, e.g. ORDER BY COUNT(*)
    order_desc: bool = False
    limit: int | None = None


@dataclass
class UpdateStmt:
    table_name: str
    assignments: dict            # {column_name: new_value}
    where: Expr | None = None


@dataclass
class DeleteStmt:
    table_name: str
    where: Expr | None = None


@dataclass
class TransactionStmt:
    kind: str   # "BEGIN" | "COMMIT" | "ROLLBACK"


@dataclass
class ExplainStmt:
    inner: SelectStmt
