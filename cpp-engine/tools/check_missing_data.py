#!/usr/bin/env python3
"""Validate the missing-close / delisting accounting against an independent ledger.

Two independent jobs:

  --self-check        Encode the contract's hand-calculated examples
                      (docs/missing_data_validation_2026-09-11.md, cost 0.0005,
                      default price 100) and confirm the Python ledger in
                      missing_close_oracle.py reproduces every published number.
  --server BINARY     Build synthetic wide parquet panels plus a YAML config,
                      start an isolated server on a free port, POST the same
                      panels over /api/simulate for mode 0/1, missingClose
                      carry/error and defaultDelistingReturn 0/-1, and compare
                      the returned timeseries and missingData totals with the
                      ledger at 1e-9.

Cases that need the missing-data response contract are reported as SKIP when the
running server predates it, so this harness is usable before the engine lands.
Reads nothing from /home/base and always stops the server it started.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from missing_close_oracle import (  # noqa: E402
    LedgerConfig, LedgerError, LedgerResult, run_ledger,
)

TOLERANCE = 1e-9
# DataPanel keeps settlement returns as float32; an explicit R such as -0.4 only
# reaches the book to that precision.
FLOAT32_TOLERANCE = 1e-6
PORT_RANGE = range(18120, 18140)
NAN = float("nan")

# JSON key -> ledger diagnostic name. Every one of these is required in the
# response: a key that disappears is a regression, not a reason to skip a check.
MISSING_DATA_KEYS = {
    "missingHeldBars": "missing_held_count",
    "blockedOrders": "blocked_order_count",
    "blockedTradeWeight": "blocked_trade_weight",
    "missingEntries": "missing_entry_count",
    "resumedHoldings": "resumed_holding_count",
    "delistings": "delisting_count",
    "delistingPnl": "delisting_pnl",
    "settledGross": "settled_gross",
    "defaultDelistings": "default_delisting_count",
    "defaultDelistingGross": "default_delisting_gross",
    "staleSettlements": "stale_settlement_count",
    "terminalFrozenGross": "terminal_frozen_gross",
    "cancelledDelistingEntries": "pending_delisting_entry_count",
    # Added after the first independent review (F-08/F-09).
    "frozenGrossSum": "frozen_gross",
    "retirements": "retirement_count",
    # Added after the second review: end date versus surviving quote.
    "postEndDateQuoteBars": "post_end_date_quote_bars",
    "pendingSettlements": "pending_settlements",
    # Phase 2: successor (merger/rename) events.
    "successorConversions": "successor_conversion_count",
    "convertedGross": "converted_gross",
    "computedSettlementReturns": "computed_settlement_return_count",
}
# Per-bar series that must be present and the same length as the ledger's.
REQUIRED_SERIES = ("turnover", "maxWeight", "drawdown", "cumPnlGross", "nonzeroCount",
                   "frozenGross", "settledGross")
# Scenarios that also compare the per-settlement log against the ledger's own.
SETTLEMENT_LOG_SCENARIOS = ("successor_ratio_two", "mixed_cash_and_stock",
                            "explicit_fallback_outside_panel", "delist_default_minus_one_long")
# Fields of a settlement record the ledger and the engine must agree on exactly.
SETTLEMENT_LOG_FIELDS = ("bar", "exposure", "return", "source")
# The lead ruled these request values out of range; the server must answer 400.
REJECTED_DEFAULT_RETURNS = (-1.5, 3.0)
# The ledger names the offending bar and asset; a rejection only matches when the
# server refuses the same position, not merely when it fails somehow (review R3).
LEDGER_LOCATION = re.compile(r"bar (\d+), asset (\d+)")


# ---------------------------------------------------------------------------
# Scenario definition
# ---------------------------------------------------------------------------
@dataclass
class Check:
    """One published number from the contract's hand-calculation table."""
    bar: int
    # ret | gross_pnl | fee | turnover | position_count | h | trade | diag:<name>
    quantity: str
    expected: float
    symbol: Optional[str] = None
    # Default 1e-9; widen only where the pipeline itself limits precision.
    tolerance: Optional[float] = None

    def label(self) -> str:
        return f"t{self.bar} {self.quantity}" + (f"[{self.symbol}]" if self.symbol else "")


@dataclass
class Scenario:
    name: str
    case: str                # the contract table row this scenario encodes
    symbols: list
    close: np.ndarray
    signal: np.ndarray
    config: LedgerConfig
    checks: list = field(default_factory=list)
    mask: Optional[np.ndarray] = None
    settlement_bar: Optional[np.ndarray] = None
    settlement_return: Optional[np.ndarray] = None
    # Phase 2 successor columns. settlement_successor holds panel indices (-1 =
    # none/outside); successor_names overrides what the events parquet records,
    # which is how an id outside the panel is expressed.
    settlement_successor: Optional[np.ndarray] = None
    settlement_successor_ratio: Optional[np.ndarray] = None
    settlement_cash: Optional[np.ndarray] = None
    # Structural facts the events file declares, independent of pricing.
    has_stock_leg: Optional[np.ndarray] = None
    has_cash_leg: Optional[np.ndarray] = None
    successor_names: Optional[list] = None
    note: str = ""
    # An abnormal (non-NaN, non-positive or infinite) quote is bad data: the
    # ledger and the engine must both refuse the run, under either policy.
    expect_reject: bool = False

    @property
    def T(self) -> int:
        return self.close.shape[0]

    @property
    def has_events(self) -> bool:
        if self.settlement_bar is not None and bool((np.asarray(self.settlement_bar) >= 0).any()):
            return True
        # A null end date with a finite explicit return is still an events row.
        return (self.settlement_return is not None
                and bool(np.isfinite(np.asarray(self.settlement_return, dtype=np.float64)).any()))

    @property
    def has_missing(self) -> bool:
        values = np.asarray(self.close, dtype=np.float64)
        return bool((~(np.isfinite(values) & (values > 0))).any())

    def run(self) -> LedgerResult:
        # successor_names records what the events file declares, which is what
        # separates "no successor" from "a successor the panel cannot resolve".
        named = None
        if self.successor_names is not None:
            named = np.array([name is not None for name in self.successor_names], dtype=bool)
        return run_ledger(self.close, self.signal, self.mask, self.settlement_bar,
                          self.settlement_return, self.settlement_successor,
                          self.settlement_successor_ratio, self.settlement_cash, named,
                          self.has_stock_leg, self.has_cash_leg, self.config)


def observed(result: LedgerResult, symbols: list, check: Check) -> float:
    if check.quantity.startswith("diag:"):
        return float(result.diagnostics[check.quantity.split(":", 1)[1]][check.bar])
    if check.quantity in ("ret", "gross_pnl", "fee", "turnover", "position_count",
                          "long_weight", "short_weight", "max_weight"):
        return float(getattr(result, check.quantity)[check.bar])
    index = symbols.index(check.symbol)
    matrix = {"h": result.weights, "trade": result.trades, "desired": result.desired,
              "mark": result.marks_after}[check.quantity]
    return float(matrix[check.bar, index])


# ---------------------------------------------------------------------------
# The ten hand-calculated contract examples (cost 0.0005, default price 100)
# ---------------------------------------------------------------------------
def _panel(rows: list) -> np.ndarray:
    return np.array(rows, dtype=np.float64)


def hand_scenarios() -> list:
    fee = 0.0005
    scenarios = []

    # 1. A held long of 1 across 100 -> NaN -> NaN -> 80, rotating out on recovery.
    scenarios.append(Scenario(
        name="hold_gap_recovery", case="보유 long 1의 100→NaN→NaN→80",
        symbols=["A", "B"],
        close=_panel([[100, 100], [NAN, 100], [NAN, 100], [80, 100]]),
        signal=_panel([[1, 0], [1, 0], [1, 0], [0, 1]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[
            Check(1, "h", 1.0, "A"), Check(1, "trade", 0.0, "A"),
            Check(1, "fee", 0.0), Check(1, "ret", 0.0),
            Check(2, "h", 1.0, "A"), Check(2, "trade", 0.0, "A"),
            Check(2, "fee", 0.0), Check(2, "ret", 0.0),
            Check(3, "gross_pnl", -0.2), Check(3, "turnover", 2.0), Check(3, "ret", -0.201),
        ]))

    # 2. Blocked entry: intent +1 while the quote is missing must not open a book.
    scenarios.append(Scenario(
        name="blocked_entry", case="미보유 종목의 blocked entry",
        symbols=["A", "B"],
        close=_panel([[100, 100], [NAN, 100], [NAN, 100], [80, 100]]),
        signal=_panel([[0, 1], [1, 0], [1, 0], [1, 0]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[
            Check(1, "h", 0.0, "A"), Check(2, "h", 0.0, "A"),
            Check(3, "gross_pnl", 0.0), Check(3, "h", 1.0, "A"),
        ],
        note="the 100->80 decline must not reach an account that held nothing"))

    # 3. Pending replacement: queued 0 then -1 are dropped, the latest +1 executes.
    scenarios.append(Scenario(
        name="pending_replacement", case="pending 대체",
        symbols=["A", "B"],
        close=_panel([[100, 100], [NAN, 100], [NAN, 100], [100, 100]]),
        signal=_panel([[1, 0], [0, 0], [-1, 0], [1, 0]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[
            Check(3, "h", 1.0, "A"), Check(3, "trade", 0.0, "A"),
            Check(3, "turnover", 0.0), Check(3, "fee", 0.0),
        ]))

    # 4 + 5. Frozen budget: shrink only, then release on recovery.
    scenarios.append(Scenario(
        name="frozen_budget", case="frozen budget, 축소만 / frozen budget 초과",
        symbols=["A", "B", "C"],
        close=_panel([[100, 100, 100], [NAN, 100, 100], [NAN, 100, 100], [80, 100, 100]]),
        signal=_panel([[6, 2, 2], [3, 1, 0], [0, 1, -1], [0, 1, -1]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[
            Check(0, "h", 0.6, "A"), Check(0, "h", 0.2, "B"), Check(0, "h", 0.2, "C"),
            Check(1, "h", 0.6, "A"), Check(1, "h", 0.25, "B"), Check(1, "h", 0.0, "C"),
            Check(1, "turnover", 0.25), Check(1, "fee", 0.000125),
            Check(2, "h", 0.6, "A"), Check(2, "h", 0.2, "B"), Check(2, "h", -0.2, "C"),
            Check(3, "gross_pnl", -0.12),
            Check(3, "h", 0.0, "A"), Check(3, "h", 0.5, "B"), Check(3, "h", -0.5, "C"),
            Check(3, "turnover", 1.2), Check(3, "fee", 0.0006),
        ]))

    # 6. Default settlement return 0: the event alone costs nothing and is final.
    scenarios.append(Scenario(
        name="delist_default_zero", case="delist fallback 0",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [100, 100]]),
        signal=_panel([[1, NAN], [1, NAN], [1, NAN], [1, NAN]]),
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=0.0),
        settlement_bar=np.array([2, -1]),
        checks=[
            Check(2, "gross_pnl", 0.0), Check(2, "turnover", 0.0), Check(2, "fee", 0.0),
            Check(2, "h", 0.0, "A"), Check(3, "h", 0.0, "A"), Check(3, "turnover", 0.0),
        ],
        note="a later valid quote must not re-enter a retired name"))

    # 7. Default settlement return -1, long and short.
    for side, sign in (("long", 1.0), ("short", -1.0)):
        scenarios.append(Scenario(
            name=f"delist_default_minus_one_{side}", case="delist fallback -1",
            symbols=["A", "B"],
            close=_panel([[100, 100], [100, 100], [NAN, 100], [100, 100]]),
            signal=_panel([[sign, NAN]] * 4),
            config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
            settlement_bar=np.array([2, -1]),
            checks=[
                Check(1, "h", sign, "A"),
                Check(2, "gross_pnl", -sign), Check(2, "h", 0.0, "A"),
            ],
            note="the settlement return replaces the quote move, never adds to it"))

    # 8. Explicit override beats either default.
    for default_return in (0.0, -1.0):
        tag = "zero" if default_return == 0.0 else "minus_one"
        scenarios.append(Scenario(
            name=f"explicit_override_default_{tag}", case="explicit event override",
            symbols=["A", "B"],
            close=_panel([[100, 100], [100, 100], [NAN, 100], [100, 100]]),
            signal=_panel([[1, NAN]] * 4),
            config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=default_return),
            settlement_bar=np.array([2, -1]),
            settlement_return=np.array([-0.25, NAN]),
            checks=[Check(2, "gross_pnl", -0.25), Check(2, "h", 0.0, "A")]))

    # 9. Event on a bar that does have a fresh quote.
    # SUPERSEDED by the second review: an end date that a live quote contradicts
    # no longer settles on the spot. The bar is valued normally (+0.2 here) and
    # the settlement waits for the first bar without a quote, where R applies to
    # the mark as of then. The table's original "gross is -0.5 on the event bar"
    # expectation belongs to the old rule and is replaced, not bent to the code.
    scenarios.append(Scenario(
        name="event_with_fresh_quote", case="event + fresh quote (2차 리뷰로 갱신)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [120, 100], [NAN, 100]]),
        signal=_panel([[1, NAN]] * 4),
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=0.0),
        settlement_bar=np.array([2, -1]),
        settlement_return=np.array([-0.5, NAN]),
        checks=[Check(2, "gross_pnl", 0.2), Check(2, "h", 1.0, "A"),
                Check(2, "diag:post_end_date_quote_bars", 1),
                Check(2, "diag:pending_settlement_count", 1),
                Check(3, "gross_pnl", -0.5), Check(3, "h", 0.0, "A"),
                Check(3, "diag:delisting_pnl", -0.5),
                Check(3, "diag:pending_settlement_count", 0)],
        note="quote 120 on the end-date bar is real P&L; R=-0.5 settles on the next NaN bar"))

    # 10. simStart with mode 0 and mode 1; a warmup event and warmup price moves.
    for mode in (0, 1):
        last_a, last_b = (1.0, 0.0) if mode == 1 else (0.0, 1.0)
        scenarios.append(Scenario(
            name=f"warmup_mode{mode}", case="simStart/mode0/1",
            symbols=["A", "B", "C"],
            close=_panel([[100, 100, 100], [110, 100, NAN], [NAN, 100, NAN], [120, 100, NAN]]),
            signal=_panel([[1, 0, 0], [1, 0, 0], [1, 0, 0], [0, 1, 0]]),
            config=LedgerConfig(mode=mode, fee_rate=fee, sim_start_index=2),
            settlement_bar=np.array([-1, -1, 1]),
            checks=[
                Check(0, "ret", 0.0), Check(0, "turnover", 0.0), Check(0, "position_count", 0),
                Check(1, "ret", 0.0), Check(1, "turnover", 0.0), Check(1, "position_count", 0),
                Check(2, "turnover", 0.0), Check(2, "fee", 0.0), Check(2, "gross_pnl", 0.0),
                Check(3, "h", last_a, "A"), Check(3, "h", last_b, "B"),
            ],
            note="warmup price moves and the warmup event create no historical P&L"))
    return scenarios


def baseline_scenarios() -> list:
    """Complete panels with no missing quote and no event: the bit-identical regression.

    These stay comparable against a server that predates the contract, so the
    harness keeps proving that carry accounting leaves clean input untouched.
    """
    rng = np.random.default_rng(20260911)
    T, symbols = 12, ["A", "B", "C", "D"]
    base = 100.0 + np.cumsum(rng.normal(0.0, 1.5, size=(T, len(symbols))), axis=0)
    close = np.round(np.abs(base) + 20.0, 4)
    signal = np.round(rng.normal(0.0, 1.0, size=(T, len(symbols))), 4)
    signal[4, 1] = NAN            # a NaN signal still means target zero
    signal[7, :] = 0.0            # an all-zero bar flattens the book
    mask = np.ones((T, len(symbols)), dtype=bool)
    mask[6:, 3] = False           # a symbol leaving the signal-date universe
    scenarios = []
    for mode in (0, 1):
        for start in (0, 2):
            scenarios.append(Scenario(
                name=f"clean_panel_mode{mode}_start{start}", case="결측·이벤트 없는 입력",
                symbols=list(symbols), close=close.copy(), signal=signal.copy(), mask=mask.copy(),
                config=LedgerConfig(mode=mode, fee_rate=0.0005, sim_start_index=start)))
    return scenarios


def contract_scenarios() -> list:
    """Contract behaviour with no published hand number, plus the independent review's gaps.

    The expected values here are fixed from the contract text (brief section 2)
    and from the review's missing-test list, not from whatever the engine does.
    """
    fee = 0.0005
    scenarios = []

    # A frozen holding settled by staleness alone after three missing bars.
    scenarios.append(Scenario(
        name="stale_settlement", case="stale_quote_settle_bars (표 외)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [NAN, 100], [NAN, 100], [NAN, 100], [100, 100]]),
        signal=_panel([[1, 0]] * 5),
        config=LedgerConfig(mode=0, fee_rate=fee, stale_quote_settle_bars=3),
        checks=[Check(2, "h", 1.0, "A"), Check(3, "h", 0.0, "A"),
                Check(3, "diag:stale_settlement_count", 1), Check(3, "diag:delisting_count", 1),
                Check(3, "diag:settled_gross", 1.0), Check(3, "diag:retirement_count", 1),
                Check(3, "turnover", 0.0)],
        note="an event-free frozen holding must retire at the third missing bar"))

    # Mode 1 settlement cancels the intent queued on the previous bar.
    scenarios.append(Scenario(
        name="mode1_pending_cancelled", case="정산 시 pending 취소 (표 외)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [100, 100]]),
        signal=_panel([[1, NAN]] * 4),
        config=LedgerConfig(mode=1, fee_rate=fee),
        settlement_bar=np.array([2, -1]),
        checks=[Check(2, "h", 0.0, "A"), Check(2, "turnover", 0.0),
                Check(2, "diag:pending_delisting_entry_count", 1)],
        note="the D1 order queued at t1 must not execute into a name retired at t2"))

    # settlement_bar == 0: the security ended before the panel starts.
    scenarios.append(Scenario(
        name="retired_before_panel", case="settlement_bar==0 (표 외)",
        symbols=["A", "B"],
        close=_panel([[NAN, 100], [NAN, 100], [NAN, 100], [NAN, 100]]),
        signal=_panel([[1, 1]] * 4),
        config=LedgerConfig(mode=0, fee_rate=fee),
        settlement_bar=np.array([0, -1]),
        checks=[Check(0, "h", 0.0, "A"), Check(0, "h", 1.0, "B"), Check(0, "gross_pnl", 0.0),
                Check(3, "h", 0.0, "A")],
        note="a name that ended before the panel is never held and never an intent"))

    # Review gap 1 / F-01: stale settlement with a finite explicit return.
    # Brief section 2.1 reads "R = settlement_return finite ? that : default" with
    # no event/stale split, so the explicit -0.4 must win on the stale path too.
    scenarios.append(Scenario(
        name="stale_with_explicit_return", case="stale × explicit R (리뷰 F-01)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [NAN, 100], [NAN, 100], [100, 100]]),
        signal=_panel([[1, 0]] * 4),
        config=LedgerConfig(mode=0, fee_rate=fee, stale_quote_settle_bars=2),
        settlement_bar=np.array([-1, -1]),
        settlement_return=np.array([-0.4, NAN]),
        checks=[Check(1, "h", 1.0, "A"),
                # -0.4 has no exact float32 form and DataPanel stores R as float,
                # so the contract number holds only to float32 precision here.
                Check(2, "gross_pnl", -0.4, tolerance=FLOAT32_TOLERANCE),
                Check(2, "h", 0.0, "A"), Check(2, "turnover", 0.0),
                Check(2, "diag:delisting_pnl", -0.4, tolerance=FLOAT32_TOLERANCE),
                Check(2, "diag:delisting_count", 1),
                Check(2, "diag:stale_settlement_count", 1),
                Check(2, "diag:default_delisting_count", 0),
                Check(2, "diag:default_delisting_gross", 0.0),
                Check(3, "h", 0.0, "A")],
        note="explicit R must beat the default on the stale path as well as the event path"))

    # Review gap 4: two holdings frozen at once.
    scenarios.append(Scenario(
        name="two_frozen_budget", case="동시 2종목 동결 (리뷰 §3-4)",
        symbols=["A", "B", "C", "D"],
        close=_panel([[100, 100, 100, 100], [NAN, NAN, 100, 100], [100, 100, 100, 100]]),
        signal=_panel([[4, 3, 2, 1], [0, 0, 1, 1], [0, 0, 1, 1]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[Check(0, "h", 0.4, "A"), Check(0, "h", 0.3, "B"),
                Check(1, "h", 0.4, "A"), Check(1, "h", 0.3, "B"),
                Check(1, "h", 0.15, "C"), Check(1, "h", 0.15, "D"),
                Check(1, "turnover", 0.1), Check(1, "fee", 0.00005),
                Check(1, "diag:frozen_gross", 0.7), Check(1, "diag:blocked_trade_weight", 0.7),
                Check(1, "diag:blocked_order_count", 2),
                Check(2, "h", 0.0, "A"), Check(2, "h", 0.5, "C"), Check(2, "turnover", 1.4)],
        note="F=0.7 leaves room 0.3 for a target gross of 1.0"))

    # Review gap 5: the frozen budget consumes the whole book.
    scenarios.append(Scenario(
        name="frozen_budget_exhausted", case="F≥1 예산 0 (리뷰 §3-5)",
        symbols=["A", "B", "C"],
        close=_panel([[100, 100, 100], [NAN, NAN, 100], [NAN, NAN, 100], [100, 100, 100]]),
        signal=_panel([[1, 1, 0], [0, 0, 1], [0, 0, 1], [0, 0, 1]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[Check(0, "h", 0.5, "A"),
                Check(1, "h", 0.0, "C"), Check(1, "turnover", 0.0), Check(1, "fee", 0.0),
                Check(1, "position_count", 2), Check(1, "diag:frozen_gross", 1.0),
                Check(2, "h", 0.0, "C"),
                Check(3, "h", 0.0, "A"), Check(3, "h", 1.0, "C"), Check(3, "turnover", 2.0)],
        note="room = max(0, 1-F) = 0, so the tradable target flattens to zero, never flips sign"))

    # Review gap 6: the universe drops a name while its quote is missing.
    mask = np.ones((5, 3), dtype=bool)
    mask[2:, 0] = False
    scenarios.append(Scenario(
        name="universe_exit_while_missing", case="결측 중 유니버스 이탈 (리뷰 §3-6)",
        symbols=["A", "B", "C"],
        close=_panel([[100, 100, 100]] + [[NAN, 100, 100]] * 4),
        signal=_panel([[2, 1, 1], [2, 1, 1], [2, 3, 1], [2, 3, 1], [2, 3, 1]]),
        mask=mask,
        config=LedgerConfig(mode=0, fee_rate=fee),
        checks=[Check(0, "h", 0.5, "A"),
                Check(2, "h", 0.5, "A"), Check(2, "h", 0.375, "B"), Check(2, "h", 0.125, "C"),
                Check(2, "turnover", 0.25),
                Check(4, "h", 0.5, "A"), Check(4, "turnover", 0.0),
                Check(4, "diag:frozen_gross", 0.5), Check(4, "diag:delisting_count", 0),
                Check(4, "diag:retirement_count", 0)],
        note="leaving the universe removes the intent, never the frozen holding"))

    # Review gap 9: the same id quotes again right after settlement.
    scenarios.append(Scenario(
        name="relisted_after_settlement", case="정산 후 호가 재개 (리뷰 §3-9)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [120, 100], [150, 100]]),
        signal=_panel([[1, 0], [1, 0], [1, 1], [1, 1], [1, 1]]),
        config=LedgerConfig(mode=0, fee_rate=fee),
        settlement_bar=np.array([2, -1]),
        checks=[Check(2, "gross_pnl", 0.0), Check(2, "h", 0.0, "A"), Check(2, "h", 1.0, "B"),
                Check(3, "gross_pnl", 0.0), Check(3, "h", 0.0, "A"), Check(3, "turnover", 0.0),
                Check(4, "gross_pnl", 0.0), Check(4, "h", 0.0, "A"), Check(4, "turnover", 0.0)],
        note="the +20% and +25% post-settlement quotes must not reach a retired book"))
    # Second review (a): the end date lands on a bar that still quotes.
    for mode in (0, 1):
        scenarios.append(Scenario(
            name=f"deferred_settlement_mode{mode}", case="종료일 bar에 호가 존재 (2차 리뷰)",
            symbols=["A", "B"],
            close=_panel([[100, 100], [100, 100], [120, 100], [NAN, 100], [100, 100]]),
            signal=_panel([[1, 0]] * 5),
            config=LedgerConfig(mode=mode, fee_rate=fee, default_delisting_return=0.0),
            settlement_bar=np.array([2, -1]),
            checks=[Check(2, "gross_pnl", 0.2), Check(2, "h", 1.0, "A"),
                    Check(2, "diag:post_end_date_quote_bars", 1),
                    Check(2, "diag:pending_settlement_count", 1),
                    Check(3, "gross_pnl", 0.0), Check(3, "h", 0.0, "A"),
                    Check(3, "diag:retirement_count", 1),
                    Check(3, "diag:pending_settlement_count", 0),
                    Check(4, "h", 0.0, "A"), Check(4, "turnover", 0.0)]
                   + ([Check(3, "diag:pending_delisting_entry_count", 1)] if mode == 1 else []),
            note="Wolfspeed shape: value the surviving quote, settle on the first NaN bar"))

    # Second review (b): the quote outlives the panel, so nothing ever settles.
    scenarios.append(Scenario(
        name="quotes_outlive_end_date", case="종료일 이후 호가 지속 (2차 리뷰)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [110, 100], [110, 100], [110, 100]]),
        signal=_panel([[1, 0]] * 5),
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        checks=[Check(2, "gross_pnl", 0.1), Check(2, "diag:post_end_date_quote_bars", 1),
                Check(4, "h", 1.0, "A"), Check(4, "diag:post_end_date_quote_bars", 1),
                Check(4, "diag:pending_settlement_count", 1),
                Check(4, "diag:retirement_count", 0), Check(4, "diag:delisting_count", 0),
                Check(4, "diag:delisting_pnl", 0.0)],
        note="an unsettled event stays pending; R=-1 must not be charged on a quoting name"))

    # Second review (c): abnormal quotes are bad data under either policy.
    for value, label in ((0.0, "zero"), (-1.0, "negative"), (np.inf, "inf")):
        scenarios.append(Scenario(
            name=f"abnormal_{label}_price", case="0/음수/inf 가격 거부 (2차 리뷰)",
            symbols=["A", "B"],
            close=_panel([[100, 100], [value, 100], [100, 100]]),
            signal=_panel([[1, 0]] * 3),
            config=LedgerConfig(mode=0, fee_rate=fee),
            expect_reject=True,
            note="carry covers NaN only; 0, negative and infinite quotes must fail fast"))

    # ---- Phase 2: successor (merger / rename) events ----------------------
    # A share exchange hands the book to the surviving security instead of cash.
    for mode in (0, 1):
        switch = 2 if mode == 0 else 1   # the bar whose intent first targets B
        signal = _panel([[1, 0]] * 5)
        signal[switch:] = [0, 1]
        scenarios.append(Scenario(
            name=f"successor_conversion_mode{mode}", case="승계 quote 있음 (P2)",
            symbols=["A", "B"],
            close=_panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100], [NAN, 110]]),
            signal=signal,
            config=LedgerConfig(mode=mode, fee_rate=fee, default_delisting_return=-1.0),
            settlement_bar=np.array([2, -1]),
            settlement_successor=np.array([1, -1]),
            settlement_successor_ratio=np.array([1.0, NAN]),
            checks=[Check(2, "gross_pnl", 0.0), Check(2, "h", 1.0, "B"),
                    Check(2, "turnover", 0.0), Check(2, "fee", 0.0),
                    Check(2, "diag:successor_conversion_count", 1),
                    Check(2, "diag:converted_gross", 1.0),
                    Check(2, "diag:computed_settlement_return_count", 1),
                    Check(2, "diag:default_delisting_count", 0),
                    # Lead ruling: a handover is not cash-settled exposure.
                    Check(2, "diag:settled_gross", 0.0),
                    Check(2, "diag:delisting_pnl", 0.0),
                    Check(4, "gross_pnl", 0.1), Check(4, "h", 1.0, "B")],
            note="ratio 1 at an unchanged price: R computes to 0, default -1 must not be used"))

    # A ratio that is not 1 changes both the settlement return and the handover.
    ratio_signal = _panel([[1, 0]] * 4)
    ratio_signal[2:] = [0, 1]
    scenarios.append(Scenario(
        name="successor_ratio_two", case="승계 ratio≠1 (P2)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 60], [NAN, 60]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_successor=np.array([1, -1]),
        settlement_successor_ratio=np.array([2.0, NAN]),
        checks=[Check(2, "gross_pnl", 0.2), Check(2, "diag:delisting_pnl", 0.2),
                Check(2, "diag:converted_gross", 1.2), Check(2, "h", 1.0, "B"),
                Check(2, "turnover", 0.2), Check(2, "fee", 0.0001),
                Check(2, "diag:computed_settlement_return_count", 1)],
        note="R = 2*60/100 - 1 = 0.2; 1.2 lands in B and only the excess 0.2 trades"))

    # Team review: the engine values the deal itself, so a computable settlement
    # outranks the explicit total return the events file carries.
    scenarios.append(Scenario(
        name="successor_with_explicit_return", case="계산 > explicit 우선순위 (팀리뷰)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 60], [NAN, 60]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_return=np.array([-0.25, NAN]),
        settlement_successor=np.array([1, -1]),
        settlement_successor_ratio=np.array([2.0, NAN]),
        checks=[Check(2, "gross_pnl", 0.2), Check(2, "diag:converted_gross", 1.2),
                Check(2, "diag:settled_gross", 0.0),
                Check(2, "diag:computed_settlement_return_count", 1),
                Check(2, "diag:default_delisting_count", 0),
                Check(2, "h", 1.0, "B"), Check(2, "turnover", 0.2), Check(2, "fee", 0.0001)],
        note="V = 2*60 = 120 is computable, so explicit -0.25 is ignored"))

    # No quote for the successor on the settlement bar: cash, not conversion.
    scenarios.append(Scenario(
        name="successor_quote_missing", case="승계 quote 없음 (P2)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, NAN], [NAN, 100]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_successor=np.array([1, -1]),
        # The generator cannot restate a ratio it cannot price, so the file
        # carries NaN here while hasStockLeg still records the deal's structure.
        settlement_successor_ratio=np.array([NAN, NAN]),
        has_stock_leg=np.array([True, False]),
        checks=[Check(2, "gross_pnl", -1.0), Check(2, "diag:successor_conversion_count", 0),
                Check(2, "diag:converted_gross", 0.0),
                Check(2, "diag:default_delisting_count", 1),
                Check(2, "diag:computed_settlement_return_count", 0),
                Check(2, "h", 0.0, "B"), Check(2, "diag:missing_entry_count", 1),
                Check(3, "h", 1.0, "B")],
        note="a successor that is not quoting cannot receive the book"))

    # The events file names a successor that the panel does not contain.
    scenarios.append(Scenario(
        name="successor_outside_panel", case="승계 대상 패널 밖 (P2)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=0.0),
        settlement_bar=np.array([2, -1]),
        settlement_successor=np.array([-1, -1]),
        successor_names=["ZZZ_NOT_IN_PANEL", None],
        checks=[Check(2, "gross_pnl", 0.0), Check(2, "diag:successor_conversion_count", 0),
                Check(2, "diag:default_delisting_count", 1),
                Check(2, "diag:computed_settlement_return_count", 0),
                Check(2, "h", 1.0, "B"), Check(2, "turnover", 1.0)],
        note="an unloadable successor id falls back to the cash default"))

    # The handover is free; the rebalance that follows it is not.
    rebalance_signal = _panel([[1, 0, 0], [1, 0, 0], [0, 1, 0], [0, 1, 1]])
    scenarios.append(Scenario(
        name="successor_then_rebalance", case="승계 후 리밸런스 turnover (P2)",
        symbols=["A", "B", "C"],
        close=_panel([[100, 100, 100], [100, 100, 100], [NAN, 100, 100], [NAN, 100, 100]]),
        signal=rebalance_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1, -1]),
        settlement_successor=np.array([1, -1, -1]),
        settlement_successor_ratio=np.array([1.0, NAN, NAN]),
        checks=[Check(2, "h", 1.0, "B"), Check(2, "turnover", 0.0), Check(2, "fee", 0.0),
                Check(2, "diag:successor_conversion_count", 1),
                Check(3, "h", 0.5, "B"), Check(3, "h", 0.5, "C"),
                Check(3, "turnover", 1.0), Check(3, "fee", 0.0005)],
        note="conversion costs nothing; the next execution pays the usual turnover"))

    # ---- Team review: per-share value V = cash + ratio * P_successor ------
    # Half cash, half stock: only the share leg crosses to the successor.
    scenarios.append(Scenario(
        name="mixed_cash_and_stock", case="혼합 딜 현금50/주식50 (팀리뷰)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_successor=np.array([1, -1]),
        settlement_successor_ratio=np.array([0.5, NAN]),
        settlement_cash=np.array([50.0, NAN]),
        checks=[Check(2, "gross_pnl", 0.0), Check(2, "diag:delisting_pnl", 0.0),
                Check(2, "diag:converted_gross", 0.5), Check(2, "diag:settled_gross", 0.5),
                Check(2, "diag:computed_settlement_return_count", 1),
                Check(2, "diag:successor_conversion_count", 1),
                Check(2, "diag:default_delisting_count", 0),
                Check(2, "h", 1.0, "B"),
                # 0.5 arrived free, so only the remaining 0.5 is a market trade.
                Check(2, "turnover", 0.5), Check(2, "fee", 0.00025)],
        note="V = 50 cash + 0.5*100 stock = 100 = mark, so R is 0 and the legs split 50/50"))

    # Cash only, no successor: the engine still values the deal instead of the default.
    scenarios.append(Scenario(
        name="cash_only_deal", case="현금 전용 딜 엔진 계산 (팀리뷰)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_cash=np.array([80.0, NAN]),
        checks=[Check(2, "gross_pnl", -0.2), Check(2, "diag:delisting_pnl", -0.2),
                # Exposure basis: a whole cash deal settles all of |h_prev|.
                Check(2, "diag:settled_gross", 1.0), Check(2, "diag:converted_gross", 0.0),
                Check(2, "diag:computed_settlement_return_count", 1),
                Check(2, "diag:successor_conversion_count", 0),
                Check(2, "diag:default_delisting_count", 0)],
        note="R = 80/100 - 1 = -0.2; the config default -1.0 must not be used"))

    # Nothing to value: explicit is the fallback, the default only after that.
    scenarios.append(Scenario(
        name="explicit_fallback_outside_panel", case="계산 불가 → explicit 폴백 (팀리뷰)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_return=np.array([-0.3, NAN]),
        settlement_successor=np.array([-1, -1]),
        successor_names=["ZZZ_NOT_IN_PANEL", None],
        # -0.3 has no exact float32 form and DataPanel stores R as float.
        checks=[Check(2, "gross_pnl", -0.3, tolerance=FLOAT32_TOLERANCE),
                Check(2, "diag:settled_gross", 1.0), Check(2, "diag:converted_gross", 0.0),
                Check(2, "diag:computed_settlement_return_count", 0),
                Check(2, "diag:successor_conversion_count", 0),
                Check(2, "diag:default_delisting_count", 0)],
        note="successor outside the panel and no cash: fall back to explicit -0.3, not -1.0"))

    # The free handover is the share leg only; the rebalance after it is charged.
    mixed_rebalance = _panel([[1, 0, 0], [1, 0, 0], [0, 1, 0], [0, 1, 1]])
    scenarios.append(Scenario(
        name="mixed_deal_then_rebalance", case="혼합 딜 후 리밸런스 turnover (팀리뷰)",
        symbols=["A", "B", "C"],
        close=_panel([[100, 100, 100], [100, 100, 100], [NAN, 100, 100], [NAN, 100, 100]]),
        signal=mixed_rebalance,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1, -1]),
        settlement_successor=np.array([1, -1, -1]),
        settlement_successor_ratio=np.array([0.5, NAN, NAN]),
        settlement_cash=np.array([50.0, NAN, NAN]),
        checks=[Check(2, "diag:converted_gross", 0.5), Check(2, "diag:settled_gross", 0.5),
                Check(2, "h", 1.0, "B"), Check(2, "turnover", 0.5), Check(2, "fee", 0.00025),
                Check(3, "h", 0.5, "B"), Check(3, "h", 0.5, "C"),
                Check(3, "turnover", 1.0), Check(3, "fee", 0.0005)],
        note="only the 0.5 share leg is free; the 0.5 top-up and the next rebalance both trade"))

    # Section 11 finding: a named successor that cannot be resolved makes the
    # share leg unknowable, so the deal is not valuable from its cash leg alone.
    for label, close_panel, indices, names in (
        ("no_quote", _panel([[100, 100], [100, 100], [NAN, NAN], [NAN, 100]]),
         np.array([1, -1]), ["B", None]),
        ("outside_panel", _panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100]]),
         np.array([-1, -1]), ["ZZZ_NOT_IN_PANEL", None]),
    ):
        scenarios.append(Scenario(
            name=f"mixed_unusable_successor_{label}", case="혼합 딜 승계 불가 → explicit (§11)",
            symbols=["A", "B"], close=close_panel, signal=ratio_signal,
            config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
            settlement_bar=np.array([2, -1]),
            settlement_return=np.array([-0.3, NAN]),
            settlement_successor=indices,
            settlement_successor_ratio=np.array([NAN, NAN]),
            settlement_cash=np.array([80.0, NAN]),
            has_stock_leg=np.array([True, False]),
            successor_names=names,
            checks=[Check(2, "gross_pnl", -0.3, tolerance=FLOAT32_TOLERANCE),
                    Check(2, "diag:computed_settlement_return_count", 0),
                    Check(2, "diag:successor_conversion_count", 0),
                    Check(2, "diag:converted_gross", 0.0),
                    Check(2, "diag:settled_gross", 1.0),
                    Check(2, "diag:default_delisting_count", 0)],
            note="cash 80 alone would read -0.2 and throw away the unpriced share leg"))

    # Phase 2e E-2: a deliverable successor but an unknown exchange ratio. The
    # share leg is declared and priceable in principle, so the cash leg alone
    # must not value the deal; the file's own return is used instead.
    scenarios.append(Scenario(
        name="mixed_stock_leg_ratio_unknown", case="혼합+ratio NaN → 파일 R (2e)",
        symbols=["A", "B"],
        close=_panel([[100, 100], [100, 100], [NAN, 100], [NAN, 100]]),
        signal=ratio_signal,
        config=LedgerConfig(mode=0, fee_rate=fee, default_delisting_return=-1.0),
        settlement_bar=np.array([2, -1]),
        settlement_return=np.array([-0.3, NAN]),
        settlement_successor=np.array([1, -1]),
        settlement_successor_ratio=np.array([NAN, NAN]),
        settlement_cash=np.array([80.0, NAN]),
        has_stock_leg=np.array([True, False]),
        has_cash_leg=np.array([True, False]),
        successor_names=["B", None],
        checks=[Check(2, "gross_pnl", -0.3, tolerance=FLOAT32_TOLERANCE),
                Check(2, "diag:computed_settlement_return_count", 0),
                Check(2, "diag:successor_conversion_count", 0),
                Check(2, "diag:converted_gross", 0.0),
                Check(2, "diag:settled_gross", 1.0),
                Check(2, "diag:default_delisting_count", 0)],
        note="cash 80 alone would read -0.2 and silently drop the share leg"))

    return scenarios


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------
def self_check(scenarios: list, group: str = "contract table") -> dict:
    rows, failures = [], 0
    for scenario in scenarios:
        if scenario.expect_reject:
            for policy in ("carry", "error"):
                probe = Scenario(**{**scenario.__dict__})
                probe.config = LedgerConfig(
                    mode=scenario.config.mode, fee_rate=scenario.config.fee_rate, policy=policy,
                    default_delisting_return=scenario.config.default_delisting_return,
                    stale_quote_settle_bars=scenario.config.stale_quote_settle_bars,
                    sim_start_index=scenario.config.sim_start_index)
                try:
                    probe.run()
                except LedgerError as error:
                    rows.append({"group": group, "scenario": scenario.name, "case": scenario.case,
                                 "check": f"{policy} rejects", "expected": "LedgerError",
                                 "ledger": str(error), "verdict": "PASS"})
                else:
                    rows.append({"group": group, "scenario": scenario.name, "case": scenario.case,
                                 "check": f"{policy} rejects", "expected": "LedgerError",
                                 "ledger": "accepted", "verdict": "FAIL"})
                    failures += 1
            continue
        if not scenario.checks:
            continue
        try:
            result = scenario.run()
        except LedgerError as error:
            rows.append({"group": group, "scenario": scenario.name, "case": scenario.case,
                         "check": "-", "expected": None, "ledger": None,
                         "verdict": "ERROR", "detail": str(error)})
            failures += 1
            continue
        for check in scenario.checks:
            value = observed(result, scenario.symbols, check)
            ok = abs(value - check.expected) <= (check.tolerance or TOLERANCE)
            failures += 0 if ok else 1
            rows.append({"group": group, "scenario": scenario.name, "case": scenario.case,
                         "check": check.label(), "expected": check.expected,
                         "ledger": value, "verdict": "PASS" if ok else "FAIL"})
    return {"rows": rows, "failures": failures, "checks": len(rows),
            "scenarios": sum(1 for scenario in scenarios
                             if scenario.checks or scenario.expect_reject)}


def merge_self_checks(*reports) -> dict:
    merged = {"rows": [], "failures": 0, "checks": 0, "scenarios": 0}
    for report in reports:
        for key in ("failures", "checks", "scenarios"):
            merged[key] += report[key]
        merged["rows"] += report["rows"]
    return merged


def print_self_check(report: dict) -> None:
    width = max((len(row["scenario"]) for row in report["rows"]), default=8)
    print(f"{'scenario'.ljust(width)}  {'check'.ljust(18)}  {'expected':>14}  {'ledger':>14}  verdict")
    for row in report["rows"]:
        expected = ("-" if row["expected"] is None else row["expected"]
                    if isinstance(row["expected"], str) else f"{row['expected']:.9g}")
        ledger = ("-" if row["ledger"] is None else row["ledger"]
                  if isinstance(row["ledger"], str) else f"{row['ledger']:.9g}")
        ledger = ledger if len(ledger) < 40 else ledger[:37] + "..."
        print(f"{row['scenario'].ljust(width)}  {row['check'].ljust(18)}  "
              f"{expected:>14}  {ledger:>14}  {row['verdict']}"
              + (f"  {row.get('detail','')}" if row.get("detail") else ""))
    print(f"\n{report['checks']} checks over {report['scenarios']} scenarios, "
          f"{report['failures']} failure(s)")


# ---------------------------------------------------------------------------
# HTTP comparison
# ---------------------------------------------------------------------------
def request(base: str, path: str, payload=None, timeout: float = 120.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def free_port(attempts: int = 20, pause: float = 1.0) -> int:
    # SO_REUSEADDR mirrors the server's own listener so a just-stopped port in
    # TIME_WAIT is usable again; repeated restarts otherwise exhaust the range.
    for attempt in range(attempts):
        for port in PORT_RANGE:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
                return port
        if attempt + 1 < attempts:
            time.sleep(pause)
    raise RuntimeError(f"No free port in {PORT_RANGE.start}..{PORT_RANGE.stop - 1}")


class TestServer:
    """An isolated server process; callers may poll it between requests."""

    def __init__(self, base: str, process: subprocess.Popen, log_path: Path):
        self.base = base
        self.process = process
        self.log_path = log_path

    def alive(self) -> bool:
        return self.process.poll() is None

    def memory_kb(self) -> dict:
        try:
            status = Path(f"/proc/{self.process.pid}/status").read_text()
        except OSError:
            return {}
        wanted = {"VmRSS": "rss_kb", "VmHWM": "peak_rss_kb"}
        found = {}
        for line in status.splitlines():
            key = line.split(":", 1)[0]
            if key in wanted:
                found[wanted[key]] = int(line.split()[1])
        return found


@contextmanager
def server(binary: Path, config: Path, log_path: Path, startup_timeout: float = 60.0):
    port = free_port()
    environment = dict(os.environ, LONGSHORT_PORT=str(port))
    with log_path.open("wb") as log:
        process = subprocess.Popen([str(binary), str(config)], stdout=log,
                                   stderr=subprocess.STDOUT, env=environment)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + startup_timeout
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Test server exited:\n{log_path.read_text()}")
                try:
                    request(base, "/api/health", timeout=5)
                    break
                except (URLError, ConnectionError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Test server did not start within "
                                           f"{startup_timeout:g}s: {log_path}")
                    time.sleep(0.1)
            yield TestServer(base, process, log_path)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def write_fixture(directory: Path, scenario: Scenario) -> Path:
    """Synthetic wide parquet panel plus the YAML config the server loads."""
    directory.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2026-01-05", periods=scenario.T, freq="D")
    symbols = scenario.symbols
    pd.DataFrame(scenario.close, index=dates, columns=symbols).to_parquet(directory / "close.parquet")
    pd.DataFrame(scenario.signal, index=dates, columns=symbols).to_parquet(directory / "signal.parquet")
    membership = np.ones((scenario.T, len(symbols)), dtype=bool) if scenario.mask is None else scenario.mask
    pd.DataFrame(membership, index=dates, columns=symbols).to_parquet(directory / "universe.parquet")

    config = {
        "datasets": [{"name": "synthetic", "format": "wide", "path": ".",
                      "fields": {"close": "close.parquet", "raw_signal": "signal.parquet"}}],
        "universe": {"path": "universe.parquet", "symbols": list(symbols)},
        "backtest": {"start_date": str(dates[0].date()), "end_date": str(dates[-1].date()),
                     "initial_capital": 1000000, "transaction_fee": scenario.config.fee_rate * 10000.0,
                     "bars_per_year": 252, "warmup_bars": 0, "mode": scenario.config.mode,
                     # Contract defaults stay in the YAML so the request options in
                     # parse_run_options are what the comparison actually exercises.
                     "missing_close_policy": "carry",
                     "default_delisting_return": 0.0,
                     "stale_quote_settle_bars": 0},
    }
    if scenario.has_events:
        bars = np.asarray(scenario.settlement_bar)
        returns = (np.full(len(symbols), NAN) if scenario.settlement_return is None
                   else np.asarray(scenario.settlement_return, dtype=np.float64))
        indices = (np.full(len(symbols), -1) if scenario.settlement_successor is None
                   else np.asarray(scenario.settlement_successor, dtype=np.int64))
        ratios = (np.full(len(symbols), NAN) if scenario.settlement_successor_ratio is None
                  else np.asarray(scenario.settlement_successor_ratio, dtype=np.float64))
        cash = (np.full(len(symbols), NAN) if scenario.settlement_cash is None
                else np.asarray(scenario.settlement_cash, dtype=np.float64))
        names = scenario.successor_names or [
            symbols[int(index)] if int(index) >= 0 else None for index in indices]
        stock = (np.isfinite(ratios) | np.array([name is not None for name in names])
                 if scenario.has_stock_leg is None
                 else np.asarray(scenario.has_stock_leg, dtype=bool))
        cash_leg = (np.isfinite(cash) if scenario.has_cash_leg is None
                    else np.asarray(scenario.has_cash_leg, dtype=bool))
        quotes = np.asarray(scenario.close, dtype=np.float64)
        valid = np.isfinite(quotes) & (quotes > 0.0)
        rows = []
        for index, symbol in enumerate(symbols):
            bar = int(bars[index])
            explicit = float(returns[index])
            if bar < 0 and not np.isfinite(explicit):
                continue
            # The loader settles on the first bar after the end date; a null end
            # date carries an explicit return with no event bar of its own.
            end = pd.NaT if bar < 0 else (dates[bar - 1] if bar > 0
                                          else dates[0] - pd.Timedelta(days=5))
            # Guard anchors: the engine settles on the first bar from the event bar
            # onward without a quote, so the successor anchor is that bar's close and
            # the target anchor is its last quote before it (Phase 2e E-1/E-2).
            settle, target_mark, target_date = None, NAN, pd.NaT
            successor_price, successor_date = NAN, pd.NaT
            if bar >= 0:
                settle = bar
                while settle < scenario.T and valid[settle, index]:
                    settle += 1
            if settle is not None and settle < scenario.T:
                earlier = np.flatnonzero(valid[:settle, index])
                if earlier.size:
                    target_mark = float(quotes[earlier[-1], index])
                    target_date = dates[earlier[-1]]
                successor_index = int(indices[index])
                if successor_index >= 0 and valid[settle, successor_index]:
                    successor_price = float(quotes[settle, successor_index])
                    successor_date = dates[settle]
            rows.append({"tradingItemId": symbol, "securityEndDate": end,
                         "settlementReturn": explicit,
                         "hasStockLeg": bool(stock[index]), "hasCashLeg": bool(cash_leg[index]),
                         "successorTradingItemId": names[index],
                         "successorRatio": float(ratios[index]),
                         "cashPerShare": float(cash[index]),
                         "targetCacheMark": target_mark, "targetCacheMarkDate": target_date,
                         "successorCacheClose": successor_price,
                         "successorCacheCloseDate": successor_date})
        frame = pd.DataFrame(rows, columns=[
            "tradingItemId", "securityEndDate", "settlementReturn", "hasStockLeg", "hasCashLeg",
            "successorTradingItemId", "successorRatio", "cashPerShare",
            "targetCacheMark", "targetCacheMarkDate", "successorCacheClose", "successorCacheCloseDate"])
        for column in ("securityEndDate", "targetCacheMarkDate", "successorCacheCloseDate"):
            frame[column] = pd.to_datetime(frame[column])
        frame["successorTradingItemId"] = frame["successorTradingItemId"].astype("string")
        frame.to_parquet(directory / "events.parquet", index=False)
        config["events"] = {"path": "events.parquet", "id_col": "tradingItemId",
                            "end_date_col": "securityEndDate", "return_col": "settlementReturn",
                            "successor_col": "successorTradingItemId",
                            "successor_ratio_col": "successorRatio",
                            "cash_col": "cashPerShare",
                            "stock_leg_col": "hasStockLeg", "cash_leg_col": "hasCashLeg",
                            "successor_price_col": "successorCacheClose",
                            "successor_price_date_col": "successorCacheCloseDate",
                            "target_price_col": "targetCacheMark",
                            "target_price_date_col": "targetCacheMarkDate"}
    path = directory / "config.yaml"
    path.write_text(json.dumps(config, indent=2))
    return path


def compare_settlement_log(scenario: Scenario, result: LedgerResult, response: dict,
                          dates: pd.DatetimeIndex) -> dict:
    """Match the engine's settlementLog record-for-record against the ledger's.

    Records are paired on symbol and timestamp; bar, exposure, return and source
    must then agree. A missing log on a case that asked for one is a failure.
    """
    problems, worst = [], 0.0
    if "settlementLog" not in response:
        return {"problems": ["settlementLog missing from the response"], "max_abs_difference": 0.0}
    expected = {}
    for record in result.settlement_log:
        expected[(scenario.symbols[record["asset"]], str(dates[record["bar"]].date()))] = record
    reported = {}
    for record in response["settlementLog"]:
        key = (str(record.get("symbol")), str(record.get("time"))[:10])
        if key in reported:
            problems.append(f"settlementLog repeats {key}")
        reported[key] = record
    for key in sorted(set(expected) - set(reported)):
        problems.append(f"settlementLog misses {key[0]} at {key[1]}")
    for key in sorted(set(reported) - set(expected)):
        problems.append(f"settlementLog has an extra record for {key[0]} at {key[1]}")
    for key in sorted(set(expected) & set(reported)):
        ledger, server = expected[key], reported[key]
        for field_name in SETTLEMENT_LOG_FIELDS:
            if field_name not in server:
                problems.append(f"settlementLog[{key[0]}].{field_name} missing")
                continue
            if field_name == "source":
                if server[field_name] != ledger["source"]:
                    problems.append(f"settlementLog[{key[0]}].source "
                                    f"{server[field_name]} != {ledger['source']}")
                continue
            difference = abs(float(server[field_name]) - float(ledger[field_name]))
            worst = max(worst, difference)
    unheld = response.get("settlementLogUnheld")
    ledger_unheld = int(result.diagnostic_totals()["settlement_unheld_count"])
    if unheld is None:
        problems.append("settlementLogUnheld missing from the response")
    elif int(unheld) != ledger_unheld:
        problems.append(f"settlementLogUnheld {unheld} != ledger {ledger_unheld}")
    return {"problems": problems, "max_abs_difference": worst,
            "records": len(expected), "ledgerUnheld": ledger_unheld}


def reject_verdict(ledger_error: str, status: Optional[int], message: str) -> tuple:
    """Grade a case the ledger refuses: only a matching HTTP 400 counts as agreement.

    A 5xx, a 200, or a 400 about something else means the two implementations
    stopped for different reasons, which is a failure, not a match (review R3).
    """
    if status != 400:
        return "FAIL", f"ledger rejects but the server answered {status}: {message.strip()[:200]}"
    match = LEDGER_LOCATION.search(ledger_error or "")
    if match:
        bar, asset = match.groups()
        absent = [token for token in (f"bar {bar}", f"asset {asset}") if token not in message]
        if absent:
            return "FAIL", (f"HTTP 400 but the message does not name {' and '.join(absent)}: "
                            f"{message.strip()[:200]}")
    return "PASS", f"both reject (400): {message.strip()[:200]}"


def compare_timeseries(scenario: Scenario, response: dict, strict: bool = True) -> dict:
    """Compare the server's timeseries and missingData totals with the ledger.

    strict=True (a server that advertises the contract) requires every series in
    REQUIRED_SERIES and every key in MISSING_DATA_KEYS: a missing key or a length
    mismatch is a failure, never a silently skipped check.
    """
    result = scenario.run()
    expected = result.http_timeseries()
    timeseries = response.get("timeseries") or {}
    totals = result.diagnostic_totals()
    differences, problems = {}, []

    if "cumPnl" not in timeseries:
        return {"status": "FAIL", "problems": ["timeseries.cumPnl missing"],
                "ledger_totals": totals}
    cum = np.asarray(timeseries["cumPnl"], dtype=np.float64)
    if len(cum) != len(expected["cumPnl"]):
        return {"status": "FAIL",
                "problems": [f"timeseries length {len(cum)} != ledger {len(expected['cumPnl'])}"],
                "ledger_totals": totals}
    ret = np.diff(np.concatenate([[0.0], cum]))
    differences["ret"] = float(np.max(np.abs(ret - expected["ret"]))) if len(ret) else 0.0
    differences["cumPnl"] = float(np.max(np.abs(cum - expected["cumPnl"]))) if len(cum) else 0.0

    for key in REQUIRED_SERIES:
        reference = np.asarray(expected[key], dtype=np.float64)
        if key not in timeseries:
            problems.append(f"timeseries.{key} missing")
            continue
        values = np.asarray(timeseries[key], dtype=np.float64)
        if len(values) != len(reference):
            problems.append(f"timeseries.{key} length {len(values)} != ledger {len(reference)}")
            continue
        differences[key] = float(np.max(np.abs(values - reference))) if len(values) else 0.0

    diagnostics = {}
    reported = response.get("missingData")
    if not isinstance(reported, dict):
        problems.append("missingData object missing")
    else:
        for key, name in MISSING_DATA_KEYS.items():
            if key not in reported:
                problems.append(f"missingData.{key} missing")
                continue
            diagnostics[key] = {"server": reported[key], "ledger": totals[name],
                                "diff": abs(float(reported[key]) - float(totals[name]))}
        for key in sorted(set(reported) - set(MISSING_DATA_KEYS)):
            problems.append(f"missingData.{key} is not in the expected key set")

    worst = max([abs(value) for value in differences.values()]
                + [entry["diff"] for entry in diagnostics.values()] + [0.0])
    failed = bool(problems) or worst > TOLERANCE
    status = "FAIL" if failed else "PASS"
    if not strict and problems and worst <= TOLERANCE:
        # An old binary cannot answer the new keys; the legacy series still must match.
        status = "SKIP"
    return {"status": status, "max_abs_difference": worst, "timeseries": differences,
            "problems": problems, "missing_data": diagnostics,
            "ledger_totals": totals, "bars": int(len(cum))}


def http_case(base: str, contract_ready: bool, scenario: Scenario, dates: pd.DatetimeIndex,
              policy: str, default_return: float) -> dict:
    """One /api/simulate request, compared with the ledger for the same options."""
    name = f"{scenario.name}__{policy}__R{default_return:g}"
    case_scenario = Scenario(**{**scenario.__dict__})
    case_scenario.config = LedgerConfig(
        mode=scenario.config.mode, fee_rate=scenario.config.fee_rate, policy=policy,
        default_delisting_return=default_return,
        stale_quote_settle_bars=scenario.config.stale_quote_settle_bars,
        sim_start_index=scenario.config.sim_start_index)
    payload = {"xml": '<data expr="raw_signal"/>',
               "simStart": str(dates[case_scenario.config.sim_start_index].date()),
               "isStart": str(dates[0].date()), "osStart": str(dates[-1].date()),
               "mode": case_scenario.config.mode, "missingClose": policy,
               "defaultDelistingReturn": default_return,
               "staleQuoteSettleBars": case_scenario.config.stale_quote_settle_bars}
    wants_log = scenario.name in SETTLEMENT_LOG_SCENARIOS
    if wants_log:
        payload["settlementLog"] = True
    needs_contract = scenario.has_missing or scenario.has_events
    entry = {"case": name, "scenario": scenario.name, "policy": policy,
             "defaultDelistingReturn": default_return, "mode": case_scenario.config.mode,
             "needs_contract": needs_contract, "server_contract": contract_ready}

    # The ledger's own verdict for this option set, independent of the server.
    ledger_error = None
    try:
        case_scenario.run()
    except LedgerError as error:
        ledger_error = str(error)
    entry["ledger_raises"] = ledger_error

    if needs_contract and not contract_ready:
        entry.update(status="SKIP", reason="server predates the missing-data response contract")
        return entry
    try:
        response = request(base, "/api/simulate", payload)
    except HTTPError as error:
        body = error.read().decode(errors="replace")
        try:
            message = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            message = body
        if ledger_error is not None:
            status, reason = reject_verdict(ledger_error, error.code, message)
            entry.update(status=status, reason=reason, http_status=error.code,
                         comparison="both_reject")
        else:
            entry.update(status="FAIL", http_status=error.code,
                         reason=f"server rejected an accepted case ({error.code}): {message}")
        return entry
    except Exception as error:
        entry.update(status="ERROR", reason=f"{type(error).__name__}: {error}")
        return entry
    if ledger_error is not None:
        entry.update(status="FAIL", reason="the ledger rejects this case but the server accepted it")
        return entry
    entry.update(compare_timeseries(case_scenario, response, strict=contract_ready))
    entry["comparison"] = "numeric"
    if wants_log and contract_ready:
        log = compare_settlement_log(case_scenario, case_scenario.run(), response, dates)
        entry["settlement_log"] = log
        entry["problems"] = list(entry.get("problems") or []) + log["problems"]
        entry["max_abs_difference"] = max(entry.get("max_abs_difference", 0.0),
                                          log["max_abs_difference"])
        if entry["problems"] or entry["max_abs_difference"] > TOLERANCE:
            entry["status"] = "FAIL"
    if entry.get("problems"):
        entry["reason"] = "; ".join(entry["problems"][:4])
    return entry


def option_range_cases(base: str, scenario: Scenario, dates: pd.DatetimeIndex) -> list:
    """defaultDelistingReturn outside the settleable range must be rejected (review F-03)."""
    cases = []
    for value in REJECTED_DEFAULT_RETURNS:
        payload = {"xml": '<data expr="raw_signal"/>',
                   "simStart": str(dates[scenario.config.sim_start_index].date()),
                   "isStart": str(dates[0].date()), "osStart": str(dates[-1].date()),
                   "mode": scenario.config.mode, "missingClose": "carry",
                   "defaultDelistingReturn": value}
        entry = {"case": f"option_range__defaultDelistingReturn_{value:g}",
                 "scenario": scenario.name, "defaultDelistingReturn": value,
                 "expectation": "HTTP 400", "comparison": "range_reject"}
        try:
            request(base, "/api/simulate", payload)
        except HTTPError as error:
            body = error.read().decode(errors="replace")
            verdict = "PASS" if error.code == 400 else "FAIL"
            entry.update(status=verdict, http_status=error.code, reason=body.strip()[:200])
        except Exception as error:
            entry.update(status="ERROR", reason=f"{type(error).__name__}: {error}")
        else:
            entry.update(status="FAIL",
                         reason=f"server accepted defaultDelistingReturn={value:g} (200)")
        cases.append(entry)
    return cases


def http_check(binary: Path, scenarios: list, root: Path) -> dict:
    """One isolated server per synthetic panel; every option set reuses it."""
    cases = []
    checked_option_range = False
    for scenario in scenarios:
        directory = root / scenario.name
        config_path = write_fixture(directory, scenario)
        dates = pd.date_range("2026-01-05", periods=scenario.T, freq="D")
        try:
            with server(binary, config_path, directory / "server.log") as running:
                base = running.base
                configuration = request(base, "/api/config")
                contract_ready = "missingClosePolicy" in configuration
                for policy in ("carry", "error"):
                    for default_return in (0.0, -1.0):
                        cases.append(http_case(base, contract_ready, scenario, dates,
                                               policy, default_return))
                if contract_ready and not checked_option_range:
                    cases += option_range_cases(base, scenario, dates)
                    checked_option_range = True
        except Exception as error:
            cases.append({"case": scenario.name, "scenario": scenario.name, "status": "ERROR",
                          "reason": f"{type(error).__name__}: {error}"})
    counts, kinds = {}, {}
    for case in cases:
        counts[case.get("status", "ERROR")] = counts.get(case.get("status", "ERROR"), 0) + 1
        kind = case.get("comparison", "skipped")
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"cases": cases, "counts": counts, "kinds": kinds}


def self_test() -> dict:
    """Feed the review's counterexamples to the graders and require a failure.

    These are in-memory fake responses, not product output: the point is that the
    harness itself would have caught them (review R3).
    """
    scenario = [item for item in contract_scenarios() if item.name == "successor_ratio_two"][0]
    result = scenario.run()
    expected = result.http_timeseries()
    totals = result.diagnostic_totals()
    good = {"timeseries": {key: list(np.asarray(value)) for key, value in expected.items()
                           if key != "begin" and key != "ret"},
            "missingData": {key: totals[name] for key, name in MISSING_DATA_KEYS.items()}}
    good["timeseries"]["cumPnl"] = list(expected["cumPnl"])
    ledger_error = "Cannot value or execute position: invalid close at bar 1, asset 0"
    log_scenario = [item for item in contract_scenarios()
                    if item.name == "successor_ratio_two"][0]
    log_result = log_scenario.run()
    log_dates = pd.date_range("2026-01-05", periods=log_scenario.T, freq="D")
    entry = log_result.settlement_log[0]
    log_record = {"symbol": log_scenario.symbols[entry["asset"]],
                  "time": str(log_dates[entry["bar"]].date()), "bar": entry["bar"],
                  "exposure": entry["exposure"], "return": entry["return"],
                  "source": entry["source"]}
    log_unheld = int(log_result.diagnostic_totals()["settlement_unheld_count"])

    probes = [
        ("baseline numeric comparison passes",
         lambda: compare_timeseries(scenario, good)["status"], "PASS"),
        ("unrelated HTTP 500 on a rejected case",
         lambda: reject_verdict(ledger_error, 500, "unrelated server implementation bug")[0], "FAIL"),
        ("HTTP 400 about a different position",
         lambda: reject_verdict(ledger_error, 400, "invalid close at bar 7, asset 3")[0], "FAIL"),
        ("HTTP 400 naming the same position",
         lambda: reject_verdict(ledger_error, 400, ledger_error)[0], "PASS"),
        ("empty 200 with no timeseries",
         lambda: compare_timeseries(scenario, {"timeseries": {}, "missingData": good["missingData"]})["status"],
         "FAIL"),
        ("200 with a truncated series",
         lambda: compare_timeseries(scenario, {**good, "timeseries": {
             **good["timeseries"], "turnover": good["timeseries"]["turnover"][:-1]}})["status"], "FAIL"),
        ("200 missing a required missingData key",
         lambda: compare_timeseries(scenario, {**good, "missingData": {
             key: value for key, value in good["missingData"].items() if key != "retirements"}})["status"],
         "FAIL"),
        ("settlementLog absent when requested",
         lambda: compare_settlement_log(log_scenario, log_result, {}, log_dates)["problems"] != [], True),
        ("settlementLog record with the wrong source",
         lambda: any("source" in problem for problem in compare_settlement_log(
             log_scenario, log_result,
             {"settlementLog": [{**log_record, "source": "default"}],
              "settlementLogUnheld": log_unheld}, log_dates)["problems"]), True),
        ("settlementLog record with a shifted exposure",
         lambda: compare_settlement_log(
             log_scenario, log_result,
             {"settlementLog": [{**log_record, "exposure": log_record["exposure"] + 0.3}],
              "settlementLogUnheld": log_unheld}, log_dates)["max_abs_difference"] > TOLERANCE, True),
        ("settlementLog matching the ledger",
         lambda: compare_settlement_log(
             log_scenario, log_result,
             {"settlementLog": [log_record], "settlementLogUnheld": log_unheld},
             log_dates)["problems"] == [], True),
        ("200 with a wrong settlement number",
         lambda: compare_timeseries(scenario, {**good, "missingData": {
             **good["missingData"], "convertedGross": good["missingData"]["convertedGross"] + 0.3}})["status"],
         "FAIL"),
    ]
    rows, failures = [], 0
    for label, probe, wanted in probes:
        try:
            observed = probe()
        except Exception as error:  # a grader that throws is also a failure
            observed = f"{type(error).__name__}: {error}"
        ok = observed == wanted
        failures += 0 if ok else 1
        rows.append({"probe": label, "expected": wanted, "observed": observed,
                     "verdict": "PASS" if ok else "FAIL"})
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: expected {wanted}, got {observed}")
    print(f"self-test: {len(rows)} probes, {failures} failure(s)")
    return {"rows": rows, "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true",
                        help="check the harness's own failure detection against the review's "
                             "counterexamples (fake responses, no server)")
    parser.add_argument("--self-check", action="store_true",
                        help="run the contract's hand-calculated examples against the ledger")
    parser.add_argument("--server", type=Path, help="longshort-server binary for the HTTP comparison")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--keep-fixtures", type=Path,
                        help="write the synthetic fixtures here instead of a temporary directory")
    args = parser.parse_args()
    if not args.self_check and args.server is None and not args.self_test:
        parser.error("choose --self-test, --self-check, --server, or a combination")

    report = {"tolerance": TOLERANCE}
    status = 0
    if args.self_test:
        report["self_test"] = self_test()
        status |= 4 if report["self_test"]["failures"] else 0
    scenarios = hand_scenarios()
    extra = contract_scenarios()
    if args.self_check:
        report["self_check"] = merge_self_checks(
            self_check(scenarios, "계약 표"), self_check(extra, "표 외/리뷰"))
        print_self_check(report["self_check"])
        status |= 1 if report["self_check"]["failures"] else 0

    if args.server is not None:
        binary = args.server.resolve()
        if not binary.exists():
            parser.error(f"server binary not found: {binary}")
        http_scenarios = baseline_scenarios() + scenarios + extra
        if args.keep_fixtures:
            args.keep_fixtures.mkdir(parents=True, exist_ok=True)
            report["http"] = http_check(binary, http_scenarios, args.keep_fixtures)
        else:
            with tempfile.TemporaryDirectory(prefix="longshort_missing_data_") as temporary:
                report["http"] = http_check(binary, http_scenarios, Path(temporary))
        counts = report["http"]["counts"]
        kinds = report["http"]["kinds"]
        print("\nHTTP comparison: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
        # Not every case is a numeric comparison; report the kinds separately so
        # the totals are never read as "N numeric comparisons" (review R3).
        print("  kinds: " + ", ".join(f"{key}={value}" for key, value in sorted(kinds.items()))
              + "  (numeric=수치 대조, both_reject=양쪽 거절, range_reject=범위 거절)")
        for case in report["http"]["cases"]:
            if case.get("status") in ("FAIL", "ERROR"):
                print(f"  {case['status']}  {case['case']}: {case.get('reason', case.get('timeseries'))}")
        status |= 2 if counts.get("FAIL") or counts.get("ERROR") else 0

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=float) + "\n")
        print(f"\nreport: {args.output}")
    return status


if __name__ == "__main__":
    sys.exit(main())
