"""Move files to the operating system's trash (Recycle Bin, Finder Trash, freedesktop Trash).

Dependency-free: Windows uses the shell's recycle operation, macOS asks Finder,
and other systems follow the freedesktop.org trash specification. Restoring is
done from the system's own trash, which puts the folders back where they were.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote


class TrashError(OSError):
    pass


def send_to_trash(paths: list[Path]) -> None:
    """Move every path to the system trash, or raise ``TrashError``."""

    paths = [Path(path).resolve() for path in paths]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise TrashError(f"not found: {missing[0]}")
    if not paths:
        return
    if sys.platform == "win32":
        _windows(paths)
    elif sys.platform == "darwin":
        _macos(paths)
    else:
        _freedesktop(paths)


def _windows(paths: list[Path]) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    fo_delete = 0x0003
    # Allow undo (recycle), no prompts or progress, but warn before anything
    # would be deleted permanently (a drive without a Recycle Bin).
    flags = 0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x4000
    operation = SHFILEOPSTRUCTW(
        wFunc=fo_delete, pFrom="\0".join(str(path) for path in paths) + "\0\0", fFlags=flags,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise TrashError(f"the Recycle Bin refused the move (code {result:#x})")


def _macos(paths: list[Path]) -> None:
    items = ", ".join('POSIX file "{}"'.format(str(path).replace("\\", "\\\\").replace('"', '\\"')) for path in paths)
    completed = subprocess.run(
        ["osascript", "-e", f'tell application "Finder" to delete {{{items}}}'],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise TrashError(f"Finder refused the move: {completed.stderr.strip()}")


def _freedesktop(paths: list[Path]) -> None:
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"
    home.mkdir(parents=True, exist_ok=True)
    for path in paths:
        # A file on another volume goes to that volume's trash, so nothing is copied.
        trash = home if path.stat().st_dev == home.stat().st_dev else _volume_trash(path)
        files, info = trash / "files", trash / "info"
        files.mkdir(parents=True, exist_ok=True)
        info.mkdir(parents=True, exist_ok=True)
        name = path.name
        for number in range(1, 10_000):
            try:
                with (info / f"{name}.trashinfo").open("x", encoding="utf-8") as stream:
                    stream.write(
                        "[Trash Info]\n"
                        f"Path={quote(str(path))}\n"
                        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
                    )
                break
            except FileExistsError:
                name = f"{path.name}.{number}"
        else:
            raise TrashError(f"cannot name {path.name} in the trash")
        shutil.move(str(path), str(files / name))


def _volume_trash(path: Path) -> Path:
    top = path
    while not os.path.ismount(top):
        top = top.parent
    return top / f".Trash-{os.getuid()}"
