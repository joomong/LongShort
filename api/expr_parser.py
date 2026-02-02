"""Expression parser: converts expr attribute strings into Signal DAGs.

Uses Python's ast module (NO eval()) for safe parsing of arithmetic expressions.
"""

from __future__ import annotations

import ast
import re

from framework.signal_base import Signal
from framework.modules.sources import Price
from framework.modules.constant import ConstantSignal
from framework.modules.delay import Delay
from api.xml_alpha import XmlCompileError

ALLOWED_COLUMNS = {"open", "high", "low", "close", "volume"}

# return -> _RET_ substitution (Python keyword workaround)
_RETURN_TOKEN = "_RET_"
_RETURN_RE = re.compile(r'\breturn\b')

ALLOWED_AST_NODES = {
    ast.Expression, ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub,
    ast.Constant, ast.Name, ast.Load,
}

_BINOP_MAP = {
    ast.Add:  lambda a, b: a + b,
    ast.Sub:  lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div:  lambda a, b: a / b,
}


def _make_return_signal() -> Signal:
    """Daily return: close / delay(close, 1) - 1"""
    close = Price("close")
    prev_close = Delay(Price("close"), 1)
    return close / prev_close - ConstantSignal(1)


def _validate_ast(node: ast.AST) -> None:
    """Only allow whitelisted AST nodes; reject everything else."""
    if type(node) not in ALLOWED_AST_NODES:
        raise XmlCompileError(f"Unsupported syntax in expr: {type(node).__name__}")
    for child in ast.iter_child_nodes(node):
        _validate_ast(child)


def _eval_node(node: ast.AST) -> Signal:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise XmlCompileError(f"Unsupported constant type: {type(node.value).__name__}")
        return ConstantSignal(node.value)
    if isinstance(node, ast.Name):
        name = node.id
        if name == _RETURN_TOKEN:
            return _make_return_signal()
        if name not in ALLOWED_COLUMNS:
            raise XmlCompileError(
                f"Unsupported expr column: {name!r}. "
                f"Allowed: {', '.join(sorted(ALLOWED_COLUMNS))}, return"
            )
        return Price(column=name)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op_fn = _BINOP_MAP.get(type(node.op))
        if op_fn is None:
            raise XmlCompileError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return ConstantSignal(-1) * _eval_node(node.operand)
    raise XmlCompileError(f"Unsupported syntax in expr: {type(node).__name__}")


def parse_expr(expr: str) -> Signal:
    """Parse an expr string into a Signal DAG."""
    raw = (expr or "").strip()
    if not raw:
        raise XmlCompileError("Unsupported expr='' (empty)")
    preprocessed = _RETURN_RE.sub(_RETURN_TOKEN, raw)
    try:
        tree = ast.parse(preprocessed, mode='eval')
    except SyntaxError as e:
        raise XmlCompileError(f"Syntax error in expr: {raw!r}") from e
    _validate_ast(tree)
    return _eval_node(tree)
