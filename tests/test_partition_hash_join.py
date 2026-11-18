import os
import tempfile
import unittest

from buffer_heap import BufferPool, FileManager, HeapFile, registro
from partition_hash_join import (PartitionHashJoin, RelationInput,
                                 partition_hash_join)


class PartitionHashJoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pool = None

    def tearDown(self):
        if self.pool is not None and self.pool.fm._fh:
            self.assertEqual(self.pool.pinned_count(), 0)
            self.pool.close()

    def make_pool(self, capacity=4, policy='LRU'):
        self.pool = BufferPool(FileManager(self.tmp.name), capacity, policy)
        return self.pool

    @staticmethod
    def load(heap, rows):
        for values in rows:
            heap.insert(registro.encode(*values))

    def temp_files(self):
        return [name for name in os.listdir(self.tmp.name) if name.startswith('phj_')]

    def test_inner_join_preserves_duplicates_and_left_right_order(self):
        pool = self.make_pool()
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        self.load(left, [(1, 10, 0), (1, 11, 0), (2, 12, 0)])
        self.load(right, [(20, 1, 0), (21, 1, 0), (22, 1, 0), (23, 3, 0)])
        iterator, operator = partition_hash_join(
            left, right, 0, 1, buffer=pool, memory_pages=4)
        pairs = [(registro.decode(a), registro.decode(b)) for a, b in iterator]
        self.assertEqual(len(pairs), 6)
        self.assertTrue(all(a[0] == b[1] for a, b in pairs))
        self.assertEqual(operator.stats.output_pairs, 6)
        self.assertEqual(self.temp_files(), [])
        self.assertFalse(any(name.startswith('phj_') for name in pool.fm.kind))

    def test_build_side_can_be_right_without_reversing_output(self):
        pool = self.make_pool()
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        self.load(left, [(i % 3, i, 0) for i in range(40)])
        self.load(right, [(2, 999, 0)])
        iterator, _ = partition_hash_join(
            left, right, 0, 0, buffer=pool, memory_pages=4)
        pairs = [(registro.decode(a), registro.decode(b)) for a, b in iterator]
        self.assertEqual(len(pairs), 13)
        self.assertTrue(all(a[0] == 2 and b[1] == 999 for a, b in pairs))

    def test_relations_larger_than_three_page_buffer_repartition(self):
        pool = self.make_pool(3, 'FIFO')
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        lrows = [(i % 17, i, 0) for i in range(124)]
        rrows = [(i % 17, 1000 + i, 0) for i in range(155)]
        self.load(left, lrows)
        self.load(right, rrows)
        pool.drop_all()
        pool.reset_stats()
        iterator, operator = partition_hash_join(
            left, right, 0, 0, buffer=pool, memory_pages=3, max_depth=4)
        got = sorted((registro.decode(a)[1], registro.decode(b)[1])
                     for a, b in iterator)
        expected = sorted((a[1], b[1]) for a in lrows for b in rrows
                          if a[0] == b[0])
        self.assertEqual(got, expected)
        self.assertGreater(operator.stats.repartition_passes, 0)
        self.assertGreater(operator.stats.temp_reads, 0)
        self.assertGreater(operator.stats.temp_writes, 0)
        self.assertLessEqual(len(pool.frames), 3)
        self.assertEqual(self.temp_files(), [])

    def test_extreme_skew_uses_bounded_chunk_fallback(self):
        pool = self.make_pool(3)
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        self.load(left, [(7, i, 0) for i in range(70)])
        self.load(right, [(7, 1000 + i, 0) for i in range(65)])
        iterator, operator = partition_hash_join(
            left, right, 0, 0, buffer=pool, memory_pages=3, max_depth=3)
        count = sum(1 for _ in iterator)
        self.assertEqual(count, 70 * 65)
        self.assertEqual(operator.stats.fallback_partitions, 1)
        self.assertEqual(operator.stats.build_records, 65)
        self.assertEqual(operator.stats.probe_records, 70 * 3)
        self.assertEqual(self.temp_files(), [])

    def test_none_key_has_sql_null_semantics(self):
        pool = self.make_pool()
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        self.load(left, [(1, 10, 0), (-1, 11, 0)])
        self.load(right, [(1, 20, 0), (-1, 21, 0)])
        nullable = lambda raw: None if registro.field(raw, 0) == -1 else registro.field(raw, 0)
        op = PartitionHashJoin(pool, 4)
        pairs = list(op.join(
            RelationInput.from_source(left, nullable),
            RelationInput.from_source(right, nullable)))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(op.stats.null_records, 2)

    def test_empty_relation_and_early_close_clean_resources(self):
        pool = self.make_pool()
        empty, populated = HeapFile(pool, 'empty'), HeapFile(pool, 'populated')
        self.load(populated, [(1, i, 0) for i in range(50)])
        iterator, _ = partition_hash_join(
            empty, populated, 0, 0, buffer=pool, memory_pages=4)
        self.assertEqual(list(iterator), [])
        self.assertEqual(self.temp_files(), [])

        left = HeapFile(pool, 'left')
        self.load(left, [(1, i, 0) for i in range(50)])
        iterator, _ = partition_hash_join(
            left, populated, 0, 0, buffer=pool, memory_pages=4)
        next(iterator)
        iterator.close()
        self.assertEqual(pool.pinned_count(), 0)
        self.assertEqual(self.temp_files(), [])

    def test_inputs_are_unchanged_and_invalid_budget_is_rejected(self):
        pool = self.make_pool(4)
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        rows = [(1, 2, 3), (4, 5, 6)]
        self.load(left, rows)
        self.load(right, rows)
        before = list(left.scan())
        iterator, _ = partition_hash_join(
            left, right, 0, 0, buffer=pool, memory_pages=4)
        list(iterator)
        self.assertEqual(list(left.scan()), before)
        with self.assertRaises(ValueError):
            PartitionHashJoin(pool, 2)
        with self.assertRaises(ValueError):
            PartitionHashJoin(pool, 5)

    def test_key_exception_and_reentrant_iterator_clean_up(self):
        pool = self.make_pool(4)
        left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
        self.load(left, [(1, 0, 0), (2, 0, 0)])
        self.load(right, [(1, 0, 0)])

        def failing_key(raw):
            value = registro.field(raw, 0)
            if value == 2:
                raise RuntimeError('falha do extrator')
            return value

        op = PartitionHashJoin(pool, 4)
        with self.assertRaises(RuntimeError):
            list(op.join(RelationInput.from_source(left, failing_key),
                         RelationInput.from_source(right, 0)))
        self.assertEqual(pool.pinned_count(), 0)
        self.assertEqual(self.temp_files(), [])

        first = op.join(RelationInput.from_source(left, 0),
                        RelationInput.from_source(right, 0))
        next(first)
        second = op.join(RelationInput.from_source(left, 0),
                         RelationInput.from_source(right, 0))
        with self.assertRaises(RuntimeError):
            next(second)
        first.close()
        self.assertEqual(self.temp_files(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
