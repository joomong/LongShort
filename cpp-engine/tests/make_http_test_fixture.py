#!/usr/bin/env python3
"""Create deterministic thin-cache inputs for the C++ HTTP contract tests."""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

SYMBOLS = ["1", "2", "3", "4"]
LABELS = dict(zip(SYMBOLS, ["A", "B", "C", "D"]))
# Static classification, one industry nested inside each sector.
SECTOR_CODES = dict(zip(SYMBOLS, [1, 1, 2, 2]))
INDUSTRY_CODES = dict(zip(SYMBOLS, [1, 2, 3, 3]))


def write_wide(path, index, columns):
    """One wide panel: timestamp index column first, then float32 symbol columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {"__index_level_0__": pa.array(index, type=pa.timestamp("ns"))}
    for symbol, values in columns.items():
        arrays[symbol] = pa.array(np.asarray(values, dtype=np.float32), type=pa.float32())
    pq.write_table(pa.table(arrays), path, row_group_size=512)


def write_compact_cache(output):
    """Own cache, so the default config keeps one universe and no events: SMALL is smaller than the panel,
    with successors outside it. 2 -> 3 is a distribution; 5 -> 6 ends 5 into 6, which has no quote for 3 days."""
    output.mkdir(parents=True, exist_ok=True)
    symbols = ["1", "2", "3", "4", "5", "6"]
    days = np.arange(np.datetime64("2020-01-01"), np.datetime64("2022-01-01"))
    index = days.astype("datetime64[ns]")
    t = np.arange(len(days))
    close = np.column_stack([100 + 15*n + 0.01*t + 2*np.sin(0.09*t + 0.4*n) for n in range(len(symbols))])
    start = int((np.datetime64("2021-04-01") - days[0]) / np.timedelta64(1, "D"))
    close[start:start + 3, 5] = np.nan
    dates = pa.array(np.repeat(index, len(symbols)), type=pa.timestamp("ns"))
    ids = pa.array(np.tile(np.array(symbols, dtype=np.int64), len(days)), type=pa.int64())
    base = {"date": dates, "tradingItemId": ids}
    for column, factor, dtype in [("close", 1.0, pa.float32()), ("open", 0.995, pa.float32()),
                                  ("high", 1.01, pa.float32()), ("low", 0.99, pa.float32()),
                                  ("vwap", 1.0005, pa.float32()), ("volume", 10.0, pa.float64()),
                                  ("marketCap_USD", 1e6, pa.float64())]:
        base[column] = pa.array((close * factor).ravel(), type=dtype)
    pq.write_table(pa.table(base), output / "base_data.parquet")
    returns = np.zeros_like(close)
    returns[1:] = close[1:] / close[:-1] - 1.0
    for filename, column in [("returns.parquet", "returns"), ("adj_returns.parquet", "adj_returns")]:
        pq.write_table(pa.table({"date": dates, "tradingItemId": ids,
                                column: pa.array(returns.ravel(), type=pa.float64())}), output / filename)
    small = ["1", "2", "5"]
    members = [("ALL", s) for s in symbols] + [("SMALL", s) for s in small]
    first = pa.array([index[0]] * len(members), type=pa.timestamp("ns"))
    last = pa.array([index[-1]] * len(members), type=pa.timestamp("ns"))
    pq.write_table(pa.table({"universe": [m[0] for m in members],
                             "tradingItemId": pa.array([int(m[1]) for m in members], type=pa.int64()),
                             "fromDate": first, "toDate": last}), output / "universe.parquet")
    security_ids = pa.array([int(s) for s in symbols], type=pa.int64())
    pq.write_table(pa.table({"tradingItemId": security_ids,
                             "validFrom": pa.array([index[0]] * len(symbols), type=pa.timestamp("ns")),
                             "validTo": pa.array([None] * len(symbols), type=pa.timestamp("ns")),
                             "factor": pa.array([1.0] * len(symbols), type=pa.float64())}), output / "adj_factor.parquet")
    event_days = np.array(["2021-03-01", "2021-04-01"], dtype="datetime64[ns]")
    pq.write_table(pa.table({"date": pa.array(event_days, type=pa.timestamp("ns")),
                             "sourceId": pa.array([2, 5], type=pa.int64()),
                             "destinationId": pa.array([3, 6], type=pa.int64()),
                             "valueRatio": pa.array([0.5, 1.0], type=pa.float64()),
                             "sourceEnds": pa.array([False, True], type=pa.bool_())}), output / "events.parquet")
    pq.write_table(pa.table({"tradingItemId": security_ids, "symbol": symbols,
                             "companyName": symbols}), output / "master.parquet")
    for name in ("sector", "industry"):
        write_wide(output / "groups" / f"{name}.parquet", index,
                   {s: np.full(len(days), 1.0) for s in symbols})
    config = {"base": {"cache": str(output), "groups": str(output / "groups")}, "universe": {"names": ["ALL", "SMALL"]},
              "backtest": {"start_date": "2020-01-01", "end_date": "2021-12-31",
                           "initial_capital": 1000000, "transaction_fee": 5}}
    (output / "config.yaml").write_text(json.dumps(config, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    days = np.arange(np.datetime64("2020-01-01"), np.datetime64("2026-01-01"))
    index = days.astype("datetime64[ns]")
    t = np.arange(len(days))
    close = {s: 100 + 15*n + 0.01*t + 2*np.sin(0.09*t + 0.4*n) for n, s in enumerate(SYMBOLS)}
    fields = {
        "close": close,
        "open": {s: v*0.995 for s, v in close.items()},
        "high": {s: v*1.01 for s, v in close.items()},
        "low": {s: v*0.99 for s, v in close.items()},
        "volume": {s: 1000 + 10*n + t % 30 for n, s in enumerate(SYMBOLS)},
        "VWAP": {s: v*1.0005 for s, v in close.items()},
        "marketCap_USD": {s: v*1e6 for s, v in close.items()},
    }
    dates = pa.array(np.repeat(index, len(SYMBOLS)), type=pa.timestamp("ns"))
    ids = pa.array(np.tile(np.array(SYMBOLS, dtype=np.int64), len(days)), type=pa.int64())
    base = {"date": dates, "tradingItemId": ids}
    for name, columns in fields.items():
        values = np.column_stack([np.asarray(columns[s], dtype=np.float32) for s in SYMBOLS]).ravel()
        column = "vwap" if name == "VWAP" else name
        dtype = pa.float64() if name in ("volume", "marketCap_USD") else pa.float32()
        base[column] = pa.array(values, type=dtype)
    pq.write_table(pa.table(base), output / "base_data.parquet", row_group_size=2048)

    prices = np.column_stack([np.asarray(close[s], dtype=np.float32) for s in SYMBOLS]).astype(np.float64)
    returns = np.zeros_like(prices)
    returns[1:] = prices[1:] / prices[:-1] - 1.0
    for filename, column in [("returns.parquet", "returns"), ("adj_returns.parquet", "adj_returns")]:
        pq.write_table(pa.table({"date": dates, "tradingItemId": ids,
                                column: pa.array(returns.ravel(), type=pa.float64())}), output / filename)
    security_ids = pa.array([int(s) for s in SYMBOLS], type=pa.int64())
    first = pa.array([index[0]] * len(SYMBOLS), type=pa.timestamp("ns"))
    last = pa.array([index[-1]] * len(SYMBOLS), type=pa.timestamp("ns"))
    pq.write_table(pa.table({"universe": ["ALL"] * len(SYMBOLS), "tradingItemId": security_ids,
                            "fromDate": first, "toDate": last}), output / "universe.parquet")
    pq.write_table(pa.table({"tradingItemId": security_ids, "validFrom": first,
                            "validTo": pa.array([None] * len(SYMBOLS), type=pa.timestamp("ns")),
                            "factor": pa.array([1.0] * len(SYMBOLS), type=pa.float64())}), output / "adj_factor.parquet")
    pq.write_table(pa.table({"date": pa.array([], type=pa.timestamp("ns")),
                            "sourceId": pa.array([], type=pa.int64()),
                            "destinationId": pa.array([], type=pa.int64()),
                            "valueRatio": pa.array([], type=pa.float64()),
                            "sourceEnds": pa.array([], type=pa.bool_())}), output / "events.parquet")
    pq.write_table(pa.table({"tradingItemId": security_ids,
                            "symbol": [LABELS[s] for s in SYMBOLS],
                            "companyName": ["Fixture " + LABELS[s] for s in SYMBOLS]}), output / "master.parquet")
    for name, codes in (("sector", SECTOR_CODES), ("industry", INDUSTRY_CODES)):
        write_wide(output / "groups" / f"{name}.parquet", index,
                   {s: np.full(len(days), float(codes[s])) for s in SYMBOLS})

    meta = output / "meta"
    members = {
        "sample/a_rank.xml": '<data expr="close" delay="1"/><rank/>',
        "sample/b_tsmean_zscore.xml": '<data expr="close" delay="1"/><tsmean days="5"/><zscore/>',
        "sample/sub/c_tsrank.xml": '<data expr="close" delay="1"/><tsrank days="10"/>',
        "sample/.hidden.xml": '<data expr="close" delay="1"/><rank/>',
        "broken/ok.xml": '<data expr="close" delay="1"/><rank/>',
        "broken/bad.xml": '<data expr="close" delay="1"/><tsmean dayz="5"/>',
        "nested/x.xml": '<meta expr="sample"/>',
    }
    # Disconnect test: enough work per member (min_count 1 keeps every rank O(days)) for a few CPU seconds.
    heavy = '<data expr="close" delay="1"/><tsrank days="1800" min_count="1"/>' * 100
    for k in range(3):
        members[f"heavy/m{k}.xml"] = heavy
    # Ten cheap members for member-count checks of the combination methods.
    for k in range(10):
        members[f"ten/m{k}.xml"] = f'<data expr="close" delay="1"/><tsmean days="{k + 1}"/><rank/>'
    for rel, xml in members.items():
        (meta / rel).parent.mkdir(parents=True, exist_ok=True)
        (meta / rel).write_text(xml + "\n")
    (meta / "empty").mkdir(parents=True, exist_ok=True)

    config = {"base": {"cache": str(output), "groups": str(output / "groups")},
              "universe": {"name": "ALL"}, "backtest": {"start_date": "2020-01-01", "end_date": "2025-12-31",
              "initial_capital": 1000000, "transaction_fee": 5},
              "meta_dir": str(meta)}
    (output / "config.yaml").write_text(json.dumps(config, indent=2))
    write_compact_cache(output / "compact")
    print(output / "config.yaml")


if __name__ == "__main__":
    main()
