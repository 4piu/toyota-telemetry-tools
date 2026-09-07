"""Bounded decoding primitives for reviewed telemetry formulas."""

from __future__ import annotations

import ast


def extract_raw(data: bytes, first_bit: int, last_bit: int) -> int:
    if first_bit < 0 or last_bit < first_bit or last_bit >= len(data) * 8:
        raise ValueError("catalog bit range exceeds the response data")
    width = last_bit - first_bit + 1
    shift = len(data) * 8 - 1 - last_bit
    return (int.from_bytes(data, "big") >> shift) & ((1 << width) - 1)


def evaluate_formula(expression: str, raw: int) -> float:
    tree = ast.parse(expression, mode="eval")

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name) and node.id == "raw":
            return float(raw)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "signed"
            and len(node.args) == 2
            and not node.keywords
        ):
            value, width_value = visit(node.args[0]), visit(node.args[1])
            width = int(width_value)
            if width_value != width or width < 1 or width > 64:
                raise ValueError("signed() width must be an integer from 1 through 64")
            unsigned = int(value)
            if value != unsigned or unsigned < 0 or unsigned >= (1 << width):
                raise ValueError("signed() value does not fit the requested width")
            return float(
                unsigned - (1 << width)
                if unsigned & (1 << (width - 1))
                else unsigned
            )
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left**right
        raise ValueError(f"unsupported formula syntax: {expression}")

    return visit(tree)
