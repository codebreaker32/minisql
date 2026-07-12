"""
parser.py — Recursive descent parser for MiniSQL.

Grammar supported (informal EBNF):

    statement    := create_table | create_index | insert | select | explain
    create_table := CREATE TABLE ident '(' coldef (',' coldef)* ')'
    coldef       := ident type [PRIMARY KEY]
    create_index := CREATE INDEX ident ON ident '(' ident ')'
    insert       := INSERT INTO ident ['(' ident (',' ident)* ')'] VALUES '(' literal (',' literal)* ')'
    select       := SELECT collist FROM ident [join] [where] [order_by] [limit]
    collist      := '*' | ident (',' ident)*
    join         := [INNER] JOIN ident ON ident '.' ident '=' ident '.' ident
    where        := WHERE or_expr
    or_expr      := and_expr (OR and_expr)*
    and_expr     := comparison (AND comparison)*
    comparison   := operand ('=' | '!=' | '<' | '<=' | '>' | '>=') operand
    operand      := ident ['.' ident] | NUMBER | STRING
    order_by     := ORDER BY ident [ASC | DESC]
    limit        := LIMIT NUMBER
    explain      := EXPLAIN select

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


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # ---- token stream helpers ----

    def peek(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def check_kw(self, kw: str) -> bool:
        t = self.peek()
        return t.type == TokType.KEYWORD and t.value == kw

    def expect_kw(self, kw: str) -> Token:
        if not self.check_kw(kw):
            raise ParseError(f"Expected keyword {kw!r}, got {self.peek()!r}")
        return self.advance()

    def expect_type(self, ttype: TokType) -> Token:
        if self.peek().type != ttype:
            raise ParseError(f"Expected {ttype.name}, got {self.peek()!r}")
        return self.advance()

    def expect_punct(self, p: str) -> Token:
        t = self.peek()
        if t.type != TokType.PUNCT or t.value != p:
            raise ParseError(f"Expected {p!r}, got {t!r}")
        return self.advance()

    def expect_op(self, op: str) -> Token:
        t = self.peek()
        if t.type != TokType.OP or t.value != op:
            raise ParseError(f"Expected operator {op!r}, got {t!r}")
        return self.advance()

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
        name = self.expect_type(TokType.IDENT).value
        self.expect_punct("(")
        cols = [self.parse_column_def()]
        while self.peek().type == TokType.PUNCT and self.peek().value == ",":
            self.advance()
            cols.append(self.parse_column_def())
        self.expect_punct(")")
        return CreateTableStmt(name, cols)

    def parse_column_def(self) -> ColumnDef:
        name = self.expect_type(TokType.IDENT).value
        type_tok = self.advance()
        if type_tok.type != TokType.KEYWORD or type_tok.value not in (
            "INT", "INTEGER", "TEXT", "VARCHAR", "REAL", "FLOAT",
        ):
            raise ParseError(f"Expected a column type, got {type_tok!r}")
        pk = False
        if self.check_kw("PRIMARY"):
            self.advance()
            self.expect_kw("KEY")
            pk = True
        return ColumnDef(name, type_tok.value, pk)

    def parse_create_index(self):
        self.expect_kw("INDEX")
        idx_name = self.expect_type(TokType.IDENT).value
        self.expect_kw("ON")
        table_name = self.expect_type(TokType.IDENT).value
        self.expect_punct("(")
        col = self.expect_type(TokType.IDENT).value
        self.expect_punct(")")
        return CreateIndexStmt(idx_name, table_name, col)

    # ---- INSERT ----

    def parse_insert(self):
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.expect_type(TokType.IDENT).value
        columns = None
        if self.peek().type == TokType.PUNCT and self.peek().value == "(":
            self.advance()
            columns = [self.expect_type(TokType.IDENT).value]
            while self.peek().value == ",":
                self.advance()
                columns.append(self.expect_type(TokType.IDENT).value)
            self.expect_punct(")")
        self.expect_kw("VALUES")
        self.expect_punct("(")
        values = [self.parse_literal_value()]
        while self.peek().value == ",":
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
        table = self.expect_type(TokType.IDENT).value
        self.expect_kw("SET")
        assignments = {}
        col = self.expect_type(TokType.IDENT).value
        self.expect_op("=")
        assignments[col] = self.parse_literal_value()
        while self.peek().type == TokType.PUNCT and self.peek().value == ",":
            self.advance()
            col = self.expect_type(TokType.IDENT).value
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
        table = self.expect_type(TokType.IDENT).value
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
        table = self.expect_type(TokType.IDENT).value

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
            group_by = self.expect_type(TokType.IDENT).value

        order_by = None
        order_desc = False
        if self.check_kw("ORDER"):
            self.advance()
            self.expect_kw("BY")
            order_by = self.expect_type(TokType.IDENT).value
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
        if self.peek().type == TokType.PUNCT and self.peek().value == "*":
            self.advance()
            return ["*"]
        cols = [self.parse_select_item()]
        while self.peek().type == TokType.PUNCT and self.peek().value == ",":
            self.advance()
            cols.append(self.parse_select_item())
        return cols

    def parse_select_item(self):
        t = self.peek()
        if t.type == TokType.KEYWORD and t.value in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
            func = t.value
            self.advance()
            self.expect_punct("(")
            if self.peek().type == TokType.PUNCT and self.peek().value == "*":
                if func != "COUNT":
                    raise ParseError(f"{func}(*) is not valid — only COUNT(*) is allowed")
                self.advance()
                arg = "*"
            else:
                arg = self.expect_type(TokType.IDENT).value
            self.expect_punct(")")
            return AggExpr(func, arg)
        return self.parse_qualified_ident()

    def parse_qualified_ident(self) -> str:
        name = self.expect_type(TokType.IDENT).value
        if self.peek().type == TokType.PUNCT and self.peek().value == ".":
            self.advance()
            name2 = self.expect_type(TokType.IDENT).value
            return f"{name}.{name2}"
        return name

    def parse_join(self) -> JoinClause:
        if self.check_kw("INNER"):
            self.advance()
        self.expect_kw("JOIN")
        table = self.expect_type(TokType.IDENT).value
        self.expect_kw("ON")
        left = self.parse_column_ref()
        self.expect_type(TokType.OP)  # '=' — join conditions are always equality here
        right = self.parse_column_ref()
        return JoinClause(table, left, right)

    def parse_column_ref(self) -> ColumnRef:
        name = self.expect_type(TokType.IDENT).value
        if self.peek().type == TokType.PUNCT and self.peek().value == ".":
            self.advance()
            col = self.expect_type(TokType.IDENT).value
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
        op_tok = self.expect_type(TokType.OP)
        op = "!=" if op_tok.value == "<>" else op_tok.value
        right = self.parse_operand()
        return BinOp(left, op, right)

    def parse_operand(self) -> Expr:
        t = self.peek()
        if t.type == TokType.IDENT:
            return self.parse_column_ref()
        if t.type in (TokType.NUMBER, TokType.STRING) or (
            t.type == TokType.KEYWORD and t.value in ("TRUE", "FALSE", "NULL")
        ):
            return Literal(self.parse_literal_value())
        raise ParseError(f"Expected column or literal, got {t!r}")


def parse(sql: str):
    tokens = tokenize(sql)
    parser = Parser(tokens)
    stmt = parser.parse_statement()
    # allow an optional trailing semicolon
    if parser.peek().type == TokType.PUNCT and parser.peek().value == ";":
        parser.advance()
    if parser.peek().type != TokType.EOF:
        raise ParseError(f"Unexpected trailing tokens starting at {parser.peek()!r}")
    return stmt
