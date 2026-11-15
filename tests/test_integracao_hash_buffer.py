"""Frente 3 (hash externo) rodando sobre o buffer e os arquivos reais da frente 1."""
import math
import os
import random
import sqlite3
import tempfile
import unittest

from buffer_heap import BufferPool, FileManager, HeapFile, registro
from hash_externo import HashPrimario, HashSecundario


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.pools = []

    def tearDown(self):
        for p in self.pools:
            p.fm.close()
        self._tmp.cleanup()

    def pool(self, capacity=32, policy='LRU'):
        p = BufferPool(FileManager(self.dir), capacity, policy)
        self.pools.append(p)
        return p

    def new_hash(self, pool, buckets, unique=True, campo=0, name='hash', kind='data'):
        pool.fm.open(name, kind)
        return HashPrimario(pool, name, pool.allocate, buckets=buckets, campo=campo, unique=unique)

    def open_hash(self, pool, name='hash', kind='data'):
        pool.fm.open(name, kind)
        return HashPrimario(pool, name, pool.allocate)


class TestHashNoBufferReal(Base):
    def test_overflow_chain_and_known_cold_cost(self):
        p = self.pool()
        h = self.new_hash(p, 4)
        for i in range(32):                       # 32 chaves no mesmo bucket: 31 + 1 em overflow
            h.insert(registro.encode(i * 4, i, 0))
        self.assertEqual(h.stats()['overflow_blocks'], 1)
        p.drop_all()
        p.reset_stats()
        self.assertEqual(len(list(h.find(124))), 1)           # 124 = 31 * 4, está no overflow
        s = p.stats()
        self.assertEqual((s['physical_reads'], s['physical_writes']), (2, 0))  # bucket + overflow
        p.reset_stats()
        self.assertEqual(list(h.find(128)), [])               # mesmo bucket, ausente
        self.assertEqual(p.stats()['physical_reads'], 0)      # as 2 páginas já estão no pool
        self.assertEqual(p.stats()['hits'], 2)
        self.assertEqual(p.pinned_count(), 0)

    def test_persistence_after_close_and_reopen(self):
        p = self.pool()
        h = self.new_hash(p, 8)
        for i in range(500):
            h.insert(registro.encode(i, i * 2, i % 100))
        before = h.stats()
        p.close()
        p2 = self.pool()
        h2 = self.open_hash(p2)
        self.assertEqual(h2.stats(), before)
        self.assertEqual(registro.decode(next(h2.find(321))), (321, 642, 21))
        with self.assertRaises(ValueError):
            h2.insert(registro.encode(321, 0, 0))             # unicidade preservada

    def test_dirty_pages_survive_eviction_with_tiny_pool(self):
        p = self.pool(capacity=3)                             # força expulsões o tempo todo
        h = self.new_hash(p, 16)
        for i in range(1000):
            h.insert(registro.encode(i, i, 0))
        self.assertGreater(p.stats()['writes_evict'], 0)
        for i in range(0, 1000, 7):
            self.assertEqual(registro.decode(next(h.find(i))), (i, i, 0))
        self.assertEqual(p.pinned_count(), 0)
        self.assertLessEqual(len(p.frames), 3)

    def test_range_scan_reads_every_block_once(self):
        p = self.pool()
        h = self.new_hash(p, 160)
        for i in range(3968):
            h.insert(registro.encode(i, i, i % 100))
        total = h.stats()['total_blocks']
        p.drop_all()
        p.reset_stats()
        n = sum(1 for _ in h.find_range(2, 50, '<'))
        s = p.stats()
        self.assertEqual(n, sum(1 for i in range(3968) if i % 100 < 50))
        self.assertEqual(s['physical_reads'], total)          # bloco 0 + buckets + overflow
        self.assertEqual(h.last_access, 'scan')

    def test_matches_sqlite_lru_and_fifo(self):
        for policy in ('LRU', 'FIFO'):
            with self.subTest(policy=policy):
                rnd = random.Random(42)
                p = self.pool(32, policy)
                h = self.new_hash(p, math.ceil(3968 / (31 * 0.8)), name=f'h_{policy}')
                db = sqlite3.connect(':memory:')
                self.addCleanup(db.close)
                db.execute('create table aluno(aluno_id int, dre int, faixa_cr int)')
                for i in range(3968):
                    row = (i, 100000 + i, rnd.randrange(100))
                    h.insert(registro.encode(*row))
                    db.execute('insert into aluno values (?,?,?)', row)
                for lim in (1, 50):
                    got = sorted(registro.decode(r) for r in h.find_range(2, lim, '<'))
                    self.assertEqual(got, sorted(db.execute('select * from aluno where faixa_cr<?', (lim,))))
                for k in (0, 1234, 3967):
                    self.assertEqual([registro.decode(r) for r in h.find(k)],
                                     list(db.execute('select * from aluno where aluno_id=?', (k,))))
                self.assertEqual(p.pinned_count(), 0)

    def test_non_unique_primary_scan_pages(self):
        p = self.pool()
        h = self.new_hash(p, 4, unique=False, campo=2)
        for i in range(300):
            h.insert(registro.encode(i, i, i % 5))            # faixa_cr repetida
        self.assertEqual(len(list(h.find(3))), 60)
        self.assertEqual(sum(len(pg) for pg in h.scan_pages()), 300)


class TestSecundarioComHeapReal(Base):
    def test_secondary_hash_over_real_heap(self):
        p = self.pool()
        p.fm.open('heap', 'data')
        heap = HeapFile(p, 'heap')
        idx_h = self.new_hash(p, 20, name='idx', kind='index')
        sec = HashSecundario(idx_h, heap)
        rids = {}
        for i in range(600):
            rid = heap.insert(registro.encode(i, 5000 + i, i % 100))
            sec.insert(i, rid)
            rids[i] = rid
        p.drop_all()
        p.reset_stats()
        got = [registro.decode(r) for r in sec.find(417)]
        s = p.stats()
        self.assertEqual(got, [(417, 5417, 17)])
        self.assertEqual(s['reads_by_kind'], {'data': 1, 'index': 1})   # 1 bloco de índice + 1 de dados
        self.assertEqual(p.pinned_count(), 0)
        self.assertEqual(sec.delete(417), True)                          # remove do índice, preserva o heap
        self.assertEqual(list(sec.find(417)), [])
        self.assertIsNotNone(heap.get(rids[417]))

    def test_secondary_survives_reopen(self):
        p = self.pool()
        p.fm.open('heap', 'data')
        heap = HeapFile(p, 'heap')
        sec = HashSecundario(self.new_hash(p, 8, name='idx', kind='index'), heap)
        for i in range(100):
            sec.insert(i, heap.insert(registro.encode(i, i, 0)))
        p.close()
        p2 = self.pool()
        p2.fm.open('heap', 'data')
        sec2 = HashSecundario(self.open_hash(p2, 'idx', 'index'), HeapFile(p2, 'heap'))
        self.assertEqual(registro.decode(next(sec2.find(77))), (77, 77, 0))


class TestEstresseIntegrado(Base):
    def test_skewed_churn_with_minimum_pool(self):
        """Chaves múltiplas de B (cadeias longas) + inserções/exclusões, pool de 2 páginas."""
        rnd = random.Random(42)
        B = 4
        p = self.pool(capacity=2)
        h = self.new_hash(p, B)
        model = set()
        for step in range(6000):
            if model and rnd.random() < 0.4:
                k = rnd.choice(sorted(model)) if len(model) < 200 else next(iter(model))
                self.assertTrue(h.delete(k))
                model.discard(k)
            else:
                k = rnd.randrange(0, 5000) * B
                if k in model:
                    with self.assertRaises(ValueError):
                        h.insert(registro.encode(k, 0, 0))
                else:
                    h.insert(registro.encode(k, step, 0))
                    model.add(k)
            self.assertEqual(p.pinned_count(), 0)
        got = {registro.decode(r)[0] for pg in h.scan_pages() for r in pg}
        self.assertEqual(got, model)
        self.assertGreater(h.stats()['overflow_blocks'], 0)


if __name__ == '__main__':
    unittest.main()
