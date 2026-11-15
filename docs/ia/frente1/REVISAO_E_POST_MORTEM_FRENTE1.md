# Frente 1 — arquivos, buffer e heap: revisão do código de IA e post-mortem

Rascunho preparado com a IA. A Giulia deve ler, rodar os comandos abaixo e reescrever com suas palavras o que for sua análise (a autoria é o critério principal do POST_MORTEM).

## Código bruto e evidências

| Item | Arquivo |
|---|---|
| Prompt-chave | `prompt.txt` |
| Código bruto v1 (antes de editar), com SHA-256 | `codigo_bruto_v1/` |
| Testes na v1 / v2 | `testes_v1.txt` / `testes_v2.txt` |
| Estresse na v1 / v2 | `bench_v1.txt` / `bench_v2.txt` |

Reproduzir: `python -m unittest tests.test_buffer_heap -v` e `python tests/bench_frente1.py`.

## Code review do que a IA gerou (v1)

| # | Problema | Tipo | Gravidade | Correção na v2 |
|---|---|---|---|---|
| 1 | `HeapFile.insert` varria o heap desde o bloco 1 a cada inserção para achar posição livre. | Complexidade O(N) por inserção, O(N²) na carga | Alta | Lista encadeada de blocos com vaga (cabeçalho do arquivo + 8 bytes do cabeçalho de página). Inserção custa 1 bloco de dados. |
| 2 | A varredura sequencial de 128 blocos num buffer de 32 páginas com LRU nunca acerta (sequential flooding): taxa de acerto 6,4 % na carga. | Eficiência de E/S | Alta | Consequência do item 1; some quando a inserção deixa de varrer. |
| 3 | `BufferPool.allocate` reservava o bloco (`fm.extend`) antes de garantir espaço no pool. Com pool todo fixado, lançava `BufferFullError` e deixava um bloco fantasma no arquivo. | Estado inconsistente / vazamento de bloco | Média | `_make_room()` antes de `extend()`. Teste de regressão `test_full_pool_failures_leave_no_trace`. |
| 4 | `BufferPool.pin` lia o bloco do disco antes de `_make_room()`. Se o pool estava cheio de fixadas, a leitura física era contada e jogada fora. | Medição errada / E/S desperdiçada | Média | `fm.check()` valida a faixa, depois abre espaço, depois lê. |
| 5 | `BufferPool` e `FileManager` não tinham `with` nem fechamento seguro. Um teste esquecido deixou arquivo aberto e o Windows não apagou a pasta temporária. | Vazamento de descritor de arquivo | Média | `__enter__/__exit__`; `BufferPool.close()` sempre fecha os arquivos (`finally`). |
| 6 | `HeapFile.insert` desfazia o pin num `except BaseException` e também chamava `unpin` no caminho normal dentro do mesmo `try`. Um erro no próprio `unpin` causaria unpin duplo. | Tratamento de erro frágil | Baixa | Um único `unpin` em `finally`, com `dirty` só depois da escrita. |
| 7 | O heap v1 não registrava a lista de blocos livres no disco; ao reabrir não teria como saber onde há vaga sem varrer tudo. | Persistência | Média | A cabeça da lista fica no bloco 0 e é recuperada na abertura (`test_reopen_persistence`). |

Não encontrei acesso fora da faixa nem pin vazado na v1: o `FileManager.check` já validava blocos e os testes mostram `pinned_count() == 0` depois de cada operação. A v1 também estava funcionalmente correta nos 25 testes; o problema era desempenho e robustez, não resultado errado.

Nota sobre a bateria v1: dois testes falharam na primeira execução, mas por defeito do próprio teste (asserção sem contar páginas alocadas no `setUp`, e arquivos abertos no Windows). Corrigi os testes, não o resultado esperado.

## Antes × depois (estresse: carga de 3.968 registros, exclusão de 1.984, reinserção de 1.984, buffer de 32 páginas, semente 42, LRU)

| Fase | Métrica | v1 | v2 |
|---|---|---|---|
| Carga 3.968 | tempo | 2,679 s | 0,013 s |
| | requisições ao buffer | 255.936 | 4.224 |
| | leituras físicas | 239.440 | 0 |
| | gravações físicas | 3.008 | 97 |
| | taxa de acerto | 6,4 % | 100 % |
| Reinserção 1.984 | tempo | 1,555 s | 0,006 s |
| | leituras físicas | 119.491 | 123 |
| | taxa de acerto | 6,5 % | 94,2 % |
| Exclusão 1.984 | leituras físicas | 1.519 | 1.523 |
| Scan frio | leituras físicas | 128 | 128 |

Suíte de testes: 25 testes em 9,86 s (v1) contra 29 testes em 0,42 s (v2).

Resultado esperado e confirmado: o custo do scan frio é igual nas duas versões (128 blocos), então a refatoração não alterou a leitura sequencial. A exclusão não melhorou porque cada exclusão aleatória precisa ler o bloco do registro. As leituras da exclusão vêm do tamanho do heap (128 blocos) contra 32 páginas do pool, não do algoritmo.

## Limitações conhecidas

- LRU e FIFO ficam equivalentes quando o padrão de acesso é sequencial ou cabe no pool. A comparação só vai importar nas junções e nos índices; precisa ser medida na bancada integrada.
- `pin` devolve o `bytearray` do próprio frame. Quem alterar sem passar `dirty=True` perde a escrita em silêncio. O buffer não consegue detectar isso sem copiar a página (o simulado da frente 3 detecta, mas por cópia). É regra do contrato, coberta por teste na camada do heap, não pelo buffer.
- Não há opções de prefetch, toss-immediate nem prioridade de índice (desabilitadas, conforme o plano).
- Não há recuperação de falhas: o arquivo só é consistente depois de `flush`/`close`.
- A lista de livres e o bloco 0 não têm checksum; um cabeçalho corrompido só é detectado pela assinatura e pelos parâmetros.
- O formato do registro é o mesmo codec provisório da frente 3 (três int64 + 104 bytes). Falta alinhar `RID`: aqui é `RID(bloco, slot)` com bloco ≥ 1, o mesmo formato usado em `hash_externo`.

## Decisões físicas desta frente

Bloco de 4.096 B; bloco 0 do heap = cabeçalho (assinatura `HEAPv002`, tamanho do registro 128, posições por bloco 31, cabeça da lista de livres). Bloco de dados = 128 B de cabeçalho (8 B de link + 31 marcas de ocupação) + 31 × 128 B = 4.096 B. 248 registros ocupam 8 blocos e 3.968 ocupam 128 blocos de dados, cheios (ocupação 100 %), como pede o plano. Buffer de 32 páginas, LRU como base e FIFO para comparação. `allocate` entra no pool como página dirty sem leitura nem gravação imediata. Métricas por tipo de arquivo (`data`, `index`, `overflow`) via `FileManager.open(nome, kind)`.

## O que falta para fechar

- Integrar com as frentes 2, 3, 4, 5 e 6 e confirmar que o contrato `pin/unpin/allocate` encaixa (a frente 3 usa um `allocate(arquivo)` externo; aqui é `buffer.allocate(arquivo)`).
- Rodar a bancada integrada (buffer frio, 3 repetições, variação < 5 %) e comparar custo medido × teórico: scan de 8 blocos = 8 leituras; 128 blocos = 128 leituras.
- Escrever com as próprias palavras a análise crítica e o post-mortem para o `POST_MORTEM.md` do grupo.
