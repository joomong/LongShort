#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/prices_factors.py - T2 step 3: half-open interval factor tables factors/split.parquet, factors/dividend.parquet.

Contract (BRIEF section 3-2): tradingItemId int64, validFrom timestamp (inclusive), validTo timestamp
(exclusive, NaT = open), factor float64, source str, snapshotDate timestamp. For every id in the symbol
set the intervals cover [1900-01-01, open) with no gap and no overlap, so a (id, date) lookup hits exactly
one row. `check_intervals` proves that and `self_test` shows the check catches broken tables first.

Sources
  split    : ciqsplitcache  [splitdate, nextsplitdate) cumulativesplitfactor (= ciqpriceequity.adjustmentfactor),
             the vendor's last window ends at the sentinel 2079-06-05 -> open (NaT).
             before the first window: cumulativesplitfactor x latestsplitfactor of the first window (`synthetic_pre_first`).
  dividend : ciqpriceequitydivadjfactor [fromdate, todate] closed -> validTo = todate + 1 day; todate NULL -> open.
             before the first window: first window's value (`synthetic_pre_first`).
  split    : ids whose ciqsplitcache windows do not reproduce the daily adjustmentfactor (missing windows, e.g. ADVV
             2587231: reverse split 1:250 in ciqsplit but no cache row) are rebuilt from the adjustmentfactor runs in
             ciqpriceequity (`ciqpriceequity_adjf`, boundary = first price row of the new run). Needs work/adjf_long.parquet.
  both     : no window at all -> 1.0 on [1900-01-01, open) (`synthetic_no_window`).
  source semantics beyond the contract list (BRIEF 3-2 is being extended by the lead):
    synthetic_gap_fill  : the vendor left [todate_k + 1, fromdate_k+1) uncovered; the row carries the PREVIOUS window's
                          value forward (no step inside the gap, the step lands on fromdate_k+1). Dividend table only in
                          practice (55 gap pairs / 34 ids, mostly pre-2000; a handful 2000+, e.g. MCRA.B 2017).
    synthetic_post_last : the vendor's last window is closed (todate not NULL) and nothing follows; the row carries the
                          LAST window's value to the open end (= "no further dividend known"). Verified: in the union
                          candidate set 7,009 such ids, 6,994 with factor 1.0; none in the prototype 22,871.
    ciqpriceequity_adjf : split intervals rebuilt from the daily adjustmentfactor runs (see below).
  overlapping vendor windows -> the earlier one is truncated at the later one's start (none observed).

CLI:
  prices_factors.py --self-test
  prices_factors.py --stage .../T2 --work .../T2_work        (ids from work/symbols.parquet)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import DB, SCHEMA, chunked, rss_gb  # noqa: E402

DEFAULT_STAGE = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2")
DEFAULT_WORK = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2_work")
EPOCH_START = pd.Timestamp("1900-01-01")
SPLIT_SENTINEL = pd.Timestamp("2079-01-01")       # nextsplitdate >= this means "open"
DAY = pd.Timedelta(days=1)
SRC_SPLIT, SRC_DIV = "ciqsplitcache", "ciqpriceequitydivadjfactor"
SRC_PRE, SRC_NONE, SRC_GAP, SRC_POST = "synthetic_pre_first", "synthetic_no_window", "synthetic_gap_fill", "synthetic_post_last"
SRC_ADJF = "ciqpriceequity_adjf"          # split intervals rebuilt from the daily adjustmentfactor runs (splitcache wrong/missing)
COLUMNS = ["tradingItemId", "validFrom", "validTo", "factor", "source", "snapshotDate"]

log = logging.getLogger("cache_v21.prices_factors")


# --------------------------------------------------------------------------------------
# interval construction
# --------------------------------------------------------------------------------------
def _normalize_one(tid: int, starts: np.ndarray, ends: np.ndarray, vals: np.ndarray, pre_first_value: float,
                   source: str) -> list[tuple]:
    """Vendor windows of one id (half-open [start, end), end NaT = open) -> gapless rows from 1900-01-01.

    starts/ends: datetime64[ns] arrays (ends may contain NaT), vals float. Rows are (tid, from, to, factor, source).
    Policy: pre-first gap -> pre_first_value; gap -> previous value (synthetic_gap_fill); overlap -> the later window
    wins (earlier one truncated/dropped); closed last window -> its value carried to the open end (synthetic_post_last)."""
    INF = np.datetime64("2200-01-01", "ns")
    epoch = EPOCH_START.to_datetime64()
    ends = np.where(np.isnat(ends), INF, ends)
    order = np.lexsort((ends.astype("int64"), starts.astype("int64")))
    rows: list[list] = []
    for s, e, v in zip(starts[order], ends[order], vals[order]):
        s = max(s, epoch)
        if e <= s:                                   # empty window
            continue
        if not rows:
            if s > epoch:
                rows.append([tid, epoch, s, float(pre_first_value), SRC_PRE])
        else:
            while rows and s < rows[-1][2]:          # overlap: later window wins
                if rows[-1][1] < s:
                    rows[-1][2] = s
                else:
                    rows.pop()
            if not rows:
                if s > epoch:
                    rows.append([tid, epoch, s, float(pre_first_value), SRC_PRE])
            elif s > rows[-1][2]:                    # gap: carry the previous value forward
                rows.append([tid, rows[-1][2], s, rows[-1][3], SRC_GAP])
        rows.append([tid, s, e, float(v), source])
    if not rows:
        return [(tid, epoch, None, 1.0, SRC_NONE)]
    if rows[-1][2] < INF:                            # last vendor window closed -> extend with its value
        rows.append([tid, rows[-1][2], INF, rows[-1][3], SRC_POST])
    return [(r[0], r[1], None if r[2] >= INF else r[2], r[3], r[4]) for r in rows]


def build_intervals(windows: pd.DataFrame, ids, source: str, snapshot: pd.Timestamp,
                    pre_first: str = "first") -> pd.DataFrame:
    """windows: columns tid, start, end (end NaT = open), value[, pre] -> contract frame for all `ids`.

    pre_first='first' uses the first window's value before it; pre_first='pre' uses the column `pre`."""
    rows = []
    have = set()
    if len(windows):
        w = windows.sort_values(["tid", "start"])
        for tid, g in w.groupby("tid", sort=True):
            starts = g.start.to_numpy().astype("datetime64[ns]")
            ends = g.end.to_numpy().astype("datetime64[ns]")
            vals = g.value.to_numpy(dtype=np.float64)
            g0 = g.iloc[0]
            pre_val = float(g0.pre) if pre_first == "pre" else float(g0.value)
            rows.extend(_normalize_one(int(tid), starts, ends, vals, pre_val, source))
            have.add(int(tid))
    for tid in ids:
        if int(tid) not in have:
            rows.append((int(tid), EPOCH_START.to_datetime64(), None, 1.0, SRC_NONE))
    out = pd.DataFrame(rows, columns=["tradingItemId", "validFrom", "validTo", "factor", "source"])
    out["tradingItemId"] = out.tradingItemId.astype(np.int64)
    out["validFrom"] = pd.to_datetime(out.validFrom).astype("datetime64[ns]")
    out["validTo"] = pd.to_datetime(out.validTo).astype("datetime64[ns]")
    out["factor"] = out.factor.astype(np.float64)
    out["source"] = out.source.astype(str)
    out["snapshotDate"] = pd.Series(np.full(len(out), np.datetime64(pd.Timestamp(snapshot).normalize(), "ns")), index=out.index)
    out = out.sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    return out[COLUMNS]


def split_windows(sc: pd.DataFrame) -> pd.DataFrame:
    """ciqsplitcache rows -> generic windows (start=splitdate, end=nextsplitdate or NaT past the sentinel, value=csf, pre=csf*lsf)."""
    w = pd.DataFrame({"tid": sc.tradingitemid.astype(np.int64),
                      "start": pd.to_datetime(sc.splitdate).astype("datetime64[ns]"),
                      "end": pd.to_datetime(sc.nextsplitdate).astype("datetime64[ns]"),
                      "value": sc.cumulativesplitfactor.astype(float),
                      "pre": sc.cumulativesplitfactor.astype(float) * sc.latestsplitfactor.astype(float)})
    w.loc[w.end >= SPLIT_SENTINEL, "end"] = pd.NaT
    return w


def dividend_windows(dv: pd.DataFrame) -> pd.DataFrame:
    """ciqpriceequitydivadjfactor rows -> generic windows: [fromdate, todate] closed -> end = todate + 1 day."""
    to = pd.to_datetime(dv.todate).astype("datetime64[ns]")
    return pd.DataFrame({"tid": dv.tradingitemid.astype(np.int64),
                         "start": pd.to_datetime(dv.fromdate).astype("datetime64[ns]"),
                         "end": to + DAY,
                         "value": dv.divadjfactor.astype(float)})


# --------------------------------------------------------------------------------------
# verification and lookup
# --------------------------------------------------------------------------------------
def check_intervals(df: pd.DataFrame, ids=None) -> dict:
    """Gapless/overlap-free proof. Returns {"ok": bool, "violations": {kind: count}, "examples": [...]}."""
    v = {}
    ex = []
    d = df.sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    if list(d.columns) != COLUMNS:
        v["columns"] = 1
    for c in ("validFrom", "validTo", "snapshotDate"):
        if str(d[c].dtype) != "datetime64[ns]":
            v[f"dtype_{c}"] = str(d[c].dtype)
    if str(d.tradingItemId.dtype) != "int64" or str(d.factor.dtype) != "float64":
        v["dtype_id_or_factor"] = f"{d.tradingItemId.dtype}/{d.factor.dtype}"
    if d.factor.isna().any() or ~np.isfinite(d.factor).all():
        v["nan_factor"] = int((~np.isfinite(d.factor)).sum())
    if (d.factor <= 0).any():
        v["nonpositive_factor"] = int((d.factor <= 0).sum())
    if d.validFrom.isna().any():
        v["nat_validFrom"] = int(d.validFrom.isna().sum())
    midnight = (d.validFrom.dt.normalize() == d.validFrom) & (d.validTo.isna() | (d.validTo.dt.normalize() == d.validTo))
    if not midnight.all():
        v["not_midnight"] = int((~midnight).sum())
    same = d.tradingItemId.to_numpy()[1:] == d.tradingItemId.to_numpy()[:-1]
    first = np.r_[True, ~same]
    last = np.r_[~same, True]
    bad_first = first & (d.validFrom.to_numpy() != EPOCH_START.to_datetime64())
    if bad_first.any():
        v["first_not_1900"] = int(bad_first.sum())
        ex += [("first_not_1900", int(t), str(f)) for t, f in zip(d.tradingItemId[bad_first][:3], d.validFrom[bad_first][:3])]
    bad_last = last & d.validTo.notna().to_numpy()
    if bad_last.any():
        v["last_not_open"] = int(bad_last.sum())
        ex += [("last_not_open", int(t), str(f)) for t, f in zip(d.tradingItemId[bad_last][:3], d.validTo[bad_last][:3])]
    open_mid = (~last) & d.validTo.isna().to_numpy()
    if open_mid.any():
        v["open_not_last"] = int(open_mid.sum())
        ex += [("open_not_last", int(t), str(f)) for t, f in zip(d.tradingItemId[open_mid][:3], d.validFrom[open_mid][:3])]
    vt = d.validTo.to_numpy()[:-1]
    vf = d.validFrom.to_numpy()[1:]
    contig = same & ~np.isnat(vt)
    gap = contig & (vt < vf)
    ovl = contig & (vt > vf)
    if gap.any():
        v["gap"] = int(gap.sum())
        i = np.flatnonzero(gap)[:3]
        ex += [("gap", int(d.tradingItemId[j]), str(d.validTo[j]), str(d.validFrom[j + 1])) for j in i]
    if ovl.any():
        v["overlap"] = int(ovl.sum())
        i = np.flatnonzero(ovl)[:3]
        ex += [("overlap", int(d.tradingItemId[j]), str(d.validTo[j]), str(d.validFrom[j + 1])) for j in i]
    empty = d.validTo.notna() & (d.validTo <= d.validFrom)
    if empty.any():
        v["empty_interval"] = int(empty.sum())
    if ids is not None:
        missing = set(int(i) for i in ids) - set(d.tradingItemId.astype(int))
        if missing:
            v["ids_missing"] = len(missing)
            ex += [("ids_missing", int(i)) for i in sorted(missing)[:3]]
    return {"ok": not v, "violations": v, "examples": ex, "rows": int(len(d)), "ids": int(d.tradingItemId.nunique())}


class FactorLookup:
    """(id, date) -> factor via one searchsorted on the combined key (id << 20 | days since 1970).

    Requires a gapless table from 1900 (proved by check_intervals) so the hit is always the right row."""

    def __init__(self, df: pd.DataFrame):
        d = df.sort_values(["tradingItemId", "validFrom"])
        days = d.validFrom.to_numpy().astype("datetime64[D]").astype(np.int64)
        # validFrom 1900-01-01 is negative in days-since-1970: shift by an offset so keys stay ordered and >= 0
        self.offset = 30000  # 1900-01-01 = -25567 days
        self.keys = (d.tradingItemId.to_numpy(np.int64) << 20) | (days + self.offset)
        self.ids = d.tradingItemId.to_numpy(np.int64)
        self.factor = d.factor.to_numpy(np.float64)
        self.valid_to = d.validTo.to_numpy().astype("datetime64[D]").astype(np.int64)  # NaT -> min int64
        self.row_index = d.index.to_numpy()
        if not (np.diff(self.keys) > 0).all():
            raise ValueError("duplicate (id, validFrom) in factor table")

    def positions(self, ids, dates) -> np.ndarray:
        days = np.asarray(dates).astype("datetime64[D]").astype(np.int64) + self.offset
        q = (np.asarray(ids, dtype=np.int64) << 20) | days
        pos = np.searchsorted(self.keys, q, side="right") - 1
        ok = pos >= 0
        ok &= self.ids[np.maximum(pos, 0)] == np.asarray(ids, dtype=np.int64)
        vt = self.valid_to[np.maximum(pos, 0)]
        is_open = vt == np.iinfo(np.int64).min
        ok &= is_open | ((days - self.offset) < vt)
        pos[~ok] = -1
        return pos

    def __call__(self, ids, dates, strict: bool = True) -> np.ndarray:
        pos = self.positions(ids, dates)
        if strict and (pos < 0).any():
            n = int((pos < 0).sum())
            bad = np.asarray(ids)[pos < 0][:5]
            raise KeyError(f"{n} (id, date) lookups have no covering interval, e.g. ids {bad.tolist()}")
        out = np.full(len(pos), np.nan)
        hit = pos >= 0
        out[hit] = self.factor[pos[hit]]
        return out


# --------------------------------------------------------------------------------------
# self-test: the check must FAIL on broken tables and PASS on the builder's output
# --------------------------------------------------------------------------------------
def _frame(rows, snapshot="2026-09-16") -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["tradingItemId", "validFrom", "validTo", "factor", "source"])
    df["validFrom"] = pd.to_datetime(df.validFrom, format="mixed")
    df["validTo"] = pd.to_datetime(df.validTo, format="mixed")
    df["tradingItemId"] = df.tradingItemId.astype(np.int64)
    df["factor"] = df.factor.astype(float)
    df["snapshotDate"] = pd.Series(np.full(len(df), np.datetime64(pd.Timestamp(snapshot), "ns")), index=df.index)
    return df[COLUMNS]


def self_test() -> list[dict]:
    res = []
    T = pd.Timestamp

    def rec(name, expect_ok, got: dict):
        res.append({"case": name, "expected": "PASS" if expect_ok else "FAIL", "got": "PASS" if got["ok"] else "FAIL",
                    "violations": got["violations"], "ok": got["ok"] == expect_ok})

    good = _frame([(1, "1900-01-01", "2014-06-09", 28.0, SRC_PRE), (1, "2014-06-09", "2020-08-31", 4.0, SRC_SPLIT),
                   (1, "2020-08-31", None, 1.0, SRC_SPLIT), (2, "1900-01-01", None, 1.0, SRC_NONE)])
    rec("valid table", True, check_intervals(good, ids=[1, 2]))
    rec("gap (1 day missing)", False, check_intervals(_frame([(1, "1900-01-01", "2014-06-09", 28.0, SRC_PRE),
                                                              (1, "2014-06-10", None, 4.0, SRC_SPLIT)])))
    rec("overlap (1 day double)", False, check_intervals(_frame([(1, "1900-01-01", "2014-06-10", 28.0, SRC_PRE),
                                                                  (1, "2014-06-09", None, 4.0, SRC_SPLIT)])))
    rec("last interval closed", False, check_intervals(_frame([(1, "1900-01-01", "2014-06-09", 28.0, SRC_PRE),
                                                                (1, "2014-06-09", "2020-08-31", 4.0, SRC_SPLIT)])))
    rec("first interval starts after 1900", False, check_intervals(_frame([(1, "1987-06-16", "2014-06-09", 28.0, SRC_SPLIT),
                                                                            (1, "2014-06-09", None, 4.0, SRC_SPLIT)])))
    rec("two open intervals", False, check_intervals(_frame([(1, "1900-01-01", None, 28.0, SRC_PRE),
                                                              (1, "2014-06-09", None, 4.0, SRC_SPLIT)])))
    rec("NaN factor", False, check_intervals(_frame([(1, "1900-01-01", "2014-06-09", np.nan, SRC_PRE),
                                                      (1, "2014-06-09", None, 4.0, SRC_SPLIT)])))
    rec("id missing from table", False, check_intervals(good, ids=[1, 2, 3]))
    rec("validFrom with time-of-day", False, check_intervals(_frame([(1, "1900-01-01", "2014-06-09 10:00", 28.0, SRC_PRE),
                                                                      (1, "2014-06-09 10:00", None, 4.0, SRC_SPLIT)])))

    # builder: vendor windows with a gap, an overlap and a closed last window -> gapless output
    sc = pd.DataFrame({"tradingitemid": [7, 7, 7, 8], "splitdate": ["2000-06-21", "2005-02-28", "2014-06-09", "2010-01-04"],
                       "nextsplitdate": ["2005-02-28", "2014-06-09", "2079-06-05", "2012-01-01"],
                       "cumulativesplitfactor": [56.0, 28.0, 4.0, 2.0], "latestsplitfactor": [2.0, 2.0, 7.0, 2.0]})
    sp = build_intervals(split_windows(sc), [7, 8, 9], SRC_SPLIT, T("2026-09-16"), pre_first="pre")
    chk = check_intervals(sp, ids=[7, 8, 9])
    rec("builder(split): sentinel->open, pre_first=csf*lsf, closed last->post_last, no window->1.0", True, chk)
    lk = FactorLookup(sp)
    got = lk([7, 7, 7, 7, 8, 8, 9], pd.to_datetime(["1999-12-31", "2000-06-21", "2014-06-08", "2014-06-09", "2012-01-01", "2009-01-01", "2026-01-01"]))
    exp = [112.0, 56.0, 28.0, 4.0, 2.0, 4.0, 1.0]
    res.append({"case": "lookup boundaries (validFrom inclusive, validTo exclusive, pre-first = csf x lsf)", "expected": exp,
                "got": got.tolist(), "ok": bool(np.allclose(got, exp))})
    srcs = sp[sp.tradingItemId == 8].source.tolist()
    res.append({"case": "builder sources for closed-last id", "expected": [SRC_PRE, SRC_SPLIT, SRC_POST], "got": srcs,
                "ok": srcs == [SRC_PRE, SRC_SPLIT, SRC_POST]})
    dv = pd.DataFrame({"tradingitemid": [5, 5, 5], "fromdate": ["2016-12-12", "2017-06-12", "2017-09-18"],
                       "todate": ["2017-03-12", "2017-09-17", None], "divadjfactor": [0.80, 0.81, 1.0]})
    dd = build_intervals(dividend_windows(dv), [5], SRC_DIV, T("2026-09-16"))
    chk = check_intervals(dd, ids=[5])
    rec("builder(dividend): closed [from,to] -> [from,to+1), gap filled with previous value", True, chk)
    gap = dd[dd.source == SRC_GAP]
    ok_gap = len(gap) == 1 and gap.validFrom.iloc[0] == T("2017-03-13") and gap.validTo.iloc[0] == T("2017-06-12") and gap.factor.iloc[0] == 0.80
    res.append({"case": "dividend gap row = [2017-03-13, 2017-06-12) x 0.80", "expected": True, "got": ok_gap, "ok": bool(ok_gap)})
    lk = FactorLookup(dd)
    got = lk([5, 5, 5, 5], pd.to_datetime(["2017-03-12", "2017-03-13", "2017-06-12", "2030-01-01"]))
    exp = [0.80, 0.80, 0.81, 1.0]
    res.append({"case": "dividend lookup at closed end / gap / next window / open", "expected": exp, "got": got.tolist(),
                "ok": bool(np.allclose(got, exp))})
    # reconcile: an id whose adjustmentfactor run (0.004) has no splitcache window must be rebuilt from the runs
    import tempfile
    import pyarrow as pa
    import pyarrow.parquet as pq
    with tempfile.TemporaryDirectory() as tmpd:
        adjf_path = Path(tmpd) / "adjf_long.parquet"
        d1 = pd.to_datetime(["2000-04-03", "2000-04-04", "2001-01-02"])           # id 11: adjf 0.004 (no window) -> 0.002 later
        d7 = pd.to_datetime(["2014-06-06", "2014-06-09"])                          # id 7: matches the splitcache table above
        tbl = pa.table({"tradingItemId": pa.array([7, 7, 11, 11, 11], pa.int64()),
                        "date": pa.array(list(d7) + list(d1), pa.timestamp("ns")),
                        "adjustmentfactor": pa.array([28.0, 4.0, 0.004, 0.004, 0.002], pa.float64()),
                        "ciqClose": pa.array([1.0] * 5, pa.float64())})
        pq.write_table(tbl, adjf_path)
        base = build_intervals(split_windows(sc), [7, 8, 11], SRC_SPLIT, T("2026-09-16"), pre_first="pre")
        rebuilt, rec = reconcile_split(base, adjf_path, T("2026-09-16"))
        chk = check_intervals(rebuilt, ids=[7, 8, 11])
        got = FactorLookup(rebuilt)([11, 11, 11, 11, 7], pd.to_datetime(["1999-01-01", "2000-04-03", "2000-12-31", "2001-01-02", "2014-06-09"]))
        exp = [0.004, 0.004, 0.004, 0.002, 4.0]
        src11 = rebuilt[rebuilt.tradingItemId == 11].source.tolist()
        res.append({"case": "reconcile: id 11 rebuilt from adjustmentfactor runs (ids_rebuilt=1, mismatches after=0, id 7 untouched)",
                    "expected": {"ids_rebuilt": 1, "after": 0, "lookup": exp, "src": [SRC_PRE, SRC_ADJF, SRC_ADJF]},
                    "got": {"ids_rebuilt": rec["ids_rebuilt_from_adjf"], "after": rec["rows_mismatched_after"], "lookup": got.tolist(), "src": src11},
                    "ok": bool(rec["ids_rebuilt_from_adjf"] == 1 and rec["rows_mismatched_after"] == 0 and np.allclose(got, exp) and chk["ok"]
                               and src11 == [SRC_PRE, SRC_ADJF, SRC_ADJF] and rec["rows_mismatched_before"] == 3)})
    # lookup must refuse an id that is not covered
    try:
        lk([6], pd.to_datetime(["2020-01-01"]))
        res.append({"case": "lookup of uncovered id raises", "expected": "KeyError", "got": "no error", "ok": False})
    except KeyError:
        res.append({"case": "lookup of uncovered id raises", "expected": "KeyError", "got": "KeyError", "ok": True})
    return res


# --------------------------------------------------------------------------------------
# reconcile the split table with the daily adjustmentfactor (ground truth of the raw reconstruction)
# --------------------------------------------------------------------------------------
def adjf_runs_and_mismatches(adjf_path: Path, lookup: FactorLookup, tol: float = 1e-6) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stream work/adjf_long.parquet (sorted id, date). Returns (runs, mismatch) where runs = one row per constant
    adjustmentfactor run (tradingItemId, start, factor, n) and mismatch = per-id count of rows where lookup != adjf."""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(adjf_path)
    runs, bad = [], []
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=["tradingItemId", "date", "adjustmentfactor"])
        ids = t["tradingItemId"].to_numpy()
        dates = t["date"].to_numpy().astype("datetime64[ns]")
        adjf = t["adjustmentfactor"].to_numpy(zero_copy_only=False)
        ok = np.isfinite(adjf)
        ids, dates, adjf = ids[ok], dates[ok], adjf[ok]
        if len(ids) == 0:
            continue
        start = np.r_[True, (ids[1:] != ids[:-1]) | (adjf[1:] != adjf[:-1])]
        idx = np.flatnonzero(start)
        n = np.diff(np.r_[idx, len(ids)])
        runs.append(pd.DataFrame({"tradingItemId": ids[idx], "start": dates[idx], "factor": adjf[idx], "n": n}))
        sf = lookup(ids, dates, strict=False)
        mism = ~(np.abs(sf / adjf - 1.0) <= tol)
        if mism.any():
            bad.append(pd.Series(ids[mism]).value_counts().rename("rows").rename_axis("tradingItemId").reset_index())
    runs = pd.concat(runs, ignore_index=True) if runs else pd.DataFrame(columns=["tradingItemId", "start", "factor", "n"])
    bad = pd.concat(bad, ignore_index=True).groupby("tradingItemId", as_index=False).rows.sum() if bad else pd.DataFrame(columns=["tradingItemId", "rows"])
    return runs, bad


def intervals_from_runs(runs: pd.DataFrame, snapshot: pd.Timestamp) -> pd.DataFrame:
    """Per id: [1900, first run start) first value (synthetic_pre_first), [start_k, start_k+1) factor_k (ciqpriceequity_adjf), last open."""
    rows = []
    for tid, g in runs.sort_values(["tradingItemId", "start"]).groupby("tradingItemId", sort=True):
        st = g.start.to_numpy().astype("datetime64[ns]")
        fv = g.factor.to_numpy(dtype=np.float64)
        # merge consecutive runs with equal factor (a run may be split across row groups)
        keep = np.r_[True, fv[1:] != fv[:-1]]
        st, fv = st[keep], fv[keep]
        rows.append((int(tid), EPOCH_START.to_datetime64(), st[0], fv[0], SRC_PRE))
        for k in range(len(st)):
            rows.append((int(tid), st[k], st[k + 1] if k + 1 < len(st) else None, fv[k], SRC_ADJF))
    out = pd.DataFrame(rows, columns=["tradingItemId", "validFrom", "validTo", "factor", "source"])
    out["validFrom"] = pd.to_datetime(out.validFrom).astype("datetime64[ns]")
    out["validTo"] = pd.to_datetime(out.validTo).astype("datetime64[ns]")
    out["snapshotDate"] = pd.Series(np.full(len(out), np.datetime64(pd.Timestamp(snapshot).normalize(), "ns")), index=out.index)
    return out[COLUMNS]


def reconcile_split(sp: pd.DataFrame, adjf_path: Path, snapshot: pd.Timestamp, tol: float = 1e-6) -> tuple[pd.DataFrame, dict]:
    """Replace the intervals of every id whose splitcache-based lookup disagrees with the daily adjustmentfactor."""
    runs, bad = adjf_runs_and_mismatches(adjf_path, FactorLookup(sp), tol)
    bad_ids = set(bad.tradingItemId.astype(int)) if len(bad) else set()
    info = {"ids_with_price_rows": int(runs.tradingItemId.nunique()), "ids_rebuilt_from_adjf": len(bad_ids),
            "rows_mismatched_before": int(bad.rows.sum()) if len(bad) else 0}
    if bad_ids:
        had_windows = set(sp[(sp.source == SRC_SPLIT) & sp.tradingItemId.isin(bad_ids)].tradingItemId.astype(int))
        info["rebuilt_ids_with_splitcache_windows"] = len(had_windows)
        info["rebuilt_ids_without_splitcache_windows"] = len(bad_ids - had_windows)
        rep = intervals_from_runs(runs[runs.tradingItemId.isin(bad_ids)], snapshot)
        sp = pd.concat([sp[~sp.tradingItemId.isin(bad_ids)], rep], ignore_index=True)
        sp = sp.sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
        info["rebuilt_ids"] = sorted(bad_ids)
        info["rebuilt_rows_by_id_top"] = bad.sort_values("rows", ascending=False).head(10).to_dict("records")
        # verify: after the rebuild every price row must match
        _, bad2 = adjf_runs_and_mismatches(adjf_path, FactorLookup(sp), tol)
        info["rows_mismatched_after"] = int(bad2.rows.sum()) if len(bad2) else 0
    else:
        info["rows_mismatched_after"] = 0
    return sp, info


# --------------------------------------------------------------------------------------
# build from the DB
# --------------------------------------------------------------------------------------
def fetch_split_windows(db: DB, ids) -> pd.DataFrame:
    parts = [db.query(f"""select tradingitemid, splitdate, nextsplitdate, cumulativesplitfactor, latestsplitfactor
                          from {SCHEMA}.ciqsplitcache where tradingitemid = any(%s)""", (list(map(int, c)),))
             for c in chunked(ids, 10000)]
    return pd.concat(parts, ignore_index=True)


def fetch_dividend_windows(db: DB, ids) -> pd.DataFrame:
    parts = [db.query(f"""select tradingitemid, fromdate, todate, divadjfactor
                          from {SCHEMA}.ciqpriceequitydivadjfactor where tradingitemid = any(%s)""", (list(map(int, c)),))
             for c in chunked(ids, 10000)]
    return pd.concat(parts, ignore_index=True)


def build_all(db: DB, ids, stage: Path, work: Path) -> dict:
    t0 = time.time()
    snapshot = pd.Timestamp.today().normalize()
    sc = fetch_split_windows(db, ids)
    t1 = time.time()
    dv = fetch_dividend_windows(db, ids)
    t2 = time.time()
    # keep the raw vendor windows for the QA module (step identities need the vendor rows, not the synthetic ones)
    work.mkdir(parents=True, exist_ok=True)
    sc.to_parquet(work / "raw_splitcache.parquet", index=False)
    dv.to_parquet(work / "raw_divadj.parquet", index=False)
    sp = build_intervals(split_windows(sc), ids, SRC_SPLIT, snapshot, pre_first="pre")
    dd = build_intervals(dividend_windows(dv), ids, SRC_DIV, snapshot)
    adjf_path = work / "adjf_long.parquet"
    if adjf_path.exists():
        sp, rec = reconcile_split(sp, adjf_path, snapshot)
        log.info("split reconcile vs adjustmentfactor: %s", {k: v for k, v in rec.items() if k != "rebuilt_ids"})
    else:
        rec = {"skipped": f"{adjf_path} not found (run prices_build.py first)"}
        log.warning("split reconcile skipped: %s", rec["skipped"])
    t3 = time.time()
    chk_sp = check_intervals(sp, ids=ids)
    chk_dd = check_intervals(dd, ids=ids)
    if not (chk_sp["ok"] and chk_dd["ok"]):
        raise RuntimeError(f"interval check failed: split {chk_sp['violations']} dividend {chk_dd['violations']}")
    (stage / "factors").mkdir(parents=True, exist_ok=True)
    sp.to_parquet(stage / "factors" / "split.parquet", index=False, compression="zstd")
    dd.to_parquet(stage / "factors" / "dividend.parquet", index=False, compression="zstd")
    info = {
        "snapshotDate": snapshot.strftime("%Y-%m-%d"), "n_ids": len(ids),
        "split": {"vendor_rows": int(len(sc)), "vendor_ids": int(sc.tradingitemid.nunique()), "rows": int(len(sp)),
                  "by_source": sp.source.value_counts().to_dict(), "check": chk_sp, "reconcile": rec,
                  "bytes": (stage / "factors" / "split.parquet").stat().st_size},
        "dividend": {"vendor_rows": int(len(dv)), "vendor_ids": int(dv.tradingitemid.nunique()), "rows": int(len(dd)),
                     "by_source": dd.source.value_counts().to_dict(), "check": chk_dd,
                     "bytes": (stage / "factors" / "dividend.parquet").stat().st_size,
                     "gap_fill_ids_2000plus": sorted(dd[(dd.source == SRC_GAP) & (dd.validTo >= "2000-01-01")].tradingItemId.unique().tolist())},
        "seconds": {"fetch_split": round(t1 - t0, 1), "fetch_dividend": round(t2 - t1, 1), "build_reconcile": round(t3 - t2, 1),
                    "check_write": round(time.time() - t3, 1),
                    "total": round(time.time() - t0, 1)}, "peak_rss_gb": round(rss_gb(), 2),
        "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (work / "factors_info.json").write_text(json.dumps(info, indent=1, default=str))
    log.info("factors: split %d rows (%s), dividend %d rows (%s), %.0fs", len(sp), info["split"]["by_source"], len(dd),
             info["dividend"]["by_source"], info["seconds"]["total"])
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--ids", type=Path, default=None, help="parquet with tradingItemId column (default work/symbols.parquet)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"], "| expected", r["expected"], "| got", r["got"],
                  ("| " + str(r.get("violations"))) if r.get("violations") else "")
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0
    ids_path = args.ids or (args.work / "symbols.parquet")
    sym = pd.read_parquet(ids_path)
    if "inSymbolSet" in sym.columns:
        sym = sym[sym.inSymbolSet]
    else:
        log.warning("%s has no inSymbolSet column: using all ids", ids_path)
    ids = sorted(sym.tradingItemId.astype(int).tolist())
    db = DB(application_name="cache_v21_T2_factors")
    info = build_all(db, ids, args.stage, args.work)
    db.close()
    print(json.dumps({k: v for k, v in info.items() if k != "split" and k != "dividend"}, indent=1))
    print("split by_source", info["split"]["by_source"], "check", info["split"]["check"]["ok"])
    print("dividend by_source", info["dividend"]["by_source"], "check", info["dividend"]["check"]["ok"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
