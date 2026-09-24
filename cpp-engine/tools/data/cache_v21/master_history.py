#!/usr/bin/env python3
"""cache_v21/master_history.py - T1: `id_mapping`, `identifier_history`, `classification_history`, `primary_history`.

id_mapping (ciqgvkeyiid; objectid == tradingItemId verified: AAPL 2590360 -> 001690/01, RIG 51757943 -> 028338/01,
SIVB 22534033 -> 017120/01): every row for our ids, dates kept as stored (NULL symbolstartdate = unknown start,
NULL symbolenddate = open).  conflictFlag = the same tradingItemId has another row with a *different* (gvkey, iid)
whose period overlaps (NULL bounds are unbounded).  Nothing is filled in.

identifier_history (sec_idhist, item in TIC/EXCHG/ISIN) joined to id_mapping on (gvkey, iid): each history
interval is clipped to the mapping period; mappingQuality
  'ambiguous'    the clipped interval is also claimed by another tradingItemId for the same (gvkey, iid), or the
                 tradingItemId has a conflicting mapping over that interval
  'period_match' the mapping row carries an explicit symbolStartDate/symbolEndDate (dated period)
  'open_period'  the mapping row has neither date (open on both sides; the common single-mapping case)
effThru 2900-01-01 = open (sec_idhist sentinel) -> the two date columns are timestamp[ms] (2900 overflows ns).

classification_history / primary_history: v1 daily master files whose mtime is after the 2025-02-06 backfill
(files 20250203.. ; the 11,668 files 19800101..20250131 were all written on 2025-02-06 and are not observations).
Value runs per tradingItemId are compressed to intervals: effectiveFrom = first file date with the value,
effectiveTo = day before the next run (NaT for the last), observedAt = effectiveFrom, source 'v1_daily_master',
historyQuality 'observed'.  Items absent from the v1 files get one 'db_current'/'current_only' row (effectiveFrom
= asOf); items whose last observed value differs from today's DB value get an extra 'db_current' row at asOf.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import DB, SCHEMA  # noqa: E402
import master_build as mb  # noqa: E402

log = logging.getLogger("cache_v21.history")

NEG_INF = pd.Timestamp("1800-01-01")
POS_INF = pd.Timestamp("2900-01-01")
IDHIST_ITEMS = ("TIC", "EXCHG", "ISIN")
MAX_GAP_DAYS = 7               # normal daily-file cadence incl. weekends/holidays; longer gaps stay uncovered
CLASS_COLS = ["sector", "simpleIndustryDescription"]
PRIM_COLS = ["primaryFlag", "primaryFlagTi", "currentPrimary"]


def _ms(s: pd.Series) -> pd.Series:
    """timestamp[ms] series from python datetimes (holds 2900-01-01; pd.to_datetime would coerce it to NaT)."""
    if str(s.dtype).startswith("datetime64"):
        return s.astype("datetime64[ms]")
    vals = [np.datetime64("NaT", "ms") if (x is None or x is pd.NaT or (isinstance(x, float) and np.isnan(x)))
            else np.datetime64(x, "ms") for x in s]
    return pd.Series(np.array(vals, dtype="datetime64[ms]"), index=s.index)


# --------------------------------------------------------------------------------------
# id_mapping
# --------------------------------------------------------------------------------------
def fetch_gvkeyiid(db: DB) -> pd.DataFrame:
    """Whole ciqgvkeyiid (216k rows, no objectid index -> one pass is cheaper than 40 seq scans)."""
    out = mb.WORK / "ciqgvkeyiid_all.parquet"
    if out.exists():
        return pd.read_parquet(out)
    t0 = time.time()
    g = db.query(f"select symbolid, gvkey, iid, relatedcompanyid, exchangeid, objectid, symbolstartdate, symbolenddate, activeflag "
                 f"from {SCHEMA}.ciqgvkeyiid")
    g["symbolstartdate"] = mb.to_ns(g.symbolstartdate)
    g["symbolenddate"] = mb.to_ns(g.symbolenddate)
    g.to_parquet(out, **mb.PARQUET_KW)
    log.info("ciqgvkeyiid: %d rows (%.0fs)", len(g), time.time() - t0)
    return g


def _conflicts(df: pd.DataFrame) -> np.ndarray:
    """conflictFlag per row: another row of the same tradingItemId with a different (gvkey, iid) overlaps in time."""
    flag = np.zeros(len(df), dtype=bool)
    s = df.symbolStartDate.astype("datetime64[ms]").fillna(NEG_INF).to_numpy()
    e = df.symbolEndDate.astype("datetime64[ms]").fillna(POS_INF).to_numpy()
    key = (df.gvkey.fillna("") + "|" + df.iid.fillna("")).to_numpy()
    for tid, idx in df.groupby("tradingItemId").indices.items():
        if len(idx) < 2:
            continue
        for a in idx:
            for b in idx:
                if a == b or key[a] == key[b]:
                    continue
                if max(s[a], s[b]) <= min(e[a], e[b]):
                    flag[a] = True
                    break
    return flag


def step_id_mapping(db: DB, ids: np.ndarray) -> pd.DataFrame:
    out = mb.MASTER_DIR / "id_mapping.parquet"
    if out.exists():
        return pd.read_parquet(out)
    g = fetch_gvkeyiid(db)
    g = g[g.objectid.isin(ids)].copy()
    m = pd.DataFrame({
        "tradingItemId": g.objectid.astype("int64").to_numpy(),
        "gvkey": g.gvkey.astype(object).to_numpy(),
        "iid": g.iid.astype(object).to_numpy(),
        "symbolStartDate": mb.to_ns(g.symbolstartdate).to_numpy(),
        "symbolEndDate": mb.to_ns(g.symbolenddate).to_numpy(),
        "activeFlag": g.activeflag.astype("Int64").to_numpy(),
    })
    m["source"] = "ciqgvkeyiid"
    m = m.sort_values(["tradingItemId", "symbolStartDate", "symbolEndDate"], na_position="first").reset_index(drop=True)
    m["conflictFlag"] = _conflicts(m)
    mb.write_parquet(m, out)
    log.info("id_mapping: %d rows, %d ids mapped of %d, conflict rows %d (%d ids)", len(m), m.tradingItemId.nunique(), len(ids),
             int(m.conflictFlag.sum()), m.loc[m.conflictFlag, "tradingItemId"].nunique())
    return m


# --------------------------------------------------------------------------------------
# identifier_history
# --------------------------------------------------------------------------------------
def fetch_idhist(db: DB) -> pd.DataFrame:
    out = mb.WORK / "sec_idhist_tic_exchg_isin.parquet"
    if out.exists():
        return pd.read_parquet(out)
    t0 = time.time()
    h = db.query(f"select gvkey, iid, item, efffrom, effthru, itemvalue from {SCHEMA}.sec_idhist where item = any(%s)",
                 (list(IDHIST_ITEMS),))
    h["efffrom"] = _ms(h.efffrom)
    h["effthru"] = _ms(h.effthru)
    h.to_parquet(out, **mb.PARQUET_KW)
    log.info("sec_idhist TIC/EXCHG/ISIN: %d rows (%.0fs)", len(h), time.time() - t0)
    return h


def step_identifier_history(db: DB, id_mapping: pd.DataFrame) -> pd.DataFrame:
    out = mb.MASTER_DIR / "identifier_history.parquet"
    if out.exists():
        return pd.read_parquet(out)
    h = fetch_idhist(db)
    m = id_mapping.copy()
    m["_s"] = m.symbolStartDate.astype("datetime64[ms]").fillna(NEG_INF)
    m["_e"] = m.symbolEndDate.astype("datetime64[ms]").fillna(POS_INF)
    m["_dated"] = m.symbolStartDate.notna() | m.symbolEndDate.notna()
    m["_row"] = np.arange(len(m))
    j = h.merge(m, on=["gvkey", "iid"], how="inner")
    lo = np.maximum(j.efffrom.to_numpy(), j._s.to_numpy())
    hi = np.minimum(j.effthru.to_numpy(), j._e.to_numpy())
    j = j.assign(effFrom=lo, effThru=hi)[lo <= hi].copy()
    # ambiguity: same (gvkey, iid, item, efffrom) history row assigned to > 1 tradingItemId with overlapping clipped intervals
    j = j.sort_values(["gvkey", "iid", "item", "efffrom", "tradingItemId"]).reset_index(drop=True)
    amb = np.zeros(len(j), dtype=bool)
    for _, idx in j.groupby(["gvkey", "iid", "item", "efffrom"]).indices.items():
        if len(idx) < 2:
            continue
        tids = j.tradingItemId.to_numpy()[idx]
        if len(set(tids)) < 2:
            continue
        f, t = j.effFrom.to_numpy()[idx], j.effThru.to_numpy()[idx]
        for a in range(len(idx)):
            for b in range(len(idx)):
                if a != b and tids[a] != tids[b] and max(f[a], f[b]) <= min(t[a], t[b]):
                    amb[idx[a]] = True
                    break
    amb |= j.conflictFlag.to_numpy(dtype=bool)
    quality = np.where(amb, "ambiguous", np.where(j._dated.to_numpy(), "period_match", "open_period"))
    ih = pd.DataFrame({
        "tradingItemId": j.tradingItemId.astype("int64").to_numpy(),
        "gvkey": j.gvkey.to_numpy(), "iid": j.iid.to_numpy(), "item": j.item.to_numpy(), "value": j.itemvalue.to_numpy(),
        "effFrom": pd.Series(j.effFrom.to_numpy()).astype("datetime64[ms]").to_numpy(),
        "effThru": pd.Series(j.effThru.to_numpy()).astype("datetime64[ms]").to_numpy(),
        "source": "sec_idhist", "mappingQuality": quality,
    })
    ih = ih.drop_duplicates().sort_values(["tradingItemId", "item", "effFrom", "effThru", "value"]).reset_index(drop=True)
    mb.write_parquet(ih, out)
    log.info("identifier_history: %d rows, %d ids; quality %s; items %s", len(ih), ih.tradingItemId.nunique(),
             ih.mappingQuality.value_counts().to_dict(), ih.item.value_counts().to_dict())
    return ih


# --------------------------------------------------------------------------------------
# v1 daily master -> classification_history / primary_history
# --------------------------------------------------------------------------------------
def v1_observation_files() -> pd.DataFrame:
    rows = []
    for f in sorted(os.listdir(mb.V1_HISTORY_DIR)):
        mt = re.match(r"uss_cash_master\.(\d{8})\.parquet$", f)
        if not mt:
            continue
        st = os.stat(mb.V1_HISTORY_DIR / f)
        rows.append((mb.V1_HISTORY_DIR / f, pd.Timestamp(mt.group(1)), pd.Timestamp(st.st_mtime, unit="s")))
    files = pd.DataFrame(rows, columns=["path", "fileDate", "mtime"])
    obs = files[files.mtime > mb.V1_BACKFILL_MTIME_CUTOFF].sort_values("fileDate").reset_index(drop=True)
    readable = obs.path.map(lambda p: os.access(p, os.R_OK))
    if (~readable).any():
        log.warning("v1 master files not readable (skipped): %s", [p.name for p in obs.path[~readable]])
    obs = obs[readable].reset_index(drop=True)
    obs.attrs["skipped_unreadable"] = [str(p) for p in files.path[(files.mtime > mb.V1_BACKFILL_MTIME_CUTOFF) & ~files.path.map(lambda p: os.access(p, os.R_OK))]]
    log.info("v1 master files: %d total, %d backfill (mtime <= %s), %d observations %s..%s", len(files), len(files) - len(obs),
             mb.V1_BACKFILL_MTIME_CUTOFF.date(), len(obs), obs.fileDate.min().date(), obs.fileDate.max().date())
    return obs


def load_v1_long(ids: np.ndarray) -> pd.DataFrame:
    out = mb.WORK / "v1_daily_long.parquet"
    if out.exists():
        return pd.read_parquet(out)
    obs = v1_observation_files()
    parts = []
    want = ["primaryFlag", "primaryFlagTi", "currentPrimary", "sector", "simpleIndustryDescription", "companyName"]
    t0 = time.time()
    for r in obs.itertuples():
        df = pd.read_parquet(r.path)
        if df.index.name != "tradingItemId":
            raise RuntimeError(f"{r.path}: index is {df.index.name!r}, expected tradingItemId")
        has_sector = "sector" in df.columns
        df = df[[c for c in want if c in df.columns]].copy()
        for c in want:
            if c not in df.columns:
                df[c] = np.nan
        df = df[df.index.isin(ids)]
        df["fileDate"] = r.fileDate
        df["fileMtime"] = r.mtime.normalize()
        df["hasSectorColumn"] = has_sector
        parts.append(df.reset_index())
    long = pd.concat(parts, ignore_index=True)
    long["tradingItemId"] = long.tradingItemId.astype("int64")
    long.to_parquet(out, **mb.PARQUET_KW)
    log.info("v1 daily long: %d rows, %d ids, %d files (%.0fs)", len(long), long.tradingItemId.nunique(), len(obs), time.time() - t0)
    return long


def compress_runs(long: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Per tradingItemId, consecutive observations (by fileDate) with the same value tuple -> one interval."""
    d = long.sort_values(["tradingItemId", "fileDate"]).reset_index(drop=True)
    key = d[cols].astype(object).where(d[cols].notna(), "<NA>").astype(str).agg("|".join, axis=1)
    new_run = (d.tradingItemId != d.tradingItemId.shift()) | (key != key.shift())
    run_id = new_run.cumsum()
    g = d.groupby(run_id)
    runs = g.agg(tradingItemId=("tradingItemId", "first"), effectiveFrom=("fileDate", "min"), lastObserved=("fileDate", "max"),
                 nObs=("fileDate", "size"), **{c: (c, "first") for c in cols})
    runs = runs.reset_index(drop=True)
    nxt = runs.groupby("tradingItemId").effectiveFrom.shift(-1)
    # the old value is known to hold through its last observation; up to the next run when the next observation
    # follows within the normal file cadence (<= MAX_GAP_DAYS), otherwise the unobserved stretch is left uncovered
    close_next = (nxt - runs.lastObserved).dt.days <= MAX_GAP_DAYS
    runs["effectiveTo"] = np.where(nxt.isna(), np.datetime64("NaT"),
                                   np.where(close_next, (nxt - pd.Timedelta(days=1)).to_numpy(), runs.lastObserved.to_numpy()))
    runs["effectiveTo"] = runs.effectiveTo.astype("datetime64[ns]")
    runs["observedAt"] = runs.effectiveFrom
    runs["source"] = "v1_daily_master"
    runs["historyQuality"] = "observed"
    return runs


def _current_values(cur: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return cur.set_index("tradingItemId")[cols]


def add_db_current(runs: pd.DataFrame, cur: pd.DataFrame, cols: list[str], ids: np.ndarray, as_of: pd.Timestamp) -> pd.DataFrame:
    """Items without v1 observations -> one current_only row; items whose last observed tuple differs from the DB
    value today -> extra db_current row at asOf (previous run closed at asOf - 1 day)."""
    cv = _current_values(cur, cols).reindex(ids)
    have = set(runs.tradingItemId.unique())
    missing = [i for i in ids if i not in have]
    rows = []
    for tid in missing:
        rows.append({"tradingItemId": int(tid), **{c: cv.at[tid, c] for c in cols}, "effectiveFrom": as_of, "effectiveTo": pd.NaT,
                     "observedAt": as_of, "source": "db_current", "historyQuality": "current_only", "nObs": 0, "lastObserved": pd.NaT})
    last = runs.sort_values(["tradingItemId", "effectiveFrom"]).groupby("tradingItemId").tail(1).set_index("tradingItemId")
    changed = []
    for tid in last.index:
        a = last.loc[tid, cols]
        b = cv.loc[tid, cols]
        same = all((pd.isna(x) and pd.isna(y)) or (not pd.isna(x) and not pd.isna(y) and str(x) == str(y)) for x, y in zip(a, b))
        if not same:
            changed.append(tid)
            rows.append({"tradingItemId": int(tid), **{c: b[c] for c in cols}, "effectiveFrom": as_of, "effectiveTo": pd.NaT,
                         "observedAt": as_of, "source": "db_current", "historyQuality": "observed", "nObs": 1, "lastObserved": as_of})
    out = runs.copy()
    if changed:
        idx = out.index[out.tradingItemId.isin(changed) & out.effectiveTo.isna()]
        # old value known through its last observation; through asOf-1 only when observed within the file cadence
        recent = (as_of - out.loc[idx, "lastObserved"]).dt.days <= MAX_GAP_DAYS
        out.loc[idx, "effectiveTo"] = np.where(recent, (as_of - pd.Timedelta(days=1)).to_datetime64(), out.loc[idx, "lastObserved"].to_numpy())
    if rows:
        out = pd.concat([out, pd.DataFrame(rows)], ignore_index=True)
    out["effectiveFrom"] = out.effectiveFrom.astype("datetime64[ns]")
    out["effectiveTo"] = out.effectiveTo.astype("datetime64[ns]")
    out["observedAt"] = out.observedAt.astype("datetime64[ns]")
    log.info("history %s: %d observed runs, %d current_only ids, %d ids changed vs DB today", cols, len(runs), len(missing), len(changed))
    return out.sort_values(["tradingItemId", "effectiveFrom"]).reset_index(drop=True)


def fill_missing_classification(runs: pd.DataFrame, cur: pd.DataFrame) -> pd.DataFrame:
    """Lead decision 2026-09-16: intervals from files without the sector/simpleIndustryDescription columns
    (2025-02-03..02-27) are filled deterministically: industry = the item's next observed v1 industry (first later
    run with a value; source 'v1_daily_master+industry_map', quality 'observed'), else the DB current industry
    (source 'db_current+industry_map', quality 'current_only'); sector = sector_info.csv map of that industry.
    Intervals stay separate so the provenance of every value is visible; overlap/gap structure is unchanged."""
    sector = pd.read_csv(mb.SECTOR_CSV).drop_duplicates("simpleindustrydescription")
    smap = dict(zip(sector.simpleindustrydescription, sector.sector))
    cur_ind = cur.set_index("tradingItemId").simpleIndustryDescription
    out = runs.sort_values(["tradingItemId", "effectiveFrom"]).reset_index(drop=True)
    nxt_ind = out.groupby("tradingItemId").simpleIndustryDescription.transform(lambda x: x.bfill())
    missing = out.simpleIndustryDescription.isna()
    from_next = missing & nxt_ind.notna()
    from_cur = missing & nxt_ind.isna() & out.tradingItemId.map(cur_ind).notna()
    out.loc[from_next, "simpleIndustryDescription"] = nxt_ind[from_next]
    out.loc[from_next, "source"] = "v1_daily_master+industry_map"
    out.loc[from_cur, "simpleIndustryDescription"] = out.loc[from_cur, "tradingItemId"].map(cur_ind)
    out.loc[from_cur, "source"] = "db_current+industry_map"
    out.loc[from_cur, "historyQuality"] = "current_only"
    filled = from_next | from_cur
    out.loc[filled, "sector"] = out.loc[filled, "simpleIndustryDescription"].map(smap)
    log.info("classification fill: %d NaN intervals -> %d from next v1 observation, %d from DB current, %d still NaN",
             int(missing.sum()), int(from_next.sum()), int(from_cur.sum()), int((missing & ~filled).sum()))
    fill_missing_classification.stats = {"nan_intervals": int(missing.sum()), "filled_from_next_observation": int(from_next.sum()),
                                         "filled_from_db_current": int(from_cur.sum()), "still_nan": int((missing & ~filled).sum())}
    return out


def step_v1_histories(ids: np.ndarray, cur: pd.DataFrame, as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    long = load_v1_long(ids)
    # classification
    cls = compress_runs(long, CLASS_COLS)
    cls = fill_missing_classification(cls, cur)
    cls = add_db_current(cls, cur, CLASS_COLS, ids, as_of)
    cls_out = cls[["tradingItemId", "sector", "simpleIndustryDescription", "effectiveFrom", "effectiveTo", "observedAt", "source", "historyQuality"]]
    cls_out = cls_out.astype({"tradingItemId": "int64"})
    mb.write_parquet(cls_out.reset_index(drop=True), mb.MASTER_DIR / "classification_history.parquet")
    # primary
    pl = long.copy()
    prim = compress_runs(pl, PRIM_COLS)
    prim = add_db_current(prim, cur, PRIM_COLS, ids, as_of)
    for c in ("primaryFlag", "primaryFlagTi"):
        prim[c] = pd.to_numeric(prim[c], errors="coerce").astype("Int64")
    prim["currentPrimary"] = prim.currentPrimary.map(lambda x: None if pd.isna(x) else bool(x)).astype("boolean")
    prim_out = prim[["tradingItemId", "primaryFlag", "primaryFlagTi", "currentPrimary", "effectiveFrom", "effectiveTo", "observedAt", "source", "historyQuality"]]
    prim_out = prim_out.astype({"tradingItemId": "int64"})
    mb.write_parquet(prim_out.reset_index(drop=True), mb.MASTER_DIR / "primary_history.parquet")
    files = v1_observation_files()
    summary = {"n_files_used": int(len(files)), "first_file": str(files.fileDate.min().date()), "last_file": str(files.fileDate.max().date()),
               "skipped_unreadable": files.attrs.get("skipped_unreadable", []),
               "n_long_rows": int(len(long)), "n_ids_in_v1": int(long.tradingItemId.nunique()),
               "classification_rows": int(len(cls_out)), "classification_by_quality": cls_out.historyQuality.value_counts().to_dict(),
               "classification_by_source": cls_out.source.value_counts().to_dict(),
               "classification_fill": getattr(fill_missing_classification, "stats", {}),
               "classification_sector_nan_rows": int(cls_out.sector.isna().sum()),
               "primary_rows": int(len(prim_out)), "primary_by_quality": prim_out.historyQuality.value_counts().to_dict(),
               "ids_with_sector_change": int((cls_out[cls_out.source.str.startswith("v1_daily_master")].groupby("tradingItemId").agg(n=("sector", lambda x: x.astype(str).nunique())).n > 1).sum()),
               "ids_with_primary_change": int((prim_out[prim_out.source == "v1_daily_master"].groupby("tradingItemId").size() > 1).sum()),
               "first_file_with_sector_column": str(long.loc[long.sector.notna(), "fileDate"].min().date()) if long.sector.notna().any() else None}
    mb.save_json(summary, mb.QA_DIR / "history_summary.json")
    return cls_out, prim_out
