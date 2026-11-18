# Revisão e post-mortem da frente 6

## Escopo

A frente 6 implementou o Partition Hash Join, sua validação e a preparação
inicial da bancada. O algoritmo usa Heap File e Buffer Pool reais, materializa
partições temporárias e produz a saída por um gerador.

## Evolução observada

### Escolha do lado de build

A primeira política comparava apenas o número de páginas. Um teste de skew usou
partições de 70 e 65 registros, ambas com três páginas, e mostrou que o lado de
70 registros era escolhido por empate. O resultado continuava correto, mas a
escolha contrariava a intenção de usar o menor lado.

A correção passou a comparar `(páginas, registros)`, escolhendo 65 registros no
empate. O teste permaneceu exigindo o menor build, sem ser enfraquecido.

### Observabilidade da memória

O limite `(B - 2) × 31` existia no fluxo, mas não havia uma métrica direta para
prová-lo. Foi adicionada `max_build_records`, atualizada no build normal e no
fallback. Os testes passaram a verificar explicitamente o pico lógico.

### Reentrância

A revisão identificou que dois geradores iniciados sobre a mesma instância
poderiam disputar o workspace interno. A instância agora rejeita uma segunda
execução ativa e libera o estado no `finally`.

### Temporários e métricas

Foi necessário adicionar expulsão seletiva de frames e remoção segura de
arquivos temporários. A primeira solução preservava cada nome removido nos mapas
do FileManager, o que faria esses mapas crescerem em campanhas longas. A versão
revisada agrega os contadores por tipo e remove os nomes aposentados.

### Cleanup do consumidor

A bancada fecha explicitamente o gerador mesmo se o cálculo do resumo da saída
falhar. Isso evita temporários e pins presos em adaptadores futuros.

## Validação

A bateria cobre correção como multiconjunto, SQLite, duplicatas, `NULL`, chaves
negativas, colisões, relações vazias, buffer mínimo, LRU, FIFO, skew,
reparticionamento, fallback, exceções, múltiplas execuções, métricas e cleanup.

A suíte final deve ser reproduzida com:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
```

## Bancada

A primeira campanha oficial executou 18 combinações e validou todas as saídas.
A E/S foi idêntica entre repetições equivalentes. O tempo de relógio, entretanto,
variou mais que 5%, mostrando que três repetições curtas não bastam para uma
conclusão temporal.

Em vez de ocultar essa variação, foi implementado um analisador que calcula
média, desvio padrão, coeficiente de variação, amplitude relativa, consistência
de E/S e diferença entre custo teórico e medido.

## Custo teórico

A referência base registrada é:

```text
3 × (blocos de R + blocos de S)
```

Ela não inclui cabeçalhos dos Heap Files temporários, atualizações de metadados,
expulsões dirty nem reparticionamento. Essas diferenças devem ser explicadas no
relatório, não tratadas automaticamente como defeito.

## Limitações assumidas

- memória limitada no modelo lógico de páginas, não no RSS do Python;
- ausência de transações, concorrência e recuperação de falha;
- saída consumida e resumida, mas não materializada;
- sem `fsync` e sem limpeza do cache do sistema operacional;
- PHJ usa scan mesmo quando outro acesso estiver disponível;
- integração com Block Nested Loop e Sort-Merge depende das implementações dos
  demais integrantes.

## Conclusão

O núcleo do PHJ está funcionalmente pronto. Os principais ganhos da revisão
foram tornar explícitos o limite de memória, o ciclo de vida dos temporários e a
reprodutibilidade da bancada. A etapa restante do grupo é conectar os outros
joins ao contrato comum e executar a matriz comparativa completa.
