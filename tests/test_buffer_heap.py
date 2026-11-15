import os
import random
import sqlite3
import tempfile
import unittest

from buffer_heap import (BLOCK, SLOTS, BufferFullError, BufferPool, CorruptFileError,
                         FileManager, HeapFile, PinError, RID, registro)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._fms = []

    def tearDown(self):
        for fm in self._fms:  # Windows não apaga arquivo aberto
            fm.close()
        self._tmp.cleanup()

    def pool(self, capacity=3, policy='LRU', files=('f',)):
        fm = FileManager(self.dir)
        self._fms.append(fm)
        for f in files:
            fm.open(f)
        return BufferPool(fm, capacity, policy)

    def blocks(self, pool, name, n):
        for _ in range(n):
            pool.allocate(name)
        pool.flush()
        pool.reset_stats()


class TestBuffer(Base):
    def seq(self, policy, accesses):
        p = self.pool(3, policy)
        self.blocks(p, 'f', 6)
        for b in accesses:
            p.pin('f', b)
            p.unpin('f', b)
        return p

    def test_lru_known_sequence(self):
        # 0 1 2 | 0 (hit) | 3 expulsa 1 | 1 (miss, expulsa 2)
        p = self.seq('LRU', [0, 1, 2, 0, 3, 1])
        s = p.stats()
        self.assertEqual((s['requests'], s['hits'], s['misses']), (6, 1, 5))
        self.assertEqual(s['physical_reads'], 5)
        self.assertEqual(list(k[1] for k in p.frames), [0, 3, 1])

    def test_fifo_known_sequence(self):
        # FIFO ignora o hit em 0: 3 expulsa 0; 1 ainda está no pool
        p = self.seq('FIFO', [0, 1, 2, 0, 3, 1])
        s = p.stats()
        self.assertEqual((s['requests'], s['hits'], s['misses']), (6, 2, 4))
        self.assertEqual(list(k[1] for k in p.frames), [1, 2, 3])

    def test_dirty_written_on_eviction_only(self):
        p = self.pool(2)
        self.blocks(p, 'f', 4)
        page = p.pin('f', 0)
        page[0] = 77
        p.unpin('f', 0, dirty=True)
        self.assertEqual(p.stats()['physical_writes'], 0)
        for b in (1, 2):
            p.pin('f', b)
            p.unpin('f', b)
        s = p.stats()
        self.assertEqual((s['writes_evict'], s['physical_writes']), (1, 1))
        self.assertEqual(p.pin('f', 0)[0], 77)  # voltou do disco
        p.unpin('f', 0)

    def test_clean_page_not_written(self):
        p = self.pool(1)
        self.blocks(p, 'f', 3)
        for b in (0, 1, 2):
            p.pin('f', b)
            p.unpin('f', b)
        self.assertEqual(p.stats()['physical_writes'], 0)

    def test_pinned_page_never_evicted_and_full_pool(self):
        p = self.pool(2)
        self.blocks(p, 'f', 4)
        p.pin('f', 0)
        p.pin('f', 1)
        with self.assertRaises(BufferFullError):
            p.pin('f', 2)
        p.unpin('f', 0)
        p.pin('f', 2)  # agora 0 pode sair
        self.assertNotIn(('f', 0), p.frames)
        self.assertIn(('f', 1), p.frames)

    def test_full_pool_failures_leave_no_trace(self):
        # regressão do code review: allocate/pin falhando com pool cheio de fixadas
        p = self.pool(2)
        self.blocks(p, 'f', 4)
        p.pin('f', 0)
        p.pin('f', 1)
        p.reset_stats()
        with self.assertRaises(BufferFullError):
            p.allocate('f')
        self.assertEqual(p.fm.nblocks('f'), 4)          # sem bloco fantasma
        with self.assertRaises(BufferFullError):
            p.pin('f', 3)
        self.assertEqual(p.stats()['physical_reads'], 0)  # sem leitura desperdiçada

    def test_context_manager_closes_files(self):
        with BufferPool(FileManager(self.dir), 2) as p:
            p.fm.open('g')
            p.allocate('g')
        self.assertEqual(p.fm._fh, {})
        os.remove(os.path.join(self.dir, 'g'))  # falharia no Windows se o arquivo seguisse aberto

    def test_nested_pins_counted(self):
        p = self.pool(2)
        self.blocks(p, 'f', 3)
        p.pin('f', 0)
        p.pin('f', 0)
        p.unpin('f', 0)
        self.assertEqual(p.pinned_count(), 1)
        p.unpin('f', 0)
        self.assertEqual(p.pinned_count(), 0)
        with self.assertRaises(PinError):
            p.unpin('f', 0)

    def test_unpin_without_pin_and_out_of_range(self):
        p = self.pool()
        self.blocks(p, 'f', 2)
        before = list(p.frames)
        with self.assertRaises(PinError):
            p.unpin('f', 1)
        with self.assertRaises(IndexError):
            p.pin('f', 2)
        with self.assertRaises(IndexError):
            p.pin('f', -1)
        self.assertEqual(list(p.frames), before)  # falha não cria frame nem expulsa nada

    def test_persistence_after_flush_and_reopen(self):
        p = self.pool(2)
        self.blocks(p, 'f', 3)
        pg = p.pin('f', 2)
        pg[10] = 9
        p.unpin('f', 2, dirty=True)
        p.close()
        p2 = self.pool(2)
        self.assertEqual(p2.fm.nblocks('f'), 3)
        self.assertEqual(p2.pin('f', 2)[10], 9)
        p2.unpin('f', 2)

    def test_close_with_pinned_page_fails(self):
        p = self.pool()
        self.blocks(p, 'f', 1)
        p.pin('f', 0)
        with self.assertRaises(PinError):
            p.close()

    def test_corrupt_file_size_rejected(self):
        with open(os.path.join(self.dir, 'bad'), 'wb') as fh:
            fh.write(b'x' * 100)
        with self.assertRaises(CorruptFileError):
            FileManager(self.dir).open('bad')

    def test_drop_all_gives_cold_buffer(self):
        p = self.pool(4)
        self.blocks(p, 'f', 3)
        p.pin('f', 0)
        p.unpin('f', 0)
        p.drop_all()
        p.reset_stats()
        p.pin('f', 0)
        p.unpin('f', 0)
        self.assertEqual(p.stats()['misses'], 1)

    def test_invalid_args(self):
        fm = FileManager(self.dir)
        with self.assertRaises(ValueError):
            BufferPool(fm, 32, 'MRU')
        with self.assertRaises(ValueError):
            BufferPool(fm, 0)


class TestHeap(Base):
    def heap(self, capacity=32, policy='LRU'):
        p = self.pool(capacity, policy, files=())
        return p, HeapFile(p, 'heap')

    def test_31_per_block_and_sizes(self):
        for n, blocks in ((248, 8), (3968, 128)):
            with self.subTest(n=n):
                p, h = self.heap()
                for i in range(n):
                    h.insert(registro.encode(i, i * 10, i % 100))
                self.assertEqual(h.data_blocks, blocks)
                self.assertEqual(h.stats()['occupancy'], 1.0)
                self.assertEqual(p.pinned_count(), 0)
                p.close()
                os.remove(os.path.join(self.dir, 'heap'))

    def test_file_layout_4096_and_header(self):
        p, h = self.heap()
        h.insert(registro.encode(1, 2, 3))
        p.close()
        raw = open(os.path.join(self.dir, 'heap'), 'rb').read()
        self.assertEqual(len(raw), 2 * BLOCK)
        self.assertEqual(raw[:8], b'HEAPv002')
        self.assertEqual(raw[BLOCK + 8], 1)                      # marca do slot 0
        self.assertEqual(registro.decode(raw[BLOCK + 128:BLOCK + 256]), (1, 2, 3))

    def test_slot_reuse_after_delete(self):
        p, h = self.heap()
        rids = [h.insert(registro.encode(i, 0, 0)) for i in range(SLOTS + 5)]
        h.delete(rids[3])
        h.delete(rids[SLOTS + 1])
        self.assertIsNone(h.get(rids[3]))
        r1 = h.insert(registro.encode(900, 0, 0))
        r2 = h.insert(registro.encode(901, 0, 0))
        self.assertEqual({r1, r2}, {rids[3], rids[SLOTS + 1]})
        self.assertEqual(h.data_blocks, 2)
        self.assertEqual(registro.decode(h.get(r1))[0], 900)

    def test_insert_cost_is_one_block(self):
        p, h = self.heap()
        for i in range(3968):
            h.insert(registro.encode(i, 0, 0))
        p.drop_all()
        h2 = HeapFile(p, 'heap')   # reabre: lista de livres vem do disco
        p.reset_stats()
        h2.delete(RID(64, 3))
        self.assertLessEqual(p.stats()['requests'], 3)   # bloco de dados + bloco 0 (cabeçalho)
        p.reset_stats()
        self.assertEqual(h2.insert(registro.encode(1, 1, 1)), RID(64, 3))
        self.assertLessEqual(p.stats()['requests'], 3)

    def test_free_list_invariant(self):
        rnd = random.Random(7)
        p, h = self.heap()
        live = [h.insert(registro.encode(i, 0, 0)) for i in range(200)]
        for _ in range(3000):
            if live and rnd.random() < 0.5:
                h.delete(live.pop(rnd.randrange(len(live))))
            else:
                live.append(h.insert(registro.encode(1, 0, 0)))
        seen, blk = set(), h.free_head
        while blk:                                  # percorre a lista de livres
            self.assertNotIn(blk, seen, 'ciclo na lista de livres')
            seen.add(blk)
            page = p.pin('heap', blk)
            has_free = 0 in page[8:39]
            nxt = int.from_bytes(page[:8], 'little')
            p.unpin('heap', blk)
            self.assertTrue(has_free, 'bloco cheio na lista de livres')
            blk = nxt
        for b in range(1, h.data_blocks + 1):       # e todo bloco com vaga está na lista
            page = p.pin('heap', b)
            has_free = 0 in page[8:39]
            p.unpin('heap', b)
            self.assertEqual(has_free, b in seen, f'bloco {b}')

    def test_delete_errors(self):
        p, h = self.heap()
        r = h.insert(registro.encode(1, 1, 1))
        h.delete(r)
        with self.assertRaises(KeyError):
            h.delete(r)
        with self.assertRaises(IndexError):
            h.get(RID(99, 0))
        with self.assertRaises(IndexError):
            h.get(RID(1, SLOTS))
        with self.assertRaises(IndexError):
            h.delete(RID(0, 0))  # bloco 0 é cabeçalho
        self.assertEqual(p.pinned_count(), 0)

    def test_insert_wrong_size(self):
        p, h = self.heap()
        with self.assertRaises(ValueError):
            h.insert(b'abc')

    def test_scan_cost_cold(self):
        p, h = self.heap()
        for i in range(248):
            h.insert(registro.encode(i, i, i % 100))
        p.drop_all()
        p.reset_stats()
        total = sum(len(page) for page in h.scan_pages())
        s = p.stats()
        self.assertEqual(total, 248)
        self.assertEqual((s['physical_reads'], s['physical_writes'], s['misses']), (8, 0, 8))

    def test_scan_pages_holds_no_pin_between_yields(self):
        p, h = self.heap()
        for i in range(100):
            h.insert(registro.encode(i, i, 0))
        it = h.scan_pages()
        next(it)
        self.assertEqual(p.pinned_count(), 0)
        it.close()

    def test_access_path_only_scan(self):
        p, h = self.heap()
        with self.assertRaises(ValueError):
            h.find_equal(0, 1, 'primary')

    def test_reopen_persistence(self):
        p, h = self.heap()
        rids = [h.insert(registro.encode(i, i, 0)) for i in range(70)]
        h.delete(rids[5])
        p.close()
        p2 = self.pool(32, 'LRU', files=())
        h2 = HeapFile(p2, 'heap')
        self.assertEqual(h2.stats()['records'], 69)
        self.assertIsNone(h2.get(rids[5]))
        self.assertEqual(h2.insert(registro.encode(5, 0, 0)), rids[5])

    def test_selection_matches_sqlite(self):
        rnd = random.Random(42)
        p, h = self.heap()
        db = sqlite3.connect(':memory:')
        db.execute('create table aluno(aluno_id int, dre int, faixa_cr int)')
        for i in range(3968):
            row = (i, 100000 + i, rnd.randrange(100))
            h.insert(registro.encode(*row))
            db.execute('insert into aluno values (?,?,?)', row)
        for lim in (1, 50):
            got = sorted(registro.decode(r) for r in h.find_range(2, lim, '<'))
            exp = sorted(db.execute('select * from aluno where faixa_cr < ?', (lim,)))
            self.assertEqual(got, exp)
        got = [registro.decode(r) for r in h.find_equal(1, 100777)]
        self.assertEqual(got, list(db.execute('select * from aluno where dre=100777')))
        self.assertEqual(p.pinned_count(), 0)


class TestStress(Base):
    def run_churn(self, policy, ops=20000, seed=42):
        rnd = random.Random(seed)
        p = self.pool(32, policy, files=())
        h = HeapFile(p, 'heap')
        model = {}
        for step in range(ops):
            if model and rnd.random() < 0.45:
                rid = rnd.choice(list(model)) if len(model) < 500 else next(iter(model))
                h.delete(rid)
                del model[rid]
            else:
                key = rnd.randrange(10 ** 9)
                rid = h.insert(registro.encode(key, step, 0))
                self.assertNotIn(rid, model, 'RID reutilizado enquanto ocupado')
                model[rid] = key
            self.assertLessEqual(len(p.frames), 32)
            self.assertEqual(p.pinned_count(), 0)
        self.assertEqual({r: registro.decode(h.get(r))[0] for r in model}, model)
        self.assertEqual(sum(len(x) for x in h.scan_pages()), len(model))
        p.close()

    def test_churn_lru(self):
        self.run_churn('LRU')

    def test_churn_fifo(self):
        self.run_churn('FIFO')

    def test_exception_in_scan_consumer_leaves_no_pin(self):
        p = self.pool(32, 'LRU', files=())
        h = HeapFile(p, 'heap')
        for i in range(200):
            h.insert(registro.encode(i, 0, 0))
        try:
            for recs in h.scan_pages():
                raise RuntimeError('consumidor falhou')
        except RuntimeError:
            pass
        self.assertEqual(p.pinned_count(), 0)


if __name__ == '__main__':
    unittest.main()
