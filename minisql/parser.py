"""
parser.py — Recursive descent parser for MiniSQL.

Grammar supported (informal EBNF):

    statement    := create_table | create_index | insert | update | delete
                  | select | explain | BEGIN [TRANSACTION] | COMMIT | ROLLBACK
    create_table := CREATE TABLE ident '(' coldef (',' coldef)* ')'
    coldef       := ident type [PRIMARY KEY]
    create_index := CREATE INDEX ident ON ident '(' ident ')'
    insert       := INSERT INTO ident ['(' ident (',' ident)* ')'] VALUES '(' literal (',' literal)* ')'
    update       := UPDATE ident SET ident '=' literal (',' ident '=' literal)* [where]
    delete       := DELETE FROM ident [where]
    select       := SELECT collist FROM ident [join] [where] [group_by] [order_by] [limit]
    collist      := '*' | select_item (',' select_item)*
    select_item  := agg '(' ('*' | ident) ')' | ident ['.' ident]
    join         := [INNER] JOIN ident ON column_ref '=' column_ref
    where        := WHERE or_expr
    or_expr      := and_expr (OR and_expr)*
    and_expr     := comparison (AND comparison)*
    comparison   := operand ('=' | '!=' | '<' | '<=' | '>' | '>=') operand
                  | operand IS [NOT] NULL
    operand      := column_ref | NUMBER | STRING | TRUE | FALSE | NULL
    group_by     := GROUP BY ident
    order_by     := ORDER BY select_item [ASC | DESC]
    limit        := LIMIT NUMBER
    explain      := EXPLAIN select

An `ident` is an IDENT token, a "double-quoted identifier", or one of the
NON_RESERVED keywords (type names, aggregate names, KEY, INDEX, ...), so a
column can be called `key`, `count` or `text` without quoting.

Each parse_X method consumes exactly the tokens for X and leaves the cursor
positioned right after it, which is the standard recursive-descent contract.
"""

from __future__ import annotations
from .lexer import Token, TokType, tokenize
from .ast_nodes import (
    ColumnDef, CreateTableStmt, CreateIndexStmt, InsertStmt,
    SelectStmt, JoinClause, ExplainStmt, ColumnRef, Literal, BinOp,
    UpdateStmt, DeleteStmt, TransactionStmt, AggExpr,
)

COLUMN_TYPES = {"INT", "INTEGER", "TEXT", "VARCHAR", "REAL", "FLOAT"}
AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}

# Keywords that may also be used as plain identifiers (column/table/index
# names) because their position in the grammar is never ambiguous.
NON_RESERVED = COLUMN_TYPES | AGG_FUNCS | {"KEY", "INDEX", "TRANSACTION"}


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # ---- token stream helpers ----

    def peek(self, offset: int = 0) -> Token:
        j = min(self.i + offset, len(self.tokens) - 1)
        return self.tokens[j]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def check_kw(self, kw: str) -> bool:
        t = self.peek()
        return t.type == TokType.KEYWORD and t.value == kw

    def check_punct(self, p: str) -> bool:
        t = self.peek()
        return t.type == TokType.PUNCT and t.value == p

    def expect_kw(self, kw: str) -> Token:
        if not self.check_kw(kw):
            raise ParseError(f"Expected keyword {kw!r}, got {self.peek()!r}")
        return self.advance()

    def expect_type(self, ttype: TokType) -> Token:
        if self.peek().type != ttype:
            raise ParseError(f"Expected {ttype.name}, got {self.peek()!r}")
        return self.advance()

    def expect_punct(self, p: str) -> Token:
        if not self.check_punct(p):
            raise ParseError(f"Expected {p!r}, got {self.peek()!r}")
        return self.advance()

    def expect_op(self, op: str) -> Token:
        t = self.peek()
        if t.type != TokType.OP or t.value != op:
            raise ParseError(f"Expected operator {op!r}, got {t!r}")
        return self.advance()

    @staticmethod
    def is_ident(t: Token) -> bool:
        return t.type == TokType.IDENT or (
            t.type == TokType.KEYWORD and t.value in NON_RESERVED
        )

    def expect_ident(self) -> str:
        t = self.peek()
        if not self.is_ident(t):
            raise ParseError(f"Expected identifier, got {t!r}")
        self.advance()
        # a non-reserved keyword used as a name keeps the case the user typed
        return t.text if t.type == TokType.KEYWORD else t.value

    # ---- entry point ----

    def parse_statement(self):
        if self.check_kw("CREATE"):
            return self.parse_create()
        if self.check_kw("INSERT"):
            return self.parse_insert()
        if self.check_kw("UPDATE"):
            return self.parse_update()
        if self.check_kw("DELETE"):
            return self.parse_delete()
        if self.check_kw("SELECT"):
            return self.parse_select()
        if self.check_kw("BEGIN"):
            self.advance()
            if self.check_kw("TRANSACTION"):
                self.advance()
            return TransactionStmt("BEGIN")
        if self.check_kw("COMMIT"):
            self.advance()
            return TransactionStmt("COMMIT")
        if self.check_kw("ROLLBACK"):
            self.advance()
            return TransactionStmt("ROLLBACK")
        if self.check_kw("EXPLAIN"):
            self.advance()
            inner = self.parse_select()
            return ExplainStmt(inner)
        raise ParseError(f"Unexpected token at start of statement: {self.peek()!r}")

    # ---- CREATE ----

    def parse_create(self):
        self.expect_kw("CREATE")
        if self.check_kw("TABLE"):
            return self.parse_create_table()
        if self.check_kw("INDEX"):
            return self.parse_create_index()
        raise ParseError(f"Expected TABLE or INDEX after CREATE, got {self.peek()!r}")

    def parse_create_table(self):
        self.expect_kw("TABLE")
        name = self.expect_ident()
        self.expect_punct("(")
        cols = [self.parse_column_def()]
        while self.check_punct(","):
            self.advance()
            cols.append(self.parse_column_def())
        self.expect_punct(")")
        return CreateTableStmt(name, cols)

    def parse_column_def(self) -> ColumnDef:
        name = self.expect_ident()
        type_tok = self.advance()
        if type_tok.type != TokType.KEYWORD or type_tok.value not in COLUMN_TYPES:
            raise ParseError(f"Expected a column type, got {type_tok!r}")
        pk = False
        if self.check_kw("PRIMARY"):
            self.advance()
            self.expect_kw("KEY")
            pk = True
        return ColumnDef(name, type_tok.value, pk)

    def parse_create_index(self):
        self.expect_kw("INDEX")
        idx_name = self.expect_ident()
        self.expect_kw("ON")
        table_name = self.expect_ident()
        self.expect_punct("(")
        col = self.expect_ident()
        self.expect_punct(")")
        return CreateIndexStmt(idx_name, table_name, col)

    # ---- INSERT ----

    def parse_insert(self):
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.expect_ident()
        columns = None
        if self.check_punct("("):
            self.advance()
            columns = [self.expect_ident()]
            while self.check_punct(","):
                self.advance()
                columns.append(self.expect_ident())
            self.expect_punct(")")
        self.expect_kw("VALUES")
        self.expect_punct("(")
        values = [self.parse_literal_value()]
        while self.check_punct(","):
            self.advance()
            values.append(self.parse_literal_value())
        self.expect_punct(")")
        return InsertStmt(table, columns, values)

    def parse_literal_value(self):
        t = self.advance()
        if t.type == TokType.NUMBER:
            return float(t.value) if "." in t.value else int(t.value)
        if t.type == TokType.STRING:
            return t.value
        if t.type == TokType.KEYWORD and t.value == "TRUE":
            return True
        if t.type == TokType.KEYWORD and t.value == "FALSE":
            return False
        if t.type == TokType.KEYWORD and t.value == "NULL":
            return None
        raise ParseError(f"Expected a literal value, got {t!r}")

    # ---- UPDATE ----

    def parse_update(self):
        self.expect_kw("UPDATE")
        table = self.expect_ident()
        self.expect_kw("SET")
        assignments = {}
        col = self.expect_ident()
        self.expect_op("=")
        assignments[col] = self.parse_literal_value()
        while self.check_punct(","):
            self.advance()
            col = self.expect_ident()
            self.expect_op("=")
            assignments[col] = self.parse_literal_value()
        where = None
        if self.check_kw("WHERE"):
            self.advance()
            where = self.parse_or_expr()
        return UpdateStmt(table, assignments, where)

    # ---- DELETE ----

    def parse_delete(self):
        self.expect_kw("DELETE")
        self.expect_kw("FROM")
        table = self.expect_ident()
        where = None
        if self.check_kw("WHERE"):
            self.advance()
            where = self.parse_or_expr()
        return DeleteStmt(table, where)

    # ---- SELECT ----

    def parse_select(self) -> SelectStmt:
        self.expect_kw("SELECT")
        columns = self.parse_column_list()
        self.expect_kw("FROM")
        table = self.expect_ident()

        join = None
        if self.check_kw("JOIN") or self.check_kw("INNER"):
            join = self.parse_join()

        where = None
        if self.check_kw("WHERE"):
            self.advance()
            where = self.parse_or_expr()

        group_by = None
        if self.check_kw("GROUP"):
            self.advance()
            self.expect_kw("BY")
            group_by = self.parse_qualified_ident()

        order_by = None
        order_desc = False
        if self.check_kw("ORDER"):
            self.advance()
            self.expect_kw("BY")
            order_by = self.parse_select_item()   # a column, or e.g. COUNT(*)
            if self.check_kw("DESC"):
                self.advance()
                order_desc = True
            elif self.check_kw("ASC"):
                self.advance()

        limit = None
        if self.check_kw("LIMIT"):
            self.advance()
            limit = int(self.expect_type(TokType.NUMBER).value)

        return SelectStmt(columns, table, join, where, group_by, order_by, order_desc, limit)

    def parse_column_list(self) -> list:
        if self.check_punct("*"):
            self.advance()
            return ["*"]
        cols = [self.parse_select_item()]
        while self.check_punct(","):
            self.advance()
            cols.append(self.parse_select_item())
        return cols

    def parse_select_item(self):
        t = self.peek()
        # An aggregate is `FUNC (`; a bare `count` not followed by '(' is just
        # a column that happens to be called count.
        if (t.type == TokType.KEYWORD and t.value in AGG_FUNCS
                and self.peek(1).type == TokType.PUNCT and self.peek(1).value == "("):
            func = t.value
            self.advance()
            self.expect_punct("(")
            if self.check_punct("*"):
                if func != "COUNT":
                    raise ParseError(f"{func}(*) is not valid — only COUNT(*) is allowed")
                self.advance()
                arg = "*"
            else:
                arg = self.parse_qualified_ident()
            self.expect_punct(")")
            return AggExpr(func, arg)
        return self.parse_qualified_ident()

    def parse_qualified_ident(self) -> str:
        name = self.expect_ident()
        if self.check_punct("."):
            self.advance()
            name2 = self.expect_ident()
            return f"{name}.{name2}"
        return name

    def parse_join(self) -> JoinClause:
        if self.check_kw("INNER"):
            self.advance()
        self.expect_kw("JOIN")
        table = self.expect_ident()
        self.expect_kw("ON")
        left = self.parse_column_ref()
        self.expect_op("=")   # only equi-joins are supported; anything else is an error
        right = self.parse_column_ref()
        return JoinClause(table, left, right)

    def parse_column_ref(self) -> ColumnRef:
        name = self.expect_ident()
        if self.check_punct("."):
            self.advance()
            col = self.expect_ident()
            return ColumnRef(col, table=name)
        return ColumnRef(name)

    # ---- WHERE expression grammar (precedence: OR < AND < comparison) ----

    def parse_or_expr(self) -> BinOp:
        left = self.parse_and_expr()
        while self.check_kw("OR"):
            self.advance()
            right = self.parse_and_expr()
            left = BinOp(left, "OR", right)
        return left

    def parse_and_expr(self):
        left = self.parse_comparison()
        while self.check_kw("AND"):
            self.advance()
            right = self.parse_comparison()
            left = BinOp(left, "AND", right)
        return left

    def parse_comparison(self) -> BinOp:
        left = self.parse_operand()
        if self.check_kw("IS"):
            self.advance()
            op = "IS"
            if self.check_kw("NOT"):
                self.advance()
                op = "IS NOT"
            self.expect_kw("NULL")
            return BinOp(left, op, Literal(None))
        op_tok = self.expect_type(TokType.OP)
        op = "!=" if op_tok.value == "<>" else op_tok.value
        right = self.parse_operand()
        return BinOp(left, op, right)

    def parse_operand(self) -> Expr:
        t = self.peek()
        if t.type == TokType.KEYWORD and t.value in ("TRUE", "FALSE", "NULL"):
            return Literal(self.parse_literal_value())
        if self.is_ident(t):
            return self.parse_column_ref()
        if t.type in (TokType.NUMBER, TokType.STRING):
            return Literal(self.parse_literal_value())
        raise ParseError(f"Expected column or literal, got {t!r}")


def parse(sql: str):
    tokens = tokenize(sql)
    parser = Parser(tokens)
    stmt = parser.parse_statement()
    # allow an optional trailing semicolon
    if parser.check_punct(";"):
        parser.advance()
    if parser.peek().type != TokType.EOF:
        raise ParseError(f"Unexpected trailing tokens starting at {parser.peek()!r}")
    return stmt
