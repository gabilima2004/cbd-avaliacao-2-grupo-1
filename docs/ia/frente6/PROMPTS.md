# Registro de prompts da frente 6

## Contexto

O trabalho foi desenvolvido em etapas, sempre usando os componentes reais do
repositório e sem pressupor interfaces inexistentes. A conversa de planejamento
original está identificada por `6afce8a9-3958-83e9-875f-d7429444c857`, com o
título `Planejar Partition Hash Join`.

## Prompt 1 Análise e planejamento

Foi solicitado inspecionar o repositório sem modificar código e analisar:

- representação e serialização de registros;
- páginas, Heap File e Buffer Pool;
- pin, unpin, substituição e persistência;
- contagem de E/S física;
- mecanismos de scan;
- joins existentes, testes e padrões;
- arquitetura e contrato do Partition Hash Join;
- dependências, arquivos, etapas e riscos.

O prompt destacou que métodos como `scan_pages()`, `flush()` e `get_stats()`
não poderiam ser presumidos e que suas assinaturas reais deveriam ser
verificadas.

## Prompt 2 Implementação

Foi solicitado implementar um INNER equijoin externo com as fases Partition,
Build, Probe e Output. Os requisitos principais foram:

- relações maiores que a memória;
- hash determinístico;
- partições temporárias pela camada de armazenamento;
- duplicatas com resultado `m × n`;
- semântica SQL para `NULL`;
- escolha do menor lado para build;
- reparticionamento recursivo limitado;
- fallback correto para skew sem progresso;
- respeito ao Buffer Pool e cleanup em exceções;
- código modular e integrado aos componentes existentes;
- apresentação de arquivos, fluxo, memória, limitações e testes reais.

O texto integral desse pedido também foi fornecido como anexo denominado
`Texto colado.txt` durante a conversa de implementação.

## Prompt 3 Testes e validação

Foi solicitado criar uma suíte abrangente cobrindo:

- relações vazias, sem correspondência e com uma correspondência;
- duplicatas em ambos os lados;
- colisões, negativos e `NULL`;
- comparação como multiconjunto;
- partições dentro e fora da memória;
- distribuição uniforme e skew;
- reparticionamento sem progresso e fallback;
- Heap File e Buffer Pool reais;
- persistência, pins, FIFO, LRU e métricas;
- referência independente com SQLite;
- propriedades determinísticas e independência do tamanho do buffer.

O pedido proibiu remover testes ou enfraquecer asserções para obter sucesso.

## Prompt 4 Preparação da bancada

Foi solicitado preparar o PHJ para uma bancada futura com Block Nested Loop e
Sort-Merge, incluindo:

- interface padronizada com R, S, chaves, buffer, memória e opções;
- tempo, leituras, escritas, hits, misses e métricas específicas;
- dados iguais, carga fora do cronômetro e buffer inicialmente controlado;
- LRU, FIFO e seeds fixas;
- consumo integral de geradores;
- política consistente de saída e flush;
- tamanhos pequeno–pequeno, pequeno–grande e grande–grande;
- três repetições;
- registro estruturado compatível com CSV;
- documentação detalhada e comandos reproduzíveis.

## Prompt 5 Revisão de pendências

Foi solicitado conferir o enunciado e decidir quais casos de produção valiam a
pena no contexto acadêmico. A decisão foi não priorizar concorrência,
transações, disco cheio, milhões de registros ou `fsync`, concentrando o esforço
em:

- volumes 248 e 3.968;
- seed 42;
- custo teórico e medido;
- repetição e estabilidade;
- caminhos de acesso;
- evidências de IA e post-mortem;
- integração posterior com os outros joins.

## Prompt 6 Fechamento

Por fim, foi solicitado verificar o que ainda faltava e implementar as
pendências possíveis sem depender dos outros integrantes. Isso motivou:

- campos de custo teórico e caminho de acesso no CSV;
- análise automática de estabilidade;
- resumo estatístico e gráficos;
- campanha ampliada;
- esta pasta de evidências e o post-mortem da frente 6.

## Observação sobre fidelidade

Este arquivo registra a sequência, o escopo e os requisitos materiais dos
prompts. O histórico da conversa preserva a redação integral e os anexos. Não
foram inventados prompts retrospectivos para simular uma evolução diferente da
que realmente ocorreu.
