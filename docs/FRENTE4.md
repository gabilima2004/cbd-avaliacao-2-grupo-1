# Frente 4 — índice B+ secundário e seleções

Implementada na branch `frente-4-bmais-selecoes`, criada a partir da `main`
`336c0d8`, que estava sem alterações locais. Não foram alterados os módulos das
frentes 1 e 3 nem implementadas junções, catálogo, gerador geral ou B+ primária.

## Conferir e repetir

```powershell
python tests/run.py docs/ia/frente4/testes_ultima_execucao.txt
python tests/bench_frente4.py
```

Resultado da implementação: **51 testes, OK, EXIT_CODE=0**. Destes, 35 já
existiam e 16 são da frente 4. Procure `BASICO`, `ESTRESSE ESTRUTURAL`,
`ESTRESSE FISICO`, `ESTRESSE SQLITE`, `CHURN` e as indicações de real/simulado.
A bancada termina com **CENARIOS=84; MEDICOES=252; SQLITE=OK**,
`VARIACAO_LEITURAS<5%=OK; PINS=0; ESCRITAS_CONSULTAS=0`.
CSV e resumo ficam em `docs/ia/frente4/bench_ultima_execucao.csv` e `.txt`.
Esses nomes de última execução podem ser sobrescritos; as evidências históricas
estão em arquivos separados. A suíte legada emite um `ResourceWarning` de um
arquivo aberto no teste da frente 1; o aviso já ocorria na linha de base.

Sem pacotes externos para executar. Nesta sessão, o agente usou
`.tools/python311/python.exe` por causa do isolamento da conta do Windows;
no terminal do integrante, use `python` ou `py -3.11`.
`pypdf`, usado para conferir o enunciado original, ficou somente em `.tools/`.

## Contratos e exemplo

`BMaisSecundario(buffer, arquivo, tabela, campo=2)` cria o índice em arquivo vazio;
omitir `campo` reabre o índice e recupera suas configurações do bloco 0.
O buffer real abre o arquivo como `index`. Com simulado, `allocate` deve reservar
blocos contíguos a partir de zero; vale o contrato de arquivo vazio do hash.

| Operação | Contrato |
|---|---|
| `insert(chave, rid)` | Acrescenta um par, rejeitando o mesmo par duplicado. |
| `find_rid(chave)` / `find(chave)` | Iteram RIDs / registros, inclusive valores repetidos. |
| `find_range_rid(limite, operador)` / `find_range(limite, operador)` | `<`, `<=`, `>` e `>=`, com limites int64. |
| `delete(chave, rid)` | Remove apenas o par do índice, preservando a tabela. |
| `delete(chave)` | Remove a entrada inteira e libera suas páginas de RIDs. |
| `validate(check_records=True)` | Audita estrutura e referências, fora da medição. |

Exemplo completo, sem deixar arquivos de dados no repositório:

```python
from tempfile import TemporaryDirectory
from buffer_heap import BufferPool, FileManager, HeapFile, registro
from indice_bmais import BMaisSecundario
from selecoes import TabelaSelecao

with TemporaryDirectory() as pasta:
    with BufferPool(FileManager(pasta), 32, "LRU") as buffer:
        heap = HeapFile(buffer, "alunos")
        indice = BMaisSecundario(buffer, "faixa_cr.bplus", heap, campo=2)
        for aluno_id, faixa in enumerate((0, 10, 50, 99, 0)):
            rid = heap.insert(registro.encode(aluno_id, 100000 + aluno_id, faixa))
            indice.insert(faixa, rid)
        tabela = TabelaSelecao(heap, {2: indice})
        resultado = tabela.find_range(2, 1, "<", "secondary")
        print([registro.decode(raw)[0] for raw in resultado])  # [0, 4]
```

`TabelaSelecao` mantém `scan_pages()`, `find_equal(campo, valor, acesso)` e
`find_range(campo, limite, operador, acesso)`. O acesso é escolhido explicitamente,
sem otimizador. `last_access` informa o caminho efetivo depois de consumir o
iterador, inclusive em resultados vazios. O caminho `primary` é delegado ao módulo
da organização; faixa no hash e igualdade em campo não organizado podem virar
`scan`. Solicitar um índice indisponível gera erro, sem substituição silenciosa.
O mapa `{campo: indice}` é o catálogo mínimo fornecido pelo integrador.

No heap, associe DRE ao `HashSecundario` da frente 3 e faixa_cr à B+; a escolha
de hash para UNIQUE segue o enunciado. B+ em campos de alta cardinalidade aparece
apenas nos testes estruturais para forçar divisões, não como configuração UNIQUE
do heap na bancada.

Não houve mudança das assinaturas compartilhadas. A necessidade adicional para
resolver RIDs de um índice secundário é `tabela.get(RID)`, já disponível no heap.
O hash não a oferece: `HashComRID(hash_primario)` é um adaptador local que usa
`pin/unpin` e o layout `HASHv001` existente, inclusive overflow. A B+ primária
precisará oferecer `get(RID)` ou adaptador equivalente, com RIDs estáveis.
Os dois módulos existentes usam classes RID diferentes; o índice aceita os
atributos `bloco/slot` e normaliza para o RID da frente 1.

A camada que mantém a tabela deve atualizar explicitamente todos os índices.
Ao excluir um registro, remova seu par do índice antes de reutilizar o slot.
Ao alterar uma chave, remova a entrada antiga e insira a nova. Reorganização da
tabela pode invalidar todos os RIDs e exige reconstrução dos índices.
O código detecta RID removido ou reutilizado com outra chave (`StaleIndexError`);
reutilização com a mesma chave não é detectável sem versionamento de RID.

## Decisões físicas

| Item | Escolha e justificativa |
|---|---|
| Bloco | 4096 bytes, como o plano e os módulos existentes. |
| Bloco 0 | `BPLSv001`, versão, campo, capacidades, raiz, primeira folha, lista de livres, contagens e total de blocos. |
| Cabeçalho de página | 128 bytes; tipo, quantidade e próximo bloco; restante reservado. |
| Folha | Entrada de 48 B: chave int64, RID inline (dois uint64), cabeça/cauda da cadeia e contagem. Até 82 valores distintos. |
| Nó interno | Primeiro filho uint64 e pares separador int64/filho uint64. Até 247 separadores e 248 filhos. |
| RIDs adicionais | 248 RIDs de 16 B por página encadeada; o RID inline evita alocar uma página para valores que ocorrem uma vez. |
| Ordenação | Uma entrada por chave nas folhas; RIDs de cada chave em ordem `(bloco, slot)`. A inserção de RIDs crescentes vai à cauda. |
| Ocupação mínima | Folhas não raiz: 41 entradas; internos não raiz: 124 filhos. Na raiz, as exceções usuais de árvore vazia ou dois filhos. |
| Manutenção | Split de folha/interno, propagação até nova raiz; exclusão com empréstimo/fusão e redução da raiz. Páginas liberadas entram em free list persistente. |
| Carga | Inserções individuais, sem bulk loading; custo da carga separado das consultas. |

Leituras/escritas usam somente `pin/unpin`, com `dirty=True` após a escrita;
alocação usa `buffer.allocate`. Nenhum iterador suspenso retém páginas fixadas.
As operações guardam cópias de poucos nós de tamanho limitado e o caminho de
identificadores, sem uma lista global de chaves ou de RIDs da resposta.
`validate` e os modelos de teste usam memória proporcional à base deliberadamente,
somente fora da medição. Páginas de RIDs parcialmente vazias são mantidas; páginas
totalmente vazias são reutilizáveis. Não há truncamento, transações, recuperação
de falhas ou suporte a alterações concorrentes durante uma iteração.

## Enunciado e plano

Foram conferidos o PDF original de oito páginas e o conteúdo do DOCX, além do
README e das implementações existentes. Referências: enunciado §§4.2 e 5.2
(pp. 3–5), §6 (pp. 5–6), §7 (pp. 7–8); plano, frente 4 e parâmetros/interfaces.

O plano adia a confirmação do armazenamento clustering. O enunciado exige B+
secundária clustering para NOT UNIQUE nas organizações B+ e hash. **O enunciado
prevalece: esses cenários continuam obrigatórios, não foram descartados.** Este
índice armazena referências, sem reorganizar as tabelas das outras frentes.
Uma lista de RIDs ordenada não torna a tabela fisicamente clustering.
A validação desse armazenamento depende da integração com as frentes 2/3;
os resultados atuais não certificam essa exigência. A B+ primária ausente foi
substituída por um dublê apenas nos testes de contrato, sem atribuir custos a ele.

## Testes e medidas

| Bateria | Módulos utilizados e o que demonstra |
|---|---|
| `TestBasico` (6 testes) | Buffer/heap simulados estritos existentes, B+ secundária real. Dirty obrigatório, um pin por vez, repetidas, limites, 763 RIDs numa chave e iteração parcial. |
| `TestSelecoes` (6 testes) | Arquivos, buffer, heap, hash primário/secundário e B+ secundária reais; SQLite como referência. Um teste usa a B+ primária simulada, sem custos físicos. |
| `TestEstresse` (4 testes) | Módulos reais: árvores com capacidade 3/4/5 e buffer de três páginas, 11 mil chaves com capacidade física padrão, 3.968 registros com exclusão/reinserção de metade, 5 mil operações intercaladas. |
| `bench_frente4.py` | Somente módulos reais. Heap, hash primário UNIQUE e NOT UNIQUE; B+ secundária para faixa, hash secundário UNIQUE no heap. |

Na bancada: mesmos dados com semente 42, 248 e 3.968 registros, LRU/FIFO,
32 páginas, três repetições. Carga, validação e leitura de metadados de abertura
ficam fora do intervalo medido. Antes de cada consulta, `drop_all()` faz flush e
esvazia o buffer; **depois** `reset_stats()` zera os contadores.
A variação inferior a 5% é das leituras físicas (0% nesta execução); o tempo
de relógio é informativo. A suíte compara tuplas completas com o SQLite; a bancada
confere contagem e checksum de multiconjunto BLAKE2b em fluxo, sem reter a resposta.

O CSV inclui seletividade obtida, acesso solicitado/efetivo, ocupação em blocos
(com cabeçalhos), altura, leituras/escritas, requisições, acertos/faltas, taxa de
acerto, expulsões/flush e leituras por tipo de arquivo. Os índices podem fazer a
configuração pequena exceder 32 páginas, embora os dados do heap ocupem oito:
os blocos auxiliares estão explicitamente registrados.

Custos confrontados com os contadores, calculados fora da medição:

- Scan no heap: exatamente B blocos de dados, sem o cabeçalho (8 ou 128).
- Scan/faixa no hash: todos os buckets/overflow e o cabeçalho.
- Igualdade UNIQUE: cadeia do bucket; via hash secundário no heap, mais um bloco de dados.
- Faixa B+: se L são as folhas visitadas, P as páginas de RIDs, R a resposta e D
  os blocos distintos dos dados, `L+P+D ≤ leituras ≤ L+P+R` no heap. No adaptador
  hash, inclui-se o cabeçalho: mínimo `L+P+D+1` se R>0, máximo `L+P+2R`.
  São limites analíticos, não uma previsão exata que ignora expulsões.

Evidência inicial da bancada após a correção de validação da API:
`docs/ia/frente4/bench_v1.csv`. O sufixo identifica a primeira execução da
bancada, não o código bruto anterior à revisão.
Com 3.968 registros/LRU, `faixa_cr < 1` encontrou 29 linhas (0,7308%):
heap scan = 128 leituras, B+ secundária = 29. `faixa_cr < 50` encontrou 1.926
(48,5383%): scan = 128, B+ = 1.606. Alta seletividade em dados espalhados
revisita páginas e pode tornar o índice pior. No hash NOT UNIQUE organizado por
faixa_cr, a mesma faixa alta custou 148 leituras pelo índice contra 222 na
varredura; isso não substitui a certificação do clustering físico requerido.

## Fala de aproximadamente 30 segundos

“Carreguei 3.968 alunos e pedi faixa_cr menor que 1. A B+ guarda uma entrada para
cada valor e segue uma lista de RIDs para os alunos correspondentes. Vieram os
mesmos 29 alunos do SQLite. Com buffer frio, a varredura leu 128 blocos e o índice
leu 29. Ao ampliar para faixa_cr menor que 50, o índice precisou de 1.606 leituras,
contra 128 da varredura. O índice ajuda na consulta seletiva, mas os dados espalhados
penalizam a faixa grande.”
