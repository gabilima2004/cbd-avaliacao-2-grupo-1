# Experimento LRU × FIFO no heap (Frente 1)

Reproduzir: `python tests/bench_lru_fifo.py docs/ia/frente1/lru_vs_fifo.csv`. Dados completos em `lru_vs_fifo.csv` e `lru_vs_fifo.txt`.

## Método

- Heap de 3.968 registros (128 blocos de dados), os mesmos dados em todas as execuções.
- Um fator varia por vez: a política (LRU ou FIFO) e, separadamente, o tamanho do pool (16, 32 e 64 páginas). O pool base do projeto é 32.
- Buffer frio e contadores zerados antes de cada carga. LRU e FIFO recebem a mesma sequência de acessos (mesma semente).
- Três repetições com sementes 42, 43 e 44. Repetir com a mesma semente dá contadores idênticos (variação 0 %, verificada por `assert`). A coluna `var_entre_sementes_pct` mostra apenas quanto muda a carga aleatória quando o sorteio muda; não é ruído de medição, e vai até 13 % nas cargas aleatórias.
- Métrica principal: leituras físicas (nenhuma carga grava, o heap não é alterado).

## Resultados (leituras físicas, média de 3 sementes)

| Carga | Acessos | Pool | LRU | FIFO | Diferença |
|---|---|---|---|---|---|
| Varredura cíclica (3 × 128 blocos) | 384 | 16 / 32 / 64 | 384 | 384 | 0 |
| Laço de 24 blocos × 20 | 480 | 16 | 480 | 480 | 0 |
| | | 32 / 64 | 24 | 24 | 0 |
| 5.000 leituras por RID, blocos uniformes | 5.000 | 16 | 4.372,7 | 4.368,3 | ≈ 0 |
| | | 32 | 3.759,7 | 3.756,7 | ≈ 0 |
| | | 64 | 2.527,3 | 2.521,3 | ≈ 0 |
| 5.000 leituras, 80 % dos acessos em 25 blocos | 5.000 | 16 | 3.034,3 | 3.157,3 | FIFO +4 % |
| | | 32 | 1.541,0 | 1.995,0 | FIFO +29 % |
| | | 64 | 686,7 | 980,7 | FIFO +43 % |
| Varredura com 8 páginas quentes consultadas a cada página | 1.152 | 16 | 128 | 232 | FIFO +81 % |
| | | 32 | 128 | 160 | FIFO +25 % |
| | | 64 | 128 | 144 | FIFO +13 % |

## Leitura dos resultados

- **Empate quando não há reuso útil.** Na varredura cíclica de 128 blocos, nenhum pool testado (até 64) segura o conjunto inteiro, então os dois dão 0 acertos e 384 leituras. O mesmo vale para o laço de 24 blocos: com pool de 16 os dois erram sempre; com 32 ou 64 ambos leem os 24 blocos uma vez só e acertam o resto. Se o conjunto cabe, a política não importa; se não cabe num acesso cíclico, nenhuma das duas ajuda.
- **Acesso uniforme também empata**, porque todos os blocos têm a mesma chance de reuso. As diferenças (3 a 6 leituras em milhares) estão dentro da variação entre sorteios e não indicam vantagem de nenhuma política.
- **Com localidade, o LRU ganha.** Quando uma parte dos blocos é muito mais acessada, o LRU mantém esses blocos porque cada acerto os rejuvenesce, e o FIFO os expulsa depois de uma certa quantidade de entradas novas, mesmo estando em uso. A diferença cresce com o pool nessa carga (FIFO 29 % pior com 32 páginas, 43 % pior com 64), e o LRU foi melhor ou igual em todas as três sementes.
- **O caso mais próximo da bancada é a varredura com páginas reutilizadas** (por exemplo, nested-loop que varre uma tabela e consulta o índice ou o catálogo a cada bloco). O LRU lê só os 128 blocos da varredura, e o FIFO relê as páginas quentes a cada ciclo de expulsão (+25 % com o pool de 32). Com pool maior, o FIFO se aproxima do LRU (+13 % com 64).
- **Mais páginas ajudam muito mais do que trocar a política** nas cargas aleatórias: de 16 para 64 páginas, as leituras da carga uniforme caem 42 % com qualquer política.

## Limites do experimento

- É feito com o heap e acessos sintéticos. Ainda não mede as junções nem os índices: essas leituras vêm da bancada integrada (frentes 4 a 6) e podem se comportar diferente.
- A carga 80/20 e a de páginas quentes foram escolhidas para exercitar localidade; seus percentuais não valem como previsão geral, só mostram a direção do efeito.
- O experimento não testa prefetch nem prioridade de índice no buffer; estão desabilitados por regra do plano.
- A diferença de 3 a 6 leituras na carga uniforme é ruído de sorteio, e não deve ser citada como ganho de FIFO.

## Para a conclusão do relatório

Recomendação com base nestes dados: manter LRU como política-base (empata quando não há reuso e ganha quando há), e usar FIFO só como referência para mostrar o custo de não reconhecer reuso. Confirmar na bancada integrada com junções antes de afirmar o mesmo para os cinco cenários.
