"""Partition Hash Join com temporários gerenciados pelo BufferPool.

O orçamento é expresso em páginas lógicas. Duas páginas ficam reservadas nas
fases de build/probe; portanto, no máximo ``B - 2`` páginas de registros são
mantidas na tabela hash. O overhead dos objetos Python não é incluído nesse
modelo acadêmico de memória.
"""

from dataclasses import dataclass
import hashlib
import math
import os
from typing import Callable, Iterable, Iterator, Sequence
import uuid

from buffer_heap import BLOCK, SLOTS, HeapFile, registro


KeyFunction = Callable[[bytes], object | None]


def field_key(campo: int) -> KeyFunction:
    """Cria um extrator para um dos três campos do registro físico atual."""
    if campo not in range(3):
        raise ValueError('campo deve estar entre 0 e 2')
    return lambda raw: registro.field(raw, campo)


@dataclass(frozen=True)
class RelationInput:
    """Fonte reescaneável usada pelo operador, independente da organização."""

    scan_pages: Callable[[], Iterable[Sequence[bytes]]]
    key: KeyFunction
    page_count: int
    name: str = 'relation'
    buffer: object | None = None

    @classmethod
    def from_source(cls, source, key: int | KeyFunction, *, name=None,
                    page_count=None):
        if not callable(getattr(source, 'scan_pages', None)):
            raise TypeError('a relação deve oferecer scan_pages()')
        key_fn = field_key(key) if isinstance(key, int) else key
        if not callable(key_fn):
            raise TypeError('chave deve ser um campo ou função')
        if page_count is None:
            page_count = getattr(source, 'data_blocks', None)
        if page_count is None and hasattr(source, 'buffer') and hasattr(source, 'arquivo'):
            page_count = source.buffer.fm.nblocks(source.arquivo) - 1
        if not isinstance(page_count, int) or page_count < 0:
            raise ValueError('page_count deve ser informado e não negativo')
        return cls(source.scan_pages, key_fn, page_count,
                   name or getattr(source, 'arquivo', 'relation'),
                   getattr(source, 'buffer', None))


@dataclass
class JoinStats:
    partitions_created: int = 0
    repartition_passes: int = 0
    fallback_partitions: int = 0
    max_depth: int = 0
    build_records: int = 0
    probe_records: int = 0
    output_pairs: int = 0
    null_records: int = 0
    physical_reads: int = 0
    physical_writes: int = 0
    temp_reads: int = 0
    temp_writes: int = 0


@dataclass
class _Partition:
    workspace: '_Workspace'
    side: str
    depth: int
    number: int
    heap: HeapFile | None = None
    records: int = 0

    @property
    def pages(self):
        return math.ceil(self.records / SLOTS)

    def insert(self, raw):
        if self.heap is None:
            self.heap = self.workspace.create(self.side, self.depth, self.number)
        self.heap.insert(raw)
        self.records += 1

    def scan_pages(self):
        if self.heap is None:
            return iter(())
        return self.heap.scan_pages()

    def materialize(self):
        if self.heap is not None:
            self.workspace.buffer.flush(self.heap.arquivo)
            self.workspace.buffer.evict_file(self.heap.arquivo, flush=False)

    def delete(self):
        if self.heap is not None:
            self.workspace.delete(self.heap.arquivo)
            self.heap = None


class _Workspace:
    def __init__(self, buffer, prefix, stats):
        if not prefix or os.path.basename(prefix) != prefix:
            raise ValueError('temp_prefix deve ser um nome simples')
        self.buffer = buffer
        self.prefix = f'{prefix}_{uuid.uuid4().hex}'
        self.stats = stats
        self.files = set()

    def create(self, side, depth, number):
        name = f'{self.prefix}_{side}_{depth}_{number}.heap'
        if name in self.files:
            raise RuntimeError('nome temporário duplicado')
        heap = HeapFile(self.buffer, name, kind='temp')
        self.files.add(name)
        self.stats.partitions_created += 1
        return heap

    def delete(self, name):
        if name in self.files:
            self.buffer.remove_file(name)
            self.files.remove(name)

    def close(self):
        error = None
        for name in tuple(self.files):
            try:
                self.delete(name)
            except Exception as exc:  # tenta limpar todos antes de propagar
                error = error or exc
        if error is not None:
            raise error


class PartitionHashJoin:
    """INNER equijoin externo; ``None`` nunca corresponde a ``None``."""

    def __init__(self, buffer, memory_pages=None, temp_prefix='phj', *,
                 max_depth=3):
        self.buffer = buffer
        self.memory_pages = buffer.capacity if memory_pages is None else memory_pages
        if not isinstance(self.memory_pages, int) or self.memory_pages < 3:
            raise ValueError('memory_pages deve ser um inteiro >= 3')
        if self.memory_pages > buffer.capacity:
            raise ValueError('memory_pages excede a capacidade do BufferPool')
        if not isinstance(max_depth, int) or max_depth < 0:
            raise ValueError('max_depth deve ser não negativo')
        self.temp_prefix = temp_prefix
        self.max_depth_limit = max_depth
        self.stats = JoinStats()
        self._workspace = None
        self._active = False

    @property
    def build_pages(self):
        return self.memory_pages - 2

    @property
    def build_records(self):
        return self.build_pages * SLOTS

    def join(self, left: RelationInput, right: RelationInput):
        """Retorna iterador de pares ``(left_raw, right_raw)``.

        As fontes devem ser reescaneáveis. Chaves ``None`` são descartadas do
        predicado, reproduzindo ``INNER JOIN ... ON left.key = right.key``.
        """
        self._validate_relation(left)
        self._validate_relation(right)

        def execute():
            before = self.buffer.stats()
            workspace = None
            if self._active:
                raise RuntimeError('o operador já possui uma execução ativa')
            self._active = True
            self.stats = JoinStats()
            try:
                workspace = _Workspace(self.buffer, self.temp_prefix, self.stats)
                self._workspace = workspace
                smaller = min(left.page_count, right.page_count)
                count = max(1, math.ceil(smaller / self.build_pages))
                count = min(self.memory_pages - 1, count)
                lparts, rparts = self._partition_pair(left, right, count, 0)
                for lp, rp in zip(lparts, rparts):
                    yield from self._process_pair(lp, rp, left.key, right.key, 0)
            finally:
                try:
                    if workspace is not None:
                        workspace.close()
                finally:
                    after = self.buffer.stats()
                    self._finish_stats(before, after)
                    self._workspace = None
                    self._active = False

        return execute()

    def _validate_relation(self, relation):
        if not isinstance(relation, RelationInput):
            raise TypeError('use RelationInput.from_source para descrever a relação')
        if relation.buffer is not None and relation.buffer is not self.buffer:
            raise ValueError('as relações devem usar o BufferPool fornecido')
        if self.buffer.pinned_count() > self.buffer.capacity - self.memory_pages:
            raise ValueError('não há páginas livres suficientes para o orçamento solicitado')

    def _partition_pair(self, left, right, count, depth):
        lparts = [_Partition(self._workspace, 'L', depth, i) for i in range(count)]
        rparts = [_Partition(self._workspace, 'R', depth, i) for i in range(count)]
        self._partition(left, lparts, depth)
        self._partition(right, rparts, depth)
        for part in lparts + rparts:
            part.materialize()
        return lparts, rparts

    def _partition(self, relation, parts, depth):
        for page in relation.scan_pages():
            for raw in page:
                raw = bytes(raw)
                key = relation.key(raw)
                if key is None:
                    self.stats.null_records += 1
                    continue
                number = self._hash(key, depth) % len(parts)
                parts[number].insert(raw)

    def _process_pair(self, left, right, left_key, right_key, depth):
        if not left.records or not right.records:
            left.delete()
            right.delete()
            return
        self.stats.max_depth = max(self.stats.max_depth, depth)
        build = self._smaller(left, right)
        if build.pages <= self.build_pages:
            try:
                yield from self._build_probe(left, right, left_key, right_key)
            finally:
                left.delete()
                right.delete()
            return

        if depth < self.max_depth_limit:
            count = max(2, math.ceil(build.pages / self.build_pages))
            count = min(self.memory_pages - 1, count)
            lrel = RelationInput(left.scan_pages, left_key, left.pages, 'left-partition', self.buffer)
            rrel = RelationInput(right.scan_pages, right_key, right.pages, 'right-partition', self.buffer)
            children_l, children_r = self._partition_pair(lrel, rrel, count, depth + 1)
            build_children = children_l if build is left else children_r
            progress = max((p.records for p in build_children), default=0) < build.records
            if progress:
                self.stats.repartition_passes += 1
                left.delete()
                right.delete()
                for lp, rp in zip(children_l, children_r):
                    yield from self._process_pair(lp, rp, left_key, right_key, depth + 1)
                return
            for part in children_l + children_r:
                part.delete()

        try:
            self.stats.fallback_partitions += 1
            yield from self._chunked_probe(left, right, left_key, right_key)
        finally:
            left.delete()
            right.delete()

    def _build_probe(self, left, right, left_key, right_key):
        build_left = self._smaller(left, right) is left
        build, probe = (left, right) if build_left else (right, left)
        build_key, probe_key = (left_key, right_key) if build_left else (right_key, left_key)
        table = {}
        for page in build.scan_pages():
            for raw in page:
                key = build_key(raw)
                table.setdefault(key, []).append(raw)
                self.stats.build_records += 1
        for page in probe.scan_pages():
            for raw in page:
                self.stats.probe_records += 1
                key = probe_key(raw)
                for match in table.get(key, ()):
                    self.stats.output_pairs += 1
                    yield (match, raw) if build_left else (raw, match)

    def _chunked_probe(self, left, right, left_key, right_key):
        build_left = self._smaller(left, right) is left
        build, probe = (left, right) if build_left else (right, left)
        build_key, probe_key = (left_key, right_key) if build_left else (right_key, left_key)
        for chunk in self._chunks(build, self.build_records):
            table = {}
            for raw in chunk:
                table.setdefault(build_key(raw), []).append(raw)
                self.stats.build_records += 1
            for page in probe.scan_pages():
                for raw in page:
                    self.stats.probe_records += 1
                    for match in table.get(probe_key(raw), ()):
                        self.stats.output_pairs += 1
                        yield (match, raw) if build_left else (raw, match)

    @staticmethod
    def _chunks(partition, limit):
        chunk = []
        for page in partition.scan_pages():
            for raw in page:
                chunk.append(raw)
                if len(chunk) == limit:
                    yield chunk
                    chunk = []
        if chunk:
            yield chunk

    @staticmethod
    def _smaller(left, right):
        """Compara páginas e usa registros para desempatar a última página."""
        return left if (left.pages, left.records) <= (right.pages, right.records) else right

    @staticmethod
    def _key_bytes(key):
        if isinstance(key, int):
            size = max(1, (key.bit_length() + 8) // 8)
            return b'i' + key.to_bytes(size, 'little', signed=True)
        if isinstance(key, bytes):
            return b'b' + key
        if isinstance(key, str):
            return b's' + key.encode('utf-8')
        raise TypeError('chaves devem ser int, bytes, str ou None')

    @classmethod
    def _hash(cls, key, depth):
        seed = depth.to_bytes(8, 'little', signed=False)
        digest = hashlib.blake2b(cls._key_bytes(key), digest_size=8,
                                 key=seed).digest()
        return int.from_bytes(digest, 'little')

    def _finish_stats(self, before, after):
        self.stats.physical_reads = after['physical_reads'] - before['physical_reads']
        self.stats.physical_writes = after['physical_writes'] - before['physical_writes']
        self.stats.temp_reads = (after['reads_by_kind'].get('temp', 0)
                                 - before['reads_by_kind'].get('temp', 0))
        self.stats.temp_writes = (after['writes_by_kind'].get('temp', 0)
                                  - before['writes_by_kind'].get('temp', 0))


def partition_hash_join(left, right, left_key, right_key, *, buffer,
                        memory_pages=None, temp_prefix='phj', max_depth=3):
    """Atalho para construir o operador e iniciar um join.

    Retorna ``(iterator, operator)`` para que as métricas possam ser consultadas
    em ``operator.stats`` após consumir ou fechar o iterador.
    """
    operator = PartitionHashJoin(buffer, memory_pages, temp_prefix,
                                 max_depth=max_depth)
    left_input = RelationInput.from_source(left, left_key)
    right_input = RelationInput.from_source(right, right_key)
    return operator.join(left_input, right_input), operator
