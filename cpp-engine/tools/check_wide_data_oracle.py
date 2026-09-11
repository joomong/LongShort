#!/usr/bin/env python3
"""Verify C++ wide loading against a small independent Arrow/NumPy reference.

This reads a handful of stocks and one AFL file; it does not create an ETL cache.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np
import pyarrow.parquet as pq


def times(table, name="__index_level_0__"):
    values = table.column(name).to_numpy(zero_copy_only=False)
    return values.astype("datetime64[ms]").astype(np.int64) if np.issubdtype(values.dtype, np.datetime64) else values.astype(np.int64)


def aligned(path, symbols, calendar, asof=False, mask=False):
    file = pq.ParquetFile(path)
    available = [symbol for symbol in symbols if symbol in file.schema_arrow.names]
    table = file.read(columns=["__index_level_0__", *available])
    source_times = times(table)
    order = np.argsort(source_times)
    source_times = source_times[order]
    assert len(np.unique(source_times)) == len(source_times)
    positions = np.searchsorted(source_times, calendar, side="right") - 1
    output = np.full((len(calendar), len(symbols)), np.nan, dtype=np.float32)
    for column, symbol in enumerate(symbols):
        if symbol not in available:
            continue
        values = np.array(table.column(symbol).to_pylist(), dtype=np.float32)[order]
        for row, position in enumerate(positions):
            if position >= 0 and (asof or source_times[position] == calendar[row]):
                output[row, column] = values[position]
    return (output == 1).astype(np.uint8) if mask else output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("/home/base/cache/cache_v1_cash_1d"))
    parser.add_argument("--catalog", type=Path, default=Path(__file__).resolve().parents[1] / "examples/datasets/afl_catalog.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--start", default="2025-01-02")
    parser.add_argument("--end", default="2025-03-31")
    parser.add_argument("--warmup-bars", type=int, default=5)
    parser.add_argument("--symbols", type=int, default=6)
    args = parser.parse_args()
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="longshort_wide_oracle_"))
    output_dir.mkdir(parents=True, exist_ok=True)
    close_path = args.cache / "BaseData/close.parquet"
    universe_path = args.cache / "TopUniverseV1/Top500.parquet"
    price = pq.ParquetFile(close_path)
    all_dates = np.sort(times(price.read(columns=["__index_level_0__"])))
    start = np.datetime64(args.start, "ms").astype(np.int64)
    end = np.datetime64(args.end, "ms").astype(np.int64) + 86400000
    first, last = np.searchsorted(all_dates, [start, end])
    begin = max(0, first - args.warmup_bars)
    calendar = all_dates[begin:last]
    assert len(calendar) > 0
    universe = pq.ParquetFile(universe_path)
    candidates = sorted(set(price.schema_arrow.names).intersection(universe.schema_arrow.names) - {"__index_level_0__"})
    symbols = []
    # Find a few active symbols using narrow column projections of the bool mask.
    for offset in range(0, len(candidates), 64):
        batch = candidates[offset:offset + 64]
        membership = aligned(universe_path, batch, calendar, mask=True)
        symbols.extend(symbol for index, symbol in enumerate(batch) if membership[:, index].any())
        if len(symbols) >= args.symbols:
            symbols = symbols[:args.symbols]
            break
    assert len(symbols) == args.symbols
    catalog = json.loads(args.catalog.read_text())
    factor_path = catalog["fields"]["afl_100"]
    aliases = [name for name, path in catalog["fields"].items() if path == factor_path]
    assert len(aliases) >= 2
    fields = ["close", "volume", *aliases]
    config = {
        "datasets": [
            {"name": "prices", "format": "wide", "path": str(args.cache / "BaseData"),
             "fields": {"close": "close.parquet", "volume": "volume.parquet"}},
            {"name": "afl", "format": "wide", "path": str(args.cache / "AflData_M"),
             "catalog": str(args.catalog.resolve()), "lazy": True, "alignment": "asof"},
        ],
        "universe": {"path": str(universe_path), "symbols": symbols},
        "backtest": {"start_date": args.start, "end_date": args.end, "warmup_bars": args.warmup_bars,
                     "bars_per_year": 252, "transaction_fee": 1.5},
    }
    config_path = output_dir / "config.json"
    config_path.write_text(json.dumps(config, indent=2))
    result = subprocess.run([str(args.probe.resolve()), str(config_path.resolve()), *fields], check=True, capture_output=True, text=True)
    actual = json.loads(result.stdout)
    assert actual["symbols"] == symbols
    np.testing.assert_array_equal(actual["timestamps"], calendar)
    assert actual["tradeStartIndex"] == first - begin
    expected_mask = aligned(universe_path, symbols, calendar, mask=True)
    np.testing.assert_array_equal(np.array(actual["universeMask"]).reshape(expected_mask.shape), expected_mask)
    for field in fields:
        path = args.cache / ("BaseData" if field in {"close", "volume"} else "AflData_M") / (field + ".parquet" if field in {"close", "volume"} else factor_path)
        reference = aligned(path, symbols, calendar, asof=field in aliases)
        received = np.array(actual["values"][field], dtype=np.float32).reshape(reference.shape)
        np.testing.assert_allclose(received, reference, rtol=0, atol=0, equal_nan=True)
    assert np.isfinite(reference).any(), "AFL check needs at least one actual finite observation"
    assert actual["eagerLoadedFields"] == 2
    assert actual["loadedFields"] == 3, "Aliases must share one lazy value buffer"
    assert actual["registeredFields"] == len(catalog["fields"]) + 2
    assert actual["loadedBytes"] == 3 * len(calendar) * len(symbols) * 4
    report = {key: value for key, value in actual.items() if key not in {"values", "timestamps", "universeMask"}}
    report.update(checks="exact float32 price/volume/AFL + timestamps + PIT mask + aliases", catalogFactors=len(set(catalog["fields"].values())), passed=True)
    (output_dir / "cpp_result.json").write_text(result.stdout)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Artifacts: {output_dir}")


if __name__ == "__main__":
    main()
