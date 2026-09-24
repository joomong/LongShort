#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/prices_qa.py - T2 step 5: QA gates for base_data / factors / returns_total (+ miprice cross-check).

Every gate first runs on a synthetic counterexample ("does the check fail when it should?") and then on the
real data. Results -> <stage>/qa/gates.md, <stage>/qa/price_differences/{mi_close_diff_2010plus.parquet,summary.md},
machine-readable <work>/qa_results.json.

Gates
  G-SPLIT  : interval lookup of factors/split.parquet vs the vendor `adjustmentfactor` on every base_data row
             (|rel| > 1e-6 -> violation).
  G-JUMP   : at split-factor boundaries (rows where the factor differs from the previous observed row) the split-adjusted
             close return must not jump (|r_adj| > 40%) while the raw close barely moves (|r_raw| < 40%). Control: the same
             rule with the v1 snapshot close as the candidate and v2.1 split-adjusted close as the reference reproduces the
             known v1 stale-basis jumps (TPX 2020-11-24 ...). Also the lead's rule (<= -40% / >= +67%, |dlog| >= 0.05) on
             all days 2009-12-01+ under the v1 ALL/Top1000/Top500 masks (prototype audit: 16 / 6 / 0).
  G-DIV    : at every vendor dividend-factor step, step = f_prev / f_new must equal 1 - D / P_prev with D = same-window USD
             cash dividends (types 1,4,5,6,7,8,10,15,17,18,19,23; raw units) and P_prev = previous observed raw close,
             corrected by the split-factor ratio when a split boundary lies in between. Steps with a non-cash distribution
             (types 11,20,21,22,31,32,33) in the same window are excluded; non-USD cash counted separately.
  G-ORACLE : 300 identity.parquet items: cash-form return (adj_t + D_t/sf_ex)/adj_prev - 1 vs returns_total (spin-off /
             non-cash days excluded), plus the exact reinvestment form adj_t/(adj_prev - D) - 1 and cumulative products.
  G-RAW    : miprice close & volume 2010+ vs base_data (|rel| > 1e-4 listed, categorised correction/tail/scale/other), row
             presence both ways; rows with |rel| > 1e-3 on close feed qualityFlag = 2 (prices_build.py --apply-flags).
  VALUES   : the known-value assertions (AAPL, COST, EBAY, TPX, BRO).

CLI: prices_qa.py --stage .../T2 --work .../T2_work [--gates split,jump,div,oracle,raw,values]
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
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import DB, SCHEMA, rss_gb  # noqa: E402
from prices_factors import SRC_DIV, SRC_SPLIT, FactorLookup, build_intervals, dividend_windows, split_windows  # noqa: E402

DEFAULT_STAGE = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2")
DEFAULT_WORK = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2_work")
REF_DIR = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/orch_v21/ref")
V1_SNAPSHOT = Path("/home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14")
PROTO_MASTER = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/Master/master.parquet")

CASH_TYPES = (1, 4, 5, 6, 7, 8, 10, 15, 17, 18, 19, 23)
NONCASH_TYPES = (11, 20, 21, 22, 31, 32, 33)
USD = 160
KEY_SHIFT = 20
DAY_OFFSET = 30000                      # days since 1970 + offset >= 0 for 1900+

log = logging.getLogger("cache_v21.prices_qa")
T = pd.Timestamp


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def key(ids, dates) -> np.ndarray:
    days = np.asarray(dates).astype("datetime64[D]").astype(np.int64) + DAY_OFFSET
    return (np.asarray(ids, dtype=np.int64) << KEY_SHIFT) | days


def to_native(o):
    """Recursively convert numpy / pandas scalars so json round-trips keep numbers as numbers."""
    if isinstance(o, dict):
        return {str(k): to_native(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_native(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(o).date()) if not pd.isna(o) else None
    if o is pd.NaT:
        return None
    return o


def symbols_map() -> dict:
    m = pd.read_parquet(PROTO_MASTER, columns=["tradingItemId", "symbol"])
    return dict(zip(m.tradingItemId.astype(int), m.symbol))


def md_table(rows: list[dict], cols: list[str] | None = None, fmt="{:.6g}") -> str:
    if not rows:
        return "(none)\n"
    cols = cols or list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c, "")
            if isinstance(v, float):
                v = fmt.format(v)
            elif isinstance(v, (pd.Timestamp, np.datetime64)):
                v = str(pd.Timestamp(v).date())
            cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


class Observed:
    """All base_data rows with close notna as flat arrays, plus split/dividend factors and previous-row index."""

    def __init__(self, stage: Path, split: FactorLookup, div: FactorLookup, work: Path | None = None):
        t0 = time.time()
        pf = pq.ParquetFile(stage / "base_data.parquet")
        pf_a = pq.ParquetFile(work / "adjf_long.parquet") if work is not None and (work / "adjf_long.parquet").exists() else None
        ids, dates, close, vol, c64 = [], [], [], [], []
        for rg in range(pf.num_row_groups):
            t = pf.read_row_group(rg, columns=["date", "tradingItemId", "close", "volume"])
            c = t["close"].to_numpy(zero_copy_only=False)
            keep = np.isfinite(c)
            ids.append(t["tradingItemId"].to_numpy()[keep])
            dates.append(t["date"].to_numpy().astype("datetime64[ns]")[keep])
            close.append(c[keep])
            vol.append(t["volume"].to_numpy(zero_copy_only=False)[keep])
            if pf_a is not None:      # float64 raw close = vendor close x adjustmentfactor (same row order as base_data)
                a = pf_a.read_row_group(rg, columns=["tradingItemId", "adjustmentfactor", "ciqClose"])
                if not np.array_equal(a["tradingItemId"].to_numpy(), t["tradingItemId"].to_numpy()):
                    raise RuntimeError(f"row group {rg}: adjf_long rows differ from base_data")
                c64.append((a["ciqClose"].to_numpy(zero_copy_only=False) * a["adjustmentfactor"].to_numpy(zero_copy_only=False))[keep])
        self.ids = np.concatenate(ids)
        self.dates = np.concatenate(dates)
        self.close = np.concatenate(close).astype(np.float32)
        self.close64 = np.concatenate(c64) if c64 else self.close.astype(np.float64)
        self.volume = np.concatenate(vol)
        self.keys = key(self.ids, self.dates)
        if not (np.diff(self.keys) > 0).all():
            raise RuntimeError("base_data observed rows are not strictly sorted by (tradingItemId, date)")
        self.sf = split(self.ids, self.dates)
        self.df = div(self.ids, self.dates)
        same = np.r_[False, self.ids[1:] == self.ids[:-1]]
        self.prev = np.where(same, np.arange(len(self.ids)) - 1, -1)
        self.split, self.div = split, div
        log.info("observed rows: %d (%d ids) loaded in %.0fs, rss %.1f GB", len(self.ids), len(np.unique(self.ids)),
                 time.time() - t0, rss_gb())

    def adj_split(self, precise: bool = False) -> np.ndarray:
        """Split-adjusted close; precise=True uses the float64 vendor close (identity tests), else the shipped float32."""
        return (self.close64 if precise else self.close.astype(np.float64)) / self.sf

    def last_before(self, ids, dates) -> np.ndarray:
        """Index of the last observed row of `id` strictly before `date` (-1 if none)."""
        pos = np.searchsorted(self.keys, key(ids, dates), side="left") - 1
        ok = (pos >= 0) & (self.ids[np.maximum(pos, 0)] == np.asarray(ids, dtype=np.int64))
        return np.where(ok, pos, -1)


class DividendSums:
    """Per-id prefix sums over ciqdividend rows sorted by (id, exdate): windowed sums via two searchsorted calls."""

    def __init__(self, div: pd.DataFrame, split: FactorLookup):
        d = div[div.exdate.notna()].copy()
        d["exdate"] = pd.to_datetime(d.exdate).dt.normalize()
        d["tid"] = d.tradingitemid.astype(np.int64)
        d = d.sort_values(["tid", "exdate"]).reset_index(drop=True)
        sf_all = split(d.tid.to_numpy(), d.exdate.to_numpy(), strict=False)
        self.n_dropped_no_factor = int((~np.isfinite(sf_all)).sum())     # ids outside the factor tables (outside the symbol set)
        d = d[np.isfinite(sf_all)].reset_index(drop=True)
        self.keys = key(d.tid.to_numpy(), d.exdate.to_numpy())
        amt = d.divamount.astype(float).fillna(0.0).to_numpy()
        cash = d.dividendtypeid.isin(CASH_TYPES).to_numpy()
        usd = (d.currencyid == USD).to_numpy()
        noncash = d.dividendtypeid.isin(NONCASH_TYPES).to_numpy()
        sf_ex = split(d.tid.to_numpy(), d.exdate.to_numpy(), strict=False)
        self.cum_usd = np.r_[0.0, np.cumsum(np.where(cash & usd, amt, 0.0))]                 # raw units
        self.cum_usd_adj = np.r_[0.0, np.cumsum(np.where(cash & usd, amt / sf_ex, 0.0))]     # current-basis units
        self.cum_nonusd = np.r_[0, np.cumsum((cash & ~usd).astype(np.int64))]
        self.cum_noncash = np.r_[0, np.cumsum(noncash.astype(np.int64))]
        self.cum_cash_n = np.r_[0, np.cumsum((cash & usd).astype(np.int64))]
        self.frame = d

    def window(self, ids, after_dates, upto_dates) -> dict:
        """Sums over exdate in (after, upto] for each (id)."""
        lo = np.searchsorted(self.keys, key(ids, after_dates), side="right")
        hi = np.searchsorted(self.keys, key(ids, upto_dates), side="right")
        return {"usd": self.cum_usd[hi] - self.cum_usd[lo], "usd_adj": self.cum_usd_adj[hi] - self.cum_usd_adj[lo],
                "n_nonusd": self.cum_nonusd[hi] - self.cum_nonusd[lo], "n_noncash": self.cum_noncash[hi] - self.cum_noncash[lo],
                "n_cash": self.cum_cash_n[hi] - self.cum_cash_n[lo]}


def fetch_dividends(db: DB, ids, work: Path) -> pd.DataFrame:
    out = work / "raw_dividends.parquet"
    if out.exists():
        return pd.read_parquet(out)
    t0 = time.time()
    d = db.query(f"""select dividendid, tradingitemid, exdate, divamount, currencyid, dividendtypeid, supplementaltypeid
                     from {SCHEMA}.ciqdividend where tradingitemid = any(%s)""", (list(map(int, ids)),))
    d["divamount"] = d.divamount.astype(float)
    d.to_parquet(out, index=False)
    log.info("dividends: %d rows for %d ids (%.0fs)", len(d), d.tradingitemid.nunique(), time.time() - t0)
    return d


# --------------------------------------------------------------------------------------
# G-SPLIT
# --------------------------------------------------------------------------------------
def split_mismatch(ids, dates, adjf, lookup: FactorLookup, tol=1e-6) -> tuple[int, np.ndarray]:
    sf = lookup(ids, dates)
    rel = np.abs(sf / adjf - 1.0)
    bad = rel > tol
    return int(bad.sum()), np.flatnonzero(bad)


def gate_split(stage: Path, work: Path, split_df: pd.DataFrame) -> dict:
    t0 = time.time()
    lookup = FactorLookup(split_df)
    pf_b = pq.ParquetFile(stage / "base_data.parquet")
    pf_a = pq.ParquetFile(work / "adjf_long.parquet")
    if pf_b.num_row_groups != pf_a.num_row_groups:
        raise RuntimeError("base_data and adjf_long row-group layouts differ")
    n_rows = n_bad = 0
    examples = []
    synthetic = None
    src_of_row = split_df.source.to_numpy()
    by_source = {}
    for rg in range(pf_b.num_row_groups):
        b = pf_b.read_row_group(rg, columns=["date", "tradingItemId"])
        a = pf_a.read_row_group(rg)
        ids = b["tradingItemId"].to_numpy()
        dates = b["date"].to_numpy().astype("datetime64[ns]")
        if not (np.array_equal(ids, a["tradingItemId"].to_numpy()) and np.array_equal(dates, a["date"].to_numpy().astype("datetime64[ns]"))):
            raise RuntimeError(f"row group {rg}: adjf side file rows differ from base_data")
        adjf = a["adjustmentfactor"].to_numpy(zero_copy_only=False)
        if rg == 0:
            # synthetic counterexample: corrupt one vendor interval of the first id in this group -> must be detected
            tid = int(ids[0])
            bad_df = split_df.copy()
            sel = (bad_df.tradingItemId == tid)
            i = bad_df.index[sel][-1]
            bad_df.loc[i, "factor"] = bad_df.loc[i, "factor"] * 1.5
            n_syn, _ = split_mismatch(ids, dates, adjf, FactorLookup(bad_df))
            n_rows_id = int(sel.sum())
            synthetic = {"perturbed_id": tid, "perturbed_intervals": 1, "detected_rows": n_syn, "ok": n_syn > 0}
        n, idx = split_mismatch(ids, dates, adjf, lookup)
        pos = lookup.positions(ids, dates)
        for k, v in zip(*np.unique(src_of_row[lookup.row_index[pos]], return_counts=True)):
            by_source[str(k)] = by_source.get(str(k), 0) + int(v)
        n_rows += len(ids)
        n_bad += n
        for j in idx[:5]:
            if len(examples) < 20:
                examples.append({"tradingItemId": int(ids[j]), "date": str(pd.Timestamp(dates[j]).date()),
                                 "adjustmentfactor": float(adjf[j]), "lookup": float(lookup([ids[j]], [dates[j]])[0])})
    sym = symbols_map()
    ext = split_df[(split_df.factor < 1e-3) | (split_df.factor > 100)].copy()
    ext["symbol"] = ext.tradingItemId.map(sym)
    ext_ids = set(ext.tradingItemId)
    bad_ids = {e["tradingItemId"] for e in examples}
    ext["id_passes_G-SPLIT"] = ~ext.tradingItemId.isin(bad_ids)
    ext = ext.sort_values("factor")
    res = {"gate": "G-SPLIT", "rows": n_rows, "violations": n_bad, "tolerance": 1e-6, "pass": n_bad == 0, "examples": examples,
           "rows_by_factor_source": by_source, "ids_by_factor_source": split_df.groupby("source").tradingItemId.nunique().to_dict(),
           "factor_min": float(split_df.factor.min()), "factor_max": float(split_df.factor.max()),
           "extreme_intervals": {"rule": "factor < 1e-3 or > 100", "rows": int(len(ext)), "ids": len(ext_ids),
                                 "ids_passing": int(ext.groupby("tradingItemId")["id_passes_G-SPLIT"].all().sum()),
                                 "by_source": ext.source.value_counts().to_dict(),
                                 "smallest": ext.head(15)[["tradingItemId", "symbol", "validFrom", "validTo", "factor", "source", "id_passes_G-SPLIT"]].to_dict("records"),
                                 "largest": ext.tail(15).iloc[::-1][["tradingItemId", "symbol", "validFrom", "validTo", "factor", "source", "id_passes_G-SPLIT"]].to_dict("records")},
           "synthetic": synthetic, "seconds": round(time.time() - t0, 1)}
    ext[["tradingItemId", "symbol", "validFrom", "validTo", "factor", "source", "id_passes_G-SPLIT"]].to_csv(work / "gsplit_extreme_intervals.csv", index=False)
    log.info("G-SPLIT: %d rows, %d violations (synthetic detected %s) %.0fs", n_rows, n_bad, synthetic, res["seconds"])
    return res


# --------------------------------------------------------------------------------------
# G-JUMP
# --------------------------------------------------------------------------------------
def fake_jumps(r_cand: np.ndarray, r_ref: np.ndarray, rule: str = "t2") -> np.ndarray:
    """Boolean mask of fake jumps. rule 't2': |r_cand| > 40% and |r_ref| < 40%.
    rule 'lead': r_cand <= -40% or >= +67% and |log1p(r_cand) - log1p(r_ref)| >= 0.05 (prototype audit's jump_check)."""
    ok = np.isfinite(r_cand) & np.isfinite(r_ref)
    if rule == "t2":
        return ok & (np.abs(r_cand) > 0.4) & (np.abs(r_ref) < 0.4)
    big = (r_cand <= -0.4) | (r_cand >= 0.67)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.abs(np.log1p(r_cand) - np.log1p(r_ref))
    return ok & big & (d >= 0.05)


def gate_jump(obs: Observed, split_df: pd.DataFrame, sym: dict) -> dict:
    t0 = time.time()
    has_prev = obs.prev >= 0
    i = np.flatnonzero(has_prev)
    p = obs.prev[i]
    boundary = obs.sf[i] != obs.sf[p]
    ib, pb = i[boundary], p[boundary]
    adj = obs.adj_split()
    r_adj = adj[ib] / adj[pb] - 1.0
    r_raw = obs.close[ib].astype(np.float64) / obs.close[pb] - 1.0
    fake = fake_jumps(r_adj, r_raw, "t2")
    # total return at the same boundary (dividend factor included): a consolidation paired with a spin-off (HON 2026-06-29,
    # type-20 distribution + 1:2 consolidation) shows a real -50% in the split-adjusted price but a small total return
    r_tot = (adj[ib] * obs.df[ib]) / (adj[pb] * obs.df[pb]) - 1.0
    cov = None
    if (REF_DIR / "coverage_stats.parquet").exists():
        cov = pd.read_parquet(REF_DIR / "coverage_stats.parquet", columns=["tradingItemId", "everTop500", "everTop1000"])
        if "tradingItemId" in cov.columns:
            cov = cov.set_index("tradingItemId")
        cov.index = cov.index.astype(np.int64)
    fi = np.flatnonzero(fake)
    cat = np.where(np.abs(r_tot[fi]) < 0.4, "spinoff_offset(total return continuous)", "unexplained(vendor factor artifact)")
    rows = [{"tradingItemId": int(obs.ids[ib[k]]), "symbol": sym.get(int(obs.ids[ib[k]])), "date": str(pd.Timestamp(obs.dates[ib[k]]).date()),
             "prevDate": str(pd.Timestamp(obs.dates[pb[k]]).date()), "r_adj": float(r_adj[k]), "r_raw": float(r_raw[k]), "r_total": float(r_tot[k]),
             "sf_prev": float(obs.sf[pb[k]]), "sf": float(obs.sf[ib[k]]), "div_step": float(obs.df[pb[k]] / obs.df[ib[k]]), "category": c,
             "everTop1000": bool(cov.everTop1000.get(int(obs.ids[ib[k]]), False)) if cov is not None else None,
             "everTop500": bool(cov.everTop500.get(int(obs.ids[ib[k]]), False)) if cov is not None else None}
            for k, c in zip(fi, cat)]
    fake_df = pd.DataFrame(rows)
    if len(fake_df):
        fake_df.to_csv(REF_DIR.parent.parent / "stage_v21" / "T2_work" / "gjump_fake_jumps.csv", index=False)
    by_cat = fake_df.category.value_counts().to_dict() if len(fake_df) else {}
    by_univ = {"everTop500": int(fake_df.everTop500.sum()) if len(fake_df) else 0, "everTop1000": int(fake_df.everTop1000.sum()) if len(fake_df) else 0,
               "unexplained_everTop1000": int(((fake_df.category.str.startswith("unexplained")) & fake_df.everTop1000).sum()) if len(fake_df) else 0}
    rows = sorted(rows, key=lambda r: (r["category"], -abs(r["r_adj"])))[:40]
    # largest adjusted moves at boundaries (informational)
    order = np.argsort(-np.abs(r_adj))[:10]
    biggest = [{"tradingItemId": int(obs.ids[ib[k]]), "symbol": sym.get(int(obs.ids[ib[k]])), "date": str(pd.Timestamp(obs.dates[ib[k]]).date()),
                "r_adj": float(r_adj[k]), "r_raw": float(r_raw[k]), "sf_prev": float(obs.sf[pb[k]]), "sf": float(obs.sf[ib[k]])} for k in order]
    # synthetic counterexample: move AAPL's 2014-06-09 boundary one trading day later -> fake +600% on 06-10
    aapl = 2590360
    bad_df = split_df.copy()
    sel = (bad_df.tradingItemId == aapl) & (bad_df.validFrom == T("2014-06-09"))
    syn = None
    if sel.any():
        j = bad_df.index[sel][0]
        bad_df.loc[j, "validFrom"] = T("2014-06-10")
        bad_df.loc[j - 1, "validTo"] = T("2014-06-10")
        lk = FactorLookup(bad_df)
        m = obs.ids == aapl
        ia = np.flatnonzero(m)
        sfa = lk(obs.ids[ia], obs.dates[ia])
        adja = obs.close[ia].astype(np.float64) / sfa
        ra = adja[1:] / adja[:-1] - 1
        rr = obs.close[ia][1:].astype(np.float64) / obs.close[ia][:-1] - 1
        bnd = sfa[1:] != sfa[:-1]
        f = fake_jumps(ra, rr, "t2") & bnd
        syn = {"perturbation": "AAPL split boundary 2014-06-09 -> 2014-06-10", "detected": int(f.sum()),
               "detected_dates": [str(pd.Timestamp(obs.dates[ia][1:][k]).date()) for k in np.flatnonzero(f)][:5], "ok": bool(f.sum() > 0)}
    res = {"gate": "G-JUMP", "boundaries": int(len(ib)), "fake_jumps": int(fake.sum()), "pass": bool(fake.sum() == 0),
           "fake_by_category": by_cat, "fake_by_universe": by_univ,
           "raw_jumps_over_40pct_at_boundaries": int((np.abs(r_raw) > 0.4).sum()), "max_abs_r_adj_at_boundary": float(np.abs(r_adj).max()),
           "examples": rows, "biggest_adjusted_moves_at_boundaries": biggest, "synthetic": syn, "seconds": round(time.time() - t0, 1)}
    log.info("G-JUMP: %d boundaries, %d fake jumps, synthetic %s (%.0fs)", len(ib), fake.sum(), syn, res["seconds"])
    return res


def gate_jump_v1_control(obs: Observed, sym: dict, since: str = "2009-12-01") -> dict:
    """Candidate = v1 snapshot close (stale-basis rows), reference = v2.1 split-adjusted close."""
    t0 = time.time()
    p_close = V1_SNAPSHOT / "BaseData" / "close.parquet"
    if not p_close.exists():
        return {"gate": "G-JUMP v1 control", "skipped": "v1 snapshot not found"}
    def load_wide(path: Path, since_ts: pd.Timestamp, dtype):
        """v1 wide parquet (columns = id strings, __index_level_0__ = dates) -> (dates, ids, 2-D array) rows >= since."""
        tbl = pq.read_table(path)
        idx_col = [n for n in tbl.schema.names if n.startswith("__index")]
        names = [n for n in tbl.schema.names if not n.startswith("__index")]
        d = pd.DatetimeIndex(pd.to_datetime(tbl.column(idx_col[0]).to_numpy()))
        first = int(np.searchsorted(d.values, np.datetime64(since_ts, "ns")))
        tbl = tbl.slice(first)
        arr = np.empty((tbl.num_rows, len(names)), dtype=dtype)
        for j, n in enumerate(names):
            arr[:, j] = tbl.column(n).to_numpy(zero_copy_only=False)
        return d[first:], np.array([int(n) for n in names], dtype=np.int64), arr

    since_ts = T(since) - pd.Timedelta(days=10)          # a few rows earlier so the 1-day mask shift has a previous row
    dates_v1, v1_ids, arr = load_wide(p_close, since_ts, np.float64)
    # masks (v1 universe, shifted one day like the lead's script: membership of the previous day)
    masks = {}
    for name in ("ALL", "Top1000", "Top500"):
        pm = V1_SNAPSHOT / "TopUniverseV1" / f"{name}.parquet"
        if pm.exists():
            md, mids, marr = load_wide(pm, since_ts, bool)
            shifted = np.vstack([np.zeros((1, marr.shape[1]), bool), marr[:-1]])
            mdf = pd.DataFrame(shifted, index=md, columns=mids)
            masks[name] = mdf.reindex(index=dates_v1, columns=v1_ids, fill_value=False).to_numpy(dtype=bool)
            del marr, shifted, mdf
    # our observed rows for v1 ids since `since` with a previous row also since `since`
    col_of = pd.Index(v1_ids)
    row_of = pd.Index(dates_v1)
    sel = np.flatnonzero((obs.prev >= 0) & (obs.dates >= np.datetime64(since, "ns")) & np.isin(obs.ids, v1_ids))
    prv = obs.prev[sel]
    c = col_of.get_indexer(obs.ids[sel])
    r_t = row_of.get_indexer(obs.dates[sel])
    r_p = row_of.get_indexer(obs.dates[prv])
    ok = (r_t >= 0) & (r_p >= 0)
    sel, prv, c, r_t, r_p = sel[ok], prv[ok], c[ok], r_t[ok], r_p[ok]
    v1_t, v1_p = arr[r_t, c], arr[r_p, c]
    r_v1 = v1_t / v1_p - 1.0
    adj = obs.adj_split()
    r_ref = adj[sel] / adj[prv] - 1.0
    r_raw = obs.close[sel].astype(np.float64) / obs.close[prv] - 1.0
    boundary = obs.sf[sel] != obs.sf[prv]
    out = {"gate": "G-JUMP v1 control", "since": since, "cells_compared": int(len(sel)), "v1_ids": int(len(v1_ids)),
           "rules": {}}
    def listing(mask):
        idx = np.flatnonzero(mask)
        idx = idx[np.argsort(-np.abs(r_v1[idx]))][:40]
        return [{"tradingItemId": int(obs.ids[sel[k]]), "symbol": sym.get(int(obs.ids[sel[k]])), "date": str(pd.Timestamp(obs.dates[sel[k]]).date()),
                 "v1_prev": float(v1_p[k]), "v1": float(v1_t[k]), "r_v1": float(r_v1[k]), "r_v21_adj": float(r_ref[k]), "r_raw": float(r_raw[k])} for k in idx]
    f_t2_b = fake_jumps(r_v1, r_ref, "t2") & boundary
    f_t2_all = fake_jumps(r_v1, r_ref, "t2")
    out["rules"]["t2_rule_at_boundaries (|r_v1|>40% & |r_v21_adj|<40%)"] = {"count": int(f_t2_b.sum()), "examples": listing(f_t2_b)}
    out["rules"]["t2_rule_all_days"] = {"count": int(f_t2_all.sum()), "examples": listing(f_t2_all)[:20]}
    f_lead = fake_jumps(r_v1, r_ref, "lead")
    big = (r_v1 <= -0.4) | (r_v1 >= 0.67)
    lead = {"unmasked": {"big_moves": int(big.sum()), "artifacts": int(f_lead.sum())}}
    for name, m in masks.items():
        mm = m[r_t, c]
        lead[name] = {"big_moves": int((big & mm).sum()), "artifacts": int((f_lead & mm).sum()),
                      "up": int((f_lead & mm & (r_v1 > 0)).sum()), "down": int((f_lead & mm & (r_v1 < 0)).sum()),
                      "examples": listing(f_lead & mm)[:20]}
    out["rules"]["lead_rule (<=-40%|>=+67%, |dlog|>=0.05), masks shifted 1 day"] = lead
    k_tpx = np.flatnonzero((obs.ids[sel] == 8214712) & (obs.dates[sel] == np.datetime64("2020-11-24", "ns")))
    out["tpx_2020_11_24_detected"] = bool(len(k_tpx) and f_t2_all[k_tpx[0]])
    if len(k_tpx):
        k = k_tpx[0]
        out["tpx_2020_11_24_row"] = {"v1_prev": float(v1_p[k]), "v1": float(v1_t[k]), "r_v1": float(r_v1[k]), "r_v21_adj": float(r_ref[k]),
                                     "r_raw": float(r_raw[k]), "boundary": bool(boundary[k]), "flagged_t2": bool(f_t2_all[k]), "flagged_lead": bool(f_lead[k])}
    out["seconds"] = round(time.time() - t0, 1)
    log.info("G-JUMP v1 control: %s cells, t2@boundaries %d, t2 all %d, lead unmasked %d (%.0fs)", len(sel), f_t2_b.sum(), f_t2_all.sum(),
             f_lead.sum(), out["seconds"])
    return out


# --------------------------------------------------------------------------------------
# G-DIV
# --------------------------------------------------------------------------------------
def div_step_identity(step_ids, step_dates, f_prev, f_new, obs: Observed, dsum: DividendSums, split: FactorLookup) -> pd.DataFrame:
    """Per step: step = f_prev/f_new vs 1 - D/P_prev (split-corrected). Returns a frame with all pieces."""
    pos = obs.last_before(step_ids, step_dates)
    have = pos >= 0
    d_prev = np.where(have, obs.dates[np.maximum(pos, 0)], np.datetime64("NaT", "ns"))
    p_prev = np.where(have, obs.close64[np.maximum(pos, 0)], np.nan)
    sf_prev = np.where(have, obs.sf[np.maximum(pos, 0)], np.nan)
    sf_x = split(step_ids, step_dates)
    p_prev_adj = p_prev * sf_x / sf_prev
    after = np.where(have, d_prev, np.datetime64("1899-12-31", "ns"))
    w = dsum.window(step_ids, after, step_dates)
    step = np.asarray(f_prev, dtype=float) / np.asarray(f_new, dtype=float)
    implied = 1.0 - w["usd"] / p_prev_adj                 # as specified: P_prev split-corrected to the ex-date basis
    implied_raw = 1.0 - w["usd"] / p_prev                 # alternative: D and P_prev both per old share (no correction)
    return pd.DataFrame({"tradingItemId": step_ids, "date": step_dates, "f_prev": f_prev, "f_new": f_new, "step": step,
                         "prevDate": d_prev, "P_prev": p_prev, "sf_prev": sf_prev, "sf_x": sf_x, "P_prev_adj": p_prev_adj,
                         "D_usd": w["usd"], "n_cash": w["n_cash"], "n_nonusd": w["n_nonusd"], "n_noncash": w["n_noncash"],
                         "implied": implied, "diff": step - implied, "diff_uncorrected": step - implied_raw, "has_prev": have,
                         "split_between": have & (sf_x != sf_prev),
                         "f_decimals": np.maximum(decimals(np.asarray(f_prev, dtype=float)), decimals(np.asarray(f_new, dtype=float)))})


def decimals(x: np.ndarray, max_d: int = 10) -> np.ndarray:
    """Smallest d such that round(x, d) == x (1e-12): how many decimals the vendor stored (5 for many old items, 10 for others)."""
    out = np.full(len(x), max_d, dtype=np.int64)
    done = np.zeros(len(x), dtype=bool)
    for d in range(max_d + 1):
        hit = ~done & (np.abs(np.round(x, d) - x) < 1e-12)
        out[hit] = d
        done |= hit
    return out


def gate_div(obs: Observed, raw_div: pd.DataFrame, dsum: DividendSums, split: FactorLookup, sym: dict, work: Path,
             tol: float = 1e-6) -> dict:
    t0 = time.time()
    d = raw_div.copy()
    d["fromdate"] = pd.to_datetime(d.fromdate).dt.normalize()
    d["todate"] = pd.to_datetime(d.todate).dt.normalize()
    d["divadjfactor"] = d.divadjfactor.astype(float)
    d = d.sort_values(["tradingitemid", "fromdate"]).reset_index(drop=True)
    same = d.tradingitemid.to_numpy()[1:] == d.tradingitemid.to_numpy()[:-1]
    k = np.flatnonzero(same)                       # step from row k to k+1
    ids = d.tradingitemid.to_numpy(np.int64)[k + 1]
    x = d.fromdate.to_numpy()[k + 1].astype("datetime64[ns]")
    contiguous = (d.todate.to_numpy()[k] + np.timedelta64(1, "D")) == d.fromdate.to_numpy()[k + 1]
    f_prev, f_new = d.divadjfactor.to_numpy()[k], d.divadjfactor.to_numpy()[k + 1]
    in_range = x >= np.datetime64("2000-01-03", "ns")
    r = div_step_identity(ids[in_range], x[in_range], f_prev[in_range], f_new[in_range], obs, dsum, split)
    r["contiguous"] = contiguous[in_range]
    r["symbol"] = r.tradingItemId.map(sym)
    r["absdiff"] = r["diff"].abs()
    base = r[r.has_prev]
    clean = base[(base.n_noncash == 0) & (base.n_nonusd == 0) & base.contiguous]
    matched = clean[clean.absdiff <= tol]
    mism = clean[clean.absdiff > tol].sort_values("absdiff", ascending=False)
    # vendor factors stored with <= 5 decimals cannot satisfy a 1e-6 identity: |step error| <= ~1e-5 / f ; separate them out
    low_prec = mism.f_decimals <= 5
    rounding = low_prec & (mism.absdiff <= 2e-5)
    uncorrected_ok = mism.split_between & (mism.diff_uncorrected.abs() <= tol)   # vendor: D and P_prev per old share
    explained = mism[rounding]
    genuine = mism[~rounding & ~uncorrected_ok]
    mism.to_csv(work / "gdiv_mismatches.csv", index=False)
    genuine.to_csv(work / "gdiv_mismatches_genuine.csv", index=False)
    sb = clean[clean.split_between]
    by_kind = {
        "steps_total": int(len(k)), "steps_2000plus": int(in_range.sum()), "steps_with_prev_close": int(len(base)),
        "steps_no_prev_close_in_base_data": int((~r.has_prev).sum()),
        "excluded_noncash_same_window": int((base.n_noncash > 0).sum()),
        "excluded_nonusd_cash_same_window": int(((base.n_noncash == 0) & (base.n_nonusd > 0)).sum()),
        "excluded_gap_steps": int(((base.n_noncash == 0) & (base.n_nonusd == 0) & ~base.contiguous).sum()),
        "tested": int(len(clean)), "matched_within_1e-6": int(len(matched)), "mismatched": int(len(mism)),
        "mismatched_over_1e-4": int((clean.absdiff > 1e-4).sum()), "mismatched_over_1e-3": int((clean.absdiff > 1e-3).sum()),
        "mismatched_explained_by_5dec_factor_rounding": int(len(explained)),
        "mismatched_explained_by_uncorrected_split_form": int(uncorrected_ok.sum()),
        "mismatched_genuine": int(len(genuine)), "mismatched_genuine_over_1e-4": int((genuine.absdiff > 1e-4).sum()),
        "mismatched_genuine_ids": int(genuine.tradingItemId.nunique()),
        "tested_factor_decimals_le5": int((clean.f_decimals <= 5).sum()),
        "tested_with_split_between": int(len(sb)),
        "split_between_matched_corrected(spec)": int((sb.absdiff <= tol).sum()),
        "split_between_matched_uncorrected(raw/raw)": int((sb.diff_uncorrected.abs() <= tol).sum()),
        "split_between_matched_either": int(((sb.absdiff <= tol) | (sb.diff_uncorrected.abs() <= tol)).sum()),
        "steps_without_any_cash_dividend": int((clean.n_cash == 0).sum()),
        "median_absdiff": float(clean.absdiff.median()), "p99_absdiff": float(clean.absdiff.quantile(0.99)),
    }
    # non-USD steps: own tally (the identity needs FX, not tested here)
    nonusd = base[(base.n_noncash == 0) & (base.n_nonusd > 0)]
    # synthetic counterexamples on a fake id 999999999 using the real machinery: (a) correct D with a split in between passes
    # only when split-corrected, (b) wrong D fails
    syn = synthetic_div_check(split)
    cols = ["tradingItemId", "symbol", "date", "prevDate", "step", "implied", "diff", "diff_uncorrected", "D_usd", "P_prev", "sf_prev", "sf_x", "n_cash", "f_decimals"]
    res = {"gate": "G-DIV", "tolerance": tol, "pass": by_kind["mismatched_genuine"] == 0, **by_kind,
           "mismatch_examples": genuine[cols].head(40).to_dict("records"), "n_ids_mismatched": int(mism.tradingItemId.nunique()),
           "mismatch_ids_top": genuine.groupby(["tradingItemId", "symbol"], dropna=False).size().sort_values(ascending=False).head(15).reset_index().rename(columns={0: "n"}).to_dict("records"),
           "split_between_examples": sb[cols].head(15).to_dict("records"),
           "nonusd_steps": int(len(nonusd)), "nonusd_ids": int(nonusd.tradingItemId.nunique()),
           "synthetic": syn, "seconds": round(time.time() - t0, 1)}
    log.info("G-DIV: tested %d, matched %d, mismatched %d (synthetic %s) %.0fs", len(clean), len(matched), len(mism), syn, res["seconds"])
    return res


def synthetic_div_check(split_real: FactorLookup) -> dict:
    """Tiny fixture: prices 100 (d-2), split 2:1 at d-1 -> 51, dividend 1.0 ex d -> factor step must be 1 - 1/51."""
    class Obs:  # minimal duck-typed Observed for div_step_identity
        pass
    fake = 999999999
    dates = pd.to_datetime(["2020-01-02", "2020-01-06"]).to_numpy().astype("datetime64[ns]")   # d-2 (pre-split), d-1 missing
    sc = pd.DataFrame({"tradingitemid": [fake], "splitdate": ["2020-01-03"], "nextsplitdate": ["2079-06-05"],
                       "cumulativesplitfactor": [1.0], "latestsplitfactor": [2.0]})
    split = FactorLookup(build_intervals(split_windows(sc), [fake], SRC_SPLIT, T("2026-09-16"), pre_first="pre"))
    o = Obs()
    o.ids = np.array([fake, fake], dtype=np.int64)
    o.dates = dates
    o.close = np.array([100.0, 52.0], dtype=np.float32)
    o.close64 = o.close.astype(np.float64)
    o.keys = key(o.ids, o.dates)
    o.sf = split(o.ids, o.dates)
    o.last_before = lambda ids, ds, _o=o: Observed.last_before(_o, ids, ds)
    step_date = np.array([np.datetime64("2020-01-03", "ns")])       # ex-date = split date, P_prev = 100 on 01-02 (sf 2 -> 1)
    good = pd.DataFrame({"tradingitemid": [fake], "exdate": ["2020-01-03"], "divamount": [1.0], "currencyid": [USD], "dividendtypeid": [1]})
    r_good = div_step_identity(np.array([fake]), step_date, np.array([1 - 1.0 / 50.0]), np.array([1.0]), o, DividendSums(good, split), split)
    bad = good.assign(divamount=[1.5])
    r_bad = div_step_identity(np.array([fake]), step_date, np.array([1 - 1.0 / 50.0]), np.array([1.0]), o, DividendSums(bad, split), split)
    # without the split correction the identity would use P_prev = 100 -> implied 0.99 vs step 0.98: mismatch
    naive = 1 - 1.0 / 100.0
    return {"fixture": "P_prev 100 (sf 2) -> split 2:1 -> ex-date D=1.0, step 1-1/50",
            "correct_D_split_corrected_diff": float(r_good["diff"][0]), "correct_passes": bool(abs(r_good["diff"][0]) < 1e-9),
            "wrong_D_diff": float(r_bad["diff"][0]), "wrong_D_detected": bool(abs(r_bad["diff"][0]) > 1e-6),
            "naive_uncorrected_would_fail": bool(abs((1 - 1.0 / 50.0) - naive) > 1e-6),
            "ok": bool(abs(r_good["diff"][0]) < 1e-9 and abs(r_bad["diff"][0]) > 1e-6)}


# --------------------------------------------------------------------------------------
# G-ORACLE
# --------------------------------------------------------------------------------------
def gate_oracle(obs: Observed, stage: Path, dsum: DividendSums, sym: dict, work: Path, tol: float = 1e-4) -> dict:
    t0 = time.time()
    ids300 = sorted(set(pd.read_parquet(REF_DIR / "identity.parquet", columns=["tradingitemid"]).tradingitemid.astype(int)))
    sel = np.flatnonzero(np.isin(obs.ids, ids300) & (obs.prev >= 0))
    prv = obs.prev[sel]
    adj = obs.adj_split()
    w = dsum.window(obs.ids[sel], obs.dates[prv], obs.dates[sel])
    r_cash = (adj[sel] + w["usd_adj"]) / adj[prv] - 1.0
    r_reinv = adj[sel] / (adj[prv] - w["usd_adj"]) - 1.0
    rt = pq.read_table(stage / "derived" / "returns_total.parquet", filters=[("tradingItemId", "in", ids300)]).to_pandas()
    rt_keys = key(rt.tradingItemId.to_numpy(), rt.date.to_numpy())
    pos = np.searchsorted(rt_keys, obs.keys[sel])
    if not np.array_equal(rt_keys[pos], obs.keys[sel]):
        raise RuntimeError("returns_total rows do not match observed rows for the 300 ids")
    ret = rt.ret.to_numpy()[pos]
    prev_ok = np.array_equal(rt.prevDate.to_numpy()[pos].astype("datetime64[ns]"), obs.dates[prv])
    excl = (w["n_noncash"] > 0) | (w["n_nonusd"] > 0)
    d_cash = np.abs(ret - r_cash)
    d_reinv = np.abs(ret - r_reinv)
    keep = ~excl & np.isfinite(d_cash)
    frame = pd.DataFrame({"tradingItemId": obs.ids[sel], "date": obs.dates[sel], "ret": ret, "r_cash": r_cash, "r_reinv": r_reinv,
                          "D_adj": w["usd_adj"], "DP": w["usd_adj"] / adj[prv], "d_cash": d_cash, "d_reinv": d_reinv, "excluded": excl})
    frame["symbol"] = frame.tradingItemId.map(sym)
    kept = frame[keep]
    top = kept.sort_values("d_cash", ascending=False).head(25)
    top_reinv = kept.sort_values("d_reinv", ascending=False).head(15)
    # cumulative products per id (all rows, and excluding nothing) - cash vs factor
    g = frame.groupby("tradingItemId")
    cum = pd.DataFrame({"cum_factor": g.ret.apply(lambda s: float(np.prod(1 + s.to_numpy()))),
                        "cum_cash": g.r_cash.apply(lambda s: float(np.prod(1 + s.to_numpy()))),
                        "n_excluded_days": g.excluded.sum(), "n": g.size()})
    cum["log_ratio"] = np.log(cum.cum_factor / cum.cum_cash)
    cum["symbol"] = cum.index.map(sym)
    no_excl = cum[cum.n_excluded_days == 0]
    cum.to_csv(work / "goracle_cumulative.csv")
    top.to_csv(work / "goracle_top_diffs.csv", index=False)
    # synthetic: perturb one return by +2e-4 -> must be detected (> tol)
    syn_ret = ret.copy()
    j = np.flatnonzero(keep)[len(np.flatnonzero(keep)) // 2]
    syn_ret[j] += 2e-4
    syn_detected = int((np.abs(syn_ret - r_cash)[keep] > tol).sum()) - int((d_cash[keep] > tol).sum())
    res = {"gate": "G-ORACLE", "ids": len(ids300), "rows": int(len(sel)), "rows_excluded_noncash_or_nonusd": int(excl.sum()),
           "rows_tested": int(keep.sum()), "prevDate_matches_returns_total": bool(prev_ok),
           "cash_form": {"max_abs_diff": float(kept.d_cash.max()), "n_over_1e-4": int((kept.d_cash > 1e-4).sum()),
                         "n_over_1e-5": int((kept.d_cash > 1e-5).sum()), "n_over_1e-3": int((kept.d_cash > 1e-3).sum()),
                         "n_over_1e-4_with_DP_below_2pct": int(((kept.d_cash > 1e-4) & (kept.DP < 0.02)).sum()),
                         "median_abs_diff_on_dividend_days": float(kept[kept.D_adj > 0].d_cash.median()) if (kept.D_adj > 0).any() else None},
           "reinvestment_form": {"max_abs_diff": float(kept.d_reinv.max()), "n_over_1e-6": int((kept.d_reinv > 1e-6).sum()),
                                 "n_over_1e-4": int((kept.d_reinv > 1e-4).sum()), "p99.9": float(kept.d_reinv.quantile(0.999))},
           "pass_cash_1e-4": bool(kept.d_cash.max() < tol),
           "top_cash_diffs": top[["tradingItemId", "symbol", "date", "ret", "r_cash", "D_adj", "DP", "d_cash", "d_reinv"]].to_dict("records"),
           "top_reinv_diffs": top_reinv[["tradingItemId", "symbol", "date", "ret", "r_reinv", "D_adj", "d_reinv"]].to_dict("records"),
           "cumulative": {"ids_without_excluded_days": int(len(no_excl)),
                          "ids_cum_within_1e-3_log": int((no_excl.log_ratio.abs() < 1e-3).sum()),
                          "ids_cum_within_1e-2_log": int((no_excl.log_ratio.abs() < 1e-2).sum()),
                          "max_abs_log_ratio_no_excl": float(no_excl.log_ratio.abs().max()),
                          "examples": cum.reset_index().sort_values("log_ratio", key=np.abs, ascending=False).head(10)[["tradingItemId", "symbol", "cum_factor", "cum_cash", "log_ratio", "n_excluded_days"]].to_dict("records"),
                          "known": {s: cum.loc[i, ["cum_factor", "cum_cash", "log_ratio", "n_excluded_days"]].to_dict() for s, i in (("AAPL", 2590360), ("COST", 2603626), ("EBAY", 2608344)) if i in cum.index}},
           "synthetic": {"perturbation": "+2e-4 on one tested return", "extra_detected": syn_detected, "ok": syn_detected == 1},
           "seconds": round(time.time() - t0, 1)}
    log.info("G-ORACLE: %d rows, cash max diff %.2e (n>1e-4: %d), reinvest max %.2e (%.0fs)", keep.sum(), res["cash_form"]["max_abs_diff"],
             res["cash_form"]["n_over_1e-4"], res["reinvestment_form"]["max_abs_diff"], res["seconds"])
    return res


# --------------------------------------------------------------------------------------
# G-RAW  (miprice vs base_data, 2010+)
# --------------------------------------------------------------------------------------
def categorize_runs(mm: pd.DataFrame, last_common_key: dict) -> pd.Series:
    """Per (id, field): runs of consecutive mismatching common rows. run reaching the id's last common row -> 'tail';
    run length <= 3 -> 'correction'; longer with a constant ciq/mi ratio -> 'scale'; else 'other'. Vectorised."""
    g = mm.sort_values(["tradingItemId", "field", "ordinal"])
    tid = g.tradingItemId.to_numpy()
    fld = g.field.to_numpy()
    ordn = g.ordinal.to_numpy()
    new_run = np.r_[True, (tid[1:] != tid[:-1]) | (fld[1:] != fld[:-1]) | (np.diff(ordn) > 1)]
    run = np.cumsum(new_run)
    df = pd.DataFrame({"run": run, "tid": tid, "ord": ordn}, index=g.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(g.ciq_raw.to_numpy(dtype=float) / g.mi.to_numpy(dtype=float))
    df["lr_bad"] = ~np.isfinite(lr)
    df["lr"] = np.where(df.lr_bad, np.nan, lr)
    grp = df.groupby("run")
    size = grp.ord.transform("size")
    max_ord = grp.ord.transform("max")
    last = pd.Series(tid).map(last_common_key).to_numpy()
    lr_std = grp.lr.transform("std").fillna(0.0)
    any_bad = grp.lr_bad.transform("max").to_numpy().astype(bool)
    cat = np.where(max_ord.to_numpy() >= last, "tail",
                   np.where(size.to_numpy() <= 3, "correction",
                            np.where((lr_std.to_numpy() < 1e-4) & ~any_bad, "scale", "other")))
    return pd.Series(cat, index=g.index).reindex(mm.index)


def gate_raw(stage: Path, work: Path, asof: str, sym: dict, tol: float = 1e-4, flag_tol: float = 1e-3) -> dict:
    t0 = time.time()
    symdf = pd.read_parquet(work / "symbols.parquet")
    set_ids = np.sort(symdf.loc[symdf.inSymbolSet, "tradingItemId"].astype(np.int64).to_numpy()) if "inSymbolSet" in symdf.columns else None
    pf_b = pq.ParquetFile(stage / "base_data.parquet")
    pf_a = pq.ParquetFile(work / "adjf_long.parquet") if (work / "adjf_long.parquet").exists() else None
    mi_paths = sorted((work / "mi_chunks").glob("chunk_*.parquet"))
    if len(mi_paths) != pf_b.num_row_groups:
        raise RuntimeError(f"mi chunks ({len(mi_paths)}) != base_data row groups ({pf_b.num_row_groups})")
    since = np.datetime64("2010-01-01", "ns")
    asof_ns = np.datetime64(asof, "ns")
    parts = []
    tot = {"ciq_rows_2010plus": 0, "ciq_rows_2010plus_with_close": 0, "mi_rows_2010plus_upto_asof": 0, "mi_rows_with_close": 0,
           "common_rows": 0, "common_both_close": 0, "common_both_volume": 0,
           "ciq_rows_not_in_mi": 0, "ciq_close_rows_not_in_mi": 0, "ciq_close_rows_not_in_mi_close": 0,
           "mi_rows_not_in_ciq": 0, "mi_close_rows_not_in_ciq": 0,
           "close_mismatch": 0, "close_mismatch_over_1e-3": 0, "close_mismatch_over_1e-2": 0, "volume_mismatch": 0,
           "volume_mismatch_over_1e-3": 0, "volume_mi_zero_ciq_nonzero": 0, "ids_common": 0}
    ids_common = set()
    last_common = {}
    for rg in range(pf_b.num_row_groups):
        b = pf_b.read_row_group(rg, columns=["date", "tradingItemId", "close", "volume"]).to_pandas()
        if pf_a is not None:
            b["adjf"] = pf_a.read_row_group(rg, columns=["adjustmentfactor"])["adjustmentfactor"].to_numpy(zero_copy_only=False)
        else:
            b["adjf"] = np.nan
        b = b[b.date >= since]
        m = pq.read_table(mi_paths[rg]).to_pandas()
        m["date"] = pd.to_datetime(m.pricedate).astype("datetime64[ns]")
        m = m[(m.date >= since) & (m.date <= asof_ns)]
        m = m.rename(columns={"tradingitemid": "tradingItemId", "priceclose": "mi_close", "volume": "mi_volume"})[["tradingItemId", "date", "mi_close", "mi_volume"]]
        if set_ids is not None:
            m = m[np.isin(m.tradingItemId.to_numpy(), set_ids)]
        if len(b) and len(m):
            if b.tradingItemId.min() > m.tradingItemId.max() or m.tradingItemId.min() > b.tradingItemId.max():
                raise RuntimeError(f"row group {rg}: base_data and mi chunk id ranges do not overlap")
        tot["ciq_rows_2010plus"] += len(b)
        tot["ciq_rows_2010plus_with_close"] += int(b.close.notna().sum())
        tot["mi_rows_2010plus_upto_asof"] += len(m)
        tot["mi_rows_with_close"] += int(m.mi_close.notna().sum())
        j = b.merge(m, on=["tradingItemId", "date"], how="outer", indicator=True)
        both = j[j._merge == "both"].copy()
        left = j[j._merge == "left_only"]
        right = j[j._merge == "right_only"]
        tot["common_rows"] += len(both)
        tot["ciq_rows_not_in_mi"] += len(left)
        tot["ciq_close_rows_not_in_mi"] += int(left.close.notna().sum())
        tot["ciq_close_rows_not_in_mi_close"] += int(left.close.notna().sum() + (both.close.notna() & both.mi_close.isna()).sum())
        tot["mi_rows_not_in_ciq"] += len(right)
        tot["mi_close_rows_not_in_ciq"] += int(right.mi_close.notna().sum())
        both = both.sort_values(["tradingItemId", "date"])
        both["ordinal"] = both.groupby("tradingItemId").cumcount()
        lc = both.groupby("tradingItemId").ordinal.max()
        last_common.update(lc.to_dict())
        ids_common.update(lc.index.tolist())
        cc = both.close.notna() & both.mi_close.notna()
        tot["common_both_close"] += int(cc.sum())
        rel_c = both.close.astype(np.float64) / both.mi_close.astype(np.float64) - 1.0
        bad_c = cc & (rel_c.abs() > tol)
        tot["close_mismatch"] += int(bad_c.sum())
        tot["close_mismatch_over_1e-3"] += int((cc & (rel_c.abs() > 1e-3)).sum())
        tot["close_mismatch_over_1e-2"] += int((cc & (rel_c.abs() > 1e-2)).sum())
        vv = both.volume.notna() & both.mi_volume.notna()
        tot["common_both_volume"] += int(vv.sum())
        with np.errstate(divide="ignore", invalid="ignore"):
            rel_v = np.where(both.mi_volume.to_numpy() != 0, both.volume.to_numpy() / both.mi_volume.to_numpy() - 1.0,
                             np.where(both.volume.to_numpy() != 0, np.inf, 0.0))
        rel_v = pd.Series(rel_v, index=both.index)
        bad_v = vv & (rel_v.abs() > tol)
        tot["volume_mismatch"] += int(bad_v.sum())
        tot["volume_mismatch_over_1e-3"] += int((vv & (rel_v.abs() > 1e-3)).sum())
        tot["volume_mi_zero_ciq_nonzero"] += int((vv & np.isinf(rel_v)).sum())
        pc_ = both[bad_c][["tradingItemId", "date", "ordinal", "adjf"]].assign(field="close", ciq_raw=both.close[bad_c].astype(np.float64), mi=both.mi_close[bad_c].astype(np.float64), rel=rel_c[bad_c])
        pv_ = both[bad_v][["tradingItemId", "date", "ordinal", "adjf"]].assign(field="volume", ciq_raw=both.volume[bad_v], mi=both.mi_volume[bad_v], rel=rel_v[bad_v])
        tot["volume_rows_adjf_below_1"] = tot.get("volume_rows_adjf_below_1", 0) + int((vv & (both.adjf < 1)).sum())
        parts.append(pd.concat([pc_, pv_], ignore_index=True))
        if (rg + 1) % 10 == 0:
            log.info("G-RAW: %d/%d groups, close mismatches so far %d (rss %.1f GB)", rg + 1, pf_b.num_row_groups, tot["close_mismatch"], rss_gb())
    tot["ids_common"] = len(ids_common)
    mm = pd.concat(parts, ignore_index=True)
    mm["category"] = categorize_runs(mm, last_common) if len(mm) else pd.Series(dtype=object)
    mm["year"] = pd.to_datetime(mm.date).dt.year
    mm["symbol"] = mm.tradingItemId.map(sym)
    mm = mm.sort_values(["field", "tradingItemId", "date"]).reset_index(drop=True)
    out_dir = stage / "qa" / "price_differences"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "mi_close_diff_2010plus.parquet"
    mm_out = mm[["date", "tradingItemId", "symbol", "field", "ciq_raw", "mi", "rel", "category", "year", "adjf"]].copy()
    mm_out["date"] = pd.to_datetime(mm_out.date).astype("datetime64[ns]")
    mm_out["tradingItemId"] = mm_out.tradingItemId.astype(np.int64)
    mm_out.to_parquet(out, index=False, compression="zstd")
    # synthetic: the comparison must flag a deliberately perturbed row (tested on the machinery with a tiny frame)
    syn_b = pd.DataFrame({"tradingItemId": [1, 1], "date": pd.to_datetime(["2020-01-02", "2020-01-03"]), "close": [10.0, 10.02], "volume": [100.0, 100.0]})
    syn_m = pd.DataFrame({"tradingItemId": [1, 1], "date": pd.to_datetime(["2020-01-02", "2020-01-03"]), "mi_close": [10.0, 10.0], "mi_volume": [100.0, 101.0]})
    sj = syn_b.merge(syn_m, on=["tradingItemId", "date"])
    syn = {"close_rel": float((sj.close / sj.mi_close - 1).abs().max()), "volume_rel": float((sj.volume / sj.mi_volume - 1).abs().max())}
    syn["ok"] = bool(syn["close_rel"] > tol and syn["volume_rel"] > tol)
    close_mm = mm[mm.field == "close"]
    vol_mm = mm[mm.field == "volume"]
    res = {"gate": "G-RAW", "tolerance": tol, "flag_tolerance": flag_tol, **tot,
           "close_mismatch_ids": int(close_mm.tradingItemId.nunique()), "volume_mismatch_ids": int(vol_mm.tradingItemId.nunique()),
           "close_by_category": close_mm.category.value_counts().to_dict(),
           "close_over_1e-3_by_category": close_mm[close_mm.rel.abs() > flag_tol].category.value_counts().to_dict(),
           "volume_by_category": vol_mm.category.value_counts().to_dict(),
           "volume_mismatch_by_adjf": {"adjf<1 (reverse split: restated volume stored as integer -> raw recoverable only to 1/adjf shares)": int((vol_mm.adjf < 1).sum()),
                                      "adjf=1": int((vol_mm.adjf == 1).sum()), "adjf>1": int((vol_mm.adjf > 1).sum())},
           "volume_rows_compared_adjf_below_1": int(tot.get("volume_rows_adjf_below_1", 0)),
           "volume_mismatch_over_1e-3_adjf_ge_1": int(((vol_mm.adjf >= 1) & (vol_mm.rel.abs() > 1e-3)).sum()),
           "close_mismatch_by_adjf": {"adjf<1": int((close_mm.adjf < 1).sum()), "adjf=1": int((close_mm.adjf == 1).sum()), "adjf>1": int((close_mm.adjf > 1).sum())},
           "close_by_year": close_mm.groupby("year").size().to_dict(),
           "close_over_1e-3_by_year": close_mm[close_mm.rel.abs() > flag_tol].groupby("year").size().to_dict(),
           "volume_by_year": vol_mm.groupby("year").size().to_dict(),
           "close_top_ids": close_mm.groupby(["tradingItemId", "symbol"], dropna=False).size().sort_values(ascending=False).head(20).reset_index().rename(columns={0: "n"}).to_dict("records"),
           "volume_top_ids": vol_mm.groupby(["tradingItemId", "symbol"], dropna=False).size().sort_values(ascending=False).head(10).reset_index().rename(columns={0: "n"}).to_dict("records"),
           "close_examples_largest": close_mm.assign(absrel=close_mm.rel.abs()).sort_values("absrel", ascending=False).head(20)[["tradingItemId", "symbol", "date", "ciq_raw", "mi", "rel", "category"]].to_dict("records"),
           "close_examples_correction": close_mm[close_mm.category == "correction"].assign(absrel=close_mm.rel.abs()).sort_values("absrel", ascending=False).head(10)[["tradingItemId", "symbol", "date", "ciq_raw", "mi", "rel"]].to_dict("records"),
           "file": str(out), "file_rows": int(len(mm_out)), "synthetic": syn, "seconds": round(time.time() - t0, 1)}
    write_raw_summary(res, out_dir / "summary.md", asof)
    log.info("G-RAW: common %d, close mismatches %d (>1e-3: %d), volume %d, ciq-not-in-mi %d, mi-not-in-ciq %d (%.0fs)",
             tot["common_rows"], tot["close_mismatch"], tot["close_mismatch_over_1e-3"], tot["volume_mismatch"], tot["ciq_rows_not_in_mi"],
             tot["mi_rows_not_in_ciq"], res["seconds"])
    return res


def write_raw_summary(r: dict, path: Path, asof: str):
    L = [f"# miprice vs base_data 대조 요약 (2010-01-01 ~ {asof})", "",
         f"생성 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} / `prices_qa.py --gates raw`. 임계 |상대차| > {r['tolerance']:g} 를 불일치로 기록, > {r['flag_tolerance']:g} 는 base_data `qualityFlag=2`.", "",
         "## 1. 행 대조", "",
         "| 항목 | 값 |", "|---|---|",
         f"| ciq 행(2010+, asOf 이하) | {r['ciq_rows_2010plus']:,} (종가 있음 {r['ciq_rows_2010plus_with_close']:,}) |",
         f"| miprice 행(2010+, asOf 이하) | {r['mi_rows_2010plus_upto_asof']:,} (종가 있음 {r['mi_rows_with_close']:,}) |",
         f"| 공통 (id,date) | {r['common_rows']:,} (종목 {r['ids_common']:,}); 양쪽 종가 {r['common_both_close']:,}; 양쪽 거래량 {r['common_both_volume']:,} |",
         f"| miprice에 없는 ciq 행 | {r['ciq_rows_not_in_mi']:,} (ciq 종가 있는 행 {r['ciq_close_rows_not_in_mi']:,}; mi 종가 기준으로는 {r['ciq_close_rows_not_in_mi_close']:,}) |",
         f"| ciq에 없는 miprice 행 | {r['mi_rows_not_in_ciq']:,} (mi 종가 있는 행 {r['mi_close_rows_not_in_ciq']:,}) |",
         f"| 종가 불일치 >1e-4 | {r['close_mismatch']:,} 행 / {r['close_mismatch_ids']:,} 종목 (>1e-3: {r['close_mismatch_over_1e-3']:,}, >1e-2: {r['close_mismatch_over_1e-2']:,}) |",
         f"| 거래량 불일치 >1e-4 | {r['volume_mismatch']:,} 행 / {r['volume_mismatch_ids']:,} 종목 (>1e-3: {r['volume_mismatch_over_1e-3']:,}; mi 0·ciq 비0: {r['volume_mi_zero_ciq_nonzero']:,}) |",
         "", "## 2. 종가 불일치 분류 (run 기준: 마지막 공통행까지 이어지면 tail, 길이≤3 correction, 비율 일정 scale, 나머지 other)", "",
         md_table([{"category": k, "rows(>1e-4)": v, "rows(>1e-3)": r["close_over_1e-3_by_category"].get(k, 0)} for k, v in r["close_by_category"].items()]),
         "## 3. 연도별 종가 불일치", "",
         md_table([{"year": k, "rows(>1e-4)": v, "rows(>1e-3)": r["close_over_1e-3_by_year"].get(k, 0), "volume rows(>1e-4)": r["volume_by_year"].get(k, 0)} for k, v in sorted(r["close_by_year"].items())]),
         "## 4. 종목별 상위 (종가)", "", md_table(r["close_top_ids"]),
         "## 5. 사례: 상대차 최대 20건 (종가)", "", md_table(r["close_examples_largest"]),
         "## 6. 사례: 정정형(correction) 상위", "", md_table(r["close_examples_correction"]),
         "## 7. 거래량 불일치 분류·상위 종목", "", md_table([{"category": k, "rows": v} for k, v in r["volume_by_category"].items()]), md_table(r["volume_top_ids"]),
         "", "거래량 불일치와 adjustmentfactor: 역분할(adjf<1) 종목은 벤더가 현재 기준으로 재계산한 거래량을 정수로 저장하므로 raw 복원이 1/adjf 주 단위로만 가능함.", "",
         md_table([{"adjf 구분": k, "rows": v} for k, v in r["volume_mismatch_by_adjf"].items()]),
         f"비교한 거래량 행 중 adjf<1 인 행 {r['volume_rows_compared_adjf_below_1']:,}; adjf≥1 이면서 >1e-3 불일치 {r['volume_mismatch_over_1e-3_adjf_ge_1']:,}행.", "",
         "종가 불일치의 adjf 구분: " + str(r["close_mismatch_by_adjf"]),
         "", f"파일: `{Path(r['file']).name}` {r['file_rows']:,}행 (date, tradingItemId, symbol, field, ciq_raw, mi, rel, category, year)."]
    path.write_text("\n".join(L))


# --------------------------------------------------------------------------------------
# known values
# --------------------------------------------------------------------------------------
def gate_values(obs: Observed, div_df: pd.DataFrame) -> dict:
    def row(tid, day):
        k = key([tid], [np.datetime64(day, "ns")])[0]
        pos = np.searchsorted(obs.keys, k)
        return pos if pos < len(obs.keys) and obs.keys[pos] == k else -1

    checks = []

    def add(name, expected, actual, tol):
        ok = actual is not None and np.isfinite(actual) and abs(actual - expected) <= tol
        checks.append({"check": name, "expected": expected, "actual": None if actual is None else float(actual), "tol": tol, "ok": bool(ok)})

    for day, exp in (("2014-06-06", 645.57), ("2014-06-09", 93.70), ("2020-08-28", 499.23), ("2020-08-31", 129.04)):
        p = row(2590360, day)
        add(f"AAPL raw close {day}", exp, float(obs.close[p]) if p >= 0 else None, 0.005)
    p = row(2590360, "2014-06-06")
    add("AAPL raw volume 2014-06-06", 12517270, float(obs.volume[p]) if p >= 0 else None, 0.5)
    p = row(2603626, "2020-12-01")
    add("COST raw close 2020-12-01", 387.56, float(obs.close[p]) if p >= 0 else None, 0.005)
    lk = FactorLookup(div_df)
    f0, f1 = lk([2603626, 2603626], pd.to_datetime(["2020-11-30", "2020-12-01"]).to_numpy())
    add("COST divAdj step f(11-30)/f(12-01) = 1-10/391.77", 1 - 10 / 391.77, f0 / f1, 1e-5)
    f0, f1 = lk([2608344, 2608344], pd.to_datetime(["2015-07-17", "2015-07-20"]).to_numpy())
    add("EBAY divAdj step f(07-17)/f(07-20)", 0.42088, f0 / f1, 1e-5)
    for day, exp in (("2020-11-23", 25.645), ("2020-11-24", 25.12)):
        p = row(8214712, day)
        add(f"TPX split_adj close {day}", exp, float(obs.close[p] / obs.sf[p]) if p >= 0 else None, 0.001)
    p0, p1 = row(8214712, "2020-11-23"), row(8214712, "2020-11-24")
    if p0 >= 0 and p1 >= 0:
        add("TPX split_adj return 11-23 -> 11-24 (continuous, ~-2%)", 25.12 / 25.645 - 1, obs.close[p1] / obs.sf[p1] / (obs.close[p0] / obs.sf[p0]) - 1, 1e-4)
        checks.append({"check": "TPX raw close 11-23 -> 11-24 jumps (4:1 split)", "expected": "~-75%", "actual": float(obs.close[p1] / obs.close[p0] - 1), "tol": None,
                       "ok": bool(obs.close[p1] / obs.close[p0] - 1 < -0.7)})
    p = row(2595740, "2014-12-02")
    add("BRO split_adj close 2014-12-02", 16.105, float(obs.close[p] / obs.sf[p]) if p >= 0 else None, 0.001)
    return {"gate": "VALUES", "checks": checks, "pass": all(c["ok"] for c in checks)}


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------
def write_gates_md(R: dict, path: Path, asof: str):
    L = [f"# T2 QA 게이트 결과 (asOf {asof})", "",
         f"생성 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} / `cache_v21/prices_qa.py`. 각 게이트는 합성 반례로 검출 능력을 먼저 보인 뒤 실데이터 결과를 적음.", "",
         "## 요약", "", "| 게이트 | 합성 반례 판정 | 실데이터 | 판정 |", "|---|---|---|---|"]
    if "split" in R:
        r = R["split"]
        L.append(f"| G-SPLIT | 계수 1구간 ×1.5 교란 → {r['synthetic']['detected_rows']:,}행 검출 ({'OK' if r['synthetic']['ok'] else 'FAIL'}) | {r['rows']:,}행 중 상대오차>1e-6 {r['violations']:,}건 | {'PASS' if r['pass'] else 'FAIL'} |")
    if "jump" in R:
        r = R["jump"]
        s = r["synthetic"] or {}
        L.append(f"| G-JUMP | AAPL 2014-06-09 경계를 하루 늦춤 → {s.get('detected')}건 검출 {s.get('detected_dates')} ({'OK' if s.get('ok') else 'FAIL'}) | 경계 {r['boundaries']:,}개, 플래그 {r['fake_jumps']}건 = {r['fake_by_category']} (raw 40%↑ 진짜 분할 점프 {r['raw_jumps_over_40pct_at_boundaries']:,}) | {'PASS' if r['pass'] else 'FAIL(분류 아래)'} |")
    if "jump_v1" in R and "rules" in R["jump_v1"]:
        r = R["jump_v1"]
        rules = r["rules"]
        t2b = rules["t2_rule_at_boundaries (|r_v1|>40% & |r_v21_adj|<40%)"]["count"]
        t2a = rules["t2_rule_all_days"]["count"]
        lead = rules["lead_rule (<=-40%|>=+67%, |dlog|>=0.05), masks shifted 1 day"]
        L.append(f"| G-JUMP v1 대조군 | (v1 스냅샷 = 반례) | 같은 규칙: 경계일 {t2b}건, 전일자 {t2a}건; 리드 규칙 ALL {lead.get('ALL', {}).get('artifacts')} / Top1000 {lead.get('Top1000', {}).get('artifacts')} / Top500 {lead.get('Top500', {}).get('artifacts')} (감사 16/6/0); TPX 2020-11-24 검출 {r['tpx_2020_11_24_detected']} | 가짜 점프 다수 재현 |")
    if "div" in R:
        r = R["div"]
        s = r["synthetic"]
        L.append(f"| G-DIV | 오류 D → diff {s['wrong_D_diff']:.3g} 검출, 정상 D(분할 보정) diff {s['correct_D_split_corrected_diff']:.2g} ({'OK' if s['ok'] else 'FAIL'}) | 검사 {r['tested']:,} 스텝, 일치(1e-6) {r['matched_within_1e-6']:,}, 불일치 {r['mismatched']:,} = 벤더 계수 5자리 반올림 설명분 {r['mismatched_explained_by_5dec_factor_rounding']:,} + 분할 무보정식 일치 {r['mismatched_explained_by_uncorrected_split_form']:,} + 잔여 {r['mismatched_genuine']:,} (>1e-4: {r['mismatched_genuine_over_1e-4']:,}, {r['mismatched_genuine_ids']} 종목); 비현금 동일창 제외 {r['excluded_noncash_same_window']:,}, 비USD {r['excluded_nonusd_cash_same_window']:,} | {'PASS' if r['pass'] else '잔여 불일치(목록 아래)'} |")
    if "oracle" in R:
        r = R["oracle"]
        c = r["cash_form"]
        L.append(f"| G-ORACLE | 수익률 1건 +2e-4 교란 → 추가 검출 {r['synthetic']['extra_detected']} ({'OK' if r['synthetic']['ok'] else 'FAIL'}) | {r['rows_tested']:,}행 현금식 최대차 {c['max_abs_diff']:.2e} (>1e-4 {c['n_over_1e-4']}건, 그중 D/P<2% {c['n_over_1e-4_with_DP_below_2pct']}건); 재투자식 최대차 {r['reinvestment_form']['max_abs_diff']:.2e} | {'PASS' if r['pass_cash_1e-4'] else '2차항 차이(아래)'} |")
    if "raw" in R:
        r = R["raw"]
        L.append(f"| G-RAW | 합성 0.2%/1% 차이 검출 ({'OK' if r['synthetic']['ok'] else 'FAIL'}) | 공통 {r['common_rows']:,}행, 종가 >1e-4 {r['close_mismatch']:,}행 (>1e-3 {r['close_mismatch_over_1e-3']:,} → qualityFlag 2), 거래량 >1e-4 {r['volume_mismatch']:,}; mi에 없는 ciq {r['ciq_rows_not_in_mi']:,}, ciq에 없는 mi {r['mi_rows_not_in_ciq']:,} | 기록 |")
    if "values" in R:
        r = R["values"]
        n_ok = sum(c["ok"] for c in r["checks"])
        L.append(f"| 검증값 | – | {n_ok}/{len(r['checks'])} 일치 | {'PASS' if r['pass'] else 'FAIL'} |")
    L.append("")
    if "values" in R:
        L += ["## 검증값", "", md_table([{**c, "ok": "OK" if c["ok"] else "FAIL"} for c in R["values"]["checks"]], ["check", "expected", "actual", "tol", "ok"], fmt="{:.6f}"), ""]
    if "split" in R:
        r = R["split"]
        L += ["## G-SPLIT", "", f"- 대상: base_data 전 행 {r['rows']:,} (close null 포함). lookup(split.parquet) / adjustmentfactor − 1 의 절대값 > 1e-6 → 위반.",
              f"- 합성 반례: 첫 row group 첫 종목(id {r['synthetic']['perturbed_id']})의 마지막 구간 계수를 ×1.5 → {r['synthetic']['detected_rows']:,}행 검출.",
              f"- 결과: 위반 {r['violations']:,}건.", ""]
        if r["examples"]:
            L += [md_table(r["examples"]), ""]
        e = r["extreme_intervals"]
        L += [f"- 계수 범위: 최소 {r['factor_min']:.3g}, 최대 {r['factor_max']:.4g}. 극단 구간({e['rule']}): {e['rows']:,}행 / {e['ids']:,}종목, 그중 G-SPLIT 통과 종목 {e['ids_passing']:,}; 출처 {e['by_source']}. 전체 목록 `T2_work/gsplit_extreme_intervals.csv`.",
              f"- 종목별 lookup 출처(행 기준): {r['rows_by_factor_source']}; 종목 기준 {r['ids_by_factor_source']}.", "",
              "극단 구간 최소 15:", "", md_table(e["smallest"]), "", "극단 구간 최대 15:", "", md_table(e["largest"]), ""]
    if "jump" in R:
        r = R["jump"]
        L += ["## G-JUMP", "", f"- 경계 정의: 같은 종목 직전 관측행과 splitFactor가 다른 관측행 ({r['boundaries']:,}개, 2000+). 규칙: |r_split_adj| > 40% & |r_raw| < 40%.",
              f"- 결과: 가짜 점프 {r['fake_jumps']}건. 경계에서 raw 40% 초과 변동(진짜 분할 점프) {r['raw_jumps_over_40pct_at_boundaries']:,}건, 경계 |r_adj| 최대 {r['max_abs_r_adj_at_boundary']:.3f}.", ""]
        L += [f"- 플래그 분류: {r['fake_by_category']} (같은 경계의 총수익률 |r_total| < 40% 이면 분할계수 점프가 배당계수(스핀오프) 스텝으로 상쇄되는 실제 사건, 아니면 벤더 분할계수 원장의 인공물). everTop500 {r['fake_by_universe']['everTop500']}건, everTop1000 {r['fake_by_universe']['everTop1000']}건(그중 unexplained {r['fake_by_universe']['unexplained_everTop1000']}건). 전체 목록 `T2_work/gjump_fake_jumps.csv`.", ""]
        if r["examples"]:
            L += ["플래그 목록(분류별, |r_adj| 큰 순, 최대 40):", "", md_table(r["examples"]), ""]
        L += ["경계에서 split_adj 변동이 가장 큰 10건 (참고, 실제 시장 변동):", "", md_table(r["biggest_adjusted_moves_at_boundaries"]), ""]
    if "jump_v1" in R and "rules" in R["jump_v1"]:
        r = R["jump_v1"]
        rules = r["rules"]
        L += ["## G-JUMP v1 대조군", "", f"- v1 스냅샷 close(wide, 열=tradingItemId) {r['v1_ids']:,}열, {r['since']}+ 공통 셀 {r['cells_compared']:,}. 후보 = v1 종가 수익률, 기준 = v2.1 split_adj 수익률.",
              f"- 같은 규칙(|r_v1| > 40% & |r_기준| < 40%): 분할 경계일 {rules['t2_rule_at_boundaries (|r_v1|>40% & |r_v21_adj|<40%)']['count']}건, 전 일자 {rules['t2_rule_all_days']['count']}건.", ""]
        lead = rules["lead_rule (<=-40%|>=+67%, |dlog|>=0.05), masks shifted 1 day"]
        if "tpx_2020_11_24_row" in r:
            L += [f"- TPX 8214712 2020-11-24: {r['tpx_2020_11_24_row']}", ""]
        L += ["리드 규칙(≤−40% 또는 ≥+67%, |Δlog| ≥ 0.05, 마스크 전일 기준) — 프로토타입 감사 16/6/0 과 비교:", "",
              md_table([{"mask": k, **{kk: vv for kk, vv in v.items() if kk != "examples"}} for k, v in lead.items()]), "",
              "v1 가짜 점프 사례(경계일, |r_v1| 큰 순):", "", md_table(rules["t2_rule_at_boundaries (|r_v1|>40% & |r_v21_adj|<40%)"]["examples"][:20]), ""]
    if "div" in R:
        r = R["div"]
        L += ["## G-DIV", "", "- 스텝 = 벤더 구간 k→k+1 (fromdate_{k+1} = 배당락일 X). `step = f_prev/f_new`(f_prev = 구간 k 계수, f_new = 구간 k+1 계수; 현금배당이면 <1). "
              "비교식 `1 − D/P_prev`: D = (직전 관측일, X] 배당락 USD 현금 유형 divamount 합(raw 단위), P_prev = 직전 관측 raw 종가 × sf(X)/sf(prev) (사이에 분할 경계 있으면 보정).",
              f"- 합성 반례: {r['synthetic']['fixture']} → 정상 D diff {r['synthetic']['correct_D_split_corrected_diff']:.2g}(통과), 잘못된 D(1.5) diff {r['synthetic']['wrong_D_diff']:.3g}(검출), 분할 미보정이면 실패 {r['synthetic']['naive_uncorrected_would_fail']}.", "",
              md_table([{"항목": k, "값": v} for k, v in r.items() if isinstance(v, (int, float)) and k not in ("tolerance", "seconds")], ["항목", "값"]), ""]
        L += ["- 분할 경계가 (직전 관측일, X] 안에 있는 스텝: 사양대로 P_prev를 sf(X)/sf(prev)로 보정한 식과, 보정 없는 식(D·P_prev 모두 구주 기준) 중 벤더 스텝과 맞는 쪽을 `split_between_matched_*`에 집계.", ""]
        if r["split_between_examples"]:
            L += ["분할 경계 포함 스텝 예:", "", md_table(r["split_between_examples"][:10]), ""]
        if r["mismatch_examples"]:
            L += [f"잔여 불일치 상위 (|diff| 큰 순, 잔여 {r['mismatched_genuine']:,}건 → `T2_work/gdiv_mismatches_genuine.csv`, 전체 {r['mismatched']:,}건 → `gdiv_mismatches.csv`):", "", md_table(r["mismatch_examples"][:30]), "",
                  "잔여 불일치 많은 종목:", "", md_table(r["mismatch_ids_top"]), ""]
    if "oracle" in R:
        r = R["oracle"]
        c, v = r["cash_form"], r["reinvestment_form"]
        L += ["## G-ORACLE (300종목)", "", f"- 행 {r['rows']:,}, 비현금/비USD 분배 창 제외 {r['rows_excluded_noncash_or_nonusd']:,} → 검사 {r['rows_tested']:,}. prevDate 일치 {r['prevDate_matches_returns_total']}.",
              f"- 현금식 `(adj_t + D/sf_ex)/adj_prev − 1` 대비: 최대 |차| {c['max_abs_diff']:.3e}, >1e-4 {c['n_over_1e-4']}건 (D/P<2% 인 것 {c['n_over_1e-4_with_DP_below_2pct']}건), >1e-5 {c['n_over_1e-5']}건, >1e-3 {c['n_over_1e-3']}건.",
              f"- 재투자식 `adj_t/(adj_prev − D/sf_ex) − 1`(계수식과 항등) 대비: 최대 |차| {v['max_abs_diff']:.3e}, >1e-6 {v['n_over_1e-6']}건, >1e-4 {v['n_over_1e-4']}건, p99.9 {v['p99.9']:.2e}.",
              "- 두 식의 차이는 2차항 ≈ (D/P)·r 이므로 특별배당일에 1e-4를 넘음(설계상 차이, 오류 아님).", "",
              "현금식 차이 상위:", "", md_table(r["top_cash_diffs"][:15]), "", "재투자식 차이 상위:", "", md_table(r["top_reinv_diffs"][:10]), "",
              f"누적곱 비교(2000+): 제외일 없는 종목 {r['cumulative']['ids_without_excluded_days']} 중 |log(누적 계수식/현금식)| < 1e-3 인 종목 {r['cumulative']['ids_cum_within_1e-3_log']}, < 1e-2 {r['cumulative']['ids_cum_within_1e-2_log']}, 최대 {r['cumulative']['max_abs_log_ratio_no_excl']:.3g}.", "",
              (md_table([{"symbol": k, **v} for k, v in r["cumulative"]["known"].items()]) if r["cumulative"]["known"] else "(AAPL/COST/EBAY는 300종목 표본에 없음)"), "",
              "누적 차이 큰 종목(스핀오프 등 제외일 있는 종목 포함):", "", md_table(r["cumulative"]["examples"]), ""]
    if "raw" in R:
        r = R["raw"]
        L += ["## G-RAW", "", f"- 상세는 `price_differences/summary.md`, 목록 `price_differences/mi_close_diff_2010plus.parquet` ({r['file_rows']:,}행).",
              f"- 공통 {r['common_rows']:,}행(양쪽 종가 {r['common_both_close']:,}): 종가 >1e-4 불일치 {r['close_mismatch']:,}행/{r['close_mismatch_ids']:,}종목, >1e-3 {r['close_mismatch_over_1e-3']:,}행(qualityFlag=2), 분류 {r['close_by_category']}.",
              f"- 거래량 >1e-4 불일치 {r['volume_mismatch']:,}행/{r['volume_mismatch_ids']:,}종목 (mi 0·ciq 비0 {r['volume_mi_zero_ciq_nonzero']:,}).",
              f"- miprice에 없는 ciq 행 {r['ciq_rows_not_in_mi']:,}(종가 있는 행 {r['ciq_close_rows_not_in_mi']:,}), ciq에 없는 miprice 행 {r['mi_rows_not_in_ciq']:,}(종가 있는 행 {r['mi_close_rows_not_in_ciq']:,}).", ""]
    path.write_text("\n".join(L))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--gates", default="values,split,jump,div,oracle,raw")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)
    pa.set_cpu_count(args.threads)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(args.work / "prices_qa.log")])
    gates = [g for g in args.gates.split(",") if g]
    t_all = time.time()
    asof = json.loads((args.work / "asof.json").read_text())["asOf"]
    sym = symbols_map()
    split_df = pd.read_parquet(args.stage / "factors" / "split.parquet")
    div_df = pd.read_parquet(args.stage / "factors" / "dividend.parquet")
    split, div = FactorLookup(split_df), FactorLookup(div_df)
    res_path = args.work / "qa_results.json"
    R = json.loads(res_path.read_text()) if res_path.exists() else {}

    def save():
        for k in list(R):
            R[k] = to_native(R[k])
        res_path.write_text(json.dumps(R, indent=1, default=str))

    if "split" in gates:
        R["split"] = gate_split(args.stage, args.work, split_df)
        save()
    need_obs = any(g in gates for g in ("values", "jump", "div", "oracle"))
    obs = Observed(args.stage, split, div, args.work) if need_obs else None
    if "values" in gates:
        R["values"] = gate_values(obs, div_df)
        save()
    if "jump" in gates:
        R["jump"] = gate_jump(obs, split_df, sym)
        save()
        R["jump_v1"] = gate_jump_v1_control(obs, sym)
        save()
    if "div" in gates or "oracle" in gates:
        symdf = pd.read_parquet(args.work / "symbols.parquet")
        ids = sorted(symdf.loc[symdf.inSymbolSet, "tradingItemId"].astype(int)) if "inSymbolSet" in symdf.columns else sorted(symdf.tradingItemId.astype(int))
        db = DB(application_name="cache_v21_T2_qa")
        dividends = fetch_dividends(db, ids, args.work)
        db.close()
        dsum = DividendSums(dividends, split)
        if "div" in gates:
            raw_div = pd.read_parquet(args.work / "raw_divadj.parquet")
            R["div"] = gate_div(obs, raw_div, dsum, split, sym, args.work)
            save()
        if "oracle" in gates:
            R["oracle"] = gate_oracle(obs, args.stage, dsum, sym, args.work)
            save()
    del obs
    if "raw" in gates:
        R["raw"] = gate_raw(args.stage, args.work, asof, sym)
        save()
    R["meta"] = {"asOf": asof, "gates_run": gates, "seconds": round(time.time() - t_all, 1), "peak_rss_gb": round(rss_gb(), 2),
                 "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    save()
    (args.stage / "qa").mkdir(parents=True, exist_ok=True)
    write_gates_md(R, args.stage / "qa" / "gates.md", asof)
    log.info("QA done in %.1f min, peak rss %.1f GB -> %s", (time.time() - t_all) / 60, rss_gb(), args.stage / "qa" / "gates.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
