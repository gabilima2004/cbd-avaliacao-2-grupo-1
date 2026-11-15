"""Bancada apenas de seleções da frente 4; não substitui a bancada da frente 6.

python tests/bench_frente4.py [--saida caminho.csv]
Dados/índices são carregados fora da medição. Três repetições com buffer frio.
Todos os arquivos usam os módulos REAIS do grupo. B+ primária ausente: excluída.
"""
import argparse
import csv
import hashlib
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buffer_heap import BufferPool, FileManager, HeapFile, registro
from hash_externo import HashPrimario, HashSecundario
from indice_bmais import BMaisSecundario
from selecoes import HashComRID, TabelaSelecao


def fingerprint(rows):
    count, checksum = 0, 0
    for raw in rows:
        count += 1
        checksum = (checksum + int.from_bytes(hashlib.blake2b(raw, digest_size=16).digest(),
                                              'little')) % (1 << 128)
    return count, checksum


def bplus_cost(index, limit, hash_base):
    """Diagnóstico fora da medição: L folhas, P listas, D dados distintos, R RIDs."""
    block, leaves, posts, records, data = index.first_leaf, 0, 0, 0, set()
    stop = False
    while block and not stop:
        node = index._read(block)
        leaves += 1
        for entry in node.entries:
            if entry.key >= limit:
                stop = True
                break
            records += entry.count
            for rid in index._rids(entry):
                data.add(rid.bloco)
            posting = entry.head
            while posting:
                posts += 1
                posting = index._read(posting).next
        block = node.next
    lower = leaves + posts + len(data) + int(hash_base and records > 0)
    upper = leaves + posts + records * (2 if hash_base else 1)
    formula = 'L+P+D <= custo <= L+P+R'
    if hash_base:
        formula = 'L+P+D+cabecalho_hash <= custo <= L+P+2R'
    return lower, upper, formula


def run_config(n, policy, organization):
    rng = random.Random(42)
    rows = [(i, 100000 + i, rng.randrange(100)) for i in range(n)]
    with tempfile.TemporaryDirectory() as directory:
        with BufferPool(FileManager(directory), 32, policy) as pool:
            is_hash = organization != 'heap'
            if is_hash:
                pool.fm.open('dados', 'data')
                base = HashPrimario(pool, 'dados', pool.allocate, buckets=n // 31,
                                    campo=1 if organization == 'hash_unique' else 2,
                                    unique=organization == 'hash_unique')
                table_base = HashComRID(base)
            else:
                base = table_base = HeapFile(pool, 'dados')
            bplus = BMaisSecundario(pool, 'bplus', table_base, campo=2)
            secondaries = {2: bplus}
            hash_index = None
            if not is_hash:
                pool.fm.open('hash_unique', 'index')
                hash_index = HashPrimario(pool, 'hash_unique', pool.allocate,
                                         buckets=n // 31, campo=0, unique=True)
                secondaries[1] = HashSecundario(hash_index, base)
            for row in rows:
                rid = base.insert(registro.encode(*row))
                bplus.insert(row[2], rid)
                if hash_index is not None:
                    secondaries[1].insert(row[1], rid)
            table = TabelaSelecao(table_base, secondaries)
            audit = bplus.validate(check_records=True)
            data_blocks = pool.fm.nblocks('dados')
            index_blocks = pool.fm.nblocks('bplus')
            hash_blocks = pool.fm.nblocks('hash_unique') if hash_index else 0
            db = sqlite3.connect(':memory:')
            try:
                db.execute('create table aluno(aluno_id, dre, faixa_cr)')
                db.executemany('insert into aluno values(?,?,?)', rows)
                point = 100000 + n // 2
                cases = [('dre_igual', 1, point, '=', 'scan')]
                if organization == 'heap':
                    cases.append(('dre_igual', 1, point, '=', 'secondary'))
                elif organization == 'hash_unique':
                    cases.append(('dre_igual', 1, point, '=', 'primary'))
                for limit in (1, 50):
                    for access in (('scan', 'primary', 'secondary') if is_hash
                                   else ('scan', 'secondary')):
                        cases.append((f'faixa_cr_lt_{limit}', 2, limit, '<', access))
                output = []
                for name, field, limit, op, access in cases:
                    column = ('aluno_id', 'dre', 'faixa_cr')[field]
                    expected = fingerprint(registro.encode(*r) for r in db.execute(
                        f'select * from aluno where {column} {op} ?', (limit,)))
                    if access == 'secondary' and field == 2:
                        lower, upper, formula = bplus_cost(bplus, limit, is_hash)
                    elif access == 'secondary':
                        chain = sum(1 for _ in hash_index._chain(limit))
                        lower = upper = chain + 1
                        formula = 'cadeia_hash_indice + 1 bloco_heap'
                    elif access == 'primary' and op == '=':
                        lower = upper = sum(1 for _ in base._chain(limit))
                        formula = 'cadeia_bucket_hash'
                    else:
                        lower = upper = data_blocks if is_hash else data_blocks - 1
                        formula = 'todos_blocos_hash_com_cabecalho' if is_hash else 'blocos_heap_sem_cabecalho'
                    repetitions = []
                    for repetition in range(1, 4):
                        pool.drop_all()
                        pool.reset_stats()
                        start = time.perf_counter()
                        iterator = (table.find_equal(field, limit, access) if op == '=' else
                                    table.find_range(field, limit, op, access))
                        actual = fingerprint(iterator)
                        elapsed = time.perf_counter() - start
                        metrics = pool.stats()  # captura ANTES de verificações/diagnósticos
                        if actual != expected:
                            raise AssertionError(f'divergência SQLite: {organization}/{name}/{access}')
                        if pool.pinned_count() or len(pool.frames) > 32:
                            raise AssertionError('contrato de memória/pin violado')
                        if not lower <= metrics['physical_reads'] <= upper:
                            raise AssertionError('custo medido fora dos limites calculados')
                        if metrics['physical_writes'] != 0:
                            raise AssertionError('seleção somente leitura gerou escrita')
                        row = dict(
                            n=n, politica=policy, organizacao=organization, consulta=name,
                            acesso_solicitado=access, acesso_efetivo=table.last_access,
                            repeticao=repetition, resultado=actual[0],
                            seletividade_pct=round(100 * actual[0] / n, 4),
                            blocos_dados_com_cabecalho=data_blocks, blocos_bplus=index_blocks,
                            blocos_hash_secundario=hash_blocks, altura_bplus=audit['height'],
                            custo_min_leituras=lower, custo_max_leituras=upper, formula=formula,
                            leituras_fisicas=metrics['physical_reads'],
                            gravacoes_fisicas=metrics['physical_writes'],
                            requisicoes=metrics['requests'], acertos=metrics['hits'],
                            faltas=metrics['misses'], taxa_acerto=metrics['hit_rate'],
                            gravacoes_expulsao=metrics['writes_evict'],
                            gravacoes_flush=metrics['writes_flush'],
                            leituras_dados=metrics['reads_by_kind'].get('data', 0),
                            leituras_indices=metrics['reads_by_kind'].get('index', 0),
                            tempo_s=round(elapsed, 6))
                        repetitions.append(row)
                    reads = [r['leituras_fisicas'] for r in repetitions]
                    mean = statistics.mean(reads)
                    variation = 100 * (max(reads) - min(reads)) / mean if mean else 0
                    if variation >= 5:
                        raise AssertionError('variação de leituras físicas >=5%')
                    for row in repetitions:
                        row['variacao_leituras_pct'] = variation
                    output.extend(repetitions)
                return output
            finally:
                db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saida', type=Path,
                        default=Path('docs/ia/frente4/bench_ultima_execucao.csv'))
    args = parser.parse_args()
    output = []
    for n in (248, 3968):
        for policy in ('LRU', 'FIFO'):
            for organization in ('heap', 'hash_unique', 'hash_not_unique'):
                output.extend(run_config(n, policy, organization))
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    with args.saida.open('w', newline='', encoding='utf-8') as target:
        writer = csv.DictWriter(target, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    lines = ['FRENTE 4: modulos REAIS; semente=42; buffer=32; frio; 3 repeticoes',
             'B+ primaria: NAO MEDIDA (modulo ausente); clustering fisico: NAO CERTIFICADO',
             'Variacao <5% se refere a leituras fisicas; tempo e apenas informativo.',
             'N politica organizacao consulta solicitado->efetivo resultado leituras estimativa[min,max]']
    for row in output[::3]:
        lines.append(
            f'{row["n"]} {row["politica"]} {row["organizacao"]} {row["consulta"]} '
            f'{row["acesso_solicitado"]}->{row["acesso_efetivo"]} {row["resultado"]} '
            f'{row["leituras_fisicas"]} [{row["custo_min_leituras"]},{row["custo_max_leituras"]}]')
    lines.extend([f'CENARIOS={len(output) // 3}; MEDICOES={len(output)}; SQLITE=OK; '
                  'VARIACAO_LEITURAS<5%=OK; PINS=0; ESCRITAS_CONSULTAS=0',
                  'EXIT_CODE=0'])
    text = '\n'.join(lines) + '\n'
    args.saida.with_suffix('.txt').write_text(text, encoding='utf-8')
    print(text, end='')


if __name__ == '__main__':
    main()
