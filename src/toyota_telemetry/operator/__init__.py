"""Qt-free operator services shared by the CLI and the desktop application."""

from .evidence import BoundedSeries, QueryCancelled, bounded_minmax
from .service import OPERATIONS, OPERATION_IDS, OperatorService

__all__ = [
    "BoundedSeries",
    "OPERATIONS",
    "OPERATION_IDS",
    "OperatorService",
    "QueryCancelled",
    "bounded_minmax",
]
