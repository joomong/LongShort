#!/usr/bin/env python3
"""Build the `sector` / `industry` wide group panels the engine's `<neutralize group="..."/>` reads.

Inputs (read-only):
  * master parquet  - `tradingItemId` + `sector` + `simpleIndustryDescription` (v1 or v2 layout)
  * price cache     - `<cache>/BaseData/close.parquet` supplies the calendar (index) and the
                      symbol columns; `BaseData/TimeStamp.parquet` and `BaseData/tradingItemId.parquet`
                      are only cross-checked, never used as the axis source
  * sector index    - base `sector/sectorIx.parquet` fixes the sector code order across caches

Output (`--output` directory):
  | file | content |
  |---|---|
  | `sector.parquet` `industry.parquet` | wide float32, same index/columns/order as `close.parquet`, codes 1..K, unclassified NaN |
  | `sectorIx.parquet` `industryIx.parquet` | code -> label, row 0 None (base `sectorIx.parquet` structure) |
  | `build_report.md` | counts, label tables, `--check-against` parity, sha256 |
  | `MANIFEST.json` | the same numbers as JSON |

The classification is static: the current master labels are broadcast over the whole calendar,
so the panel carries look-ahead (see README_group_panels.md). Writes are deterministic - the same
inputs produce byte-identical files (no timestamps anywhere in the outputs).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

INDEX_COLUMN = "__index_level_0__"
MASTER_ID = "tradingItemId"
MASTER_SECTOR = "sector"
MASTER_INDUSTRY = "simpleIndustryDescription"
SECTOR_INDEX_COLUMN = "sectorIx"
INDUSTRY_INDEX_COLUMN = "industryIx"
CHECK_COLUMN_BATCH = 1000
# Fixed so a rebuild is byte-identical; the reader does not care about row group layout.
# use_dictionary keeps a constant column at ~1 value (75MB -> 13MB on the v1 panel).
WRITE_OPTIONS = dict(compression="snappy", use_dictionary=True, write_statistics=False, version="2.6")


class BuildError(RuntimeError):
    """A condition the caller must fix; reported on stderr with exit code 1."""


# --------------------------------------------------------------------------------------
# Axes: calendar and symbols come from close.parquet only (schema + index column)
# --------------------------------------------------------------------------------------
def wide_axes(path: Path):
    """Return (symbols, timestamps, index_column) of a wide panel without loading its values."""
    parquet = pq.ParquetFile(path)
    schema = parquet.schema_arrow
    index_column = INDEX_COLUMN if INDEX_COLUMN in schema.names else None
    if index_column is None:
        metadata = schema.pandas_metadata or {}
        for entry in metadata.get("index_columns", []):
            if isinstance(entry, str) and entry in schema.names:
                index_column = entry
                break
    if index_column is None:
        raise BuildError(f"{path} has no index column ({INDEX_COLUMN} or a pandas index column)")
    symbols = [name for name in schema.names if name != index_column]
    stamps = parquet.read(columns=[index_column]).column(0)
    if not pa.types.is_timestamp(stamps.type) and not pa.types.is_date(stamps.type):
        raise BuildError(f"{path} index column {index_column} is {stamps.type}, expected a timestamp")
    timestamps = stamps.to_pandas().to_numpy().astype("datetime64[ns]")
    return symbols, timestamps, index_column


def axis_cross_checks(cache: Path, symbols, timestamps) -> dict:
    """Report whether BaseData/TimeStamp.parquet and tradingItemId.parquet agree with close.parquet."""
    checks = {}
    stamp_file = cache / "BaseData" / "TimeStamp.parquet"
    if not stamp_file.exists():
        checks["TimeStamp.parquet"] = {"present": False}
    else:
        values = pq.read_table(stamp_file).column(0).to_pandas().to_numpy().astype("datetime64[ns]")
        checks["TimeStamp.parquet"] = {
            "present": True, "rows": int(values.size),
            "matches_close_index": bool(values.size == timestamps.size and np.array_equal(values, timestamps)),
            "same_set": bool(set(values.tolist()) == set(timestamps.tolist())),
            "first_order_difference": next((i for i, (a, b) in enumerate(zip(values, timestamps))
                                            if a != b), None),
        }
    id_file = cache / "BaseData" / "tradingItemId.parquet"
    if not id_file.exists():
        checks["tradingItemId.parquet"] = {"present": False}
    else:
        table = pq.read_table(id_file)
        column = MASTER_ID if MASTER_ID in table.column_names else table.column_names[0]
        values = [normalized_id(value) for value in table.column(column).to_pylist()]
        checks["tradingItemId.parquet"] = {
            "present": True, "rows": len(values),
            "matches_close_columns": bool(values == list(symbols)),
            "same_set": bool(set(values) == set(symbols)),
            "first_order_difference": next((i for i, (a, b) in enumerate(zip(values, symbols))
                                            if a != b), None),
        }
    return checks


# --------------------------------------------------------------------------------------
# Master
# --------------------------------------------------------------------------------------
def normalized_id(value) -> str:
    """Cache column names are decimal trading item ids; normalise every id source to that string."""
    if isinstance(value, str):
        return value.strip()
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return ""
    return str(int(value))


def clean_labels(series: pd.Series) -> pd.Series:
    text = series.astype(object).map(lambda v: v.strip() if isinstance(v, str) else v)
    text = text.where(text.notna() & (text != ""), other=None)
    return text


def load_master(path: Path) -> pd.DataFrame:
    """Read the three classification columns. `tradingItemId` may be the index (v1) or a column (v2)."""
    schema = pq.ParquetFile(path).schema_arrow
    wanted = [name for name in (MASTER_ID, MASTER_SECTOR, MASTER_INDUSTRY) if name in schema.names]
    missing = {MASTER_SECTOR, MASTER_INDUSTRY} - set(wanted)
    if missing:
        raise BuildError(f"{path} has no column(s) {sorted(missing)}")
    frame = pq.read_table(path, columns=wanted).to_pandas()
    if MASTER_ID not in frame.columns:
        frame = frame.reset_index()
    if MASTER_ID not in frame.columns:
        raise BuildError(f"{path} has no {MASTER_ID} column or index")
    out = pd.DataFrame({
        MASTER_ID: frame[MASTER_ID].map(normalized_id),
        MASTER_SECTOR: clean_labels(frame[MASTER_SECTOR]),
        MASTER_INDUSTRY: clean_labels(frame[MASTER_INDUSTRY]),
    })
    out = out[out[MASTER_ID] != ""]
    duplicates = int(out[MASTER_ID].duplicated().sum())
    out = out.drop_duplicates(subset=[MASTER_ID], keep="first").reset_index(drop=True)
    out.attrs["duplicate_rows"] = duplicates
    return out


def nesting_violations(master: pd.DataFrame) -> list:
    """Every industry must sit inside exactly one sector; anything else breaks the group hierarchy."""
    both = master.dropna(subset=[MASTER_SECTOR, MASTER_INDUSTRY])
    spread = both.groupby(MASTER_INDUSTRY)[MASTER_SECTOR].unique()
    return [(industry, sorted(sectors)) for industry, sectors in spread.items() if len(sectors) > 1]


# --------------------------------------------------------------------------------------
# Label -> code
# --------------------------------------------------------------------------------------
def read_label_index(path: Path) -> list:
    """Read a base `*Ix.parquet` (row 0 None, codes 1..K in row order)."""
    table = pq.read_table(path)
    values = table.column(0).to_pylist()
    if not values or values[0] is not None:
        raise BuildError(f"{path} must start with a None row (code 0 is 'unclassified')")
    labels = []
    for row, value in enumerate(values[1:], start=1):
        if value is None or str(value).strip() == "":
            raise BuildError(f"{path} row {row} is empty")
        labels.append(str(value).strip())
    if len(set(labels)) != len(labels):
        raise BuildError(f"{path} has duplicate labels")
    return labels


def label_codes(labels_present, index_path: Path | None, what: str) -> list:
    """Code order: the given index file when supplied, otherwise sorted labels."""
    present = sorted(labels_present)
    if index_path is None:
        return present
    ordered = read_label_index(index_path)
    unknown = [label for label in present if label not in ordered]
    if unknown:
        raise BuildError(f"{what} label(s) {unknown} are missing from {index_path}; "
                         "regenerate the index file or drop --sector-index")
    return ordered


def symbol_codes(symbols, master: pd.DataFrame, column: str, labels: list) -> np.ndarray:
    """float32 code per cache symbol: 1..K for a known label, NaN for unclassified/absent."""
    code_of = {label: float(position + 1) for position, label in enumerate(labels)}
    lookup = dict(zip(master[MASTER_ID], master[column]))
    codes = np.full(len(symbols), np.nan, dtype=np.float32)
    for position, symbol in enumerate(symbols):
        label = lookup.get(symbol)
        if label is not None:
            codes[position] = code_of[label]
    return codes


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------
def wide_schema(symbols, timestamps_dtype="datetime64[ns]") -> pa.Schema:
    """pandas-compatible wide schema (float32 columns + `__index_level_0__`), built without data."""
    empty = pd.DataFrame(np.empty((0, len(symbols)), dtype=np.float32), columns=list(symbols),
                         index=pd.DatetimeIndex(np.array([], dtype=timestamps_dtype)))
    return pa.Table.from_pandas(empty, preserve_index=True).schema


def write_wide_panel(path: Path, symbols, timestamps, codes, row_group_rows: int) -> None:
    """One arrow array per distinct code is shared by every column that carries it, so peak memory
    is (K+1) x rows x 4B instead of N x rows x 4B."""
    schema = wide_schema(symbols)
    rows = len(timestamps)
    step = rows if row_group_rows <= 0 else min(row_group_rows, rows)
    with pq.ParquetWriter(path, schema, **WRITE_OPTIONS) as writer:
        for start in range(0, max(rows, 1), max(step, 1)):
            stop = min(start + step, rows)
            if start >= stop and rows:
                break
            length = stop - start
            shared = {}
            for code in np.unique(codes[np.isfinite(codes)]).tolist() + [None]:
                fill = np.float32(np.nan) if code is None else np.float32(code)
                shared[code] = pa.array(np.full(length, fill, dtype=np.float32))
            arrays = [shared[None if not np.isfinite(code) else float(code)] for code in codes]
            arrays.append(pa.array(timestamps[start:stop]))
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            if not rows:
                break


def write_label_index(path: Path, column: str, labels) -> None:
    frame = pd.DataFrame({column: pd.Series([None] + list(labels), dtype=object)})
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=None), path, **WRITE_OPTIONS)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------
# Parity check against an existing panel
# --------------------------------------------------------------------------------------
def check_against(path: Path, symbols, timestamps, codes, batch: int) -> dict:
    """Cell agreement of the freshly built sector panel against a reference wide panel.

    Compared by symbol name, not by position: the base v1 panel holds the same symbol set as
    close.parquet in a different column order. Mismatching cells are split by kind so a base-side
    partial fill (NaN before the symbol entered the cache) is not confused with a different code.
    """
    reference_symbols, reference_timestamps, _ = wide_axes(path)
    order_difference = next((position for position, (a, b)
                             in enumerate(zip(reference_symbols, symbols)) if a != b), None)
    result = {
        "path": str(path),
        "reference_rows": int(reference_timestamps.size),
        "reference_columns": len(reference_symbols),
        "same_index": bool(reference_timestamps.size == timestamps.size
                           and np.array_equal(reference_timestamps, timestamps)),
        "same_columns": bool(list(reference_symbols) == list(symbols)),
        "same_column_set": bool(set(reference_symbols) == set(symbols)),
        "column_order_first_difference": order_difference,
    }
    if not result["same_index"]:
        result["note"] = "index differs; cell comparison skipped"
        return result
    available = set(reference_symbols)
    missing = [symbol for symbol in symbols if symbol not in available]
    comparable = [(position, symbol) for position, symbol in enumerate(symbols) if symbol in available]
    parquet = pq.ParquetFile(path)
    rows = int(timestamps.size)
    agree = 0
    cells = {"reference_nan_built_code": 0, "built_nan_reference_code": 0, "both_finite_differ": 0}
    kinds = {}
    mismatches = {}
    for begin in range(0, len(comparable), batch):
        chunk = comparable[begin:begin + batch]
        table = parquet.read(columns=[symbol for _, symbol in chunk])
        for position, symbol in chunk:
            values = table.column(symbol).to_numpy(zero_copy_only=False).astype(np.float64)
            finite = np.isfinite(values)
            code = codes[position]
            built_nan = not np.isfinite(code)
            same = ~finite if built_nan else (values == np.float64(code))
            hits = int(same.sum())
            agree += hits
            if hits == rows:
                continue
            reference_codes = np.unique(values[finite]).tolist()
            differ = 0 if built_nan else int(np.count_nonzero(finite & (values != np.float64(code))))
            reference_nan = 0 if built_nan else int(np.count_nonzero(~finite))
            built_nan_cells = int(finite.sum()) if built_nan else 0
            cells["reference_nan_built_code"] += reference_nan
            cells["built_nan_reference_code"] += built_nan_cells
            cells["both_finite_differ"] += differ
            if len(reference_codes) > 1:
                kind = "reference_varies"          # the base code itself changes over time
            elif differ:
                kind = "code_differs"              # constant base code, different from the master
            elif built_nan_cells:
                kind = "unclassified_here"         # base classifies it, the current master does not
            else:
                kind = "reference_partial"         # base only filled the tail of the calendar
            kinds[kind] = kinds.get(kind, 0) + 1
            first = np.argmax(finite) if finite.any() else None
            mismatches[symbol] = {
                "cells": rows - hits, "kind": kind,
                "built_code": None if built_nan else float(code),
                "reference_codes": reference_codes,
                "reference_varies": len(reference_codes) > 1,
                "reference_first_finite_date": None if first is None else str(pd.Timestamp(timestamps[first]).date()),
            }
    total = rows * len(comparable)
    # Everything but `reference_partial` means a real code disagreement, so it is listed in full;
    # `reference_partial` is a base-side fill artifact and only needs a count plus a sample.
    partial = sorted(symbol for symbol, info in mismatches.items() if info["kind"] == "reference_partial")
    result.update({
        "compared_symbols": len(comparable), "missing_in_reference": missing[:50],
        "missing_in_reference_count": len(missing),
        "cells": total, "agreeing_cells": agree,
        "agreement": (agree / total) if total else 1.0,
        "mismatching_cells_by_kind": cells,
        "mismatching_symbols": len(mismatches),
        "mismatching_symbols_by_kind": dict(sorted(kinds.items())),
        "mismatching_symbols_varying_reference": sum(1 for v in mismatches.values() if v["reference_varies"]),
        "mismatch_detail": {symbol: info for symbol, info in sorted(mismatches.items())
                            if info["kind"] != "reference_partial"},
        "reference_partial_symbols": len(partial),
        "reference_partial_sample": {symbol: mismatches[symbol] for symbol in partial[:20]},
    })
    return result


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------
def label_rows(labels, codes) -> list:
    counts = {}
    for code in codes[np.isfinite(codes)].astype(int).tolist():
        counts[code] = counts.get(code, 0) + 1
    return [(position + 1, label, counts.get(position + 1, 0)) for position, label in enumerate(labels)]


def write_report(path: Path, manifest: dict) -> None:
    axes, counts, check = manifest["axes"], manifest["counts"], manifest.get("check")
    lines = [
        "# 그룹 패널 생성 보고", "",
        "생성: `cpp-engine/tools/data/build_group_panels.py` (재실행 시 바이트 동일 — 보고서에 시각 없음)", "",
        "## 1. 입력", "",
        "| 항목 | 값 |", "|---|---|",
        f"| master | `{manifest['inputs']['master']}` |",
        f"| cache | `{manifest['inputs']['cache']}` |",
        f"| sector index | `{manifest['inputs']['sector_index']}` |",
        f"| output | `{manifest['inputs']['output']}` |", "",
        "## 2. 축(달력·심볼)", "",
        "달력과 심볼은 `BaseData/close.parquet`의 index/columns를 그대로 쓴다(값은 읽지 않음).", "",
        "| 항목 | 값 |", "|---|---|",
        f"| 행(날짜) | {axes['rows']} ({axes['first_date']} ~ {axes['last_date']}) |",
        f"| 열(심볼) | {axes['columns']} |",
    ]
    for name, info in axes["cross_checks"].items():
        if not info.get("present"):
            lines.append(f"| `{name}` | 없음 |")
        else:
            key = "matches_close_index" if "matches_close_index" in info else "matches_close_columns"
            detail = "" if info[key] else (f" (집합 동일: {info['same_set']}, "
                                           f"첫 불일치 위치 {info['first_order_difference']})")
            lines.append(f"| `{name}` | {info['rows']}행, close와 일치: {info[key]}{detail} |")
    lines += ["", "## 3. 매칭·결측", "", "| 항목 | 값 |", "|---|---|",
              f"| 마스터 행(중복 제거 후) | {counts['master_rows']} (중복 tradingItemId {counts['master_duplicate_rows']}행 버림) |",
              f"| 캐시 심볼 중 마스터에 있는 수 | {counts['symbols_in_master']} |",
              f"| 마스터에 없는 심볼 | {counts['symbols_absent_from_master']} |",
              f"| sector 코드 있는 심볼 | {counts['sector_symbols']} (NaN {counts['sector_nan_symbols']}) |",
              f"| industry 코드 있는 심볼 | {counts['industry_symbols']} (NaN {counts['industry_nan_symbols']}) |",
              f"| sector NaN 셀 | {counts['sector_nan_cells']} / {counts['cells']} |",
              f"| industry NaN 셀 | {counts['industry_nan_cells']} / {counts['cells']} |",
              f"| K(sector / industry) | {counts['sector_k']} / {counts['industry_k']} |", ""]
    for section, (title, column, rows) in enumerate(
            (("sector", SECTOR_INDEX_COLUMN, manifest["labels"]["sector"]),
             ("industry", INDUSTRY_INDEX_COLUMN, manifest["labels"]["industry"])), start=4):
        lines += [f"## {section}. {title} 라벨 (`{title}Ix.parquet`, 0행 None)", "",
                  f"| code | {column} | 심볼 수 |", "|---|---|---|"]
        lines += [f"| {code} | {label} | {count} |" for code, label, count in rows]
        lines.append("")
    lines += ["## 6. 대조(`--check-against`)", ""]
    if check is None:
        lines += ["실행하지 않음.", ""]
    else:
        lines += [f"기준 파일: `{check['path']}` ({check['reference_rows']}행 x {check['reference_columns']}열)", "",
                  "| 항목 | 값 |", "|---|---|",
                  f"| index 동일 | {check['same_index']} |",
                  f"| columns 동일 | {check['same_columns']} |"]
        if check["column_order_first_difference"] is not None:
            lines.append(f"| 열 순서 첫 불일치 위치 | {check['column_order_first_difference']} "
                         f"(심볼 집합 동일: {check['same_column_set']}) |")
        if "cells" in check:
            by_kind = check["mismatching_symbols_by_kind"]
            cells_by_kind = check["mismatching_cells_by_kind"]
            lines += [f"| 비교 심볼 | {check['compared_symbols']} (기준에 없는 심볼 {check['missing_in_reference_count']}) |",
                      f"| 셀 일치 | {check['agreeing_cells']} / {check['cells']} = {check['agreement'] * 100:.6f}% |",
                      f"| 불일치 심볼 | {check['mismatching_symbols']} |", ""]
            lines += ["불일치 심볼 분류", "", "| kind | 심볼 수 | 뜻 |", "|---|---|---|",
                      f"| reference_varies | {by_kind.get('reference_varies', 0)} | 기준 코드가 시간에 따라 변함(정적 방송과 구조적으로 다름) |",
                      f"| reference_partial | {by_kind.get('reference_partial', 0)} | 기준이 달력 뒷부분만 채움(코드 자체는 동일). 기준의 증분 갱신 아티팩트 |",
                      f"| code_differs | {by_kind.get('code_differs', 0)} | 기준 코드가 상수인데 현재 마스터와 다름 |",
                      f"| unclassified_here | {by_kind.get('unclassified_here', 0)} | 기준은 분류했으나 현재 마스터는 미분류 |", "",
                      "불일치 셀 분류", "", "| kind | 셀 수 |", "|---|---|",
                      f"| 기준 NaN / 생성 코드 있음 | {cells_by_kind['reference_nan_built_code']} |",
                      f"| 생성 NaN / 기준 코드 있음 | {cells_by_kind['built_nan_reference_code']} |",
                      f"| 둘 다 유한, 코드 다름 | {cells_by_kind['both_finite_differ']} |", ""]
            header = ["| 심볼 | 불일치 셀 | kind | 생성 코드 | 기준 코드 | 기준 첫 유한 날짜 |",
                      "|---|---|---|---|---|---|"]
            def rows_for(detail):
                return [f"| {symbol} | {info['cells']} | {info['kind']} | {info['built_code']} | "
                        f"{info['reference_codes']} | {info['reference_first_finite_date']} |"
                        for symbol, info in detail.items()]
            lines += [f"코드가 실제로 다른 심볼(= `reference_partial` 제외) **전수** "
                      f"{len(check['mismatch_detail'])}개", ""]
            lines += (header + rows_for(check["mismatch_detail"])) if check["mismatch_detail"] else ["없음"]
            lines += ["", f"`reference_partial` {check['reference_partial_symbols']}심볼 "
                          "— 코드는 같고 기준이 달력 뒷부분만 채운 경우. 앞 20개만 표기(전수는 생략).", ""]
            if check["reference_partial_sample"]:
                lines += header + rows_for(check["reference_partial_sample"]) + [""]
        else:
            lines += ["", check.get("note", ""), ""]
    lines += ["## 7. 파일", "", "| 파일 | bytes | sha256 |", "|---|---|---|"]
    lines += [f"| `{item['name']}` | {item['bytes']} | `{item['sha256']}` |" for item in manifest["files"]]
    lines += ["", "## 8. 한계", "",
              "- 분류는 **정적**이다. 현재 마스터 라벨을 전 기간에 방송하므로 과거 구간에는 look-ahead가 있다 "
              "(base `sector/sector.parquet`도 같은 방식).",
              "- 분류 이력(PIT)이 생기면 같은 wide 포맷으로 시변 패널만 교체하면 된다.",
              "- 캐시를 재구축하면 달력·심볼이 바뀌므로 이 파일들도 다시 만들어야 한다.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------
def build(args) -> int:
    cache = Path(args.cache)
    close = cache / "BaseData" / "close.parquet"
    if not close.exists():
        raise BuildError(f"{close} does not exist")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    symbols, timestamps, _ = wide_axes(close)
    cross_checks = axis_cross_checks(cache, symbols, timestamps)
    master = load_master(Path(args.master))

    violations = nesting_violations(master)
    if violations:
        for industry, sectors in violations[:20]:
            print(f"industry '{industry}' spans sectors {sectors}", file=sys.stderr)
        raise BuildError(f"{len(violations)} industry label(s) span more than one sector; "
                         "the group hierarchy is broken - fix the master before rebuilding")

    sector_labels = label_codes(master[MASTER_SECTOR].dropna().unique(),
                               Path(args.sector_index) if args.sector_index else None, "sector")
    industry_labels = label_codes(master[MASTER_INDUSTRY].dropna().unique(),
                                  Path(args.industry_index) if args.industry_index else None, "industry")
    sector_codes = symbol_codes(symbols, master, MASTER_SECTOR, sector_labels)
    industry_codes = symbol_codes(symbols, master, MASTER_INDUSTRY, industry_labels)
    # "in the master" is membership, not classification: an unclassified master row still counts.
    master_ids = set(master[MASTER_ID])
    matched = sum(1 for symbol in symbols if symbol in master_ids)

    write_wide_panel(output / "sector.parquet", symbols, timestamps, sector_codes, args.row_group_rows)
    write_wide_panel(output / "industry.parquet", symbols, timestamps, industry_codes, args.row_group_rows)
    write_label_index(output / "sectorIx.parquet", SECTOR_INDEX_COLUMN, sector_labels)
    write_label_index(output / "industryIx.parquet", INDUSTRY_INDEX_COLUMN, industry_labels)

    check = None
    if args.check_against:
        check = check_against(Path(args.check_against), symbols, timestamps, sector_codes,
                              args.check_batch_columns)

    rows, columns = int(timestamps.size), len(symbols)
    sector_nan = int(np.count_nonzero(~np.isfinite(sector_codes)))
    industry_nan = int(np.count_nonzero(~np.isfinite(industry_codes)))
    manifest = {
        "tool": "cpp-engine/tools/data/build_group_panels.py",
        "inputs": {"master": str(Path(args.master)), "cache": str(cache), "output": str(output),
                   "sector_index": str(args.sector_index) if args.sector_index else None,
                   "industry_index": str(args.industry_index) if args.industry_index else None,
                   "check_against": str(args.check_against) if args.check_against else None},
        "axes": {"rows": rows, "columns": columns,
                 "first_date": str(pd.Timestamp(timestamps[0]).date()) if rows else None,
                 "last_date": str(pd.Timestamp(timestamps[-1]).date()) if rows else None,
                 "cross_checks": cross_checks},
        "counts": {
            "master_rows": int(len(master)), "master_duplicate_rows": int(master.attrs["duplicate_rows"]),
            "symbols_in_master": int(matched), "symbols_absent_from_master": columns - int(matched),
            "sector_symbols": columns - sector_nan, "sector_nan_symbols": sector_nan,
            "industry_symbols": columns - industry_nan, "industry_nan_symbols": industry_nan,
            "sector_nan_cells": sector_nan * rows, "industry_nan_cells": industry_nan * rows,
            "cells": rows * columns, "sector_k": len(sector_labels), "industry_k": len(industry_labels),
        },
        "labels": {"sector": label_rows(sector_labels, sector_codes),
                   "industry": label_rows(industry_labels, industry_codes)},
        "check": check,
        "files": [],
    }
    for name in ("sector.parquet", "industry.parquet", "sectorIx.parquet", "industryIx.parquet"):
        path = output / name
        manifest["files"].append({"name": name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    report = output / "build_report.md"
    write_report(report, manifest)
    manifest["files"].append({"name": report.name, "bytes": report.stat().st_size,
                              "sha256": sha256_file(report)})
    (output / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=False) + "\n",
                                          encoding="utf-8")
    print(f"{output}: {rows} x {columns}, sector K={len(sector_labels)} (NaN {sector_nan} symbols), "
          f"industry K={len(industry_labels)} (NaN {industry_nan} symbols)")
    if check and "agreement" in check:
        print(f"check-against {check['path']}: {check['agreement'] * 100:.6f}% cells, "
              f"{check['mismatching_symbols']} mismatching symbols")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master", required=True, help="master parquet (tradingItemId as index or column)")
    parser.add_argument("--cache", required=True, help="price cache root; BaseData/close.parquet gives the axes")
    parser.add_argument("--output", required=True, help="output directory, e.g. data/groups/cache_v1_cash_1d")
    parser.add_argument("--sector-index", default=None,
                        help="base sectorIx.parquet; fixes the sector code order (default: sorted labels)")
    parser.add_argument("--industry-index", default=None,
                        help="existing industryIx.parquet to keep codes stable (default: sorted labels)")
    parser.add_argument("--check-against", default=None,
                        help="existing wide sector panel to compare the built sector.parquet against")
    parser.add_argument("--row-group-rows", type=int, default=0,
                        help="rows per parquet row group (0 = single row group, like close.parquet)")
    parser.add_argument("--check-batch-columns", type=int, default=CHECK_COLUMN_BATCH,
                        help="columns read at a time during --check-against")
    args = parser.parse_args(argv)
    try:
        return build(args)
    except BuildError as error:
        print(f"build_group_panels.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
