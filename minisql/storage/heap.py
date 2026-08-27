"""
heap.py — Heap file storage.

This is the "table" itself on disk: an append-only binary file where every
row is stored as [1-byte live/dead marker][4-byte length][pickled row dict].
The file offset at which a row's marker byte begins is used as that row's
rowid — this is exactly how SQLite's rowid and Postgres's ctid work
conceptually (a physical location that identifies a row so an index can
point at it without duplicating the row's data). Because the file is
append-only, an offset never moves, so rowids are stable.

Deletes are represented as tombstones (flipping the marker byte) rather than
physically removing bytes, since shifting file contents on every delete
would be O(n) and would invalidate every rowid after the hole. A real
system would periodically compact/vacuum; MiniSQL does not implement
vacuum (documented as a limitation).

One read/write handle is kept open per heap for the lifetime of the
HeapFile (instead of re-opening on every point read); `sync()` fsyncs it,
which the engine calls at commit time when running in durable mode. Scans
open their own short-lived read handle so they stream sequentially.
"""

from __future__ import annotations
import os
import pickle
import struct

LEN_STRUCT = struct.Struct(">I")   # 4-byte unsigned length prefix
HEADER_SIZE = 1 + LEN_STRUCT.size
LIVE = b"L"
DEAD = b"D"


class HeapFile:
    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            open(path, "wb").close()
        self._f = open(path, "r+b")

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def next_rowid(self) -> int:
        """The rowid the next insert will get (the current end of file)."""
        self._f.seek(0, os.SEEK_END)
        return self._f.tell()

    def insert(self, row: dict) -> int:
        """Append a row, return its rowid (byte offset in the file)."""
        payload = pickle.dumps(row)
        f = self._f
        f.seek(0, os.SEEK_END)
        offset = f.tell()
        f.write(LIVE + LEN_STRUCT.pack(len(payload)) + payload)
        f.flush()
        return offset

    def read(self, rowid: int) -> dict | None:
        """Fetch a single row by rowid. Returns None if tombstoned (or if the
        rowid points past the end of the file)."""
        f = self._f
        f.seek(rowid)
        header = f.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE or header[:1] != LIVE:
            return None
        (length,) = LEN_STRUCT.unpack(header[1:])
        payload = f.read(length)
        if len(payload) < length:
            return None
        return pickle.loads(payload)

    def delete(self, rowid: int) -> None:
        self._f.seek(rowid)
        self._f.write(DEAD)
        self._f.flush()

    def undelete(self, rowid: int) -> None:
        """Flip a tombstoned row back to live. Used only by transaction
        ROLLBACK to undo a DELETE — not part of normal SQL semantics."""
        self._f.seek(rowid)
        self._f.write(LIVE)
        self._f.flush()

    def truncate(self, offset: int) -> None:
        """Cut the file back to `offset`. Used only by crash recovery to
        discard the trailing records of an interrupted statement (including
        a partially written, "torn" record)."""
        self._f.truncate(offset)
        self._f.flush()

    def sync(self) -> None:
        """Force written bytes to stable storage (fsync)."""
        self._f.flush()
        os.fsync(self._f.fileno())

    def scan(self):
        """Yield (rowid, row) for every live row, in insertion order.
        Uses its own read-only handle so it streams through the OS buffer
        sequentially and is independent of the write handle's position
        (an index lookup can happen while a scan generator is suspended).
        Stops cleanly at a truncated trailing record."""
        with open(self.path, "rb") as f:
            pos = 0
            while True:
                header = f.read(HEADER_SIZE)
                if len(header) < HEADER_SIZE:
                    break
                (length,) = LEN_STRUCT.unpack(header[1:])
                payload = f.read(length)
                if len(payload) < length:
                    break
                if header[:1] == LIVE:
                    yield pos, pickle.loads(payload)
                pos += HEADER_SIZE + length

    def __len__(self):
        return sum(1 for _ in self.scan())
