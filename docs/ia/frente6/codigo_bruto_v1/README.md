# Código bruto e referências imutáveis

O primeiro estado versionado da implementação está no commit:

```text
4b70881 feature: implementacao do partition hash join
```

Esse commit é preservado pelo Git e pode ser inspecionado sem depender da versão
atual:

```powershell
git show 4b70881:partition_hash_join/core.py
git show 4b70881:partition_hash_join/__init__.py
git show 4b70881:tests/test_partition_hash_join.py
git diff 4b70881..HEAD -- partition_hash_join tests/test_partition_hash_join.py
```

Identificadores dos blobs originais:

| Blob | Arquivo |
|---|---|
| `7dc3615a4e94707e8615f2931e852f5d446628c5` | `partition_hash_join/core.py` |
| `39188f66eedec3c55e2b896b9bdbf0e08de2e677` | `partition_hash_join/__init__.py` |
| `5c7edf27ba4d8c5fef107e1b37e920816c3b3a52` | `tests/test_partition_hash_join.py` |
| `d2a6ea43376265bc0ebb988d2283aaea22f8f57f` | `buffer_heap/buffer.py` |
| `397d3ae1c603958c0281e1414302d4cbbabbceee` | `buffer_heap/files.py` |
| `e21e5afd7ddb3cb196c117695d7f18064ee30718` | `buffer_heap/heap.py` |

A ampliação da validação está no commit:

```text
3e43787 feat: testes do hash join
```

Blobs dessa etapa:

| Blob | Arquivo |
|---|---|
| `4cca1fe9af0efe94d3d3045c56cdeb37840f419a` | `partition_hash_join/core.py` |
| `9d43a8f48f7e6ebce7ce09bbbf01a08c4ef541b0` | `tests/test_partition_hash_join_validation.py` |

A bancada inicial está no commit:

```text
7c4384d feature: implementacao da bancada
```

Usar os objetos Git como fonte evita uma cópia divergente ou alterada do código
bruto. Os identificadores acima permitem verificar exatamente os bytes que
foram versionados em cada etapa.
