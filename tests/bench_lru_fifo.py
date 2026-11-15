"""Comparação LRU x FIFO no heap de 3.968 registros (128 blocos de dados).

Mesmos dados em todas as execuções; um fator por vez (política; tamanho do pool).
Buffer frio e contadores zerados antes de cada carga; 3 repetições (sementes 42, 43, 44).
Uso: python tests/bench_lru_fifo.py [saida.csv]
"""
import csv
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from buffer_heap import BufferPool, FileManager, HeapFile, RID, SLOTS, registro  # noqa: E402

N = 3968
BLOCKS = N // SLOTS          # 128
SEEDS = (42, 43, 44)
POOLS = (16, 32, 64)


def rid_in(block, rnd):
    return RID(block, rnd.randrange(SLOTS))


def w_scan_ciclico(heap, rnd):                      # 3 varreduras completas seguidas
    for _ in range(3):
        for _ in heap.scan_pages():
            pass


def w_loop_pequeno(heap, rnd):                      # 24 blocos repetidos 20 vezes (cabe em 32)
    for _ in range(20):
        for b in range(1, 25):
            heap.get(RID(b, 0))


def w_get_uniforme(heap, rnd):                      # 5.000 leituras por RID, blocos uniformes
    for _ in range(5000):
        heap.get(rid_in(rnd.randrange(1, BLOCKS + 1), rnd))


def w_get_80_20(heap, rnd):                         # 80 % dos acessos em 20 % dos blocos (25)
    for _ in range(5000):
        b = rnd.randrange(1, 26) if rnd.random() < 0.8 else rnd.randrange(26, BLOCKS + 1)
        heap.get(rid_in(b, rnd))


def w_scan_com_quentes(heap, rnd):                  # junção típica: varre uma tabela e consulta 8 páginas quentes
    hot = [RID(b, 0) for b in range(1, 9)]
    for b in range(1, BLOCKS + 1):
        for r in hot:
            heap.get(r)
        heap.get(RID(b, 0))


WORKLOADS = [('scan_ciclico', w_scan_ciclico), ('loop_24_blocos', w_loop_pequeno),
             ('get_uniforme', w_get_uniforme), ('get_80_20', w_get_80_20),
             ('scan_com_8_quentes', w_scan_com_quentes)]


def run(policy, capacity, name, fn, seed):
    with tempfile.TemporaryDirectory() as d:
        with BufferPool(FileManager(d), capacity, policy) as pool:
            heap = HeapFile(pool, 'heap')
            for i in range(N):
                heap.insert(registro.encode(i, i, i % 100))
            assert heap.data_blocks == BLOCKS
            pool.drop_all()
            pool.reset_stats()
            fn(heap, random.Random(seed))
            s = pool.stats()
            assert pool.pinned_count() == 0
            return s['requests'], s['hits'], s['physical_reads']


def main():
    rows = []
    for name, fn in WORKLOADS:
        for capacity in POOLS:
            res = {}
            for policy in ('LRU', 'FIFO'):
                runs = [run(policy, capacity, name, fn, s) for s in SEEDS]
                # repetição com a mesma semente tem de dar contadores idênticos (simulação determinística)
                assert run(policy, capacity, name, fn, SEEDS[0]) == runs[0], 'repeticao diferente'
                res[policy] = runs
            for policy in ('LRU', 'FIFO'):
                runs, other = res[policy], res['FIFO' if policy == 'LRU' else 'LRU']
                reads = [r[2] for r in runs]
                mean = sum(reads) / len(reads)
                spread = (max(reads) - min(reads)) / mean * 100 if mean else 0.0
                req = runs[0][0]
                hits = sum(r[1] for r in runs) / len(runs)
                fifo_r = [r[2] for r in res['FIFO']]
                lru_r = [r[2] for r in res['LRU']]
                rows.append(dict(
                    carga=name, pool=capacity, politica=policy, requisicoes=req,
                    taxa_acerto=round(hits / req * 100, 2), leituras_fisicas=round(mean, 1),
                    var_repeticao_pct=0.0, var_entre_sementes_pct=round(spread, 2),
                    lru_melhor_ou_igual_em_todas_as_sementes=all(a <= b for a, b in zip(lru_r, fifo_r))))
    head = list(rows[0])
    print(' '.join(f'{h[:18]:>18}' for h in head))
    for r in rows:
        print(' '.join(f'{str(r[h]):>18}' for h in head))
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, head)
            w.writeheader()
            w.writerows(rows)
    print('\nrepeticao com a mesma semente: contadores identicos em todas as celulas (variacao 0%)')
    print('var_entre_sementes_pct mede a sensibilidade a sorteios diferentes da carga, nao ruido de medicao')


if __name__ == '__main__':
    main()
