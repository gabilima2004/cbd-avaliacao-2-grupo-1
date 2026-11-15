# Frente 3 — revisão e evidências de IA

Prompt integral em `docs/ia/prompt.txt`. Código bruto preservado em `docs/ia/core_v1.py` e `init_v1.py` antes da primeira execução e de qualquer refatoração. SHA-256 de `core_v1.py`: `01E8B3A4417A750C44F2F916380298AE97C733F491D20F0A7801B7D1D674F017`. A implementação atual é idêntica à versão inicial; não houve refatoração nem defeito funcional observado nesta bateria. Não se apresenta melhoria fictícia sobre uma versão deliberadamente defeituosa.

A revisão conferiu: busca e remoção percorrem a cadeia completa; unicidade considera overflow; slots removidos são reutilizados; leituras e escritas passam pelo buffer; `finally` libera pins; dirty acompanha escrita; um gerador suspenso não retém pin; layout cabe exatamente em 4096 bytes; configuração é armazenada no cabeçalho e recuperada na reabertura.

Os testes preservados em `tests/test_hash.py` cobrem, em ordem: chave única com overflow e ausente no mesmo bucket; valores repetidos em três blocos e exclusão em overflow; índice secundário com heap simulado; reabertura lógica/negativos/scan/faixa; estresse de 3.968 registros e 234 exclusões comparado ao SQLite; entradas inválidas e liberação de página durante iteração parcial. O simulado rejeita dirty omitido, pin duplo e unpin sem pin e a bateria exige no máximo uma página fixada simultaneamente.

A primeira execução passou os seis testes, mas o PowerShell tratou o stderr normal do unittest como `NativeCommandError` no pipeline com Tee-Object. A evidência original está em `docs/ia/testes_primeira_execucao.txt`. Isso foi um problema de captura, não falha do hash: o runner `tests/run.py` reúne stdout/stderr via subprocess, salva UTF-8 e propaga o código real. A execução final está em `docs/ia/testes_final.txt`, com `EXIT_CODE=0`. Não existe evidência antes/depois de correção funcional porque nenhum defeito funcional foi encontrado; estes testes não provam ausência de defeitos.

Limitações revisadas: o formato do registro e o adaptador de alocação são provisórios; índice secundário depende de manutenção explícita; páginas vazias não são desalocadas; módulo simples sofre com skew; cadeias longas aumentam custo de inserção (inclusive no hash não único). O simulado não verifica persistência real, substituição LRU/FIFO ou contadores de disco. As validações de integração estão listadas no README. A autoria e a análise final devem ser completadas pelo integrante após executar a bancada real e compreender os testes.

# Frente 4 — índice B+ secundário e seleções

Revisão assistida e evidências em `docs/ia/frente4/REVISAO.md`; prompt em
`prompt.txt`, código inicial exato em `codigo_bruto_v1/`, com SHA-256. A frente
começou na `main` limpa `336c0d8`, passou os 35 testes existentes e foi desenvolvida
na branch `frente-4-bmais-selecoes`, reaproveitando arquivos, buffer, heap, hash,
codec e runner do grupo. Os módulos das outras frentes não foram modificados.

A revisão verificou uma chave por valor nas folhas, listas de RIDs paginadas,
folhas encadeadas, separadores mínimos, splits até a raiz, redistribuição/fusão,
redução da raiz, reuso de páginas e leitura/escrita exclusivamente pelo buffer.
Os testes conferem dirty, ausência de pins entre yields e persistência real com
expulsão de páginas, LRU/FIFO e buffer de três páginas.

A primeira suíte teve três falhas de teste: classes RID distintas com os mesmos
campos e uma expectativa incorreta de ocupação igual após mudar a ordem de
inserção. As saídas foram preservadas em `testes_v1.txt`; as correções não alteram
formatos de outras frentes. A revisão encontrou também um defeito real da API:
`None` era interpretado como faixa sem limite e devolvia todos os registros.
O caso preservado em `casos_falhos/limite_none.py` falhou antes da correção e passou
depois; as duas saídas estão em `falha_limite_none_antes.txt` e
`falha_limite_none_depois.txt`. A API agora exige limites int64. Não foi fabricada
uma falha funcional nem atribuído ganho de desempenho a essa validação.

Em `testes_v2.txt`, os 51 testes passam. O estresse cobre 763 RIDs numa chave,
árvores profundas com capacidades reduzidas, 11 mil chaves com fan-out físico 248,
3.968 registros com 1.984 exclusões/reinserções e 5 mil operações intercaladas.
A B+ secundária é a implementação real em todos esses casos. Os testes básicos
usam buffer/heap simulados estritos; persistência, estresse e seleções sobre
heap/hash usam os módulos reais. A B+ primária ausente foi simulada apenas para
conferir o contrato, sem produzir medidas de desempenho.

A bancada da frente, executada após a correção, está em `bench_v1.csv` e
`bench_primeira_execucao.txt` (v1 identifica a primeira execução da bancada).
São 84 cenários, 252 medições, semente 42, 248/3.968 registros, buffer frio de
32 páginas, LRU/FIFO e três repetições. Leituras físicas variaram 0%, consultas não
escreveram páginas e não deixaram pins. A suíte compara tuplas completas com o
SQLite; a bancada verifica contagem/checksum em fluxo e registra custos calculados.
No heap grande/LRU, faixa_cr < 1 retornou 29 alunos: scan 128 leituras, B+ 29.
Para faixa_cr < 50, retornou 1.926: scan 128, B+ 1.606, mostrando o custo de
revisitar páginas espalhadas. Um índice não é automaticamente a melhor escolha.

O enunciado exige clustering secundário em B+/hash; esse requisito prevalece
sobre o adiamento do plano e continua obrigatório na integração. Listas de RIDs
não certificam clustering físico. Ainda faltam a B+ primária real, esse arranjo
físico, alinhamento de `get(RID)` e manutenção conjunta dos índices pela camada
de tabela/catálogo. `HashComRID` oferece a adaptação local sem alterar a frente 3.
Não há transações ou recuperação de falhas; slots reutilizados com a mesma chave
não são detectados como RID obsoleto. Páginas de RIDs de grupos pequenos têm baixa
ocupação. Os testes evidenciam os casos executados e não provam ausência de defeitos.
