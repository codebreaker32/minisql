"""
benchmark.py — Indexed lookup vs full table scan on a 100k-row table.

Run: python3 benchmark.py

Loads 100,000 rows into a `users` table, then times a single equality
lookup (WHERE age = <value>) two ways:
  1. Before any index exists -> planner falls back to SeqScan + Filter,
     which touches every row.
  2. After CREATE INDEX idx_age ON users(age) -> planner picks IndexScan,
     which does an O(log n) B-tree search plus O(k) row fetches for the k
     matches.

Also sweeps table size (1k / 10k / 50k / 100k / 200k rows) to show how the
gap grows, and saves a chart to benchmark_result.png.
"""

import os
import random
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minisql.engine import Engine

DATA_DIR = "bench_data"


def build_table(n_rows: int) -> Engine:
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    # sync=False: skip fsync during the bulk load (like PRAGMA synchronous=OFF).
    # Query timings are unaffected; only load time is.
    engine = Engine(data_dir=DATA_DIR, sync=False)
    engine.execute_sql("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, age INT)")
    random.seed(0)
    names = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi"]
    for i in range(n_rows):
        name = random.choice(names)
        age = random.randint(18, 80)
        engine.execute_sql(f"INSERT INTO users VALUES ({i}, '{name}', {age})")
    return engine


def time_query(engine: Engine, sql: str, repeats: int = 5) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        engine.execute_sql(sql)
        elapsed = time.perf_counter() - start
        best = min(best, elapsed)
    return best


def run_single_size_report(n_rows: int, lookup_age: int = 42):
    print(f"\n=== {n_rows:,} rows, WHERE age = {lookup_age} ===")
    engine = build_table(n_rows)

    seq_time = time_query(engine, f"SELECT id FROM users WHERE age = {lookup_age}")
    print(f"  SeqScan (no index):    {seq_time * 1000:8.3f} ms")

    engine.execute_sql("CREATE INDEX idx_age ON users (age)")
    idx_time = time_query(engine, f"SELECT id FROM users WHERE age = {lookup_age}")
    print(f"  IndexScan (B-tree):    {idx_time * 1000:8.3f} ms")

    speedup = seq_time / idx_time if idx_time > 0 else float("inf")
    print(f"  Speedup:               {speedup:8.1f}x")
    engine.close()
    return seq_time, idx_time


def sweep():
    sizes = [1_000, 10_000, 50_000, 100_000, 200_000]
    seq_times, idx_times = [], []
    for n in sizes:
        seq_t, idx_t = run_single_size_report(n)
        seq_times.append(seq_t * 1000)
        idx_times.append(idx_t * 1000)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot(sizes, seq_times, marker="o", label="SeqScan (no index)")
        ax.plot(sizes, idx_times, marker="o", label="IndexScan (B-tree)")
        ax.set_xlabel("Table size (rows)")
        ax.set_ylabel("Query time (ms, best of 5)")
        ax.set_title("Equality lookup: SeqScan vs B-tree IndexScan")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig("benchmark_result.png", dpi=150)
        print("\nChart saved to benchmark_result.png")
    except ImportError:
        print("\n(matplotlib not installed — skipping chart, numbers above still valid)")

    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)


if __name__ == "__main__":
    sweep()
