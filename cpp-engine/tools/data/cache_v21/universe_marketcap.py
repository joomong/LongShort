#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/universe_marketcap.py - `derived/marketcap.parquet` (T5 step 1).

Definition (prototype `build_cache_v2.fill_mcap_panel`, v1 `save_uss_master_market_data.py`):

    marketCap_USD(id, d) = ciqmarketcap.marketcap(companyId(id), d) * 1e6 * fx(USD, d) / fx(primaryCurrency(companyId), d)

  * `ciqmarketcap` is keyed by companyid (calendar-day rows); every tradingItemId of the company gets the
    company value (prototype method, so dual-listed items share one cap).
  * primaryCurrency = currency of the company's primary security's primary trading item (`ciqsecurity.primaryflag=1`
    join `ciqtradingitem.primaryflag=1`; smallest tradingItemId when a company has several).
  * fx = `ciqexchangerate.priceclose`, snapid 6, exact-date match; USD row is asserted to be 1.0; a non-USD company
    without an fx row that day gets NaN (kept as a row with NaN, counted).
  * rows = (tradingItemId, date) of `T2/base_data.parquet` with raw close notna (v1 meaning: a market-cap value exists
    only on priced days) and a `ciqmarketcap` row with marketcap notna.  2000-01-03 .. asOf (T2 max date).

Output (long, sorted (tradingItemId, date), zstd):
    date timestamp[ns] | tradingItemId int64 | marketCap_USD float64 | sharesOutstanding float64 | source str
    source: 'ciqmarketcap' (USD primary currency) / 'ciqmarketcap_fx' (converted) / 'ciqmarketcap_nofx' (fx missing -> NaN)

QA: compared cell-by-cell with the prototype wide `BaseData/marketCap_USD.parquet` (float32) -> `T5/qa/marketcap_compare.md`.

DB access: read-only sessions (`db.DB`), `companyid = any(%s)` chunks of <= 1000 companies, `wait_for_quiet` before
the pull.  Work files (chunk pulls with done markers, resumable) under `stage_v21/T5_work/mcap/`.

CLI:
    universe_marketcap.py build   [--chunk 1000] [--force]
    universe_marketcap.py compare
    universe_marketcap.py all
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from universe_common import (  # noqa: E402
    DAY_SHIFT, FX_SNAP, NS_PER_DAY, PROTO_ROOT, START, T1_MASTER, T2_ROOT, T5_ROOT, T5_WORK, USD, Timer,
    days_from_ns, fmt_int, load_instruments, log, lookup_sorted, make_key, md_table, read_json, rss_gb,
    setup_logging, to_ns_i8, write_json, write_parquet,
)
from db import DB, SCHEMA, chunked, wait_for_quiet  # noqa: E402

MCAP_TYPES = {"companyid": pa.int32(), "pricingdate": pa.timestamp("s"), "marketcap": pa.float64(),
              "sharesoutstanding": pa.float64()}
SOURCE_CODES = {0: "ciqmarketcap", 1: "ciqmarketcap_fx", 2: "ciqmarketcap_nofx"}


# ------------------------------------------------------------------------------------------------------
# DB pulls
# ------------------------------------------------------------------------------------------------------
def pull_primary_currency(db: DB, company_ids: list[int]) -> pd.DataFrame:
    """companyid -> (primarytradingitemid, currencyid, isocode); prototype rule (smallest primary item on ties)."""
    parts = []
    for ids in chunked(company_ids, 1000):
        parts.append(db.query(f"""
            select ss.companyid, titi.tradingitemid as primarytradingitemid, titi.currencyid, cu.isocode
            from {SCHEMA}.ciqsecurity ss
            join {SCHEMA}.ciqtradingitem titi on titi.securityid = ss.securityid and titi.primaryflag = 1
            join {SCHEMA}.ciqcurrency cu on cu.currencyid = titi.currencyid
            where ss.primaryflag = 1 and ss.companyid = any(%s)
        """, (ids,)))
    prim = pd.concat(parts, ignore_index=True)
    n_multi = int((prim.groupby("companyid").size() > 1).sum())
    prim = prim.sort_values(["companyid", "primarytradingitemid"]).drop_duplicates("companyid").reset_index(drop=True)
    log.info("primary currency: %d companies resolved of %d (multi-primary companies %d -> smallest item id)",
             len(prim), len(company_ids), n_multi)
    return prim


def pull_fx(db: DB, currencies: list[int], start: str = "1999-06-01") -> pd.DataFrame:
    ccy = sorted(set(int(c) for c in currencies) - {USD})
    if ccy:
        fx = db.query(f"""
            select currencyid, pricedate, priceclose as fx
            from {SCHEMA}.ciqexchangerate
            where snapid = %s and pricedate >= %s and currencyid = any(%s)
        """, (FX_SNAP, start, ccy))
        fx["fx"] = fx.fx.astype(float)
        fx["pricedate"] = pd.to_datetime(fx.pricedate)
    else:
        fx = pd.DataFrame({"currencyid": pd.Series([], dtype=int), "pricedate": pd.Series([], dtype="datetime64[ns]"),
                           "fx": pd.Series([], dtype=float)})
    usd = db.query(f"select min(priceclose) mn, max(priceclose) mx, count(*) n from {SCHEMA}.ciqexchangerate "
                   f"where snapid = %s and currencyid = %s and pricedate >= %s", (FX_SNAP, USD, START))
    if not (abs(float(usd.mn[0]) - 1) < 1e-9 and abs(float(usd.mx[0]) - 1) < 1e-9):
        raise RuntimeError(f"USD exchange rate is not 1.0 on snap {FX_SNAP}: {usd.to_dict()}")
    log.info("fx rows: %d for %d non-USD currencies; USD rows %d all 1.0", len(fx), len(ccy), int(usd.n[0]))
    return fx


def pull_mcap_chunks(db: DB, company_ids: list[int], start: str, end: str, work: Path, chunk: int,
                     force: bool = False) -> list[Path]:
    """ciqmarketcap rows (companyid chunks <= 1000) into work/mcap/chunk_XXXX.parquet with .done markers."""
    if chunk > 1000:
        raise ValueError("chunk must be <= 1000 (BRIEF §1)")
    out_dir = work / "mcap"
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = list(chunked(company_ids, chunk))
    paths = []
    t_all = time.time()
    n_rows = 0
    pulled = 0
    for k, ids in enumerate(chunks):
        p = out_dir / f"chunk_{k:04d}.parquet"
        marker = p.with_suffix(".done")
        paths.append(p)
        if marker.exists() and p.exists() and not force:
            info = read_json(marker, {})
            if info.get("ids") == len(ids) and info.get("first") == int(ids[0]) and info.get("last") == int(ids[-1]):
                n_rows += int(info.get("rows", 0))
                continue
        t0 = time.time()
        table = db.copy_arrow(f"""
            select companyid, pricingdate, marketcap, sharesoutstanding
            from {SCHEMA}.ciqmarketcap
            where companyid = any(%s) and pricingdate >= %s and pricingdate <= %s
        """, (list(map(int, ids)), start, end), MCAP_TYPES)
        write_parquet(table, p, row_group_size=4_000_000)
        marker.write_text(json.dumps({"rows": table.num_rows, "ids": len(ids), "first": int(ids[0]),
                                      "last": int(ids[-1]), "at": dt.datetime.now().isoformat(timespec="seconds"),
                                      "start": start, "end": end}))
        n_rows += table.num_rows
        pulled += 1
        log.info("mcap chunk %d/%d: %d companies -> %s rows (%.0fs)", k + 1, len(chunks), len(ids),
                 fmt_int(table.num_rows), time.time() - t0)
    log.info("mcap chunks ready: %d files, %s rows, %d pulled now (%.0fs)", len(paths), fmt_int(n_rows), pulled,
             time.time() - t_all)
    return paths


def pull_company_types(db: DB, company_ids: list[int]) -> pd.DataFrame:
    """companyid -> companytypeid (fund screen of the all-items universe variant)."""
    parts = []
    for ids in chunked(company_ids, 1000):
        parts.append(db.query(f"select companyid, companytypeid from {SCHEMA}.ciqcompany where companyid = any(%s)",
                              (ids,)))
    ct = pd.concat(parts, ignore_index=True).drop_duplicates("companyid")
    log.info("company types: %d rows; top counts %s", len(ct), ct.companytypeid.value_counts().head(5).to_dict())
    return ct


# ------------------------------------------------------------------------------------------------------
# Assembly
# ------------------------------------------------------------------------------------------------------
def base_close_rows(t2_root: Path) -> tuple[np.ndarray, np.ndarray, pd.Timestamp]:
    """(tradingItemId int64, date_ns int64) of base_data rows with close notna, in file order; asOf = max date."""
    pf = pq.ParquetFile(t2_root / "base_data.parquet")
    ids, dts = [], []
    for g in range(pf.metadata.num_row_groups):
        t = pf.read_row_group(g, columns=["tradingItemId", "date", "close"])
        close = t.column("close").to_numpy(zero_copy_only=False)
        keep = np.isfinite(close)
        ids.append(t.column("tradingItemId").to_numpy()[keep].astype(np.int64))
        dts.append(to_ns_i8(t.column("date").to_numpy())[keep])
    ids = np.concatenate(ids)
    dts = np.concatenate(dts)
    as_of = pd.Timestamp(dts.max())
    log.info("base_data close-notna rows: %s of %s; asOf %s", fmt_int(ids.size), fmt_int(pf.metadata.num_rows),
             as_of.date())
    return ids, dts, as_of


def load_mcap_long(paths: list[Path], start_day: int, end_day: int) -> dict[str, np.ndarray]:
    """Concatenate chunk pulls into sorted arrays keyed by (companyid << 20 | day)."""
    keys, mcap, shares = [], [], []
    for p in paths:
        t = pq.read_table(p)
        if t.num_rows == 0:
            continue
        cid = t.column("companyid").to_numpy().astype(np.int64)
        day = days_from_ns(to_ns_i8(t.column("pricingdate").to_numpy()))
        m = t.column("marketcap").to_numpy(zero_copy_only=False).astype(np.float64)
        s = t.column("sharesoutstanding").to_numpy(zero_copy_only=False).astype(np.float64)
        ok = np.isfinite(m) & (day >= start_day) & (day <= end_day)
        keys.append(make_key(cid[ok], day[ok]))
        mcap.append(m[ok])
        shares.append(s[ok])
    keys = np.concatenate(keys)
    mcap = np.concatenate(mcap)
    shares = np.concatenate(shares)
    order = np.argsort(keys, kind="stable")
    keys, mcap, shares = keys[order], mcap[order], shares[order]
    if keys.size and (np.diff(keys) == 0).any():
        raise RuntimeError("duplicate (companyid, pricingdate) in ciqmarketcap pull")
    log.info("mcap rows with marketcap notna in range: %s (rss %.1f GB)", fmt_int(keys.size), rss_gb())
    return {"keys": keys, "mcap": mcap, "shares": shares}


def assemble(work: Path, out_root: Path, t2_root: Path, master_root: Path, chunk: int, force: bool) -> dict:
    """Build derived/marketcap.parquet; returns stats dict (also written to work/marketcap_stats.json)."""
    tm = Timer("marketcap build")
    inst = load_instruments(master_root)
    id2co = inst.set_index("tradingItemId").companyId.astype(np.int64)
    company_ids = sorted(set(int(c) for c in id2co.to_numpy()))
    log.info("instruments: %d ids, %d companies", len(inst), len(company_ids))

    base_ids, base_ns, as_of = base_close_rows(t2_root)
    start_ts = pd.Timestamp(START)
    end_str = as_of.strftime("%Y-%m-%d")

    db = DB(application_name="cache_v21_T5_mcap")
    prim_p, fx_p, ct_p = work / "primary_currency.parquet", work / "fx_snap6.parquet", work / "company_types.parquet"
    if force or not (prim_p.exists() and fx_p.exists() and ct_p.exists()):
        prim = pull_primary_currency(db, company_ids)
        fx = pull_fx(db, prim.currencyid.dropna().astype(int).unique().tolist())
        ct = pull_company_types(db, company_ids)
        write_parquet(prim, prim_p)
        write_parquet(fx, fx_p)
        write_parquet(ct, ct_p)
    else:
        prim, fx = pd.read_parquet(prim_p), pd.read_parquet(fx_p)
    quiet = wait_for_quiet(db, max_active=2, poll_s=20.0, max_wait_s=600.0)
    log.info("wait_for_quiet: %s", quiet)
    paths = pull_mcap_chunks(db, company_ids, START, end_str, work, chunk, force=force)
    db.close()

    start_day = int(days_from_ns(np.array([start_ts.value]))[0])
    end_day = int(days_from_ns(np.array([as_of.value]))[0])
    mc = load_mcap_long(paths, start_day, end_day)

    # join base rows -> company mcap on the same day
    co_of_row = id2co.reindex(base_ids).to_numpy()
    outside = np.isnan(co_of_row.astype(float))
    if outside.any():
        # rc2 (D2): base_data also holds the auxiliary instruments (master/aux_instruments, never universe members) -> their rows are skipped
        n_out_ids = int(np.unique(base_ids[outside]).size)
        log.info("base_data rows of %s ids outside instruments (aux instruments) skipped: %s rows", fmt_int(n_out_ids), fmt_int(int(outside.sum())))
        base_ids, base_ns, co_of_row = base_ids[~outside], base_ns[~outside], co_of_row[~outside]
    co_of_row = co_of_row.astype(np.int64)
    base_day = days_from_ns(base_ns)
    pos, found = lookup_sorted(mc["keys"], make_key(co_of_row, base_day))
    n_base = base_ids.size
    n_found = int(found.sum())
    log.info("base rows with a marketcap row: %s / %s (%.2f%%)", fmt_int(n_found), fmt_int(n_base), 100 * n_found / n_base)

    ids_o = base_ids[found]
    ns_o = base_ns[found]
    day_o = base_day[found]
    co_o = co_of_row[found]
    mcap_o = mc["mcap"][pos[found]] * 1e6
    shares_o = mc["shares"][pos[found]]
    del mc

    # FX: primary currency per company (missing primary -> treated as USD like the prototype's fillna(USD))
    ccy_map = prim.set_index("companyid").currencyid
    ccy = ccy_map.reindex(co_o).fillna(USD).astype(np.int64).to_numpy()
    non = ccy != USD
    source = np.zeros(ids_o.size, dtype=np.int8)
    n_nofx = 0
    if non.any():
        fx_keys = make_key(fx.currencyid.to_numpy(np.int64), days_from_ns(to_ns_i8(fx.pricedate.to_numpy())))
        order = np.argsort(fx_keys, kind="stable")
        fx_keys_s, fx_vals = fx_keys[order], fx.fx.to_numpy(np.float64)[order]
        fpos, ffound = lookup_sorted(fx_keys_s, make_key(ccy[non], day_o[non]))
        rate = np.where(ffound, fx_vals[fpos], np.nan)
        mcap_o[non] = mcap_o[non] / rate
        source[non] = np.where(ffound, 1, 2)
        n_nofx = int((~ffound).sum())
    n_company_missing_primary = int((~ccy_map.index.isin(company_ids)).sum())
    n_missing_prim_rows = int((~pd.Index(co_o).isin(ccy_map.index)).sum())

    src_arr = pa.DictionaryArray.from_arrays(pa.array(source, type=pa.int8()),
                                             pa.array([SOURCE_CODES[i] for i in range(3)]))
    table = pa.table({
        "date": pa.array(ns_o.astype("datetime64[ns]")),
        "tradingItemId": pa.array(ids_o, type=pa.int64()),
        "marketCap_USD": pa.array(mcap_o, type=pa.float64()),
        "sharesOutstanding": pa.array(shares_o, type=pa.float64()),
        "source": src_arr,
    })
    # base_data is (tradingItemId, date) sorted; the filter kept that order -> assert instead of re-sorting
    if not (np.all(np.diff(ids_o) >= 0) and np.all((np.diff(ids_o) > 0) | (np.diff(ns_o) > 0))):
        order = np.lexsort((ns_o, ids_o))
        table = table.take(pa.array(order))
    out_p = out_root / "derived" / "marketcap.parquet"
    info = write_parquet(table, out_p, row_group_size=2_000_000)
    stats = {
        "asOf": end_str, "start": START, "n_ids": int(len(inst)), "n_companies": len(company_ids),
        "base_close_rows": int(n_base), "rows_out": int(n_found), "rows_without_mcap": int(n_base - n_found),
        "rows_nonusd": int(non.sum()), "rows_nonusd_fx_missing": n_nofx,
        "rows_company_without_primary_currency_row": n_missing_prim_rows,
        "companies_without_primary_currency_row": n_company_missing_primary,
        "primary_currency_counts": prim.currencyid.value_counts().head(12).to_dict(),
        "mcap_min": float(np.nanmin(mcap_o)), "mcap_max": float(np.nanmax(mcap_o)),
        "mcap_nonpositive": int((mcap_o <= 0).sum()), "shares_nan": int(np.isnan(shares_o).sum()),
        "ids_with_rows": int(np.unique(ids_o).size),
        "file": str(out_p), "bytes": info["bytes"], "wall_s": round(tm.done(), 1), "peak_rss_gb": round(rss_gb(), 2),
    }
    write_json(work / "marketcap_stats.json", stats)
    log.info("marketcap stats: %s", {k: v for k, v in stats.items() if k != "primary_currency_counts"})
    return stats


# ------------------------------------------------------------------------------------------------------
# Comparison with the prototype wide panel
# ------------------------------------------------------------------------------------------------------
def wide_to_long_finite(path: Path, value_dtype=np.float64) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(id int64, day int64, value) of finite cells of a wide parquet (columns = str(id), index = dates)."""
    t = pq.read_table(path)
    idx_col = "__index_level_0__" if "__index_level_0__" in t.column_names else None
    cols = [c for c in t.column_names if c != idx_col]
    dates = to_ns_i8(t.column(idx_col).to_numpy()) if idx_col else None
    days = days_from_ns(dates)
    ids_out, days_out, vals_out = [], [], []
    for c in cols:
        v = t.column(c).to_numpy(zero_copy_only=False).astype(value_dtype)
        fin = np.isfinite(v)
        if not fin.any():
            continue
        n = int(fin.sum())
        ids_out.append(np.full(n, int(c), dtype=np.int64))
        days_out.append(days[fin])
        vals_out.append(v[fin])
    return np.concatenate(ids_out), np.concatenate(days_out), np.concatenate(vals_out)


def compare_with_proto(out_root: Path, work: Path, proto_root: Path = PROTO_ROOT, tol: float = 1e-6) -> dict:
    tm = Timer("marketcap compare")
    ours = pq.read_table(out_root / "derived" / "marketcap.parquet",
                         columns=["date", "tradingItemId", "marketCap_USD", "source"]).to_pandas()
    o_ids = ours.tradingItemId.to_numpy(np.int64)
    o_day = days_from_ns(to_ns_i8(ours.date.to_numpy()))
    o_val = ours.marketCap_USD.to_numpy(np.float64)
    o_src = ours.source.astype(str).to_numpy()
    o_key = make_key(o_ids, o_day)
    order = np.argsort(o_key, kind="stable")
    o_key, o_val, o_src, o_ids, o_day = o_key[order], o_val[order], o_src[order], o_ids[order], o_day[order]

    p_ids, p_day, p_val = wide_to_long_finite(proto_root / "BaseData" / "marketCap_USD.parquet", np.float64)
    p_key = make_key(p_ids, p_day)
    porder = np.argsort(p_key, kind="stable")
    p_key, p_val, p_ids, p_day = p_key[porder], p_val[porder], p_ids[porder], p_day[porder]
    proto_last_day = int(p_day.max())
    proto_dates = pd.to_datetime(np.unique(p_day) * NS_PER_DAY)

    # restrict ours to proto's date span and proto's id set for the cell comparison
    in_span = o_day <= proto_last_day
    pos, found = lookup_sorted(p_key, o_key)
    common = found
    only_ours = in_span & ~found
    only_proto = np.ones(p_key.size, dtype=bool)
    only_proto[pos[found]] = False

    ov, pv = o_val[common], p_val[pos[common]]
    both_fin = np.isfinite(ov) & np.isfinite(pv)
    rel = np.abs(ov[both_fin] - pv[both_fin]) / np.maximum(np.abs(pv[both_fin]), 1e-300)
    bad = rel > tol
    n_common = int(common.sum())
    res = {
        "tolerance_rel": tol,
        "proto_cells": int(p_key.size), "ours_rows": int(o_key.size), "ours_rows_in_proto_span": int(in_span.sum()),
        "common_cells": n_common, "only_ours_in_span": int(only_ours.sum()), "only_proto": int(only_proto.sum()),
        "ours_nan_in_common": int((~np.isfinite(ov)).sum()), "proto_nan_in_common": int((~np.isfinite(pv)).sum()),
        "rel_diff_gt_tol": int(bad.sum()), "rel_diff_gt_tol_frac": float(bad.mean()) if bad.size else 0.0,
        "rel_diff_median": float(np.median(rel)) if rel.size else 0.0,
        "rel_diff_p99": float(np.quantile(rel, 0.99)) if rel.size else 0.0,
        "rel_diff_max": float(rel.max()) if rel.size else 0.0,
        "proto_last_date": str(pd.Timestamp(proto_last_day * NS_PER_DAY).date()),
        "proto_n_dates": int(proto_dates.size),
    }
    # ---- cause classification of cells with rel diff > tol
    idx_common = np.flatnonzero(common)[both_fin]
    bad_idx = idx_common[bad]
    bad_df = pd.DataFrame({"tradingItemId": o_ids[bad_idx], "date": pd.to_datetime(o_day[bad_idx] * NS_PER_DAY),
                           "ours": o_val[bad_idx], "proto": p_val[pos[bad_idx]], "source": o_src[bad_idx]})
    bad_df["relDiff"] = np.abs(bad_df.ours - bad_df.proto) / bad_df.proto.abs()
    f32 = np.abs(bad_df.ours.to_numpy(np.float32).astype(np.float64) - bad_df.proto) / bad_df.proto.abs() <= tol
    bad_df["cause"] = np.where(f32, "float32_rounding",
                               np.where(bad_df.source != "ciqmarketcap", "fx_or_primary_currency", "vendor_value_changed"))
    # vendor restatement: only in the last days of the proto span vs older history
    recent = bad_df.date >= (pd.Timestamp(proto_last_day * NS_PER_DAY) - pd.Timedelta(days=7))
    bad_df.loc[(bad_df.cause == "vendor_value_changed") & recent, "cause"] = "vendor_value_changed_recent_7d"
    res["bad_by_cause"] = bad_df.cause.value_counts().to_dict()
    res["bad_ids"] = int(bad_df.tradingItemId.nunique())
    res["bad_by_year"] = bad_df.date.dt.year.value_counts().sort_index().to_dict()
    res["bad_by_source"] = bad_df.source.value_counts().to_dict()
    res["bad_top_ids"] = bad_df.groupby("tradingItemId").size().sort_values(ascending=False).head(15).to_dict()
    ex = bad_df.sort_values("relDiff", ascending=False).head(20)
    res["bad_examples"] = ex.to_dict("records")
    # ---- only_ours / only_proto breakdown
    oo_ids = o_ids[only_ours]
    res["only_ours_ids"] = int(np.unique(oo_ids).size)
    res["only_ours_by_year"] = pd.Series(pd.to_datetime(o_day[only_ours] * NS_PER_DAY).year).value_counts().sort_index().to_dict()
    res["only_ours_top_ids"] = pd.Series(oo_ids).value_counts().head(10).to_dict()
    op_ids = p_ids[only_proto]
    res["only_proto_ids"] = int(np.unique(op_ids).size)
    res["only_proto_by_year"] = pd.Series(pd.to_datetime(p_day[only_proto] * NS_PER_DAY).year).value_counts().sort_index().to_dict()
    res["only_proto_top_ids"] = pd.Series(op_ids).value_counts().head(10).to_dict()
    res["wall_s"] = round(tm.done(), 1)
    write_json(work / "marketcap_compare.json", res)
    bad_df.sort_values(["tradingItemId", "date"]).to_csv(out_root / "qa" / "marketcap_diff_cells.csv", index=False)
    return res


def write_compare_md(stats: dict, res: dict, out_root: Path) -> Path:
    lines = ["# marketCap_USD 대조 (T5 vs 프로토타입 `BaseData/marketCap_USD.parquet`)", "",
             f"생성 {dt.datetime.now():%Y-%m-%d %H:%M:%S}. 정의: ciqmarketcap.marketcap(companyId, d)×1e6 / fx(primary ccy, d), "
             "snap 6, 종가 있는 (id,date)만. 프로토는 float32 wide, T5는 float64 long.", "",
             "## 1. 구축 통계", "",
             md_table(pd.DataFrame([{"항목": k, "값": (fmt_int(v) if isinstance(v, (int, np.integer)) and not isinstance(v, bool) else v)}
                                    for k, v in stats.items() if k not in ("primary_currency_counts", "file")])), "",
             "primary currency 분포(회사 수): " + ", ".join(f"{k}: {fmt_int(v)}" for k, v in stats["primary_currency_counts"].items()), "",
             "## 2. 셀 대조 (프로토 날짜 범위 " + f"~{res['proto_last_date']}, 공통 셀 기준)", "",
             md_table(pd.DataFrame([{"항목": k, "값": (fmt_int(v) if isinstance(v, (int, np.integer)) and not isinstance(v, bool) else v)}
                                    for k, v in res.items()
                                    if k in ("proto_cells", "ours_rows", "ours_rows_in_proto_span", "common_cells", "only_ours_in_span",
                                             "only_proto", "ours_nan_in_common", "proto_nan_in_common", "rel_diff_gt_tol",
                                             "rel_diff_gt_tol_frac", "rel_diff_median", "rel_diff_p99", "rel_diff_max")])), "",
             "## 3. 상대차 > 1e-6 셀의 원인 분류", "",
             md_table(pd.DataFrame([{"원인": k, "셀": fmt_int(v)} for k, v in res["bad_by_cause"].items()])), "",
             f"종목 수 {fmt_int(res['bad_ids'])}; source별 {res['bad_by_source']}; 연도별 {res['bad_by_year']}", "",
             "상위 종목(셀 수): " + ", ".join(f"{k}: {v}" for k, v in res["bad_top_ids"].items()), "",
             "### 상대차 상위 예", "",
             md_table(pd.DataFrame(res["bad_examples"])) if res["bad_examples"] else "없음", "",
             "## 4. 한쪽에만 있는 셀", "",
             f"- T5에만(프로토 범위 내): {fmt_int(res['only_ours_in_span'])}셀 / {fmt_int(res['only_ours_ids'])}종목, 연도별 {res['only_ours_by_year']}, 상위 {res['only_ours_top_ids']}",
             f"- 프로토에만: {fmt_int(res['only_proto'])}셀 / {fmt_int(res['only_proto_ids'])}종목, 연도별 {res['only_proto_by_year']}, 상위 {res['only_proto_top_ids']}",
             "", f"차이 셀 목록: `qa/marketcap_diff_cells.csv`"]
    p = out_root / "qa" / "marketcap_compare.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "compare", "all"])
    ap.add_argument("--t2", type=Path, default=T2_ROOT)
    ap.add_argument("--master", type=Path, default=T1_MASTER)
    ap.add_argument("--out", type=Path, default=T5_ROOT)
    ap.add_argument("--work", type=Path, default=T5_WORK)
    ap.add_argument("--chunk", type=int, default=1000)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    (args.out / "qa").mkdir(parents=True, exist_ok=True)
    setup_logging(args.work / "marketcap.log")
    pa.set_cpu_count(8)
    pa.set_io_thread_count(4)
    log.info("==== universe_marketcap %s start (pid %d)", args.cmd, __import__("os").getpid())
    stats = None
    if args.cmd in ("build", "all"):
        stats = assemble(args.work, args.out, args.t2, args.master, args.chunk, args.force)
    if args.cmd in ("compare", "all"):
        if stats is None:
            stats = read_json(args.work / "marketcap_stats.json")
        res = compare_with_proto(args.out, args.work)
        p = write_compare_md(stats, res, args.out)
        log.info("compare written: %s ; %s", p, {k: v for k, v in res.items() if not isinstance(v, (dict, list))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
