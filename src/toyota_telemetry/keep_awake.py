"""Keep the computer from sleeping while a live collection runs.

A sleeping computer stops reading the vehicle: the run goes silent until it
wakes. The display may still turn off. Windows asks the system through
SetThreadExecutionState for the calling thread; macOS runs ``caffeinate``
for as long as the block lasts; elsewhere nothing is done.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
import os
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> int:
    """The previous state; 0 means Windows refused."""

    function = ctypes.windll.kernel32.SetThreadExecutionState
    function.restype = ctypes.c_uint
    return function(ctypes.c_uint(flags))


@contextmanager
def keep_awake() -> Iterator[None]:
    """Hold off system sleep (not the display) until the block ends."""

    if os.name == "nt":
        held = _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0
        try:
            yield
        finally:
            if held:
                _set_execution_state(ES_CONTINUOUS)
        return
    if sys.platform == "darwin":
        try:
            caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            caffeinate = None
        try:
            yield
        finally:
            if caffeinate is not None:
                caffeinate.terminate()
        return
    yield
