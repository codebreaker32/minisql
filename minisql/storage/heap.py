"""
heap.py — Heap file storage.

This is the "table" itself on disk: an append-only binary file where every
row is stored as [4-byte length][pickled row dict]. The file offset at which
a row's length-prefix begins is used as that row's rowid — this is exactly
how SQLite's rowid and Postgres's ctid work conceptually (a physical location
that identifies a row so an index can point at it without duplicating the
row's data).

Deletes are represented as tombstones (a leading marker byte) rather than
physically removing bytes, since shifting file contents on every delete
would be O(n). A real system would periodically compact/vacuum; MiniSQL
does not implement vacuum (documented as a limitation).
"""

from __future__ import annotations
import os
import pickle
import struct

LEN_STRUCT = struct.Struct(">I")   # 4-byte unsigned length prefix
LIVE = b"L"
DEAD = b"D"


class HeapFile:
    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            open(path, "wb").close()

    def insert(self, row: dict) -> int:
        """Append a row, return its rowid (byte offset in the file)."""
        payload = pickle.dumps(row)
        with open(self.path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            offset = f.tell()
            f.write(LIVE)
            f.write(LEN_STRUCT.pack(len(payload)))
            f.write(payload)
        return offset

    def read(self, rowid: int) -> dict | None:
        """Fetch a single row by rowid. Returns None if tombstoned."""
        with open(self.path, "rb") as f:
            f.seek(rowid)
            tomb = f.read(1)
            if tomb != LIVE:
                return None
            (length,) = LEN_STRUCT.unpack(f.read(4))
            payload = f.read(length)
        return pickle.loads(payload)

    def delete(self, rowid: int) -> None:
        with open(self.path, "r+b") as f:
            f.seek(rowid)
            f.write(DEAD)

    def undelete(self, rowid: int) -> None:
        """Flip a tombstoned row back to live. Used only by transaction
        ROLLBACK to undo a DELETE — not part of normal SQL semantics."""
        with open(self.path, "r+b") as f:
            f.seek(rowid)
            f.write(LIVE)

    def scan(self):
        """Yield (rowid, row) for every live row, in insertion order."""
        with open(self.path, "rb") as f:
            while True:
                start = f.tell()
                tomb = f.read(1)
                if not tomb:
                    break
                (length,) = LEN_STRUCT.unpack(f.read(4))
                payload = f.read(length)
                if tomb == LIVE:
                    yield start, pickle.loads(payload)

    def __len__(self):
        return sum(1 for _ in self.scan())
