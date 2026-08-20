"""Console output shared by every command.

Three rules hold across the toolkit.

Results go to stdout, diagnostics go to stderr.  A summary someone pipes into a
file or greps is data; a warning about a refused ECU is not.  That split is why
this module keeps ``print`` for results and uses :mod:`logging` for everything
else, rather than routing both through one channel.

Human output is the default.  These commands are read by a person deciding what
to do next far more often than by a script, and a screen of raw JSON makes that
person work.  ``--json`` gives the machine-readable form when something does
need parsing.

Everything printed stays ASCII.  A Windows console can be running a legacy code
page - cp932 on the machine this was developed against - where a single
en-dash raises UnicodeEncodeError and takes the command with it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from typing import Any, Iterable, Sequence


from . import __version__

LOG = logging.getLogger("telemetry")

_LEVELS = {0: logging.INFO, 1: logging.DEBUG}


class _Formatter(logging.Formatter):
    """Terse for the common case, explicit when something is wrong."""

    PREFIX = {
        logging.DEBUG: "debug: ",
        logging.INFO: "",
        logging.WARNING: "warning: ",
        logging.ERROR: "ERROR: ",
        logging.CRITICAL: "ERROR: ",
    }

    def format(self, record: logging.LogRecord) -> str:
        return self.PREFIX.get(record.levelno, "") + record.getMessage()


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    """Options every command shares, declared once so they cannot drift apart.

    ``--version`` belongs here rather than beside each command: an operator
    reporting a problem, or deciding whether a pin is worth moving, should be
    able to ask any of them the same way.
    """
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s (toyota-telemetry-tools {__version__})",
    )
    group = parser.add_argument_group("output")
    group.add_argument(
        "--json", action="store_true", dest="as_json",
        help="emit machine-readable JSON instead of the human summary",
    )
    group.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="more detail; repeat for debug",
    )
    group.add_argument(
        "-q", "--quiet", action="store_true",
        help="warnings and errors only",
    )
    group.add_argument(
        "--no-progress", action="store_true",
        help="never draw a progress line, even on a terminal",
    )


class _Handler(logging.StreamHandler):
    """Flush stdout before writing, so a diagnostic never overtakes the result
    it describes.  stdout is block-buffered once redirected to a file or a
    pipe; stderr is not, so without this the two streams interleave wrongly in
    exactly the case where the output is being kept."""

    def emit(self, record: logging.LogRecord) -> None:
        sys.stdout.flush()
        super().emit(record)


def configure(args: argparse.Namespace) -> None:
    level = logging.WARNING if getattr(args, "quiet", False) else _LEVELS.get(
        min(getattr(args, "verbose", 0), 1), logging.DEBUG
    )
    # Catalog text is not ours and is not always ASCII.  A legacy console code
    # page - cp932 on the machine this was developed against - raises on the
    # first character it cannot encode, which would turn a signal name into a
    # crash.  Degrade the character instead of losing the output.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass

    handler = _Handler(sys.stderr)
    handler.setFormatter(_Formatter())
    root = logging.getLogger("telemetry")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def emit(payload: dict[str, Any] | list[Any], human: str, as_json: bool) -> None:
    """Print the machine form or the human form, never both."""
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(human)


# ---------------------------------------------------------------- rendering

def table(
    rows: Sequence[Sequence[Any]],
    headers: Sequence[str],
    aligns: Sequence[str] | None = None,
) -> str:
    """A plain aligned table. No borders: they cost width and add nothing."""
    if not rows:
        return "  (none)"
    columns = list(zip(*([headers] + [[_cell(c) for c in r] for r in rows])))
    widths = [max(len(str(v)) for v in col) for col in columns]
    aligns = list(aligns or ["<"] * len(headers))
    aligns += ["<"] * (len(headers) - len(aligns))

    def line(values: Sequence[Any]) -> str:
        return "  " + "  ".join(
            f"{_cell(v):{a}{w}}" for v, a, w in zip(values, aligns, widths)
        ).rstrip()

    out = [line(headers), "  " + "  ".join("-" * w for w in widths)]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def fields(pairs: Iterable[tuple[str, Any]], indent: str = "  ") -> str:
    """Aligned label/value lines for a short summary."""
    items = [(str(k), _cell(v)) for k, v in pairs]
    if not items:
        return ""
    width = max(len(k) for k, _ in items)
    return "\n".join(f"{indent}{k:<{width}}  {v}" for k, v in items)


def heading(text: str) -> str:
    return f"\n{text}\n{'=' * len(text)}"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


# ---------------------------------------------------------------- progress

class Progress:
    """A single-line progress indicator that degrades to periodic log lines.

    A road sequence runs unattended for hours and its output is often
    redirected, so drawing carriage returns into a file would be useless. When
    stderr is not a terminal this logs a line at intervals instead.
    """

    def __init__(
        self,
        total: int,
        label: str = "",
        stream: Any = None,
        enabled: bool = True,
        log_interval: float = 30.0,
    ) -> None:
        self.total = max(total, 0)
        self.label = label
        self.stream = stream or sys.stderr
        self.log_interval = log_interval
        self.count = 0
        self.started = time.monotonic()
        self._last_log = 0.0
        self._drawn = False
        self.live = bool(
            enabled
            and getattr(self.stream, "isatty", lambda: False)()
            and not os.environ.get("NO_COLOR_PROGRESS")
        )

    @property
    def width(self) -> int:
        try:
            return max(shutil.get_terminal_size().columns - 1, 40)
        except OSError:
            return 79

    def advance(self, note: str = "") -> None:
        self.count += 1
        self.render(note)

    def render(self, note: str = "") -> None:
        elapsed = time.monotonic() - self.started
        if self.live:
            self.stream.write("\r" + self._line(note, elapsed)[: self.width].ljust(self.width))
            self.stream.flush()
            self._drawn = True
        elif elapsed - self._last_log >= self.log_interval:
            self._last_log = elapsed
            LOG.info(self._line(note, elapsed).strip())

    def _line(self, note: str, elapsed: float) -> str:
        if self.total:
            done = min(self.count, self.total)
            filled = int(20 * done / self.total)
            bar = "[" + "#" * filled + "-" * (20 - filled) + "]"
            head = f"{self.label} {bar} {done}/{self.total}"
            if done and done < self.total:
                remaining = elapsed / done * (self.total - done)
                head += f" eta {duration(remaining)}"
        else:
            head = f"{self.label} {duration(elapsed)}"
        return f"{head}  {note}" if note else head

    def close(self, note: str = "") -> None:
        if self.live and self._drawn:
            self.stream.write("\r" + " " * self.width + "\r")
            self.stream.flush()
            self._drawn = False
        if note:
            LOG.info(note)


class Timer(Progress):
    """Progress for one bounded step, measured in seconds rather than items."""

    def __init__(self, seconds: int | None, label: str, **kwargs: Any) -> None:
        super().__init__(total=0, label=label, **kwargs)
        self.seconds = seconds

    def _line(self, note: str, elapsed: float) -> str:
        if self.seconds:
            filled = int(20 * min(elapsed / self.seconds, 1.0))
            bar = "[" + "#" * filled + "-" * (20 - filled) + "]"
            head = f"{self.label} {bar} {duration(elapsed)}/{duration(self.seconds)}"
        else:
            head = f"{self.label} {duration(elapsed)} (no time limit)"
        return f"{head}  {note}" if note else head
