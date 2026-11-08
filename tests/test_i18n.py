from __future__ import annotations

import gettext
import io
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from toyota_telemetry import i18n
from toyota_telemetry.i18n import compile_po, parse_po, resolve

ROOT = Path(__file__).resolve().parents[1]
CATALOG = '''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n"

#: src/example.py:1
msgid "Saved {count} rows to {name}."
msgstr "Сохранено строк: {count}, файл {name}."

msgid "{count} signal"
msgid_plural "{count} signals"
msgstr[0] "{count} сигнал"
msgstr[1] "{count} сигнала"
msgstr[2] "{count} сигналов"

#, fuzzy
msgid "Fuzzy"
msgstr "Нечётко"

msgid "Not yet"
msgstr ""

msgid "Line one\\nline \\"two\\""
msgstr "Первая\\nвторая \\"строка\\""
'''


def translations(text: str) -> gettext.GNUTranslations:
    return gettext.GNUTranslations(io.BytesIO(compile_po(parse_po(text.encode("utf-8")))))


class CatalogTests(unittest.TestCase):
    def test_utf8_text_and_plural_forms_round_trip(self) -> None:
        catalog = translations(CATALOG)
        self.assertEqual(catalog.gettext("Saved {count} rows to {name}.").format(count=3, name="a.json"),
                         "Сохранено строк: 3, файл a.json.")
        self.assertEqual([catalog.ngettext("{count} signal", "{count} signals", n).format(count=n)
                          for n in (1, 3, 5, 21)],
                         ["1 сигнал", "3 сигнала", "5 сигналов", "21 сигнал"])
        self.assertEqual(catalog.gettext("Line one\nline \"two\""), "Первая\nвторая \"строка\"")

    def test_fuzzy_and_empty_entries_fall_back_to_english(self) -> None:
        catalog = translations(CATALOG)
        self.assertEqual(catalog.gettext("Fuzzy"), "Fuzzy")
        self.assertEqual(catalog.gettext("Not yet"), "Not yet")

    def test_arabic_has_six_plural_forms(self) -> None:
        forms = "\n".join(f'msgstr[{index}] "form {index}"' for index in range(6))
        catalog = translations(
            'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
            '"Plural-Forms: nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : '
            'n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);\\n"\n\n'
            f'msgid "one"\nmsgid_plural "many"\n{forms}\n'
        )
        self.assertEqual([catalog.ngettext("one", "many", n) for n in (0, 1, 2, 5, 11, 100)],
                         ["form 0", "form 1", "form 2", "form 3", "form 4", "form 5"])

    def test_a_byte_order_mark_or_another_code_page_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "byte-order mark"):
            parse_po(b"\xef\xbb\xbf" + CATALOG.encode("utf-8"))
        with self.assertRaises(UnicodeDecodeError):
            parse_po(CATALOG.encode("cp1251"))

    def test_system_locale_names_map_to_catalogues(self) -> None:
        self.assertEqual([resolve(name) for name in (
            "zh-Hans-CN", "zh_CN.UTF-8", "zh-TW", "fr_FR", "es-419", "ru_RU.KOI8-R", "ar_SA", "de_DE", None,
        )], ["zh_CN", "zh_CN", "en", "fr", "es", "ru", "ar", "en", "en"])

    def test_the_environment_overrides_a_saved_choice(self) -> None:
        try:
            with mock.patch.dict("os.environ", {"TELEMETRY_LANG": "en"}):
                self.assertEqual(i18n.init("fr"), "en")
            with mock.patch.dict("os.environ", {}, clear=True), \
                    mock.patch.object(i18n, "system_language", return_value="ru_RU"):
                self.assertEqual(i18n.init(None), "ru")
        finally:
            i18n.set_language("en")


class ShippedCatalogTests(unittest.TestCase):
    def test_catalogs_are_valid_utf8_with_matching_placeholders(self) -> None:
        spec = importlib.util.spec_from_file_location("messages", ROOT / "tools" / "i18n" / "messages.py")
        messages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(messages)
        self.assertEqual(messages.check(), [])

    def test_every_language_loads(self) -> None:
        try:
            for code in i18n.LANGUAGES:
                self.assertEqual(i18n.set_language(code), code)
                self.assertTrue(i18n._("Save"))
        finally:
            i18n.set_language("en")


class SourceTests(unittest.TestCase):
    def test_no_module_that_translates_rebinds_the_underscore(self) -> None:
        # `for _, x in ...` or `a, _ = ...` would hide _() and fail at run time.
        import ast

        problems = []
        for path in sorted((ROOT / "src" / "toyota_telemetry").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if not any(isinstance(node, ast.ImportFrom) and any(alias.name == "_" for alias in node.names)
                       for node in ast.walk(tree)):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(problems, [])
