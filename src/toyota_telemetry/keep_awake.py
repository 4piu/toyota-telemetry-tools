"""Keep the computer awake while a live collection runs.

A sleeping computer stops reading the vehicle: the run goes silent until it
wakes. The display is kept on as well, because on laptops with Modern Standby
the display turning off (idle timeout) is what sends the whole system to
sleep, and a request for the system alone does not prevent that. Closing the
lid still sleeps. Windows asks through SetThreadExecutionState for the calling
thread; macOS runs ``caffeinate`` for as long as the block lasts; elsewhere
nothing is done.
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
ES_DISPLAY_REQUIRED = 0x00000002
AWAKE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED


def _set_execution_state(flags: int) -> int:
    """The previous state; 0 means Windows refused."""

    function = ctypes.windll.kernel32.SetThreadExecutionState
    function.restype = ctypes.c_uint
    return function(ctypes.c_uint(flags))


@contextmanager
def keep_awake() -> Iterator[None]:
    """Hold off system and display sleep until the block ends."""

    if os.name == "nt":
        held = _set_execution_state(AWAKE) != 0
        try:
            yield
        finally:
            if held:
                _set_execution_state(ES_CONTINUOUS)
        return
    if sys.platform == "darwin":
        try:
            caffeinate = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
        except OSError:
            caffeinate = None
        try:
            yield
        finally:
            if caffeinate is not None:
                caffeinate.terminate()
        return
    yield
