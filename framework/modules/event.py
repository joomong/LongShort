from __future__ import annotations

import numpy as np

from ..signal_base import Signal
from framework.exceptions import XmlCompileError

import numba


@numba.jit(nopython=True, cache=True)
def _event_kernel_numba(base, trig, hold, threshold, max_holddays, bet_all):
    """Numba kernel: hold decrement + trigger + mask."""
    N = len(base)
    out = np.empty(N, dtype=np.float64)
    for i in range(N):
        # Decrement hold
        h = hold[i] - 1
        if h < 0:
            h = 0
        # Fire trigger
        if trig[i] > threshold:
            h = max_holddays
        hold[i] = h
        # Output
        if bet_all:
            out[i] = base[i] if h > 0 else 0.0
        else:
            out[i] = base[i]
    return out

_event_kernel = _event_kernel_numba


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
    """

    def __init__(
        self,
        signal: Signal,
        trigger: Signal,
        threshold: float,
        max_holddays: int,
        bet_all: bool = True,
    ):
        if max_holddays <= 0:
            raise ValueError("max_holddays must be > 0")
        self.signal = signal
        self.trigger = trigger
        self.threshold = threshold
        self.max_holddays = max_holddays
        self.bet_all = bet_all
        self._hold: np.ndarray | None = None  # (N,) remaining hold days per symbol
        self._last_t_idx: int = -1

    def reset(self):
        self._hold = None
        self._last_t_idx = -1
        super().reset()

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        base = self.signal.compute(t_idx, arrays)
        trig = self.trigger.compute(t_idx, arrays)
        N = len(base)

        # Lazy init hold array
        if self._hold is None:
            self._hold = np.zeros(N, dtype=np.int32)

        # Only process once per new timestamp
        if t_idx != self._last_t_idx:
            base = _event_kernel(
                base, trig, self._hold,
                self.threshold, self.max_holddays, self.bet_all,
            )
            self._last_t_idx = t_idx
            return base

        # Same timestamp: return based on current hold state
        if not self.bet_all:
            return base
        active = self._hold > 0
        return np.where(active, base, 0.0)
