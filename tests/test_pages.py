"""
test_pages.py — Page-based heap storage: slotted-page layout, overflow
chains, the write-back page cache, and page-image rollback/recovery.
"""

import os
import shutil
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minisql.engine import Engine
from minisql.storage.heap import (
    HeapFile, PAGE_SIZE, PAGE_DATA, PAGE_OVERFLOW, MAGIC, make_rowid, split_rowid,
)

TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "_tmp_pages_test_data")


class HeapPageLayoutTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        os.makedirs(TEST_DATA_DIR)
        self.path = os.path.join(TEST_DATA_DIR, "t.tbl")
        self.heap = HeapFile(self.path)

    def tearDown(self):
        self.heap.close()
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def test_file_header_and_page_size(self):
        self.heap.close()
        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertEqual(len(raw), PAGE_SIZE)            # header page only
        self.assertTrue(raw.startswith(MAGIC))
        with self.assertRaises(ValueError):
            with open(self.path, "wb") as f:
                f.write(b"not a heap" + b"\x00" * 100)
            HeapFile(self.path)

    def test_rowid_is_page_and_slot(self):
        r0 = self.heap.insert({"v": 0})
        r1 = self.heap.insert({"v": 1})
        self.assertEqual(split_rowid(r0), (1, 0))       # first data page, first slot
        self.assertEqual(split_rowid(r1), (1, 1))
        self.assertEqual(make_rowid(1, 1), r1)
        self.assertEqual(self.heap.read(r1), {"v": 1})
        self.assertIsNone(self.heap.read(make_rowid(1, 99)))   # no such slot
        self.assertIsNone(self.heap.read(make_rowid(9, 0)))    # no such page

    def test_rows_spill_to_new_pages_in_order(self):
        rowids = [self.heap.insert({"i": i, "pad": "x" * 100}) for i in range(200)]
        pages = sorted({split_rowid(r)[0] for r in rowids})
        self.assertGreater(len(pages), 1)
        self.assertEqual(pages, list(range(1, len(pages) + 1)))
        self.assertEqual([row["i"] for _, row in self.heap.scan()], list(range(200)))
        self.assertEqual(len(self.heap), 200)

    def test_delete_is_a_flag_and_space_is_not_reused(self):
        r0 = self.heap.insert({"v": 0})
        self.heap.delete(r0)
        self.assertIsNone(self.heap.read(r0))
        r1 = self.heap.insert({"v": 1})
        self.assertEqual(split_rowid(r1), (1, 1))       # slot 0 is not reused
        self.heap.undelete(r0)
        self.assertEqual([r["v"] for _, r in self.heap.scan()], [0, 1])

    def test_large_row_uses_overflow_pages(self):
        big = {"blob": "y" * (3 * PAGE_SIZE)}          # bigger than any page
        small_before = self.heap.insert({"v": "before"})
        r = self.heap.insert(big)
        small_after = self.heap.insert({"v": "after"})
        self.assertEqual(self.heap.read(r), big)
        self.assertEqual(split_rowid(r)[0], 1)         # the pointer slot lives on the data page
        self.heap.flush()
        with open(self.path, "rb") as f:
            types = [f.read(PAGE_SIZE)[0] for _ in range(self.heap.page_count())]
        self.assertEqual(types[1], PAGE_DATA)
        self.assertEqual(types.count(PAGE_OVERFLOW), 4)   # 3*4096 bytes of payload + pickle overhead
        # scan follows the chain, skips overflow pages, keeps insertion order
        self.assertEqual([r for _, r in self.heap.scan()],
                         [{"v": "before"}, big, {"v": "after"}])
        self.heap.delete(r)
        self.assertEqual(len(self.heap), 2)

    def test_dirty_pages_survive_close_and_reopen(self):
        rowids = [self.heap.insert({"i": i}) for i in range(50)]
        self.heap.delete(rowids[7])
        self.heap.close()
        heap = HeapFile(self.path)
        try:
            self.assertEqual([r["i"] for _, r in heap.scan()], [i for i in range(50) if i != 7])
            r = heap.insert({"i": 50})
            self.assertEqual(split_rowid(r), (1, 50))  # appends to the existing last page
        finally:
            heap.close()

    def test_cache_eviction_writes_dirty_pages(self):
        path = os.path.join(TEST_DATA_DIR, "small_cache.tbl")
        heap = HeapFile(path, cache_pages=2)
        writes = []
        real_write = heap._write_raw
        heap._write_raw = lambda n, img: (writes.append(n), real_write(n, img))
        try:
            rowids = [heap.insert({"i": i, "pad": "x" * 1000}) for i in range(40)]  # ~14 pages, 2-page cache
            self.assertGreater(heap.page_count(), 10)
            self.assertLessEqual(len(heap._cache), 2)             # the cap is enforced on the write path
            self.assertGreater(len(writes), 8)                    # evicted dirty pages were written back
            with open(path, "rb") as f:                            # …and are really on disk, before close()
                f.seek(PAGE_SIZE)
                self.assertEqual(f.read(1)[0], PAGE_DATA)
            self.assertEqual([heap.read(r)["i"] for r in rowids], list(range(40)))   # reads fault pages back in
            self.assertEqual(len(heap), 40)
            self.assertLessEqual(len(heap._cache), 2)             # …and the reads evicted too
        finally:
            heap.close()

    def test_page_size_must_be_an_int(self):
        with self.assertRaises(ValueError):
            HeapFile(os.path.join(TEST_DATA_DIR, "float.tbl"), page_size=1024.0)
        self.assertFalse(os.path.exists(os.path.join(TEST_DATA_DIR, "float.tbl")))

    def test_old_format_file_with_full_header_length_is_also_rejected(self):
        path = os.path.join(TEST_DATA_DIR, "old_long.tbl")
        with open(path, "wb") as f:                                 # a real pre-pages record: L + u32 len + pickle
            f.write(b"L" + (30).to_bytes(4, "big") + b"\x80\x04\x95" + b"x" * 27)
        with self.assertRaisesRegex(ValueError, "older MiniSQL"):
            HeapFile(path)

    def test_restore_of_torn_last_data_page_keeps_last_page_pointer(self):
        # The last data page's type byte is destroyed on disk (a zero-filled
        # block after a crash). Startup skips it; recovery restores it; the
        # next insert must go there, not to a fresh page.
        rowids = [self.heap.insert({"i": i, "pad": "x" * 1000}) for i in range(4)]   # page 1 full-ish, page 2 has 1 row
        self.assertEqual(split_rowid(rowids[-1])[0], 2)
        self.heap.flush()
        image2 = bytes(self.heap.read_page(2))
        self.heap.close()
        with open(self.path, "r+b") as f:
            f.seek(2 * PAGE_SIZE)
            f.write(b"\x00" * PAGE_SIZE)
        self.heap = HeapFile(self.path)
        self.assertEqual(self.heap._last_data_page, 1)              # torn page skipped at open
        self.heap.restore_page(2, image2)
        self.assertEqual(self.heap._last_data_page, 2)              # pointer follows the restored image
        r = self.heap.insert({"i": 4, "pad": "x" * 1000})
        self.assertEqual(split_rowid(r), (2, 1))
        self.assertEqual([row["i"] for _, row in self.heap.scan()], [0, 1, 2, 3, 4])

    def test_page_size_must_fit_u16_fields(self):
        for bad in (65536, 100):
            with self.assertRaises(ValueError):
                HeapFile(os.path.join(TEST_DATA_DIR, f"bad{bad}.tbl"), page_size=bad)
        ok = HeapFile(os.path.join(TEST_DATA_DIR, "ok.tbl"), page_size=1024)
        try:
            r = ok.insert({"v": 1})
            self.assertEqual(ok.read(r), {"v": 1})
        finally:
            ok.close()

    def test_old_format_file_gives_a_clear_error(self):
        path = os.path.join(TEST_DATA_DIR, "old.tbl")
        with open(path, "wb") as f:
            f.write(b"L" + (7).to_bytes(4, "big") + b"oldrow!")   # pre-pages byte-append format
        with self.assertRaisesRegex(ValueError, "older MiniSQL"):
            HeapFile(path)

    def test_restore_page_does_not_rescan_the_file(self):
        rowids = [self.heap.insert({"i": i, "pad": "x" * 100}) for i in range(100)]
        self.heap.flush()
        image = bytes(self.heap.read_page(1))
        calls = []
        real = self.heap._find_last_data_page
        self.heap._find_last_data_page = lambda: (calls.append(1), real())[1]
        self.heap.restore_page(1, image)                          # existing page: no rescan
        self.assertEqual(calls, [])
        new_page = self.heap.page_count()
        self.heap.restore_page(new_page, image)                   # extending the file: still no rescan —
        self.assertEqual(calls, [])                               # the restored image says it is a data page,
        self.assertEqual(self.heap._last_data_page, new_page)     # so the pointer just follows it

    def test_page_write_hook_sees_pre_image_or_none(self):
        events = []
        heap = HeapFile(os.path.join(TEST_DATA_DIR, "hook.tbl"),
                        on_page_write=lambda p, old: events.append((p, old is None)))
        try:
            heap.insert({"v": 1})                       # new page 1
            heap.insert({"v": 2})                       # modifies page 1
            self.assertEqual(events, [(1, True), (1, False)])
            self.assertEqual(len(events), 2)
        finally:
            heap.close()


class PageJournalRecoveryTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.engine.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, pad TEXT)")
        self.engine.execute_sql("BEGIN")
        for i in range(120):                            # enough rows to span several pages
            self.engine.execute_sql(f"INSERT INTO t VALUES ({i}, '{'x' * 100}')")
        self.engine.execute_sql("COMMIT")

    def tearDown(self):
        if self.engine is not None:
            self.engine.close()
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def ids(self, engine=None):
        return [r["id"] for r in (engine or self.engine).execute_sql("SELECT id FROM t ORDER BY id")]

    def pages(self):
        return self.engine.get_heap("t").page_count()

    def test_rollback_truncates_new_pages_and_restores_old_ones(self):
        before_pages = self.pages()
        self.engine.execute_sql("BEGIN")
        for i in range(120, 300):
            self.engine.execute_sql(f"INSERT INTO t VALUES ({i}, '{'x' * 100}')")
        self.engine.execute_sql("DELETE FROM t WHERE id = 0")     # modifies page 1
        self.assertGreater(self.pages(), before_pages)
        self.engine.execute_sql("ROLLBACK")
        self.assertEqual(self.pages(), before_pages)            # new pages gone
        self.assertEqual(self.ids(), list(range(120)))          # row 0 back, 120.. gone
        self.assertEqual(self.engine._journal.entries(), [])

    def test_slot_reuse_after_rollback_does_not_confuse_indexes(self):
        self.engine.execute_sql("CREATE INDEX idx_pad ON t (pad)")
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO t VALUES (500, 'rolled-back')")
        self.engine.execute_sql("ROLLBACK")
        # The slot 500 occupied is free again; the next insert reuses it.
        self.engine.execute_sql("INSERT INTO t VALUES (501, 'fresh')")
        self.assertEqual(self.engine.execute_sql("SELECT id FROM t WHERE pad = 'rolled-back'"), [])
        self.assertEqual(self.engine.execute_sql("SELECT id FROM t WHERE pad = 'fresh'"), [{"id": 501}])
        self.assertEqual(self.engine.execute_sql("SELECT id FROM t WHERE id = 500"), [])

    def test_failed_statement_inside_transaction_restores_only_its_pages(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("INSERT INTO t VALUES (900, 'kept')")
        images_at_rollback = []
        real = Engine._rollback_statement
        with unittest.mock.patch.object(Engine, "_rollback_statement",
                                        lambda eng: (images_at_rollback.append(dict(eng._stmt_images)), real(eng))):
            with self.assertRaises(ValueError):
                # 118 -> 5000 succeeds (pages modified), then 119 -> 5000 is a duplicate
                self.engine.execute_sql("UPDATE t SET id = 5000 WHERE id > 117")
        self.assertTrue(images_at_rollback and images_at_rollback[0], "statement rollback had pages to restore")
        self.assertNotIn(("t", "id"), self.engine._indexes)      # index dropped, will rebuild lazily
        self.assertEqual(self.engine.execute_sql("SELECT id FROM t WHERE id = 5000"), [])
        self.assertEqual(self.engine.execute_sql("SELECT id FROM t WHERE id = 118"), [{"id": 118}])
        self.assertEqual(self.ids(), list(range(120)) + [900])   # Dan-equivalent (900) survived
        self.engine.execute_sql("COMMIT")
        self.engine.close()
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), list(range(120)) + [900])

    def test_failed_statement_truncates_pages_it_allocated(self):
        before_pages, before_ids = self.pages(), self.ids()
        self.engine.execute_sql("BEGIN")
        with self.assertRaises(ValueError):
            # each new version is 3 KB, so the first row forces a new data page
            # (and the 9 KB one an overflow chain) before the duplicate key fails
            self.engine.execute_sql(f"UPDATE t SET id = 5000, pad = '{'y' * 3000}' WHERE id > 117")
        self.assertEqual(self.pages(), before_pages)                # allocated pages truncated away
        self.assertEqual(self.ids(), before_ids)
        with self.assertRaises(ValueError):
            self.engine.execute_sql(f"UPDATE t SET id = 5000, pad = '{'y' * 9000}' WHERE id > 117")
        self.assertEqual(self.pages(), before_pages)
        self.engine.execute_sql("COMMIT")
        self.engine.close()
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), before_ids)

    def test_pre_pages_directory_fails_at_open_without_touching_its_journal(self):
        self.engine.close()
        # Fake a directory written by the byte-append engine: catalog present,
        # an old-format table file, and a non-empty old text journal.
        with open(os.path.join(TEST_DATA_DIR, "t.tbl"), "wb") as f:
            f.write(b"L" + (30).to_bytes(4, "big") + b"\x80\x04\x95" + b"x" * 27)
        jpath = os.path.join(TEST_DATA_DIR, "undo.journal")
        with open(jpath, "wb") as f:
            f.write(b"insert t 47\n")
        with self.assertRaisesRegex(ValueError, "older MiniSQL"):
            Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(open(jpath, "rb").read(), b"insert t 47\n")   # untouched
        self.engine = None

    def test_crash_mid_transaction_recovers_from_pre_images(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM t WHERE id = 5")             # page 1
        self.engine.execute_sql("UPDATE t SET pad = 'z' WHERE id = 119")   # last page + append
        for i in range(120, 200):
            self.engine.execute_sql(f"INSERT INTO t VALUES ({i}, 'new')")
        self.engine.close()                                                # no COMMIT
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), list(range(120)))
        self.assertEqual(self.engine.execute_sql("SELECT pad FROM t WHERE id = 119"), [{"pad": "x" * 100}])
        self.assertEqual(self.engine._journal.entries(), [])

    def test_torn_middle_page_is_repaired_from_its_pre_image(self):
        # A crash while rewriting a page in the middle of the file corrupts
        # rows committed long ago. Only a page pre-image can repair that.
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM t WHERE id = 3")   # page 1 gets journaled + rewritten
        self.engine.close()
        path = os.path.join(TEST_DATA_DIR, "t.tbl")
        with open(path, "r+b") as f:                            # scribble over page 1
            f.seek(PAGE_SIZE)
            f.write(os.urandom(PAGE_SIZE))
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), list(range(120)))

    def test_torn_first_journal_entry_does_not_hide_later_transactions(self):
        # A crash during the very first append() of a transaction leaves a
        # torn head entry. It must be cleared on the next start; otherwise the
        # next transaction's entries land behind it and recovery never sees them.
        self.engine.close()
        jpath = os.path.join(TEST_DATA_DIR, "undo.journal")
        self.assertEqual(os.path.getsize(jpath), 0)
        with open(jpath, "ab") as f:
            f.write(b"\x01\x00\x01t\x00\x00\x00")             # torn head, nothing else
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(os.path.getsize(jpath), 0)                 # cleared at startup
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM t WHERE id = 3")
        self.engine.close()                                         # crash before COMMIT
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), list(range(120)))              # recovered — row 3 is back

    def _fail_next_journal_write_after(self, nbytes):
        """Make the next os.write on the journal land only `nbytes` bytes and
        then raise ENOSPC, as a full disk would."""
        import minisql.storage.journal as jmod
        real_write = os.write
        state = {"armed": True}

        def flaky(fd, data):
            if state["armed"] and fd == self.engine._journal._fd:
                state["armed"] = False
                real_write(fd, bytes(data[:nbytes]))
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        patcher = unittest.mock.patch.object(jmod.os, "write", flaky)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_journal_append_repairs_journal_and_aborts_transaction(self):
        before = self.ids()
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM t WHERE id = 3")           # journals page 1, modifies it
        jsize = self.engine._journal.size()
        self._fail_next_journal_write_after(20)                      # next entry is torn mid-write
        with self.assertRaises(OSError):
            self.engine.execute_sql("INSERT INTO t VALUES (900, 'x')")   # would touch another page
        # journal was cut back to the last complete entry, then the whole
        # transaction was rolled back and closed
        self.assertFalse(self.engine._in_transaction)
        self.assertEqual(self.engine._journal.size(), 0)
        self.assertEqual(self.ids(), before)                         # row 3 is back
        with self.assertRaises(ValueError):
            self.engine.execute_sql("COMMIT")                        # no transaction in progress
        # and a later transaction is still recoverable after a crash
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM t WHERE id = 5")
        self.engine.close()
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), before)
        self.assertGreater(jsize, 0)

    def test_torn_journal_entry_is_ignored(self):
        self.engine.execute_sql("BEGIN")
        self.engine.execute_sql("DELETE FROM t WHERE id = 3")
        self.engine.close()
        jpath = os.path.join(TEST_DATA_DIR, "undo.journal")
        with open(jpath, "ab") as f:
            f.write(b"\x01\x00\x01t\x00\x00\x00\x00\x00\x00\x00\x07\x00\x00\x10\x00garbage")  # half an entry
        self.engine = Engine(data_dir=TEST_DATA_DIR)
        self.assertEqual(self.ids(), list(range(120)))          # the good entry still recovered


if __name__ == "__main__":
    unittest.main(verbosity=2)
