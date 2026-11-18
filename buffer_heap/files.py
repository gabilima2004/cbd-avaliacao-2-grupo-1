"""Gerente de arquivos em blocos de 4 KiB (E/S física)."""
import os

BLOCK = 4096


class CorruptFileError(Exception):
    pass


class FileManager:
    """Abre arquivos de blocos. O bloco n começa em n * 4096 bytes.

    Não interpreta o conteúdo: o bloco 0 pertence a quem usa o arquivo
    (heap, hash, B+). Novos blocos são sempre alocados no fim.
    """

    def __init__(self, directory):
        os.makedirs(directory, exist_ok=True)
        self.dir = directory
        self._fh = {}
        self._n = {}
        self.kind = {}
        self.reads = {}
        self.writes = {}
        self._retired_reads = {}
        self._retired_writes = {}

    def open(self, name, kind='data'):
        """kind: 'data', 'index' ou 'overflow' (só para as métricas)."""
        if name in self._fh:
            return
        path = os.path.join(self.dir, name)
        fh = open(path, 'r+b' if os.path.exists(path) else 'w+b', buffering=0)
        size = os.fstat(fh.fileno()).st_size
        if size % BLOCK:
            fh.close()
            raise CorruptFileError(f'{name}: tamanho {size} não é múltiplo de {BLOCK}')
        self._fh[name] = fh
        self._n[name] = size // BLOCK
        self.kind[name] = kind
        self.reads.setdefault(name, 0)
        self.writes.setdefault(name, 0)

    def nblocks(self, name):
        return self._n[name]

    def extend(self, name):
        """Reserva (logicamente) um bloco no fim e devolve seu número."""
        n = self._n[name]
        self._n[name] = n + 1
        return n

    def check(self, name, n):
        if not 0 <= n < self._n[name]:
            raise IndexError(f'{name}: bloco {n} fora da faixa [0, {self._n[name]})')

    def read_block(self, name, n):
        self.check(name, n)
        fh = self._fh[name]
        fh.seek(n * BLOCK)
        data = fh.read(BLOCK)
        self.reads[name] += 1
        if len(data) < BLOCK:  # bloco alocado e ainda nunca gravado
            data += bytes(BLOCK - len(data))
        return data

    def write_block(self, name, n, data):
        self.check(name, n)
        if len(data) != BLOCK:
            raise ValueError('bloco deve ter 4096 bytes')
        fh = self._fh[name]
        fh.seek(n * BLOCK)
        fh.write(data)
        self.writes[name] += 1

    def close_file(self, name):
        """Fecha um arquivo específico, preservando seus contadores de E/S."""
        fh = self._fh.pop(name, None)
        if fh is not None:
            fh.close()
        self._n.pop(name, None)

    def remove(self, name):
        """Fecha e remove um arquivo, preservando métricas já contabilizadas."""
        self.close_file(name)
        path = os.path.join(self.dir, name)
        if os.path.exists(path):
            os.remove(path)
        kind = self.kind.pop(name, None)
        reads = self.reads.pop(name, 0)
        writes = self.writes.pop(name, 0)
        if kind is not None:
            self._retired_reads[kind] = self._retired_reads.get(kind, 0) + reads
            self._retired_writes[kind] = self._retired_writes.get(kind, 0) + writes

    def close(self):
        for fh in self._fh.values():
            fh.close()
        self._fh.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
