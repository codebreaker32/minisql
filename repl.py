"""
repl.py — Interactive command-line shell for MiniSQL.

Run: python3 repl.py [data_dir]

This is what you'd actually open in an interview to demo the engine live:
type a query, see the parse -> plan -> result happen in real time, and use
EXPLAIN to show the index-vs-scan decision.
"""

import sys
from minisql.engine import Engine
from minisql.parser import ParseError
from minisql.lexer import LexError


BANNER = """MiniSQL — a SQL query engine built from scratch (lexer, parser,
B-tree index, heap storage, Volcano-style executor, transactions).

Type SQL statements ending in ';' is optional. Type 'exit' or Ctrl-D to quit.
Try: CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT);
     INSERT INTO users VALUES (1, 'Alice', 30);
     SELECT * FROM users WHERE age > 25;
     UPDATE users SET age = 31 WHERE id = 1;
     DELETE FROM users WHERE age > 60;
     SELECT age, COUNT(*) FROM users GROUP BY age;
     BEGIN; INSERT INTO users VALUES (2, 'Bob', 40); ROLLBACK;
     EXPLAIN SELECT * FROM users WHERE age > 25;
"""


def format_rows(rows: list[dict]) -> str:
    if not rows:
        return "(0 rows)"
    cols = list(rows[0].keys())
    widths = [max(len(str(c)), max((len(str(r.get(c, ""))) for r in rows), default=0)) for c in cols]
    lines = []
    header = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    lines.append(header)
    lines.append("-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)))
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    engine = Engine(data_dir=data_dir)
    print(BANNER)

    buffer = ""
    while True:
        try:
            prompt = "minisql> " if not buffer else "     ... "
            line = input(prompt)
        except EOFError:
            print()
            break

        if not buffer and line.strip().lower() in ("exit", "quit"):
            break

        buffer += (" " if buffer else "") + line
        if not buffer.strip().endswith(";") and buffer.strip() != "":
            # allow simple statements without trailing ';' too — only keep
            # buffering if the line looks obviously incomplete
            if line.strip() == "":
                continue

        sql = buffer.strip()
        buffer = ""
        if not sql:
            continue

        try:
            result = engine.execute_sql(sql)
        except (ParseError, LexError) as e:
            print(f"SQL error: {e}")
            continue
        except Exception as e:
            print(f"Error: {e}")
            continue

        if result is None:
            print("OK")
        elif isinstance(result, str):  # EXPLAIN output
            print(result)
        elif isinstance(result, int):  # UPDATE / DELETE affected-row count
            print(f"OK ({result} row{'s' if result != 1 else ''} affected)")
        else:
            print(format_rows(result))


if __name__ == "__main__":
    main()
