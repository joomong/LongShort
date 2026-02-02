import pandas as pd
import pytest

from framework.modules.event import EventSignal
from framework.modules.sources import Price


def _df(ts: str, symbols: list[str], base: list[float], trig: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"base": base, "trig": trig}, index=pd.Index(symbols, name="symbol"))


def test_event_signal_hold_window_off_by_one():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    t0 = pd.Timestamp("2024-01-01")
    t1 = pd.Timestamp("2024-01-02")
    t2 = pd.Timestamp("2024-01-03")

    out0 = ev.compute(t0, _df("t0", ["A", "B"], [10, 10], [1, 0]))
    assert out0.loc["A"] == 10
    assert out0.loc["B"] == 0

    out1 = ev.compute(t1, _df("t1", ["A", "B"], [10, 10], [0, 0]))
    assert out1.loc["A"] == 10
    assert out1.loc["B"] == 0

    out2 = ev.compute(t2, _df("t2", ["A", "B"], [10, 10], [0, 0]))
    assert out2.loc["A"] == 0
    assert out2.loc["B"] == 0


def test_event_signal_refire_extends_hold():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    t0 = pd.Timestamp("2024-01-01")
    t1 = pd.Timestamp("2024-01-02")
    t2 = pd.Timestamp("2024-01-03")
    t3 = pd.Timestamp("2024-01-04")

    assert ev.compute(t0, _df("t0", ["A"], [10], [1])).loc["A"] == 10
    assert ev.compute(t1, _df("t1", ["A"], [10], [1])).loc["A"] == 10
    assert ev.compute(t2, _df("t2", ["A"], [10], [0])).loc["A"] == 10
    assert ev.compute(t3, _df("t3", ["A"], [10], [0])).loc["A"] == 0


def test_event_signal_threshold_strict_greater():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=1.0, max_holddays=1, bet_all=True)

    t0 = pd.Timestamp("2024-01-01")

    # equal threshold does not fire
    out0 = ev.compute(t0, _df("t0", ["A"], [10], [1.0]))
    assert out0.loc["A"] == 0

    # above threshold fires
    out1 = ev.compute(pd.Timestamp("2024-01-02"), _df("t1", ["A"], [10], [1.1]))
    assert out1.loc["A"] == 10


def test_event_signal_same_timestamp_idempotent():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    t0 = pd.Timestamp("2024-01-01")
    df0 = _df("t0", ["A"], [10], [1])
    out0 = ev.compute(t0, df0)
    out0b = ev.compute(t0, df0)
    assert out0b.loc["A"] == out0.loc["A"]


def test_event_signal_bet_all_false_no_mask():
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=False)

    t0 = pd.Timestamp("2024-01-01")
    out = ev.compute(t0, _df("t0", ["A", "B"], [10, 20], [1, 0]))
    assert out.loc["A"] == 10
    assert out.loc["B"] == 20


def test_event_signal_universe_changes_hold_does_not_persist_without_presence():
    """Current semantics: hold decrements by timestamp; if a symbol disappears, it may expire before it reappears."""
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=2, bet_all=True)

    t0 = pd.Timestamp("2024-01-01")
    t1 = pd.Timestamp("2024-01-02")
    t2 = pd.Timestamp("2024-01-03")

    # t0: A,B exist; A fires
    out0 = ev.compute(t0, _df("t0", ["A", "B"], [10, 10], [1, 0]))
    assert out0.loc["A"] == 10
    assert out0.loc["B"] == 0

    # t1: universe changes to B,C (A absent)
    out1 = ev.compute(t1, _df("t1", ["B", "C"], [10, 10], [0, 0]))
    assert out1.loc["B"] == 0
    assert out1.loc["C"] == 0

    # t2: A returns; hold has already decremented/expired while A was absent
    out2 = ev.compute(t2, _df("t2", ["A", "B"], [10, 10], [0, 0]))
    assert out2.loc["A"] == 0
    assert out2.loc["B"] == 0


def test_event_signal_integer_symbol_index_roundtrip():
    """Integer index should still work; internal hold uses str(symbol)."""
    base_sig = Price("base")
    trig_sig = Price("trig")
    ev = EventSignal(signal=base_sig, trigger=trig_sig, threshold=0.0, max_holddays=1, bet_all=True)

    t0 = pd.Timestamp("2024-01-01")
    df0 = pd.DataFrame({"base": [10.0, 20.0], "trig": [1.0, 0.0]}, index=pd.Index([1, 2], name="symbol"))
    out0 = ev.compute(t0, df0)
    assert out0.loc[1] == 10.0
    assert out0.loc[2] == 0.0
