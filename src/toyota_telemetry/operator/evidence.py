"""Renderer-neutral evidence reduction used by the desktop prototype.

The production evidence service will query bounded intervals from SQLite.  This
first slice proves the invariant at the presentation boundary: a plot receives
at most four ordered representatives per horizontal pixel, never the complete
source series.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math


class QueryCancelled(RuntimeError):
    """Raised cooperatively when a superseded viewport query is cancelled."""


@dataclass(frozen=True)
class BoundedSeries:
    x: tuple[float, ...]
    y: tuple[float, ...]
    source_points: int

    @property
    def rendered_points(self) -> int:
        return len(self.x)


def bounded_minmax(
    x: Sequence[float],
    y: Sequence[float],
    viewport_width: int,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> BoundedSeries:
    """Return ordered first/minimum/maximum/last samples for each pixel bucket."""

    if len(x) != len(y):
        raise ValueError("evidence x/y lengths differ")
    if viewport_width < 1:
        raise ValueError("viewport width must be positive")
    source_points = len(x)
    if source_points == 0:
        return BoundedSeries((), (), 0)

    point_budget = viewport_width * 4
    if source_points <= point_budget:
        return BoundedSeries(
            tuple(float(value) for value in x),
            tuple(float(value) for value in y),
            source_points,
        )

    bucket_size = math.ceil(source_points / viewport_width)
    selected: list[int] = []
    for start in range(0, source_points, bucket_size):
        if cancelled():
            raise QueryCancelled("evidence query was superseded")
        stop = min(source_points, start + bucket_size)
        finite = [index for index in range(start, stop) if math.isfinite(float(y[index]))]
        if not finite:
            candidates = {start}
        else:
            minimum = min(finite, key=lambda index: float(y[index]))
            maximum = max(finite, key=lambda index: float(y[index]))
            candidates = {start, minimum, maximum, stop - 1}
        selected.extend(sorted(candidates))

    return BoundedSeries(
        tuple(float(x[index]) for index in selected),
        tuple(float(y[index]) for index in selected),
        source_points,
    )
