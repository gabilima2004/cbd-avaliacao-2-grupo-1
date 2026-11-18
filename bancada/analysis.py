"""Consolidação estatística e gráficos da bancada de joins."""

import argparse
import csv
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path
import statistics


@dataclass
class ExperimentSummary:
    algorithm: str
    r_size: int
    s_size: int
    replacement_policy: str
    buffer_pages: int
    memory_pages: int
    seed: int
    available_access_r: str
    available_access_s: str
    effective_access_r: str
    effective_access_s: str
    repetitions: int
    valid_runs: int
    invalid_runs: int
    mean_seconds: float
    min_seconds: float
    max_seconds: float
    stdev_seconds: float
    cv_percent: float
    range_percent: float
    stable: bool
    physical_reads: int
    physical_writes: int
    io_consistent: bool
    theoretical_base_io: int
    measured_io: int
    io_difference: int
    io_ratio: float
    mean_result_count: float
    partitions_created: int
    repartition_passes: int
    fallback_partitions: int

    def as_row(self):
        return asdict(self)


GROUP_FIELDS = (
    'algorithm', 'r_size', 's_size', 'replacement_policy', 'buffer_pages',
    'memory_pages', 'seed', 'available_access_r', 'available_access_s',
    'effective_access_r', 'effective_access_s',
)


def _number(row, name, cast=float, default=0):
    value = row.get(name, '')
    return cast(value) if value not in ('', None) else default


def read_results(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        return list(csv.DictReader(stream))


def analyze_results(rows, threshold_percent=5.0):
    groups = {}
    for row in rows:
        key = tuple(row.get(field, '') for field in GROUP_FIELDS)
        groups.setdefault(key, []).append(row)

    summaries = []
    for key, group in sorted(groups.items()):
        valid = [row for row in group if row.get('validation_status') == 'ok']
        times = [_number(row, 'elapsed_seconds') for row in valid]
        mean = statistics.mean(times) if times else 0.0
        stdev = statistics.stdev(times) if len(times) >= 2 else 0.0
        cv = stdev / mean * 100 if mean else 0.0
        spread = (max(times) - min(times)) / mean * 100 if times and mean else 0.0
        reads = {_number(row, 'physical_reads', int) for row in valid}
        writes = {_number(row, 'physical_writes', int) for row in valid}
        measured = {_number(row, 'measured_io', int) for row in valid}
        theory = {_number(row, 'theoretical_base_io', int) for row in valid}
        first = valid[0] if valid else group[0]
        measured_value = next(iter(measured)) if len(measured) == 1 else 0
        theory_value = next(iter(theory)) if len(theory) == 1 else 0
        summaries.append(ExperimentSummary(
            algorithm=key[0],
            r_size=int(key[1]),
            s_size=int(key[2]),
            replacement_policy=key[3],
            buffer_pages=int(key[4]),
            memory_pages=int(key[5]),
            seed=int(key[6]),
            available_access_r=key[7],
            available_access_s=key[8],
            effective_access_r=key[9],
            effective_access_s=key[10],
            repetitions=len(group),
            valid_runs=len(valid),
            invalid_runs=len(group) - len(valid),
            mean_seconds=mean,
            min_seconds=min(times) if times else 0.0,
            max_seconds=max(times) if times else 0.0,
            stdev_seconds=stdev,
            cv_percent=cv,
            range_percent=spread,
            stable=len(times) >= 3 and cv <= threshold_percent,
            physical_reads=next(iter(reads)) if len(reads) == 1 else 0,
            physical_writes=next(iter(writes)) if len(writes) == 1 else 0,
            io_consistent=bool(valid) and len(reads) == 1 and len(writes) == 1,
            theoretical_base_io=theory_value,
            measured_io=measured_value,
            io_difference=measured_value - theory_value,
            io_ratio=measured_value / theory_value if theory_value else 0.0,
            mean_result_count=statistics.mean(
                _number(row, 'result_count', int) for row in valid) if valid else 0.0,
            partitions_created=_number(first, 'partitions_created', int),
            repartition_passes=_number(first, 'repartition_passes', int),
            fallback_partitions=_number(first, 'fallback_partitions', int),
        ))
    return summaries


def write_summary_csv(path, summaries):
    rows = [summary.as_row() for summary in summaries]
    if not rows:
        raise ValueError('não há resumos para exportar')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown_report(path, summaries, source_csv, threshold_percent):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '# Resultados do Partition Hash Join', '',
        f'Fonte: `{source_csv}`.', '',
        f'Estabilidade: coeficiente de variação menor ou igual a '
        f'{threshold_percent:.1f}%.', '',
        '| R | S | Política | Repetições válidas | Média s | CV % | Estável | '
        'E/S medida | E/S teórica | Razão | Repartições | Fallback |',
        '|---:|---:|---|---:|---:|---:|---|---:|---:|---:|---:|---:|',
    ]
    for item in summaries:
        lines.append(
            f'| {item.r_size} | {item.s_size} | {item.replacement_policy} | '
            f'{item.valid_runs}/{item.repetitions} | {item.mean_seconds:.6f} | '
            f'{item.cv_percent:.2f} | {"sim" if item.stable else "não"} | '
            f'{item.measured_io} | {item.theoretical_base_io} | '
            f'{item.io_ratio:.3f} | {item.repartition_passes} | '
            f'{item.fallback_partitions} |')
    unstable = sum(not item.stable for item in summaries)
    invalid = sum(item.invalid_runs for item in summaries)
    io_inconsistent = sum(not item.io_consistent for item in summaries)
    lines.extend([
        '', '## Síntese', '',
        f'- Configurações analisadas: {len(summaries)}.',
        f'- Configurações fora do limite de estabilidade: {unstable}.',
        f'- Execuções inválidas: {invalid}.',
        f'- Configurações com E/S inconsistente entre repetições: '
        f'{io_inconsistent}.', '',
        'O custo teórico apresentado é a referência simples '
        '`3 × (blocos de R + blocos de S)`. Cabeçalhos temporários, expulsões '
        'dirty e reparticionamentos podem aumentar o valor medido.', '',
        '![Tempo médio](../results/charts/phj_mean_time.svg)', '',
        '![E/S medida e teórica](../results/charts/phj_io_comparison.svg)', '',
    ])
    target.write_text('\n'.join(lines), encoding='utf-8')


def _bar_chart(path, title, bars, y_label):
    width = max(900, 90 * len(bars) + 120)
    height = 500
    left, top, bottom = 80, 55, 130
    plot_height = height - top - bottom
    maximum = max((value for _, value, _ in bars), default=1) or 1
    slot = (width - left - 30) / max(1, len(bars))
    bar_width = slot * 0.65
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" '
        f'font-family="Arial" font-size="20">{escape(title)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" '
        'stroke="#333"/>',
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-20}" '
        f'y2="{height-bottom}" stroke="#333"/>',
        f'<text x="18" y="{top + plot_height/2}" text-anchor="middle" '
        f'transform="rotate(-90 18 {top + plot_height/2})" '
        f'font-family="Arial" font-size="13">{escape(y_label)}</text>',
    ]
    for index, (label, value, color) in enumerate(bars):
        x = left + index * slot + (slot - bar_width) / 2
        bar_height = value / maximum * plot_height
        y = height - bottom - bar_height
        parts.extend([
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_height:.1f}" fill="{color}"/>',
            f'<text x="{x + bar_width/2:.1f}" y="{max(top, y)-5:.1f}" '
            f'text-anchor="middle" font-family="Arial" font-size="11">'
            f'{value:.4g}</text>',
            f'<text x="{x + bar_width/2:.1f}" y="{height-bottom+15}" '
            f'text-anchor="end" transform="rotate(-45 '
            f'{x + bar_width/2:.1f} {height-bottom+15})" '
            f'font-family="Arial" font-size="10">{escape(label)}</text>',
        ])
    parts.append('</svg>')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(parts), encoding='utf-8')


def write_charts(directory, summaries):
    directory = Path(directory)
    time_bars = []
    io_bars = []
    colors = {'LRU': '#2563EB', 'FIFO': '#F97316'}
    for item in summaries:
        label = f'{item.r_size}x{item.s_size} {item.replacement_policy}'
        time_bars.append((label, item.mean_seconds,
                          colors.get(item.replacement_policy, '#64748B')))
        io_bars.append((label + ' med.', item.measured_io, '#2563EB'))
        io_bars.append((label + ' teor.', item.theoretical_base_io, '#94A3B8'))
    _bar_chart(directory / 'phj_mean_time.svg',
               'Tempo médio do Partition Hash Join', time_bars, 'segundos')
    _bar_chart(directory / 'phj_io_comparison.svg',
               'E/S medida e custo teórico base', io_bars, 'páginas transferidas')


def build_parser():
    parser = argparse.ArgumentParser(description='Analisa o CSV da bancada de joins.')
    parser.add_argument('--input', required=True)
    parser.add_argument('--summary', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--charts-dir', required=True)
    parser.add_argument('--threshold-percent', type=float, default=5.0)
    parser.add_argument('--fail-unstable', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    summaries = analyze_results(read_results(args.input), args.threshold_percent)
    write_summary_csv(args.summary, summaries)
    write_markdown_report(args.report, summaries, args.input,
                          args.threshold_percent)
    write_charts(args.charts_dir, summaries)
    invalid = sum(item.invalid_runs for item in summaries)
    unstable = sum(not item.stable for item in summaries)
    print(f'configurações={len(summaries)}; inválidas={invalid}; '
          f'instáveis={unstable}; resumo={args.summary}')
    if invalid:
        return 1
    if args.fail_unstable and unstable:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
