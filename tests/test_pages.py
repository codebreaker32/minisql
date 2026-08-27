"""
test_pages.py — Page-based heap storage: slotted-page layout, overflow
chains, the write-back page cache, and page-image rollback/recovery.
"""

import os
import shutil
import sys
import unittest

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
        heap = HeapFile(os.path.join(TEST_DATA_DIR, "small_cache.tbl"), cache_pages=2)
        try:
            rowids = [heap.insert({"i": i, "pad": "x" * 1000}) for i in range(40)]  # many pages, 2-page cache
            self.assertEqual([heap.read(r)["i"] for r in rowids], list(range(40)))
            self.assertEqual(len(heap), 40)
        finally:
            heap.close()

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
        with self.assertRaises(ValueError):
            self.engine.execute_sql("UPDATE t SET id = 1 WHERE id > 100")   # 2nd row duplicates
        self.assertEqual(self.ids(), list(range(120)) + [900])
        self.engine.execute_sql("COMMIT")
        self.assertEqual(self.ids(), list(range(120)) + [900])

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
