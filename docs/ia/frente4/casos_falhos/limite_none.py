"""Regressão descoberta na revisão: None não é um limite int64 válido."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from buffer_heap import registro
from indice_bmais import BMaisSecundario
from tests.simulados import BufferSimulado, HeapSimulado


class LimiteInvalido(unittest.TestCase):
    def test_none_deve_ser_rejeitado(self):
        buffer = BufferSimulado()
        heap = HeapSimulado(buffer)
        index = BMaisSecundario(buffer, 'bplus', heap, campo=2)
        index.insert(7, heap.insert(registro.encode(1, 1, 7)))
        for method in (index.find_range, index.find_range_rid):
            with self.subTest(method=method.__name__):
                with self.assertRaises(ValueError):
                    list(method(None, '<'))
        self.assertEqual(buffer.pinned, {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
