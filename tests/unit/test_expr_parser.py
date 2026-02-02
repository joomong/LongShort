"""Tests for api/expr_parser.py — expression parsing into Signal DAGs."""

import pytest

from api.expr_parser import parse_expr
from api.xml_alpha import compile, XmlCompileError
from framework.modules.sources import Price
from framework.modules.constant import ConstantSignal
from framework.modules.ops import Sub, Div, Mul, Add
from framework.modules.delay import Delay
from framework.signal_base import Signal


# ── Backward compatibility (TC-EP-001~003) ──────────────────────────

class TestBackwardCompat:
    def test_close(self):
        """TC-EP-001: 'close' -> Price with column=='close'."""
        sig = parse_expr("close")
        assert isinstance(sig, Price)
        assert sig.column == "close"

    def test_open(self):
        """TC-EP-002: 'open' -> Price with column=='open'."""
        sig = parse_expr("open")
        assert isinstance(sig, Price)
        assert sig.column == "open"

    def test_whitespace_trimmed(self):
        """TC-EP-003: '  close  ' -> Price (whitespace trimmed)."""
        sig = parse_expr("  close  ")
        assert isinstance(sig, Price)
        assert sig.column == "close"


# ── Arithmetic (TC-EP-010~014) ───────────────────────────────────────

class TestArithmetic:
    def test_sub(self):
        """TC-EP-010: 'close - open' -> Sub."""
        sig = parse_expr("close - open")
        assert isinstance(sig, Sub)

    def test_div_of_subs(self):
        """TC-EP-011: '(close - low) / (high - low)' -> Div."""
        sig = parse_expr("(close - low) / (high - low)")
        assert isinstance(sig, Div)
        assert isinstance(sig.a, Sub)
        assert isinstance(sig.b, Sub)

    def test_const_mul(self):
        """TC-EP-012: '4 * close' -> Mul."""
        sig = parse_expr("4 * close")
        assert isinstance(sig, Mul)

    def test_negate(self):
        """TC-EP-013: '-close' -> Mul(ConstantSignal(-1), Price)."""
        sig = parse_expr("-close")
        assert isinstance(sig, Mul)
        assert isinstance(sig.a, ConstantSignal)
        assert sig.a.value == -1.0
        assert isinstance(sig.b, Price)

    def test_precedence(self):
        """TC-EP-014: 'close * 0.5 + open * 0.5' -> Add (precedence correct)."""
        sig = parse_expr("close * 0.5 + open * 0.5")
        assert isinstance(sig, Add)
        assert isinstance(sig.a, Mul)
        assert isinstance(sig.b, Mul)


# ── return (TC-EP-020~022) ───────────────────────────────────────────

class TestReturn:
    def test_return_signal(self):
        """TC-EP-020: 'return' -> compound signal involving Div and Sub."""
        sig = parse_expr("return")
        assert isinstance(sig, Signal)
        # return = close / delay(close,1) - 1  =>  Sub(Div(...), Constant(1))
        assert isinstance(sig, Sub)
        assert isinstance(sig.a, Div)

    def test_neg_return(self):
        """TC-EP-021: '-return' -> works without error."""
        sig = parse_expr("-return")
        assert isinstance(sig, Signal)

    def test_scaled_return(self):
        """TC-EP-022: '2 * return' -> works without error."""
        sig = parse_expr("2 * return")
        assert isinstance(sig, Signal)
        assert isinstance(sig, Mul)


# ── Error cases (TC-EP-040~044) ──────────────────────────────────────

class TestErrors:
    def test_empty(self):
        """TC-EP-040: '' -> XmlCompileError."""
        with pytest.raises(XmlCompileError):
            parse_expr("")

    def test_unknown_column(self):
        """TC-EP-041: 'adj_close' -> XmlCompileError."""
        with pytest.raises(XmlCompileError):
            parse_expr("adj_close")

    def test_syntax_error(self):
        """TC-EP-042: 'close +' -> XmlCompileError."""
        with pytest.raises(XmlCompileError):
            parse_expr("close +")

    def test_string_literal(self):
        """TC-EP-043: \"'close'\" -> XmlCompileError (string constant)."""
        with pytest.raises(XmlCompileError):
            parse_expr("'close'")

    def test_attribute_access(self):
        """TC-EP-044: 'os.system' -> XmlCompileError (attribute access blocked)."""
        with pytest.raises(XmlCompileError):
            parse_expr("os.system")


# ── Integration via compile() (TC-EP-060~063) ───────────────────────

class TestIntegration:
    def test_expr_arithmetic(self):
        """TC-EP-060: <data expr='close - open'/> -> 1 alpha."""
        alphas = compile('<data expr="close - open"/>')
        assert len(alphas) == 1
        assert isinstance(alphas[0], Sub)

    def test_pipe_with_expr(self):
        """TC-EP-061: pipe produces 2 alphas from two expressions."""
        alphas = compile('<data expr="close - open | high - low"/>')
        assert len(alphas) == 2

    def test_return_with_delay(self):
        """TC-EP-062: <data expr='return' delay='1'/> -> 1 alpha with Delay wrapper."""
        alphas = compile('<data expr="return" delay="1"/>')
        assert len(alphas) == 1
        assert isinstance(alphas[0], Delay)

    def test_backward_compat_close(self):
        """TC-EP-063: <data expr='close'/> -> 1 alpha, backward compatible."""
        alphas = compile('<data expr="close"/>')
        assert len(alphas) == 1
        assert isinstance(alphas[0], Price)
