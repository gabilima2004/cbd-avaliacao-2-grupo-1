# Frente 1 — arquivos em blocos, buffer pool e heap

Python 3, somente biblioteca padrão. Código em `buffer_heap/`; testes em `tests/test_buffer_heap.py` e `tests/test_integracao_hash_buffer.py`; estresse em `tests/bench_frente1.py`; evidências de IA em `docs/ia/frente1/`.

```powershell
python -m unittest tests.test_buffer_heap tests.test_integracao_hash_buffer -v
python tests/bench_frente1.py
```

## Uso

```python
from buffer_heap import BufferPool, FileManager, HeapFile, registro

with BufferPool(FileManager('dados'), capacity=32, policy='LRU') as buffer:   # ou 'FIFO'
    heap = HeapFile(buffer, 'aluno.heap')           # abre o arquivo (kind 'data') e cria o bloco 0
    rid = heap.insert(registro.encode(1, 123456789, 50))
    heap.get(rid)                                    # bytes ou None se a posição está livre
    heap.delete(rid)
    for registros in heap.scan_pages(): ...          # uma página por vez, sem pin entre yields
    list(heap.find_equal(1, 123456789, 'scan'))      # só 'scan' no heap
    list(heap.find_range(2, 50, '<', 'scan'))
```

Para arquivos de outras frentes: `buffer.fm.open('indice.hash', 'index')` antes de criar a estrutura, e `buffer.allocate` como adaptador de alocação (é o que `HashPrimario(buffer, arquivo, buffer.allocate, ...)` espera). O tipo do arquivo (`'data'`, `'index'` ou `'overflow'`) só serve para separar as métricas.

## Contrato do buffer

- `buffer.pin(arquivo, bloco)` devolve o `bytearray` de 4096 bytes da própria página. Pode ser repetido; cada `pin` exige um `unpin`.
- `buffer.unpin(arquivo, bloco, dirty=False)`: passe `dirty=True` se alterou os bytes. **Alteração sem `dirty=True` se perde em silêncio**; o buffer não detecta isso.
- `buffer.allocate(arquivo)` reserva um bloco zerado no fim do arquivo e devolve o número. A página entra no pool como dirty, sem leitura. Se todas as páginas do pool estão fixadas, lança `BufferFullError` e não reserva nada.
- `buffer.flush(arquivo=None)` grava as páginas dirty. `buffer.drop_all()` grava e esvazia o pool (buffer frio, para iniciar uma medição). `buffer.close()` falha se houver página fixada e sempre fecha os arquivos.
- `unpin` sem `pin`, bloco fora da faixa e arquivo com tamanho que não é múltiplo de 4096 geram `PinError`, `IndexError` e `CorruptFileError`.

## Métricas

`buffer.stats()` devolve requisições, acertos, faltas, taxa de acerto, leituras e gravações físicas, gravações por expulsão (`writes_evict`) e por flush (`writes_flush`), expulsões, e leituras e gravações físicas por tipo de arquivo (`reads_by_kind`, `writes_by_kind`). `buffer.reset_stats()` zera tudo sem esvaziar o pool.

Para uma medição: carregue a base, chame `buffer.drop_all()` e `buffer.reset_stats()`, execute a consulta e leia `stats()`. `heap.stats()` e `HashPrimario.stats()` leem páginas pelo buffer, então devem ser chamadas fora da medição.

## Decisões físicas

| Item | Escolha |
|---|---|
| Bloco | 4.096 B; o bloco n começa em n × 4.096; novos blocos no fim do arquivo |
| Bloco 0 do heap | assinatura `HEAPv002`, versão, tamanho do registro (128), posições por bloco (31), cabeça da lista de blocos com vaga |
| Bloco de dados | 128 B de cabeçalho de página (8 B de link para a lista de livres + 31 marcas de ocupação) + 31 × 128 B de registros = 4.096 B |
| Registro | 128 B: três int64 little-endian (`aluno_id, dre, faixa_cr` ou `inscricao_id, aluno_id, turma_id`) + 104 B de preenchimento |
| RID | `RID(bloco, slot)`, bloco ≥ 1 |
| Tamanho das bases | 248 registros = 8 blocos de dados; 3.968 = 128 blocos de dados (100 % de ocupação) |
| Posições liberadas | lista encadeada de blocos com vaga; inserção custa 1 bloco de dados; invariante: um bloco está na lista se, e só se, tem posição livre |
| Buffer pool | 32 páginas de 4 KiB, uma página por bloco; LRU como base e FIFO para comparação |
| Política LRU | cada acerto leva a página ao fim da fila; a vítima é a primeira página sem pin |
| Política FIFO | acerto não muda a ordem; a vítima é a primeira página sem pin na ordem de entrada |
| Escrita | só na expulsão de página dirty ou em `flush`/`close`; página limpa nunca é gravada |
| Desabilitado | prefetch, toss-immediate e prioridade para índice/catálogo |

## Custos esperados (para comparar com o medido)

- Varredura fria do heap: um bloco de dados por leitura, ou seja 8 leituras na base pequena e 128 na grande (confirmado em teste e no benchmark).
- Inserção, exclusão e `get`: 1 bloco de dados, mais o bloco 0 só quando a lista de livres muda (um bloco de dados que enche ou volta a ter vaga).
- Hash primário (frente 3) em busca fria: comprimento da cadeia do bucket (2 leituras com 1 overflow). Faixa: bloco 0 + todos os buckets e overflows.
- Índice secundário sobre o heap: 1 leitura de índice (mais overflow, se houver) + 1 de dados por registro encontrado.

## Limitações

- Sem recuperação de falhas, transações, concorrência ou checksum; o arquivo só é consistente depois de `flush`/`close`.
- O heap só oferece o acesso `'scan'`; `primary` e `secondary` pertencem às outras frentes.
- Em acesso sequencial ou quando tudo cabe no pool, LRU e FIFO dão o mesmo resultado; a diferença só aparece com reuso de páginas (índices, junções).
- Exclusões não devolvem blocos ao sistema operacional; blocos vazios continuam na lista de livres e são reutilizados.
- Registro e `RID` seguem o codec provisório da frente 3 (três int64 + preenchimento). Mudar o formato exige mudar `registro.py` e as constantes do heap.
