#!/usr/bin/env python3
"""Independent missing-close / delisting accounting ledger.

Written from the accounting contract (docs/data_contract.md
and the implementation brief), not from the C++ simulator. It reproduces the
fixed-book notional-weight book bar by bar so the engine can be compared against
a second implementation rather than against itself.

Contract summary implemented here (bar t, in this order):
  0. quote state         valid = isfinite(close) and close > 0; missing = NaN;
                         anything else (0, negative, +/-inf) is abnormal data and
                         is an error under BOTH policies - carry covers NaN only
  1. settlement          an event bar whose quote is gone, a still-pending event
                         reaching its first NaN bar, or the carry stale rule.
                         A settled name is valued per share as cash + ratio *
                         P_successor; the share leg moves into the successor and
                         only the cash leg is realised
  2. intent              normalize the raw signal row over the signal-date mask
  3. holding P&L         h_prev * (close / mark - 1) for observable quotes
  4. execution           tradable names move to the (possibly shrunk) target
  5. turnover / fee      market trades only; settlement is fee free
  6. statistics          computed on the post-trade book h
  7. warmup              marks and intents only, no book, no fees, no P&L

Forward filling exists only in the valuation mark; raw close is never altered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

EPS_POSITION = 1e-12
EPS_SIGNAL_SUM = 1e-12


class LedgerError(RuntimeError):
    """Raised for the fail-fast ('error') missing-close policy."""


@dataclass
class LedgerConfig:
    fee_rate: float = 0.0005            # one-way fraction of traded notional
    policy: str = "carry"               # "carry" | "error"
    default_delisting_return: float = 0.0
    stale_quote_settle_bars: int = 0    # 0 = never settle on staleness alone
    sim_start_index: int = 0            # bars before this only warm marks/intents
    # The brief's step 1 does not spell out whether the stale rule may retire an
    # asset that is not held. simulator.h documents it as settling "frozen
    # holdings", and retiring never-listed names would silently delete them from
    # the tradable universe, so the ledger requires a nonzero held weight.
    stale_requires_holding: bool = True
    # simulator.h: "Counts refer to affected nonzero bookkeeping weights".
    count_only_held_settlements: bool = True
    # retirement_count answers "how many securities retired inside this panel",
    # so unlike the book counters it also counts a retirement during warmup: the
    # name is gone from the tradable set for the whole trading period.
    count_warmup_retirements: bool = True
    # post_end_date_quote_bars counts the data conflict itself, not book exposure,
    # so it is independent of whether the name is held. It is still recorded on
    # trading bars only, matching "valued" in the counter's definition.
    count_unheld_post_end_date_bars: bool = True

    def __post_init__(self) -> None:
        if self.policy not in ("carry", "error"):
            raise ValueError("policy must be 'carry' or 'error'")
        if not np.isfinite(self.fee_rate) or self.fee_rate < 0.0:
            raise ValueError("fee_rate must be finite and nonnegative")
        if not np.isfinite(self.default_delisting_return) or self.default_delisting_return < -1.0:
            raise ValueError("default_delisting_return must be finite and at least -1.0")


DIAGNOSTIC_FIELDS = (
    "missing_held_count",
    "blocked_order_count",
    "frozen_gross",
    "blocked_trade_weight",
    "resumed_holding_count",
    "delisting_count",
    "delisting_pnl",
    "settled_gross",          # exposure |h_prev| settled for cash; mixed deals split by value
    "default_delisting_count",
    "default_delisting_gross",
    "missing_entry_count",
    "pending_delisting_entry_count",
    "stale_settlement_count",
    "retirement_count",
    # Second review: end date versus surviving quote.
    "post_end_date_quote_bars",   # asset-bars valued on a quote after the end date
    "pending_settlement_count",   # events still waiting for a quote to disappear
    # Phase 2: successor (merger/rename) events.
    "successor_conversion_count",     # settlements that moved weight to a successor
    "converted_gross",                # weight actually handed over (value basis),
                                      # h_prev*ratio*P_succ/mark_old = the h_succ increment
    "computed_settlement_return_count",  # R derived from ratio * close_succ / mark_old - 1
    "settlement_unheld_count",        # settlements that touched no exposure (log excludes them)
)
_INTEGER_DIAGNOSTICS = {name for name in DIAGNOSTIC_FIELDS
                        if name.endswith("_count") or name.endswith("_bars")}


@dataclass
class LedgerResult:
    config: LedgerConfig
    timestamps: Optional[np.ndarray]
    ret: np.ndarray               # net per-bar return (gross P&L minus fee)
    gross_pnl: np.ndarray
    fee: np.ndarray
    turnover: np.ndarray
    position_count: np.ndarray
    long_weight: np.ndarray
    short_weight: np.ndarray
    max_weight: np.ndarray
    weights: np.ndarray           # T x N post-trade book
    previous_weights: np.ndarray  # T x N book carried into the bar (pre settlement)
    desired: np.ndarray           # T x N execution intent before frozen-budget scaling
    desired_scaled: np.ndarray    # T x N intent actually presented to the market
    trades: np.ndarray            # T x N signed market trades (settlement excluded)
    marks_before: np.ndarray
    marks_after: np.ndarray
    retired: np.ndarray           # T x N post-bar retired flags
    settlement_returns: np.ndarray  # T x N applied R, NaN where no event
    settled_gross_asset: np.ndarray
    conversions: np.ndarray       # T x N weight received from a settled predecessor
    # One record per settlement that actually touched the book, matching the
    # engine's settlementLog contract (trading bars, nonzero exposure only).
    settlement_log: list = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    @property
    def terminal_frozen_gross(self) -> float:
        return float(self.diagnostics["frozen_gross"][-1]) if len(self.ret) else 0.0

    def diagnostic_totals(self) -> dict:
        totals = {}
        for name in DIAGNOSTIC_FIELDS:
            values = self.diagnostics[name]
            totals[name] = int(values.sum()) if name in _INTEGER_DIAGNOSTICS else float(values.sum())
        totals["terminal_frozen_gross"] = self.terminal_frozen_gross
        # A state, not an event: report the last bar's outstanding events.
        totals["pending_settlements"] = (int(self.diagnostics["pending_settlement_count"][-1])
                                         if len(self.ret) else 0)
        return totals

    def http_timeseries(self) -> dict:
        """The server's timeseries contract from the first metric bar: cumulative net and gross P&L, drawdown on gross."""
        begin = self.config.sim_start_index
        begin = min(begin, len(self.ret))
        ret = self.ret[begin:]
        turnover = self.turnover[begin:]
        cum = np.cumsum(ret)
        cum_gross = np.cumsum(ret + turnover * self.config.fee_rate)
        peak = np.maximum.accumulate(np.concatenate([[0.0], cum_gross]))[1:] if len(cum_gross) else cum_gross
        peak = np.maximum(peak, 0.0)
        return {
            "begin": begin,
            "ret": ret,
            "cumPnl": cum,
            "cumPnlGross": cum_gross,
            "turnover": turnover * 100.0,
            "maxWeight": self.max_weight[begin:] * 100.0,
            "nonzeroCount": self.position_count[begin:],
            "drawdown": cum_gross - peak,
            "frozenGross": self.diagnostics["frozen_gross"][begin:],
            "settledGross": self.diagnostics["settled_gross"][begin:],
        }


def _valid_quote(row: np.ndarray) -> np.ndarray:
    return np.isfinite(row) & (row > 0.0)


def _quote_states(row: np.ndarray):
    """valid / missing / abnormal. Only NaN is a missing quote; the rest is bad data."""
    valid = _valid_quote(row)
    missing = np.isnan(row)
    return valid, missing, (~valid) & (~missing)


def normalize_signal_row(signal_row: np.ndarray, valid: np.ndarray, mask_row: Optional[np.ndarray],
                         retired: np.ndarray, policy: str) -> np.ndarray:
    """Gross-one normalization over the signal-date universe.

    carry keeps a finite signal eligible without a quote; error retains the
    original valid-close gate. A NaN signal always means target zero, and a
    retired asset is never an intent.
    """
    eligible = np.isfinite(signal_row) & (~retired)
    if mask_row is not None:
        eligible &= mask_row
    if policy == "error":
        eligible &= valid
    targets = np.zeros(signal_row.shape[0], dtype=np.float64)
    total = float(np.abs(signal_row[eligible].astype(np.float64)).sum())
    if total > EPS_SIGNAL_SUM:
        targets[eligible] = signal_row[eligible].astype(np.float64) / total
    return targets


def run_ledger(close: np.ndarray,
               signal: np.ndarray,
               mask: Optional[np.ndarray] = None,
               settlement_bar: Optional[np.ndarray] = None,
               settlement_return: Optional[np.ndarray] = None,
               settlement_successor: Optional[np.ndarray] = None,
               settlement_successor_ratio: Optional[np.ndarray] = None,
               settlement_cash: Optional[np.ndarray] = None,
               settlement_successor_named: Optional[np.ndarray] = None,
               settlement_has_stock_leg: Optional[np.ndarray] = None,
               settlement_has_cash_leg: Optional[np.ndarray] = None,
               config: Optional[LedgerConfig] = None,
               timestamps: Optional[np.ndarray] = None) -> LedgerResult:
    """Run the independent book over a close/signal panel.

    close:             T x N observed close (NaN or <= 0 means no quote)
    signal:            T x N raw alpha signal (NaN means target zero)
    mask:              T x N signal-date universe membership, None = all members
    settlement_bar:    N int32, first bar strictly after the security end date;
                       -1 no event, 0 already ended before the panel starts
    settlement_return: N float, explicit total R from the last mark; NaN = default
    settlement_successor:       N int, index into symbols of the surviving security;
                                -1 = none or outside the panel
    settlement_successor_ratio: N float, successor shares per old share; NaN = 1.0
    settlement_cash:            N float, cash per old share paid at settlement; NaN = none
    settlement_successor_named: N bool, the events row named a successor even if it
                                could not be resolved into the panel; None = infer
    settlement_has_stock_leg:   N bool, the deal pays shares (structural fact from the
                                events file); None = infer from ratio/successor id
    settlement_has_cash_leg:    N bool, the deal pays cash; None = infer from cash
    """
    cfg = config or LedgerConfig()
    close = np.asarray(close, dtype=np.float32)
    signal = np.asarray(signal, dtype=np.float32)
    if close.shape != signal.shape:
        raise ValueError("close and signal must share the T x N shape")
    T, N = close.shape
    if mask is not None:
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != close.shape:
            raise ValueError("mask must share the T x N shape")
    if settlement_bar is None:
        settlement_bar = np.full(N, -1, dtype=np.int64)
    else:
        settlement_bar = np.asarray(settlement_bar, dtype=np.int64)
    if settlement_return is None:
        settlement_return = np.full(N, np.nan, dtype=np.float64)
    else:
        # DataPanel::settlement_return is std::vector<float>, so the R actually
        # applied is the float32 value, not the parquet double (e.g. -0.4).
        settlement_return = np.asarray(settlement_return, dtype=np.float32).astype(np.float64)
    successor = (np.full(N, -1, dtype=np.int64) if settlement_successor is None
                 else np.asarray(settlement_successor, dtype=np.int64))
    # NaN means the exchange ratio is unknown, not one-for-one: a declared share
    # leg with an unknown ratio cannot be valued (Phase 2e E-2).
    successor_ratio = (np.full(N, np.nan, dtype=np.float64) if settlement_successor_ratio is None
                       else np.asarray(settlement_successor_ratio, dtype=np.float64))
    settlement_cash = (np.full(N, np.nan, dtype=np.float64) if settlement_cash is None
                       else np.asarray(settlement_cash, dtype=np.float32).astype(np.float64))
    # A structural fact about the deal, independent of whether it can be priced.
    stock_leg = (np.isfinite(successor_ratio) | (successor >= 0)
                 if settlement_has_stock_leg is None
                 else np.asarray(settlement_has_stock_leg, dtype=bool))
    cash_leg = (np.isfinite(settlement_cash) if settlement_has_cash_leg is None
                else np.asarray(settlement_has_cash_leg, dtype=bool))
    named_successor = (successor >= 0 if settlement_successor_named is None
                       else np.asarray(settlement_successor_named, dtype=bool))
    if settlement_has_stock_leg is None:
        stock_leg = stock_leg | named_successor
    for vector in (settlement_bar, settlement_return, successor, successor_ratio,
                   settlement_cash, named_successor, stock_leg, cash_leg):
        if vector.shape != (N,):
            raise ValueError("event vectors must be aligned with the N symbols")

    h_prev = np.zeros(N, dtype=np.float64)
    marks = np.full(N, np.nan, dtype=np.float64)
    retired = np.zeros(N, dtype=bool)
    # An event bar reached while the security still quotes: settlement waits.
    pending = np.zeros(N, dtype=bool)
    missing_run = np.zeros(N, dtype=np.int64)

    out = {name: np.zeros(T, dtype=np.float64) for name in
           ("ret", "gross_pnl", "fee", "turnover", "long_weight", "short_weight", "max_weight")}
    position_count = np.zeros(T, dtype=np.int64)
    weights_hist = np.zeros((T, N), dtype=np.float64)
    prev_hist = np.zeros((T, N), dtype=np.float64)
    desired_hist = np.zeros((T, N), dtype=np.float64)
    desired_scaled_hist = np.zeros((T, N), dtype=np.float64)
    trade_hist = np.zeros((T, N), dtype=np.float64)
    marks_before = np.zeros((T, N), dtype=np.float64)
    marks_after = np.zeros((T, N), dtype=np.float64)
    retired_hist = np.zeros((T, N), dtype=bool)
    settlement_r_hist = np.full((T, N), np.nan, dtype=np.float64)
    settled_gross_hist = np.zeros((T, N), dtype=np.float64)
    conversion_hist = np.zeros((T, N), dtype=np.float64)
    settlement_log = []
    diagnostics = {name: np.zeros(T, dtype=np.float64) for name in DIAGNOSTIC_FIELDS}

    for t in range(T):
        row = close[t]
        valid, missing, abnormal = _quote_states(row)
        previous_missing_run = missing_run.copy()
        missing_run = np.where(missing, missing_run + 1, 0)
        trading = t >= cfg.sim_start_index
        marks_before[t] = marks
        prev_hist[t] = h_prev

        gross_pnl = 0.0
        diag = {name: 0.0 for name in DIAGNOSTIC_FIELDS}

        # ---- 1. settlement: administrative, quote free, fee free --------------
        # The end date opens a pending settlement; a security that still quotes
        # keeps trading and only settles on its first bar without a quote.
        pending |= (settlement_bar == t) & (~retired)
        deferred = pending & valid & (~retired)
        if trading and deferred.any():
            countable = deferred if cfg.count_unheld_post_end_date_bars else deferred & (h_prev != 0.0)
            diag["post_end_date_quote_bars"] += float(np.count_nonzero(countable))
        stale = np.zeros(N, dtype=bool)
        if cfg.policy == "carry" and cfg.stale_quote_settle_bars > 0:
            stale = ((~retired) & (settlement_bar < 0)
                     & (missing_run >= cfg.stale_quote_settle_bars))
            if cfg.stale_requires_holding:
                stale &= h_prev != 0.0
        settling = (pending & missing & (~retired)) | stale
        # A successor must survive this bar to receive anything.
        post_retired = retired | settling
        conversion = np.zeros(N, dtype=np.float64)
        for i in np.flatnonzero(settling):
            successor_index = int(successor[i])
            usable_successor = (successor_index >= 0 and successor_index != i
                                and valid[successor_index]
                                and not post_retired[successor_index])
            explicit = settlement_return[i]
            cash = settlement_cash[i]
            old_mark = marks[i]
            mark_usable = np.isfinite(old_mark) and old_mark > 0.0
            # Per-share consideration: cash plus whatever the exchange ratio buys.
            # The engine values the deal itself whenever it can; an explicit total
            # return is only the fallback, and the config default the last resort.
            # Phase 2e E-2: every leg the deal declares must be priceable. Valuing
            # a share deal from its cash leg alone would invent a large loss (real
            # case: ANSS $199.91 + 0.3399 SNPS on a 374.30 mark is +6.5%, but cash
            # alone reads -46.6%), so cash-only valuation needs hasStockLeg=false.
            stock_ok = (not stock_leg[i]) or (usable_successor and np.isfinite(successor_ratio[i]))
            cash_ok = (not cash_leg[i]) or np.isfinite(cash)
            computable = (mark_usable and (stock_leg[i] or cash_leg[i]) and stock_ok and cash_ok)
            exposure = abs(h_prev[i])
            if computable:
                stock_value = (successor_ratio[i] * float(row[successor_index])
                               if stock_leg[i] else 0.0)
                cash_value = float(cash) if cash_leg[i] else 0.0
                per_share_value = cash_value + stock_value
                r, source = per_share_value / old_mark - 1.0, "computed"
                converted_weight = h_prev[i] * stock_value / old_mark
                # Lead ruling: settled_gross stays on the Phase 1 exposure basis;
                # a mixed deal splits that exposure by the value of each leg.
                cash_share = cash_value / per_share_value if per_share_value > 0.0 else 1.0
                settled_exposure = exposure * cash_share
            else:
                # Nothing to value: the whole exposure settles in cash.
                r, source = ((float(explicit), "explicit") if np.isfinite(explicit)
                             else (cfg.default_delisting_return, "default"))
                converted_weight, settled_exposure = 0.0, exposure
            held = h_prev[i] != 0.0
            settlement_r_hist[t, i] = r
            if trading or cfg.count_warmup_retirements:
                # Every retirement, including the weightless ones delisting_count skips.
                diag["retirement_count"] += 1
            converted = computable and stock_leg[i] and usable_successor and held
            if trading and not held:
                diag["settlement_unheld_count"] += 1
            if trading and held:
                stock_share = 0.0 if exposure == 0.0 else 1.0 - (settled_exposure / exposure)
                settlement_log.append({
                    "bar": t, "asset": i, "exposure": h_prev[i], "return": r,
                    "source": source, "cash_share": 1.0 - stock_share,
                    "stock_share": stock_share,
                    "successor": (successor_index if usable_successor and computable
                                  and stock_leg[i] else -1),
                    "transferred": converted_weight if converted else 0.0})
            if trading:
                gross_pnl += h_prev[i] * r
                # Exposure settled for cash: all of it, or the cash leg's share.
                settled_gross_hist[t, i] = settled_exposure
                diag["settled_gross"] += settled_exposure
                counted = held or not cfg.count_only_held_settlements
                if counted:
                    diag["delisting_count"] += 1
                    if source == "default":
                        diag["default_delisting_count"] += 1
                        diag["default_delisting_gross"] += exposure
                    elif source == "computed":
                        diag["computed_settlement_return_count"] += 1
                    if stale[i]:
                        diag["stale_settlement_count"] += 1
                diag["delisting_pnl"] += h_prev[i] * r
                if converted:
                    # Not a market fill: the share leg moves across, fee free.
                    conversion[successor_index] += converted_weight
                    conversion_hist[t, successor_index] += converted_weight
                    diag["successor_conversion_count"] += 1
                    diag["converted_gross"] += abs(converted_weight)
            # The settled mark is the settlement value; the book is gone for good.
            if np.isfinite(marks[i]):
                marks[i] = marks[i] * (1.0 + r)
            h_prev[i] = 0.0
            retired[i] = True
            pending[i] = False

        diag["pending_settlement_count"] = float(np.count_nonzero(pending & (~retired)))

        # ---- 2. intent over the signal-date universe --------------------------
        mask_row = None if mask is None else mask[t]
        targets = normalize_signal_row(signal[t], valid, mask_row, retired, cfg.policy)
        execution = targets
        desired_hist[t] = execution

        if not trading:
            # Warmup builds marks and the first executable intent only.
            observed = valid & np.isfinite(row)
            marks[observed] = row[observed].astype(np.float64)
            marks_after[t] = marks
            retired_hist[t] = retired
            for name in DIAGNOSTIC_FIELDS:
                diagnostics[name][t] = diag[name]
            continue

        engaged = (~retired) & ((h_prev != 0.0) | (execution != 0.0))
        bad_data = engaged & abnormal
        if bad_data.any():
            i = int(np.flatnonzero(bad_data)[0])
            raise LedgerError(
                f"Cannot value or execute position: abnormal close {float(row[i])!r} "
                f"at bar {t}, asset {i}")
        if cfg.policy == "error":
            offenders = engaged & missing
            if offenders.any():
                i = int(np.flatnonzero(offenders)[0])
                raise LedgerError(
                    f"Cannot value or execute position: invalid close at bar {t}, asset {i}")

        # ---- 3. holding P&L against the carried valuation mark ----------------
        live = ~retired
        observable = live & valid
        for i in np.flatnonzero(observable):
            mark = marks[i]
            price = float(row[i])
            if np.isfinite(mark):
                if h_prev[i] != 0.0:
                    gross_pnl += h_prev[i] * (price / mark - 1.0)
                    if previous_missing_run[i] > 0:
                        diag["resumed_holding_count"] += 1
            elif h_prev[i] != 0.0:
                raise LedgerError(f"Held asset {i} has no valuation mark at bar {t}")
            marks[i] = price
        frozen = live & missing
        for i in np.flatnonzero(frozen):
            if h_prev[i] != 0.0:
                diag["missing_held_count"] += 1
                diag["frozen_gross"] += abs(h_prev[i])

        # ---- 3b. successor conversion, after valuation ------------------------
        # marks[successor] is already this bar's close, so the received weight
        # carries no same-bar return and no market trade.
        if conversion.any():
            h_prev = h_prev + conversion

        # ---- 4. execution: only observable quotes trade -----------------------
        frozen_budget = float(np.abs(h_prev[frozen]).sum())
        tradable_gross = float(np.abs(execution[observable]).sum())
        room = max(0.0, 1.0 - frozen_budget)
        scale = 1.0
        # F == 0 must take the original path exactly (contract section 2.3), so a
        # target sum that floats a hair above 1 cannot shrink a clean book.
        if frozen_budget > 0.0 and tradable_gross > room:
            # Shrink only; leftover budget is never redistributed.
            scale = room / tradable_gross if tradable_gross > 0.0 else 0.0
        scaled = np.where(observable, execution * scale, 0.0)
        desired_scaled_hist[t] = scaled

        h = np.zeros(N, dtype=np.float64)
        h[observable] = scaled[observable]
        h[frozen] = h_prev[frozen]
        for i in np.flatnonzero(frozen):
            if execution[i] != h_prev[i]:
                diag["blocked_order_count"] += 1
                diag["blocked_trade_weight"] += abs(execution[i] - h_prev[i])
            if h_prev[i] == 0.0 and execution[i] != 0.0:
                diag["missing_entry_count"] += 1
        cancelled = retired & (execution != 0.0)
        diag["pending_delisting_entry_count"] += float(np.count_nonzero(cancelled))

        # ---- 5. market turnover and fee --------------------------------------
        trades = np.where(observable, h - h_prev, 0.0)
        trade_hist[t] = trades
        turnover = float(np.abs(trades).sum())
        fee = turnover * cfg.fee_rate

        # ---- 6. statistics on the post-trade book ----------------------------
        out["gross_pnl"][t] = gross_pnl
        out["fee"][t] = fee
        out["turnover"][t] = turnover
        out["ret"][t] = gross_pnl - fee
        out["max_weight"][t] = float(np.abs(h).max()) if N else 0.0
        out["long_weight"][t] = float(h[h > 0.0].sum())
        out["short_weight"][t] = float(-h[h < 0.0].sum())
        position_count[t] = int(np.count_nonzero(np.abs(h) > EPS_POSITION))
        weights_hist[t] = h
        marks_after[t] = marks
        retired_hist[t] = retired
        for name in DIAGNOSTIC_FIELDS:
            diagnostics[name][t] = diag[name]

        h_prev = h

    return LedgerResult(
        config=cfg, timestamps=timestamps,
        ret=out["ret"], gross_pnl=out["gross_pnl"], fee=out["fee"], turnover=out["turnover"],
        position_count=position_count, long_weight=out["long_weight"],
        short_weight=out["short_weight"], max_weight=out["max_weight"],
        weights=weights_hist, previous_weights=prev_hist, desired=desired_hist,
        desired_scaled=desired_scaled_hist, trades=trade_hist,
        marks_before=marks_before, marks_after=marks_after, retired=retired_hist,
        settlement_returns=settlement_r_hist, settled_gross_asset=settled_gross_hist,
        conversions=conversion_hist, settlement_log=settlement_log,
        diagnostics=diagnostics)
