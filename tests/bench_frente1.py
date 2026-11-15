"""Estresse da frente 1: carga + exclusões + novas inserções no heap, buffer de 32 páginas.

Uso: python tests/bench_frente1.py [saida.txt]
"""
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from buffer_heap import BufferPool, FileManager, HeapFile, registro  # noqa: E402

N = 3968


def workload(policy):
    rnd = random.Random(42)
    with tempfile.TemporaryDirectory() as d:
        pool = BufferPool(FileManager(d), 32, policy)
        heap = HeapFile(pool, 'heap')
        out = {}

        def phase(name, fn):
            pool.reset_stats()
            t = time.perf_counter()
            fn()
            dt = time.perf_counter() - t
            s = pool.stats()
            out[name] = (dt, s['requests'], s['physical_reads'], s['physical_writes'], s['hit_rate'])

        rids = []
        phase('carga 3968', lambda: rids.extend(
            heap.insert(registro.encode(i, i, rnd.randrange(100))) for i in range(N)))
        victims = rnd.sample(rids, N // 2)
        phase('exclusao 1984', lambda: [heap.delete(r) for r in victims])
        phase('reinsercao 1984', lambda: [heap.insert(registro.encode(i, i, 0)) for i in range(N // 2)])
        pool.drop_all()
        phase('scan frio', lambda: sum(len(x) for x in heap.scan_pages()))
        pool.close()
        return out


def main():
    lines = [f'{"fase":<18}{"politica":<7}{"tempo(s)":>10}{"requisicoes":>13}{"leit.fis":>10}{"grav.fis":>10}{"hit%":>7}']
    for policy in ('LRU', 'FIFO'):
        for name, (dt, req, r, w, hr) in workload(policy).items():
            lines.append(f'{name:<18}{policy:<7}{dt:>10.3f}{req:>13}{r:>10}{w:>10}{hr * 100:>7.1f}')
    text = '\n'.join(lines)
    print(text)
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')


if __name__ == '__main__':
    main()
