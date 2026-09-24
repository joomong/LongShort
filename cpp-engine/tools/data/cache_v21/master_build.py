#!/usr/bin/env python3
"""cache_v21/master_build.py - T1 master: symbol set, `instruments`, `current` (+ pipeline driver).

Symbol set = prototype v2 master (22,871 ids, 2026-09-14) UNION new candidates found by the *same*
rule (`build_cache_v2.step_candidates`: v1 current-exchange rule UNION ever on Compustat EXCHG
11/12/14 & Common Stock & USD) that have >= 1 ciq close on/after 2000-01-03.

Outputs (BRIEF section 3-4), stage `T1/master/`:
  instruments.parquet   tradingItemId, securityId, companyId, companyName, securitySubTypeId, tiCurrencyId,
                        inV1Rule, everMainExchange, inV1Cache, inProtoV2, addedOn
  current.parquet       tradingItemId, symbol, exchangeId, exchangeName, primaryFlag, primaryFlagTi, currentPrimary,
                        sector, simpleIndustryDescription, tradingItemStatusId, securityStartDate, securityEndDate,
                        gvkey, iid, asOf

Driver:  python master_build.py --steps candidates,agg,instruments,current,history,lifecycle,lineage,adjust,qa ; then master_report.py
Every expensive pull is staged under `T1/_work/` and reused on rerun (delete the file to redo).
Rules: read-only DB (cache_v21/db.py), big tables only via id chunks, threads <= 8, nothing written
outside the stage directory.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "8")

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
sys.path.insert(0, str(HERE))              # cache_v21/db.py (shared, T2)
sys.path.insert(0, str(HERE.parent))       # build_cache_v2.py, build_events.py (import only)
from db import DB, chunked, wait_for_quiet, SCHEMA  # noqa: E402

log = logging.getLogger("cache_v21.master")

# --------------------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------------------
SCRATCH = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad")
STAGE = SCRATCH / "stage_v21" / "T1"
WORK = STAGE / "_work"
MASTER_DIR = STAGE / "master"
QA_DIR = STAGE / "qa"
REPORT_PATH = SCRATCH / "orch_v21" / "reports" / "T1.md"

PROTO_ROOT = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d")
PROTO_MASTER = PROTO_ROOT / "Master" / "master.parquet"
PROTO_MANIFEST = PROTO_ROOT / "MANIFEST.json"
PROTO_UNIVERSE_DIR = PROTO_ROOT / "TopUniverseV2"
EVENTS_PATH = Path("/home/jgo/61/data2/temp_stock/compare_v1_v2/events_v2_altaba/events.parquet")
V1_HISTORY_DIR = Path("/home/base/vendor_data/uss/cash/master/history")
V1_BACKFILL_MTIME_CUTOFF = pd.Timestamp("2025-02-06 23:59:59")   # files with mtime <= this are the one-shot backfill
SECTOR_CSV = Path("/home/base/notebook/share/sector_info.csv")
PRIMARY_NAME_PATTERN = "Trust|ETF"
PRICE_START = pd.Timestamp("2000-01-03")
CALENDAR_SYMBOLS = ("IBM", "XOM", "KO", "JPM", "AAPL", "MSFT", "GE")   # long-lived items -> trading calendar
OPEN_END = pd.Timestamp("2900-01-01")   # sec_idhist open sentinel (kept, needs timestamp[ms])

PARQUET_KW = dict(engine="pyarrow", compression="zstd", index=False)


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    for d in (WORK, MASTER_DIR, QA_DIR, REPORT_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)


def write_parquet(df: pd.DataFrame, path: Path, **kw):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    df.to_parquet(tmp, **{**PARQUET_KW, **kw})
    os.replace(tmp, path)
    log.info("wrote %s rows=%d cols=%d %.1f MB", path.name, len(df), df.shape[1], path.stat().st_size / 1e6)


def to_ns(s: pd.Series) -> pd.Series:
    """timestamp[ns] (00:00) series; NaT preserved."""
    return pd.to_datetime(s, errors="coerce").astype("datetime64[ns]")


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(obj, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str))


# --------------------------------------------------------------------------------------
# Step 1: candidates (same rule as the prototype) and the symbol set
# --------------------------------------------------------------------------------------
def step_candidates(db: DB) -> pd.DataFrame:
    """Reuse build_cache_v2.step_candidates verbatim (identity + flags for the rule union)."""
    out = WORK / "candidates.parquet"
    if out.exists():
        return pd.read_parquet(out)
    import build_cache_v2 as bc  # noqa: WPS433  (import only; its DB helper is read-only too)

    t0 = time.time()
    stage = bc.Stage(WORK / "bc_stage", force=set())
    cand = bc.step_candidates(bc.DB(db.timeout_ms), stage)
    cand = cand.sort_values("tradingitemid").reset_index(drop=True)
    cand.to_parquet(out, **PARQUET_KW)
    log.info("candidates: %d rows (inV1Rule %d, everMainExchange %d) %.0fs", len(cand), int(cand.inv1rule.sum()),
             int(cand.evermainexchange.sum()), time.time() - t0)
    return cand


def identity_for(db: DB, ids: list[int]) -> pd.DataFrame:
    """Identity rows (same SQL as build_cache_v2) for ids that are not in the candidate frame."""
    parts = []
    for chunk in chunked(sorted(set(int(i) for i in ids)), 1000):
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
        """, (chunk,)))
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def proto_ids() -> tuple[np.ndarray, pd.Timestamp]:
    m = pd.read_parquet(PROTO_MASTER, columns=["tradingItemId"])
    man = load_json(PROTO_MANIFEST, {})
    built = pd.Timestamp(man.get("build_time", "2026-09-14")).normalize()
    return np.sort(m.tradingItemId.to_numpy(dtype=np.int64)), built


def step_symbol_set(db: DB) -> dict:
    """Aggregation set = proto ∪ candidates. Final set decided after the ciq aggregation (>= 1 close 2000+)."""
    out = WORK / "symbol_set.json"
    if out.exists():
        return load_json(out)
    cand = step_candidates(db)
    p_ids, built = proto_ids()
    c_ids = cand.tradingitemid.to_numpy(dtype=np.int64)
    agg_ids = np.union1d(p_ids, c_ids)
    info = {"proto_ids": p_ids.tolist(), "proto_built": str(built.date()), "candidate_ids": c_ids.tolist(),
            "agg_ids": agg_ids.tolist(), "n_proto": int(len(p_ids)), "n_candidates": int(len(c_ids)),
            "n_proto_not_candidate": int(len(np.setdiff1d(p_ids, c_ids))),
            "n_candidate_not_proto": int(len(np.setdiff1d(c_ids, p_ids)))}
    save_json(info, out)
    log.info("symbol set: proto %d, candidates %d, proto∖cand %d, cand∖proto %d, agg %d", info["n_proto"],
             info["n_candidates"], info["n_proto_not_candidate"], info["n_candidate_not_proto"], len(agg_ids))
    return info


def final_ids(ciq_agg: pd.DataFrame, sym: dict) -> np.ndarray:
    """proto ∪ {candidates with >= 1 ciq close on/after 2000-01-03}."""
    has = ciq_agg.loc[ciq_agg.n_close_2000 > 0, "tradingItemId"].to_numpy(dtype=np.int64)
    new = np.intersect1d(np.asarray(sym["candidate_ids"], dtype=np.int64), has)
    return np.union1d(np.asarray(sym["proto_ids"], dtype=np.int64), new)


# --------------------------------------------------------------------------------------
# Step 3/4: instruments, current
# --------------------------------------------------------------------------------------
def load_identity(db: DB, ids: np.ndarray) -> pd.DataFrame:
    """Candidate frame rows for ids (flags included) + fresh identity rows for proto-only ids."""
    out = WORK / "identity.parquet"
    if out.exists():
        ident = pd.read_parquet(out)
        if set(ident.tradingitemid.astype(int)) >= set(ids.tolist()):
            return ident[ident.tradingitemid.isin(ids)].reset_index(drop=True)
    cand = step_candidates(db)
    cand = cand[cand.tradingitemid.isin(ids)]
    missing = np.setdiff1d(ids, cand.tradingitemid.to_numpy(dtype=np.int64))
    log.info("identity: %d from candidates, %d proto-only ids need a fresh identity query", len(cand), len(missing))
    extra = identity_for(db, missing.tolist()) if len(missing) else pd.DataFrame()
    if len(extra):
        # flags for proto-only rows: they are outside today's rule union -> False; inV1Cache from the prototype master
        proto = pd.read_parquet(PROTO_MASTER, columns=["tradingItemId", "inV1Cache", "inV1Rule", "everMainExchange"]).set_index("tradingItemId")
        extra["inv1rule"] = False
        extra["evermainexchange"] = False
        extra["evermaincommonusd"] = False
        extra["inv1cache"] = extra.tradingitemid.map(proto.inV1Cache).fillna(False).astype(bool)
        name = extra.companyname.fillna("")
        extra["currentprimary"] = ((extra.primaryflag == 1) & (extra.primaryflagti == 1)
                                   & ~name.str.contains(PRIMARY_NAME_PATTERN, regex=True))
        extra["exchangehistory"] = None
        extra["gvkey"] = None
        extra["iid"] = None
    ident = pd.concat([cand, extra], ignore_index=True) if len(extra) else cand.copy()
    ident = ident.sort_values("tradingitemid").reset_index(drop=True)
    if ident.tradingitemid.duplicated().any():
        raise RuntimeError("identity has duplicate tradingItemIds")
    if len(ident) != len(ids):
        miss = np.setdiff1d(ids, ident.tradingitemid.to_numpy(dtype=np.int64))
        raise RuntimeError(f"identity rows {len(ident)} != ids {len(ids)}; missing e.g. {miss[:5]}")
    ident.to_parquet(out, **PARQUET_KW)
    return ident


def build_instruments(ident: pd.DataFrame, sym: dict, as_of: pd.Timestamp) -> pd.DataFrame:
    proto = np.asarray(sym["proto_ids"], dtype=np.int64)
    in_proto = ident.tradingitemid.isin(proto).to_numpy()
    built = pd.Timestamp(sym["proto_built"])
    inst = pd.DataFrame({
        "tradingItemId": ident.tradingitemid.astype("int64").to_numpy(),
        "securityId": ident.securityid.astype("int64").to_numpy(),
        "companyId": ident.companyid.astype("int64").to_numpy(),
        "companyName": ident.companyname.astype(object).where(ident.companyname.notna(), None),
        "securitySubTypeId": ident.securitysubtypeid.astype("Int64"),
        "tiCurrencyId": ident.ticurrencyid.astype("Int64"),
        "inV1Rule": ident.inv1rule.astype(bool).to_numpy(),
        "everMainExchange": ident.evermainexchange.astype(bool).to_numpy(),
        "inV1Cache": ident.inv1cache.astype(bool).to_numpy(),
        "inProtoV2": in_proto,
        "addedOn": pd.Series(np.where(in_proto, built.to_datetime64(), as_of.to_datetime64())).astype("datetime64[ns]").to_numpy(),
    })
    return inst.sort_values("tradingItemId").reset_index(drop=True)


def fetch_exchanges(db: DB) -> pd.DataFrame:
    return db.query(f"select exchangeid, exchangename, exchangesymbol from {SCHEMA}.ciqexchange")


def pick_current_gvkey(id_mapping: pd.DataFrame) -> pd.DataFrame:
    """One (gvkey, iid) per tradingItemId: active row first, then the latest symbolStartDate (prototype rule)."""
    if id_mapping.empty:
        return pd.DataFrame(columns=["tradingItemId", "gvkey", "iid"])
    g = id_mapping.assign(_act=id_mapping.activeFlag.fillna(0).astype(int),
                          _start=id_mapping.symbolStartDate.fillna(pd.Timestamp("1900-01-01")))
    g = g.sort_values(["tradingItemId", "_act", "_start"], ascending=[True, False, False])
    return g.drop_duplicates("tradingItemId")[["tradingItemId", "gvkey", "iid"]]


def build_current(ident: pd.DataFrame, exch: pd.DataFrame, id_mapping: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    sector = pd.read_csv(SECTOR_CSV).drop_duplicates("simpleindustrydescription")
    sector_map = dict(zip(sector.simpleindustrydescription, sector.sector))
    exch_name = dict(zip(exch.exchangeid.astype(int), exch.exchangename))
    name = ident.companyname.fillna("")
    cur = pd.DataFrame({
        "tradingItemId": ident.tradingitemid.astype("int64").to_numpy(),
        "symbol": ident.symbol.astype(object).where(ident.symbol.notna(), None),
        "exchangeId": ident.exchangeid.astype("Int64"),
        "exchangeName": ident.exchangeid.map(lambda x: exch_name.get(int(x)) if pd.notna(x) else None),
        "primaryFlag": ident.primaryflag.astype("Int64"),
        "primaryFlagTi": ident.primaryflagti.astype("Int64"),
        "currentPrimary": ((ident.primaryflag == 1) & (ident.primaryflagti == 1)
                           & ~name.str.contains(PRIMARY_NAME_PATTERN, regex=True)).to_numpy(dtype=bool),
        "sector": ident.simpleindustrydescription.map(sector_map),
        "simpleIndustryDescription": ident.simpleindustrydescription.astype(object).where(ident.simpleindustrydescription.notna(), None),
        "tradingItemStatusId": ident.tradingitemstatusid.astype("Int64"),
        "securityStartDate": to_ns(ident.securitystartdate),
        "securityEndDate": to_ns(ident.securityenddate),
    })
    gv = pick_current_gvkey(id_mapping)
    cur = cur.merge(gv, on="tradingItemId", how="left")
    cur["asOf"] = as_of
    cur["asOf"] = cur["asOf"].astype("datetime64[ns]")
    unmapped_ind = cur.loc[cur.simpleIndustryDescription.notna() & cur.sector.isna(), "simpleIndustryDescription"].unique()
    if len(unmapped_ind):
        log.warning("%d simpleIndustryDescription values have no sector in %s: %s", len(unmapped_ind), SECTOR_CSV, list(unmapped_ind)[:10])
    return cur.sort_values("tradingItemId").reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", default="candidates,agg,instruments,current,history,lifecycle,lineage,adjust,qa")
    ap.add_argument("--workers", type=int, default=2, help="parallel DB sessions for the big-table aggregation")
    ap.add_argument("--timeout-min", type=int, default=10)
    args = ap.parse_args(argv)
    steps = [s for s in args.steps.split(",") if s]
    ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(STAGE / "build.log")])
    t_start = time.time()
    db = DB(args.timeout_min * 60_000, application_name="cache_v21_T1")
    log.info("start steps=%s pid=%d", steps, os.getpid())

    import master_lifecycle as ml
    import master_history as mh
    import master_lineage as mg
    import master_qa as mq

    sym = step_symbol_set(db) if "candidates" in steps or not (WORK / "symbol_set.json").exists() else load_json(WORK / "symbol_set.json")
    agg_ids = np.asarray(sym["agg_ids"], dtype=np.int64)

    # big-table aggregation (ciq + mi, id chunks, parallel sessions)
    if "agg" in steps or not (WORK / "ciq_agg.parquet").exists():
        wait_for_quiet(db, max_active=2)
        ml.step_aggregate(agg_ids, workers=args.workers, timeout_ms=args.timeout_min * 60_000)
    ciq_agg = pd.read_parquet(WORK / "ciq_agg.parquet")
    ids = final_ids(ciq_agg, sym)
    save_json({"n_ids": int(len(ids)), "ids": ids.tolist()}, WORK / "final_ids.json")
    as_of_price = pd.Timestamp(ciq_agg.last_row.max()).normalize()          # ciq max pricingdate over the set
    as_of = pd.Timestamp(dt.date.today())                                    # DB query date
    log.info("final symbol set %d (proto %d, new %d); ciq max pricingdate %s; asOf(query) %s", len(ids), sym["n_proto"],
             len(ids) - sym["n_proto"], as_of_price.date(), as_of.date())

    ident = load_identity(db, ids)
    calendar = ml.step_calendar(db, ident)

    if "instruments" in steps:
        inst = build_instruments(ident, sym, as_of)
        write_parquet(inst, MASTER_DIR / "instruments.parquet")

    id_mapping = mh.step_id_mapping(db, ids)
    if "current" in steps:
        cur = build_current(ident, fetch_exchanges(db), id_mapping, as_of)
        write_parquet(cur, MASTER_DIR / "current.parquet")
    cur = pd.read_parquet(MASTER_DIR / "current.parquet")

    if "history" in steps:
        mh.step_identifier_history(db, id_mapping)
        mh.step_v1_histories(ids, cur, as_of)

    if "lifecycle" in steps:
        ml.step_lifecycle(ids, cur, calendar, as_of_price, as_of)

    if "lineage" in steps:
        mg.step_lineage(db, ident, cur, id_mapping, calendar)

    if "adjust" in steps:      # lead decision 2026-09-16: lift stitched new ids' firstTradeDate; dual-listing exclusions
        ml.apply_lineage_first_dates(calendar, db)
        ml.step_exclusions(db, cur, calendar)

    if "qa" in steps:
        mq.run_all(ident, cur, id_mapping, calendar, as_of_price, sym)

    db.close()
    log.info("done in %.0fs, %d queries (%.0fs in DB)", time.time() - t_start, db.n_queries, db.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
