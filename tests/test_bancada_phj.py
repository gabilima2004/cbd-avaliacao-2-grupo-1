import csv
import tempfile
import unittest
from pathlib import Path

from buffer_heap import BufferPool, FileManager, HeapFile, registro
from bancada import (ExperimentSpec, JoinRequest, PartitionHashJoinAdapter,
                     measure_join)
from bancada.phj import generate_master_detail, run_initial_experiments
from bancada.analysis import (analyze_results, read_results, write_charts,
                              write_markdown_report, write_summary_csv)


class PartitionHashJoinBenchTests(unittest.TestCase):
    def test_standard_adapter_and_general_specific_metrics(self):
        data = generate_master_detail(20, 35, 42)
        with tempfile.TemporaryDirectory() as directory:
            pool = BufferPool(FileManager(directory), 4, 'FIFO')
            left, right = HeapFile(pool, 'left'), HeapFile(pool, 'right')
            for row in data.master_rows:
                left.insert(registro.encode(*row))
            for row in data.detail_rows:
                right.insert(registro.encode(*row))
            request = JoinRequest(left, right, 0, 1, pool, 3,
                                  {'max_depth': 4})
            spec = ExperimentSpec('unit-phj', 20, 35, 4, 3, 'FIFO', 42, 1)
            result = measure_join(
                PartitionHashJoinAdapter(), request, spec, data.expected_summary)
            self.assertEqual(result.validation_status, 'ok')
            self.assertEqual(result.result_count, 35)
            self.assertEqual(result.algorithm, 'partition_hash_join')
            self.assertGreater(result.physical_reads, 0)
            self.assertGreater(result.physical_writes, 0)
            self.assertGreater(result.partitions_created, 0)
            self.assertEqual(result.theoretical_base_io, 9)
            self.assertEqual(result.measured_io,
                             result.physical_reads + result.physical_writes)
            self.assertEqual(result.effective_access_r, 'scan')
            self.assertEqual(result.effective_access_s, 'scan')
            self.assertEqual(pool.pinned_count(), 0)
            pool.close()

    def test_generation_is_reproducible_and_seed_changes_data(self):
        first = generate_master_detail(25, 40, 42)
        second = generate_master_detail(25, 40, 42)
        other = generate_master_detail(25, 40, 43)
        self.assertEqual(first, second)
        self.assertNotEqual(first.detail_rows, other.detail_rows)

    def test_initial_matrix_writes_csv_compatible_records(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'matrix.csv'
            results = run_initial_experiments(
                output,
                small_size=8,
                large_size=40,
                buffer_pages=4,
                memory_pages=3,
                seed=7,
                repetitions=2,
                policies=('LRU', 'FIFO'),
                scenarios=('small-small', 'small-large', 'large-large'),
                max_depth=4,
                verbose=False,
            )
            self.assertEqual(len(results), 12)
            self.assertTrue(all(r.validation_status == 'ok' for r in results))
            with output.open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 12)
            required = {
                'experiment_id', 'algorithm', 'r_size', 's_size',
                'buffer_pages', 'memory_pages', 'replacement_policy', 'seed',
                'repetition', 'elapsed_seconds', 'physical_reads',
                'physical_writes', 'cache_hits', 'cache_misses', 'result_count',
                'validation_status', 'partitions_created',
                'repartition_passes', 'fallback_partitions',
                'theoretical_base_io', 'measured_io', 'io_difference',
                'available_access_r', 'available_access_s',
                'effective_access_r', 'effective_access_s',
            }
            self.assertTrue(required.issubset(rows[0]))

            summaries = analyze_results(rows, threshold_percent=100.0)
            self.assertEqual(len(summaries), 6)
            self.assertTrue(all(item.valid_runs == 2 for item in summaries))
            self.assertTrue(all(item.io_consistent for item in summaries))
            self.assertTrue(all(not item.stable for item in summaries),
                            'menos de três repetições não prova estabilidade')
            summary_path = Path(directory) / 'summary.csv'
            report_path = Path(directory) / 'report.md'
            charts = Path(directory) / 'charts'
            write_summary_csv(summary_path, summaries)
            write_markdown_report(report_path, summaries, output, 100.0)
            write_charts(charts, summaries)
            self.assertEqual(len(read_results(summary_path)), 6)
            self.assertTrue(report_path.exists())
            self.assertTrue((charts / 'phj_mean_time.svg').exists())
            self.assertTrue((charts / 'phj_io_comparison.svg').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
