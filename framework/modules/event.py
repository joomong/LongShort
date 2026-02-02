from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import pandas as pd

from ..signal_base import Signal
from framework.exceptions import XmlCompileError


@dataclass
class EventSignal(Signal):
    tag = "event"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        """sig parameter unused: event reads from registry, not pipeline chain."""
        sig_id = (attrib.get("signal") or "").strip()
        trig_id = (attrib.get("trigger") or "").strip()
        if not sig_id:
            raise XmlCompileError("Missing required attribute: signal")
        if not trig_id:
            raise XmlCompileError("Missing required attribute: trigger")
        if sig_id not in registry:
            raise XmlCompileError(f"Unknown signal id: {sig_id!r}")
        if trig_id not in registry:
            raise XmlCompileError(f"Unknown signal id: {trig_id!r}")

        threshold_raw = (attrib.get("threshold") or "").strip()
        max_hold_raw = (attrib.get("maxHolddays") or "").strip()
        bet_all_raw = (attrib.get("betAll") or "1").strip()

        if not threshold_raw:
            raise XmlCompileError("Missing required attribute: threshold")
        if not max_hold_raw:
            raise XmlCompileError("Missing required attribute: maxHolddays")

        try:
            threshold = float(threshold_raw)
        except ValueError as e:
            raise XmlCompileError(f"Invalid threshold: {threshold_raw!r}") from e
        try:
            max_holddays = int(max_hold_raw)
        except ValueError as e:
            raise XmlCompileError(f"Invalid maxHolddays: {max_hold_raw!r}") from e
        if max_holddays <= 0:
            raise XmlCompileError(f"Invalid maxHolddays: {max_hold_raw!r}")
        if bet_all_raw not in {"0", "1"}:
            raise XmlCompileError(f"Invalid betAll: {bet_all_raw!r}")

        return cls(
            signal=registry[sig_id],
            trigger=registry[trig_id],
            threshold=threshold,
            max_holddays=max_holddays,
            bet_all=bet_all_raw == "1",
        )

    """Event-driven alpha wrapper.

    This module applies a holding mask driven by a trigger signal.

    Semantics (minimal, Phase 3):
    - If trigger[t, sym] > threshold, then sym becomes "active" for max_holddays bars,
      including the current bar.
    - If bet_all=True, output is base_signal masked by active set (inactive -> 0).

    Notes:
    - This Signal is stateful across compute() calls. Backtester calls compute()
      sequentially in timestamp order.
    """

    signal: Signal
    trigger: Signal
    threshold: float
    max_holddays: int
    bet_all: bool = True

    def __post_init__(self) -> None:
        if self.max_holddays <= 0:
            raise ValueError("max_holddays must be > 0")
        # remaining hold days per symbol
        self._hold: Dict[str, int] = {}
        self._last_ts: pd.Timestamp | None = None

    def _as_series(self, x, index: pd.Index) -> pd.Series:
        if isinstance(x, pd.Series):
            return x.reindex(index)
        if isinstance(x, dict):
            return pd.Series(x, index=index, dtype=float)
        if isinstance(x, (list, tuple)):
            return pd.Series(list(x), index=index, dtype=float)
        # scalar
        return pd.Series(float(x), index=index, dtype=float)

    def compute(self, timestamp: pd.Timestamp, data_slice: pd.DataFrame):
        base_raw = self.signal.compute(timestamp, data_slice)
        trig_raw = self.trigger.compute(timestamp, data_slice)

        # Determine symbol index (prefer data_slice index)
        idx = data_slice.index

        base = self._as_series(base_raw, idx).fillna(0.0)
        trig = self._as_series(trig_raw, idx).fillna(0.0)

        # decrement holds once per new timestamp
        if self._last_ts is None or timestamp != self._last_ts:
            for k in list(self._hold.keys()):
                self._hold[k] -= 1
                if self._hold[k] <= 0:
                    del self._hold[k]
            self._last_ts = timestamp

        # apply triggers
        fired = trig > self.threshold
        for sym, is_fired in fired.items():
            if bool(is_fired):
                self._hold[str(sym)] = self.max_holddays

        if not self.bet_all:
            # Minimal implementation: if bet_all is false, return base unchanged.
            return base

        active_syms = set(self._hold.keys())
        mask = pd.Series([str(s) in active_syms for s in idx], index=idx)
        return base.where(mask, 0.0)
