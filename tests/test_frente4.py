"""Correção estrutural, seleções e estresse da frente 4; referência: SQLite."""
import random
import sqlite3
import tempfile
import unittest

from buffer_heap import BufferPool, CorruptFileError, FileManager, HeapFile, RID, registro
from hash_externo import HashPrimario, HashSecundario
from indice_bmais import BMaisSecundario, StaleIndexError
from indice_bmais.core import POST_CAPACITY
from selecoes import HashComRID, TabelaSelecao
from tests.simulados import BufferSimulado, HeapSimulado
from tests.simulados_frente4 import BMaisPrimariaSimulada


def decoded(rows):
    return sorted(registro.decode(raw) for raw in rows)


class RealBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def pool(self, policy='LRU', capacity=32):
        pool = BufferPool(FileManager(self.temp.name), capacity, policy)
        self.addCleanup(pool.close)
        return pool

    def database(self, rows):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('create table aluno(aluno_id integer, dre integer, faixa_cr integer)')
        db.executemany('insert into aluno values(?,?,?)', rows)
        return db

    def assert_range(self, table, db, campo, limit, op, acesso):
        column = ('aluno_id', 'dre', 'faixa_cr')[campo]
        self.assertEqual(decoded(table.find_range(campo, limit, op, acesso)),
                         sorted(db.execute(f'select * from aluno where {column} {op} ?', (limit,))))


class TestBasico(unittest.TestCase):
    def setUp(self):
        self.buffer = BufferSimulado()
        self.heap = HeapSimulado(self.buffer)
        self.index = BMaisSecundario(self.buffer, 'bplus', self.heap, campo=2, capacidade=4)

    def tearDown(self):
        self.assertEqual(self.buffer.pinned, {})
        self.assertLessEqual(self.buffer.max_pinned, 1)

    def test_01_valores_repetidos_negativos_e_limites(self):
        rows = [(i, 1000 + i, (i % 13) - 6) for i in range(248)]
        for values in rows:
            rid = self.heap.insert(registro.encode(*values))
            self.index.insert(values[2], rid)
        audit = self.index.validate(check_records=True)
        self.assertEqual(audit['distinct_keys'], 13)
        self.assertEqual(audit['records'], 248)
        self.assertGreaterEqual(audit['height'], 2)
        for op in ('<', '<=', '>', '>='):
            for limit in (-7, -6, 0, 6, 7):
                fn = {'<': lambda x: x < limit, '<=': lambda x: x <= limit,
                      '>': lambda x: x > limit, '>=': lambda x: x >= limit}[op]
                self.assertEqual(decoded(self.index.find_range(limit, op)),
                                 sorted(r for r in rows if fn(r[2])))
        self.assertEqual(list(self.index.find(7)), [])
        self.assertEqual(len(list(self.index.find(0))), 19)
        print('BASICO: 248 registros, 13 chaves distintas; negativos e quatro desigualdades OK (buffer/heap simulados)')

    def test_02_rids_em_varias_paginas_e_ordem_inversa(self):
        count = POST_CAPACITY * 3 + 19
        native_rids = [self.heap.insert(registro.encode(i, i, 7)) for i in range(count)]
        rids = [RID(rid.bloco, rid.slot) for rid in native_rids]
        for rid in reversed(rids):
            self.index.insert(7, rid)
        self.assertEqual(list(self.index.find_rid(7)), rids)
        before = self.index.validate(check_records=True)
        self.assertGreaterEqual(before['posting_pages'], 3)
        for rid in rids[::3]:
            self.assertTrue(self.index.delete(7, rid))
        expected = [rid for i, rid in enumerate(rids) if i % 3]
        self.assertEqual(list(self.index.find_rid(7)), expected)
        self.index.validate(check_records=True)
        self.assertTrue(self.index.delete(7))
        self.assertEqual(self.index.validate()['records'], 0)
        for rid in rids:
            self.index.insert(7, rid)
        self.assertLessEqual(self.index.blocks, before['total_blocks'])
        self.index.validate(check_records=True)

    def test_03_duplicata_e_entradas_invalidas_nao_alteram_indice(self):
        rid = self.heap.insert(registro.encode(1, 1, 2))
        self.index.insert(2, rid)
        before = self.index.validate()
        for key, invalid in ((2, rid), (True, rid), (1 << 63, rid),
                             (1, RID(0, 0)), (1, RID(1, 31)), (1, (1, 0))):
            with self.assertRaises(ValueError):
                self.index.insert(key, invalid)
        self.assertEqual(self.index.validate(), before)
        self.assertFalse(self.index.delete(2, RID(99, 0)))
        self.assertFalse(self.index.delete(999))
        with self.assertRaises(ValueError):
            list(self.index.find_range(2, '!='))

    def test_04_iteracao_parcial_libera_pins_e_excecao_do_consumidor(self):
        for i in range(600):
            self.index.insert(2, self.heap.insert(registro.encode(i, i, 2)))
        for iterator in (self.index.find(2), self.index.find_range(3, '<'),
                         self.index.find_rid(2), self.index.find_range_rid(1, '>')):
            next(iterator)
            self.assertEqual(self.buffer.pinned, {})
            iterator.close()
        original_get = self.heap.get
        def fail(_):
            raise RuntimeError('falha simulada da tabela')
        self.heap.get = fail
        try:
            with self.assertRaisesRegex(RuntimeError, 'falha simulada'):
                next(self.index.find(2))
            self.assertEqual(self.buffer.pinned, {})
        finally:
            self.heap.get = original_get

    def test_05_reabertura_recupera_metadados_e_chaves_int64(self):
        for i, key in enumerate((-(1 << 63), -1, 0, (1 << 63) - 1)):
            self.index.insert(key, self.heap.insert(registro.encode(i, i, key)))
        reopened = BMaisSecundario(self.buffer, 'bplus', self.heap)
        self.assertEqual((reopened.campo, reopened.leaf_cap), (2, 4))
        self.assertEqual(decoded(reopened.find_range(0, '>=')),
                         [(2, 2, 0), (3, 3, (1 << 63) - 1)])
        reopened.validate(check_records=True)

    def test_06_limite_none_e_outros_limites_invalidos(self):
        self.index.insert(7, self.heap.insert(registro.encode(1, 1, 7)))
        for method in (self.index.find_range, self.index.find_range_rid):
            for limit in (None, True, '7', 1.5, 1 << 63):
                with self.subTest(method=method.__name__, limit=limit):
                    with self.assertRaises(ValueError):
                        list(method(limit, '<'))


class TestSelecoes(RealBase):
    def test_01_heap_real_hash_unique_e_bplus_not_unique(self):
        pool = self.pool()
        heap = HeapFile(pool, 'heap')
        bplus = BMaisSecundario(pool, 'bplus', heap, campo=2)
        pool.fm.open('hash_index', 'index')
        unique = HashSecundario(HashPrimario(pool, 'hash_index', pool.allocate,
                                             buckets=16, campo=0, unique=True), heap)
        rng = random.Random(42)
        rows = [(i, 100000 + i, rng.randrange(100)) for i in range(248)]
        db = self.database(rows)
        for row in rows:
            rid = heap.insert(registro.encode(*row))
            bplus.insert(row[2], rid)
            unique.insert(row[1], rid)
        table = TabelaSelecao(heap, {1: unique, 2: bplus})
        for key in (100000, 100123, 100247, 999999):
            expected = sorted(db.execute('select * from aluno where dre=?', (key,)))
            for access in ('scan', 'secondary'):
                self.assertEqual(decoded(table.find_equal(1, key, access)), expected)
                self.assertEqual(table.last_access, access)
        for op in ('<', '<=', '>', '>='):
            for limit in (0, 1, 50, 99, 100):
                for access in ('scan', 'secondary'):
                    self.assert_range(table, db, 2, limit, op, access)
        with self.assertRaises(ValueError):
            list(table.find_equal(1, 100000, 'primary'))
        with self.assertRaises(ValueError):
            list(table.find_range(1, 100100, '<', 'secondary'))
        self.assertEqual(pool.pinned_count(), 0)
        bplus.validate(check_records=True)
        print('SELECOES REAIS: heap + hash secundario UNIQUE + B+ NOT UNIQUE; resultados iguais ao SQLite')

    def test_02_hash_real_primary_faixa_vira_scan_e_secondary(self):
        pool = self.pool()
        pool.fm.open('hash', 'data')
        base = HashPrimario(pool, 'hash', pool.allocate, buckets=4, campo=1, unique=True)
        adapted = HashComRID(base)
        index = BMaisSecundario(pool, 'bplus', adapted, campo=2)
        rows = [(i, 100000 + i, i % 100) for i in range(248)]
        db = self.database(rows)
        rids = []
        for row in rows:
            rid = base.insert(registro.encode(*row))
            rids.append(rid)
            index.insert(row[2], rid)
        table = TabelaSelecao(adapted, {2: index})
        self.assertEqual(decoded(table.find_equal(1, 100200, 'primary')), [rows[200]])
        self.assertEqual(table.last_access, 'primary')
        self.assertEqual(list(table.find_equal(1, -1, 'primary')), [])
        self.assertEqual(table.last_access, 'primary')
        for op in ('<', '<=', '>', '>='):
            for access in ('scan', 'primary', 'secondary'):
                self.assert_range(table, db, 2, 50, op, access)
                self.assertEqual(table.last_access, 'secondary' if access == 'secondary' else 'scan')
        self.assertEqual(adapted.get(rids[200]), registro.encode(*rows[200]))
        self.assertTrue(index.delete(rows[200][2], rids[200]))
        self.assertTrue(base.delete(rows[200][1], registro.encode(*rows[200])))
        self.assertIsNone(adapted.get(rids[200]))
        self.assertEqual(pool.pinned_count(), 0)
        index.validate(check_records=True)
        print('HASH REAL: igualdade usa primary; faixa solicitada como primary registra scan; B+ secundaria OK')

    def test_03_bplus_primaria_simulada_delega_contrato(self):
        for campo in (1, 2):
            with self.subTest(campo=campo):
                base = BMaisPrimariaSimulada(campo)
                index = BMaisSecundario(base.buffer, 'bplus', base, campo=2)
                rows = [(i, 100000 + i, i % 100) for i in range(248)]
                db = self.database(rows)
                for row in rows:
                    index.insert(row[2], base.insert(registro.encode(*row)))
                table = TabelaSelecao(base, {2: index})
                self.assertEqual(decoded(table.find_equal(1, 100123, 'primary')), [rows[123]])
                self.assertEqual(table.last_access, 'primary' if campo == 1 else 'scan')
                for op in ('<', '<=', '>', '>='):
                    self.assert_range(table, db, 2, 50, op, 'primary')
                    self.assertEqual(table.last_access, 'primary' if campo == 2 else 'scan')
                    self.assert_range(table, db, 2, 50, op, 'secondary')
                self.assertTrue(base.calls)
                self.assertTrue(all(call[-1] == 'primary' for call in base.calls))
                self.assertEqual(base.buffer.pinned, {})
        print('B+ PRIMARIA SIMULADA: somente contrato/delegacao; nenhuma medida fisica atribuida a este duble')

    def test_04_remocao_e_reuso_de_rid_exigem_manutencao(self):
        pool = self.pool()
        heap = HeapFile(pool, 'heap')
        index = BMaisSecundario(pool, 'bplus', heap, campo=2)
        rid = heap.insert(registro.encode(1, 1, 7))
        index.insert(7, rid)
        heap.delete(rid)
        with self.assertRaises(StaleIndexError):
            list(index.find(7))
        self.assertEqual(heap.insert(registro.encode(2, 2, 8)), rid)
        with self.assertRaises(StaleIndexError):
            list(index.find(7))
        self.assertTrue(index.delete(7, rid))
        index.insert(8, rid)
        self.assertEqual(decoded(index.find(8)), [(2, 2, 8)])
        index.validate(check_records=True)
        self.assertEqual(pool.pinned_count(), 0)

    def test_05_persistencia_real_e_evicao_dirty_lru_fifo(self):
        for policy in ('LRU', 'FIFO'):
            with self.subTest(policy=policy):
                pool = self.pool(policy, capacity=3)
                heap = HeapFile(pool, f'heap_{policy}')
                index = BMaisSecundario(pool, f'bplus_{policy}', heap, campo=2, capacidade=4)
                for i in range(248):
                    index.insert(i % 100, heap.insert(registro.encode(i, i, i % 100)))
                self.assertGreater(pool.stats()['writes_evict'], 0)
                index.validate(check_records=True)
                pool.close()
                reopened_pool = self.pool(policy, capacity=3)
                reopened_heap = HeapFile(reopened_pool, f'heap_{policy}')
                reopened = BMaisSecundario(reopened_pool, f'bplus_{policy}', reopened_heap)
                self.assertEqual(reopened.validate(check_records=True)['records'], 248)
                self.assertEqual(len(list(reopened.find_range(50, '<'))), 148)
                self.assertEqual(reopened_pool.pinned_count(), 0)
                reopened_pool.close()

    def test_06_erros_de_catalogo_parametros_e_cabecalho(self):
        pool = self.pool()
        heap = HeapFile(pool, 'heap')
        index = BMaisSecundario(pool, 'bplus', heap, campo=2)
        for capacity in (0, 2, 83, True):
            with self.assertRaises(ValueError):
                BMaisSecundario(pool, f'invalid_{capacity}', heap, campo=2, capacidade=capacity)
        with self.assertRaises(ValueError):
            BMaisSecundario(pool, 'bplus', heap, campo=2)
        with self.assertRaises(ValueError):
            BMaisSecundario(pool, 'heap', heap, campo=2)
        with self.assertRaises(ValueError):
            TabelaSelecao(heap, {1: index})
        table = TabelaSelecao(heap)
        for campo, value, acesso in ((-1, 0, 'scan'), (3, 0, 'scan'),
                                     (0, 1.1, 'scan'), (0, 1, 'auto')):
            with self.assertRaises(ValueError):
                list(table.find_equal(campo, value, acesso))
        with self.assertRaises(ValueError):
            list(table.find_equal(0, 1, 'secondary'))
        page = pool.pin('bplus', 0)
        page[0] = 0
        pool.unpin('bplus', 0, dirty=True)
        with self.assertRaises(CorruptFileError):
            BMaisSecundario(pool, 'bplus', heap)
        self.assertEqual(pool.pinned_count(), 0)


class TestEstresse(RealBase):
    def test_01_divisoes_fusoes_redistribuicao_e_raiz(self):
        for capacity in (3, 4, 5):
            with self.subTest(capacity=capacity):
                pool = self.pool(capacity=3)
                heap = HeapFile(pool, f'heap_{capacity}')
                index = BMaisSecundario(pool, f'bplus_{capacity}', heap,
                                        campo=0, capacidade=capacity)
                keys = list(range(-124, 124))
                random.Random(42).shuffle(keys)
                insertion_order = keys.copy()
                rids = {}
                for key in keys:
                    rids[key] = heap.insert(registro.encode(key, key, key % 100))
                    index.insert(key, rids[key])
                self.assertGreaterEqual(index.validate(check_records=True)['height'], 4)
                random.Random(43).shuffle(keys)
                for i, key in enumerate(keys):
                    self.assertTrue(index.delete(key))
                    heap.delete(rids[key])
                    if i % 7 == 0:
                        index.validate(check_records=True)
                empty = index.validate()
                self.assertEqual((empty['records'], empty['height']), (0, 1))
                before = index.blocks
                # A mesma sequência produz a mesma ocupação; uma ordem diferente
                # pode legitimamente precisar de mais páginas por causa dos splits.
                for key in insertion_order:
                    index.insert(key, heap.insert(registro.encode(key, key, key % 100)))
                self.assertLessEqual(index.blocks, before)
                index.validate(check_records=True)
                self.assertEqual(pool.pinned_count(), 0)
                pool.close()
        print('ESTRESSE ESTRUTURAL: capacidades 3/4/5; altura >=4, exclusao total, raiz reduzida e paginas reutilizadas')

    def test_02_fanout_fisico_divide_no_interno_e_raiz(self):
        pool = self.pool()
        heap = HeapFile(pool, 'heap')
        index = BMaisSecundario(pool, 'bplus', heap, campo=0)
        for key in range(11000):
            index.insert(key, heap.insert(registro.encode(key, key, 0)))
        audit = index.validate(check_records=True)
        self.assertGreaterEqual(audit['height'], 3)
        self.assertEqual(audit['internal_fanout'], 248)
        self.assertEqual(audit['records'], 11000)
        self.assertEqual(decoded(index.find(10999)), [(10999, 10999, 0)])
        self.assertEqual(pool.pinned_count(), 0)
        print(f'ESTRESSE FISICO: 11000 chaves, folhas=82, fanout=248, altura={audit["height"]}; sem capacidade artificial')

    def test_03_3968_registros_manutencao_e_sqlite_lru_fifo(self):
        for policy in ('LRU', 'FIFO'):
            with self.subTest(policy=policy):
                pool = self.pool(policy)
                heap = HeapFile(pool, f'heap_{policy}')
                index = BMaisSecundario(pool, f'bplus_{policy}', heap, campo=2)
                rng = random.Random(42)
                rows = [(i, 100000 + i, rng.randrange(100)) for i in range(3968)]
                db = self.database(rows)
                live = {}
                for row in rows:
                    rid = heap.insert(registro.encode(*row))
                    index.insert(row[2], rid)
                    live[row[0]] = (rid, row)
                for key in rng.sample(list(live), 1984):
                    rid, row = live.pop(key)
                    self.assertTrue(index.delete(row[2], rid))
                    heap.delete(rid)
                    db.execute('delete from aluno where aluno_id=?', (key,))
                for key in range(3968, 5952):
                    row = (key, 100000 + key, rng.randrange(100))
                    rid = heap.insert(registro.encode(*row))
                    index.insert(row[2], rid)
                    db.execute('insert into aluno values(?,?,?)', row)
                index.validate(check_records=True)
                table = TabelaSelecao(heap, {2: index})
                for op in ('<', '<=', '>', '>='):
                    for limit in (-1, 0, 1, 50, 99, 100):
                        for acesso in ('scan', 'secondary'):
                            self.assert_range(table, db, 2, limit, op, acesso)
                for key in range(-1, 101):
                    self.assertEqual(decoded(index.find(key)),
                                     sorted(db.execute('select * from aluno where faixa_cr=?', (key,))))
                self.assertEqual(index.validate()['records'], 3968)
                self.assertEqual(pool.pinned_count(), 0)
                pool.close()
        print('ESTRESSE SQLITE: semente=42; 3968 cargas, 1984 exclusoes e reinsercoes; LRU/FIFO, todos os operadores OK')

    def test_04_churn_intercalado_em_arvore_profunda(self):
        pool = self.pool(capacity=3)
        heap = HeapFile(pool, 'heap_churn')
        index = BMaisSecundario(pool, 'bplus_churn', heap, campo=2, capacidade=4)
        rng, model = random.Random(123), {}
        for step in range(5000):
            if not model or rng.random() < 0.58:
                key = rng.randrange(-64, 65)
                row = (step, step, key)
                rid = heap.insert(registro.encode(*row))
                index.insert(key, rid)
                model.setdefault(key, {})[rid] = row
            else:
                key = rng.choice(list(model))
                if rng.random() < 0.15:
                    self.assertTrue(index.delete(key))
                    for rid in model.pop(key):
                        heap.delete(rid)
                else:
                    rid = rng.choice(list(model[key]))
                    self.assertTrue(index.delete(key, rid))
                    heap.delete(rid)
                    del model[key][rid]
                    if not model[key]:
                        del model[key]
            if step % 53 == 0:
                index.validate(check_records=True)
                limit = rng.randrange(-65, 66)
                expected = sorted(row for bucket in model.values()
                                  for row in bucket.values() if row[2] >= limit)
                self.assertEqual(decoded(index.find_range(limit, '>=')), expected)
                self.assertEqual(pool.pinned_count(), 0)
        index.validate(check_records=True)
        print('CHURN: 5000 operacoes intercaladas, chaves repetidas, exclusao de pares/grupos; buffer real de 3 paginas')


if __name__ == '__main__':
    unittest.main(verbosity=2)
