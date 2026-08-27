"""
journal.py — Page-image rollback journal.

Before the engine lets a heap page be modified for the first time in a
transaction, the page's original bytes ("pre-image") are appended here; a
page that is brand new is recorded as such. To roll back — whether for an
explicit ROLLBACK, or during startup recovery after a crash — the engine
copies every pre-image back over its page and truncates the file below
the lowest new page. This is exactly SQLite's rollback-journal design.

Why page images rather than "undo this rowid": inserting into a half-full
page rewrites the whole page, so a crash mid-write can corrupt rows that
were committed long ago. Only a copy of the page's earlier bytes can repair
that. (Postgres solves the same problem with full-page writes in its WAL.)

Entry format (binary, big-endian):

    u8  kind          1 = PRE_IMAGE, 2 = NEW_PAGE
    u16 table length, table name bytes
    u64 page number
    u32 image length, image bytes     (0 bytes for NEW_PAGE)
    u32 CRC-32 of everything above

The CRC lets recovery ignore a torn final entry: because the engine never
touches a page until its journal entry is fully written (and fsync'd in
durable mode), a torn entry can only describe a modification that never
started.

Write-ahead ordering, the whole point: the entry reaches the journal — and
the disk, in durable mode — *before* the page changes. Clearing the journal
is the commit point, and the engine fsyncs the heap files first.
"""

from __future__ import annotations
import os
import struct
import zlib

PRE_IMAGE = 1
NEW_PAGE = 2

_HDR = struct.Struct(">BH")      # kind, table length
_PAGE = struct.Struct(">QI")     # page number, image length
_CRC = struct.Struct(">I")


class UndoJournal:
    def __init__(self, path: str, sync: bool = True):
        self.path = path
        self.sync = sync
        self._f = open(path, "a+b")

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    @staticmethod
    def _encode(kind: int, table: str, page_no: int, image: bytes | None) -> bytes:
        name = table.encode()
        image = image or b""
        body = _HDR.pack(kind, len(name)) + name + _PAGE.pack(page_no, len(image)) + image
        return body + _CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)

    def append(self, kind: int, table: str, page_no: int, image: bytes | None) -> None:
        self._f.seek(0, os.SEEK_END)
        self._f.write(self._encode(kind, table, page_no, image))
        self._f.flush()
        if self.sync:
            os.fsync(self._f.fileno())

    def entries(self) -> list[tuple[int, str, int, bytes | None]]:
        """Parse the journal; stops at the first short or corrupt entry."""
        self._f.seek(0)
        data = self._f.read()
        out = []
        pos = 0
        while pos < len(data):
            try:
                kind, name_len = _HDR.unpack_from(data, pos)
                p = pos + _HDR.size
                name = data[p:p + name_len].decode()
                p += name_len
                page_no, img_len = _PAGE.unpack_from(data, p)
                p += _PAGE.size
                image = data[p:p + img_len]
                p += img_len
                (crc,) = _CRC.unpack_from(data, p)
                p += _CRC.size
            except (struct.error, UnicodeDecodeError):
                break
            if len(image) != img_len or zlib.crc32(data[pos:p - _CRC.size]) & 0xFFFFFFFF != crc:
                break
            out.append((kind, name, page_no, bytes(image) if kind == PRE_IMAGE else None))
            pos = p
        return out

    def rewrite(self, entries) -> None:
        """Replace the journal contents wholesale."""
        self._f.seek(0)
        self._f.truncate(0)
        for kind, table, page_no, image in entries:
            self._f.write(self._encode(kind, table, page_no, image))
        self._f.flush()
        if self.sync:
            os.fsync(self._f.fileno())

    def clear(self) -> None:
        """The commit point."""
        self.rewrite([])
