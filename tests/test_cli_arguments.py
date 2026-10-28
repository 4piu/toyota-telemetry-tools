"""Every ``args.<name>`` a CLI reads must be an option its parser declares.

A dangling reference survives every dry run when it sits behind a ``--live``
guard, and then fails at a vehicle. This is a static check precisely because
the paths that hide such a bug are the ones that cannot be exercised offline.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "toyota_telemetry"


def declared_options(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "set_defaults":
            names.update(keyword.arg for keyword in node.keywords if keyword.arg)
            continue
        if node.func.attr not in {"add_argument", "add_subparsers"}:
            continue
        for keyword in node.keywords:
            if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant):
                names.add(str(keyword.value.value))
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                text = argument.value
                if text.startswith("--"):
                    names.add(text[2:].replace("-", "_"))
                elif not text.startswith("-"):
                    names.add(text.replace("-", "_"))
    return names


def used_options(tree: ast.AST) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    }


def calls_helper(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        for node in ast.walk(tree)
    )


class CliArgumentTests(unittest.TestCase):
    def test_every_referenced_option_is_declared(self) -> None:
        shared: set[str] = set()
        checked = 0
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            declared = declared_options(tree)
            if not declared:
                continue
            if calls_helper(tree, "add_output_arguments"):
                declared |= shared
            checked += 1
            missing = sorted(used_options(tree) - declared)
            self.assertEqual(
                missing, [], f"{path.name} reads undeclared option(s): {missing}"
            )
        self.assertGreater(checked, 0, "no CLI modules were checked")


if __name__ == "__main__":
    unittest.main()
