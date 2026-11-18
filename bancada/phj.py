"""Matriz inicial Heap File + Partition Hash Join."""

import argparse
from dataclasses import dataclass
import random
import tempfile

from buffer_heap import BufferPool, FileManager, HeapFile, registro

from .core import (MASK128, ExperimentSpec, JoinRequest,
                   PartitionHashJoinAdapter, measure_join, pair_token, write_csv)


SCENARIOS = ('small-small', 'small-large', 'large-large')
POLICIES = ('LRU', 'FIFO')


@dataclass(frozen=True)
class MasterDetailData:
    master_rows: tuple[tuple[int, int, int], ...]
    detail_rows: tuple[tuple[int, int, int], ...]
    expected_summary: tuple[int, str]


def generate_master_detail(r_size, s_size, seed):
    """Gera Aluno e Inscrição; toda FK referencia exatamente um aluno."""
    if r_size < 1 or s_size < 0:
        raise ValueError('r_size deve ser positivo e s_size não negativo')
    rng = random.Random(seed)
    masters = tuple((i, 100_000_000 + i, rng.randrange(100))
                    for i in range(r_size))
    details = tuple((i, rng.randrange(r_size), rng.randrange(1, 65))
                    for i in range(s_size))
    encoded_master = {row[0]: registro.encode(*row) for row in masters}
    checksum = 0
    for row in details:
        checksum = (checksum + pair_token(
            encoded_master[row[1]], registro.encode(*row))) & MASK128
    return MasterDetailData(
        masters, details, (len(details), f'{checksum:032x}'))


def scenario_sizes(scenario, small_size, large_size):
    if scenario == 'small-small':
        return small_size, small_size
    if scenario == 'small-large':
        return small_size, large_size
    if scenario == 'large-large':
        return large_size, large_size
    raise ValueError(f'cenário desconhecido: {scenario}')


def run_initial_experiments(output, *, small_size=248, large_size=3968,
                            buffer_pages=32, memory_pages=16, seed=42,
                            repetitions=3, policies=POLICIES,
                            scenarios=SCENARIOS, max_depth=3, verbose=True):
    """Executa a matriz inicial e grava um CSV; geração nunca é cronometrada."""
    if repetitions < 1:
        raise ValueError('repetitions deve ser positivo')
    datasets = {}
    for scenario in scenarios:
        r_size, s_size = scenario_sizes(scenario, small_size, large_size)
        datasets[scenario] = generate_master_detail(r_size, s_size, seed)

    adapter = PartitionHashJoinAdapter()
    results = []
    for scenario in scenarios:
        data = datasets[scenario]
        r_size, s_size = scenario_sizes(scenario, small_size, large_size)
        for policy in policies:
            with tempfile.TemporaryDirectory(prefix='cbd_phj_') as directory:
                pool = BufferPool(FileManager(directory), buffer_pages, policy)
                relation_r = HeapFile(pool, 'aluno.heap')
                relation_s = HeapFile(pool, 'inscricao.heap')
                for row in data.master_rows:
                    relation_r.insert(registro.encode(*row))
                for row in data.detail_rows:
                    relation_s.insert(registro.encode(*row))
                pool.flush()

                request = JoinRequest(
                    relation_r=relation_r,
                    relation_s=relation_s,
                    key_r=0,
                    key_s=1,
                    buffer=pool,
                    memory_pages=memory_pages,
                    options={'max_depth': max_depth},
                )
                for repetition in range(1, repetitions + 1):
                    experiment_id = (
                        f'phj-{scenario}-{policy.lower()}-seed{seed}-rep{repetition}')
                    spec = ExperimentSpec(
                        experiment_id=experiment_id,
                        r_size=r_size,
                        s_size=s_size,
                        buffer_pages=buffer_pages,
                        memory_pages=memory_pages,
                        replacement_policy=policy,
                        seed=seed,
                        repetition=repetition,
                        r_data_pages=relation_r.data_blocks,
                        s_data_pages=relation_s.data_blocks,
                        available_access_r='heap_scan',
                        available_access_s='heap_scan',
                        effective_access_r='scan',
                        effective_access_s='scan',
                    )
                    result = measure_join(
                        adapter, request, spec, data.expected_summary)
                    results.append(result)
                    if verbose:
                        print(
                            f'{experiment_id}: {result.validation_status}; '
                            f'{result.elapsed_seconds:.6f}s; '
                            f'R={result.physical_reads}; W={result.physical_writes}; '
                            f'pares={result.result_count}')
                pool.close()
    write_csv(output, results)
    return results


def build_parser():
    parser = argparse.ArgumentParser(
        description='Executa a matriz inicial Heap File + Partition Hash Join.')
    parser.add_argument('--output', default='results/partition_hash_join_initial.csv')
    parser.add_argument('--small-size', type=int, default=248)
    parser.add_argument('--large-size', type=int, default=3968)
    parser.add_argument('--buffer-pages', type=int, default=32)
    parser.add_argument('--memory-pages', type=int, default=16)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--max-depth', type=int, default=3)
    parser.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS,
                        default=list(SCENARIOS))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    results = run_initial_experiments(
        args.output,
        small_size=args.small_size,
        large_size=args.large_size,
        buffer_pages=args.buffer_pages,
        memory_pages=args.memory_pages,
        seed=args.seed,
        repetitions=args.repetitions,
        policies=tuple(args.policies),
        scenarios=tuple(args.scenarios),
        max_depth=args.max_depth,
    )
    failures = [r for r in results if r.validation_status != 'ok']
    print(f'CSV: {args.output}; execuções={len(results)}; falhas={len(failures)}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
