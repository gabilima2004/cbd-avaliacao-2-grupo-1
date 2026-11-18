"""Validação funcional, externa e por propriedades do Partition Hash Join."""

from collections import Counter
import os
import random
import sqlite3
import tempfile
import unittest

from buffer_heap import SLOTS, BufferPool, FileManager, HeapFile, registro
from partition_hash_join import PartitionHashJoin, RelationInput


def reference_multiset(left, right, left_key=0, right_key=0):
    """INNER JOIN simples usado como oráculo em memória nos casos pequenos."""
    return Counter((lrow, rrow) for lrow in left for rrow in right
                   if lrow[left_key] is not None
                   and rrow[right_key] is not None
                   and lrow[left_key] == rrow[right_key])


def sqlite_multiset(left, right):
    """Oráculo independente que compara todas as seis colunas e multiplicidade."""
    db = sqlite3.connect(':memory:')
    try:
        db.execute('create table r(a integer, b integer, c integer)')
        db.execute('create table s(a integer, b integer, c integer)')
        db.executemany('insert into r values(?,?,?)', left)
        db.executemany('insert into s values(?,?,?)', right)
        rows = db.execute(
            'select r.a,r.b,r.c,s.a,s.b,s.c from r join s on r.a=s.a')
        return Counter((row[:3], row[3:]) for row in rows)
    finally:
        db.close()


class PartitionHashJoinValidationTests(unittest.TestCase):
    @staticmethod
    def run_join(left_rows, right_rows, *, capacity=4, memory_pages=None,
                 policy='LRU', left_key=0, right_key=0, max_depth=3,
                 left_extractor=None, right_extractor=None):
        """Executa sobre Heap/Buffer reais e devolve resultado e evidências."""
        with tempfile.TemporaryDirectory() as directory:
            pool = BufferPool(FileManager(directory), capacity, policy)
            left = HeapFile(pool, 'left')
            right = HeapFile(pool, 'right')
            for row in left_rows:
                left.insert(registro.encode(*row))
            for row in right_rows:
                right.insert(registro.encode(*row))

            # Força relações de entrada persistidas e uma medição fria.
            pool.drop_all()
            pool.reset_stats()
            operator = PartitionHashJoin(
                pool, memory_pages, max_depth=max_depth)
            lkey = left_extractor if left_extractor is not None else left_key
            rkey = right_extractor if right_extractor is not None else right_key
            result = Counter(
                (registro.decode(lraw), registro.decode(rraw))
                for lraw, rraw in operator.join(
                    RelationInput.from_source(left, lkey),
                    RelationInput.from_source(right, rkey)))

            io = pool.stats()
            evidence = {
                'stats': operator.stats,
                'io': io,
                'pins': pool.pinned_count(),
                'frames': len(pool.frames),
                'temp_files': tuple(
                    name for name in os.listdir(directory)
                    if name.startswith('phj_')),
                'temp_catalog': tuple(
                    name for name in pool.fm.kind if name.startswith('phj_')),
            }
            pool.close()
            return result, evidence

    def assert_clean_and_bounded(self, evidence, memory_pages):
        self.assertEqual(evidence['pins'], 0)
        self.assertEqual(evidence['temp_files'], ())
        self.assertEqual(evidence['temp_catalog'], ())
        self.assertLessEqual(evidence['frames'], memory_pages)
        self.assertLessEqual(evidence['stats'].max_build_records,
                             (memory_pages - 2) * SLOTS)

    def test_functional_matrix_as_multisets(self):
        cases = {
            'one-match': ([(1, 10, 100)], [(1, 20, 200)]),
            'no-match': ([(1, 10, 100)], [(2, 20, 200)]),
            'both-empty': ([], []),
            'left-empty': ([], [(1, 20, 200)]),
            'right-empty': ([(1, 10, 100)], []),
            'duplicates-left': (
                [(1, 10, 0), (1, 11, 0), (1, 12, 0)], [(1, 20, 0)]),
            'duplicates-right': (
                [(1, 10, 0)], [(1, 20, 0), (1, 21, 0), (1, 22, 0)]),
            'duplicates-both': (
                [(1, 10, 0), (1, 11, 0)],
                [(1, 20, 0), (1, 21, 0), (1, 22, 0)]),
            'negative-keys': (
                [(-7, 10, 0), (-1, 11, 0), (2, 12, 0)],
                [(-7, 20, 0), (-1, 21, 0), (3, 22, 0)]),
            'different-cardinalities': (
                [(i % 4, i, 0) for i in range(19)],
                [(i % 5, 100 + i, 0) for i in range(7)]),
        }
        for name, (left, right) in cases.items():
            with self.subTest(name=name):
                actual, evidence = self.run_join(left, right)
                self.assertEqual(actual, reference_multiset(left, right))
                self.assertEqual(evidence['stats'].output_pairs,
                                 sum(actual.values()))
                self.assert_clean_and_bounded(evidence, 4)

    def test_inverting_relations_preserves_logical_multiset(self):
        left = [(i % 6 - 3, i, i % 2) for i in range(37)]
        right = [(i % 5 - 2, 1000 + i, i % 3) for i in range(23)]
        forward, forward_evidence = self.run_join(left, right)
        reverse, reverse_evidence = self.run_join(right, left)
        normalized_reverse = Counter((left_row, right_row)
                                     for right_row, left_row in reverse.elements())
        self.assertEqual(forward, normalized_reverse)
        self.assertEqual(forward, sqlite_multiset(left, right))
        self.assert_clean_and_bounded(forward_evidence, 4)
        self.assert_clean_and_bounded(reverse_evidence, 4)

    def test_different_keys_sharing_partitions_do_not_false_match(self):
        # 64 chaves e no máximo 2 partições garantem muitas colisões externas.
        left = [(key, key + 1000, 1) for key in range(-32, 32)]
        right = [(key, key + 2000, 2) for key in range(-32, 32)]
        actual, evidence = self.run_join(
            left, right, capacity=3, memory_pages=3, max_depth=4)
        self.assertEqual(actual, reference_multiset(left, right))
        self.assertEqual(sum(actual.values()), 64)
        self.assert_clean_and_bounded(evidence, 3)

    def test_null_semantics_matches_sql(self):
        sentinel = -(2 ** 62)
        stored_left = [(1, 10, 0), (sentinel, 11, 0), (2, 12, 0)]
        stored_right = [(1, 20, 0), (sentinel, 21, 0), (3, 22, 0)]
        sql_left = [(None if a == sentinel else a, b, c)
                    for a, b, c in stored_left]
        sql_right = [(None if a == sentinel else a, b, c)
                     for a, b, c in stored_right]
        nullable = lambda raw: (None if registro.field(raw, 0) == sentinel
                                else registro.field(raw, 0))
        actual, evidence = self.run_join(
            stored_left, stored_right,
            left_extractor=nullable, right_extractor=nullable)
        expected_sql = sqlite_multiset(sql_left, sql_right)
        # O único par SQL não contém NULL, logo tem a mesma forma armazenada.
        self.assertEqual(actual, expected_sql)
        self.assertEqual(evidence['stats'].null_records, 2)
        self.assert_clean_and_bounded(evidence, 4)

    def test_fit_repartition_and_fallback_memory_paths(self):
        fit_left = [(i % 5, i, 0) for i in range(20)]
        fit_right = [(i % 5, 100 + i, 0) for i in range(25)]
        fit, fit_ev = self.run_join(fit_left, fit_right, capacity=4)
        self.assertEqual(fit, reference_multiset(fit_left, fit_right))
        self.assertEqual(fit_ev['stats'].repartition_passes, 0)
        self.assertEqual(fit_ev['stats'].fallback_partitions, 0)
        self.assert_clean_and_bounded(fit_ev, 4)

        uniform_left = [(i % 29, i, 0) for i in range(155)]
        uniform_right = [(i % 29, 1000 + i, 0) for i in range(186)]
        uniform, uniform_ev = self.run_join(
            uniform_left, uniform_right, capacity=3, memory_pages=3,
            max_depth=5)
        self.assertEqual(uniform, reference_multiset(uniform_left, uniform_right))
        self.assertGreater(uniform_ev['stats'].repartition_passes, 0)
        self.assert_clean_and_bounded(uniform_ev, 3)

        skew_left = [(7, i, 0) for i in range(96)]
        skew_right = [(7, 1000 + i, 0) for i in range(70)]
        skew, skew_ev = self.run_join(
            skew_left, skew_right, capacity=3, memory_pages=3,
            max_depth=5)
        self.assertEqual(skew, reference_multiset(skew_left, skew_right))
        self.assertEqual(skew_ev['stats'].repartition_passes, 0)
        self.assertEqual(skew_ev['stats'].fallback_partitions, 1)
        self.assertEqual(skew_ev['stats'].max_build_records, SLOTS)
        self.assert_clean_and_bounded(skew_ev, 3)

    def test_io_metrics_match_buffer_and_prove_temp_materialization(self):
        left = [(i % 13, i, 0) for i in range(100)]
        right = [(i % 13, 1000 + i, 0) for i in range(120)]
        actual, evidence = self.run_join(
            left, right, capacity=4, memory_pages=4)
        stats, io = evidence['stats'], evidence['io']
        self.assertEqual(actual, sqlite_multiset(left, right))
        self.assertEqual(stats.physical_reads, io['physical_reads'])
        self.assertEqual(stats.physical_writes, io['physical_writes'])
        self.assertEqual(stats.temp_reads, io['reads_by_kind']['temp'])
        self.assertEqual(stats.temp_writes, io['writes_by_kind']['temp'])
        self.assertGreater(stats.temp_reads, 0)
        self.assertGreater(stats.temp_writes, 0)
        self.assert_clean_and_bounded(evidence, 4)

    def test_deterministic_property_cases_against_sqlite(self):
        for seed in (0, 7, 42):
            for left_size, right_size in ((1, 1), (17, 9), (32, 47), (75, 63)):
                with self.subTest(seed=seed, sizes=(left_size, right_size)):
                    rng = random.Random(seed * 1000 + left_size * 10 + right_size)
                    left = [(rng.randrange(-8, 9), i, rng.randrange(5))
                            for i in range(left_size)]
                    right = [(rng.randrange(-8, 9), 1000 + i, rng.randrange(5))
                             for i in range(right_size)]
                    actual, evidence = self.run_join(
                        left, right, capacity=4, memory_pages=4)
                    self.assertEqual(actual, sqlite_multiset(left, right))
                    self.assert_clean_and_bounded(evidence, 4)

    def test_result_independent_of_buffer_size_and_policy(self):
        rng = random.Random(20261117)
        left = [(rng.randrange(-12, 13), i, rng.randrange(10))
                for i in range(111)]
        right = [(rng.randrange(-12, 13), 1000 + i, rng.randrange(10))
                 for i in range(137)]
        expected = sqlite_multiset(left, right)
        observed = []
        for policy in ('LRU', 'FIFO'):
            for pages in (3, 4, 8):
                with self.subTest(policy=policy, pages=pages):
                    actual, evidence = self.run_join(
                        left, right, capacity=pages, memory_pages=pages,
                        policy=policy, max_depth=5)
                    self.assertEqual(actual, expected)
                    self.assert_clean_and_bounded(evidence, pages)
                    observed.append(actual)
        self.assertTrue(all(result == observed[0] for result in observed[1:]))

    def test_multiple_consecutive_runs_are_reproducible(self):
        rng = random.Random(12345)
        left_rows = [(rng.randrange(9), i, 0) for i in range(80)]
        right_rows = [(rng.randrange(9), 1000 + i, 0) for i in range(90)]
        expected = sqlite_multiset(left_rows, right_rows)
        with tempfile.TemporaryDirectory() as directory:
            pool = BufferPool(FileManager(directory), 4, 'LRU')
            left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
            for row in left_rows:
                left.insert(registro.encode(*row))
            for row in right_rows:
                right.insert(registro.encode(*row))
            operator = PartitionHashJoin(pool, 4)
            left_input = RelationInput.from_source(left, 0)
            right_input = RelationInput.from_source(right, 0)
            results = []
            for _ in range(3):
                results.append(Counter(
                    (registro.decode(a), registro.decode(b))
                    for a, b in operator.join(left_input, right_input)))
                self.assertEqual(pool.pinned_count(), 0)
                self.assertFalse(any(name.startswith('phj_')
                                     for name in os.listdir(directory)))
            self.assertEqual(results, [expected, expected, expected])
            pool.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
