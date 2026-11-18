"""Arquivo heap.

Bloco 0: cabeçalho do arquivo (assinatura, tamanho do registro, posições por bloco, cabeça da
lista de blocos com posição livre). Blocos 1..: 128 B de cabeçalho de página + 31 registros de 128 B.
Cabeçalho de página: bytes 0-7 = próximo bloco da lista de livres (uint64, 0 = fim);
bytes 8..38 = marca de ocupação de cada posição.

Invariante: um bloco de dados está na lista de livres se, e somente se, tem posição livre.
"""
import operator
import struct
from typing import NamedTuple

from .files import CorruptFileError
from .registro import RECORD, field

SLOTS = 31
PAGE_HEADER = 128
MARKS = 8
MARKS_END = MARKS + SLOTS
MAGIC = b'HEAPv002'
_META = struct.Struct('<8sIIII')        # assinatura, versão, registro, posições, cabeça livres
_FREE_OFF = _META.size - 4
_LINK = struct.Struct('<Q')
_U32 = struct.Struct('<I')

_OPS = {'<': operator.lt, '<=': operator.le, '>': operator.gt,
        '>=': operator.ge, '=': operator.eq, '==': operator.eq, '!=': operator.ne}


class RID(NamedTuple):
    bloco: int
    slot: int


class HeapFile:
    def __init__(self, buffer, arquivo, kind='data'):
        self.buffer, self.arquivo = buffer, arquivo
        buffer.fm.open(arquivo, kind)
        self.free_head = 0
        if buffer.fm.nblocks(arquivo) == 0:
            n = buffer.allocate(arquivo)
            page = buffer.pin(arquivo, n)
            try:
                _META.pack_into(page, 0, MAGIC, 2, RECORD, SLOTS, 0)
            finally:
                buffer.unpin(arquivo, n, dirty=True)
        else:
            page = buffer.pin(arquivo, 0)
            try:
                magic, _, rec, slots, self.free_head = _META.unpack_from(page, 0)
            finally:
                buffer.unpin(arquivo, 0)
            if magic != MAGIC or rec != RECORD or slots != SLOTS:
                raise CorruptFileError(f'{arquivo}: cabeçalho inválido')

    @property
    def data_blocks(self):
        return self.buffer.fm.nblocks(self.arquivo) - 1

    def _set_free_head(self, blk):
        self.free_head = blk
        page = self.buffer.pin(self.arquivo, 0)
        try:
            _U32.pack_into(page, _FREE_OFF, blk)
        finally:
            self.buffer.unpin(self.arquivo, 0, dirty=True)

    # --- operações ---------------------------------------------------------
    def insert(self, raw):
        """Custo: 1 bloco de dados (mais o bloco 0 só quando a lista de livres muda)."""
        if len(raw) != RECORD:
            raise ValueError('registro deve ter 128 bytes')
        b = self.buffer
        blk = self.free_head
        new = blk == 0
        if new:
            blk = b.allocate(self.arquivo)
        page = b.pin(self.arquivo, blk)
        dirty = False
        try:
            slot = page.find(0, MARKS, MARKS_END) - MARKS
            if slot < 0:
                raise CorruptFileError(f'{self.arquivo}: bloco {blk} na lista de livres está cheio')
            start = PAGE_HEADER + RECORD * slot
            page[start:start + RECORD] = raw
            page[MARKS + slot] = 1
            dirty = True
            full = page.find(0, MARKS, MARKS_END) < 0
            next_free = _LINK.unpack_from(page, 0)[0]
        finally:
            b.unpin(self.arquivo, blk, dirty=dirty)
        if new and not full:
            self._set_free_head(blk)         # página nova entra na lista (link já é 0)
        elif not new and full:
            self._set_free_head(next_free)   # página encheu: sai da lista
        return RID(blk, slot)

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
        b = self.buffer
        page = b.pin(self.arquivo, rid.bloco)
        dirty = was_full = False
        try:
            if not page[MARKS + rid.slot]:
                raise KeyError(f'posição livre: {rid}')
            was_full = page.find(0, MARKS, MARKS_END) < 0
            start = PAGE_HEADER + RECORD * rid.slot
            page[start:start + RECORD] = bytes(RECORD)
            page[MARKS + rid.slot] = 0
            if was_full:
                _LINK.pack_into(page, 0, self.free_head)
            dirty = True
        finally:
            b.unpin(self.arquivo, rid.bloco, dirty=dirty)
        if was_full:
            self._set_free_head(rid.bloco)   # bloco voltou a ter posição livre

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
