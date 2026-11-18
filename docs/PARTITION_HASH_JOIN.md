# Partition Hash Join

## Visão geral

Este documento explica a implementação do **Partition Hash Join (PHJ)** feita
para o projeto de Computação em Banco de Dados. O objetivo do operador é juntar
duas relações pelo valor de uma chave, mesmo quando os dados são maiores que a
memória reservada para a operação.

A implementação realiza um `INNER JOIN` por igualdade. Para cada registro da
relação esquerda, ela encontra todos os registros da relação direita que têm a
mesma chave. Se uma chave aparece `m` vezes à esquerda e `n` vezes à direita, o
resultado contém `m × n` pares.

O operador foi construído sobre o `HeapFile`, o `BufferPool` e o `FileManager`
existentes. As leituras e escritas das partições temporárias passam pelo buffer;
o código do join não lê nem grava páginas diretamente no sistema operacional.

## Requisitos atendidos

Os prompts de análise, implementação e validação estabeleceram os seguintes
requisitos, que estão presentes na versão atual:

- executar um equijoin entre duas relações;
- aceitar relações maiores que o Buffer Pool;
- percorrer as relações por `scan_pages()`;
- usar uma função de hash determinística;
- materializar partições temporárias em disco;
- escolher a menor partição como lado de build;
- preservar chaves duplicadas;
- manter a ordem lógica `(esquerda, direita)` do resultado;
- limitar o build ao orçamento de páginas;
- reparticionar partições que não cabem em memória;
- usar um fallback limitado quando o reparticionamento não progride;
- aplicar a semântica SQL para `NULL`;
- liberar pins e remover temporários em sucesso ou exceção;
- fornecer métricas para testes e para a futura bancada experimental.

## Arquivos envolvidos

| Arquivo | Responsabilidade |
|---|---|
| `partition_hash_join/core.py` | Implementação do operador, partições, build, probe, fallback e métricas |
| `partition_hash_join/__init__.py` | Exportação da interface pública |
| `buffer_heap/registro.py` | Codificação, decodificação e leitura dos campos dos registros |
| `buffer_heap/heap.py` | Armazenamento e scan das relações e partições temporárias |
| `buffer_heap/buffer.py` | Pin, unpin, substituição, flush, expulsão e remoção de temporários |
| `buffer_heap/files.py` | Leitura, escrita, fechamento e remoção dos arquivos físicos |
| `tests/test_partition_hash_join.py` | Testes básicos, integração, skew, cleanup e erros |
| `tests/test_partition_hash_join_validation.py` | Testes por multiconjunto, SQLite, propriedades, memória e E/S |

O hash externo existente em `hash_externo/core.py` é uma organização de arquivo
por buckets. Ele não é usado como substituto do Partition Hash Join, pois os
dois componentes resolvem problemas diferentes.

## Formato dos registros e páginas

Cada registro possui 128 bytes:

- três inteiros signed de 64 bits em little-endian;
- 104 bytes de preenchimento.

Cada página possui 4096 bytes. No Heap File, uma página de dados contém:

- 128 bytes de cabeçalho;
- 31 registros de 128 bytes.

Assim:

```text
128 + 31 × 128 = 4096 bytes
```

As chaves do esquema atual são identificadas pelas posições `0`, `1` ou `2`.
Também é possível fornecer uma função própria para extrair a chave.

## Interface pública

### RelationInput

`RelationInput` descreve uma relação que pode ser lida pelo operador. Ela
contém:

- uma função `scan_pages()` reescaneável;
- uma função que extrai a chave de um registro;
- a quantidade de páginas da relação;
- um nome descritivo;
- o Buffer Pool da fonte, quando aplicável.

A forma mais simples de criar essa descrição para um `HeapFile` é:

```python
left = RelationInput.from_source(heap_left, 0)
right = RelationInput.from_source(heap_right, 1)
```

Nesse exemplo, a chave da esquerda está no campo 0 e a chave da direita está no
campo 1.

### PartitionHashJoin

O operador é criado com o Buffer Pool e o número de páginas de trabalho:

```python
from partition_hash_join import PartitionHashJoin, RelationInput

operator = PartitionHashJoin(
    pool,
    memory_pages=8,
    temp_prefix='phj',
    max_depth=3,
)
```

Parâmetros:

- `buffer`: Buffer Pool compartilhado pelas relações e pelos temporários;
- `memory_pages`: orçamento lógico de páginas do operador;
- `temp_prefix`: prefixo dos arquivos temporários;
- `max_depth`: limite de níveis de reparticionamento.

O orçamento deve ter pelo menos três páginas e não pode ultrapassar a capacidade
do Buffer Pool.

### Execução

```python
left = RelationInput.from_source(heap_left, 0)
right = RelationInput.from_source(heap_right, 1)

for left_raw, right_raw in operator.join(left, right):
    left_values = registro.decode(left_raw)
    right_values = registro.decode(right_raw)
    print(left_values, right_values)
```

O resultado é produzido aos poucos por um iterador. O join não carrega todo o
resultado em memória e não altera os registros originais.

Também existe o atalho:

```python
iterator, operator = partition_hash_join(
    heap_left,
    heap_right,
    0,
    1,
    buffer=pool,
    memory_pages=8,
)
```

O atalho devolve o iterador e o operador, permitindo consultar
`operator.stats` depois da execução.

## Funcionamento do algoritmo

O PHJ possui quatro etapas principais: Partition, Build, Probe e Output.

### 1. Partition

As duas relações são percorridas página por página. Para cada registro:

1. o extrator obtém a chave;
2. a função de hash calcula o número da partição;
3. o registro é inserido no Heap File temporário correspondente.

As duas relações usam a mesma função e a mesma profundidade de hash. Portanto,
registros com chaves iguais sempre chegam ao mesmo par de partições.

A função de hash usa BLAKE2b com resultado de 64 bits. A profundidade atual é
usada como seed. Isso traz duas propriedades importantes:

- o resultado é determinístico entre execuções;
- um novo nível de reparticionamento usa uma distribuição diferente.

As chaves suportadas pelo particionamento são `int`, `bytes`, `str` e `None`.
O esquema atual usa inteiros.

O número inicial de partições considera o tamanho da menor relação e a
capacidade de build:

```text
capacidade de build = B - 2 páginas

partições = teto(páginas da menor relação / capacidade de build)
```

O número de partições é limitado a `B - 1`, em que `B` é o orçamento de páginas
do operador.

### 2. Materialização das partições

Cada partição não vazia é um `HeapFile` classificado como `temp`. Depois da fase
de escrita, o operador:

1. executa `flush(arquivo)`;
2. expulsa somente as páginas daquele temporário do buffer;
3. lê posteriormente a partição pelo `scan_pages()` do Heap File.

Esse processo garante que a bancada contabilize a escrita e a releitura das
partições. O operador não chama `drop_all()`, pois isso também removeria páginas
de outras relações do buffer.

### 3. Build

Para cada par de partições, o operador escolhe a menor como lado de build. A
comparação usa o número de páginas e, em caso de empate, o número de registros.

A tabela hash em memória tem o formato conceitual:

```python
{
    chave: [registro_1, registro_2, ...]
}
```

A lista é necessária porque uma chave pode aparecer mais de uma vez. Colisões
da função de particionamento não causam falsos resultados: durante o build e o
probe, a chave completa continua sendo comparada por igualdade.

O build pode manter no máximo:

```text
(B - 2) × 31 registros
```

A métrica `max_build_records` registra o maior número de registros mantidos em
uma tabela de build ou lote durante a execução.

### 4. Probe e Output

A partição do outro lado é percorrida página por página. Para cada registro:

1. o operador extrai a chave;
2. procura a chave na tabela construída;
3. produz um par para cada registro encontrado.

Se a chave aparece duas vezes à esquerda e três vezes à direita, são produzidos
seis pares.

Mesmo quando a relação direita é escolhida para build, a saída continua na
ordem:

```text
(registro da relação esquerda, registro da relação direita)
```

Não há garantia sobre a ordem entre os pares, e os testes comparam o resultado
como multiconjunto para preservar a multiplicidade sem exigir ordenação.

## Partições maiores que a memória

Uma partição pode continuar maior que `B - 2` páginas, especialmente em dados
desbalanceados. O operador trata esse caso de duas formas.

### Reparticionamento

Quando ainda há profundidade disponível, o par de partições é dividido
novamente usando outra seed. Se a maior partição filha ficar menor que a
partição original, houve progresso e o processamento continua recursivamente.

O limite `max_depth` impede recursão infinita.

### Fallback por lotes

Se todos os registros continuam na mesma partição ou o limite de profundidade é
atingido, o operador usa um fallback limitado:

1. lê no máximo `(B - 2) × 31` registros do lado de build;
2. monta a tabela hash para esse lote;
3. reescaneia toda a partição de probe;
4. repete o processo com o próximo lote.

Essa estratégia pode fazer mais leituras, mas preserva a correção e nunca
aumenta silenciosamente o orçamento. É especialmente importante quando muitos
registros possuem exatamente a mesma chave.

## Tratamento de casos especiais

### Duplicatas

Duplicatas são preservadas. O resultado contém o produto cartesiano de todas as
ocorrências da mesma chave.

### NULL

O registro físico atual não possui um marcador próprio para `NULL`. Entretanto,
um extrator de chave pode devolver `None`. Nesse caso, o registro não participa
da junção.

Assim, dois valores `None` não correspondem entre si, seguindo a semântica de:

```sql
INNER JOIN ... ON left.key = right.key
```

### Chaves negativas

Chaves inteiras negativas são codificadas de forma determinística e são
suportadas normalmente.

### Relações vazias

Se uma das relações ou uma das partições correspondentes estiver vazia, nenhum
par é produzido. Temporários vazios não são criados desnecessariamente.

### Inversão das relações

O algoritmo funciona independentemente de qual relação é fornecida à esquerda
ou à direita. A escolha interna do build não muda o significado lógico da
saída.

## Gerenciamento dos temporários

Cada execução recebe um prefixo com UUID, evitando colisões entre nomes. O
workspace interno registra todos os temporários criados.

Ao terminar uma partição ou a execução completa, o operador:

- verifica se não há página fixada;
- remove os frames do arquivo do Buffer Pool;
- fecha o arquivo no FileManager;
- remove o arquivo do disco;
- preserva as métricas de E/S já contabilizadas;
- remove o nome dos mapas ativos do FileManager.

O cleanup ocorre em blocos `finally`, inclusive se o extrator de chave ou o
consumidor gerar uma exceção. Se o consumidor abandonar o resultado antes do
fim, deve chamar `close()` no gerador para antecipar a limpeza:

```python
results = operator.join(left, right)
try:
    first = next(results)
finally:
    results.close()
```

Uma instância de `PartitionHashJoin` aceita apenas uma execução ativa por vez.

## Integração com o Buffer Pool

O join não reimplementa o Buffer Pool. As partições utilizam as interfaces já
existentes:

- `pin(arquivo, bloco)`;
- `unpin(arquivo, bloco, dirty=False)`;
- `allocate(arquivo)`;
- `flush(arquivo)`;
- `evict_file(arquivo)`;
- `remove_file(arquivo)`.

As operações compartilhadas adicionadas para os temporários são compatíveis com
as interfaces anteriores:

- `HeapFile(buffer, arquivo, kind='data')` permite marcar uma partição como
  `kind='temp'`;
- `BufferPool.evict_file()` expulsa somente um arquivo;
- `BufferPool.remove_file()` aceita apenas arquivos classificados como
  temporários;
- `FileManager.remove()` fecha e apaga o arquivo, preservando os contadores por
  tipo.

As políticas LRU e FIFO continuam sendo controladas pelo Buffer Pool.

## Métricas disponíveis

Depois de consumir ou fechar o iterador, `operator.stats` contém:

| Métrica | Significado |
|---|---|
| `partitions_created` | Arquivos de partição não vazios criados |
| `repartition_passes` | Reparticionamentos que reduziram a maior partição |
| `fallback_partitions` | Pares processados pelo fallback em lotes |
| `max_depth` | Maior profundidade efetivamente processada |
| `build_records` | Total de registros carregados nos builds |
| `max_build_records` | Maior build ou lote mantido de uma vez |
| `probe_records` | Total de registros examinados no probe, incluindo releituras |
| `output_pairs` | Pares entregues ao consumidor |
| `null_records` | Registros ignorados porque a chave era `None` |
| `physical_reads` | Leituras físicas durante a execução |
| `physical_writes` | Escritas físicas durante a execução |
| `temp_reads` | Leituras físicas de arquivos temporários |
| `temp_writes` | Escritas físicas de arquivos temporários |

As métricas físicas são calculadas pela diferença entre os contadores do Buffer
Pool no início e no fim. O operador não executa `reset_stats()`, pois isso
apagaria métricas pertencentes a outros componentes.

## Testes realizados

A implementação foi testada com `unittest`, Heap File e Buffer Pool reais. A
validação inclui:

- uma correspondência e nenhuma correspondência;
- relações vazias;
- duplicatas à esquerda, à direita e em ambos os lados;
- colisões de partição entre chaves diferentes;
- chaves negativas;
- semântica de `NULL`;
- relações com tamanhos diferentes;
- inversão de esquerda e direita;
- partições que cabem e que não cabem no build;
- relações maiores que um Buffer Pool de três páginas;
- distribuição uniforme e distribuição desbalanceada;
- reparticionamento com e sem progresso;
- fallback por lotes;
- limpeza dos arquivos temporários;
- pins balanceados;
- políticas LRU e FIFO;
- consistência entre métricas do operador e do Buffer Pool;
- múltiplas execuções consecutivas;
- independência lógica em relação ao tamanho do buffer;
- conjuntos determinísticos comparados ao SQLite;
- comparação como multiconjunto, incluindo todas as colunas.

Na validação realizada durante o desenvolvimento, a suíte completa apresentou:

```text
Ran 52 tests
OK
```

Para repetir os testes:

```powershell
python -m unittest discover -s tests -v
```

Para executar somente os testes do Partition Hash Join:

```powershell
python -m unittest tests.test_partition_hash_join `
    tests.test_partition_hash_join_validation -v
```

## Decisões e justificativas

### Reutilizar Heap File nas partições

Essa decisão evita criar outro formato de página. As partições passam pelas
mesmas regras de pin, unpin, dirty, flush, substituição e métricas das relações
principais.

### Usar BLAKE2b

O `hash()` genérico do Python não é apropriado para partições persistidas porque
alguns tipos usam randomização entre processos. BLAKE2b fornece uma distribuição
reprodutível e permite variar a seed por profundidade.

### Produzir resultado lazy

O resultado pode ser maior que as duas entradas por causa das duplicatas.
Produzir os pares aos poucos evita armazenar toda a saída em memória.

### Usar fallback em vez de recursão ilimitada

Quando muitos registros têm a mesma chave, nenhuma nova função de
particionamento consegue separá-los. O fallback por lotes garante término sem
descartar registros ou ultrapassar o limite lógico.

### Medir por páginas lógicas

O orçamento representa a quantidade de dados que caberia nas páginas do modelo
do SGBD. Objetos Python, como `dict` e `bytes`, possuem overhead próprio que não
é incluído nessa conta. A métrica `max_build_records` torna o limite lógico
observável e testável.

## Limitações conhecidas

- O limite de memória é lógico e não corresponde exatamente ao RSS do processo
  Python.
- Não há transações, concorrência ou recuperação após encerramento abrupto.
- O resultado não é materializado automaticamente em outro Heap File.
- O consumidor deve fechar um iterador abandonado para cleanup imediato.
- O operador utiliza scan das fontes; índices disponíveis não são usados para
  evitar o particionamento.
- Não há garantia de ordenação dos pares.
- Falhas reais de disco e corrupção deliberada de temporários não fazem parte da
  suíte atual.

Essas limitações são compatíveis com o escopo acadêmico, que concentra a
avaliação em correção, memória lógica, E/S física e comparação entre algoritmos.

## Trabalho ainda pendente para a bancada

O núcleo do PHJ e sua suíte estão implementados. A bancada experimental ainda
deve executar os cenários oficiais do grupo:

- dados gerados com semente 42;
- relações de 248 e 3.968 registros;
- combinações pequena–pequena, pequena–grande e grande–grande;
- Buffer Pool de 32 páginas;
- 16 páginas de trabalho para o join;
- comparação entre LRU e FIFO;
- buffer frio e contadores zerados antes de cada medição;
- pelo menos três repetições, buscando variação inferior a 5%;
- validação do resultado com SQLite;
- geração de CSV com configuração, caminho efetivo e métricas;
- comparação entre custo teórico e custo medido;
- integração com nested-loop em blocos e sort-merge join.

Para o Partition Hash Join, o custo clássico sem reparticionamento adicional
pode ser usado como referência:

```text
3 × (blocos de R + blocos de S)
```

O valor representa leitura das relações, escrita das partições e releitura das
partições. Cabeçalhos, partições vazias, flush, cache, reparticionamento e
fallback explicam possíveis diferenças entre a fórmula e a medição real.

## Resumo para apresentação

O Partition Hash Join divide as duas relações com a mesma função de hash. Para
cada par de partições, carrega a menor na memória, monta uma tabela hash e
percorre a outra procurando correspondências. Duplicatas são preservadas e a
saída é produzida aos poucos. Se uma partição ainda for grande, ela é dividida
novamente; se isso não ajudar por causa de skew, o build é processado em lotes.
Todas as partições passam pelo Buffer Pool e são removidas ao final, permitindo
medir corretamente leituras e escritas físicas.
