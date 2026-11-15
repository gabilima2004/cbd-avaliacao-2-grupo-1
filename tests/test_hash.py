import random
import sqlite3
import unittest
from hash_externo import HashPrimario, HashSecundario, Registro
from tests.simulados import BufferSimulado, HeapSimulado


def row(a, b=0, c=0):
    return Registro(a, b, c).encode()


class HashTests(unittest.TestCase):
    def setUp(self):
        self.buffer = BufferSimulado()

    def table(self, **kw):
        return HashPrimario(self.buffer, 'hash', self.buffer.allocate, buckets=4, **kw)

    def tearDown(self):
        self.assertEqual(self.buffer.pinned, {})
        self.assertLessEqual(self.buffer.max_pinned, 1)

    def test_01_unique_overflow(self):
        h = self.table()
        locations = [h.insert(row(i*4, i)) for i in range(32)]
        self.assertEqual(locations[-1].bloco, 5)
        self.assertEqual(h.stats()['overflow_blocks'], 1)
        self.assertEqual(list(h.find(124)), [row(124, 31)])
        self.assertEqual(list(h.find(128)), [])
        with self.assertRaises(ValueError):
            h.insert(row(124, 999))
        print('UNIQUE: 32 chaves no mesmo bucket, overflow=5; chave 124 encontrada; 128 ausente; duplicata rejeitada')

    def test_02_repeated_delete_overflow(self):
        h = self.table(unique=False)
        rows = [row(7, i) for i in range(70)]
        for raw in rows:
            h.insert(raw)
        self.assertEqual(list(h.find(7)), rows)
        self.assertEqual(h.stats()['overflow_blocks'], 2)
        self.assertTrue(h.delete(7, rows[40]))
        self.assertFalse(h.delete(7, rows[40]))
        self.assertEqual(list(h.find(7)), rows[:40]+rows[41:])
        rid = h.insert(row(7, 999))
        self.assertEqual((rid.bloco, rid.slot), (5, 9))
        print('REPETIDA: 70 registros em 3 blocos; removido registro 40 no overflow; slot reutilizado')

    def test_03_secondary_heap(self):
        heap = HeapSimulado(self.buffer)
        index = HashSecundario(self.table(), heap)
        for i in range(65):
            raw = row(i, 1000+4*i, i % 100)
            rid = heap.insert(raw)
            index.insert(1000+4*i, rid)
        self.assertEqual(list(index.find(1256)), [row(64, 1256, 64)])
        self.assertEqual(list(index.find(1260)), [])
        self.assertEqual(list(index.find_rid(1256))[0].bloco, 3)
        with self.assertRaises(ValueError):
            index.insert(1256, rid)
        self.assertTrue(index.delete(1256))
        self.assertEqual(list(index.find(1256)), [])
        self.assertEqual(heap.get(rid), row(64, 1256, 64))
        print('SECUNDARIO: chave 1256 -> RID(3,2) -> aluno 64; remocao preserva heap')

    def test_04_reopen_scan_range(self):
        h = self.table(campo=1)
        for i in range(70):
            h.insert(row(i, -4*i, i % 10))
        h = HashPrimario(self.buffer, 'hash', self.buffer.allocate)
        self.assertEqual(list(h.find(-256)), [row(64, -256, 4)])
        self.assertEqual(len(list(h.find_range(2, 5, '<'))), 35)
        self.assertEqual(h.last_access, 'scan')
        self.assertEqual(list(h.find_equal(0, 64)), [row(64, -256, 4)])
        self.assertEqual(h.stats()['records'], 70)

    def test_05_stress_sqlite(self):
        h = self.table(unique=False)
        rng = random.Random(42)
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('create table r(a,b,c)')
        rows = [(rng.randrange(-20, 20), i, rng.randrange(100)) for i in range(3968)]
        for values in rows:
            h.insert(row(*values))
        db.executemany('insert into r values(?,?,?)', rows)
        for values in rows[::17]:
            self.assertTrue(h.delete(values[0], row(*values)))
            db.execute('delete from r where b=?', (values[1],))
        for key in range(-21, 21):
            actual = sorted(Registro.decode(r).b for r in h.find(key))
            expected = sorted(x[0] for x in db.execute('select b from r where a=?', (key,)))
            self.assertEqual(actual, expected)
        actual = sorted(Registro.decode(r).b for r in h.find_range(2, 50, '<'))
        expected = sorted(x[0] for x in db.execute('select b from r where c<50'))
        self.assertEqual(actual, expected)
        self.assertEqual(h.stats()['records'], db.execute('select count(*) from r').fetchone()[0])
        print('ESTRESSE: semente=42, 3968 insercoes, 234 exclusoes, 42 buscas e faixa conferidas no SQLite')

    def test_06_invalid_and_lazy_release(self):
        h = self.table()
        with self.assertRaises(ValueError):
            h.insert(b'curto')
        self.assertFalse(h.delete(0))
        h.insert(row(0))
        it = h.find(0)
        next(it)
        self.assertEqual(self.buffer.pinned, {})
        it.close()
        with self.assertRaises(ValueError):
            list(h.find_equal(3, 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
