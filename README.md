# Frente 3 — hash externo

Implementação em Python 3.11, somente biblioteca padrão. Fontes: enunciado da Avaliação 2 (seções 3.5, 4.2 e 7) e plano do grupo. O repositório recebido continha somente esses dois documentos, sem interfaces implementadas nem AGENTS.md.

Na raiz, execute:

```powershell
python tests/run.py
```

Confira seis testes `ok`, `Ran 6 tests`, `OK` e `EXIT_CODE=0`. As linhas UNIQUE, REPETIDA, SECUNDARIO e ESTRESSE descrevem as verificações, feitas com asserts. A saída é salva em `docs/ia/testes_ultima_execucao.txt`. Não é necessário instalar dependências. `pypdf` foi usado apenas para ler o enunciado, não faz parte da implementação.

## Uso e integração

```python
from hash_externo import HashPrimario, Registro, HashSecundario

h = HashPrimario(buffer, arquivo, allocate, buckets=4, campo=0, unique=True)
h.insert(Registro(1, 123456789, 50).encode())
registros = list(h.find_equal(0, 1, 'primary'))
# Abrir existente: omitir buckets; configuração é recuperada do bloco 0.
h = HashPrimario(buffer, arquivo, allocate)
```

`buffer.pin(arquivo, bloco)` deve devolver bytes mutáveis de 4096 bytes e `buffer.unpin(arquivo, bloco, dirty=False)` deve liberar a página. Não há I/O direto no hash. O adaptador `allocate(arquivo)` deve reservar um bloco zerado no fim e retornar seu número; criação exige arquivo vazio. A alocação é responsabilidade do gerente de arquivos, ainda ausente. Arquivos de dados e índice devem ter identificadores distintos.

`Registro` é um codec provisório para três inteiros signed de 64 bits, little-endian, mais 104 bytes de preenchimento. Os campos 0/1/2 representam `aluno_id/dre/faixa_cr` ou `inscricao_id/aluno_id/turma_id`. As consultas devolvem iteradores de registros em `bytes`; `scan_pages()` entrega uma lista por página, sem manter páginas fixadas entre yields. `campo` usa posição, a tradução de nomes deve vir do catálogo.

`HashSecundario(HashPrimario(..., campo=0, unique=True), heap)` armazena entradas de 128 bytes `(chave, bloco, slot)`; `insert(chave, RID)`, `find_rid(chave)`, `find(chave)` e `delete(chave)` são explícitos. `heap.get(RID)` deve recuperar o registro pelo buffer. Exclusão do índice preserva o heap. Inserções, alterações e exclusões no heap precisam atualizar o índice pela camada de tabela; não há sincronização automática nem proteção contra RID obsoleto.

## Decisões físicas

Bloco 0: assinatura/versionamento, quantidade fixa de buckets, campo, unicidade e próximo bloco. Buckets ocupam os blocos 1 a B; overflow é alocado no fim. Cada página reserva 128 bytes para cabeçalho (ponteiro uint64 para o próximo bloco, zero no fim; 31 marcas de ocupação) e contém 31 registros de 128 bytes: `128 + 31*128 = 4096`. Exclusão desmarca o slot; inserção reutiliza o primeiro slot livre em toda a cadeia. Páginas vazias permanecem encadeadas e são reutilizadas, sem compactação ou truncamento.

Função `chave % B`, determinística inclusive com negativos. Módulo tem custo baixo para inteiros; distribuições enviesadas ou múltiplos de B podem gerar cadeias longas, deliberadamente exercitadas nos testes. Dimensionar B antes da carga a partir de `ceil(N/(31*fator_alvo))`, avaliando distribuição e colisões. Não há crescimento dinâmico. Uma busca custa até o comprimento da cadeia; unicidade é verificada na cadeia inteira antes da escrita, inclusive quando há um slot livre anterior. Memória auxiliar é limitada a uma página por leitura, sem dicionário global de chaves.

Faixa sempre percorre todos os blocos de dados e overflow. `last_access='scan'` informa esse caminho efetivo à bancada. Igualdade em outro campo também usa scan. `stats()` inclui cabeçalho, buckets, overflow, registros, ocupação física e fator de carga sobre buckets-base (pode exceder 1). As métricas de E/S devem vir do buffer, não de `stats()`, que também gera requisições e deve ser chamado fora da medição.

## Apresentação do overflow — aproximadamente 30 segundos

“O hash tem quatro buckets e cada bloco comporta 31 registros. Inserimos 32 chaves múltiplas de quatro, que caem no mesmo bucket. A última vai para o bloco de overflow 5. A busca pela chave 124 segue o encadeamento e encontra esse registro. A chave 128 cai no mesmo bucket, percorre a cadeia e retorna vazio. Com valores repetidos, percorremos todos os blocos; também testamos remover um registro do overflow e reutilizar seu espaço.”

## Pendências com o grupo

- Adaptar alocação, identificadores de arquivo, tipo de página e codec de registros às implementações reais; alinhar RID e `heap.get` com a frente 1.
- Validar persistência após flush, fechamento e reabertura de arquivos reais, expulsão de páginas dirty, buffer de 32 páginas, LRU/FIFO e contadores físicos. O simulado guarda o disco em RAM, copia cada página no pin e grava no unpin dirty; não simula cache, disco persistente ou custos reais.
- Integrar índice secundário à interface de consultas da tabela heap e à manutenção de registros; validar nomes de campos no catálogo.
- Rodar bancada integrada com 248/3.968 registros, semente 42, buffer frio, contadores zerados, três repetições e variação menor que 5%; comparar resultados com SQLite e medir custos, não só correção. Não usar os contadores do simulado como resultados de desempenho.
- Confirmar clustering com o professor, conforme ressalva do plano; índice B+ secundário pertence à outra frente.

Não há transações, recuperação de falhas, concorrência, detecção de corrupção de cadeias ou reorganização. Exclusões não devolvem blocos ao sistema operacional.
