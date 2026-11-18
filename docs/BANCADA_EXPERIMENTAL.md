# Bancada experimental do Partition Hash Join

## Objetivo e escopo

Esta bancada prepara o Partition Hash Join para uma comparação futura com Block
Nested Loop Join e Sort-Merge Join. A etapa atual executa somente:

- relações armazenadas em Heap File;
- Partition Hash Join;
- tamanhos pequeno–pequeno, pequeno–grande e grande–grande;
- políticas LRU e FIFO;
- três repetições por configuração.

A matriz completa com os outros algoritmos e caminhos de acesso ainda não foi
implementada. O objetivo desta etapa é estabelecer um contrato comum, uma
fronteira de medição clara e um CSV que possa ser reutilizado posteriormente.

## Arquivos da bancada

| Arquivo | Responsabilidade |
|---|---|
| `bancada/core.py` | Contrato comum, adaptador do PHJ, medição, validação e CSV |
| `bancada/phj.py` | Geração das relações e matriz inicial do PHJ |
| `bancada/__init__.py` | Interface pública da bancada |
| `tests/test_bancada_phj.py` | Testes do contrato, geração, métricas e exportação |
| `results/partition_hash_join_initial.csv` | Resultado real da primeira matriz oficial |

## Interface padronizada

Os algoritmos de join devem receber uma `JoinRequest`:

```python
JoinRequest(
    relation_r=relation_r,
    relation_s=relation_s,
    key_r=0,
    key_s=1,
    buffer=pool,
    memory_pages=16,
    options={'max_depth': 3},
)
```

Campos:

- `relation_r`: relação esquerda;
- `relation_s`: relação direita;
- `key_r`: campo ou extrator da chave de R;
- `key_s`: campo ou extrator da chave de S;
- `buffer`: Buffer Pool compartilhado;
- `memory_pages`: páginas de trabalho reservadas ao algoritmo;
- `options`: parâmetros que pertencem somente ao algoritmo.

Um adaptador implementa:

```python
class JoinAdapter:
    name: str

    def execute(self, request: JoinRequest) -> JoinExecution:
        ...
```

`JoinExecution` contém:

- `pairs`: iterador de pares `(registro_r, registro_s)`;
- `specific_metrics()`: função consultada depois do consumo do iterador.

O `PartitionHashJoinAdapter` converte a requisição comum para `RelationInput` e
`PartitionHashJoin`. Block Nested Loop e Sort-Merge poderão implementar novos
adaptadores sem alterar o núcleo do PHJ ou a função de medição.

## Dados usados

O gerador representa uma relação mestre–detalhe:

```text
Aluno(aluno_id, dre, faixa_cr)
Inscricao(inscricao_id, aluno_id, turma_id)
```

Predicado da junção:

```text
Aluno.aluno_id = Inscricao.aluno_id
```

Propriedades:

- `aluno_id` é único em Aluno;
- toda inscrição referencia um aluno existente;
- uma inscrição produz exatamente um par no resultado;
- a semente padrão é 42;
- pequena: 248 registros, equivalentes a 8 páginas de dados cheias;
- grande: 3.968 registros, equivalentes a 128 páginas de dados cheias.

Os mesmos dados lógicos são usados em todas as políticas e repetições de um
cenário. A geração ocorre antes do cronômetro.

## Configuração inicial

| Parâmetro | Valor padrão |
|---|---:|
| Registros da relação pequena | 248 |
| Registros da relação grande | 3.968 |
| Páginas do Buffer Pool | 32 |
| Páginas de trabalho do join | 16 |
| Seed | 42 |
| Repetições | 3 |
| Políticas | LRU e FIFO |
| Cenários | pequena–pequena, pequena–grande e grande–grande |

Isso gera 18 linhas:

```text
3 cenários × 2 políticas × 3 repetições = 18 execuções
```

## Fronteira da medição

### Fora do cronômetro

- geração das tuplas;
- criação e carga dos Heap Files de entrada;
- flush inicial das entradas;
- cálculo do resultado de referência;
- criação do identificador do experimento;
- escrita do CSV;
- fechamento final do pool depois de todas as repetições.

### Dentro do cronômetro

- criação do operador pelo adaptador;
- particionamento;
- build e probe;
- reparticionamento ou fallback, quando necessários;
- consumo completo do gerador;
- cálculo do checksum da saída;
- cleanup executado ao terminar o gerador;
- `buffer.flush()` para incluir escritas ainda pendentes.

Antes de iniciar o cronômetro, a bancada chama:

```python
buffer.drop_all()
buffer.reset_stats()
```

Assim, cada repetição começa com Buffer Pool frio e contadores zerados. O cache
do sistema operacional não é descartado; portanto, “buffer frio” refere-se ao
Buffer Pool implementado pelo projeto.

## Política de saída

A saída não é gravada em outro Heap File. Todos os pares são consumidos e cada
par contribui para:

- uma contagem total;
- um checksum BLAKE2b de 128 bits acumulado de forma independente da ordem.

Essa política evita que a ordem física escolhida por um algoritmo altere a
validação. Ela também preserva multiplicidade: pares repetidos contribuem
repetidamente para o acumulador.

O tempo inclui a contagem e o checksum. Os outros algoritmos deverão usar a
mesma função `measure_join`, garantindo o mesmo custo de consumo da saída.

Não há materialização da saída e não há chamada a `fsync`. As escritas passam
pelo `FileManager` sem buffering Python e o `BufferPool.flush()` é chamado, mas
o instante em que o sistema operacional persiste os bytes no dispositivo físico
não faz parte do modelo.

## Validação

O gerador conhece a chave primária de cada aluno e calcula antecipadamente o
resultado mestre–detalhe esperado, fora do cronômetro.

Depois da execução, a bancada compara:

- quantidade observada e esperada de pares;
- checksum observado e esperado;
- contador `output_pairs` do PHJ e quantidade consumida.

O campo `validation_status` assume:

- `ok`: contagem, checksum e contador interno concordam;
- `mismatch`: a execução terminou, mas o resultado está incorreto;
- `error`: uma exceção ocorreu; a mensagem fica em `error_message`.

Uma linha com erro ainda pode ser exportada, o que evita perder a identificação
da configuração que falhou. O comando termina com código diferente de zero se
qualquer linha não estiver com status `ok`.

## Métricas gerais

Estas métricas vêm diretamente do Buffer Pool:

- `physical_reads`;
- `physical_writes`;
- `cache_hits`;
- `cache_misses`;
- `buffer_requests`;
- `hit_rate`;
- `evictions`.

A bancada não cria contadores paralelos de leitura ou escrita.

## Métricas específicas do PHJ

- `partitions_created`;
- `repartition_passes`;
- `fallback_partitions`;
- `max_depth`;
- `build_records`;
- `max_build_records`;
- `probe_records`;
- `null_records`;
- `temp_reads`;
- `temp_writes`.

Esses campos podem ficar zerados para outros algoritmos. Quando Block Nested
Loop e Sort-Merge forem adicionados, suas métricas próprias devem usar novas
colunas ou um conjunto comum definido pelo grupo, sem alterar o significado das
colunas existentes.

## Estrutura do CSV

Cada linha possui 35 campos. Os principais são:

| Grupo | Campos |
|---|---|
| Identificação | `experiment_id`, `algorithm`, `seed`, `repetition` |
| Dados | `r_size`, `s_size` |
| Memória | `buffer_pages`, `memory_pages`, `replacement_policy` |
| Tempo | `elapsed_seconds` |
| Buffer | `physical_reads`, `physical_writes`, `cache_hits`, `cache_misses`, `buffer_requests`, `hit_rate`, `evictions` |
| Resultado | `result_count`, `result_checksum`, `validation_status`, `error_message` |
| PHJ | `partitions_created`, `repartition_passes`, `fallback_partitions`, `max_depth`, `build_records`, `max_build_records`, `probe_records`, `null_records`, `temp_reads`, `temp_writes` |
| Política de medição | `output_policy`, `output_materialized`, `pending_writes_flushed`, `os_fsync` |

O formato é plano e pode ser aberto diretamente por Python, Excel, LibreOffice,
R ou ferramentas de gráficos.

## Passo a passo para executar

### 1. Abrir o diretório do projeto

No PowerShell:

```powershell
cd D:\Code\Trab\cbd-avaliacao-2-grupo-1
```

### 2. Executar todos os testes

```powershell
python -m unittest discover -s tests -v
```

O resultado esperado é `OK`. Não prossiga para a campanha oficial se houver
falha.

### 3. Executar somente os testes da bancada

```powershell
python -m unittest tests.test_bancada_phj -v
```

Esses testes usam tamanhos reduzidos e verificam contrato, reprodutibilidade,
métricas e CSV.

### 4. Fazer uma execução curta de verificação

```powershell
python -m bancada.phj `
  --output results/phj_smoke.csv `
  --small-size 8 `
  --large-size 40 `
  --buffer-pages 4 `
  --memory-pages 3 `
  --seed 7 `
  --repetitions 1
```

Devem ser produzidas seis linhas: três cenários vezes duas políticas.

### 5. Executar a matriz inicial oficial

Todos os valores oficiais já são os padrões:

```powershell
python -m bancada.phj `
  --output results/partition_hash_join_initial.csv
```

Forma explícita equivalente:

```powershell
python -m bancada.phj `
  --output results/partition_hash_join_initial.csv `
  --small-size 248 `
  --large-size 3968 `
  --buffer-pages 32 `
  --memory-pages 16 `
  --seed 42 `
  --repetitions 3 `
  --policies LRU FIFO `
  --scenarios small-small small-large large-large `
  --max-depth 3
```

### 6. Executar somente uma parte da matriz

Exemplo: grande–grande com LRU e cinco repetições:

```powershell
python -m bancada.phj `
  --output results/phj_large_large_lru.csv `
  --scenarios large-large `
  --policies LRU `
  --repetitions 5
```

### 7. Conferir falhas no PowerShell

```powershell
Import-Csv results/partition_hash_join_initial.csv |
  Group-Object validation_status |
  Select-Object Name, Count
```

O esperado é somente o grupo `ok`.

### 8. Conferir leituras e escritas

```powershell
Import-Csv results/partition_hash_join_initial.csv |
  Select-Object experiment_id, physical_reads, physical_writes, `
    cache_hits, cache_misses, result_count, validation_status |
  Format-Table -AutoSize
```

### 9. Avaliar estabilidade do tempo

```powershell
Import-Csv results/partition_hash_join_initial.csv |
  Group-Object r_size, s_size, replacement_policy |
  ForEach-Object {
    $times = $_.Group.elapsed_seconds | ForEach-Object { [double]$_ }
    [pscustomobject]@{
      Configuracao = $_.Name
      Minimo = ($times | Measure-Object -Minimum).Minimum
      Maximo = ($times | Measure-Object -Maximum).Maximum
      Media = ($times | Measure-Object -Average).Average
    }
  } | Format-Table -AutoSize
```

Se a variação continuar acima de 5%, execute mais repetições e investigue
interferência do sistema. A contagem de E/S é o indicador principal do trabalho;
tempo de relógio é complementar.

## Evidência da primeira execução oficial

O comando padrão foi executado com sucesso e produziu 18 linhas válidas.

| R | S | Política | Tempo médio inicial | Leituras | Escritas | Hits | Misses | Partições | Repartições | Fallback |
|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 248 | 248 | LRU | 0,008375 s | 32 | 18 | 530 | 32 | 2 | 0 | 0 |
| 248 | 248 | FIFO | 0,006579 s | 32 | 18 | 530 | 32 | 2 | 0 | 0 |
| 248 | 3.968 | LRU | 0,042081 s | 272 | 138 | 4.490 | 272 | 2 | 0 | 0 |
| 248 | 3.968 | FIFO | 0,092904 s | 279 | 145 | 4.483 | 279 | 2 | 0 | 0 |
| 3.968 | 3.968 | LRU | 0,134902 s | 748 | 516 | 9.225 | 748 | 24 | 1 | 0 |
| 3.968 | 3.968 | FIFO | 0,110723 s | 716 | 484 | 9.257 | 716 | 24 | 1 | 0 |

Todas as execuções retornaram a quantidade esperada de pares:

- pequena–pequena: 248;
- pequena–grande: 3.968;
- grande–grande: 3.968.

As métricas de E/S foram estáveis entre as três repetições de cada configuração.
Os tempos não ficaram dentro de uma faixa de 5% nesta primeira rodada. A maior
variação ocorreu em execuções muito curtas e em algumas primeiras repetições.
Portanto, esses tempos servem como evidência de funcionamento, não como conclusão
estatística final.

O caso grande–grande realizou um reparticionamento. Nenhum caso precisou usar o
fallback.

## Interpretação inicial

O cenário pequeno–pequeno usa uma partição por lado e tem custo próximo do
modelo clássico de leitura, escrita e releitura. Os cabeçalhos temporários
explicam parte das operações adicionais.

No cenário pequeno–grande, LRU e FIFO já apresentam contagens diferentes porque
o conjunto ativo de páginas supera o espaço disponível durante partes do
particionamento.

No cenário grande–grande, uma das partições ficou acima do limite do build e foi
reparticionada. Por isso o custo ultrapassa a fórmula simples:

```text
3 × (blocos de R + blocos de S)
```

Também há custo dos cabeçalhos dos Heap Files temporários e atualizações da
lista de páginas livres. A comparação teórica final deve declarar se inclui ou
exclui esses metadados.

## Limites da medição

- O cronômetro inclui checksum da saída; isso é intencional e deve ser igual
  para todos os joins.
- A saída não é materializada em disco.
- `BufferPool.flush()` está dentro do tempo, mas não há `fsync`.
- O cache do sistema operacional não é esvaziado.
- O limite de memória do PHJ é lógico em páginas e não mede o RSS do Python.
- A primeira matriz tem exatamente três repetições e ainda não satisfaz o alvo
  de variação temporal inferior a 5%.
- Somente Heap File e PHJ estão incluídos nesta etapa.
- O tempo de criação de índices não é medido porque este cenário não usa índice.

## Como integrar os outros joins

### Block Nested Loop Join

Criar `BlockNestedLoopJoinAdapter` com:

```python
name = 'block_nested_loop_join'

def execute(self, request):
    return JoinExecution(pairs=..., specific_metrics=...)
```

O adaptador deve usar `request.memory_pages`, respeitar a ordem R/S e não
executar `drop_all()` ou `reset_stats()`. Esse controle pertence à bancada.

Métricas específicas possíveis:

- blocos carregados por lote externo;
- número de rescans da relação interna.

### Sort-Merge Join

Criar `SortMergeJoinAdapter` com o mesmo contrato. Arquivos das corridas e do
merge devem passar pelo Buffer Pool e usar `kind='temp'`.

Métricas específicas possíveis:

- corridas iniciais;
- passes de merge;
- páginas temporárias lidas e escritas.

### Regras comuns

Os novos adaptadores não devem:

- gerar dados;
- limpar ou zerar o Buffer Pool;
- iniciar cronômetros próprios;
- escrever CSV;
- materializar a saída com política diferente;
- contar leituras e escritas fora do Buffer Pool.

Devem apenas receber `JoinRequest` e devolver `JoinExecution`. Assim,
`measure_join` garante dados, consumo, validação, cronômetro e exportação
comparáveis.

## Checklist para a campanha final

1. Executar toda a suíte de testes.
2. Confirmar que os outros joins usam `JoinRequest` e `JoinExecution`.
3. Gerar uma única base lógica por cenário e seed.
4. Carregar dados e índices fora do cronômetro.
5. Começar cada repetição com Buffer Pool frio.
6. Consumir toda a saída com a mesma política de checksum.
7. Incluir flush pendente dentro do tempo de todos os algoritmos.
8. Exportar uma linha por execução.
9. Rejeitar linhas com `error` ou `mismatch`.
10. Repetir até obter estabilidade ou justificar a variação.
11. Comparar custo medido com custo teórico.
12. Preservar CSV bruto, comandos e ambiente usados no relatório.
