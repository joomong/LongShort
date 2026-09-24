#!/usr/bin/env python3
"""Build cache_v2: a single-basis US cash-equity daily cache from one snapshot of xfeed.

Why: cache_v1 is an accumulation of daily vendor snapshots, so rows collected before a later
split stay on the old share basis (~1% of in-universe cells) and names that later moved to
OTC/BATS are missing for their whole life (v1 keys on the *current* exchange).  cache_v2 reads
every field from today's `ciqpriceequity`, which the vendor keeps restated onto the current
share basis, and widens the symbol set to everything that was ever listed on NYSE/AMEX/Nasdaq.

Contract: orchestrator brief `BRIEF_cache_v2.md` (section "v2 contract").  Outputs under
`--out-root` (61 server):

  Master/master.parquet            one row per tradingItemId (identity, flags, price coverage)
  Master/build_report.md           counts, timings, assertions with actual values, deviations
  Master/build.log                 full log
  BaseData/<field>.parquet         wide float32 panels, index = trading dates, columns = tradingItemId
  BaseData/TimeStamp.parquet, BaseData/tradingItemId.parquet   like v1
  DivData/divAmount_USD.parquet, divAmountCacheUnit.parquet    cash dividends on ex-date (calendar index)
  DivData/dividends_long.parquet   every raw ciqdividend row for the symbol set
  TopUniverseV2/<name>.parquet     v1 universe rule on v2 data (bool panels)
  TopUniverseV2_v1rule/<name>.parquet   same rule, candidates restricted to inV1Rule items
  MANIFEST.json, README.md

Safety: the DB session is read-only (build_events.connect); nothing under /home/base is written;
credentials are never logged.  Every expensive step stages its result under `--stage` with a
done marker, so a rerun resumes.  Big files are written locally once and copied to the 61 mount
once (sha256 computed during the copy).
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "8")

import argparse
import datetime as dt
import hashlib
import io
import json
import logging
import re
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_events as be  # noqa: E402  (read-only DB helpers)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------
DEFAULT_OUT_ROOT = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d")
DEFAULT_STAGE = Path("/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/cache_v2_stage")
V1_SNAPSHOT = Path("/home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14")
V1_LIVE = Path("/home/base/cache/cache_v1_cash_1d")
SECTOR_CSV = Path("/home/base/notebook/share/sector_info.csv")
SCHEMA = "xfl_target"

V1_EXCHANGES = (102, 104, 106, 173, 458)       # NYSE MKT, NasdaqGM, NYSE, NasdaqCM, NasdaqGS (current exchange)
MAIN_EXCHG = ("11", "12", "14")                 # Compustat sec_idhist EXCHG: NYSE, AMEX, Nasdaq
COMMON_STOCK = "Common Stock"                   # ciqsecuritysubtype name used by v1
USD = 160
FX_SNAP = 6
# cash types: brief list (1,4,5,7,8,10) + tax-code variants confirmed as per-share cash by the 2026-09-14 audit
CASH_DIV_TYPES = (1, 4, 5, 6, 7, 8, 10, 15, 17, 18, 19, 23)
STOCK_DIV_TYPES = (11, 20, 21, 22)
OTHER_NONCASH_DIV_TYPES = (31, 33)                 # rights/scrip codes: kept out of the cash panels
# TopUniverseV2 (all-items variant) fund screen, applied ONLY to items outside the v1 rule (v1 parity:
# v1 itself holds BDCs/CEFs that pass its exchange+name rule): Public Fund companies and fund-venue listings
# whose names carry neither "Trust" nor "ETF", so currentPrimary alone would let them in (e.g. DBC).
FUND_COMPANY_TYPE = 13
FUND_EXCHANGES = (117, 467, 358)
PRIMARY_NAME_PATTERN = "Trust|ETF"              # v1: currentPrimary excludes names matching this

# ciqpriceequity column -> cache field
PRICE_FIELDS = {
    "open": "priceopen", "high": "pricehigh", "low": "pricelow", "close": "priceclose",
    "volume": "volume", "VWAP": "vwap", "mid": "pricemid", "bid": "pricebid", "ask": "priceask",
    "adjustmentFactor": "adjustmentfactor",
}
BASE_FIELD_ORDER = ["open", "high", "low", "close", "volume", "VWAP", "mid", "bid", "ask", "midPrice",
                    "marketCap_USD", "currentPrimary", "adjustmentFactor", "divAdjFactor"]
UNIVERSE_NAMES = ["ALL", "Top1000", "Top700", "Top500", "Top300", "Top200", "Top100", "Top10"]

# TopUniverseV1/METADATA.yml (cross-checked against the file at run time)
UNIV_PARAMS = dict(RankWindow=21, FilterWindow=21, DollarValueWindow=100, StickyExitDays=10,
                   StickyEntryDays=60, MinDollarMarketCap=1e7, MinSpotDollarVolume=3e6)
TOKENS_TO_REMOVE = [
    27860642, 2642243, 1913901735, 1933861078, 2588570, 216534627, 2615898, 25745176, 659337840,
    1776948460, 2622876, 622839848, 1683446337, 1673049332, 2640322, 715588599, 24687604, 1846726403,
    132508040, 25389977, 2618746, 2626233, 1952948011, 1820944852, 2627536, 2659893, 268941272,
    2623717, 266673936, 39734352, 1672696918, 43858210, 604619421, 2593888, 1672821289, 709423890,
    2640332, 2595170, 2603117, 2594156, 2623439, 307533788, 2597012, 2611328, 2618950, 2629108,
]

# Known-value assertions from the brief (current-basis closes; USD dividends as paid)
ASSERT_CLOSE = [
    ("AAPL", 2590360, "2014-06-06", 23.056071), ("AAPL", 2590360, "2020-08-28", 124.8075),
    ("TPX", 8214712, "2020-11-23", 25.645), ("TPX", 8214712, "2020-11-24", 25.12),
    ("BRO", 2595740, "2014-12-02", 16.105),
    ("EBAY", 2608344, "2015-07-17", 66.29), ("EBAY", 2608344, "2015-07-20", 28.57),
]
ASSERT_DIV = [("COST", 2603626, "2020-12-01", 10.0)]
SIVB_ID = 22534033
FRC_COMPANY = 272383

log = logging.getLogger("cache_v2")


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_ns(x) -> pd.DatetimeIndex:
    """DatetimeIndex with ns resolution (pyarrow timestamp[s] columns come back as datetime64[s])."""
    return pd.DatetimeIndex(np.asarray(x).astype("datetime64[ns]"))


class Stage:
    """Local staging area with done markers; every expensive step goes through it."""

    def __init__(self, root: Path, force: set[str]):
        self.root = root
        self.force = force
        for sub in ("steps", "prices", "mcap", "out", "manifest"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    def done(self, step: str) -> bool:
        return step not in self.force and (self.root / "steps" / f"{step}.done").exists()

    def mark(self, step: str, info: dict | None = None):
        (self.root / "steps" / f"{step}.done").write_text(json.dumps({"at": now(), **(info or {})}))

    def path(self, name: str) -> Path:
        return self.root / name


def write_stage_parquet(df: pd.DataFrame, path: Path):
    tmp = path.with_suffix(".tmp.parquet")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def copy_with_sha256(src: Path, dst: Path) -> tuple[str, int]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as f, open(tmp, "wb") as g:
        while True:
            block = f.read(32 << 20)
            if not block:
                break
            h.update(block)
            g.write(block)
            size += len(block)
    os.replace(tmp, dst)
    if dst.stat().st_size != size or src.stat().st_size != size:
        raise RuntimeError(f"size mismatch after copy: {dst}")
    return h.hexdigest(), size


class Publisher:
    """Writes a parquet once locally, copies it once to the output root, records sha256."""

    def __init__(self, stage: Stage, out_root: Path, keep_local: set[str]):
        self.stage = stage
        self.out_root = out_root
        self.keep_local = keep_local
        self.entries: dict[str, dict] = {}
        for p in (stage.root / "manifest").glob("*.json"):
            e = json.loads(p.read_text())
            self.entries[e["path"]] = e

    def is_published(self, rel: str) -> bool:
        e = self.entries.get(rel)
        if e is None:
            return False
        remote = self.out_root / rel
        return remote.exists() and remote.stat().st_size == e["bytes"]

    def local_path(self, rel: str) -> Path:
        return self.stage.root / "out" / rel

    def publish_table(self, rel: str, table: pa.Table, rows: int, cols: int, row_group_size=None):
        if self.is_published(rel):
            log.info("  %s already published (skip)", rel)
            return self.entries[rel]
        local = self.local_path(rel)
        local.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        pq.write_table(table, local, compression="snappy", use_dictionary=False, write_statistics=False,
                       row_group_size=row_group_size or max(rows, 1))
        t1 = time.time()
        sha, size = copy_with_sha256(local, self.out_root / rel)
        entry = {"path": rel, "sha256": sha, "bytes": size, "rows": rows, "cols": cols, "written_at": now()}
        (self.stage.root / "manifest" / (rel.replace("/", "__") + ".json")).write_text(json.dumps(entry))
        self.entries[rel] = entry
        if rel not in self.keep_local:
            local.unlink(missing_ok=True)
        log.info("  wrote %s  rows=%d cols=%d  %.0f MB  write %.0fs copy %.0fs", rel, rows, cols,
                 size / 1e6, t1 - t0, time.time() - t1)
        return entry

    def publish_df(self, rel: str, df: pd.DataFrame, preserve_index=True):
        table = pa.Table.from_pandas(df, preserve_index=preserve_index)
        return self.publish_table(rel, table, len(df), df.shape[1])


def panel_frame(arr: np.ndarray, dates: pd.DatetimeIndex, ids: np.ndarray, col_name: str) -> pd.DataFrame:
    """Wide frame in the v1 layout: DatetimeIndex (unnamed) x int64 tradingItemId columns."""
    return pd.DataFrame(arr, index=pd.DatetimeIndex(dates), columns=pd.Index(ids, dtype="int64", name=col_name))


def read_v1_columns() -> tuple[set[int], Path]:
    """tradingItemIds present in v1 close.parquet (snapshot preferred, live cache as fallback)."""
    for root in (V1_SNAPSHOT, V1_LIVE):
        p = root / "BaseData" / "close.parquet"
        if p.exists():
            names = pq.read_schema(p).names
            return {int(n) for n in names if not n.startswith("__index")}, root
    raise RuntimeError("no v1 close.parquet found")


def load_v1_univ_params() -> dict:
    """Read the v1 METADATA.yml to cross-check the constants above."""
    import yaml
    for root in (V1_SNAPSHOT, V1_LIVE):
        p = root / "TopUniverseV1" / "METADATA.yml"
        if p.exists():
            return yaml.safe_load(p.read_text())
    return {}


# --------------------------------------------------------------------------------------
# DB pulls
# --------------------------------------------------------------------------------------
class DB:
    def __init__(self, timeout_ms: int):
        self.timeout_ms = timeout_ms
        self.conn = None
        self.n_queries = 0

    def connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = be.connect(self.timeout_ms)
        return self.conn

    def query(self, sql: str, params=None) -> pd.DataFrame:
        for attempt in range(3):
            try:
                self.n_queries += 1
                return be.query(self.connect(), sql, params)
            except Exception as exc:  # reconnect once; never log params (may be long id lists)
                log.warning("query failed (attempt %d): %s", attempt + 1, str(exc)[:200])
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                time.sleep(5)
        raise RuntimeError("query failed 3 times")

    def copy_csv(self, sql: str, column_types: dict) -> pa.Table:
        for attempt in range(3):
            try:
                buf = io.BytesIO()
                with self.connect().cursor() as cur:
                    cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buf)
                buf.seek(0)
                self.n_queries += 1
                return pacsv.read_csv(buf, convert_options=pacsv.ConvertOptions(column_types=column_types))
            except Exception as exc:
                log.warning("COPY failed (attempt %d): %s", attempt + 1, str(exc)[:200])
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                time.sleep(5)
        raise RuntimeError("COPY failed 3 times")

    def close(self):
        if self.conn is not None and not self.conn.closed:
            self.conn.close()


def sql_int_array(ids) -> str:
    return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]::int[]"


def step_candidates(db: DB, stage: Stage) -> pd.DataFrame:
    """Symbol set = (v1 current-exchange rule) UNION (ever on NYSE/AMEX/Nasdaq, Common Stock, USD).

    Returns the identity frame for the union (one row per tradingItemId) with the flags.
    """
    out = stage.path("candidates.parquet")
    if stage.done("candidates") and out.exists():
        return pd.read_parquet(out)
    t0 = time.time()
    # A) v1 structural rule: every inner join of save_uss_master_market_data.py except the
    #    per-date price / FX joins (those are row-level, not symbol-level).
    v1 = db.query(f"""
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
    """, (COMMON_STOCK, V1_EXCHANGES))
    v1_ids = set(v1.tradingitemid.astype(int))
    log.info("v1 rule items: %d (%.0fs)", len(v1_ids), time.time() - t0)

    # B) ever on a main exchange (Compustat EXCHG 11/12/14), Common Stock, USD trading item
    t0 = time.time()
    ever = db.query(f"""
        select distinct gi.objectid as tradingitemid
        from {SCHEMA}.sec_idhist h
        join {SCHEMA}.ciqgvkeyiid gi on gi.gvkey = h.gvkey and gi.iid = h.iid
        join {SCHEMA}.ciqtradingitem ti on ti.tradingitemid = gi.objectid and ti.currencyid = %s
        join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
        join {SCHEMA}.ciqsecuritysubtype sub on sub.securitysubtypeid = s.securitysubtypeid
             and sub.securitysubtypename = %s
        where h.item = 'EXCHG' and h.itemvalue in %s
    """, (USD, COMMON_STOCK, MAIN_EXCHG))
    ever_ids = set(ever.tradingitemid.dropna().astype(int))
    union = sorted(v1_ids | ever_ids)
    log.info("everMainExchange & Common & USD items: %d; union %d (%.0fs)", len(ever_ids), len(union), time.time() - t0)

    # identity for the union
    t0 = time.time()
    parts = []
    for ids in be.chunked(union, 5000):
        parts.append(db.query(f"""
            select ti.tradingitemid, ti.securityid, ti.tickersymbol as symbol, ti.exchangeid,
                   ti.currencyid as ticurrencyid, ti.primaryflag as primaryflagti, ti.tradingitemstatusid,
                   s.companyid, s.primaryflag, s.securitysubtypeid, sub.securitysubtypename,
                   s.securitystartdate, s.securityenddate,
                   c.companyname, si.simpleindustrydescription
            from {SCHEMA}.ciqtradingitem ti
            join {SCHEMA}.ciqsecurity s on s.securityid = ti.securityid
            left join {SCHEMA}.ciqsecuritysubtype sub on sub.securitysubtypeid = s.securitysubtypeid
            left join {SCHEMA}.ciqcompany c on c.companyid = s.companyid
            left join {SCHEMA}.ciqsimpleindustry si on si.simpleindustryid = c.simpleindustryid
            where ti.tradingitemid = any(%s)
        """, (ids,)))
    ident = pd.concat(parts, ignore_index=True)
    if ident.tradingitemid.duplicated().any():
        raise RuntimeError("identity query returned duplicate tradingItemIds")
    n_nocomp = int(ident.companyname.isna().sum())
    log.info("identity rows %d of %d union ids; %d have no ciqcompany row (kept, companyName null; "
             "they fail the v1 rule by construction) (%.0fs)", len(ident), len(union), n_nocomp, time.time() - t0)
    if len(ident) != len(union):
        raise RuntimeError("identity rows do not cover the union (trading item or security row missing)")

    # primary currency per company (v1: company's primary security's primary trading item)
    t0 = time.time()
    cids = sorted(set(ident.companyid.astype(int)))
    parts = []
    for ids in be.chunked(cids, 10000):
        parts.append(db.query(f"""
            select ss.companyid, titi.tradingitemid as primarytradingitemid, titi.currencyid, cu.isocode
            from {SCHEMA}.ciqsecurity ss
            join {SCHEMA}.ciqtradingitem titi on titi.securityid = ss.securityid and titi.primaryflag = 1
            join {SCHEMA}.ciqcurrency cu on cu.currencyid = titi.currencyid
            where ss.primaryflag = 1 and ss.companyid = any(%s)
        """, (ids,)))
    prim = pd.concat(parts, ignore_index=True)
    n_multi = int((prim.groupby("companyid").size() > 1).sum())
    if n_multi:
        log.warning("%d companies have >1 primary security/trading item; taking the smallest tradingItemId", n_multi)
    prim = prim.sort_values(["companyid", "primarytradingitemid"]).drop_duplicates("companyid")
    log.info("primary currency rows %d for %d companies (%.0fs)", len(prim), len(cids), time.time() - t0)

    # gvkey/iid pairs and EXCHG history
    t0 = time.time()
    parts = []
    for ids in be.chunked(union, 10000):
        parts.append(db.query(f"""
            select objectid as tradingitemid, gvkey, iid, symbolstartdate, symbolenddate, activeflag
            from {SCHEMA}.ciqgvkeyiid where objectid = any(%s)
        """, (ids,)))
    gv = pd.concat(parts, ignore_index=True)
    gvkeys = sorted(set(gv.gvkey.dropna()))
    parts = []
    for keys in be.chunked(gvkeys, 10000):
        parts.append(db.query(f"""
            select gvkey, iid, itemvalue as exchg, efffrom, effthru
            from {SCHEMA}.sec_idhist where item = 'EXCHG' and gvkey = any(%s)
        """, (keys,)))
    hist = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["gvkey", "iid", "exchg", "efffrom", "effthru"])
    hist = hist.merge(gv[["tradingitemid", "gvkey", "iid"]].drop_duplicates(), on=["gvkey", "iid"])
    hist = hist.sort_values(["tradingitemid", "efffrom"])
    log.info("gvkeyiid rows %d, EXCHG history rows %d (%.0fs)", len(gv), len(hist), time.time() - t0)

    def hist_json(g: pd.DataFrame) -> str:
        items = []
        for r in g.itertuples():
            thru = None if pd.isna(r.effthru) or r.effthru.year >= 2900 else r.effthru.strftime("%Y-%m-%d")
            items.append({"exchg": int(r.exchg) if str(r.exchg).isdigit() else str(r.exchg),
                          "from": None if pd.isna(r.efffrom) else r.efffrom.strftime("%Y-%m-%d"), "thru": thru})
        return json.dumps(items)

    hist_map = {tid: hist_json(g) for tid, g in hist.groupby("tradingitemid")}
    ever_any = set(hist.loc[hist.exchg.astype(str).isin(MAIN_EXCHG), "tradingitemid"].astype(int))
    # gvkey/iid choice: active row first, then the latest symbolstartdate
    gv = gv.assign(_act=gv.activeflag.fillna(0).astype(int)).sort_values(
        ["tradingitemid", "_act", "symbolstartdate"], ascending=[True, False, False])
    gv1 = gv.drop_duplicates("tradingitemid")[["tradingitemid", "gvkey", "iid"]]

    cand = ident.merge(prim, on="companyid", how="left").merge(gv1, on="tradingitemid", how="left")
    cand["exchangehistory"] = cand.tradingitemid.map(hist_map)
    cand["inv1rule"] = cand.tradingitemid.isin(v1_ids)
    cand["evermainexchange"] = cand.tradingitemid.isin(ever_any)
    cand["evermaincommonusd"] = cand.tradingitemid.isin(ever_ids)
    v1_cols, v1_root = read_v1_columns()
    cand["inv1cache"] = cand.tradingitemid.isin(v1_cols)
    log.info("inV1Cache reference: %s (%d columns)", v1_root, len(v1_cols))
    name = cand.companyname.fillna("")
    cand["currentprimary"] = ((cand.primaryflag == 1) & (cand.primaryflagti == 1)
                              & ~name.str.contains(PRIMARY_NAME_PATTERN, regex=True))
    cand = cand.sort_values("tradingitemid").reset_index(drop=True)
    write_stage_parquet(cand, out)
    stage.mark("candidates", {"n_v1rule": len(v1_ids), "n_ever": len(ever_ids), "n_union": len(union)})
    return cand


PRICE_TYPES = {"tradingitemid": pa.int32(), "pricingdate": pa.timestamp("s"), "priceopen": pa.float64(),
               "pricehigh": pa.float64(), "pricelow": pa.float64(), "pricemid": pa.float64(),
               "priceclose": pa.float64(), "pricebid": pa.float64(), "priceask": pa.float64(),
               "volume": pa.float64(), "adjustmentfactor": pa.float64(), "vwap": pa.float64()}


def step_pull_prices(db: DB, stage: Stage, ids: list[int], start: str, chunk_size: int) -> list[Path]:
    chunks = list(be.chunked(ids, chunk_size))
    paths = []
    t_all = time.time()
    n_pulled = 0
    for k, chunk in enumerate(chunks):
        p = stage.path(f"prices/chunk_{k:04d}.parquet")
        marker = p.with_suffix(".done")
        paths.append(p)
        if marker.exists() and p.exists() and "prices" not in stage.force:
            continue
        t0 = time.time()
        table = db.copy_csv(f"""
            select tradingitemid, pricingdate, priceopen, pricehigh, pricelow, pricemid, priceclose,
                   pricebid, priceask, volume, adjustmentfactor, vwap
            from {SCHEMA}.ciqpriceequity
            where tradingitemid = any({sql_int_array(chunk)}) and pricingdate >= '{start}'
        """, PRICE_TYPES)
        tmp = p.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="snappy")
        os.replace(tmp, p)
        marker.write_text(json.dumps({"rows": table.num_rows, "ids": len(chunk), "at": now()}))
        n_pulled += 1
        log.info("prices chunk %d/%d: %d ids -> %d rows (%.0fs)", k + 1, len(chunks), len(chunk),
                 table.num_rows, time.time() - t0)
    log.info("price chunks ready: %d files (%d pulled now, %.0fs)", len(paths), n_pulled, time.time() - t_all)
    return paths


def step_pull_mcap(db: DB, stage: Stage, company_ids: list[int], start: str, chunk_size: int) -> list[Path]:
    chunks = list(be.chunked(company_ids, chunk_size))
    paths = []
    t_all = time.time()
    types = {"companyid": pa.int32(), "pricingdate": pa.timestamp("s"), "marketcap": pa.float64(),
             "sharesoutstanding": pa.float64()}
    for k, chunk in enumerate(chunks):
        p = stage.path(f"mcap/chunk_{k:04d}.parquet")
        marker = p.with_suffix(".done")
        paths.append(p)
        if marker.exists() and p.exists() and "mcap" not in stage.force:
            continue
        t0 = time.time()
        table = db.copy_csv(f"""
            select companyid, pricingdate, marketcap, sharesoutstanding
            from {SCHEMA}.ciqmarketcap
            where companyid = any({sql_int_array(chunk)}) and pricingdate >= '{start}'
        """, types)
        tmp = p.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="snappy")
        os.replace(tmp, p)
        marker.write_text(json.dumps({"rows": table.num_rows, "ids": len(chunk), "at": now()}))
        log.info("mcap chunk %d/%d: %d companies -> %d rows (%.0fs)", k + 1, len(chunks), len(chunk),
                 table.num_rows, time.time() - t0)
    log.info("mcap chunks ready: %d files (%.0fs)", len(paths), time.time() - t_all)
    return paths


def step_pull_small(db: DB, stage: Stage, ids: list[int], currencies: set[int], start: str):
    """FX (snap 6), dividend-adjustment windows, raw dividends. Small enough for plain queries."""
    outs = {n: stage.path(f"{n}.parquet") for n in ("divadj", "dividends_raw", "fx")}
    if stage.done("small") and all(p.exists() for p in outs.values()):
        return {n: pd.read_parquet(p) for n, p in outs.items()}
    t0 = time.time()
    parts = []
    for chunk in be.chunked(ids, 10000):
        parts.append(db.query(f"""
            select tradingitemid, fromdate, todate, divadjfactor
            from {SCHEMA}.ciqpriceequitydivadjfactor where tradingitemid = any(%s)
        """, (chunk,)))
    divadj = pd.concat(parts, ignore_index=True)
    divadj["divadjfactor"] = divadj.divadjfactor.astype(float)
    log.info("divadj windows: %d rows (%.0fs)", len(divadj), time.time() - t0)

    t0 = time.time()
    div = db.query(f"""
        select d.dividendid, d.tradingitemid, d.exdate, d.paydate, d.recorddate, d.announceddate,
               d.divamount, d.netamount, d.grossamount, d.currencyid, d.exchangeid, d.divfreqtypeid,
               d.dividendtypeid, dt.dividendtypename, d.supplementaltypeid, st.supplementaltypename
        from {SCHEMA}.ciqdividend d
        left join {SCHEMA}.ciqdividendtype dt on dt.dividendtypeid = d.dividendtypeid
        left join {SCHEMA}.ciqdividendsupplementaltype st on st.supplementaltypeid = d.supplementaltypeid
        where d.tradingitemid = any(%s)
    """, (ids,))
    for c in ("divamount", "netamount", "grossamount"):
        div[c] = div[c].astype(float)
    log.info("dividend rows: %d (%.0fs)", len(div), time.time() - t0)

    t0 = time.time()
    ccy = sorted((currencies | set(div.currencyid.dropna().astype(int))) - {USD})
    if ccy:
        fx = db.query(f"""
            select currencyid, pricedate, priceclose as fx
            from {SCHEMA}.ciqexchangerate
            where snapid = %s and pricedate >= %s and currencyid = any(%s)
        """, (FX_SNAP, "1999-06-01", ccy))
        fx["fx"] = fx.fx.astype(float)
    else:
        fx = pd.DataFrame({"currencyid": pd.Series([], dtype=int), "pricedate": pd.Series([], dtype="datetime64[ns]"),
                           "fx": pd.Series([], dtype=float)})
    # sanity: the USD row is 1.0 on snap 6 (the v1 SQL divides by it)
    usd = db.query(f"select min(priceclose) mn, max(priceclose) mx from {SCHEMA}.ciqexchangerate "
                   f"where snapid = %s and currencyid = %s and pricedate >= %s", (FX_SNAP, USD, start))
    if not (abs(float(usd.mn[0]) - 1) < 1e-9 and abs(float(usd.mx[0]) - 1) < 1e-9):
        raise RuntimeError(f"USD exchange rate is not 1.0 on snap {FX_SNAP}: {usd.to_dict()}")
    log.info("fx rows: %d for %d non-USD currencies (%.0fs)", len(fx), len(ccy), time.time() - t0)

    write_stage_parquet(divadj, outs["divadj"])
    write_stage_parquet(div, outs["dividends_raw"])
    write_stage_parquet(fx, outs["fx"])
    stage.mark("small")
    return {"divadj": divadj, "dividends_raw": div, "fx": fx}


def step_company_types(db: DB, stage: Stage, company_ids: list[int]) -> pd.DataFrame:
    out = stage.path("company_types.parquet")
    if stage.done("company_types") and out.exists():
        return pd.read_parquet(out)
    t0 = time.time()
    has_names = db.query("select count(*) n from information_schema.tables where table_schema = %s and table_name = 'ciqcompanytype'",
                         (SCHEMA,)).n[0] > 0
    parts = []
    for ids in be.chunked(company_ids, 10000):
        if has_names:
            parts.append(db.query(f"""
                select c.companyid, c.companytypeid, ct.companytypename
                from {SCHEMA}.ciqcompany c left join {SCHEMA}.ciqcompanytype ct on ct.companytypeid = c.companytypeid
                where c.companyid = any(%s)""", (ids,)))
        else:
            parts.append(db.query(f"select companyid, companytypeid, null as companytypename from {SCHEMA}.ciqcompany "
                                  f"where companyid = any(%s)", (ids,)))
    ct = pd.concat(parts, ignore_index=True)
    write_stage_parquet(ct, out)
    stage.mark("company_types", {"rows": len(ct)})
    log.info("company types: %d rows; type counts %s (%.0fs)", len(ct), ct.companytypeid.value_counts().head(6).to_dict(), time.time() - t0)
    return ct


# --------------------------------------------------------------------------------------
# Date index
# --------------------------------------------------------------------------------------
def step_date_index(stage: Stage, chunk_paths: list[Path], start: str, min_day_frac: float) -> dict:
    out = stage.path("date_index.json")
    if stage.done("date_index") and out.exists():
        return json.loads(out.read_text())
    t0 = time.time()
    counts = None
    per_id = None
    for p in chunk_paths:
        t = pq.read_table(p, columns=["tradingitemid", "pricingdate", "priceclose"])
        df = t.to_pandas()
        df = df[df.priceclose.notna()]
        c = pd.Series(to_ns(df.pricingdate.to_numpy())).value_counts()
        counts = c if counts is None else counts.add(c, fill_value=0)
        n = df.groupby("tradingitemid").size()
        per_id = n if per_id is None else per_id.add(n, fill_value=0)
    counts = counts.sort_index().astype(int)
    counts.index = pd.to_datetime(counts.index)
    # last complete date: the newest date whose count is at least half of the recent full-day level
    recent = counts[counts.index >= counts.index.max() - pd.Timedelta(days=120)]
    ref = float(recent.quantile(0.9))
    last_date = counts[counts >= 0.5 * ref].index.max()
    counts = counts[(counts.index >= pd.Timestamp(start)) & (counts.index <= last_date)]
    local_med = counts.rolling(21, center=True, min_periods=1).median()
    keep = counts >= min_day_frac * local_med
    dropped = counts[~keep]
    dates = counts.index[keep]
    info = {
        "start": start, "last_date": last_date.strftime("%Y-%m-%d"), "ref_full_day_count": ref,
        "n_dates": int(len(dates)), "min_day_frac": min_day_frac,
        "dropped_dates": {d.strftime("%Y-%m-%d"): int(v) for d, v in dropped.items()},
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "n_close_rows_by_date": {d.strftime("%Y-%m-%d"): int(v) for d, v in counts.items()},
        "ids_with_close_rows": int((per_id > 0).sum()),
    }
    out.write_text(json.dumps(info))
    per_id.rename("n").rename_axis("tradingitemid").reset_index().to_parquet(stage.path("per_id_close_rows.parquet"), index=False)
    stage.mark("date_index", {"n_dates": info["n_dates"], "last_date": info["last_date"]})
    log.info("date index: %d dates %s..%s, %d sparse dates dropped, ref full-day count %.0f (%.0fs)",
             len(dates), dates[0].date(), dates[-1].date(), len(dropped), ref, time.time() - t0)
    if len(dropped):
        log.info("dropped dates (count): %s", ", ".join(f"{d}({v})" for d, v in info["dropped_dates"].items()))
    return info


# --------------------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------------------
def fill_price_panels(chunk_paths: list[Path], dates: pd.DatetimeIndex, ids: np.ndarray) -> dict[str, np.ndarray]:
    T, N = len(dates), len(ids)
    id_index = pd.Index(ids)
    panels = {f: np.full((T, N), np.nan, dtype=np.float32) for f in PRICE_FIELDS}
    t0 = time.time()
    n_rows = 0
    n_skipped = 0
    for p in chunk_paths:
        df = pq.read_table(p).to_pandas()
        r = dates.get_indexer(to_ns(df.pricingdate.to_numpy()))
        c = id_index.get_indexer(df.tradingitemid.to_numpy())
        ok = (r >= 0) & (c >= 0)
        n_skipped += int((~ok).sum())
        r, c = r[ok], c[ok]
        n_rows += int(ok.sum())
        for field, col in PRICE_FIELDS.items():
            v = df[col].to_numpy(dtype=np.float32, na_value=np.nan)[ok]
            panels[field][r, c] = v
    log.info("price panels filled: %d rows placed, %d rows outside index/ids skipped (%.0fs, rss %.1f GB)",
             n_rows, n_skipped, time.time() - t0, rss_gb())
    return panels


def fill_mcap_panel(chunk_paths: list[Path], dates: pd.DatetimeIndex, ids: np.ndarray, master: pd.DataFrame,
                    fx: pd.DataFrame) -> tuple[np.ndarray, dict]:
    """marketCap_USD = ciqmarketcap.marketcap * 1e6 * fx_USD(=1) / fx_primaryCurrency(date), snap 6."""
    T, N = len(dates), len(ids)
    panel = np.full((T, N), np.nan, dtype=np.float32)
    items = master[["tradingItemId", "companyId", "primaryCurrencyId"]].copy()
    items["col"] = pd.Index(ids).get_indexer(items.tradingItemId)
    fx_map = fx.set_index(["currencyid", "pricedate"]).fx if len(fx) else pd.Series(dtype=float)
    stats = {"rows": 0, "nonusd_rows": 0, "nonusd_missing_fx": 0}
    t0 = time.time()
    for p in chunk_paths:
        df = pq.read_table(p).to_pandas()
        df = df[df.marketcap.notna()]
        df = df.merge(items, left_on="companyid", right_on="companyId", how="inner")
        if df.empty:
            continue
        usd = df.marketcap.to_numpy(dtype=np.float64) * 1e6
        ccy = df.primaryCurrencyId.fillna(USD).astype(int).to_numpy()
        non = ccy != USD
        stats["rows"] += len(df)
        if non.any() and len(fx_map):
            key = pd.MultiIndex.from_arrays([ccy[non], to_ns(df.pricingdate.to_numpy()[non])])
            rate = fx_map.reindex(key).to_numpy()
            stats["nonusd_rows"] += int(non.sum())
            stats["nonusd_missing_fx"] += int(np.isnan(rate).sum())
            usd[non] = usd[non] / rate
        elif non.any():
            usd[non] = np.nan
            stats["nonusd_rows"] += int(non.sum())
            stats["nonusd_missing_fx"] += int(non.sum())
        r = dates.get_indexer(to_ns(df.pricingdate.to_numpy()))
        c = df.col.to_numpy()
        ok = (r >= 0) & (c >= 0)
        panel[r[ok], c[ok]] = usd[ok].astype(np.float32)
    log.info("marketCap panel filled: %s (%.0fs)", stats, time.time() - t0)
    return panel, stats


def fill_divadj_panel(divadj: pd.DataFrame, dates: pd.DatetimeIndex, ids: np.ndarray) -> np.ndarray:
    """Daily divAdjFactor from [fromdate, todate] windows (todate NULL = open)."""
    T, N = len(dates), len(ids)
    panel = np.full((T, N), np.nan, dtype=np.float32)
    col = pd.Index(ids).get_indexer(divadj.tradingitemid.to_numpy())
    fr = dates.searchsorted(to_ns(divadj.fromdate.to_numpy()), side="left")
    todate = to_ns(divadj.todate.to_numpy())
    to = np.full(len(divadj), T, dtype=np.int64)               # open window -> to the end
    has_to = ~todate.isna()
    to[has_to] = dates.searchsorted(todate[has_to], side="right")
    vals = divadj.divadjfactor.to_numpy(dtype=np.float32)
    n_overlap = 0
    for j in range(len(divadj)):
        if col[j] < 0 or fr[j] >= to[j]:
            continue
        seg = panel[fr[j]:to[j], col[j]]
        n_overlap += int(np.isfinite(seg).sum())
        seg[:] = vals[j]
    log.info("divAdjFactor panel: %d windows, %d overlapping cells overwritten", len(divadj), n_overlap)
    return panel


# --------------------------------------------------------------------------------------
# Universe rule (port of sim/modules/TopUniverse_uss_v1.py; lf.ts_* = rolling with min_periods=1,
# NaN skipped; lf.cs_rank = average-rank scaled to [0, 1])
# --------------------------------------------------------------------------------------
def rolling_mean(x: np.ndarray, w: int) -> np.ndarray:
    return pd.DataFrame(x).rolling(w, min_periods=1).mean().to_numpy(dtype=np.float64)


def rolling_median(x: np.ndarray, w: int) -> np.ndarray:
    return pd.DataFrame(x).rolling(w, min_periods=1).median().to_numpy(dtype=np.float64)


def cs_rank_rows(score: np.ndarray) -> np.ndarray:
    """cpf.cs_rank: per row, finite values get (avg 0-based rank)/(n-1); n==1 -> 0.5; NaN stays NaN."""
    out = np.full(score.shape, np.nan, dtype=np.float64)
    for t in range(score.shape[0]):
        row = score[t]
        idx = np.flatnonzero(np.isfinite(row))
        n = idx.size
        if n == 0:
            continue
        if n == 1:
            out[t, idx] = 0.5
            continue
        v = row[idx]
        order = np.argsort(v, kind="stable")
        sv = v[order]
        new = np.r_[True, sv[1:] != sv[:-1]]
        grp = np.cumsum(new) - 1
        starts = np.flatnonzero(new)
        ends = np.r_[starts[1:], n] - 1
        out[t, idx[order]] = (starts[grp] + ends[grp]) / 2.0 / (n - 1)
    return out


def make_sticky(univ: np.ndarray, exit_days: int, entry_days: int) -> np.ndarray:
    """univ.where(univ).ffill(limit=exit).fillna(False); keep runs longer than entry_days."""
    s = univ.copy()
    for k in range(1, exit_days + 1):
        s[k:] |= univ[:-k]
    cs = np.cumsum(s, axis=0, dtype=np.int32)
    last_reset = np.maximum.accumulate(np.where(s, 0, cs), axis=0)
    days = cs - last_reset
    return s & (days > entry_days)


def build_universe(close, volume, mcap, prim, ids: np.ndarray, tokens: set[int], allowed: np.ndarray | None,
                   params: dict) -> dict[str, np.ndarray]:
    """Returns {name: bool (T,N)}. `allowed` (bool N) restricts the candidate set (None = all)."""
    t0 = time.time()
    if allowed is not None:
        close = np.where(allowed[None, :], close, np.nan)
        volume = np.where(allowed[None, :], volume, np.nan)
        mcap = np.where(allowed[None, :], mcap, np.nan)
    dollar = rolling_mean(close, params["DollarValueWindow"]) * rolling_mean(volume, params["DollarValueWindow"])
    dvv = dollar > params["MinSpotDollarVolume"]
    del dollar
    dv = mcap.astype(np.float64)
    med_f = rolling_median(dv, params["FilterWindow"])
    med_r = med_f if params["RankWindow"] == params["FilterWindow"] else rolling_median(dv, params["RankWindow"])
    sdv = np.nan_to_num(prim, nan=0.0) > 0            # pandas: bool & float -> NaN acts as False
    dvol_ok = (med_f > params["MinDollarMarketCap"]) & sdv & dvv
    sel = (dv > 0) & dvol_ok
    liq = cs_rank_rows(np.where(sel, med_r, np.nan))
    del med_f, med_r, dvv, dvol_ok, sel
    dv_pos = dv > 0
    token_cols = np.isin(ids, list(tokens))
    out = {}
    univ = np.isfinite(liq)
    u = make_sticky(univ, params["StickyExitDays"], params["StickyEntryDays"]) & dv_pos
    u[:, token_cols] = False
    out["ALL"] = u
    n_fin = np.isfinite(liq).sum(axis=1)
    for name in UNIVERSE_NAMES[1:]:
        usize = int(name[3:])
        thr = np.maximum(0, n_fin + 1 - usize) / (n_fin + 1)
        univ = liq > thr[:, None]
        u = make_sticky(univ, params["StickyExitDays"], params["StickyEntryDays"]) & dv_pos
        u[:, token_cols] = False
        out[name] = u
    log.info("universe built (%s): %s (%.0fs, rss %.1f GB)", "all items" if allowed is None else "inV1Rule items",
             {k: int(np.median(v.sum(axis=1)[-2000:])) for k, v in out.items()}, time.time() - t0, rss_gb())
    return out


def overlap_vs_v1(v2: dict[str, np.ndarray], v2_v1rule: dict[str, np.ndarray], dates: pd.DatetimeIndex,
                  ids: np.ndarray, since: str = "2010-01-01") -> pd.DataFrame:
    root = V1_SNAPSHOT if (V1_SNAPSHOT / "TopUniverseV1" / "Top500.parquet").exists() else V1_LIVE
    rows = []
    id_index = pd.Index(ids)
    sel_t = dates >= pd.Timestamp(since)
    for name in UNIVERSE_NAMES:
        p = root / "TopUniverseV1" / f"{name}.parquet"
        if not p.exists():
            continue
        m1 = pd.read_parquet(p)
        m1.index = pd.to_datetime(m1.index)
        m1 = m1.loc[(m1.index >= pd.Timestamp(since)) & m1.index.isin(dates[sel_t])]
        common_dates = m1.index
        r = dates.get_indexer(common_dates)
        v1_cols = np.asarray(m1.columns, dtype=np.int64)
        c = id_index.get_indexer(v1_cols)
        in_v2 = c >= 0
        a1 = m1.to_numpy(dtype=bool)
        v1_n = a1.sum(axis=1)
        v1_outside = a1[:, ~in_v2].sum(axis=1)          # v1 names that v2 does not carry at all
        for label, mv in (("v2", v2), ("v2_v1rule", v2_v1rule)):
            a2 = mv[name][r]
            a2_on_v1 = a2[:, c[in_v2]]
            both = (a1[:, in_v2] & a2_on_v1).sum(axis=1)
            v2_n = a2.sum(axis=1)
            rows.append({
                "mask": name, "variant": label, "days": len(common_dates),
                "v1_names_med": float(np.median(v1_n)), "v2_names_med": float(np.median(v2_n)),
                "v1_names_not_in_v2_columns_med": float(np.median(v1_outside)),
                "overlap_frac_of_v1_med": float(np.median(both / np.maximum(v1_n, 1))),
                "overlap_frac_of_v2_med": float(np.median(both / np.maximum(v2_n, 1))),
                "jaccard_pooled": float(both.sum() / max((a1.sum() + a2.sum() - both.sum()), 1)),
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# Dividends
# --------------------------------------------------------------------------------------
def build_dividends(div: pd.DataFrame, fx: pd.DataFrame, master: pd.DataFrame, adj_ffill: pd.DataFrame,
                    dates: pd.DatetimeIndex, ids: np.ndarray, start: str, last_date: pd.Timestamp):
    """Long table with USD and cache-unit amounts; wide panels on a calendar-day index (like v1)."""
    d = div.copy()
    for c in ("exdate", "paydate", "recorddate", "announceddate"):
        d[c] = pd.to_datetime(d[c])
    d = d.merge(master[["tradingItemId", "companyName", "symbol"]], left_on="tradingitemid",
                right_on="tradingItemId", how="left").drop(columns=["tradingItemId"])
    # FX on ex-date (snap 6); USD = 1; non-USD: last rate on/before ex-date within 10 days
    d["fxRate"] = np.where(d.currencyid == USD, 1.0, np.nan)
    non = d.currencyid.notna() & (d.currencyid != USD) & d.exdate.notna()
    if non.any() and len(fx):
        sub = d.loc[non, ["dividendid", "currencyid", "exdate"]].sort_values("exdate")
        sub["currencyid"] = sub.currencyid.astype(int)
        f = fx.sort_values("pricedate").copy()
        f["currencyid"] = f.currencyid.astype(int)
        f["pricedate"] = pd.to_datetime(f.pricedate)
        m = pd.merge_asof(sub, f, left_on="exdate", right_on="pricedate", by="currencyid",
                          direction="backward", tolerance=pd.Timedelta("10D"))
        d.loc[non, "fxRate"] = m.set_index("dividendid").fx.reindex(d.loc[non, "dividendid"]).to_numpy()
    d["divAmount_USD"] = d.divamount / d.fxRate
    # adjustment factor on ex-date: value at the last index date <= exdate, forward-filled per item
    col = pd.Index(ids).get_indexer(d.tradingitemid.to_numpy())
    pos = dates.searchsorted(d.exdate.fillna(pd.Timestamp("1900-01-01")).to_numpy(), side="right") - 1
    ok = (col >= 0) & (pos >= 0) & d.exdate.notna().to_numpy()
    adj = np.full(len(d), np.nan, dtype=np.float64)
    adj_arr = adj_ffill.to_numpy()
    adj[ok] = adj_arr[pos[ok], col[ok]]
    d["adjustmentFactorAtEx"] = adj
    d["divAmountCacheUnit"] = d.divAmount_USD / d.adjustmentFactorAtEx
    d["isCash"] = d.dividendtypeid.isin(CASH_DIV_TYPES)
    d["isStock"] = d.dividendtypeid.isin(STOCK_DIV_TYPES)
    d["inRange"] = (d.exdate >= pd.Timestamp(start)) & (d.exdate <= last_date)
    d = d.rename(columns={"tradingitemid": "tradingItemId", "dividendid": "dividendId", "exdate": "exDate",
                          "paydate": "payDate", "recorddate": "recordDate", "announceddate": "announcedDate",
                          "divamount": "divAmount", "netamount": "netAmount", "grossamount": "grossAmount",
                          "currencyid": "currencyId", "exchangeid": "exchangeId", "divfreqtypeid": "divFreqTypeId",
                          "dividendtypeid": "dividendTypeId", "dividendtypename": "dividendTypeName",
                          "supplementaltypeid": "supplementalTypeId", "supplementaltypename": "supplementalTypeName"})
    d = d.sort_values(["tradingItemId", "exDate", "dividendId"]).reset_index(drop=True)

    cal = pd.date_range(pd.Timestamp(start), last_date, freq="D")
    cash = d[d.isCash & d.inRange]
    stats = {
        "rows_total": int(len(d)), "rows_in_range": int(d.inRange.sum()), "cash_rows_in_range": int(len(cash)),
        "cash_rows_missing_fx": int(cash.fxRate.isna().sum()),
        "cash_rows_missing_adjfactor": int(cash.adjustmentFactorAtEx.isna().sum()),
        "cash_rows_nonusd": int((cash.currencyId != USD).sum()),
        "cash_exdates_not_trading_days": int((~cash.exDate.isin(dates)).sum()),
        "by_type_in_range": {str(k): int(v) for k, v in d[d.inRange].groupby("dividendTypeId").size().items()},
        "usd_sum_by_type_in_range": {str(k): float(v) for k, v in d[d.inRange].groupby("dividendTypeId").divAmount_USD.sum().items()},
    }
    tt = d[d.inRange].groupby("dividendTypeId").agg(name=("dividendTypeName", "first"), rows=("dividendId", "size"),
                                                    items=("tradingItemId", "nunique"), usd_sum=("divAmount_USD", "sum")).reset_index()
    tt["isCash"] = tt.dividendTypeId.isin(CASH_DIV_TYPES)
    tt["usd_sum"] = tt.usd_sum.round(1)
    stats["type_table"] = tt.to_dict(orient="records")
    panels = {}
    for field in ("divAmount_USD", "divAmountCacheUnit"):
        g = cash.groupby(["exDate", "tradingItemId"])[field].sum(min_count=1)
        arr = np.full((len(cal), len(ids)), np.nan, dtype=np.float32)
        r = cal.get_indexer(g.index.get_level_values(0))
        c = pd.Index(ids).get_indexer(g.index.get_level_values(1))
        okk = (r >= 0) & (c >= 0)
        arr[r[okk], c[okk]] = g.to_numpy(dtype=np.float32)[okk]
        panels[field] = arr
    log.info("dividends: %s", {k: v for k, v in stats.items() if not isinstance(v, dict)})
    return d, panels, cal, stats


# --------------------------------------------------------------------------------------
# Coverage and close parity against the v1 snapshot (no DB needed)
# --------------------------------------------------------------------------------------
def v1_coverage_check(close: np.ndarray, dates: pd.DatetimeIndex, ids: np.ndarray, master: pd.DataFrame,
                      out_root: Path, since: str = "2010-01-01") -> dict:
    root = V1_SNAPSHOT if (V1_SNAPSHOT / "BaseData" / "close.parquet").exists() else V1_LIVE
    t0 = time.time()
    c1 = pd.read_parquet(root / "BaseData" / "close.parquet")
    c1.index = pd.to_datetime(c1.index)
    c1 = c1.loc[(c1.index >= dates[0]) & (c1.index <= dates[-1])]
    a1 = c1.to_numpy(dtype=np.float64)
    fin1 = np.isfinite(a1)
    v1_cols = np.asarray(c1.columns, dtype=np.int64)
    live = fin1.any(axis=0)
    v1_live = v1_cols[live]
    id_index = pd.Index(ids)
    only_v1 = np.setdiff1d(v1_live, ids)
    only_v2 = np.setdiff1d(ids, v1_live)
    res = {"v1_reference": str(root), "v1_columns": int(len(v1_cols)), "v1_columns_with_close_in_range": int(live.sum()),
           "v1_items_not_in_v2": int(len(only_v1)), "v2_items_not_in_v1": int(len(only_v2))}
    # v1-only items: how much do they matter (v1 universe membership since `since`)
    pos1 = pd.Index(v1_cols).get_indexer(only_v1)
    n_rows = fin1[:, pos1].sum(axis=0)
    last1 = np.where(n_rows > 0, len(c1) - 1 - fin1[::-1][:, pos1].argmax(axis=0), 0)
    v1only = pd.DataFrame({"tradingItemId": only_v1, "v1_close_rows": n_rows,
                           "v1_last_close": [c1.index[i].strftime("%Y-%m-%d") if n > 0 else "" for i, n in zip(last1, n_rows)]})
    for name in ("Top500", "ALL"):
        p = root / "TopUniverseV1" / f"{name}.parquet"
        if p.exists():
            m1 = pd.read_parquet(p)
            m1.index = pd.to_datetime(m1.index)
            m1 = m1.loc[(m1.index >= pd.Timestamp(since)) & (m1.index <= dates[-1])]
            cols = [c for c in only_v1 if c in m1.columns]
            days = m1[cols].sum(axis=0) if cols else pd.Series(dtype=int)
            v1only[f"v1_{name}_days_since_{since[:4]}"] = v1only.tradingItemId.map(days).fillna(0).astype(int)
            res[f"v1_only_items_in_v1_{name}_since_{since[:4]}"] = int((days > 0).sum())
            res[f"v1_only_member_days_{name}_since_{since[:4]}"] = int(days.sum())
            res[f"v1_{name}_member_days_since_{since[:4]}"] = int(m1.to_numpy(dtype=bool).sum())
    v1only["v1_close_rows"] = v1only.v1_close_rows.astype(int)
    v1only.to_csv(out_root / "Master" / "v1_only_items.csv", index=False)
    mm = master.set_index("tradingItemId")
    v2only = mm.loc[only_v2, ["symbol", "companyName", "exchangeId", "inV1Rule", "everMainExchange", "firstPriceDate",
                              "lastPriceDate", "nPriceRows", "currentPrimary"]].reset_index()
    v2only.to_csv(out_root / "Master" / "v2_only_items.csv", index=False)
    res["v2_only_by_exchange"] = {int(k): int(v) for k, v in v2only.exchangeId.value_counts().head(8).items()}
    res["v2_only_inV1Rule"] = int(v2only.inV1Rule.sum())
    # close parity on common cells since `since`
    common = np.intersect1d(v1_cols, ids)
    sel_t = c1.index >= pd.Timestamp(since)
    r2 = dates.get_indexer(c1.index[sel_t])
    b1 = a1[sel_t][:, pd.Index(v1_cols).get_indexer(common)]
    b2 = close[r2][:, id_index.get_indexer(common)].astype(np.float64)
    both = np.isfinite(b1) & np.isfinite(b2)
    rel = np.abs(b1 / b2 - 1)
    res["parity_since"] = since
    res["cells_both"] = int(both.sum())
    res["cells_v1_only"] = int((np.isfinite(b1) & ~np.isfinite(b2)).sum())
    res["cells_v2_only"] = int((~np.isfinite(b1) & np.isfinite(b2)).sum())
    for thr in (1e-4, 1e-2, 0.2):
        k = int((both & (rel > thr)).sum())
        res[f"cells_reldiff_gt_{thr:g}"] = k
        res[f"pct_reldiff_gt_{thr:g}"] = round(100 * k / max(both.sum(), 1), 3)
    p = root / "TopUniverseV1" / "Top500.parquet"
    if p.exists():
        m1 = pd.read_parquet(p)
        m1.index = pd.to_datetime(m1.index)
        m1 = m1.reindex(index=c1.index[sel_t], columns=common).fillna(False).to_numpy(dtype=bool)
        bb = both & m1
        res["top500_cells"] = int(m1.sum())
        res["top500_cells_reldiff_gt_0.01"] = int((bb & (rel > 1e-2)).sum())
        res["top500_pct_reldiff_gt_0.01"] = round(100 * (bb & (rel > 1e-2)).sum() / max(bb.sum(), 1), 3)
        res["top500_cells_reldiff_gt_0.2"] = int((bb & (rel > 0.2)).sum())
        idx = np.argwhere(bb & (rel > 0.2))[:6]
        res["top500_examples_date_id_v1_v2"] = [(c1.index[sel_t][i].strftime("%Y-%m-%d"), int(common[j]), round(float(b1[i, j]), 4),
                                                round(float(b2[i, j]), 4)) for i, j in idx]
    log.info("v1 coverage/parity check (%.0fs): %s", time.time() - t0, {k: v for k, v in res.items() if not isinstance(v, (dict, list))})
    return res


# --------------------------------------------------------------------------------------
# Master
# --------------------------------------------------------------------------------------
def finalize_master(cand: pd.DataFrame, close: np.ndarray, dates: pd.DatetimeIndex, ids: np.ndarray) -> pd.DataFrame:
    fin = np.isfinite(close)
    n = fin.sum(axis=0)
    first = np.where(n > 0, fin.argmax(axis=0), -1)
    last = np.where(n > 0, len(dates) - 1 - fin[::-1].argmax(axis=0), -1)
    stats = pd.DataFrame({"tradingItemId": ids, "nPriceRows": n.astype(np.int64),
                          "firstPriceDate": pd.to_datetime(np.where(first >= 0, dates.values[np.maximum(first, 0)], np.datetime64("NaT"))),
                          "lastPriceDate": pd.to_datetime(np.where(last >= 0, dates.values[np.maximum(last, 0)], np.datetime64("NaT")))})
    sector = pd.read_csv(SECTOR_CSV).drop_duplicates("simpleindustrydescription")
    c = cand.set_index("tradingitemid").reindex(ids).reset_index()
    m = pd.DataFrame({
        "tradingItemId": ids.astype(np.int64),
        "companyId": c.companyid.astype("int64"),
        "companyName": c.companyname,
        "symbol": c.symbol,
        "exchangeId": c.exchangeid.astype("Int64"),
        "exchangeHistory": c.exchangehistory,
        "gvkey": c.gvkey,
        "iid": c.iid,
        "securityId": c.securityid.astype("int64"),
        "primaryFlag": c.primaryflag.astype("Int64"),
        "primaryFlagTi": c.primaryflagti.astype("Int64"),
        "currentPrimary": c.currentprimary.astype(bool),
        "ISOCode": c.isocode,
        "simpleIndustryDescription": c.simpleindustrydescription,
    })
    m = m.merge(sector.rename(columns={"simpleindustrydescription": "simpleIndustryDescription"}),
                on="simpleIndustryDescription", how="left")
    m = m.merge(stats, on="tradingItemId", how="left")
    if len(m) != len(ids):
        raise RuntimeError("master row count changed by the sector/stats merges")
    m["inV1Rule"] = c.inv1rule.astype(bool).to_numpy()
    m["everMainExchange"] = c.evermainexchange.astype(bool).to_numpy()
    m["inV1Cache"] = c.inv1cache.astype(bool).to_numpy()
    m["securityStartDate"] = pd.to_datetime(c.securitystartdate)
    m["securityEndDate"] = pd.to_datetime(c.securityenddate)
    # extras (not in the contract list; documented in README)
    m["everMainCommonUSD"] = c.evermaincommonusd.astype(bool).to_numpy()
    m["tiCurrencyId"] = c.ticurrencyid.astype("Int64")
    m["tradingItemStatusId"] = c.tradingitemstatusid.astype("Int64")
    m["securitySubTypeId"] = c.securitysubtypeid.astype("Int64")
    m["securitySubTypeName"] = c.securitysubtypename
    m["primaryTradingItemId"] = c.primarytradingitemid.astype("Int64")
    m["primaryCurrencyId"] = c.currencyid.astype("Int64")
    order = ["tradingItemId", "companyId", "companyName", "symbol", "exchangeId", "exchangeHistory", "gvkey", "iid",
             "securityId", "primaryFlag", "primaryFlagTi", "currentPrimary", "ISOCode", "sector",
             "simpleIndustryDescription", "firstPriceDate", "lastPriceDate", "nPriceRows", "inV1Rule",
             "everMainExchange", "inV1Cache", "securityStartDate", "securityEndDate",
             "everMainCommonUSD", "tiCurrencyId", "tradingItemStatusId", "securitySubTypeId", "securitySubTypeName",
             "primaryTradingItemId", "primaryCurrencyId"]
    return m[order]


# --------------------------------------------------------------------------------------
# Assertions / report / manifest / README
# --------------------------------------------------------------------------------------
def run_assertions(close: np.ndarray, dates: pd.DatetimeIndex, ids: np.ndarray, div_usd: np.ndarray,
                   cal: pd.DatetimeIndex, master: pd.DataFrame, v1_cols: set[int]) -> list[dict]:
    id_index = pd.Index(ids)
    res = []

    def cell(arr, index, tid, day):
        c = id_index.get_loc(tid) if tid in id_index else -1
        r = index.get_indexer([pd.Timestamp(day)])[0]
        if c < 0 or r < 0:
            return np.nan
        return float(arr[r, c])

    for sym, tid, day, exp in ASSERT_CLOSE:
        got = cell(close, dates, tid, day)
        res.append({"check": f"{sym} {tid} close {day}", "expected": exp, "actual": got,
                    "ok": bool(np.isfinite(got) and abs(got - exp) <= 1e-5 * max(1.0, abs(exp)))})
    for sym, tid, day, exp in ASSERT_DIV:
        got = cell(div_usd, cal, tid, day)
        res.append({"check": f"{sym} {tid} divAmount_USD {day}", "expected": exp, "actual": got,
                    "ok": bool(np.isfinite(got) and abs(got - exp) <= 1e-6)})
    # SIVB.Q: closes in 2022, present in v2, absent from v1
    if SIVB_ID in id_index:
        c = id_index.get_loc(SIVB_ID)
        sel = (dates >= "2022-01-01") & (dates <= "2022-12-31")
        n = int(np.isfinite(close[sel, c]).sum())
    else:
        n = -1
    res.append({"check": f"SIVB.Q {SIVB_ID} closes in 2022 (count>0) and not in v1", "expected": ">0 & absent in v1",
                "actual": f"{n} closes; in v1={SIVB_ID in v1_cols}", "ok": bool(n > 0 and SIVB_ID not in v1_cols)})
    # FRCB: First Republic 2010 item via companyId
    frc = master[(master.companyId == FRC_COMPANY) & (master.securityStartDate >= "2010-01-01")]
    best, n = None, -1
    for tid in frc.tradingItemId:
        c = id_index.get_loc(tid)
        sel = (dates >= "2023-03-01") & (dates <= "2023-03-31")
        k = int(np.isfinite(close[sel, c]).sum())
        if k > n:
            best, n = int(tid), k
    res.append({"check": f"FRCB (companyId {FRC_COMPANY}, 2010 security) present with 2023-03 closes",
                "expected": ">0", "actual": f"item {best}: {n} closes in 2023-03 (symbols {frc.symbol.tolist()})",
                "ok": bool(n > 0)})
    return res


def write_readme(out_root: Path, info: dict):
    text = f"""# cache_v2_cash_1d

Single-basis US cash-equity daily cache built from one snapshot of the xfeed (CIQ) database on
{info['build_time']} (DB max pricingdate {info['db_max_pricingdate']}, last complete date used
{info['last_date']}).  Built by `longshort_platform/cpp-engine/tools/data/build_cache_v2.py`.
Not a replacement of the team's cache_v1; a parallel, corrected copy for research.

## Layout (all wide panels: index = trading dates {info['start']}..{info['last_date']} (T={info['T']}),
## columns = tradingItemId int64 (N={info['N']}, sorted ascending, same order in every file)

- `Master/master.parquet` - one row per tradingItemId: identity (companyId, companyName, symbol,
  exchangeId = *current* exchange, exchangeHistory = JSON list of Compustat EXCHG windows, gvkey/iid,
  securityId, primaryFlag, primaryFlagTi, currentPrimary, ISOCode = currency of the company's primary
  trading item, sector/simpleIndustryDescription), coverage (firstPriceDate, lastPriceDate,
  nPriceRows), flags (inV1Rule = passes the v1 current-exchange rule; everMainExchange = EXCHG in
  {{11,12,14}} at any time; inV1Cache = column exists in v1 close.parquet), securityStart/EndDate,
  plus extras (everMainCommonUSD, tiCurrencyId, tradingItemStatusId, securitySubTypeId/Name,
  primaryTradingItemId, primaryCurrencyId).
- `Master/build_report.md`, `Master/build.log` - build report and full log.
- `BaseData/open,high,low,close,volume,VWAP,mid,bid,ask` - `ciqpriceequity` fields as of the snapshot,
  i.e. split-adjusted onto the *current* share basis (vendor restatement). float32. NaN = no row.
- `BaseData/midPrice` = (open+high+low+close)/4 (NaN if any leg missing).
- `BaseData/adjustmentFactor` - as-traded multiplier: asTraded = close * adjustmentFactor.
- `BaseData/divAdjFactor` - daily value of `ciqpriceequitydivadjfactor` windows (cumulative factor
  that includes cash dividends and spin-offs); NaN outside any window.
- `BaseData/marketCap_USD` = `ciqmarketcap.marketcap * 1e6 * fx(USD)/fx(primary currency)` on the
  date (snap 6), masked to days with a close (v1 semantics: market cap only where a price row exists).
- `BaseData/currentPrimary` - 1.0/0.0 on days with a close, NaN elsewhere (v1 semantics):
  primaryFlag==1 & primaryFlagTi==1 & companyName not matching 'Trust|ETF'.
- `BaseData/TimeStamp.parquet`, `BaseData/tradingItemId.parquet` - index and column lists like v1.
- `DivData/divAmount_USD.parquet` - cash dividend per share on ex-date, as paid, USD (types
  {list(CASH_DIV_TYPES)}), summed per (ex-date, item). Index = calendar days {info['start']}..{info['last_date']}
  (like v1's DivData), so ex-dates on non-trading days are kept.
- `DivData/divAmountCacheUnit.parquet` = divAmount_USD / adjustmentFactor on the ex-date (previous
  available factor if the ex-date has no price row) -> amount per *current-basis* share.
- `DivData/dividends_long.parquet` - every `ciqdividend` row for the symbol set with type names,
  fxRate, divAmount_USD, adjustmentFactorAtEx, divAmountCacheUnit, isCash/isStock/inRange.
- `TopUniverseV2/ALL,Top1000,...,Top10` - v1 universe rule (TopUniverse_uss_v1.py + v1 METADATA
  parameters, same TokensToRemove) applied to v2 data. bool panels.
- `TopUniverseV2_v1rule/...` - same rule with candidates restricted to inV1Rule items (apples to
  apples with v1). Columns are the full N; non-inV1Rule columns are always False.
- `TopUniverseV2/overlap_vs_v1.csv|md` - per-mask overlap with the v1 snapshot masks, 2010+.
- `MANIFEST.json` - build metadata, counts, parameters, per-file sha256/rows/cols.

## Symbol set
(inV1Rule) UNION (everMainExchange & Common Stock & USD trading item), keeping items with >= 1 close
in range.  Funds/ETFs are not removed here (as in v1 the universe rule's currentPrimary does that).

## Known limits
- Prices are the vendor's current restatement: split-adjusted, NOT adjusted for cash dividends or
  spin-offs (use divAdjFactor / DivData / Distributions for that).
- `volume` is float32 (exact up to 2^24).
- marketCap_USD for non-USD primary currencies needs an FX row on the date; missing -> NaN (v1 would
  drop the whole price row instead).
- Dividend cash types = brief list (1,4,5,7,8,10) plus the tax-code cash variants 6,15,17,18,19,23 confirmed
  by the 2026-09-14 audit; stock dividends 11/20/21/22 and codes 31/33 are excluded (see build_report.md table).
- TopUniverseV2 = v1rule universe plus formerly-listed operating companies; funds outside the v1 rule are
  screened by companyTypeId 13 / non-main exchange. Exact rule: exclude (NOT inV1Rule) AND (ciqcompany.companytypeid
  == 13 OR exchangeId in 117/467/358) from the ranking pool (`TopUniverseV2/fund_exclusions.csv`); items inside
  the v1 rule are never removed (v1 parity); currentPrimary and TopUniverseV2_v1rule are unchanged.
- Sparse dates (fewer than {info['min_day_frac']:.0%} of the surrounding daily close count) are dropped from
  the index; the list is in MANIFEST.json `dropped_dates`.
- Delisted items simply end (NaN after lastPriceDate); no settlement values here (see events/Distributions).
- Items whose company has no `ciqcompany` row in our DB copy (mostly recent listings) are kept with
  companyName/sector null (`Master/master.parquet` companyName is null); they are never inV1Rule.
"""
    (out_root / "README.md").write_text(text)


def md_table(df: pd.DataFrame, floatfmt="{:.4g}") -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in df.itertuples(index=False):
        cells = []
        for v in r:
            if isinstance(v, float):
                cells.append(floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--start", default="2000-01-03")
    ap.add_argument("--chunk-size", type=int, default=1500)
    ap.add_argument("--mcap-chunk-size", type=int, default=1500)
    ap.add_argument("--min-day-frac", type=float, default=0.2,
                    help="drop index dates whose close count is below this fraction of the local median")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--statement-timeout-min", type=int, default=120)
    ap.add_argument("--force", default="", help="comma list of steps to redo: candidates,prices,mcap,small,date_index,panels")
    ap.add_argument("--smoke", type=int, default=0, help="debug: restrict to N candidate ids (+ assertion ids)")
    ap.add_argument("--republish", default="", help="comma list of output path prefixes to rewrite (e.g. DivData/,TopUniverseV2/)")
    args = ap.parse_args(argv)

    t_start = time.time()
    pa.set_cpu_count(args.threads)
    pa.set_io_thread_count(max(2, args.threads // 2))
    out_root: Path = args.out_root
    for sub in ("Master", "BaseData", "DivData", "TopUniverseV2", "TopUniverseV2_v1rule"):
        (out_root / sub).mkdir(parents=True, exist_ok=True)
    stage = Stage(args.stage, set(s for s in args.force.split(",") if s))

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.FileHandler(out_root / "Master" / "build.log"), logging.FileHandler(stage.root / "build.log"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.info("==== build_cache_v2 start: out=%s stage=%s start=%s force=%s", out_root, stage.root, args.start, sorted(stage.force))

    # cross-check universe parameters with the v1 METADATA
    meta = load_v1_univ_params()
    for k, v in UNIV_PARAMS.items():
        if k in meta and float(meta[k]) != float(v):
            raise RuntimeError(f"universe parameter {k} differs from v1 METADATA: {meta[k]} vs {v}")
    if "TokensToRemove" in meta and set(int(x) for x in meta["TokensToRemove"]) != set(TOKENS_TO_REMOVE):
        raise RuntimeError("TokensToRemove differ from v1 METADATA")

    db = DB(args.statement_timeout_min * 60_000)
    db_max = db.query(f"select max(pricingdate) as mx from {SCHEMA}.ciqpriceequity").mx[0]
    db_max = pd.Timestamp(db_max).strftime("%Y-%m-%d")
    log.info("DB max(pricingdate) = %s", db_max)

    # 1) candidates
    cand = step_candidates(db, stage)
    cand_ids = cand.tradingitemid.astype(int).tolist()
    if args.smoke:
        must = {t for _, t, _, _ in ASSERT_CLOSE} | {t for _, t, _, _ in ASSERT_DIV} | {SIVB_ID}
        must |= set(cand.loc[cand.companyid == FRC_COMPANY, "tradingitemid"].astype(int))
        cand_ids = sorted(set(cand_ids[: args.smoke]) | must)
        cand = cand[cand.tradingitemid.isin(cand_ids)].reset_index(drop=True)
        log.info("SMOKE MODE: %d ids", len(cand_ids))
    log.info("candidates: %d (inV1Rule %d, everMainExchange %d, everMainCommonUSD %d, inV1Cache %d)",
             len(cand), int(cand.inv1rule.sum()), int(cand.evermainexchange.sum()),
             int(cand.evermaincommonusd.sum()), int(cand.inv1cache.sum()))

    # 2) prices
    chunk_paths = step_pull_prices(db, stage, cand_ids, args.start, args.chunk_size)

    # 3) date index and the ids that have at least one close
    dinfo = step_date_index(stage, chunk_paths, args.start, args.min_day_frac)
    dates = pd.DatetimeIndex(pd.to_datetime(dinfo["dates"]))
    last_date = pd.Timestamp(dinfo["last_date"])
    per_id = pd.read_parquet(stage.path("per_id_close_rows.parquet")).set_index("tradingitemid").n
    ids = np.array(sorted(set(cand_ids) & set(per_id[per_id > 0].index.astype(int))), dtype=np.int64)
    log.info("items with >= 1 close row: %d of %d candidates; T=%d N=%d", len(ids), len(cand_ids), len(dates), len(ids))

    # 4) market cap, small tables
    cand_kept = cand[cand.tradingitemid.isin(ids)]
    company_ids = sorted(set(cand_kept.companyid.astype(int)))
    mcap_paths = step_pull_mcap(db, stage, company_ids, args.start, args.mcap_chunk_size)
    currencies = set(cand_kept.currencyid.dropna().astype(int))
    small = step_pull_small(db, stage, ids.tolist(), currencies, args.start)
    ctypes = step_company_types(db, stage, company_ids)
    db.close()
    log.info("DB session closed after %d statements", db.n_queries)

    # 5) panels
    pub = Publisher(stage, out_root, keep_local=set())
    for prefix in [x for x in args.republish.split(",") if x]:
        for rel in [r for r in pub.entries if r.startswith(prefix)]:
            (stage.root / "manifest" / (rel.replace("/", "__") + ".json")).unlink(missing_ok=True)
            del pub.entries[rel]
            log.info("will republish %s", rel)
    panels = fill_price_panels(chunk_paths, dates, ids)
    close = panels["close"]
    fin = np.isfinite(close)
    zero = np.flatnonzero(fin.sum(axis=0) == 0)
    if len(zero):
        log.warning("%d items have rows but no close inside the index; dropping them", len(zero))
        keep = np.setdiff1d(np.arange(len(ids)), zero)
        ids = ids[keep]
        panels = {k: np.ascontiguousarray(v[:, keep]) for k, v in panels.items()}
        close = panels["close"]
        fin = np.isfinite(close)
    master = finalize_master(cand, close, dates, ids)
    master_prim = master.set_index("tradingItemId").currentPrimary.reindex(ids).to_numpy(dtype=bool)
    prim = np.where(fin, master_prim[None, :].astype(np.float32), np.float32(np.nan))
    panels["midPrice"] = (panels["open"] + panels["high"] + panels["low"] + panels["close"]) / np.float32(4)
    panels["currentPrimary"] = prim
    mcap, mcap_stats = fill_mcap_panel(mcap_paths, dates, ids, master, small["fx"])
    n_mcap_masked = int((np.isfinite(mcap) & ~fin).sum())
    mcap[~fin] = np.nan
    panels["marketCap_USD"] = mcap
    panels["divAdjFactor"] = fill_divadj_panel(small["divadj"], dates, ids)
    log.info("all panels in memory, rss %.1f GB", rss_gb())

    keep_fields = {"close", "volume", "marketCap_USD", "currentPrimary", "adjustmentFactor"}
    for field in BASE_FIELD_ORDER:
        arr = panels[field]
        pub.publish_table(f"BaseData/{field}.parquet", pa.Table.from_pandas(panel_frame(arr, dates, ids, "tradingItemId"),
                                                                            preserve_index=True), len(dates), len(ids))
        if field not in keep_fields:
            del panels[field]
    pub.publish_df("BaseData/TimeStamp.parquet", pd.DataFrame({"TimeStamp": dates}), preserve_index=False)
    pub.publish_df("BaseData/tradingItemId.parquet", pd.DataFrame({"tradingItemId": ids}), preserve_index=True)
    pub.publish_df("Master/master.parquet", master, preserve_index=False)

    # 6) dividends
    adj_ffill = pd.DataFrame(panels["adjustmentFactor"]).ffill()
    div_long, div_panels, cal, div_stats = build_dividends(small["dividends_raw"], small["fx"], master, adj_ffill,
                                                          dates, ids, args.start, last_date)
    del adj_ffill, panels["adjustmentFactor"]
    for field, arr in div_panels.items():
        pub.publish_table(f"DivData/{field}.parquet", pa.Table.from_pandas(panel_frame(arr, cal, ids, "tradingItemId"),
                                                                           preserve_index=True), len(cal), len(ids))
    pub.publish_df("DivData/dividends_long.parquet", div_long, preserve_index=False)

    # 7) universes
    tokens = set(TOKENS_TO_REMOVE)
    mi = master.set_index("tradingItemId").reindex(ids)
    ctype = ctypes.drop_duplicates("companyid").set_index("companyid").companytypeid
    ctype_of = mi.companyId.map(ctype)
    not_v1 = ~mi.inV1Rule.astype(bool).to_numpy()
    is_fund_co = (ctype_of == FUND_COMPANY_TYPE).to_numpy() & not_v1
    is_fund_venue = mi.exchangeId.isin(FUND_EXCHANGES).to_numpy() & not_v1
    fund_like = is_fund_co | is_fund_venue
    univ_all_raw = build_universe(panels["close"], panels["volume"], panels["marketCap_USD"], panels["currentPrimary"],
                                  ids, tokens, None, UNIV_PARAMS)
    univ_all = build_universe(panels["close"], panels["volume"], panels["marketCap_USD"], panels["currentPrimary"],
                              ids, tokens, ~fund_like, UNIV_PARAMS)
    allowed = master.set_index("tradingItemId").inV1Rule.reindex(ids).to_numpy(dtype=bool)
    univ_v1 = build_universe(panels["close"], panels["volume"], panels["marketCap_USD"], panels["currentPrimary"],
                             ids, tokens, allowed, UNIV_PARAMS)
    fund_stats = {"n_fund_company": int(is_fund_co.sum()), "n_fund_venue": int(is_fund_venue.sum()),
                  "n_excluded": int(fund_like.sum()), "rule": f"(NOT inV1Rule) AND (companyTypeId == {FUND_COMPANY_TYPE} OR "
                  f"exchangeId in {FUND_EXCHANGES}); excluded items leave the ranking pool (like the v1rule variant); "
                  "items inside the v1 rule are never removed (v1 parity)",
                  "per_mask": {}}
    excl_rows = pd.DataFrame({"tradingItemId": ids[fund_like], "symbol": mi.symbol.to_numpy()[fund_like],
                              "companyName": mi.companyName.to_numpy()[fund_like], "exchangeId": mi.exchangeId.to_numpy()[fund_like],
                              "companyTypeId": ctype_of.to_numpy()[fund_like], "everMainExchange": mi.everMainExchange.to_numpy()[fund_like],
                              "reason": np.where(is_fund_co[fund_like], "public_fund_company", "fund_venue")})
    for name in UNIVERSE_NAMES:
        old, new = univ_all_raw[name], univ_all[name]
        removed_days = old[:, fund_like].sum(axis=0)
        excl_rows[f"{name}_days_before"] = removed_days
        fund_stats["per_mask"][name] = {"items_removed": int((removed_days > 0).sum()), "name_days_removed": int(removed_days.sum()),
                                        "name_days_before": int(old.sum()), "name_days_after": int(new.sum()),
                                        "names_med_2010_before": float(np.median(old[dates >= "2010-01-01"].sum(axis=1))),
                                        "names_med_2010_after": float(np.median(new[dates >= "2010-01-01"].sum(axis=1))),
                                        "v1rule_mask_contained_frac": round(float((univ_v1[name] & new).sum() / max(univ_v1[name].sum(), 1)), 4)}
    excl_rows = excl_rows.sort_values("ALL_days_before", ascending=False)
    excl_rows.to_csv(out_root / "TopUniverseV2" / "fund_exclusions.csv", index=False)
    del univ_all_raw
    log.info("fund screen (all-items variant): %s", {k: v for k, v in fund_stats.items() if k != "per_mask"})
    log.info("fund screen per mask: %s", fund_stats["per_mask"])
    for folder, masks in (("TopUniverseV2", univ_all), ("TopUniverseV2_v1rule", univ_v1)):
        for name in UNIVERSE_NAMES:
            pub.publish_table(f"{folder}/{name}.parquet", pa.Table.from_pandas(panel_frame(masks[name], dates, ids, "Symbol"),
                                                                              preserve_index=True), len(dates), len(ids))
    overlap = overlap_vs_v1(univ_all, univ_v1, dates, ids)
    overlap.to_csv(out_root / "TopUniverseV2" / "overlap_vs_v1.csv", index=False)
    (out_root / "TopUniverseV2" / "overlap_vs_v1.md").write_text(
        "# Universe overlap vs v1 snapshot (2010-01-01 .. last date, common dates)\n\n" + md_table(overlap) + "\n")
    univ_summary = {}
    sel2010 = dates >= "2010-01-01"
    for name in UNIVERSE_NAMES:
        univ_summary[name] = {"v2_names_med_2010": float(np.median(univ_all[name][sel2010].sum(axis=1))),
                              "v2_v1rule_names_med_2010": float(np.median(univ_v1[name][sel2010].sum(axis=1)))}
    del univ_all, univ_v1

    # 8) assertions
    v1_cols, _ = read_v1_columns()
    checks = run_assertions(panels["close"], dates, ids, div_panels["divAmount_USD"], cal, master, v1_cols)
    for c in checks:
        log.info("ASSERT %s: expected %s actual %s -> %s", c["check"], c["expected"], c["actual"], "OK" if c["ok"] else "FAIL")
    n_fail = sum(1 for c in checks if not c["ok"])

    # 8b) coverage / parity vs the v1 snapshot (writes Master/v1_only_items.csv, v2_only_items.csv)
    cov = v1_coverage_check(panels["close"], dates, ids, master, out_root)

    # 9) manifest, README, report
    wall = time.time() - t_start
    first_run = stage.path("first_full_run.json")
    if not first_run.exists():
        first_run.write_text(json.dumps({"at": now(), "wall_seconds": round(wall), "peak_rss_gb": round(rss_gb(), 2)}))
    first = json.loads(first_run.read_text())
    counts = {
        "candidates_union": int(len(cand)), "inV1Rule": int(master.inV1Rule.sum()),
        "everMainExchange": int(master.everMainExchange.sum()), "everMainCommonUSD": int(master.everMainCommonUSD.sum()),
        "with_prices": int(len(master)), "inV1Cache": int(master.inV1Cache.sum()),
        "currentPrimary": int(master.currentPrimary.sum()), "no_company_row": int(master.companyName.isna().sum()),
        "v2_not_in_v1cache": int((~master.inV1Cache).sum()),
        "candidates_without_prices": int(len(cand) - len(master)),
        "inV1Rule_candidates": int(cand.inv1rule.sum()), "everMainExchange_candidates": int(cand.evermainexchange.sum()),
    }
    info = {
        "build_time": now(), "db_max_pricingdate": db_max, "start": args.start, "last_date": dinfo["last_date"],
        "T": int(len(dates)), "N": int(len(ids)), "min_day_frac": args.min_day_frac,
    }
    manifest = {
        **info, "counts": counts, "wall_seconds": round(wall), "peak_rss_gb": round(rss_gb(), 2),
        "parameters": {"chunk_size": args.chunk_size, "v1_exchanges": V1_EXCHANGES, "main_exchg": MAIN_EXCHG,
                       "cash_dividend_types": CASH_DIV_TYPES, "stock_dividend_types": STOCK_DIV_TYPES,
                       "fx_snap": FX_SNAP, "universe": UNIV_PARAMS, "tokens_to_remove": TOKENS_TO_REMOVE,
                       "primary_name_pattern": PRIMARY_NAME_PATTERN},
        "dropped_dates": dinfo["dropped_dates"], "mcap_stats": mcap_stats, "n_mcap_cells_masked_no_close": n_mcap_masked,
        "dividend_stats": div_stats, "universe_summary": univ_summary, "fund_screen_all_items_variant": fund_stats,
        "assertions": checks, "assertions_failed": n_fail, "v1_coverage": cov,
        "first_full_run": first,
        "files": {k: {kk: vv for kk, vv in v.items() if kk != "path"} for k, v in sorted(pub.entries.items())},
        "v1_reference": str(V1_SNAPSHOT if V1_SNAPSHOT.exists() else V1_LIVE),
    }
    (out_root / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, default=str))
    write_readme(out_root, info)

    # compare the date index with v1 (2000+)
    v1_ts = None
    for root in (V1_SNAPSHOT, V1_LIVE):
        p = root / "BaseData" / "TimeStamp.parquet"
        if p.exists():
            v1_ts = pd.DatetimeIndex(pd.read_parquet(p).TimeStamp)
            break
    idx_note = ""
    if v1_ts is not None:
        v1_ts = v1_ts[(v1_ts >= pd.Timestamp(args.start)) & (v1_ts <= last_date)]
        only_v2 = dates.difference(v1_ts)
        only_v1 = v1_ts.difference(dates)
        idx_note = (f"- v1 index {args.start}..{dinfo['last_date']}: {len(v1_ts)} dates; v2: {len(dates)}. "
                    f"Only in v2: {len(only_v2)} {[d.strftime('%Y-%m-%d') for d in only_v2[:20]]}; "
                    f"only in v1: {len(only_v1)} {[d.strftime('%Y-%m-%d') for d in only_v1[:20]]}\n")

    rep = [f"# cache_v2 build report ({info['build_time']})", "",
           f"- Output root: `{out_root}`", f"- DB max(pricingdate): {db_max}; last complete date used: {dinfo['last_date']}; start {args.start}",
           f"- T x N = {len(dates)} x {len(ids)}; this run wall {wall/60:.1f} min, peak RSS {rss_gb():.1f} GB; "
           f"first full build {first['at']}: wall {first['wall_seconds']/60:.1f} min, peak RSS {first['peak_rss_gb']} GB",
           f"- Items: inV1Rule {counts['inV1Rule']} / everMainExchange {counts['everMainExchange']} "
           f"(everMain & Common & USD {counts['everMainCommonUSD']}) / union candidates {counts['candidates_union']} "
           f"/ with prices {counts['with_prices']} / inV1Cache {counts['inV1Cache']} / not in v1 cache {counts['v2_not_in_v1cache']} "
           f"/ currentPrimary {counts['currentPrimary']} / no ciqcompany row {counts['no_company_row']}",
           f"- Sparse dates dropped from the index: {len(dinfo['dropped_dates'])} "
           f"{list(dinfo['dropped_dates'].items())[:30]}",
           idx_note.rstrip(),
           f"- marketCap fill: {mcap_stats}; cells masked because no close that day: {n_mcap_masked}",
           f"- Dividends: {json.dumps({k: v for k, v in div_stats.items() if not isinstance(v, dict)})}",
           f"- Dividend cash types {list(CASH_DIV_TYPES)}; stock types {list(STOCK_DIV_TYPES)} and {list(OTHER_NONCASH_DIV_TYPES)} excluded.",
           "", "### Dividend rows in range by type", "",
           md_table(pd.DataFrame(div_stats["type_table"])[["dividendTypeId", "name", "rows", "items", "usd_sum", "isCash"]], floatfmt="{:.1f}"),
           "", "### Fund screen (TopUniverseV2 all-items variant only)", "",
           f"- Rule: {fund_stats['rule']}. Items excluded: {fund_stats['n_excluded']} "
           f"(Public Fund companies {fund_stats['n_fund_company']}, fund-venue listings {fund_stats['n_fund_venue']}; both counted "
           f"outside the v1 rule only). List with per-mask name-days: `TopUniverseV2/fund_exclusions.csv`. "
           "`v1rule_mask_contained_frac` = share of TopUniverseV2_v1rule name-days also in TopUniverseV2 (the pool is a superset; "
           "ranking can still drop a v1rule name when a non-v1 name outranks it).",
           md_table(pd.DataFrame([{"mask": k, **v} for k, v in fund_stats["per_mask"].items()])),
           "", "## Assertions", "",
           md_table(pd.DataFrame(checks)[["check", "expected", "actual", "ok"]], floatfmt="{:.6f}"),
           "", f"Failed: {n_fail}", "", "## Universe names/day (median, 2010+)", "",
           md_table(pd.DataFrame([{"mask": k, **v} for k, v in univ_summary.items()])),
           "", "## Overlap with v1 snapshot masks (2010+, common dates)", "", md_table(overlap),
           "", "## Coverage and close parity vs the v1 snapshot", "",
           f"- v1 close.parquet: {cov['v1_columns']} columns, {cov['v1_columns_with_close_in_range']} with a close in "
           f"{args.start}..{dinfo['last_date']}; of those {cov['v1_items_not_in_v2']} are not in v2 (`Master/v1_only_items.csv`); "
           f"{cov['v2_items_not_in_v1']} v2 items have no v1 close in range (`Master/v2_only_items.csv`; by current exchange "
           f"{cov['v2_only_by_exchange']}; {cov['v2_only_inV1Rule']} of them pass the v1 rule).",
           f"- v1-only items in v1 masks since {cov['parity_since'][:4]}: Top500 {cov.get('v1_only_items_in_v1_Top500_since_2010', 'n/a')} items / "
           f"{cov.get('v1_only_member_days_Top500_since_2010', 'n/a')} member-days (of {cov.get('v1_Top500_member_days_since_2010', 'n/a')}); "
           f"ALL {cov.get('v1_only_items_in_v1_ALL_since_2010', 'n/a')} items / {cov.get('v1_only_member_days_ALL_since_2010', 'n/a')} member-days "
           f"(of {cov.get('v1_ALL_member_days_since_2010', 'n/a')}).",
           f"- Common cells since {cov['parity_since']}: {cov['cells_both']} with both closes; v1-only {cov['cells_v1_only']}, v2-only {cov['cells_v2_only']}. "
           f"|v1/v2-1| > 1e-4: {cov['cells_reldiff_gt_0.0001']} ({cov['pct_reldiff_gt_0.0001']}%), > 1%: {cov['cells_reldiff_gt_0.01']} "
           f"({cov['pct_reldiff_gt_0.01']}%), > 20%: {cov['cells_reldiff_gt_0.2']} ({cov['pct_reldiff_gt_0.2']}%).",
           f"- Within v1 Top500 cells: {cov.get('top500_cells', 'n/a')} cells, > 1% diff {cov.get('top500_cells_reldiff_gt_0.01', 'n/a')} "
           f"({cov.get('top500_pct_reldiff_gt_0.01', 'n/a')}%), > 20% diff {cov.get('top500_cells_reldiff_gt_0.2', 'n/a')}; "
           f"examples (date, id, v1, v2): {cov.get('top500_examples_date_id_v1_v2', [])}",
           "- Why v1-only items are absent from v2 (checked against the DB on 2026-09-14, outside this script): 891 of the 895 v1-only ids "
           "exist in ciqtradingitem, but 838 of them have NO ciqpriceequity row at all today (the vendor purged their price history; "
           "v1 keeps the rows it collected at the time), 8 are no longer Common Stock (preferred/notes subtypes), 2 have no ciqcompany "
           "row and no main-exchange EXCHG history; v2 is one DB snapshot and cannot carry purged history.",
           "", "## Deviations / notes", "",
           "- marketCap_USD for non-USD primary currencies without an FX row that day is NaN (v1 drops the row).",
           "- Sparse index dates (holiday/weekend stragglers) dropped; list above and in MANIFEST.json.",
           "- DivData index is calendar days (like v1); BaseData/TopUniverse index is trading days.",
           "- volume/prices/factors stored as float32.",
           "- exchangeHistory comes from Compustat sec_idhist EXCHG via ciqgvkeyiid (all gvkey/iid pairs of the item).",
           "- Universe: pandas/numpy port of TopUniverse_uss_v1.py (rolling min_periods=1, cs_rank average ties in [0,1]);"
           " currentPrimary NaN treated as False as pandas does in the original.",
           ]
    (out_root / "Master" / "build_report.md").write_text("\n".join(rep) + "\n")
    log.info("report: %s", out_root / "Master" / "build_report.md")
    log.info("==== done in %.1f min, peak rss %.1f GB, assertions failed: %d", wall / 60, rss_gb(), n_fail)
    if n_fail:
        raise AssertionError(f"{n_fail} known-value assertions failed; see build_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
