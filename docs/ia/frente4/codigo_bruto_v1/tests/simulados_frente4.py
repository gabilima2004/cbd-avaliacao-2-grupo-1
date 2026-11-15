"""Dublê lógico da B+ primária ausente. Não representa seus custos físicos."""
from buffer_heap import registro
from indice_bmais.core import OPS
from tests.simulados import BufferSimulado, HeapSimulado


class BMaisPrimariaSimulada:
    def __init__(self, campo):
        self.buffer = BufferSimulado()
        self.heap = HeapSimulado(self.buffer)
        self.arquivo, self.campo = self.heap.arquivo, campo
        self.rows = []
        self.calls = []
        self.last_access = None

    def insert(self, raw):
        self.rows.append(raw)
        return self.heap.insert(raw)

    def get(self, rid):
        return self.heap.get(rid)

    def scan_pages(self):
        for start in range(0, len(self.rows), 31):
            yield self.rows[start:start + 31]

    def find_equal(self, campo, valor, acesso):
        self.calls.append(('equal', campo, valor, acesso))
        self.last_access = 'primary' if campo == self.campo and acesso == 'primary' else 'scan'
        return (r for r in self.rows if registro.field(r, campo) == valor)

    def find_range(self, campo, limite, operador, acesso):
        self.calls.append(('range', campo, limite, operador, acesso))
        self.last_access = 'primary' if campo == self.campo and acesso == 'primary' else 'scan'
        return (r for r in self.rows if OPS[operador](registro.field(r, campo), limite))
