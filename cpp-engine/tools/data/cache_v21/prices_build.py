#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/prices_build.py - T2 step 1-2: symbol set + raw as-quoted daily prices (long) -> base_data.parquet.

Contract (BRIEF section 3-1): one row per (tradingItemId, date) that exists in `ciqpriceequity`
(2000-01-01 .. asOf), columns date, tradingItemId, open, high, low, close, vwap (float32 raw USD =
ciq x adjustmentfactor), volume (float64 = ciq volume / adjustmentfactor), source (int8, 1 = ciq),
qualityFlag (int8, 0 confirmed / 2 mi_mismatch / 3 ciq_null_close). Sorted (tradingItemId, date).
Trailing tails after the last real trade are kept (lifecycle masks them later).

Symbol set = prototype master (22,871 ids) UNION new ids under the build_cache_v2 rule
(v1 current-exchange rule UNION ever on Compustat EXCHG 11/12/14 & Common Stock & USD) that have at least one
non-null close in ciqpriceequity since 2000-01-01 (the prototype kept only items with >= 1 close in range; rule-only
ids without a 2000+ close are pre-2000 dead lines and are dropped after the pull, no re-query). No fund screen.
The final set is also written to <stage>/../shared/symbols_v21.parquet (tradingItemId, isNew, firstCloseDate,
lastCloseDate, nRows) for T1's instruments cross-check.

asOf (last complete date) = last trading day on which BOTH ciqpriceequity and miprice carry closes for
at least half of the recent full-day count; rows after it (same-day provisional prints) are dropped.

Steps (each resumable through done markers in --work):
  symbols   -> work/symbols.parquet (+ symbols.json counts)
  pull_ciq  -> work/ciq_chunks/chunk_NNNN.parquet   (ciqpriceequity, <= --chunk-size ids per query)
  pull_mi   -> work/mi_chunks/chunk_NNNN.parquet    (miprice close/volume since --mi-start, for asOf + QA)
  asof      -> work/asof.json
  symbol_set-> work/symbols.parquet gains nCloseRows2000/firstCloseDate/lastCloseDate/inSymbolSet; shared/symbols_v21.parquet
  assemble  -> <stage>/base_data.parquet (zstd, one row group per id chunk, rows of inSymbolSet ids only) + work/adjf_long.parquet
  apply_flags (separate CLI) -> rewrite qualityFlag=2 from the QA mismatch file

Memory: one id chunk in memory at a time (RSS well under the 25 GB cap); DB session read-only.

CLI:
  prices_build.py --stage .../stage_v21/T2 --work .../stage_v21/T2_work [--steps symbols,pull_ciq,...]
  prices_build.py --stage ... --work ... --apply-flags .../T2/qa/price_differences/mi_close_diff_2010plus.parquet
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from db import DB, SCHEMA, chunked, rss_gb, wait_for_quiet  # noqa: E402

# symbol-set rule constants, imported from the prototype builder so the rule cannot drift
try:
    import build_cache_v2 as bc2  # noqa: E402

    V1_EXCHANGES, MAIN_EXCHG, COMMON_STOCK, USD = bc2.V1_EXCHANGES, bc2.MAIN_EXCHG, bc2.COMMON_STOCK, bc2.USD
except Exception:  # pragma: no cover - fallback to the documented values
    V1_EXCHANGES, MAIN_EXCHG, COMMON_STOCK, USD = (102, 104, 106, 173, 458), ("11", "12", "14"), "Common Stock", 160

PROTO_MASTER = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/Master/master.parquet")
DEFAULT_STAGE = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2")
DEFAULT_WORK = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2_work")
START = "2000-01-01"
MI_START = "2010-01-01"
COMPRESSION = "zstd"

CIQ_TYPES = {"tradingitemid": pa.int64(), "pricingdate": pa.timestamp("s"), "priceopen": pa.float64(),
             "pricehigh": pa.float64(), "pricelow": pa.float64(), "priceclose": pa.float64(), "vwap": pa.float64(),
             "volume": pa.float64(), "adjustmentfactor": pa.float64()}
MI_TYPES = {"tradingitemid": pa.int64(), "pricedate": pa.date32(), "priceclose": pa.float64(), "volume": pa.float64()}

BASE_SCHEMA = pa.schema([
    ("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()),
    ("open", pa.float32()), ("high", pa.float32()), ("low", pa.float32()), ("close", pa.float32()),
    ("vwap", pa.float32()), ("volume", pa.float64()), ("source", pa.int8()), ("qualityFlag", pa.int8()),
])
ADJF_SCHEMA = pa.schema([("tradingItemId", pa.int64()), ("date", pa.timestamp("ns")),
                         ("adjustmentfactor", pa.float64()), ("ciqClose", pa.float64())])

Q_CONFIRMED, Q_PROVISIONAL, Q_MI_MISMATCH, Q_NULL_CLOSE = 0, 1, 2, 3
SOURCE_CIQ = 1

log = logging.getLogger("cache_v21.prices_build")


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------
class Timer:
    """Collects step timings + peak RSS into work/timings_build.json (consumed by prices_perf.py)."""

    def __init__(self, path: Path):
        self.path = path
        self.rec = json.loads(path.read_text()) if path.exists() else {}

    def step(self, name: str, seconds: float, **info):
        self.rec[name] = {"seconds": round(seconds, 1), "peak_rss_gb": round(rss_gb(), 2),
                          "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **info}
        self.path.write_text(json.dumps(self.rec, indent=1, default=str))


def row_keys(ids: np.ndarray, dates) -> np.ndarray:
    """int64 key = tradingItemId << 20 | days since 1970 (ids < 2^31, days < 2^20): (id, date) as one sortable int."""
    days = np.asarray(dates).astype("datetime64[D]").astype(np.int64)
    if (days < 0).any() or (days >= 1 << 20).any():
        raise ValueError("date outside 1970..4837")
    return (np.asarray(ids, dtype=np.int64) << 20) | days


def marker(path: Path) -> Path:
    return path.with_suffix(".done")


def write_atomic(table: pa.Table, path: Path, **kw):
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression=COMPRESSION, **kw)
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------
# step 1: symbol set
# --------------------------------------------------------------------------------------
SQL_V1_RULE = f"""
    select distinct ti.tradingitemid
    from {SCHEMA}.ciqtradingitem ti
    join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
    join {SCHEMA}.ciqsecuritysubtype sub on sub.securitysubtypeid = s.securitysubtypeid
         and sub.securitysubtypename = %s
    join {SCHEMA}.ciqcompany c on c.companyid = s.companyid
    join {SCHEMA}.ciqsimpleindustry si on si.simpleindustryid = c.simpleindustryid
    join {SCHEMA}.ciqexchange ex on ex.exchangeid = ti.exchangeid
    join {SCHEMA}.ciqsecurity ss on ss.companyid = c.companyid and ss.primaryflag = 1
    join {SCHEMA}.ciqtradingitem titi on titi.securityid = ss.securityid and titi.primaryflag = 1
    join {SCHEMA}.ciqcurrency cuti on cuti.currencyid = titi.currencyid
    where ti.exchangeid in %s
"""
SQL_EVER_MAIN = f"""
    select distinct gi.objectid as tradingitemid
    from {SCHEMA}.sec_idhist h
    join {SCHEMA}.ciqgvkeyiid gi on gi.gvkey = h.gvkey and gi.iid = h.iid
    join {SCHEMA}.ciqtradingitem ti on ti.tradingitemid = gi.objectid and ti.currencyid = %s
    join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
    join {SCHEMA}.ciqsecuritysubtype sub on sub.securitysubtypeid = s.securitysubtypeid
         and sub.securitysubtypename = %s
    where h.item = 'EXCHG' and h.itemvalue in %s
"""


def step_symbols(db: DB, work: Path, force: bool = False) -> pd.DataFrame:
    out = work / "symbols.parquet"
    if out.exists() and marker(out).exists() and not force:
        return pd.read_parquet(out)
    t0 = time.time()
    master = pd.read_parquet(PROTO_MASTER, columns=["tradingItemId", "symbol", "companyName"])
    proto_ids = set(master.tradingItemId.astype(int))
    v1 = set(db.query(SQL_V1_RULE, (COMMON_STOCK, V1_EXCHANGES)).tradingitemid.astype(int))
    t1 = time.time()
    ever = set(db.query(SQL_EVER_MAIN, (USD, COMMON_STOCK, MAIN_EXCHG)).tradingitemid.dropna().astype(int))
    t2 = time.time()
    rule = v1 | ever
    union = sorted(proto_ids | rule)
    new_ids = sorted(rule - proto_ids)
    # identity for the new ids (report only)
    ident = pd.DataFrame(columns=["tradingitemid", "symbol", "companyname", "exchangeid", "securitystartdate"])
    if new_ids:
        parts = [db.query(f"""
            select ti.tradingitemid, ti.tickersymbol as symbol, c.companyname, ti.exchangeid, s.securitystartdate,
                   ti.tradingitemstatusid
            from {SCHEMA}.ciqtradingitem ti
            join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
            left join {SCHEMA}.ciqcompany c on c.companyid = s.companyid
            where ti.tradingitemid = any(%s)""", (ids,)) for ids in chunked(new_ids, 5000)]
        ident = pd.concat(parts, ignore_index=True)
    sym = pd.DataFrame({"tradingItemId": np.array(union, dtype=np.int64)})
    sym["inProtoMaster"] = sym.tradingItemId.isin(proto_ids)
    sym["inV1Rule"] = sym.tradingItemId.isin(v1)
    sym["everMainCommonUSD"] = sym.tradingItemId.isin(ever)
    sym["isNew"] = ~sym.inProtoMaster
    sym = sym.merge(ident.rename(columns={"tradingitemid": "tradingItemId", "companyname": "companyName",
                                          "exchangeid": "exchangeId", "securitystartdate": "securityStartDate",
                                          "tradingitemstatusid": "tradingItemStatusId"}),
                    on="tradingItemId", how="left")
    m2 = master.set_index("tradingItemId")
    sym["symbol"] = sym.symbol.where(sym.symbol.notna(), sym.tradingItemId.map(m2.symbol))
    sym["companyName"] = sym.companyName.where(sym.companyName.notna(), sym.tradingItemId.map(m2.companyName))
    tmp = out.with_name(out.name + ".tmp")
    sym.to_parquet(tmp, index=False, compression=COMPRESSION)
    os.replace(tmp, out)
    info = {"n_proto_master": len(proto_ids), "n_v1_rule": len(v1), "n_ever_main_common_usd": len(ever),
            "n_rule_union": len(rule), "n_union_total": len(union), "n_new_ids": len(new_ids),
            "n_proto_not_in_rule_now": len(proto_ids - rule), "new_ids": new_ids,
            "seconds_v1_rule": round(t1 - t0, 1), "seconds_ever_main": round(t2 - t1, 1),
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    (work / "symbols.json").write_text(json.dumps(info, indent=1))
    marker(out).write_text(json.dumps({"at": info["at"], "n": len(union)}))
    log.info("symbols: proto %d, v1 rule %d, everMain %d, union %d, new %d (%.0fs)", len(proto_ids), len(v1),
             len(ever), len(union), len(new_ids), time.time() - t0)
    return sym


# --------------------------------------------------------------------------------------
# step 2: chunked pulls (ciqpriceequity, miprice)
# --------------------------------------------------------------------------------------
SQL_CIQ = f"""
    select tradingitemid, pricingdate, priceopen, pricehigh, pricelow, priceclose, vwap, volume, adjustmentfactor
    from {SCHEMA}.ciqpriceequity
    where tradingitemid = any(%s) and pricingdate >= %s {{extra}}
    order by tradingitemid, pricingdate
"""
SQL_MI = f"""
    select tradingitemid, pricedate, priceclose, volume
    from {SCHEMA}.miprice
    where tradingitemid = any(%s) and pricedate >= %s {{extra}}
    order by tradingitemid, pricedate
"""


def fetch_ciq(db: DB, ids: list[int], start: str, end: str | None = None) -> pa.Table:
    """ciqpriceequity rows for <= 1000 ids, pricingdate in [start, end] (end inclusive, None = open)."""
    if len(ids) > 1000:
        raise ValueError("chunk larger than 1000 ids")
    extra, params = "", [list(map(int, ids)), start]
    if end is not None:
        extra = "and pricingdate <= %s"
        params.append(end)
    return db.copy_arrow(SQL_CIQ.format(extra=extra), tuple(params), CIQ_TYPES)


def fetch_mi(db: DB, ids: list[int], start: str, end: str | None = None) -> pa.Table:
    if len(ids) > 1000:
        raise ValueError("chunk larger than 1000 ids")
    extra, params = "", [list(map(int, ids)), start]
    if end is not None:
        extra = "and pricedate <= %s"
        params.append(end)
    return db.copy_arrow(SQL_MI.format(extra=extra), tuple(params), MI_TYPES)


def pull_chunks(db: DB, ids: list[int], out_dir: Path, fetch, start: str, chunk_size: int, label: str,
                force: bool = False) -> list[Path]:
    """Chunk loop with done markers; checks pg_stat_activity before the loop and every 5 chunks."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = list(chunked(ids, chunk_size))
    paths, t_all, n_now, n_rows = [], time.time(), 0, 0
    for k, chunk in enumerate(chunks):
        p = out_dir / f"chunk_{k:04d}.parquet"
        paths.append(p)
        if p.exists() and marker(p).exists() and not force:
            continue
        if n_now % 5 == 0:
            w = wait_for_quiet(db)
            if w["waited_s"]:
                log.info("%s: waited %.0fs for other queries (%s)", label, w["waited_s"], w)
        t0 = time.time()
        table = fetch(db, chunk, start)
        write_atomic(table, p)
        marker(p).write_text(json.dumps({"rows": table.num_rows, "ids": len(chunk), "id_min": int(min(chunk)),
                                         "id_max": int(max(chunk)), "at": dt.datetime.now().isoformat()}))
        n_now += 1
        n_rows += table.num_rows
        log.info("%s chunk %d/%d: %d ids -> %d rows (%.0fs, rss %.1f GB)", label, k + 1, len(chunks), len(chunk),
                 table.num_rows, time.time() - t0, rss_gb())
    log.info("%s: %d chunk files (%d pulled now, %d rows now, %.0fs)", label, len(paths), n_now, n_rows,
             time.time() - t_all)
    return paths


# --------------------------------------------------------------------------------------
# step 3: asOf (last complete date)
# --------------------------------------------------------------------------------------
def close_counts(paths: list[Path], date_col: str, close_col: str, since: str) -> pd.Series:
    """Number of non-null closes per date over all chunk files (dates >= since)."""
    total = None
    for p in paths:
        t = pq.read_table(p, columns=[date_col, close_col])
        t = t.filter(pc.is_valid(t[close_col]))
        d = pd.Series(pd.to_datetime(t.column(date_col).to_numpy()).values.astype("datetime64[ns]"))
        c = d[d >= pd.Timestamp(since)].value_counts()
        total = c if total is None else total.add(c, fill_value=0)
    return total.sort_index().astype(int)


def determine_asof(ciq_counts: pd.Series, mi_counts: pd.Series, frac: float = 0.5, lookback_days: int = 120) -> dict:
    """asOf = last date where both sources have >= frac x (90th pct of the last `lookback_days` daily counts)."""
    def ref(s: pd.Series) -> float:
        recent = s[s.index >= s.index.max() - pd.Timedelta(days=lookback_days)]
        return float(recent.quantile(0.9))

    r_ciq, r_mi = ref(ciq_counts), ref(mi_counts)
    both = pd.DataFrame({"ciq": ciq_counts, "mi": mi_counts}).fillna(0)
    ok = both[(both.ciq >= frac * r_ciq) & (both.mi >= frac * r_mi)]
    asof = ok.index.max()
    tail = both[both.index > asof]
    return {"asOf": asof.strftime("%Y-%m-%d"), "ref_ciq": r_ciq, "ref_mi": r_mi,
            "ciq_count_asof": int(both.ciq.get(asof, 0)), "mi_count_asof": int(both.mi.get(asof, 0)),
            "dropped_after_asof": {d.strftime("%Y-%m-%d"): {"ciq": int(r.ciq), "mi": int(r.mi)} for d, r in tail.iterrows()},
            "ciq_max_date": ciq_counts.index.max().strftime("%Y-%m-%d"),
            "mi_max_date": mi_counts.index.max().strftime("%Y-%m-%d")}


def step_asof(ciq_paths: list[Path], mi_paths: list[Path], work: Path, force: bool = False) -> dict:
    out = work / "asof.json"
    if out.exists() and not force:
        return json.loads(out.read_text())
    t0 = time.time()
    since = (pd.Timestamp.today().normalize() - pd.Timedelta(days=200)).strftime("%Y-%m-%d")
    c_ciq = close_counts(ciq_paths, "pricingdate", "priceclose", since)
    c_mi = close_counts(mi_paths, "pricedate", "priceclose", since)
    info = determine_asof(c_ciq, c_mi)
    info["recent_counts"] = {d.strftime("%Y-%m-%d"): {"ciq": int(c_ciq.get(d, 0)), "mi": int(c_mi.get(d, 0))}
                             for d in sorted(set(c_ciq.index[-12:]) | set(c_mi.index[-12:]))}
    info["seconds"] = round(time.time() - t0, 1)
    out.write_text(json.dumps(info, indent=1))
    log.info("asOf = %s (ciq %d / ref %.0f, mi %d / ref %.0f); dropped after: %s", info["asOf"],
             info["ciq_count_asof"], info["ref_ciq"], info["mi_count_asof"], info["ref_mi"], info["dropped_after_asof"])
    return info


# --------------------------------------------------------------------------------------
# step 3b: final symbol set (>= 1 non-null close since 2000 for rule-only ids) + shared file for T1
# --------------------------------------------------------------------------------------
def per_id_close_stats(paths: list[Path], asof: str | None) -> pd.DataFrame:
    """tradingItemId -> nRows (all rows), nCloseRows (non-null close), firstCloseDate, lastCloseDate; rows <= asOf."""
    parts = []
    for p in paths:
        t = pq.read_table(p, columns=["tradingitemid", "pricingdate", "priceclose"])
        if asof is not None:
            t = t.filter(pc.less_equal(t["pricingdate"], pa.scalar(pd.Timestamp(asof).to_pydatetime(), type=pa.timestamp("s"))))
        if t.num_rows == 0:
            continue
        n_all = t.group_by("tradingitemid").aggregate([("pricingdate", "count")]).to_pandas().rename(columns={"pricingdate_count": "nRows"})
        tc = t.filter(pc.is_valid(t["priceclose"]))
        agg = tc.group_by("tradingitemid").aggregate([("pricingdate", "count"), ("pricingdate", "min"), ("pricingdate", "max")]).to_pandas()
        agg = agg.rename(columns={"pricingdate_count": "nCloseRows", "pricingdate_min": "firstCloseDate", "pricingdate_max": "lastCloseDate"})
        parts.append(n_all.merge(agg, on="tradingitemid", how="left"))
    st = pd.concat(parts, ignore_index=True).rename(columns={"tradingitemid": "tradingItemId"})
    st["nCloseRows"] = st.nCloseRows.fillna(0).astype(np.int64)
    st["nRows"] = st.nRows.astype(np.int64)
    for c in ("firstCloseDate", "lastCloseDate"):
        st[c] = pd.to_datetime(st[c]).astype("datetime64[ns]")
    return st


def step_symbol_set(ciq_paths: list[Path], work: Path, shared: Path, asof: str, force: bool = False) -> pd.DataFrame:
    out = work / "symbols.parquet"
    sym = pd.read_parquet(out)
    if "inSymbolSet" in sym.columns and (shared / "symbols_v21.parquet").exists() and not force:
        return sym
    t0 = time.time()
    st = per_id_close_stats(ciq_paths, asof)
    sym = sym.drop(columns=[c for c in ("nRows", "nCloseRows2000", "firstCloseDate", "lastCloseDate", "inSymbolSet") if c in sym.columns])
    sym = sym.merge(st.rename(columns={"nCloseRows": "nCloseRows2000"}), on="tradingItemId", how="left")
    sym["nRows"] = sym.nRows.fillna(0).astype(np.int64)
    sym["nCloseRows2000"] = sym.nCloseRows2000.fillna(0).astype(np.int64)
    sym["inSymbolSet"] = sym.inProtoMaster | (sym.isNew & (sym.nCloseRows2000 > 0))
    tmp = out.with_name(out.name + ".tmp")
    sym.to_parquet(tmp, index=False, compression=COMPRESSION)
    os.replace(tmp, out)
    final = sym[sym.inSymbolSet]
    shared.mkdir(parents=True, exist_ok=True)
    shared_df = pd.DataFrame({"tradingItemId": final.tradingItemId.astype(np.int64).to_numpy(), "isNew": final.isNew.astype(bool).to_numpy(),
                              "firstCloseDate": final.firstCloseDate.astype("datetime64[ns]").to_numpy(),
                              "lastCloseDate": final.lastCloseDate.astype("datetime64[ns]").to_numpy(),
                              "nRows": final.nRows.astype(np.int64).to_numpy()}).sort_values("tradingItemId").reset_index(drop=True)
    tmp = shared / "symbols_v21.parquet.tmp"
    shared_df.to_parquet(tmp, index=False, compression=COMPRESSION)
    os.replace(tmp, shared / "symbols_v21.parquet")
    new = final[final.isNew].sort_values("tradingItemId")
    info = {"n_union_candidates": int(len(sym)), "n_proto_master": int(sym.inProtoMaster.sum()),
            "n_rule_only_candidates": int(sym.isNew.sum()), "n_rule_only_with_close_2000plus": int(new.shape[0]),
            "n_rule_only_without_close_dropped": int((sym.isNew & (sym.nCloseRows2000 == 0)).sum()),
            "n_final_symbol_set": int(len(final)), "n_proto_without_close_now": int((sym.inProtoMaster & (sym.nCloseRows2000 == 0)).sum()),
            "new_ids": [{"tradingItemId": int(r.tradingItemId), "symbol": r.symbol, "companyName": r.companyName,
                         "exchangeId": None if pd.isna(r.exchangeId) else int(r.exchangeId),
                         "tradingItemStatusId": None if pd.isna(r.tradingItemStatusId) else int(r.tradingItemStatusId),
                         "firstCloseDate": str(pd.Timestamp(r.firstCloseDate).date()), "lastCloseDate": str(pd.Timestamp(r.lastCloseDate).date()),
                         "nCloseRows2000": int(r.nCloseRows2000)} for r in new.itertuples()],
            "seconds": round(time.time() - t0, 1), "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    (work / "symbol_set.json").write_text(json.dumps(info, indent=1, default=str))
    log.info("symbol set: candidates %d = proto %d + rule-only %d; rule-only with 2000+ close %d (dropped %d) -> final %d (%.0fs)",
             info["n_union_candidates"], info["n_proto_master"], info["n_rule_only_candidates"], info["n_rule_only_with_close_2000plus"],
             info["n_rule_only_without_close_dropped"], info["n_final_symbol_set"], info["seconds"])
    return sym


# --------------------------------------------------------------------------------------
# step 4: transform + assemble
# --------------------------------------------------------------------------------------
def transform_chunk(table: pa.Table, asof: str | None, keep_ids: np.ndarray | None = None) -> tuple[pa.Table, pa.Table]:
    """ciqpriceequity chunk -> (base_data rows, adjf side rows). raw = ciq x adjf (float32), volume = ciq / adjf.
    keep_ids: restrict to the final symbol set (ids sorted, int64)."""
    if asof is not None:
        table = table.filter(pc.less_equal(table["pricingdate"], pa.scalar(pd.Timestamp(asof).to_pydatetime(),
                                                                          type=pa.timestamp("s"))))
    if keep_ids is not None:
        table = table.filter(pc.is_in(table["tradingitemid"], value_set=pa.array(np.asarray(keep_ids, dtype=np.int64))))
    table = table.sort_by([("tradingitemid", "ascending"), ("pricingdate", "ascending")])
    adjf = table["adjustmentfactor"]
    n = table.num_rows
    date = pc.cast(table["pricingdate"], pa.timestamp("ns"))
    tid = table["tradingitemid"]
    cols = {"date": date, "tradingItemId": tid}
    for out_name, col in (("open", "priceopen"), ("high", "pricehigh"), ("low", "pricelow"), ("close", "priceclose"),
                          ("vwap", "vwap")):
        cols[out_name] = pc.cast(pc.multiply(table[col], adjf), pa.float32())
    cols["volume"] = pc.divide(table["volume"], adjf)
    cols["source"] = pa.array(np.full(n, SOURCE_CIQ, dtype=np.int8))
    close_null = pc.is_null(table["priceclose"]).to_numpy(zero_copy_only=False)
    q = np.where(close_null, Q_NULL_CLOSE, Q_CONFIRMED).astype(np.int8)
    cols["qualityFlag"] = pa.array(q)
    base = pa.table(cols, schema=BASE_SCHEMA)
    side = pa.table({"tradingItemId": tid, "date": date, "adjustmentfactor": adjf, "ciqClose": table["priceclose"]},
                    schema=ADJF_SCHEMA)
    return base, side


class RowGroupWriter:
    """ParquetWriter that writes one row group per call and replaces the target atomically on close."""

    def __init__(self, path: Path, schema: pa.Schema, compression: str = COMPRESSION):
        self.path = path
        self.tmp = path.with_name(path.name + ".tmp")
        self.writer = pq.ParquetWriter(self.tmp, schema, compression=compression, write_statistics=True,
                                       use_dictionary=False)
        self.rows = 0
        self.groups = 0

    def write(self, table: pa.Table):
        if table.num_rows == 0:
            return
        self.writer.write_table(table, row_group_size=table.num_rows)
        self.rows += table.num_rows
        self.groups += 1

    def close(self):
        self.writer.close()
        os.replace(self.tmp, self.path)


def step_assemble(ciq_paths: list[Path], asof: str, stage: Path, work: Path, force: bool = False) -> dict:
    out = stage / "base_data.parquet"
    side_out = work / "adjf_long.parquet"
    done = work / "base_data.done"                 # marker lives in work/ so the stage keeps the contract layout only
    if out.exists() and done.exists() and side_out.exists() and not force:
        return json.loads(done.read_text())
    t0 = time.time()
    sym = pd.read_parquet(work / "symbols.parquet")
    if "inSymbolSet" not in sym.columns:
        raise RuntimeError("symbols.parquet has no inSymbolSet column: run the symbol_set step first")
    keep_ids = np.sort(sym.loc[sym.inSymbolSet, "tradingItemId"].astype(np.int64).to_numpy())
    w = RowGroupWriter(out, BASE_SCHEMA)
    ws = RowGroupWriter(side_out, ADJF_SCHEMA)
    n_null_close, last_id = 0, -1
    for k, p in enumerate(ciq_paths):
        table = pq.read_table(p)
        base, side = transform_chunk(table, asof, keep_ids)
        if base.num_rows:
            first_id = int(base["tradingItemId"][0].as_py())
            if first_id <= last_id:
                raise RuntimeError(f"chunk {p} not in ascending id order after {last_id}")
            last_id = int(base["tradingItemId"][-1].as_py())
        n_null_close += int(pc.sum(pc.equal(base["qualityFlag"], Q_NULL_CLOSE)).as_py() or 0)
        w.write(base)
        ws.write(side)
        if (k + 1) % 5 == 0:
            log.info("assemble: %d/%d chunks, %d rows so far (rss %.1f GB)", k + 1, len(ciq_paths), w.rows, rss_gb())
    w.close()
    ws.close()
    info = {"rows": w.rows, "row_groups": w.groups, "n_null_close": n_null_close, "asOf": asof, "n_ids_symbol_set": int(len(keep_ids)),
            "bytes": out.stat().st_size, "seconds": round(time.time() - t0, 1),
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    done.write_text(json.dumps(info))
    log.info("base_data: %d rows, %d row groups, %d null-close rows, %.0f MB (%.0fs, rss %.1f GB)", w.rows, w.groups,
             n_null_close, info["bytes"] / 1e6, info["seconds"], rss_gb())
    return info


# --------------------------------------------------------------------------------------
# rc2 / R3: raw prices for an explicit id list (auxiliary instruments), same schema as base_data
# --------------------------------------------------------------------------------------
def build_ids(db: DB, ids: list[int], out: Path, work: Path, start: str = START, asof: str | None = None,
              chunk_size: int = 1000, label: str = "aux") -> dict:
    """Pull ciqpriceequity for exactly `ids` (no symbol-set rule, no mi pull) and write `out` with BASE_SCHEMA
    (one row group per id chunk, sorted (tradingItemId, date), rows <= asof).  Chunk files are cached under
    work/<label>_chunks/ like the main build.  Returns per-id coverage (rows, first/last close) in `info["coverage"]`."""
    ids = sorted(set(int(i) for i in ids))
    t0 = time.time()
    paths = pull_chunks(db, ids, work / f"{label}_chunks", fetch_ciq, start, chunk_size, label)
    w = RowGroupWriter(out, BASE_SCHEMA)
    ws = RowGroupWriter(work / "adjf_long.parquet", ADJF_SCHEMA)       # side table for prices_factors.reconcile_split
    cov = []
    n_null = 0
    for p in paths:
        base, side = transform_chunk(pq.read_table(p), asof, np.asarray(ids, dtype=np.int64))
        ws.write(side)
        if base.num_rows:
            n_null += int(pc.sum(pc.equal(base["qualityFlag"], Q_NULL_CLOSE)).as_py() or 0)
            df = base.select(["tradingItemId", "date", "close"]).to_pandas()
            g = df[df.close.notna()].groupby("tradingItemId").date.agg(["min", "max", "count"])
            g_all = df.groupby("tradingItemId").date.count().rename("nRows")
            cov.append(g.rename(columns={"min": "firstCloseDate", "max": "lastCloseDate", "count": "nCloseRows"}).join(g_all, how="outer"))
        w.write(base)
    w.close()
    ws.close()
    coverage = pd.concat(cov).reset_index() if cov else pd.DataFrame(columns=["tradingItemId", "firstCloseDate", "lastCloseDate", "nCloseRows", "nRows"])
    coverage = coverage.set_index("tradingItemId").reindex(ids).reset_index()
    coverage["nRows"] = coverage.nRows.fillna(0).astype(np.int64)
    coverage["nCloseRows"] = coverage.nCloseRows.fillna(0).astype(np.int64)
    info = {"ids": len(ids), "rows": w.rows, "row_groups": w.groups, "n_null_close": n_null, "asOf": asof, "start": start,
            "ids_without_rows": int((coverage.nRows == 0).sum()), "bytes": out.stat().st_size, "seconds": round(time.time() - t0, 1),
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "coverage": coverage}
    log.info("%s prices: %d ids -> %d rows (%d without rows), %.1f MB (%.0fs)", label, len(ids), w.rows, info["ids_without_rows"],
             info["bytes"] / 1e6, info["seconds"])
    return info


# --------------------------------------------------------------------------------------
# qualityFlag=2 from the QA mismatch file (rewrite, row group by row group)
# --------------------------------------------------------------------------------------
def apply_quality_flags(base_path: Path, diff_path: Path, rel_threshold: float = 1e-3, field: str = "close") -> dict:
    """Set qualityFlag=2 on base_data rows whose (id, date) is in the mismatch file with |rel| > threshold."""
    t0 = time.time()
    diff = pd.read_parquet(diff_path)
    diff = diff[(diff.field == field) & (diff.rel.abs() > rel_threshold)]
    diff_keys = row_keys(diff.tradingItemId.to_numpy(np.int64), pd.to_datetime(diff.date).values)
    pf = pq.ParquetFile(base_path)
    tmp = base_path.with_name(base_path.name + ".tmp")
    writer = pq.ParquetWriter(tmp, pf.schema_arrow, compression=COMPRESSION, write_statistics=True, use_dictionary=False)
    n_set, n_rows = 0, 0
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg)
        ids = t["tradingItemId"].to_numpy()
        q = t["qualityFlag"].to_numpy().copy()
        hit = np.isin(row_keys(ids, t["date"].to_numpy()), diff_keys) if len(diff_keys) else np.zeros(len(ids), bool)
        hit &= q != Q_NULL_CLOSE
        # reset previous mismatch flags (idempotent rewrite), then set the current ones
        q[q == Q_MI_MISMATCH] = Q_CONFIRMED
        q[hit] = Q_MI_MISMATCH
        n_set += int(hit.sum())
        n_rows += len(ids)
        t = t.set_column(t.schema.get_field_index("qualityFlag"), "qualityFlag", pa.array(q, type=pa.int8()))
        writer.write_table(t, row_group_size=t.num_rows)
    writer.close()
    os.replace(tmp, base_path)
    info = {"rows": n_rows, "flagged_mi_mismatch": n_set, "threshold": rel_threshold, "seconds": round(time.time() - t0, 1)}
    log.info("apply_quality_flags: %s", info)
    return info


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--start", default=START)
    ap.add_argument("--mi-start", default=MI_START)
    ap.add_argument("--chunk-size", type=int, default=1000)
    ap.add_argument("--steps", default="symbols,pull_ciq,pull_mi,asof,symbol_set,assemble")
    ap.add_argument("--shared", type=Path, default=None, help="shared dir for symbols_v21.parquet (default <stage>/../shared)")
    ap.add_argument("--force", default="", help="comma list of steps to redo")
    ap.add_argument("--smoke", type=int, default=0, help="debug: only the first N ids (+ the assertion ids)")
    ap.add_argument("--apply-flags", type=Path, default=None, help="mismatch parquet -> set qualityFlag=2 and exit")
    ap.add_argument("--ids-file", type=Path, default=None,
                    help="aux mode (rc2/R3): parquet/csv with a tradingItemId column -> pull raw prices for exactly these ids into "
                         "<stage>/<out-name> (same schema as base_data; no symbol-set rule, no mi pull) and exit")
    ap.add_argument("--out-name", default="base_data_aux.parquet", help="output file name for --ids-file mode")
    ap.add_argument("--asof", default=None, help="last date kept in --ids-file mode (default: <work>/asof.json if present, else all rows)")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)

    pa.set_cpu_count(args.threads)
    pa.set_io_thread_count(max(2, args.threads // 2))
    args.stage.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(args.work / "prices_build.log")])
    timer = Timer(args.work / "timings_build.json")

    if args.apply_flags is not None:
        info = apply_quality_flags(args.stage / "base_data.parquet", args.apply_flags)
        timer.step("apply_flags", info["seconds"], **{k: v for k, v in info.items() if k != "seconds"})
        return 0
    if args.ids_file is not None:
        ids_df = pd.read_csv(args.ids_file) if args.ids_file.suffix == ".csv" else pd.read_parquet(args.ids_file)
        ids = sorted(set(ids_df["tradingItemId"].astype(int)))
        asof = args.asof
        if asof is None and (args.work / "asof.json").exists():
            asof = json.loads((args.work / "asof.json").read_text())["asOf"]
        db = DB(application_name="cache_v21_R3_aux")
        info = build_ids(db, ids, args.stage / args.out_name, args.work, args.start, asof, args.chunk_size)
        db.close()
        info["coverage"].to_parquet(args.work / (Path(args.out_name).stem + "_coverage.parquet"), index=False)
        timer.step("aux_ids", info["seconds"], **{k: v for k, v in info.items() if k not in ("seconds", "coverage")})
        return 0

    steps = [s for s in args.steps.split(",") if s]
    force = {s for s in args.force.split(",") if s}
    db = DB(application_name="cache_v21_T2_build")
    t_all = time.time()
    log.info("==== prices_build start: stage=%s work=%s steps=%s force=%s", args.stage, args.work, steps, sorted(force))

    t0 = time.time()
    sym = step_symbols(db, args.work, force="symbols" in force)
    ids = sym.tradingItemId.astype(int).tolist()
    if args.smoke:
        must = [2590360, 2603626, 2608344, 8214712, 2595740]
        ids = sorted(set(ids[: args.smoke]) | set(must))
        log.info("SMOKE: %d ids", len(ids))
    timer.step("symbols", time.time() - t0, n_ids=len(ids))

    ciq_paths = mi_paths = None
    if "pull_ciq" in steps:
        t0 = time.time()
        ciq_paths = pull_chunks(db, ids, args.work / "ciq_chunks", fetch_ciq, args.start, args.chunk_size, "ciq",
                                force="pull_ciq" in force)
        timer.step("pull_ciq", time.time() - t0, chunks=len(ciq_paths), db_seconds=round(db.seconds, 1))
    if "pull_mi" in steps:
        t0 = time.time()
        mi_paths = pull_chunks(db, ids, args.work / "mi_chunks", fetch_mi, args.mi_start, args.chunk_size, "mi",
                               force="pull_mi" in force)
        timer.step("pull_mi", time.time() - t0, chunks=len(mi_paths))
    if "asof" in steps:
        t0 = time.time()
        ciq_paths = ciq_paths or sorted((args.work / "ciq_chunks").glob("chunk_*.parquet"))
        mi_paths = mi_paths or sorted((args.work / "mi_chunks").glob("chunk_*.parquet"))
        info = step_asof(ciq_paths, mi_paths, args.work, force="asof" in force)
        timer.step("asof", time.time() - t0, asOf=info["asOf"])
    if "symbol_set" in steps:
        t0 = time.time()
        ciq_paths = ciq_paths or sorted((args.work / "ciq_chunks").glob("chunk_*.parquet"))
        asof = json.loads((args.work / "asof.json").read_text())["asOf"]
        shared = args.shared or (args.stage.parent / "shared")
        sym = step_symbol_set(ciq_paths, args.work, shared, asof, force="symbol_set" in force)
        timer.step("symbol_set", time.time() - t0, n_final=int(sym.inSymbolSet.sum()))
    if "assemble" in steps:
        t0 = time.time()
        ciq_paths = ciq_paths or sorted((args.work / "ciq_chunks").glob("chunk_*.parquet"))
        asof = json.loads((args.work / "asof.json").read_text())["asOf"]
        info = step_assemble(ciq_paths, asof, args.stage, args.work, force="assemble" in force)
        timer.step("assemble", time.time() - t0, rows=info["rows"], bytes=info["bytes"], asOf=asof)
    db.close()
    timer.step("total_build", time.time() - t_all, db_queries=db.n_queries)
    log.info("==== prices_build done in %.1f min, peak rss %.1f GB", (time.time() - t_all) / 60, rss_gb())
    return 0


if __name__ == "__main__":
    sys.exit(main())
