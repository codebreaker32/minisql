"""
journal.py — On-disk undo journal (the "rollback journal").

Every heap mutation the engine performs is preceded by one line in this
file describing how to undo it:

    insert <table> <rowid>     -> undo by tombstoning (live) / truncating (recovery)
    delete <table> <rowid>     -> undo by flipping the marker back to live

This is write-ahead in the classical sense: the undo record reaches the
journal (and, in durable mode, the disk via fsync) *before* the heap file is
touched. That ordering is the whole trick — if the process dies at any
point, the journal on disk describes exactly the set of heap changes that
may have happened but were never committed, and startup recovery undoes
them in reverse order.

The journal is cleared at the commit point: after COMMIT, or after every
autocommit statement. Clearing it is what makes a change durable, so the
engine fsyncs the heap files *before* clearing (otherwise a crash between
the two could lose a committed write with no record left to recover from).

SQLite's rollback-journal mode works the same way, one level of abstraction
down (it journals whole pages rather than rows).
"""

from __future__ import annotations
import os


class UndoJournal:
    def __init__(self, path: str, sync: bool = True):
        self.path = path
        self.sync = sync
        self._f = open(path, "a+b")

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def append(self, action: str, table: str, rowid: int) -> None:
        self._f.seek(0, os.SEEK_END)
        self._f.write(f"{action} {table} {rowid}\n".encode())
        self._f.flush()
        if self.sync:
            os.fsync(self._f.fileno())

    def entries(self) -> list[tuple[str, str, int]]:
        self._f.seek(0)
        out = []
        for line in self._f.read().decode().splitlines():
            parts = line.split()
            if len(parts) == 3:       # ignore a torn trailing line
                out.append((parts[0], parts[1], int(parts[2])))
        return out

    def rewrite(self, entries: list[tuple[str, str, int]]) -> None:
        """Replace the journal contents (used to drop the entries of a single
        rolled-back statement while an enclosing transaction stays open)."""
        self._f.seek(0)
        self._f.truncate(0)
        for action, table, rowid in entries:
            self._f.write(f"{action} {table} {rowid}\n".encode())
        self._f.flush()
        if self.sync:
            os.fsync(self._f.fileno())

    def clear(self) -> None:
        """The commit point."""
        self.rewrite([])
