#!/usr/bin/env python3
"""Create deterministic synthetic base data for the C++ HTTP contract tests.

The layout mirrors a production price cache so the generated config can use the
`base:` block: <output>/BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
plus <output>/groups/{sector,industry}.parquet. Every file is a wide panel whose
index column is `__index_level_0__` (timestamp[ns]) and whose remaining columns are
the symbols, exactly like the real cache. Group codes are small integers stored as
floats; NaN would mean unclassified.

<output>/engine/ca_ledger_cache.parquet is an eight-row corporate-action ledger in the
loader's export layout (one cash dividend, one spin-off into a panel symbol, one
spin-off into a symbol outside the panel, one warrant into the never-priced W with
an expiry, two of the export's marker rows: a cash row without an amount and a
spin-off without a child, status unresolved with `markerReason`, one spin-off into
a symbol outside the panel that carries a vendor-implied value per share, booked as
P&L without a position, and one quote-priced spin-off flagged as an estimate with
`estimateReason` ratio), wired in through
`backtest.corporate_actions` so the HTTP contract exercises that path too, including
the unconfirmed-P&L report. The ex-dates sit years after the panel start, away from
the bars the contract tests pin.

<output>/costs/borrow_rates.csv is a two-row borrow rate table (symbol, from, to, rate)
wired in through `backtest.costs.borrow` with a made-up default rate; the contract
alphas are long-only, so it changes no number and only exercises loading/reporting.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

SYMBOLS = ["A", "B", "C", "D"]
# W is a listed-but-never-priced warrant (all-NaN close): the child of the rights row
# below, so the pending -> expired path runs through the HTTP contract.
WARRANT = "W"
# Static classification, one industry nested inside each sector (plan 1.1).
SECTOR_CODES = {"A": 1, "B": 1, "C": 2, "D": 2, "W": 2}
INDUSTRY_CODES = {"A": 1, "B": 2, "C": 3, "D": 3, "W": 3}


def write_wide(path, index, columns):
    """One wide panel: timestamp index column first, then float32 symbol columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {"__index_level_0__": pa.array(index, type=pa.timestamp("ns"))}
    for symbol, values in columns.items():
        arrays[symbol] = pa.array(np.asarray(values, dtype=np.float32), type=pa.float32())
    pq.write_table(pa.table(arrays), path, row_group_size=512)


LEDGER_ROWS = [
    # (parent, ex-date, event type, leg type, cash, child, child ratio, expiry, subscription, exercise ratio, marker reason, value per share, estimate reason)
    ("A", "2022-06-15", "cash_dividend", "cash", 0.5, None, None, None, None, None, None, None, None),
    ("C", "2023-03-10", "spin_off", "stock", None, "D", 0.1, None, None, None, None, None, None),
    ("B", "2024-02-20", "spin_off", "stock", None, "E", 0.2, None, None, None, None, None, None),   # E is not in the panel
    ("D", "2024-09-10", "warrant", "rights", None, WARRANT, 0.1, "2024-10-15", 100.0, 1.0, None, None, None),
    # Marker rows (loader export, status unresolved, values NaN): visible, never booked.
    ("A", "2023-09-12", "special_dividend", "cash", None, None, None, None, None, None, "no_cash_amount", None, None),
    ("C", "2025-02-14", "spin_off", "stock", None, None, None, None, None, None, "no_child", None, None),
    # A child outside the panel with a vendor-implied value per parent share: booked as P&L, no position (estimate).
    ("B", "2025-06-12", "spin_off", "stock", None, "F", None, None, None, None, None, 3.0, "value"),
    # A quote-priced spin-off whose ratio the ledger estimated: booked from the quote, reported unconfirmed (child_close:ratio).
    ("D", "2025-08-15", "spin_off", "stock", None, "A", 0.05, None, None, None, None, None, "ratio"),
]


def write_ledger(path, days, close):
    """Ledger rows anchored to the float32 closes the panel files hold."""
    path.parent.mkdir(parents=True, exist_ok=True)
    day_index = {str(d): i for i, d in enumerate(days)}
    price = {s: np.asarray(v, dtype=np.float32) for s, v in close.items()}
    columns = {name: [] for name in (
        "eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShareCache",
        "childTradingItemId", "childRatioCache", "childCloseAtExCache", "parentCloseAtExCache",
        "parentCloseBeforeCache", "parentFactorCache", "payDate", "isEstimate", "status",
        "childValueSource", "expiryDate", "subscriptionPriceCache", "exerciseRatioCache", "markerReason",
        "childValuePerShareCache", "estimateReason")}
    for seq, (parent, ex_date, kind, leg, cash, child, ratio, expiry, strike, per_right, marker, value, reason) in enumerate(LEDGER_ROWS):
        t = day_index[ex_date]
        child_close = float(price[child][t]) if child in price and np.isfinite(price[child][t]) else None
        columns["eventId"].append(f"{parent}_{ex_date}_{kind}_{seq}")
        columns["tradingItemId"].append(parent)
        columns["exDate"].append(np.datetime64(ex_date, "ns"))
        columns["eventType"].append(kind)
        columns["legType"].append(leg)
        columns["cashPerShareCache"].append(cash)
        columns["childTradingItemId"].append(child)
        columns["childRatioCache"].append(ratio)
        columns["childCloseAtExCache"].append(child_close)
        columns["parentCloseAtExCache"].append(float(price[parent][t]))
        columns["parentCloseBeforeCache"].append(float(price[parent][t - 1]))
        columns["parentFactorCache"].append(1.0)
        columns["payDate"].append(np.datetime64(ex_date, "ns"))
        flagged = reason is not None
        columns["isEstimate"].append(flagged)
        columns["status"].append("unresolved" if marker else "estimated" if flagged else "ok")
        columns["childValueSource"].append("vendor_implied" if value is not None else "child_close_ex" if child_close is not None else "unknown")
        columns["expiryDate"].append(np.datetime64(expiry, "ns") if expiry else None)
        columns["subscriptionPriceCache"].append(strike)
        columns["exerciseRatioCache"].append(per_right)
        columns["markerReason"].append(marker)
        columns["childValuePerShareCache"].append(value)
        columns["estimateReason"].append(reason)
    types = {"exDate": pa.timestamp("ns"), "payDate": pa.timestamp("ns"), "isEstimate": pa.bool_(),
             "cashPerShareCache": pa.float64(), "childRatioCache": pa.float64(),
             "childCloseAtExCache": pa.float64(), "parentCloseAtExCache": pa.float64(),
             "parentCloseBeforeCache": pa.float64(), "parentFactorCache": pa.float64(),
             "expiryDate": pa.timestamp("ns"), "subscriptionPriceCache": pa.float64(),
             "exerciseRatioCache": pa.float64(), "childValuePerShareCache": pa.float64()}
    table = pa.table({name: pa.array(values, type=types.get(name, pa.string()))
                      for name, values in columns.items()})
    pq.write_table(table, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    days = np.arange(np.datetime64("2020-01-01"), np.datetime64("2026-01-01"))
    index = days.astype("datetime64[ns]")
    t = np.arange(len(days))
    # Same per-symbol series as the original long fixture, so existing expectations hold.
    close = {s: 100 + 15*n + 0.01*t + 2*np.sin(0.09*t + 0.4*n) for n, s in enumerate(SYMBOLS)}
    # The warrant never prints: NaN everywhere, in every price file.
    unpriced = np.full(len(days), np.nan)
    fields = {
        "close": close,
        "open": {s: v*0.995 for s, v in close.items()},
        "high": {s: v*1.01 for s, v in close.items()},
        "low": {s: v*0.99 for s, v in close.items()},
        "volume": {s: 1000 + 10*n + t % 30 for n, s in enumerate(SYMBOLS)},
        "VWAP": {s: v*1.0005 for s, v in close.items()},
        "marketCap_USD": {s: v*1e6 for s, v in close.items()},
    }
    for name, columns in fields.items():
        write_wide(output / "BaseData" / f"{name}.parquet", index, {**columns, WARRANT: unpriced})
    for name, codes in (("sector", SECTOR_CODES), ("industry", INDUSTRY_CODES)):
        write_wide(output / "groups" / f"{name}.parquet", index,
                   {s: np.full(len(days), float(codes[s])) for s in SYMBOLS + [WARRANT]})

    ledger = output / "engine" / "ca_ledger_cache.parquet"
    write_ledger(ledger, days, {**close, WARRANT: unpriced})
    rates = output / "costs" / "borrow_rates.csv"
    rates.parent.mkdir(parents=True, exist_ok=True)
    rates.write_text("symbol,from,to,rate\nB,2021-01-01,2022-12-31,0.02\nC,2023-01-01,,0.015\n")

    config = {"base": {"cache": str(output), "groups": str(output / "groups")},
              "universe": ["all"], "backtest": {"start_date": "2020-01-01", "end_date": "2025-12-31",
              "initial_capital": 1000000, "transaction_fee": 5,
              "corporate_actions": {"path": str(ledger)},
              "costs": {"borrow": {"enabled": True, "default_rate": 0.01, "path": str(rates),
                                   "day_count": 360}, "report_unapplied": True}}}
    (output / "config.yaml").write_text(json.dumps(config, indent=2))
    print(output / "config.yaml")


if __name__ == "__main__":
    main()
