"""Gera os gráficos da Frente 1 em SVG (biblioteca padrão) a partir dos CSV/TXT de docs/ia/frente1.

Uso: python tests/gerar_graficos_frente1.py
Saída: docs/graficos_frente1/*.svg
"""
import csv
import os
import re
from xml.sax.saxutils import escape

RAIZ = os.path.join(os.path.dirname(__file__), '..')
ENTRADA = os.path.join(RAIZ, 'docs', 'ia', 'frente1')
SAIDA = os.path.join(RAIZ, 'docs', 'graficos_frente1')

SURFACE, INK, INK2, MUTED, GRID, BASE = '#fcfcfb', '#0b0b0b', '#52514e', '#898781', '#e1e0d9', '#c3c2b7'
AZUL, LARANJA = '#2a78d6', '#eb6834'      # séries 1 e 2 da paleta validada (modo claro)
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def num(v):
    return f'{round(v):,}'.replace(',', '.')


def teto_redondo(v):
    """Menor valor 'redondo' (1, 2, 2.5, 4, 5 ou 10 × 10^k) que comporta v; assim as marcas do eixo ficam legíveis."""
    e = 1
    while e * 10 <= v:
        e *= 10
    while e > 1 and e > v:
        e //= 10
    for m in (1, 2, 2.5, 4, 5, 10):
        if m * e >= v:
            return m * e
    return 10 * e


class Svg:
    def __init__(self, w, h, titulo, subtitulo):
        self.w, self.h, self.p = w, h, []
        self.p.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
                      f'role="img" aria-label="{escape(titulo)}" font-family=\'{FONT}\'>')
        self.p.append(f'<rect width="{w}" height="{h}" fill="{SURFACE}"/>')
        self.text(24, 30, titulo, 17, INK, weight=600)
        self.text(24, 50, subtitulo, 12, INK2)

    def text(self, x, y, s, size, fill, anchor='start', weight=400):
        self.p.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
                      f'font-weight="{weight}">{escape(s)}</text>')

    def rect(self, x, y, w, h, fill, rx=2):
        self.p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(h, 0):.1f}" rx="{rx}" fill="{fill}"/>')

    def line(self, x1, y1, x2, y2, stroke, width=1):
        self.p.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{width}"/>')

    def legenda(self, x, y, itens):
        for nome, cor in itens:
            self.rect(x, y - 10, 12, 12, cor)
            self.text(x + 18, y, nome, 12, INK2)
            x += 24 + 8 * len(nome)

    def salvar(self, nome):
        os.makedirs(SAIDA, exist_ok=True)
        caminho = os.path.join(SAIDA, nome)
        with open(caminho, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(self.p) + '\n</svg>\n')
        return caminho


def painel(svg, x0, y0, w, h, titulo, grupos, cores, ymax):
    """Barras agrupadas. grupos = [(rotulo_do_grupo|None, [(serie, valor), ...])]."""
    svg.text(x0 + w / 2, y0 - 12, titulo, 13, INK, anchor='middle', weight=600)
    ymax = teto_redondo(ymax)
    base = y0 + h
    for frac in (0.5, 1.0):
        y = base - h * frac
        svg.line(x0, y, x0 + w, y, GRID)
        svg.text(x0 - 6, y + 4, num(ymax * frac), 10, MUTED, anchor='end')
    svg.line(x0, base, x0 + w, base, BASE)
    n = len(grupos)
    larg_grupo = w / n
    for gi, (rotulo, barras) in enumerate(grupos):
        k = len(barras)
        bw = min(44, (larg_grupo - 16) / k - 2)
        gx = x0 + gi * larg_grupo + (larg_grupo - k * bw - (k - 1) * 2) / 2
        for bi, (serie, valor) in enumerate(barras):
            x = gx + bi * (bw + 2)
            alt = h * valor / ymax
            svg.rect(x, base - alt, bw, alt, cores[serie])
            svg.text(x + bw / 2, base - alt - 5, num(valor), 11, INK, anchor='middle', weight=600)
            svg.text(x + bw / 2, base + 14, serie, 10, INK2, anchor='middle')
        if rotulo:
            svg.text(x0 + gi * larg_grupo + larg_grupo / 2, base + 30, rotulo, 11, INK2, anchor='middle')


def ler_csv():
    dados = {}
    with open(os.path.join(ENTRADA, 'lru_vs_fifo.csv'), encoding='utf-8') as fh:
        for r in csv.DictReader(fh):
            dados[(r['carga'], int(r['pool']), r['politica'])] = float(r['leituras_fisicas'])
    return dados


def ler_bench(nome):
    out = {}
    with open(os.path.join(ENTRADA, nome), encoding='utf-8') as fh:
        for linha in fh.read().splitlines()[1:]:
            m = re.match(r'^(.*?)\s+(LRU|FIFO)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)$', linha)
            if m and m.group(2) == 'LRU':
                out[m.group(1).strip()] = int(m.group(5))
    return out


def grafico_politicas(d):
    cargas = [('scan_ciclico', 'Varredura cíclica'), ('loop_24_blocos', 'Laço de 24 blocos'),
              ('get_uniforme', 'Acesso uniforme'), ('get_80_20', 'Acesso 80/20'),
              ('scan_com_8_quentes', 'Varredura + 8 quentes')]
    svg = Svg(1000, 330, 'LRU × FIFO com o pool base de 32 páginas',
              'Leituras físicas por carga (menos é melhor); heap de 128 blocos, buffer frio, média de 3 sementes')
    svg.legenda(24, 76, [('LRU', AZUL), ('FIFO', LARANJA)])
    cores = {'LRU': AZUL, 'FIFO': LARANJA}
    w, gap, x = 150, 48, 56
    for chave, titulo in cargas:
        valores = [(p, d[(chave, 32, p)]) for p in ('LRU', 'FIFO')]
        ymax = max(v for _, v in valores) * 1.25
        painel(svg, x, 130, w, 150, titulo, [(None, valores)], cores, ymax)
        x += w + gap
    svg.text(24, 318, 'Cada painel tem a própria escala. Empate nas três primeiras cargas; o LRU ganha com localidade (painéis 4 e 5).', 11, MUTED)
    return svg.salvar('lru_vs_fifo_pool32.svg')


def grafico_pools(d):
    svg = Svg(1000, 340, 'Efeito do tamanho do pool nas cargas com localidade',
              'Leituras físicas (menos é melhor) com 16, 32 e 64 páginas; mesma carga e mesmos dados')
    svg.legenda(24, 76, [('LRU', AZUL), ('FIFO', LARANJA)])
    cores = {'LRU': AZUL, 'FIFO': LARANJA}
    for x, chave, titulo in ((60, 'get_80_20', 'Acesso 80/20 (5.000 leituras)'),
                             (550, 'scan_com_8_quentes', 'Varredura + 8 páginas quentes')):
        grupos = [(f'{p} páginas', [(s, d[(chave, p, s)]) for s in ('LRU', 'FIFO')]) for p in (16, 32, 64)]
        ymax = max(v for _, bs in grupos for _, v in bs) * 1.2
        painel(svg, x, 130, 400, 150, titulo, grupos, cores, ymax)
    svg.text(24, 328, 'No acesso 80/20, mais páginas aumentam a vantagem do LRU. Na varredura com páginas quentes, a vantagem diminui com mais páginas.', 11, MUTED)
    return svg.salvar('lru_vs_fifo_pools.svg')


def grafico_v1_v2():
    v1, v2 = ler_bench('bench_v1.txt'), ler_bench('bench_v2.txt')
    svg = Svg(1000, 330, 'Heap: código bruto da IA (v1) × refatorado (v2)',
              'Leituras físicas no estresse com buffer de 32 páginas e semente 42 (menos é melhor)')
    svg.legenda(24, 76, [('v1 (bruto)', AZUL), ('v2 (refatorado)', LARANJA)])
    cores = {'v1': AZUL, 'v2': LARANJA}
    for x, chave, titulo in ((60, 'carga 3968', 'Carga de 3.968 registros'),
                             (550, 'reinsercao 1984', 'Reinserção de 1.984 registros')):
        valores = [('v1', v1[chave]), ('v2', v2[chave])]
        painel(svg, x, 130, 400, 150, titulo, [(None, valores)], cores, max(v1[chave], v2[chave]) * 1.2)
    svg.text(24, 318, 'A v1 varria o heap inteiro a cada inserção; a v2 usa uma lista de blocos com vaga.', 11, MUTED)
    return svg.salvar('heap_v1_vs_v2.svg')


if __name__ == '__main__':
    dados = ler_csv()
    for caminho in (grafico_politicas(dados), grafico_pools(dados), grafico_v1_v2()):
        print(os.path.relpath(caminho, RAIZ))
