"""Buffer pool com LRU/FIFO, pin/unpin, dirty e contadores de E/S."""
from collections import OrderedDict

from .files import BLOCK


class BufferFullError(Exception):
    """Todas as páginas do pool estão fixadas."""


class PinError(Exception):
    pass


class _Frame:
    __slots__ = ('data', 'pins', 'dirty')

    def __init__(self, data):
        self.data = data
        self.pins = 0
        self.dirty = False


class BufferPool:
    def __init__(self, fm, capacity=32, policy='LRU'):
        if policy not in ('LRU', 'FIFO'):
            raise ValueError('policy deve ser LRU ou FIFO')
        if capacity < 1:
            raise ValueError('capacity deve ser >= 1')
        self.fm = fm
        self.capacity = capacity
        self.policy = policy
        self.frames = OrderedDict()  # (arquivo, bloco) -> _Frame; início = próxima vítima
        self.reset_stats()

    def reset_stats(self):
        self.requests = self.hits = self.misses = 0
        self.writes_evict = self.writes_flush = 0
        self.evictions = 0
        self.fm.reads = {k: 0 for k in self.fm.reads}
        self.fm.writes = {k: 0 for k in self.fm.writes}

    # --- interface do contrato -------------------------------------------
    def pin(self, arquivo, bloco):
        """Fixa a página e devolve o bytearray de 4096 bytes (mutável)."""
        self.requests += 1
        key = (arquivo, bloco)
        f = self.frames.get(key)
        if f is not None:
            self.hits += 1
            if self.policy == 'LRU':
                self.frames.move_to_end(key)
        else:
            self.misses += 1
            self.fm.check(arquivo, bloco)  # valida a faixa antes de expulsar ou ler
            self._make_room()
            f = _Frame(bytearray(self.fm.read_block(arquivo, bloco)))
            self.frames[key] = f
        f.pins += 1
        return f.data

    def unpin(self, arquivo, bloco, dirty=False):
        f = self.frames.get((arquivo, bloco))
        if f is None or f.pins == 0:
            raise PinError(f'unpin sem pin: {arquivo}:{bloco}')
        f.pins -= 1
        if dirty:
            f.dirty = True

    def allocate(self, arquivo):
        """Aloca um bloco zerado no fim do arquivo; fica no pool como dirty, sem leitura."""
        self._make_room()  # antes de reservar: se falhar, não sobra bloco fantasma
        n = self.fm.extend(arquivo)
        f = _Frame(bytearray(BLOCK))
        f.dirty = True
        self.frames[(arquivo, n)] = f
        return n

    def flush(self, arquivo=None):
        for (a, b), f in self.frames.items():
            if f.dirty and (arquivo is None or a == arquivo):
                self.fm.write_block(a, b, bytes(f.data))
                f.dirty = False
                self.writes_flush += 1

    # --- internos ----------------------------------------------------------
    def _make_room(self):
        while len(self.frames) >= self.capacity:
            victim = None
            for key, f in self.frames.items():
                if f.pins == 0:
                    victim = key
                    break
            if victim is None:
                raise BufferFullError('todas as páginas estão fixadas')
            f = self.frames.pop(victim)
            if f.dirty:
                self.fm.write_block(victim[0], victim[1], bytes(f.data))
                self.writes_evict += 1
            self.evictions += 1

    # --- utilidades da bancada --------------------------------------------
    def pinned_count(self):
        return sum(1 for f in self.frames.values() if f.pins)

    def drop_all(self):
        """Grava e esvazia o pool (buffer frio). Falha se houver página fixada."""
        if self.pinned_count():
            raise PinError('há páginas fixadas')
        self.flush()
        self.frames.clear()

    def stats(self):
        reads = {}
        writes = {}
        for name, kind in self.fm.kind.items():
            reads[kind] = reads.get(kind, 0) + self.fm.reads[name]
            writes[kind] = writes.get(kind, 0) + self.fm.writes[name]
        return {
            'requests': self.requests, 'hits': self.hits, 'misses': self.misses,
            'hit_rate': self.hits / self.requests if self.requests else 0.0,
            'physical_reads': sum(reads.values()), 'physical_writes': sum(writes.values()),
            'writes_evict': self.writes_evict, 'writes_flush': self.writes_flush,
            'evictions': self.evictions,
            'reads_by_kind': reads, 'writes_by_kind': writes,
        }

    def close(self):
        try:
            if self.pinned_count():
                raise PinError('há páginas fixadas ao fechar')
            self.flush()
            self.frames.clear()
        finally:
            self.fm.close()  # sempre libera os arquivos

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
