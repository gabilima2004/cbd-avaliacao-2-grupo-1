"""Seleções por scan, organização primária ou índice secundário.

Não materializa a relação. O catálogo/integrador fornece o mapa campo -> índice.
O hash secundário UNIQUE da frente 3 é reutilizado para igualdade no heap.
"""
from buffer_heap import registro
from hash_externo.core import HEADER, SIZE, SLOTS
from indice_bmais.core import OPS, check_field, check_key, check_rid


class TabelaSelecao:
    def __init__(self, tabela, secundarios=None):
        self.tabela = tabela
        self.secundarios = dict(secundarios or {})
        for campo, indice in self.secundarios.items():
            check_field(campo)
            index_field = getattr(indice, 'campo', campo)
            if index_field != campo:
                raise ValueError('campo do índice difere do campo no catálogo')
        self.last_access = None

    def scan_pages(self):
        return self.tabela.scan_pages()

    def _scan(self, campo, valor, op):
        self.last_access = 'scan'
        for page in self.scan_pages():
            for raw in page:
                if op(registro.field(raw, campo), valor):
                    yield raw

    def _primary(self, method, *args):
        self.last_access = 'primary'
        iterator = getattr(self.tabela, method)(*args, 'primary')
        try:
            for raw in iterator:
                self.last_access = getattr(self.tabela, 'last_access', 'primary')
                yield raw
        finally:
            self.last_access = getattr(self.tabela, 'last_access', 'primary')
            close = getattr(iterator, 'close', None)
            if close:
                close()

    def _secondary(self, campo, method, *args):
        self.last_access = 'secondary'
        indice = self.secundarios.get(campo)
        if indice is None or not hasattr(indice, method):
            raise ValueError('índice secundário não oferece este caminho')
        yield from getattr(indice, method)(*args)

    @staticmethod
    def _check(campo, valor, acesso):
        check_field(campo)
        check_key(valor)
        if acesso not in ('scan', 'primary', 'secondary'):
            raise ValueError('acesso deve ser scan, primary ou secondary')

    def find_equal(self, campo, valor, acesso='scan'):
        self._check(campo, valor, acesso)
        if acesso == 'scan':
            return self._scan(campo, valor, lambda a, b: a == b)
        if acesso == 'primary':
            return self._primary('find_equal', campo, valor)
        return self._secondary(campo, 'find', valor)

    def find_range(self, campo, limite, operador, acesso='scan'):
        self._check(campo, limite, acesso)
        if operador not in OPS:
            raise ValueError('operador deve ser <, <=, > ou >=')
        if acesso == 'scan':
            return self._scan(campo, limite, OPS[operador])
        if acesso == 'primary':
            return self._primary('find_range', campo, limite, operador)
        return self._secondary(campo, 'find_range', limite, operador)


class HashComRID:
    """Adaptador da frente 4: adiciona get(RID) ao hash existente sem modificá-lo.

    Usa o layout público da frente 3, inclusive ocupação e overflow. RIDs de hash
    ficam estáveis enquanto não houver reorganização. O integrador mantém os
    índices nas inserções/exclusões e deve reconstruí-los se os RIDs mudarem.
    """

    def __init__(self, tabela):
        self.tabela = tabela
        self.buffer, self.arquivo = tabela.buffer, tabela.arquivo

    def __getattr__(self, name):
        return getattr(self.tabela, name)

    def get(self, rid):
        rid = check_rid(rid)
        # O bloco 0 guarda o total de blocos no offset 20 (contrato HASHv001).
        import struct
        meta = self.buffer.pin(self.arquivo, 0)
        try:
            total = struct.unpack_from('<I', meta, 20)[0]
        finally:
            self.buffer.unpin(self.arquivo, 0)
        if rid.bloco >= total:
            raise IndexError('RID fora do arquivo hash')
        page = self.buffer.pin(self.arquivo, rid.bloco)
        try:
            if not page[8 + rid.slot]:
                return None
            start = HEADER + SIZE * rid.slot
            return bytes(page[start:start + SIZE])
        finally:
            self.buffer.unpin(self.arquivo, rid.bloco)
