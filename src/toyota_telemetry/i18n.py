"""Translations for the desktop application and the CLI's human-readable text.

Catalogs are gettext ``.po`` files in ``locale/`` (UTF-8, no byte-order
mark), compiled in memory when a language is chosen, so there is no build
step and the files stay plain text. Plural forms follow each catalog's own
``Plural-Forms`` header (Arabic has six, Russian three).

Rules for code:

- ``_("text")`` translates at the moment text is shown. Never call it while a
  module is being imported: module-level text is marked with ``N_("text")``
  and passed through ``_()`` where it is displayed.
- Placeholders are named: ``_("Saved {count} rows").format(count=n)``.
- ``ngettext(singular, plural, n)`` picks the form for a count.
- Values that are stored, compared, sent to a worker, or written as JSON are
  never translated; only their display is.
"""

from __future__ import annotations

import ast
import gettext
import io
import locale
import os
from pathlib import Path
import struct
import sys

LOCALE_DIR = Path(__file__).resolve().parent / "locale"
ENVIRONMENT = "TELEMETRY_LANG"
SOURCE_LANGUAGE = "en"
# Language code -> its own name, as a language menu shows it.
LANGUAGES = {
    "en": "English",
    "ar": "العربية",
    "zh_CN": "简体中文",
    "fr": "Français",
    "ru": "Русский",
    "es": "Español",
}
RIGHT_TO_LEFT = {"ar"}

_translation: gettext.NullTranslations = gettext.NullTranslations()
_current: str | None = None


def _(message: str) -> str:
    _ensure()
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    _ensure()
    return _translation.ngettext(singular, plural, count)


def N_(message: str) -> str:  # noqa: N802 - gettext convention
    """Mark text for translation without translating it (module-level text)."""

    return message


def current_language() -> str:
    _ensure()
    return _current or SOURCE_LANGUAGE


def is_right_to_left() -> bool:
    return current_language() in RIGHT_TO_LEFT


def set_language(code: str | None) -> str:
    """Use a supported language (or the closest one); returns the code in use."""

    global _translation, _current
    chosen = resolve(code)
    _translation = gettext.NullTranslations() if chosen == SOURCE_LANGUAGE else load_catalog(chosen)
    _current = chosen
    return chosen


def init(saved: str | None = None) -> str:
    """Choose the language: TELEMETRY_LANG, then a saved choice, then the system's."""

    return set_language(os.environ.get(ENVIRONMENT) or saved or system_language())


def resolve(code: str | None) -> str:
    """Map a locale name such as zh-Hans-CN, fr_FR.UTF-8, or es-419 to a supported code."""

    if not code:
        return SOURCE_LANGUAGE
    name = code.split(".")[0].split("@")[0].replace("-", "_")
    parts = name.split("_")
    language = parts[0].lower()
    if language == "zh":
        # Only Simplified Chinese is catalogued; Traditional falls back to English.
        traditional = any(part.lower() in {"hant", "tw", "hk", "mo"} for part in parts[1:])
        return SOURCE_LANGUAGE if traditional else "zh_CN"
    return language if language in LANGUAGES else SOURCE_LANGUAGE


def system_language() -> str | None:
    if sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return locale.windows_locale.get(lcid)
        except (AttributeError, OSError):
            pass
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(variable):
            return os.environ[variable]
    return locale.getlocale()[0]


def load_catalog(code: str) -> gettext.GNUTranslations:
    path = LOCALE_DIR / f"{code}.po"
    return gettext.GNUTranslations(io.BytesIO(compile_po(parse_po(path.read_bytes()))))


def _ensure() -> None:
    if _current is None:
        init()


# .po reading and .mo compiling (stdlib only) ------------------------------


class PoEntry:
    __slots__ = ("context", "msgid", "plural", "strings", "fuzzy", "comments", "references")

    def __init__(self) -> None:
        self.context: str | None = None
        self.msgid: str | None = None
        self.plural: str | None = None
        self.strings: dict[int, str] = {}
        self.fuzzy = False
        self.comments: list[str] = []
        self.references: list[str] = []

    @property
    def translated(self) -> bool:
        return bool(self.strings) and all(self.strings.values()) and not self.fuzzy


def parse_po(data: bytes) -> list[PoEntry]:
    """Entries of a UTF-8 .po file. A byte-order mark or other encoding is an error."""

    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("translation file starts with a byte-order mark; save it as UTF-8 without BOM")
    text = data.decode("utf-8")  # raises on anything that is not UTF-8
    entries: list[PoEntry] = []
    entry = PoEntry()
    field: tuple[str, int] | None = None

    def finish() -> None:
        nonlocal entry, field
        if entry.msgid is not None:
            entries.append(entry)
        entry = PoEntry()
        field = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            finish()
            continue
        if line.startswith("#"):
            if entry.msgid is not None and field is not None and field[0] != "comment":
                finish()
            if line.startswith("#,"):
                entry.fuzzy = entry.fuzzy or "fuzzy" in line
            elif line.startswith("#:"):
                entry.references.extend(line[2:].split())
            elif line.startswith("#."):
                entry.comments.append(line[2:].strip())
            continue
        keyword, _separator, rest = line.partition(" ")
        if line.startswith('"'):
            if field is None:
                raise ValueError(f"line {number}: text outside an entry")
            _append(entry, field, _unquote(line, number))
            continue
        if keyword in ("msgctxt", "msgid") and entry.msgid is not None and field and field[0] in ("msgstr",):
            finish()
        if keyword == "msgctxt":
            entry.context, field = _unquote(rest, number), ("msgctxt", 0)
        elif keyword == "msgid":
            entry.msgid, field = _unquote(rest, number), ("msgid", 0)
        elif keyword == "msgid_plural":
            entry.plural, field = _unquote(rest, number), ("msgid_plural", 0)
        elif keyword == "msgstr":
            entry.strings[0], field = _unquote(rest, number), ("msgstr", 0)
        elif keyword.startswith("msgstr[") and keyword.endswith("]"):
            index = int(keyword[7:-1])
            entry.strings[index], field = _unquote(rest, number), ("msgstr", index)
        else:
            raise ValueError(f"line {number}: unexpected {keyword!r}")
    finish()
    return entries


def _append(entry: PoEntry, field: tuple[str, int], text: str) -> None:
    name, index = field
    if name == "msgctxt":
        entry.context = (entry.context or "") + text
    elif name == "msgid":
        entry.msgid = (entry.msgid or "") + text
    elif name == "msgid_plural":
        entry.plural = (entry.plural or "") + text
    else:
        entry.strings[index] = entry.strings.get(index, "") + text


def _unquote(token: str, number: int) -> str:
    token = token.strip()
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        raise ValueError(f"line {number}: expected a quoted string")
    try:
        return ast.literal_eval(token)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"line {number}: bad string {token}") from exc


def compile_po(entries: list[PoEntry]) -> bytes:
    """GNU .mo bytes for the translated entries (and the header)."""

    pairs: dict[bytes, bytes] = {}
    for entry in entries:
        if entry.msgid is None:
            continue
        header = entry.msgid == "" and entry.context is None
        if not header and not entry.translated:
            continue
        key = entry.msgid if entry.plural is None else f"{entry.msgid}\x00{entry.plural}"
        if entry.context is not None:
            key = f"{entry.context}\x04{key}"
        value = "\x00".join(entry.strings[index] for index in sorted(entry.strings))
        pairs[key.encode("utf-8")] = value.encode("utf-8")
    keys = sorted(pairs)
    ids = b"".join(key + b"\x00" for key in keys)
    strings = b"".join(pairs[key] + b"\x00" for key in keys)
    count = len(keys)
    header_size = 7 * 4
    key_table = header_size
    value_table = key_table + count * 8
    ids_start = value_table + count * 8
    strings_start = ids_start + len(ids)
    offsets, position = [], 0
    for key in keys:
        offsets.append((len(key), ids_start + position))
        position += len(key) + 1
    value_offsets, position = [], 0
    for key in keys:
        value_offsets.append((len(pairs[key]), strings_start + position))
        position += len(pairs[key]) + 1
    output = struct.pack("<7I", 0x950412DE, 0, count, key_table, value_table, 0, 0)
    output += b"".join(struct.pack("<2I", *row) for row in offsets)
    output += b"".join(struct.pack("<2I", *row) for row in value_offsets)
    return output + ids + strings
