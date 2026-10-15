#!/usr/bin/env python3
"""Create deterministic synthetic base data for the C++ HTTP contract tests.

The layout mirrors a production price cache so the generated config can use the
`base:` block: <output>/BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
plus <output>/groups/{sector,industry}.parquet. Every file is a wide panel whose
index column is `__index_level_0__` (timestamp[ns]) and whose remaining columns are
the symbols, exactly like the real cache. Group codes are small integers stored as
floats; NaN would mean unclassified.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

SYMBOLS = ["A", "B", "C", "D"]
# Static classification, one industry nested inside each sector.
SECTOR_CODES = {"A": 1, "B": 1, "C": 2, "D": 2}
INDUSTRY_CODES = {"A": 1, "B": 2, "C": 3, "D": 3}


def write_wide(path, index, columns):
    """One wide panel: timestamp index column first, then float32 symbol columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {"__index_level_0__": pa.array(index, type=pa.timestamp("ns"))}
    for symbol, values in columns.items():
        arrays[symbol] = pa.array(np.asarray(values, dtype=np.float32), type=pa.float32())
    pq.write_table(pa.table(arrays), path, row_group_size=512)


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
        write_wide(output / "BaseData" / f"{name}.parquet", index, columns)
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
    heavy = '<data expr="close" delay="1"/>' + '<tsrank days="2000" min_count="1"/>' * 100
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
              "universe": ["all"], "backtest": {"start_date": "2020-01-01", "end_date": "2025-12-31",
              "initial_capital": 1000000, "transaction_fee": 5},
              "meta_dir": str(meta)}
    (output / "config.yaml").write_text(json.dumps(config, indent=2))
    print(output / "config.yaml")


if __name__ == "__main__":
    main()
