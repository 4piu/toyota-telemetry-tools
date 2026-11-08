"""Maintain the translation catalogs in src/toyota_telemetry/locale/.

    python tools/i18n/messages.py extract   # messages.pot from _(), N_(), ngettext()
    python tools/i18n/messages.py update    # merge messages.pot into every <lang>.po
    python tools/i18n/messages.py check     # validate every <lang>.po; exit 1 on a problem
    python tools/i18n/messages.py todo LANG OUT.json      # untranslated messages, as UTF-8 JSON
    python tools/i18n/messages.py fill LANG IN.json ...   # merge translations from UTF-8 JSON

A fill file is a JSON object mapping each English message (msgid) to its
translation; a message with plural forms maps its singular msgid to a list
with one string per plural form of the language.

Every file is read and written as UTF-8 without a byte-order mark, with LF
line ends, whatever the console's code page. Edit catalogs with an editor or
tool that keeps UTF-8 (Poedit, VS Code); never through shell redirection,
which on Windows may re-encode the text.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "toyota_telemetry"
sys.path.insert(0, str(ROOT / "src"))

from toyota_telemetry.i18n import LANGUAGES, LOCALE_DIR, SOURCE_LANGUAGE, PoEntry, parse_po  # noqa: E402

TEMPLATE = LOCALE_DIR / "messages.pot"
PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)(?:[:!][^{}]*)?\}(?!\})")
# Number of plural forms and the gettext rule for each catalogued language.
PLURAL_FORMS = {
    "ar": "nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);",
    "zh_CN": "nplurals=1; plural=0;",
    "fr": "nplurals=2; plural=(n > 1);",
    "ru": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "es": "nplurals=2; plural=(n != 1);",
}


def extract() -> list[PoEntry]:
    """Every literal passed to _(), N_(), or ngettext() in the package, in source order."""

    found: dict[tuple[str, str | None], PoEntry] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name in ("_", "N_") and node.args:
                singular, plural = _literal(node.args[0], relative, node), None
            elif name == "ngettext" and len(node.args) >= 2:
                singular, plural = _literal(node.args[0], relative, node), _literal(node.args[1], relative, node)
            else:
                continue
            if singular is None:
                continue
            entry = found.get((singular, plural))
            if entry is None:
                entry = PoEntry()
                entry.msgid, entry.plural = singular, plural
                found[(singular, plural)] = entry
            entry.references.append(f"{relative}:{node.lineno}")
    return list(found.values())


def _literal(node: ast.AST, where: str, call: ast.Call) -> str | None:
    """The literal text, or None for a variable (a value marked with N_ elsewhere)."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    raise SystemExit(
        f"{where}:{call.lineno}: translate a literal string with named placeholders, "
        "e.g. _(\"Saved {count} rows\").format(count=n), not an f-string or variable"
    )


def write_po(path: Path, header: dict[str, str], entries: list[PoEntry]) -> None:
    lines = ['msgid ""', 'msgstr ""']
    lines += [_quote(f"{name}: {value}\n") for name, value in header.items()]
    for entry in entries:
        lines.append("")
        lines += [f"#. {comment}" for comment in entry.comments]
        if entry.references:
            lines.append("#: " + " ".join(entry.references))
        if entry.fuzzy:
            lines.append("#, fuzzy")
        if entry.context is not None:
            lines.append(f"msgctxt {_quote(entry.context)}")
        lines.append(f"msgid {_quote(entry.msgid)}")
        if entry.plural is None:
            lines.append(f"msgstr {_quote(entry.strings.get(0, ''))}")
        else:
            lines.append(f"msgid_plural {_quote(entry.plural)}")
            for index in sorted(entry.strings) or [0]:
                lines.append(f"msgstr[{index}] {_quote(entry.strings.get(index, ''))}")
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _header(language: str | None) -> dict[str, str]:
    header = {
        "Project-Id-Version": "toyota-telemetry-tools",
        "MIME-Version": "1.0",
        "Content-Type": "text/plain; charset=UTF-8",
        "Content-Transfer-Encoding": "8bit",
    }
    if language is not None:
        header["Language"] = language
        header["Plural-Forms"] = PLURAL_FORMS[language]
    return header


def update() -> None:
    template = extract()
    write_po(TEMPLATE, _header(None), template)
    for language in LANGUAGES:
        if language == SOURCE_LANGUAGE:
            continue
        path = LOCALE_DIR / f"{language}.po"
        old = {(entry.msgid, entry.plural, entry.context): entry for entry in (
            parse_po(path.read_bytes()) if path.is_file() else []
        ) if entry.msgid}
        nplurals = int(PLURAL_FORMS[language].split(";")[0].split("=")[1])
        merged = []
        for entry in template:
            new = PoEntry()
            new.msgid, new.plural, new.references = entry.msgid, entry.plural, entry.references
            previous = old.get((entry.msgid, entry.plural, None))
            if previous is not None:
                new.strings, new.fuzzy, new.comments = previous.strings, previous.fuzzy, previous.comments
            elif entry.plural is not None:
                new.strings = {index: "" for index in range(nplurals)}
            merged.append(new)
        write_po(path, _header(language), merged)


def check() -> list[str]:
    """Problems in the catalogs; an untranslated entry is reported as a count, not a problem."""

    problems: list[str] = []
    template = {(entry.msgid, entry.plural) for entry in extract()}
    for language in LANGUAGES:
        if language == SOURCE_LANGUAGE:
            continue
        path = LOCALE_DIR / f"{language}.po"
        if not path.is_file():
            problems.append(f"{path.name}: missing")
            continue
        data = path.read_bytes()
        if b"\r\n" in data:
            problems.append(f"{path.name}: use LF line ends")
        try:
            entries = parse_po(data)
        except (UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        header = next((entry for entry in entries if entry.msgid == ""), None)
        if header is None or f"Plural-Forms: {PLURAL_FORMS[language]}" not in header.strings.get(0, ""):
            problems.append(f"{path.name}: header must declare Plural-Forms: {PLURAL_FORMS[language]}")
        nplurals = int(PLURAL_FORMS[language].split(";")[0].split("=")[1])
        present = set()
        for entry in entries:
            if not entry.msgid:
                continue
            present.add((entry.msgid, entry.plural))
            wanted = set(PLACEHOLDER.findall(entry.msgid)) | set(PLACEHOLDER.findall(entry.plural or ""))
            for index, text in entry.strings.items():
                if text and set(PLACEHOLDER.findall(text)) != wanted:
                    problems.append(f"{path.name}: placeholders differ for {entry.msgid!r} (form {index})")
            if entry.plural is not None and entry.translated and len(entry.strings) != nplurals:
                problems.append(f"{path.name}: {entry.msgid!r} needs {nplurals} plural forms")
        if missing := template - present:
            problems.append(f"{path.name}: {len(missing)} messages missing; run update")
    return problems


def coverage() -> dict[str, tuple[int, int]]:
    result = {}
    for language in LANGUAGES:
        if language == SOURCE_LANGUAGE:
            continue
        path = LOCALE_DIR / f"{language}.po"
        entries = [entry for entry in parse_po(path.read_bytes()) if entry.msgid] if path.is_file() else []
        result[language] = (sum(1 for entry in entries if entry.translated), len(entries))
    return result


def _catalog(language: str) -> tuple[Path, list[PoEntry]]:
    if language not in PLURAL_FORMS:
        raise SystemExit(f"unknown language {language!r}; one of: {', '.join(PLURAL_FORMS)}")
    path = LOCALE_DIR / f"{language}.po"
    return path, parse_po(path.read_bytes())


def todo(language: str, output: Path) -> int:
    _path, entries = _catalog(language)
    rows = [
        {"msgid": entry.msgid, "plural": entry.plural, "where": entry.references[:2]}
        for entry in entries if entry.msgid and not entry.translated
    ]
    output.write_bytes(json.dumps(rows, ensure_ascii=False, indent=1).encode("utf-8"))
    return len(rows)


def fill(language: str, sources: list[Path]) -> tuple[int, list[str]]:
    """Merge JSON translations; returns how many were applied and the unknown msgids."""

    path, entries = _catalog(language)
    nplurals = int(PLURAL_FORMS[language].split(";")[0].split("=")[1])
    by_id = {entry.msgid: entry for entry in entries if entry.msgid}
    applied, unknown = 0, []
    for source in sources:
        data = source.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            raise SystemExit(f"{source}: remove the byte-order mark (save as UTF-8 without BOM)")
        for msgid, text in json.loads(data.decode("utf-8")).items():
            entry = by_id.get(msgid)
            if entry is None:
                unknown.append(msgid)
                continue
            if entry.plural is None:
                if not isinstance(text, str):
                    raise SystemExit(f"{source}: {msgid!r} takes one string")
                entry.strings = {0: text}
            else:
                forms = [text] * nplurals if isinstance(text, str) else list(text)
                if len(forms) != nplurals:
                    raise SystemExit(f"{source}: {msgid!r} needs {nplurals} plural forms")
                entry.strings = dict(enumerate(forms))
            entry.fuzzy = False
            applied += 1
    header = next(entry for entry in entries if entry.msgid == "")
    write_po(path, _header(language), [entry for entry in entries if entry is not header])
    return applied, unknown


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "extract":
        write_po(TEMPLATE, _header(None), extract())
        print(f"{TEMPLATE.relative_to(ROOT)}: {len(extract())} messages")
    elif command == "update":
        update()
        for language, (done, total) in coverage().items():
            print(f"{language}: {done}/{total} translated")
    elif command == "check":
        problems = check()
        for problem in problems:
            print(problem)
        for language, (done, total) in coverage().items():
            print(f"{language}: {done}/{total} translated")
        return 1 if problems else 0
    elif command == "todo" and len(argv) == 3:
        count = todo(argv[1], Path(argv[2]))
        print(f"{argv[1]}: {count} untranslated messages written to {argv[2]}")
    elif command == "fill" and len(argv) >= 3:
        applied, unknown = fill(argv[1], [Path(name) for name in argv[2:]])
        print(f"{argv[1]}: {applied} translations applied")
        for msgid in unknown:
            print(f"  not in the catalog (ignored): {msgid[:70]!r}")
        done, total = coverage()[argv[1]]
        print(f"{argv[1]}: {done}/{total} translated")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
