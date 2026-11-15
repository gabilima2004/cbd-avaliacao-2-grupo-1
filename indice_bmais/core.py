"""B+ secundária em blocos de 4 KiB. Toda página passa por pin/unpin.

Cada chave aparece uma vez nas folhas: um RID inline e uma cadeia de páginas
para os demais RIDs. As listas são ordenadas por RID, sem cópia dos registros.
Não há cache de nós, transações ou manutenção automática da tabela base.
"""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import operator
import struct

from buffer_heap import BLOCK, RID, SLOTS, CorruptFileError, registro

HEADER = 128
_META = struct.Struct('<8sIIIIIQQQQQQ')
_NODE = struct.Struct('<B7xQQ')       # tipo, quantidade, próxima página
_ENTRY = struct.Struct('<qQQQQQ')    # chave, RID inline, cabeça, cauda, quantidade
_PAIR = struct.Struct('<qQ')        # separador, filho à direita
_RID = struct.Struct('<QQ')
_U64 = struct.Struct('<Q')
MAGIC = b'BPLSv001'
LEAF, INNER, POST, FREE = 1, 2, 3, 4
LEAF_CAPACITY = (BLOCK - HEADER) // _ENTRY.size       # 82
INNER_CAPACITY = (BLOCK - HEADER - 8) // _PAIR.size   # 247 (248 filhos)
POST_CAPACITY = (BLOCK - HEADER) // _RID.size        # 248
OPS = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge}


class StaleIndexError(RuntimeError):
    """A tabela mudou sem a correspondente manutenção do índice."""


def check_field(campo):
    if isinstance(campo, bool) or not isinstance(campo, int) or campo not in range(3):
        raise ValueError('campo deve ser 0, 1 ou 2')


def check_key(key):
    if isinstance(key, bool) or not isinstance(key, int) or not -(1 << 63) <= key < (1 << 63):
        raise ValueError('chave deve ser um int64')


def check_rid(rid):
    try:
        bloco, slot = rid.bloco, rid.slot
    except AttributeError as exc:
        raise ValueError('RID deve oferecer bloco e slot') from exc
    if (isinstance(bloco, bool) or not isinstance(bloco, int) or
            not 1 <= bloco < (1 << 64) or isinstance(slot, bool) or
            not isinstance(slot, int) or not 0 <= slot < SLOTS):
        raise ValueError('RID fora da faixa')
    return RID(bloco, slot)


@dataclass
class _Entry:
    key: int
    rid: RID
    head: int = 0
    tail: int = 0
    count: int = 1


@dataclass
class _Leaf:
    entries: list
    next: int = 0


@dataclass
class _Inner:
    keys: list
    children: list


@dataclass
class _Post:
    rids: list
    next: int = 0


class BMaisSecundario:
    """Criar: campo=posição; reabrir: omitir campo (cabeçalho persistente).

    O arquivo de índice deve estar vazio na criação e ser distinto dos dados.
    tabela.get(RID) devolve bytes/None e também deve usar o buffer.
    capacidade reduz a capacidade lógica de folhas/internos APENAS nos testes;
    as páginas continuam tendo 4096 bytes. Padrão: 82 / 247 chaves.
    """

    def __init__(self, buffer, arquivo, tabela, allocate=None, *, campo=None,
                 capacidade=None):
        self.buffer, self.arquivo, self.tabela = buffer, arquivo, tabela
        self.allocate = allocate or buffer.allocate
        if arquivo == getattr(tabela, 'arquivo', None):
            raise ValueError('dados e índice precisam de arquivos distintos')
        fm = getattr(buffer, 'fm', None)
        if fm is not None:
            fm.open(arquivo, 'index')
        if campo is None:
            if capacidade is not None:
                raise ValueError('a capacidade é recuperada do cabeçalho')
            self._load_meta()
            if fm is not None and fm.nblocks(arquivo) != self.blocks:
                raise CorruptFileError('quantidade de blocos diferente do cabeçalho')
        else:
            check_field(campo)
            leaf_cap = LEAF_CAPACITY if capacidade is None else capacidade
            inner_cap = INNER_CAPACITY if capacidade is None else capacidade
            if (isinstance(leaf_cap, bool) or not isinstance(leaf_cap, int) or
                    not 3 <= leaf_cap <= LEAF_CAPACITY or
                    not 3 <= inner_cap <= INNER_CAPACITY):
                raise ValueError('capacidade lógica deve estar entre 3 e 82')
            if fm is not None and fm.nblocks(arquivo):
                raise ValueError('criação exige arquivo vazio')
            self.campo, self.leaf_cap, self.inner_cap = campo, leaf_cap, inner_cap
            self.root = self.first_leaf = 1
            self.free_head = self.records = self.distinct = 0
            self.blocks = 1
            if self.allocate(arquivo) != 0:
                raise ValueError('criação exige arquivo vazio')
            self.root = self.first_leaf = self._new(_Leaf([]))
            self._save_meta()

    def _bytes(self, block):
        page = self.buffer.pin(self.arquivo, block)
        try:
            if len(page) != BLOCK:
                raise CorruptFileError('página deve ter 4096 bytes')
            return bytes(page)
        finally:
            self.buffer.unpin(self.arquivo, block)

    def _write_bytes(self, block, raw):
        if len(raw) != BLOCK:
            raise ValueError('página deve ter 4096 bytes')
        page = self.buffer.pin(self.arquivo, block)
        dirty = False
        try:
            if len(page) != BLOCK:
                raise CorruptFileError('página deve ter 4096 bytes')
            page[:] = raw
            dirty = True
        finally:
            self.buffer.unpin(self.arquivo, block, dirty=dirty)

    def _load_meta(self):
        values = _META.unpack_from(self._bytes(0))
        (magic, version, block, self.campo, self.leaf_cap, self.inner_cap,
         self.root, self.first_leaf, self.free_head, self.records,
         self.distinct, self.blocks) = values
        if (magic != MAGIC or version != 1 or block != BLOCK or
                self.campo not in range(3) or not 3 <= self.leaf_cap <= LEAF_CAPACITY or
                not 3 <= self.inner_cap <= INNER_CAPACITY or self.blocks < 2 or
                not 0 < self.root < self.blocks or not 0 < self.first_leaf < self.blocks or
                not 0 <= self.free_head < self.blocks or self.distinct > self.records):
            raise CorruptFileError('cabeçalho B+ inválido')

    def _save_meta(self):
        raw = bytearray(BLOCK)
        _META.pack_into(raw, 0, MAGIC, 1, BLOCK, self.campo, self.leaf_cap,
                        self.inner_cap, self.root, self.first_leaf, self.free_head,
                        self.records, self.distinct, self.blocks)
        self._write_bytes(0, raw)

    def _read(self, block):
        if not 0 < block < self.blocks:
            raise CorruptFileError('ponteiro de página fora da faixa')
        raw = self._bytes(block)
        kind, count, nxt = _NODE.unpack_from(raw)
        if nxt >= self.blocks:
            raise CorruptFileError('próxima página fora da faixa')
        if kind == LEAF and count <= self.leaf_cap:
            entries = []
            for i in range(count):
                key, b, s, head, tail, total = _ENTRY.unpack_from(raw, HEADER + i * _ENTRY.size)
                entries.append(_Entry(key, RID(b, s), head, tail, total))
            return _Leaf(entries, nxt)
        if kind == INNER and count <= self.inner_cap:
            children = [_U64.unpack_from(raw, HEADER)[0]]
            keys = []
            for i in range(count):
                key, child = _PAIR.unpack_from(raw, HEADER + 8 + i * _PAIR.size)
                keys.append(key)
                children.append(child)
            return _Inner(keys, children)
        if kind == POST and count <= POST_CAPACITY:
            return _Post([RID(*_RID.unpack_from(raw, HEADER + i * _RID.size))
                          for i in range(count)], nxt)
        if kind == FREE and count == 0:
            return nxt
        raise CorruptFileError('tipo ou quantidade inválida na página')

    def _write(self, block, node):
        raw = bytearray(BLOCK)
        if isinstance(node, _Leaf):
            if len(node.entries) > self.leaf_cap:
                raise ValueError('folha excedeu a capacidade')
            _NODE.pack_into(raw, 0, LEAF, len(node.entries), node.next)
            for i, entry in enumerate(node.entries):
                _ENTRY.pack_into(raw, HEADER + i * _ENTRY.size, entry.key,
                                 *entry.rid, entry.head, entry.tail, entry.count)
        elif isinstance(node, _Inner):
            if len(node.keys) > self.inner_cap or len(node.children) != len(node.keys) + 1:
                raise ValueError('nó interno inválido')
            _NODE.pack_into(raw, 0, INNER, len(node.keys), 0)
            _U64.pack_into(raw, HEADER, node.children[0])
            for i, (key, child) in enumerate(zip(node.keys, node.children[1:])):
                _PAIR.pack_into(raw, HEADER + 8 + i * _PAIR.size, key, child)
        elif isinstance(node, _Post):
            if len(node.rids) > POST_CAPACITY:
                raise ValueError('página de RIDs excedeu a capacidade')
            _NODE.pack_into(raw, 0, POST, len(node.rids), node.next)
            for i, rid in enumerate(node.rids):
                _RID.pack_into(raw, HEADER + i * _RID.size, *rid)
        else:
            _NODE.pack_into(raw, 0, FREE, 0, node)
        self._write_bytes(block, raw)

    def _new(self, node):
        if self.free_head:
            block = self.free_head
            nxt = self._read(block)
            if not isinstance(nxt, int):
                raise CorruptFileError('lista de páginas livres inválida')
            self.free_head = nxt
        else:
            block = self.allocate(self.arquivo)
            if block != self.blocks:
                raise ValueError('alocação deve ser contígua')
            self.blocks += 1
        self._write(block, node)
        return block

    def _free(self, block):
        self._write(block, self.free_head)
        self.free_head = block

    def _descend(self, key):
        block, path = self.root, []
        for _ in range(self.blocks):
            node = self._read(block)
            if isinstance(node, _Leaf):
                return block, node, path
            if not isinstance(node, _Inner):
                raise CorruptFileError('página não pertence à árvore')
            pos = bisect_right(node.keys, key)
            path.append((block, pos))  # só identificadores, sem cache de nós
            block = node.children[pos]
        raise CorruptFileError('ciclo na árvore')

    def _minimum(self, block):
        for _ in range(self.blocks):
            node = self._read(block)
            if isinstance(node, _Leaf):
                return node.entries[0].key if node.entries else None
            if not isinstance(node, _Inner):
                break
            block = node.children[0]
        raise CorruptFileError('subárvore inválida')

    def _propagate_min(self, path, key):
        if key is None:
            return
        for block, pos in reversed(path):
            if pos:
                parent = self._read(block)
                parent.keys[pos - 1] = key
                self._write(block, parent)
                return

    def _insert_post(self, entry, rid):
        if not entry.head:
            entry.head = entry.tail = self._new(_Post([rid]))
            return
        block = entry.tail
        node = self._read(block)
        if rid < node.rids[-1]:
            block = entry.head
            node = self._read(block)
            while node.next and rid > node.rids[-1]:
                block = node.next
                node = self._read(block)
        pos = bisect_left(node.rids, rid)
        if pos < len(node.rids) and node.rids[pos] == rid:
            raise ValueError('par (chave, RID) duplicado')
        node.rids.insert(pos, rid)
        if len(node.rids) <= POST_CAPACITY:
            self._write(block, node)
            return
        middle = len(node.rids) // 2
        right = self._new(_Post(node.rids[middle:], node.next))
        if entry.tail == block:
            entry.tail = right
        self._write(block, _Post(node.rids[:middle], right))

    def insert(self, key, rid):
        check_key(key)
        rid = check_rid(rid)
        block, leaf, path = self._descend(key)
        pos = bisect_left([e.key for e in leaf.entries], key)
        if pos < len(leaf.entries) and leaf.entries[pos].key == key:
            entry = leaf.entries[pos]
            if rid == entry.rid:
                raise ValueError('par (chave, RID) duplicado')
            if rid < entry.rid:
                entry.rid, rid = rid, entry.rid
            self._insert_post(entry, rid)
            entry.count += 1
            self._write(block, leaf)
        else:
            leaf.entries.insert(pos, _Entry(key, rid))
            self.distinct += 1
            self._propagate_min(path, leaf.entries[0].key)
            if len(leaf.entries) <= self.leaf_cap:
                self._write(block, leaf)
            else:
                middle = len(leaf.entries) // 2
                right_node = _Leaf(leaf.entries[middle:], leaf.next)
                right = self._new(right_node)
                self._write(block, _Leaf(leaf.entries[:middle], right))
                self._insert_parent(path, block, right_node.entries[0].key, right)
        self.records += 1
        self._save_meta()

    def _insert_parent(self, path, left, key, right):
        while path:
            block, pos = path.pop()
            node = self._read(block)
            node.keys.insert(pos, key)
            node.children.insert(pos + 1, right)
            if len(node.keys) <= self.inner_cap:
                self._write(block, node)
                return
            middle = len(node.keys) // 2
            key = node.keys[middle]
            right = self._new(_Inner(node.keys[middle + 1:], node.children[middle + 1:]))
            self._write(block, _Inner(node.keys[:middle], node.children[:middle + 1]))
            left = block
        self.root = self._new(_Inner([key], [left, right]))

    def _rids(self, entry):
        yield entry.rid
        block = entry.head
        for _ in range(self.blocks):
            if not block:
                return
            node = self._read(block)
            if not isinstance(node, _Post):
                raise CorruptFileError('cadeia de RIDs inválida')
            yield from node.rids  # a página já foi liberada
            block = node.next
        raise CorruptFileError('ciclo na cadeia de RIDs')

    def find_rid(self, key):
        check_key(key)
        _, leaf, _ = self._descend(key)
        pos = bisect_left([e.key for e in leaf.entries], key)
        if pos < len(leaf.entries) and leaf.entries[pos].key == key:
            yield from self._rids(leaf.entries[pos])

    def _entries(self, limite, operador):
        check_key(limite)
        if operador not in OPS:
            raise ValueError('operador deve ser <, <=, > ou >=')
        block = self.first_leaf
        if operador in ('>', '>='):
            block, _, _ = self._descend(limite)
        for _ in range(self.blocks):
            if not block:
                return
            leaf = self._read(block)
            if not isinstance(leaf, _Leaf):
                raise CorruptFileError('encadeamento de folhas inválido')
            for entry in leaf.entries:
                if OPS[operador](entry.key, limite):
                    yield entry
                elif operador in ('<', '<='):
                    return
            block = leaf.next
        raise CorruptFileError('ciclo nas folhas')

    def _rows(self, entry):
        for rid in self._rids(entry):
            raw = self.tabela.get(rid)
            if raw is None or len(raw) != registro.RECORD or registro.field(raw, self.campo) != entry.key:
                raise StaleIndexError(f'índice desatualizado para {rid}')
            yield raw

    def find(self, key):
        check_key(key)
        _, leaf, _ = self._descend(key)
        pos = bisect_left([e.key for e in leaf.entries], key)
        if pos < len(leaf.entries) and leaf.entries[pos].key == key:
            yield from self._rows(leaf.entries[pos])

    def find_range_rid(self, limite, operador):
        for entry in self._entries(limite, operador):
            yield from self._rids(entry)

    def find_range(self, limite, operador):
        for entry in self._entries(limite, operador):
            yield from self._rows(entry)

    def _remove_rid(self, entry, rid):
        if rid == entry.rid:
            if entry.head:
                node = self._read(entry.head)
                entry.rid = node.rids.pop(0)
                if node.rids:
                    self._write(entry.head, node)
                else:
                    old, entry.head = entry.head, node.next
                    if not entry.head:
                        entry.tail = 0
                    self._free(old)
            entry.count -= 1
            return True
        previous, block = 0, entry.head
        while block:
            node = self._read(block)
            pos = bisect_left(node.rids, rid)
            if pos < len(node.rids) and node.rids[pos] == rid:
                node.rids.pop(pos)
                if node.rids:
                    self._write(block, node)
                else:
                    if previous:
                        prev = self._read(previous)
                        prev.next = node.next
                        self._write(previous, prev)
                    else:
                        entry.head = node.next
                    if entry.tail == block:
                        entry.tail = previous
                    self._free(block)
                entry.count -= 1
                return True
            if node.rids and rid < node.rids[-1]:
                return False
            previous, block = block, node.next
        return False

    def delete(self, key, rid=None):
        """Remove o par indicado; sem RID, remove toda a entrada. Preserva os dados."""
        check_key(key)
        if rid is not None:
            rid = check_rid(rid)
        block, leaf, path = self._descend(key)
        pos = bisect_left([e.key for e in leaf.entries], key)
        if pos == len(leaf.entries) or leaf.entries[pos].key != key:
            return False
        entry = leaf.entries[pos]
        removed = entry.count if rid is None else 1
        if rid is None:
            posting = entry.head
            while posting:
                nxt = self._read(posting).next
                self._free(posting)
                posting = nxt
            entry.count = 0
        elif not self._remove_rid(entry, rid):
            return False
        self.records -= removed
        if entry.count:
            self._write(block, leaf)
        else:
            leaf.entries.pop(pos)
            self.distinct -= 1
            self._write(block, leaf)
            self._rebalance(block, leaf, path)
        self._save_meta()
        return True

    def _rebalance(self, block, node, path):
        while path:
            parent_block, pos = path.pop()
            parent = self._read(parent_block)
            is_leaf = isinstance(node, _Leaf)
            minimum = (self.leaf_cap + 1) // 2 if is_leaf else self.inner_cap // 2
            size = len(node.entries) if is_leaf else len(node.keys)
            new_min = self._minimum(block)
            if pos and new_min is not None:
                parent.keys[pos - 1] = new_min
            if size >= minimum:
                self._write(parent_block, parent)
                self._propagate_min(path, self._minimum(parent.children[0]))
                return
            left_block = parent.children[pos - 1] if pos else 0
            right_block = parent.children[pos + 1] if pos + 1 < len(parent.children) else 0
            left = self._read(left_block) if left_block else None
            right = self._read(right_block) if right_block else None
            left_size = len(left.entries) if is_leaf and left else len(left.keys) if left else 0
            right_size = len(right.entries) if is_leaf and right else len(right.keys) if right else 0
            if left and left_size > minimum:
                if is_leaf:
                    node.entries.insert(0, left.entries.pop())
                    parent.keys[pos - 1] = node.entries[0].key
                else:
                    node.keys.insert(0, parent.keys[pos - 1])
                    node.children.insert(0, left.children.pop())
                    parent.keys[pos - 1] = left.keys.pop()
                self._write(left_block, left)
            elif right and right_size > minimum:
                if is_leaf:
                    node.entries.append(right.entries.pop(0))
                    parent.keys[pos] = right.entries[0].key
                    if pos:
                        parent.keys[pos - 1] = node.entries[0].key
                else:
                    node.keys.append(parent.keys[pos])
                    node.children.append(right.children.pop(0))
                    parent.keys[pos] = right.keys.pop(0)
                self._write(right_block, right)
            else:
                if left:
                    if is_leaf:
                        left.entries.extend(node.entries)
                        left.next = node.next
                    else:
                        left.keys.extend([parent.keys[pos - 1], *node.keys])
                        left.children.extend(node.children)
                    self._write(left_block, left)
                    self._free(block)
                    parent.children.pop(pos)
                    parent.keys.pop(pos - 1)
                else:
                    if is_leaf:
                        node.entries.extend(right.entries)
                        node.next = right.next
                    else:
                        node.keys.extend([parent.keys[pos], *right.keys])
                        node.children.extend(right.children)
                    self._write(block, node)
                    self._free(right_block)
                    parent.children.pop(pos + 1)
                    parent.keys.pop(pos)
                if parent_block == self.root and not parent.keys:
                    self.root = parent.children[0]
                    self._free(parent_block)
                    return
                self._write(parent_block, parent)
                block, node = parent_block, parent
                continue
            self._write(block, node)
            self._write(parent_block, parent)
            self._propagate_min(path, self._minimum(parent.children[0]))
            return

    def validate(self, check_records=False):
        """Auditoria fora da medição; usa conjuntos/listas proporcionais ao índice."""
        used, leaves, depths = set(), [], set()
        counts = dict(leaf_pages=0, internal_pages=0, posting_pages=0, free_pages=0,
                      records=0, distinct_keys=0, total_blocks=self.blocks)

        def visit(block, depth):
            if block in used:
                raise CorruptFileError('página repetida/ciclo')
            used.add(block)
            node = self._read(block)
            if isinstance(node, _Leaf):
                keys = [e.key for e in node.entries]
                if keys != sorted(set(keys)):
                    raise CorruptFileError('chaves repetidas ou fora de ordem')
                if block != self.root and len(keys) < (self.leaf_cap + 1) // 2:
                    raise CorruptFileError('folha abaixo da ocupação mínima')
                leaves.append((block, node.next))
                depths.add(depth)
                counts['leaf_pages'] += 1
                counts['distinct_keys'] += len(keys)
                for entry in node.entries:
                    previous, total, posting, tail = check_rid(entry.rid), 1, entry.head, 0
                    while posting:
                        if posting in used:
                            raise CorruptFileError('cadeia de RIDs repetida/cíclica')
                        used.add(posting)
                        post = self._read(posting)
                        if not isinstance(post, _Post) or not post.rids:
                            raise CorruptFileError('página de RIDs vazia/inválida')
                        for rid in post.rids:
                            check_rid(rid)
                            if rid <= previous:
                                raise CorruptFileError('RIDs repetidos ou fora de ordem')
                            previous = rid
                            total += 1
                        counts['posting_pages'] += 1
                        tail, posting = posting, post.next
                    if total != entry.count or tail != entry.tail:
                        raise CorruptFileError('contagem/cauda de RIDs incorreta')
                    counts['records'] += total
                    if check_records:
                        for _ in self._rows(entry):
                            pass
                return (keys[0], keys[-1]) if keys else (None, None)
            if not isinstance(node, _Inner) or not node.keys:
                raise CorruptFileError('nó interno vazio/inválido')
            if block != self.root and len(node.keys) < self.inner_cap // 2:
                raise CorruptFileError('nó interno abaixo da ocupação mínima')
            counts['internal_pages'] += 1
            ranges = [visit(child, depth + 1) for child in node.children]
            if any(lo is None for lo, _ in ranges):
                raise CorruptFileError('subárvore vazia')
            if node.keys != [lo for lo, _ in ranges[1:]]:
                raise CorruptFileError('separador diferente do mínimo do filho')
            if any(ranges[i][1] >= ranges[i + 1][0] for i in range(len(ranges) - 1)):
                raise CorruptFileError('subárvores sobrepostas')
            return ranges[0][0], ranges[-1][1]

        visit(self.root, 1)
        if len(depths) != 1 or self.first_leaf != leaves[0][0]:
            raise CorruptFileError('altura ou primeira folha incorreta')
        for i, (_, nxt) in enumerate(leaves):
            expected = leaves[i + 1][0] if i + 1 < len(leaves) else 0
            if nxt != expected:
                raise CorruptFileError('folhas não encadeadas em ordem')
        free = self.free_head
        while free:
            if free in used:
                raise CorruptFileError('página livre ainda referenciada/ciclo')
            used.add(free)
            free = self._read(free)
            if not isinstance(free, int):
                raise CorruptFileError('lista de livres inválida')
            counts['free_pages'] += 1
        if (len(used) + 1 != self.blocks or counts['records'] != self.records or
                counts['distinct_keys'] != self.distinct):
            raise CorruptFileError('página perdida ou contagens divergentes')
        return dict(counts, height=next(iter(depths)), leaf_capacity=self.leaf_cap,
                    internal_fanout=self.inner_cap + 1)
