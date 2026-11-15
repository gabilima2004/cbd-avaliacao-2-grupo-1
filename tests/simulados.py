"""Simulados estritos: disco em RAM, páginas por cópia e dirty obrigatório."""
from hash_externo.core import BLOCK, HEADER, SIZE, SLOTS


class BufferSimulado:
    def __init__(self):
        self.disk = {}
        self.pinned = {}
        self.requests = self.writes = 0
        self.max_pinned = 0

    def allocate(self, file):
        pages = self.disk.setdefault(file, [])
        pages.append(bytes(BLOCK))
        return len(pages)-1

    def pin(self, file, block):
        key = file, block
        if key in self.pinned:
            raise AssertionError('pin duplo')
        self.requests += 1
        page = bytearray(self.disk[file][block])
        self.pinned[key] = page
        self.max_pinned = max(self.max_pinned, len(self.pinned))
        return page

    def unpin(self, file, block, dirty=False):
        page = self.pinned.pop((file, block))
        if dirty:
            self.disk[file][block] = bytes(page)
            self.writes += 1
        elif bytes(page) != self.disk[file][block]:
            raise AssertionError('alteração sem dirty')


class HeapSimulado:
    def __init__(self, buffer, arquivo='heap'):
        self.buffer, self.arquivo = buffer, arquivo
        buffer.allocate(arquivo)
        self.block = buffer.allocate(arquivo)
        self.slot = 0

    def insert(self, raw):
        from hash_externo import RID
        if self.slot == SLOTS:
            self.block = self.buffer.allocate(self.arquivo)
            self.slot = 0
        rid = RID(self.block, self.slot)
        p = self.buffer.pin(self.arquivo, rid.bloco)
        try:
            start = HEADER + SIZE*rid.slot
            p[start:start+SIZE] = raw
            p[8+rid.slot] = 1
        finally:
            self.buffer.unpin(self.arquivo, rid.bloco, dirty=True)
        self.slot += 1
        return rid

    def get(self, rid):
        p = self.buffer.pin(self.arquivo, rid.bloco)
        try:
            start = HEADER + SIZE*rid.slot
            return bytes(p[start:start+SIZE]) if p[8+rid.slot] else None
        finally:
            self.buffer.unpin(self.arquivo, rid.bloco)
