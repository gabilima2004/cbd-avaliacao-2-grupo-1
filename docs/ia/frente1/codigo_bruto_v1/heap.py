"""Arquivo heap: bloco 0 = cabeçalho; blocos 1.. = 128 B de cabeçalho + 31 registros de 128 B."""
import operator
import struct
from typing import NamedTuple

from .files import BLOCK, CorruptFileError
from .registro import RECORD, field

SLOTS = 31
PAGE_HEADER = 128            # bytes 0-7 reservados; 8..38 marcas de ocupação
MARKS = 8
MAGIC = b'HEAPv001'
_META = struct.Struct('<8sIII')

_OPS = {'<': operator.lt, '<=': operator.le, '>': operator.gt,
        '>=': operator.ge, '=': operator.eq, '==': operator.eq, '!=': operator.ne}


class RID(NamedTuple):
    bloco: int
    slot: int


class HeapFile:
    def __init__(self, buffer, arquivo):
        self.buffer, self.arquivo = buffer, arquivo
        buffer.fm.open(arquivo, 'data')
        if buffer.fm.nblocks(arquivo) == 0:
            n = buffer.allocate(arquivo)
            page = buffer.pin(arquivo, n)
            try:
                page[:_META.size] = _META.pack(MAGIC, 1, RECORD, SLOTS)
            finally:
                buffer.unpin(arquivo, n, dirty=True)
        else:
            page = buffer.pin(arquivo, 0)
            try:
                magic, _, rec, slots = _META.unpack_from(page, 0)
            finally:
                buffer.unpin(arquivo, 0)
            if magic != MAGIC or rec != RECORD or slots != SLOTS:
                raise CorruptFileError(f'{arquivo}: cabeçalho inválido')

    @property
    def data_blocks(self):
        return self.buffer.fm.nblocks(self.arquivo) - 1

    # --- operações ---------------------------------------------------------
    def insert(self, raw):
        if len(raw) != RECORD:
            raise ValueError('registro deve ter 128 bytes')
        b = self.buffer
        for blk in range(1, b.fm.nblocks(self.arquivo)):
            page = b.pin(self.arquivo, blk)
            try:
                for s in range(SLOTS):
                    if not page[MARKS + s]:
                        self._put(page, s, raw)
                        b.unpin(self.arquivo, blk, dirty=True)
                        return RID(blk, s)
            except BaseException:
                b.unpin(self.arquivo, blk)
                raise
            b.unpin(self.arquivo, blk)
        blk = b.allocate(self.arquivo)
        page = b.pin(self.arquivo, blk)
        self._put(page, 0, raw)
        b.unpin(self.arquivo, blk, dirty=True)
        return RID(blk, 0)

    @staticmethod
    def _put(page, s, raw):
        start = PAGE_HEADER + RECORD * s
        page[start:start + RECORD] = raw
        page[MARKS + s] = 1

    def _check(self, rid):
        if not (1 <= rid.bloco < self.buffer.fm.nblocks(self.arquivo) and 0 <= rid.slot < SLOTS):
            raise IndexError(f'RID fora da faixa: {rid}')

    def get(self, rid):
        self._check(rid)
        page = self.buffer.pin(self.arquivo, rid.bloco)
        try:
            if not page[MARKS + rid.slot]:
                return None
            start = PAGE_HEADER + RECORD * rid.slot
            return bytes(page[start:start + RECORD])
        finally:
            self.buffer.unpin(self.arquivo, rid.bloco)

    def delete(self, rid):
        self._check(rid)
        page = self.buffer.pin(self.arquivo, rid.bloco)
        dirty = False
        try:
            if not page[MARKS + rid.slot]:
                raise KeyError(f'posição livre: {rid}')
            start = PAGE_HEADER + RECORD * rid.slot
            page[start:start + RECORD] = bytes(RECORD)
            page[MARKS + rid.slot] = 0
            dirty = True
        finally:
            self.buffer.unpin(self.arquivo, rid.bloco, dirty=dirty)

    # --- leitura -----------------------------------------------------------
    def scan_pages(self):
        """Entrega a lista de registros de uma página por vez; sem pin entre yields."""
        b = self.buffer
        for blk in range(1, b.fm.nblocks(self.arquivo)):
            page = b.pin(self.arquivo, blk)
            try:
                recs = [bytes(page[PAGE_HEADER + RECORD * s:PAGE_HEADER + RECORD * (s + 1)])
                        for s in range(SLOTS) if page[MARKS + s]]
            finally:
                b.unpin(self.arquivo, blk)
            if recs:
                yield recs

    def scan(self):
        for recs in self.scan_pages():
            yield from recs

    def find_equal(self, campo, valor, acesso='scan'):
        self._only_scan(acesso)
        return (r for r in self.scan() if field(r, campo) == valor)

    def find_range(self, campo, limite, operador, acesso='scan'):
        self._only_scan(acesso)
        op = _OPS[operador]
        return (r for r in self.scan() if op(field(r, campo), limite))

    @staticmethod
    def _only_scan(acesso):
        if acesso != 'scan':
            raise ValueError("o heap só oferece acesso 'scan'")

    def stats(self):
        """Chamar fora da medição: lê todas as páginas pelo buffer."""
        records = sum(len(r) for r in self.scan_pages())
        blocks = self.data_blocks
        return {'data_blocks': blocks, 'records': records,
                'occupancy': records / (blocks * SLOTS) if blocks else 0.0}
