from __future__ import annotations

import math
import unittest

from toyota_telemetry.operator import QueryCancelled, bounded_minmax


class BoundedEvidenceTests(unittest.TestCase):
    def test_empty_and_small_series_are_preserved(self) -> None:
        empty = bounded_minmax((), (), 10)
        self.assertEqual(empty.source_points, 0)
        self.assertEqual(empty.rendered_points, 0)

        small = bounded_minmax((0, 1, 2), (4, 2, 8), 10)
        self.assertEqual(small.x, (0.0, 1.0, 2.0))
        self.assertEqual(small.y, (4.0, 2.0, 8.0))

    def test_reduction_is_ordered_bounded_and_preserves_bucket_extrema(self) -> None:
        x = tuple(range(100))
        y = tuple(float((value % 10) - 5) for value in x)
        reduced = bounded_minmax(x, y, 5)

        self.assertEqual(reduced.source_points, 100)
        self.assertLessEqual(reduced.rendered_points, 20)
        self.assertEqual(tuple(sorted(reduced.x)), reduced.x)
        self.assertIn(-5.0, reduced.y)
        self.assertIn(4.0, reduced.y)

    def test_non_finite_source_values_remain_plot_gaps(self) -> None:
        x = tuple(range(100))
        y = tuple(math.nan if value == 40 else float(value) for value in x)
        reduced = bounded_minmax(x, y, 10)
        self.assertTrue(any(math.isnan(value) for value in reduced.y))

    def test_superseded_query_stops_cooperatively(self) -> None:
        with self.assertRaises(QueryCancelled):
            bounded_minmax(range(100), range(100), 5, cancelled=lambda: True)

    def test_million_point_source_never_reaches_plot_size(self) -> None:
        source = range(1_000_000)
        reduced = bounded_minmax(source, source, 1280)
        self.assertEqual(reduced.source_points, 1_000_000)
        self.assertLessEqual(reduced.rendered_points, 1280 * 4)


if __name__ == "__main__":
    unittest.main()
