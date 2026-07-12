"""
lexer.py — Tokenizer for MiniSQL.

Converts a raw SQL string into a flat list of Token objects. Uses a single
compiled regex with named groups (the classic "longest match wins" tokenizer
pattern) rather than hand-rolled character-by-character scanning, because it's
far less error prone for things like multi-character operators (<=, >=, !=).
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum, auto


class TokType(Enum):
    KEYWORD = auto()
    IDENT = auto()
    NUMBER = auto()
    STRING = auto()
    OP = auto()
    PUNCT = auto()
    EOF = auto()


KEYWORDS = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT",
    "ORDER", "BY", "GROUP", "ASC", "DESC", "LIMIT",
    "INSERT", "INTO", "VALUES",
    "UPDATE", "SET", "DELETE",
    "CREATE", "TABLE", "INDEX", "ON",
    "JOIN", "INNER",
    "PRIMARY", "KEY",
    "INT", "INTEGER", "TEXT", "VARCHAR", "REAL", "FLOAT",
    "EXPLAIN", "TRUE", "FALSE", "NULL",
    "BEGIN", "COMMIT", "ROLLBACK", "TRANSACTION",
    "COUNT", "SUM", "AVG", "MIN", "MAX",
}


@dataclass
class Token:
    type: TokType
    value: str
    pos: int

    def __repr__(self):
        return f"Token({self.type.name}, {self.value!r})"


# Order matters: longer operators must come before shorter prefixes of themselves.
TOKEN_REGEX = re.compile(r"""
    (?P<WS>\s+)
  | (?P<NUMBER>\d+\.\d+|\d+)
  | (?P<STRING>'(?:[^'\\]|\\.)*')
  | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<OP><=|>=|!=|<>|=|<|>)
  | (?P<PUNCT>[(),;.*])
""", re.VERBOSE)


class LexError(Exception):
    pass


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    length = len(sql)
    while pos < length:
        m = TOKEN_REGEX.match(sql, pos)
        if not m:
            raise LexError(f"Unexpected character {sql[pos]!r} at position {pos}")
        kind = m.lastgroup
        text = m.group()
        if kind == "WS":
            pos = m.end()
            continue
        if kind == "IDENT" and text.upper() in KEYWORDS:
            tokens.append(Token(TokType.KEYWORD, text.upper(), pos))
        elif kind == "STRING":
            # strip quotes, unescape \' 
            inner = text[1:-1].replace("\\'", "'")
            tokens.append(Token(TokType.STRING, inner, pos))
        else:
            tokens.append(Token(TokType[kind], text, pos))
        pos = m.end()
    tokens.append(Token(TokType.EOF, "", pos))
    return tokens
