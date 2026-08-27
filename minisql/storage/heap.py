"""
heap.py — Page-based heap file storage (slotted pages + overflow pages).

This is the "table" itself on disk. The file is a sequence of fixed-size
pages (PAGE_SIZE bytes, default 4 KB), which is how every real engine lays
out a heap: the OS and the disk move data in blocks, a cache needs
fixed-size units to hold and evict, and a crash-recovery scheme needs a
unit whose "before" image it can save.

    page 0            file header: magic, format version, page size
    pages 1..N        data pages and overflow pages

Data page (Postgres-style "slotted page"):

    0   u8    page type (1 = data)
    1   u16   number of slots
    3   u16   free_upper: offset where the record area starts
    5.. slot array, 5 bytes each, growing *forward*:
              u16 offset, u16 length, u8 flags (bit0 = live, bit1 = overflow)
    ..  free space ..
    ..  record payloads, growing *backward* from the end of the page

A row's identity — its rowid — is (page number, slot number) packed into
one integer: rowid = page_no << 16 | slot_no. That is exactly Postgres's
ctid (block, offset) and the reason an index can find a row with one page
read. Slots are never reused (no VACUUM), so a rowid stays unique for the
life of the file — except across a ROLLBACK, which restores a page's
earlier image and thereby frees its newest slots; the engine invalidates
loaded indexes on rollback for that reason.

Deletes flip the slot's live bit (a tombstone) and leave the payload in
place; updates are a tombstone plus a fresh insert (the engine does that).

Overflow pages: a row whose pickled payload does not fit in an empty data
page is written to a chain of overflow pages (type 2: u8 type, u64 next
page, u16 data length, data) and the data page slot holds an 8+4 byte
pointer (first overflow page, total length) with the overflow flag set —
the same idea as Postgres TOAST.

Page cache: a small write-back cache (a "buffer pool") keeps recently used
pages in memory and marks modified ones dirty; dirty pages are written when
evicted, on flush(), or at the engine's commit point. sync() flushes and
fsyncs.

Crash safety is not this module's job: before any page is modified, the
`on_page_write(page_no, old_image_or_None)` hook is called so the engine
can journal the page's pre-image (or the fact that the page is new) —
SQLite's rollback-journal design. restore_page() / truncate_pages() are the
recovery primitives that put pages back.
"""

from __future__ import annotations
import os
import pickle
import struct
from collections import OrderedDict

PAGE_SIZE = 4096
MAGIC = b"MSQLHEAP"
FORMAT_VERSION = 1
SLOT_BITS = 16
SLOT_MASK = (1 << SLOT_BITS) - 1

PAGE_DATA = 1
PAGE_OVERFLOW = 2

FLAG_LIVE = 0x01
FLAG_OVERFLOW = 0x02

_FILE_HDR = struct.Struct(">8sHI")          # magic, version, page size
_DATA_HDR = struct.Struct(">BHH")           # type, num_slots, free_upper
_SLOT = struct.Struct(">HHB")               # offset, length, flags
_OVF_HDR = struct.Struct(">BQH")            # type, next page, data length
_OVF_PTR = struct.Struct(">QI")             # first overflow page, total length

DATA_HDR_SIZE = _DATA_HDR.size              # 5
SLOT_SIZE = _SLOT.size                      # 5
OVF_HDR_SIZE = _OVF_HDR.size                # 11


def make_rowid(page_no: int, slot_no: int) -> int:
    return (page_no << SLOT_BITS) | slot_no


def split_rowid(rowid: int) -> tuple[int, int]:
    return rowid >> SLOT_BITS, rowid & SLOT_MASK


class HeapFile:
    def __init__(self, path: str, on_page_write=None, cache_pages: int = 256,
                 page_size: int = PAGE_SIZE):
        self.path = path
        self.page_size = page_size
        self._on_page_write = on_page_write
        self._cache_cap = cache_pages
        self._cache: OrderedDict[int, bytearray] = OrderedDict()   # page_no -> image (LRU)
        self._dirty: set[int] = set()
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._f = open(path, "w+b" if new else "r+b")
        if new:
            self._write_raw(0, self._file_header())
            self._f.flush()
            self._num_pages = 1
        else:
            try:
                self._check_header()
            except ValueError:
                self._f.close()
                raise
            self._num_pages = max(1, os.path.getsize(path) // self.page_size)
        self._last_data_page = self._find_last_data_page()

    # ---------------- file header ----------------

    def _file_header(self) -> bytes:
        hdr = _FILE_HDR.pack(MAGIC, FORMAT_VERSION, self.page_size)
        return hdr + b"\x00" * (self.page_size - len(hdr))

    def _check_header(self) -> None:
        self._f.seek(0)
        raw = self._f.read(_FILE_HDR.size)
        if len(raw) < _FILE_HDR.size:
            raise ValueError(f"{self.path}: not a MiniSQL heap file")
        magic, version, page_size = _FILE_HDR.unpack(raw)
        if magic != MAGIC or version != FORMAT_VERSION:
            raise ValueError(f"{self.path}: unknown heap file format")
        self.page_size = page_size

    # ---------------- raw page I/O + cache ----------------

    def _write_raw(self, page_no: int, image: bytes) -> None:
        self._f.seek(page_no * self.page_size)
        self._f.write(image)

    def _read_raw(self, page_no: int) -> bytes | None:
        self._f.seek(page_no * self.page_size)
        image = self._f.read(self.page_size)
        return image if len(image) == self.page_size else None

    def read_page(self, page_no: int) -> bytearray | None:
        """The page's current image (cache first, then disk); None if the
        page doesn't exist or is truncated on disk."""
        if page_no in self._cache:
            self._cache.move_to_end(page_no)
            return self._cache[page_no]
        if page_no >= self._num_pages:
            return None
        raw = self._read_raw(page_no)
        if raw is None:
            return None
        image = bytearray(raw)
        self._cache_put(page_no, image)
        return image

    def _cache_put(self, page_no: int, image: bytearray) -> None:
        self._cache[page_no] = image
        self._cache.move_to_end(page_no)
        while len(self._cache) > self._cache_cap:
            victim, victim_img = self._cache.popitem(last=False)
            if victim in self._dirty:
                self._write_raw(victim, bytes(victim_img))
                self._dirty.discard(victim)

    def _modify(self, page_no: int, image: bytearray) -> None:
        """Mark a cached page dirty. The pre-image hook must already have run."""
        self._cache[page_no] = image
        self._cache.move_to_end(page_no)
        self._dirty.add(page_no)

    def _prepare_modify(self, page_no: int) -> bytearray:
        """Announce a modification of an existing page (journal hook), and
        return its image for editing."""
        image = self.read_page(page_no)
        if self._on_page_write is not None:
            self._on_page_write(page_no, bytes(image))
        return image

    def _allocate_page(self) -> int:
        """Append a brand-new page (journal hook with None = 'new page')."""
        page_no = self._num_pages
        if self._on_page_write is not None:
            self._on_page_write(page_no, None)
        self._num_pages += 1
        return page_no

    def flush(self) -> None:
        """Write every dirty page to the file (OS cache)."""
        for page_no in sorted(self._dirty):
            self._write_raw(page_no, bytes(self._cache[page_no]))
        self._dirty.clear()
        self._f.flush()

    def sync(self) -> None:
        """Force everything to stable storage: flush dirty pages, then fsync."""
        self.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        if not self._f.closed:
            self.flush()
            self._f.close()

    def page_count(self) -> int:
        return self._num_pages

    # ---------------- recovery primitives (bypass the journal hook) ----------------

    def restore_page(self, page_no: int, image: bytes) -> None:
        """Overwrite a page with a journaled pre-image."""
        self._cache.pop(page_no, None)
        self._dirty.discard(page_no)
        self._write_raw(page_no, image)
        self._f.flush()
        if page_no >= self._num_pages:
            self._num_pages = page_no + 1
        self._last_data_page = self._find_last_data_page()

    def truncate_pages(self, num_pages: int) -> None:
        """Drop every page >= num_pages (undo of page allocation)."""
        num_pages = max(1, num_pages)
        if num_pages >= self._num_pages:
            return
        for page_no in [p for p in self._cache if p >= num_pages]:
            self._cache.pop(page_no)
            self._dirty.discard(page_no)
        self._f.truncate(num_pages * self.page_size)
        self._f.flush()
        self._num_pages = num_pages
        self._last_data_page = self._find_last_data_page()

    # ---------------- page-format helpers ----------------

    @staticmethod
    def _data_header(image) -> tuple[int, int]:
        _t, num_slots, free_upper = _DATA_HDR.unpack_from(image, 0)
        return num_slots, free_upper

    @staticmethod
    def _slot(image, slot_no: int) -> tuple[int, int, int]:
        return _SLOT.unpack_from(image, DATA_HDR_SIZE + slot_no * SLOT_SIZE)

    def _empty_data_page(self) -> bytearray:
        image = bytearray(self.page_size)
        _DATA_HDR.pack_into(image, 0, PAGE_DATA, 0, self.page_size)
        return image

    def _free_space(self, image) -> int:
        num_slots, free_upper = self._data_header(image)
        return free_upper - (DATA_HDR_SIZE + num_slots * SLOT_SIZE)

    def _find_last_data_page(self) -> int | None:
        for page_no in range(self._num_pages - 1, 0, -1):
            image = self.read_page(page_no)
            if image is not None and image[0] == PAGE_DATA:
                return page_no
        return None

    # ---------------- overflow chain ----------------

    def _write_overflow(self, payload: bytes) -> int:
        """Store payload across new overflow pages; return the first page."""
        chunk = self.page_size - OVF_HDR_SIZE
        chunks = [payload[i:i + chunk] for i in range(0, len(payload), chunk)] or [b""]
        pages = [self._allocate_page() for _ in chunks]
        for i, (page_no, data) in enumerate(zip(pages, chunks)):
            image = bytearray(self.page_size)
            nxt = pages[i + 1] if i + 1 < len(pages) else 0
            _OVF_HDR.pack_into(image, 0, PAGE_OVERFLOW, nxt, len(data))
            image[OVF_HDR_SIZE:OVF_HDR_SIZE + len(data)] = data
            self._modify(page_no, image)
        return pages[0]

    def _read_overflow(self, first_page: int, total: int) -> bytes | None:
        out = bytearray()
        page_no = first_page
        while page_no:
            image = self.read_page(page_no)
            if image is None or image[0] != PAGE_OVERFLOW:
                return None
            _t, nxt, length = _OVF_HDR.unpack_from(image, 0)
            out += image[OVF_HDR_SIZE:OVF_HDR_SIZE + length]
            page_no = nxt
        return bytes(out) if len(out) == total else None

    # ---------------- public row API ----------------

    def insert(self, row: dict) -> int:
        """Store a row; return its rowid = (page, slot)."""
        payload = pickle.dumps(row)
        flags = FLAG_LIVE
        if len(payload) + SLOT_SIZE > self.page_size - DATA_HDR_SIZE:
            first = self._write_overflow(payload)
            payload = _OVF_PTR.pack(first, len(payload))
            flags |= FLAG_OVERFLOW
        need = len(payload) + SLOT_SIZE

        page_no = self._last_data_page
        if page_no is not None and self._free_space(self.read_page(page_no)) >= need:
            image = self._prepare_modify(page_no)
        else:
            page_no = self._allocate_page()
            image = self._empty_data_page()
            self._last_data_page = page_no

        num_slots, free_upper = self._data_header(image)
        offset = free_upper - len(payload)
        image[offset:free_upper] = payload
        _SLOT.pack_into(image, DATA_HDR_SIZE + num_slots * SLOT_SIZE, offset, len(payload), flags)
        _DATA_HDR.pack_into(image, 0, PAGE_DATA, num_slots + 1, offset)
        self._modify(page_no, image)
        return make_rowid(page_no, num_slots)

    def _locate(self, rowid: int):
        """(page image, slot_no, offset, length, flags) or None if no such slot."""
        page_no, slot_no = split_rowid(rowid)
        if page_no < 1:
            return None
        image = self.read_page(page_no)
        if image is None or image[0] != PAGE_DATA:
            return None
        num_slots, _ = self._data_header(image)
        if slot_no >= num_slots:
            return None
        offset, length, flags = self._slot(image, slot_no)
        return image, slot_no, offset, length, flags

    def _payload(self, image, offset: int, length: int, flags: int) -> bytes | None:
        data = bytes(image[offset:offset + length])
        if flags & FLAG_OVERFLOW:
            first, total = _OVF_PTR.unpack(data)
            return self._read_overflow(first, total)
        return data

    def read(self, rowid: int) -> dict | None:
        """Fetch a row by rowid. None if the slot is tombstoned or doesn't exist."""
        loc = self._locate(rowid)
        if loc is None:
            return None
        image, _slot_no, offset, length, flags = loc
        if not flags & FLAG_LIVE:
            return None
        payload = self._payload(image, offset, length, flags)
        return None if payload is None else pickle.loads(payload)

    def _set_live(self, rowid: int, live: bool) -> None:
        loc = self._locate(rowid)
        if loc is None:
            raise ValueError(f"No such rowid {rowid}")
        page_no, slot_no = split_rowid(rowid)
        image = self._prepare_modify(page_no)
        offset, length, flags = self._slot(image, slot_no)
        flags = (flags | FLAG_LIVE) if live else (flags & ~FLAG_LIVE)
        _SLOT.pack_into(image, DATA_HDR_SIZE + slot_no * SLOT_SIZE, offset, length, flags)
        self._modify(page_no, image)

    def delete(self, rowid: int) -> None:
        """Tombstone: clear the slot's live bit; the payload stays in place."""
        self._set_live(rowid, False)

    def undelete(self, rowid: int) -> None:
        self._set_live(rowid, True)

    def scan(self):
        """Yield (rowid, row) for every live row, in page/slot order (which
        is insertion order). Overflow pages are skipped; a truncated
        trailing page ends the scan."""
        for page_no in range(1, self._num_pages):
            image = self.read_page(page_no)
            if image is None:
                return
            if image[0] != PAGE_DATA:
                continue
            num_slots, _ = self._data_header(image)
            for slot_no in range(num_slots):
                offset, length, flags = self._slot(image, slot_no)
                if not flags & FLAG_LIVE:
                    continue
                payload = self._payload(image, offset, length, flags)
                if payload is not None:
                    yield make_rowid(page_no, slot_no), pickle.loads(payload)

    def __len__(self):
        return sum(1 for _ in self.scan())
