"""Páginas obtidas exclusivamente por pin/unpin. Sem cache de registros."""
from contextlib import contextmanager
from dataclasses import dataclass
import operator
import struct

BLOCK = 4096
SIZE = 128
SLOTS = 31
HEADER = 128
META = struct.Struct('<8sIIII')
LINK = struct.Struct('<Q')
FIELDS = struct.Struct('<qqq')
MAGIC = b'HASHv001'


@dataclass(frozen=True)
class Registro:
    """Três inteiros signed de 64 bits e preenchimento até 128 bytes."""
    a: int
    b: int
    c: int

    def encode(self):
        return FIELDS.pack(self.a, self.b, self.c).ljust(SIZE, b'\0')

    @classmethod
    def decode(cls, raw):
        return cls(*FIELDS.unpack_from(raw))


@dataclass(frozen=True)
class RID:
    bloco: int
    slot: int


class HashPrimario:
    """allocate(arquivo) reserva bloco zerado no fim e retorna seu número.

    Esse adaptador do gerente de arquivos deve ser fornecido pelo integrador.
    pin devolve bytearray/memoryview mutável de 4096 bytes. Uso monothread,
    sem recuperação de falhas ou transações. campo é índice 0, 1 ou 2.
    """

    def __init__(self, buffer, arquivo, allocate, *, buckets=None, campo=0,
                 unique=True):
        self.buffer, self.arquivo, self.allocate = buffer, arquivo, allocate
        self.last_access = None
        if buckets is not None:
            if not isinstance(buckets, int) or buckets < 1 or campo not in range(3):
                raise ValueError('buckets positivo e campo entre 0 e 2')
            if allocate(arquivo) != 0:
                raise ValueError('criação exige arquivo vazio')
            self.buckets, self.campo, self.unique = buckets, campo, unique
            for expected in range(1, buckets + 1):
                if allocate(arquivo) != expected:
                    raise ValueError('alocação deve ser contígua')
            with self._page(0, True) as p:
                META.pack_into(p, 0, MAGIC, buckets, campo, int(unique), buckets + 1)
        else:
            with self._page(0) as p:
                magic, self.buckets, self.campo, flag, total = META.unpack_from(p)
            if magic != MAGIC or self.buckets < 1 or self.campo > 2 or flag > 1:
                raise ValueError('cabeçalho hash inválido')
            self.unique = bool(flag)

    @contextmanager
    def _page(self, block, dirty=False):
        p = self.buffer.pin(self.arquivo, block)
        try:
            if len(p) != BLOCK:
                raise ValueError('página deve ter 4096 bytes')
            yield p
        finally:
            self.buffer.unpin(self.arquivo, block, dirty=dirty)

    def _key(self, raw):
        return FIELDS.unpack_from(raw)[self.campo]

    def bucket(self, key):
        # Inteiros: módulo estável entre processos, inclusive para negativos.
        return 1 + key % self.buckets

    def _read(self, block):
        with self._page(block) as p:
            nxt = LINK.unpack_from(p)[0]
            rows = [(s, bytes(p[HEADER+s*SIZE:HEADER+(s+1)*SIZE]))
                    for s in range(SLOTS) if p[8+s]]
        return nxt, rows

    def _chain(self, key):
        block = self.bucket(key)
        while block:
            nxt, rows = self._read(block)
            yield block, rows
            block = nxt

    def find(self, key):
        for _, rows in self._chain(key):
            for _, raw in rows:
                if self._key(raw) == key:
                    yield raw

    def insert(self, raw):
        raw = bytes(raw)
        if len(raw) != SIZE:
            raise ValueError('registro deve ter 128 bytes')
        key = self._key(raw)
        free = None
        last = None
        for block, rows in self._chain(key):
            last = block
            if self.unique and any(self._key(r) == key for _, r in rows):
                raise ValueError('chave duplicada')
            if free is None and len(rows) < SLOTS:
                used = {s for s, _ in rows}
                free = (block, next(s for s in range(SLOTS) if s not in used))
        if free is None:
            new = self.allocate(self.arquivo)
            with self._page(0, True) as p:
                total = struct.unpack_from('<I', p, 20)[0]
                if new != total:
                    raise ValueError('alocação deve ser contígua')
                struct.pack_into('<I', p, 20, total + 1)
            with self._page(last, True) as p:
                LINK.pack_into(p, 0, new)
            free = (new, 0)
        block, slot = free
        with self._page(block, True) as p:
            start = HEADER + slot * SIZE
            p[start:start+SIZE] = raw
            p[8+slot] = 1
        return RID(block, slot)

    def delete(self, key, raw=None):
        """Remove uma ocorrência exata, ou a primeira da chave. Mantém cadeia."""
        for block, rows in self._chain(key):
            for slot, row in rows:
                if self._key(row) == key and (raw is None or row == raw):
                    with self._page(block, True) as p:
                        p[8+slot] = 0
                    return True
        return False

    def scan_pages(self):
        # Ordem física: inclui cada bucket/overflow exatamente uma vez.
        with self._page(0) as p:
            total = struct.unpack_from('<I', p, 20)[0]
        for block in range(1, total):
            _, rows = self._read(block)
            yield [raw for _, raw in rows]

    def find_equal(self, campo, valor, acesso='primary'):
        if acesso not in ('scan', 'primary'):
            raise ValueError('acesso indisponível')
        if campo not in range(3):
            raise ValueError('campo inválido')
        if acesso == 'primary' and campo == self.campo:
            self.last_access = 'primary'
            yield from self.find(valor)
        else:
            self.last_access = 'scan'
            for page in self.scan_pages():
                for raw in page:
                    if FIELDS.unpack_from(raw)[campo] == valor:
                        yield raw

    def find_range(self, campo, limite, operador, acesso='primary'):
        ops = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge}
        if campo not in range(3) or operador not in ops or acesso not in ('primary', 'scan'):
            raise ValueError('consulta inválida')
        self.last_access = 'scan'
        for page in self.scan_pages():
            for raw in page:
                if ops[operador](FIELDS.unpack_from(raw)[campo], limite):
                    yield raw

    def stats(self):
        with self._page(0) as p:
            total = struct.unpack_from('<I', p, 20)[0]
        count = sum(len(page) for page in self.scan_pages())
        return dict(header_blocks=1, bucket_blocks=self.buckets,
                    overflow_blocks=total-1-self.buckets, total_blocks=total,
                    records=count, load_factor=count/(SLOTS*self.buckets),
                    occupancy=count/(SLOTS*(total-1)))


class HashSecundario:
    """Entradas fixas (chave, bloco heap, slot heap). Manutenção explícita.

    heap.get(RID) deve devolver bytes ou None, usando seu próprio buffer.
    Campo e unicidade pertencem ao catálogo do heap. Sem cópia dos dados.
    """

    def __init__(self, indice, heap):
        if not indice.unique or indice.campo != 0:
            raise ValueError('índice requer hash único no campo 0')
        self.indice, self.heap = indice, heap

    def insert(self, key, rid):
        if rid.bloco < 1 or not 0 <= rid.slot < SLOTS:
            raise ValueError('RID inválido')
        self.indice.insert(Registro(key, rid.bloco, rid.slot).encode())

    def find_rid(self, key):
        for raw in self.indice.find(key):
            entry = Registro.decode(raw)
            yield RID(entry.b, entry.c)

    def find(self, key):
        for rid in self.find_rid(key):
            raw = self.heap.get(rid)
            if raw is not None:
                yield raw

    def delete(self, key):
        return self.indice.delete(key)
