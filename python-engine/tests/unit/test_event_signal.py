import numpy as np
import pytest

from framework.modules.event import EventSignal
from framework.modules.sources import Price


def _arrays(symbols: list[str], base_rows: list[list[float]], trig_rows: list[list[float]]) -> dict[str, np.ndarray]:
    """Build an arrays dict with (T, N) base/trig arrays and metadata keys."""
    return {
        "base": np.array(base_rows, dtype=float),
        "trig": np.array(trig_rows, dtype=float),
        "__n_symbols__": len(symbols),
        "__symbols__": np.array(symbols),
    }


def test_event_signal_hold_window_off_by_one():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    # 3 timestamps, 2 symbols (A, B)
    arrays = _arrays(
        ["A", "B"],
        base_rows=[[10, 10], [10, 10], [10, 10]],
        trig_rows=[[1, 0], [0, 0], [0, 0]],
    )

    out0 = ev.compute(0, arrays)
    assert out0[0] == 10  # A
    assert out0[1] == 0   # B

    out1 = ev.compute(1, arrays)
    assert out1[0] == 10  # A still held
    assert out1[1] == 0   # B

    out2 = ev.compute(2, arrays)
    assert out2[0] == 0   # A expired
    assert out2[1] == 0   # B


def test_event_signal_refire_extends_hold():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    # 4 timestamps, 1 symbol (A)
    arrays = _arrays(
        ["A"],
        base_rows=[[10], [10], [10], [10]],
        trig_rows=[[1], [1], [0], [0]],
    )

    assert ev.compute(0, arrays)[0] == 10
    assert ev.compute(1, arrays)[0] == 10  # refire extends
    assert ev.compute(2, arrays)[0] == 10  # still held from refire at t1
    assert ev.compute(3, arrays)[0] == 0   # expired


def test_event_signal_threshold_strict_greater():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=1.0, max_holddays=1, bet_all=True)

    # 2 timestamps, 1 symbol (A)
    arrays = _arrays(
        ["A"],
        base_rows=[[10], [10]],
        trig_rows=[[1.0], [1.1]],
    )

    # equal threshold does not fire
    out0 = ev.compute(0, arrays)
    assert out0[0] == 0

    # above threshold fires
    out1 = ev.compute(1, arrays)
    assert out1[0] == 10


def test_event_signal_same_timestamp_idempotent():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    arrays = _arrays(
        ["A"],
        base_rows=[[10]],
        trig_rows=[[1]],
    )

    out0 = ev.compute(0, arrays)
    out0b = ev.compute(0, arrays)
    assert out0b[0] == out0[0]


def test_event_signal_bet_all_false_no_mask():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=False)

    arrays = _arrays(
        ["A", "B"],
        base_rows=[[10, 20]],
        trig_rows=[[1, 0]],
    )

    out = ev.compute(0, arrays)
    assert out[0] == 10  # A
    assert out[1] == 20  # B (no mask when bet_all=False)


def test_event_signal_universe_changes_hold_does_not_persist_without_presence():
    """Current semantics: hold decrements by timestamp; if a symbol disappears, it may expire before it reappears.

    Note: With the numpy migration, the symbol universe is fixed (same N across all bars).
    We test with a fixed universe where symbols that 'disappear' simply have zero trigger/base.
    """
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    # Fixed universe: A, B, C (3 symbols, 3 timestamps)
    arrays = _arrays(
        ["A", "B", "C"],
        base_rows=[
            [10, 10, 0],   # t0: A,B present, C absent
            [0, 10, 10],   # t1: A absent, B,C present
            [10, 10, 0],   # t2: A,B present, C absent
        ],
        trig_rows=[
            [1, 0, 0],     # t0: A fires
            [0, 0, 0],     # t1: nothing fires
            [0, 0, 0],     # t2: nothing fires
        ],
    )

    out0 = ev.compute(0, arrays)
    assert out0[0] == 10  # A active
    assert out0[1] == 0   # B not active

    out1 = ev.compute(1, arrays)
    assert out1[0] == 0   # A base=0 (absent), but still held
    assert out1[1] == 0   # B not active
    assert out1[2] == 0   # C not active

    out2 = ev.compute(2, arrays)
    assert out2[0] == 0   # A hold expired
    assert out2[1] == 0   # B not active


def test_event_signal_integer_symbol_index_roundtrip():
    """Integer symbols should still work with numpy arrays."""
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=1, bet_all=True)

    arrays = {
        "base": np.array([[10.0, 20.0]]),
        "trig": np.array([[1.0, 0.0]]),
        "__n_symbols__": 2,
        "__symbols__": np.array([1, 2]),
    }

    out0 = ev.compute(0, arrays)
    assert out0[0] == 10.0
    assert out0[1] == 0.0
