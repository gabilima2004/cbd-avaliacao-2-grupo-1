"""Contrato e medição comuns aos algoritmos de junção da bancada."""

from dataclasses import asdict, dataclass, field
import csv
import hashlib
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping, Protocol

from partition_hash_join import PartitionHashJoin, RelationInput


MASK128 = (1 << 128) - 1


@dataclass(frozen=True)
class JoinRequest:
    """Entrada padronizada para qualquer algoritmo de junção."""

    relation_r: object
    relation_s: object
    key_r: int | Callable[[bytes], object | None]
    key_s: int | Callable[[bytes], object | None]
    buffer: object
    memory_pages: int
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JoinExecution:
    """Iterador de saída e acesso tardio às métricas específicas."""

    pairs: Iterable[tuple[bytes, bytes]]
    specific_metrics: Callable[[], Mapping[str, Any]]


class JoinAdapter(Protocol):
    """Interface que Block Nested Loop e Sort-Merge também podem implementar."""

    name: str

    def execute(self, request: JoinRequest) -> JoinExecution:
        ...


class PartitionHashJoinAdapter:
    name = 'partition_hash_join'

    def execute(self, request: JoinRequest) -> JoinExecution:
        allowed = {'temp_prefix', 'max_depth'}
        unknown = set(request.options) - allowed
        if unknown:
            raise ValueError(f'opções desconhecidas do PHJ: {sorted(unknown)}')
        operator = PartitionHashJoin(
            request.buffer,
            request.memory_pages,
            temp_prefix=request.options.get('temp_prefix', 'phj'),
            max_depth=request.options.get('max_depth', 3),
        )
        left = RelationInput.from_source(request.relation_r, request.key_r)
        right = RelationInput.from_source(request.relation_s, request.key_s)

        def metrics():
            stats = operator.stats
            return {
                'partitions_created': stats.partitions_created,
                'repartition_passes': stats.repartition_passes,
                'fallback_partitions': stats.fallback_partitions,
                'max_depth': stats.max_depth,
                'build_records': stats.build_records,
                'max_build_records': stats.max_build_records,
                'probe_records': stats.probe_records,
                'null_records': stats.null_records,
                'operator_output_pairs': stats.output_pairs,
                'temp_reads': stats.temp_reads,
                'temp_writes': stats.temp_writes,
            }

        return JoinExecution(operator.join(left, right), metrics)


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    r_size: int
    s_size: int
    buffer_pages: int
    memory_pages: int
    replacement_policy: str
    seed: int
    repetition: int
    r_data_pages: int = 0
    s_data_pages: int = 0
    available_access_r: str = 'heap_scan'
    available_access_s: str = 'heap_scan'
    effective_access_r: str = 'scan'
    effective_access_s: str = 'scan'


@dataclass
class ExperimentResult:
    experiment_id: str
    algorithm: str
    r_size: int
    s_size: int
    r_data_pages: int
    s_data_pages: int
    buffer_pages: int
    memory_pages: int
    replacement_policy: str
    seed: int
    repetition: int
    elapsed_seconds: float
    physical_reads: int
    physical_writes: int
    cache_hits: int
    cache_misses: int
    buffer_requests: int
    hit_rate: float
    evictions: int
    theoretical_base_io: int
    measured_io: int
    io_difference: int
    io_ratio: float
    result_count: int
    result_checksum: str
    validation_status: str
    available_access_r: str
    available_access_s: str
    effective_access_r: str
    effective_access_s: str
    partitions_created: int = 0
    repartition_passes: int = 0
    fallback_partitions: int = 0
    max_depth: int = 0
    build_records: int = 0
    max_build_records: int = 0
    probe_records: int = 0
    null_records: int = 0
    temp_reads: int = 0
    temp_writes: int = 0
    output_policy: str = 'count_and_order_independent_checksum'
    output_materialized: bool = False
    pending_writes_flushed: bool = True
    os_fsync: bool = False
    error_message: str = ''

    def as_row(self):
        return asdict(self)


def pair_token(left: bytes, right: bytes) -> int:
    digest = hashlib.blake2b(left + right, digest_size=16).digest()
    return int.from_bytes(digest, 'little')


def summarize_pairs(pairs):
    """Consome toda a saída e calcula um resumo independente de ordem."""
    count = 0
    checksum = 0
    for left, right in pairs:
        count += 1
        checksum = (checksum + pair_token(left, right)) & MASK128
    return count, f'{checksum:032x}'


def measure_join(adapter: JoinAdapter, request: JoinRequest, spec: ExperimentSpec,
                 expected_summary: tuple[int, str]) -> ExperimentResult:
    """Mede uma execução com entradas persistidas e buffer inicialmente frio.

    A carga das relações, a referência esperada e a escrita do CSV ficam fora
    desta função. O tempo inclui criação do operador, consumo completo da saída,
    cleanup do algoritmo e flush das escritas ainda pendentes no Buffer Pool.
    """
    buffer = request.buffer
    buffer.drop_all()
    buffer.reset_stats()
    count = 0
    checksum = '0' * 32
    specific = {}
    error = ''
    started = perf_counter()
    try:
        execution = adapter.execute(request)
        try:
            count, checksum = summarize_pairs(execution.pairs)
        finally:
            close = getattr(execution.pairs, 'close', None)
            if close is not None:
                close()
        specific = dict(execution.specific_metrics())
        buffer.flush()
    except Exception as exc:  # a bancada registra a falha na linha do experimento
        error = f'{type(exc).__name__}: {exc}'
    elapsed = perf_counter() - started
    general = buffer.stats()

    expected_count, expected_checksum = expected_summary
    if error:
        validation = 'error'
    elif (count, checksum) != (expected_count, expected_checksum):
        validation = 'mismatch'
    elif specific.get('operator_output_pairs', count) != count:
        validation = 'mismatch'
    else:
        validation = 'ok'

    r_pages = spec.r_data_pages or getattr(request.relation_r, 'data_blocks', 0)
    s_pages = spec.s_data_pages or getattr(request.relation_s, 'data_blocks', 0)
    theoretical = 3 * (r_pages + s_pages)
    measured = general['physical_reads'] + general['physical_writes']

    return ExperimentResult(
        experiment_id=spec.experiment_id,
        algorithm=adapter.name,
        r_size=spec.r_size,
        s_size=spec.s_size,
        r_data_pages=r_pages,
        s_data_pages=s_pages,
        buffer_pages=spec.buffer_pages,
        memory_pages=spec.memory_pages,
        replacement_policy=spec.replacement_policy,
        seed=spec.seed,
        repetition=spec.repetition,
        elapsed_seconds=elapsed,
        physical_reads=general['physical_reads'],
        physical_writes=general['physical_writes'],
        cache_hits=general['hits'],
        cache_misses=general['misses'],
        buffer_requests=general['requests'],
        hit_rate=general['hit_rate'],
        evictions=general['evictions'],
        theoretical_base_io=theoretical,
        measured_io=measured,
        io_difference=measured - theoretical,
        io_ratio=measured / theoretical if theoretical else 0.0,
        result_count=count,
        result_checksum=checksum,
        validation_status=validation,
        available_access_r=spec.available_access_r,
        available_access_s=spec.available_access_s,
        effective_access_r=spec.effective_access_r,
        effective_access_s=spec.effective_access_s,
        partitions_created=specific.get('partitions_created', 0),
        repartition_passes=specific.get('repartition_passes', 0),
        fallback_partitions=specific.get('fallback_partitions', 0),
        max_depth=specific.get('max_depth', 0),
        build_records=specific.get('build_records', 0),
        max_build_records=specific.get('max_build_records', 0),
        probe_records=specific.get('probe_records', 0),
        null_records=specific.get('null_records', 0),
        temp_reads=specific.get('temp_reads', 0),
        temp_writes=specific.get('temp_writes', 0),
        error_message=error,
    )


def write_csv(path, results):
    rows = [result.as_row() for result in results]
    if not rows:
        raise ValueError('não há resultados para exportar')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
