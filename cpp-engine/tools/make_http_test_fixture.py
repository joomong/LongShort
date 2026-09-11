#!/usr/bin/env python3
"""Create deterministic synthetic long data for the C++ HTTP contract tests."""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    days = np.arange(np.datetime64("2020-01-01"), np.datetime64("2026-01-01"))
    t = np.repeat(np.arange(len(days)), 4)
    n = np.tile(np.arange(4), len(days))
    close = 100 + 15*n + 0.01*t + 2*np.sin(0.09*t + 0.4*n)
    timestamps = np.repeat(days.astype("datetime64[ms]").astype(np.int64), 4)
    table = pa.table({"open_time": timestamps, "symbol": np.array(["A", "B", "C", "D"])[n],
                      "close": close, "open": close*0.995, "high": close*1.01, "low": close*0.99,
                      "volume": 1000 + 10*n + t % 30})
    pq.write_table(table, output / "prices.parquet", row_group_size=512)
    config = {"datasets": [{"name": "prices", "path": str(output / "prices.parquet"),
                             "time_col": "open_time", "symbol_col": "symbol",
                             "use_col": ["open", "high", "low", "close", "volume"]}],
              "universe": ["all"], "backtest": {"start_date": "2020-01-01", "end_date": "2025-12-31",
              "initial_capital": 1000000, "transaction_fee": 5}}
    (output / "config.yaml").write_text(json.dumps(config, indent=2))
    print(output / "config.yaml")


if __name__ == "__main__":
    main()
