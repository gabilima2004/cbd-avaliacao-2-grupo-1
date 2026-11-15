# Frente 4 — revisão e evidências de IA

Texto de apoio à revisão e à apresentação pelo integrante. Os números abaixo
vieram das execuções preservadas; não atribuem revisão manual ao aluno.

## Registro das etapas

1. Conferência da `main` limpa (`336c0d8`), README, PDF original, plano DOCX e
   código das frentes 1/3. Criação da branch `frente-4-bmais-selecoes`.
2. Linha de base: `testes_base.txt`, **35 testes OK**.
3. Primeira implementação de cada arquivo, copiada sem alterações de bytes para
   `codigo_bruto_v1/` imediatamente após criá-lo e antes da revisão/execução desse
   arquivo. `SHA256SUMS.txt` permite verificar essas cópias. O script da bancada
   foi criado depois da revisão inicial dos módulos; sua cópia é a primeira
   versão desse script, não uma alegação de que a bancada rodou no código bruto.
4. Primeira suíte: `testes_v1.txt`, **49 testes, três falhas**, código de saída 1.
5. Revisão e reprodução do defeito de limite `None`:
   `casos_falhos/limite_none.py`, saídas `falha_limite_none_antes.txt` e
   `falha_limite_none_depois.txt`. Mesmo caso e asserções, antes/depois da correção.
6. Testes corrigidos e duas verificações acrescentadas (limites e churn):
   `testes_v2.txt`, **51 testes OK**, incluindo os 35 originais.
7. Bancada após a correção da API: `bench_primeira_execucao.txt`,
   `bench_v1.csv` e `bench_v1.txt`, **84 cenários, 252 medições, EXIT_CODE=0**.
   O sufixo v1 nestes arquivos é da bancada; não mede uma melhoria de desempenho
   entre a versão bruta e a corrigida do índice.

O prompt desta solicitação está integralmente em `prompt.txt`.
O diff `revisao_v1_v2.diff` e o manifesto de hashes `manifesto.json`
identificam código, evidências e quais arquivos mudaram após a cópia inicial.
O arquivo `.gitattributes` preserva os bytes das evidências e fixa LF no código
auditado, para que a conversão automática de quebras de linha do Git no Windows
não invalide os hashes após um clone ou checkout.

## Problemas observados e decisões de revisão

| Achado real | Evidência e tratamento |
|---|---|
| A API do índice aceitava `None` como limite de faixa e devolvia todos os registros. | O parâmetro interno usava `None` como sentinela de varredura. A reprodução falhou nas duas APIs (`find_range` e `find_range_rid`). Removida a sentinela: toda consulta valida int64 e operador. Mesmo caso passou depois; a suíte mantém a regressão. |
| Teste comparava RID de `hash_externo` (dataclass) com RID de `buffer_heap` (NamedTuple). | Os campos eram idênticos, mas a igualdade entre classes não era. Corrigida a expectativa para o RID normalizado da frente 1. Nenhum formato compartilhado foi alterado. |
| Teste exigia que uma nova ordem de inserção ocupasse o mesmo número de páginas. | Para capacidades 4 e 5, a árvore precisou de uma e duas páginas adicionais, legitimamente por splits/ocupação. O teste de reuso repete agora a mesma sequência, após excluir também os registros base. A auditoria exige que não sobrem páginas perdidas. |
| Aviso de arquivo não fechado em `tests/test_buffer_heap.py:205`. | Preexistente, reproduzido nos 35 testes de base. Não foi modificado código de outra frente para ocultá-lo. |
| Import não usado em `selecoes/core.py`. | Removido na revisão; sem efeito funcional ou alegação de ganho. |

O defeito corrigido é de validação da consulta. As consultas com limites válidos
e as principais invariantes já passavam na primeira versão; não foi introduzida
uma falha artificial para fabricar um ganho. Não se afirma que a versão final
executa as consultas válidas mais rápido por causa dessa correção.

## Pontos conferidos no código

- Separadores são mínimos dos filhos à direita; buscas descem por ponteiros de
  blocos. O bisect atua nas chaves de um nó, não numa relação carregada em memória.
- Folhas ordenadas, encadeadas e com chaves distintas. Valores repetidos viram
  RIDs adicionais, incluindo o caso com 763 RIDs da mesma chave.
- Splits de folha/interno chegam à raiz; remoção redistribui/funde, corrige
  mínimos dos ancestrais, reduz raiz e reutiliza páginas livres.
- `validate` percorre árvore, cadeia de folhas, RIDs e free list, verifica
  ocupação mínima/altura uniforme, contagens e ausência de páginas perdidas.
- Leituras/escritas passam por `pin/unpin`, dirty após escrita, liberação em
  `finally`; os testes simulados rejeitam escrita sem dirty e pin duplo.
- Nenhum yield retém pin. Arquivos reais reabrem após flush/fechamento e
  expulsões dirty, tanto em LRU quanto FIFO e com buffer de três páginas.
- A camada de seleção não materializa a resposta, usa o caminho explícito e
  registra o scan efetivo do hash para faixas.
- Igualdade UNIQUE secundária no heap usa o hash da frente 3, conforme §4.2.

## O que o estresse demonstrou

Com capacidade lógica 3/4/5 foram exercitadas árvores de altura pelo menos quatro,
exclusão até esvaziar a árvore e recarga usando páginas liberadas. Separadamente,
11 mil chaves com folhas de 82 e fan-out 248 produziram altura três; portanto,
a propagação até a raiz não foi testada apenas com capacidades artificiais.
O caso de 3.968 registros remove/reinsere 1.984, confere todos os operadores e
102 valores de igualdade no SQLite com LRU/FIFO. O churn intercala cinco mil
inserções/exclusões de pares/grupos e verifica invariantes a cada 53 operações.

Na bancada, os 84 cenários tiveram três repetições frias e a mesma quantidade
de leituras físicas (variação 0%). Não se aplica esse resultado à estabilidade
do tempo de relógio. Nenhum cenário usou um simulado para gerar contadores
físicos. A comparação completa das tuplas pertence à suíte; a bancada usa
contagem/checksum de multiconjunto em fluxo.

## Limitações e integração ainda necessária

- A B+ primária está ausente. Seu dublê testa apenas a delegação da seleção,
  inclusive caminho e resultado; não certifica persistência ou desempenho da frente 2.
- O enunciado exige clustering secundário em B+/hash e prevalece sobre o adiamento
  do plano. O código não reorganiza essas tabelas: os cenários continuam pendentes
  de comprovar o armazenamento físico, com RIDs estáveis/atualizados.
- `HashComRID` depende do layout `HASHv001`; a frente 3 deve alinhar o futuro
  método nativo `get(RID)`. A frente 2 precisará do mesmo contrato para resolver
  referências, sem alterar as assinaturas de seleção do plano.
- Manutenção da tabela e dos índices é explícita e não atômica. O detector de
  índice desatualizado não reconhece reutilização de RID com a mesma chave.
- Cada valor repetido usa páginas de RIDs dedicadas: para grupos pequenos, há
  desperdício de espaço; páginas parcialmente vazias não são compactadas. O CSV
  contabiliza esse custo, que é perceptível mesmo na configuração pequena.
- Não há WAL, recuperação após falha, concorrência ou segurança para modificar
  a estrutura durante uma consulta. Há validações de formato e auditoria, mas não
  uma prova de tolerância a toda corrupção possível.
- A bancada desta frente cobre seleções; integração com catálogo/gerador geral,
  junções e relatório consolidado pertence às frentes correspondentes.

Todos os testes passarem dá evidência para esses casos e parâmetros; não prova
ausência de defeitos. O integrante deve executar os comandos e revisar o trecho
de código e a interpretação dos resultados antes da entrega/apresentação.
