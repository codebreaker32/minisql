"""
ast_nodes.py — AST node definitions.

Every parsed SQL statement becomes one of these dataclasses. Expressions
(WHERE clauses, join conditions) are represented as a small recursive
expression tree: Literal / ColumnRef are leaves, BinOp is the only internal
node type (it covers both comparisons like `age > 25` and boolean logic like
`a AND b`, distinguished by the `op` field).
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
    table: str | None = None  # set when qualified, e.g. t1.col


@dataclass
class BinOp(Expr):
    left: Expr
    op: str   # one of: = != < <= > >= AND OR
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
    order_by: str | None = None
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
