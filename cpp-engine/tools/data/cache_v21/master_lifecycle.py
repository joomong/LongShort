#!/usr/bin/env python3
"""cache_v21/master_lifecycle.py - T1: per-item price coverage (ciq/mi, id chunks) and `lifecycle`.

Contract (BRIEF 3-4):
  lifecycle: tradingItemId, firstTradeDate, ciqLastCloseDate, miLastCloseDate, securityEndDate,
             tradingItemStatusId, eventEndDate, lastTradeDate, lastTradeSource('ciq'/'mi'/'event'/'enddate'),
             endReason('active'/'merger'/'bankruptcy'/'delist_other'/'data_gap'/'unknown'),
             status('active'/'confirmed'/'provisional'), observedAt, evidence
  lastTradeDate = min(ciqLastCloseDate, miLastCloseDate if any, eventEndDate if any).
  confirmed = an end marker (securityEndDate not after which the item still traded / status in END_STATUSES /
              events end).  Silence alone never confirms (lead decision 2026-09-16).
  active    = last close within 10 trading days of asOf (and no valid end marker).
  provisional = no marker and the last close is older than 10 trading days (illiquid / data gap).
  After `lineage`, `apply_lineage_first_dates` lifts the new id's firstTradeDate above the old id's lastTradeDate
  for stitched rename/relist pairs (firstPriceDate keeps the ciq start, firstTradeSource says which bound won), and
  `step_exclusions` writes `tradable_exclusions.parquet` for same-securityId dual listings (smaller dollar volume
  excluded over the overlap).

The aggregation runs `min/max(pricingdate) filter (where priceclose is not null)` per 1000-id chunk on
ciqpriceequity and miprice (never a full scan), with one DB session per table (<= 2 parallel sessions).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import DB, chunked, SCHEMA  # noqa: E402
import master_build as mb  # noqa: E402

log = logging.getLogger("cache_v21.lifecycle")

# tradingitemstatusid meaning (ciqtradingitemstatus): 2 In bankruptcy (still trades: BLIB.Q), 4 Delisted/suspended
# indefinitely, 5 Expired/matured/called, 8 Merged, 11 Inactive, 15 Active.
END_STATUSES = {8, 11, 4, 5}            # BRIEF: {8, 11, bankruptcy}; 4/5 are terminal too (no item with 4/5 is priced today)
BANKRUPT_STATUSES = {2}
RECENT_TDAYS = 10
STITCH_MIN_EQUAL_SHARE = 0.9           # share of common dates with equal as-quoted closes for a pair to count as a stitched copy
END_MARKER_TOL_DAYS = 5                # exception listing tolerance (calendar days)
EVENT_MERGER = {"ACQ", "MGD"}
EVENT_BANKRUPT = {"BK11", "BKR", "BK7"}


# --------------------------------------------------------------------------------------
# Aggregation over the big price tables (id chunks)
# --------------------------------------------------------------------------------------
CIQ_SQL = f"""
    select tradingitemid,
           min(pricingdate) filter (where priceclose is not null) as first_close,
           max(pricingdate) filter (where priceclose is not null) as last_close,
           min(pricingdate) as first_row, max(pricingdate) as last_row,
           count(*) as n_rows,
           count(*) filter (where priceclose is not null) as n_close,
           count(*) filter (where priceclose is not null and pricingdate >= '2000-01-03') as n_close_2000
    from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) group by 1"""
MI_SQL = f"""
    select tradingitemid,
           min(pricedate) filter (where priceclose is not null) as first_close,
           max(pricedate) filter (where priceclose is not null) as last_close,
           min(pricedate) as first_row, max(pricedate) as last_row,
           count(*) as n_rows,
           count(*) filter (where priceclose is not null) as n_close,
           count(*) filter (where priceclose is not null and pricedate >= '2000-01-03') as n_close_2000
    from {SCHEMA}.miprice where tradingitemid = any(%s) group by 1"""


def _agg_table(name: str, sql: str, ids: np.ndarray, out: Path, timeout_ms: int) -> pd.DataFrame:
    """Chunked aggregation with a per-chunk cache (resume on rerun)."""
    part_dir = out.parent / f"{name}_agg_parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    db = DB(timeout_ms, application_name=f"cache_v21_T1_{name}")
    chunks = list(chunked([int(i) for i in ids], 1000))
    t0 = time.time()
    frames = []
    for k, chunk in enumerate(chunks):
        p = part_dir / f"part_{k:04d}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
            continue
        t1 = time.time()
        df = db.query(sql, (chunk,))
        df.to_parquet(p, **mb.PARQUET_KW)
        frames.append(df)
        log.info("%s chunk %d/%d: %d ids -> %d rows (%.0fs, total %.0fs)", name, k + 1, len(chunks), len(chunk),
                 len(df), time.time() - t1, time.time() - t0)
    db.close()
    agg = pd.concat(frames, ignore_index=True)
    agg = agg.rename(columns={"tradingitemid": "tradingItemId"})
    agg["tradingItemId"] = agg.tradingItemId.astype("int64")
    for c in ("first_close", "last_close", "first_row", "last_row"):
        agg[c] = mb.to_ns(agg[c])
    for c in ("n_rows", "n_close", "n_close_2000"):
        agg[c] = agg[c].astype("int64")
    # ids without any row -> explicit zero rows (so "no prices" is a value, not a missing key)
    missing = np.setdiff1d(ids, agg.tradingItemId.to_numpy(dtype=np.int64))
    if len(missing):
        agg = pd.concat([agg, pd.DataFrame({"tradingItemId": missing, "first_close": pd.NaT, "last_close": pd.NaT,
                                            "first_row": pd.NaT, "last_row": pd.NaT, "n_rows": 0, "n_close": 0,
                                            "n_close_2000": 0})], ignore_index=True)
        for c in ("first_close", "last_close", "first_row", "last_row"):
            agg[c] = mb.to_ns(agg[c])
    agg = agg.sort_values("tradingItemId").reset_index(drop=True)
    agg["queriedAt"] = pd.Timestamp(mb.now())
    mb.write_parquet(agg, out)
    return agg


def step_aggregate(ids: np.ndarray, workers: int = 2, timeout_ms: int = 600_000) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ciq and mi aggregation, one session each, run in parallel (<= 2 sessions on the DB)."""
    ciq_out, mi_out = mb.WORK / "ciq_agg.parquet", mb.WORK / "mi_agg.parquet"
    if ciq_out.exists() and mi_out.exists():
        return pd.read_parquet(ciq_out), pd.read_parquet(mi_out)
    jobs = {"ciq": (CIQ_SQL, ciq_out), "mi": (MI_SQL, mi_out)}
    res = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 2))) as ex:
        futs = {ex.submit(_agg_table, name, sql, ids, out, timeout_ms): name for name, (sql, out) in jobs.items()
                if not out.exists()}
        for f, name in futs.items():
            res[name] = f.result()
    return (res.get("ciq", pd.read_parquet(ciq_out) if ciq_out.exists() else None),
            res.get("mi", pd.read_parquet(mi_out) if mi_out.exists() else None))


# --------------------------------------------------------------------------------------
# Trading calendar (union of close dates of a few long-lived items; index scans only)
# --------------------------------------------------------------------------------------
def step_calendar(db: DB, ident: pd.DataFrame) -> np.ndarray:
    out = mb.WORK / "calendar.parquet"
    if out.exists():
        return pd.read_parquet(out).date.to_numpy(dtype="datetime64[ns]")
    sel = ident[ident.symbol.isin(mb.CALENDAR_SYMBOLS) & (ident.ticurrencyid == 160)]
    ids = sel.tradingitemid.astype(int).tolist()
    df = db.query(f"select distinct pricingdate from {SCHEMA}.ciqpriceequity where tradingitemid = any(%s) "
                  f"and priceclose is not null order by 1", (ids,))
    cal = mb.to_ns(df.pricingdate).to_numpy(dtype="datetime64[ns]")
    pd.DataFrame({"date": cal}).to_parquet(out, **mb.PARQUET_KW)
    log.info("trading calendar from %s: %d days %s..%s", list(sel.symbol), len(cal), cal[0], cal[-1])
    return cal


def tdays_between(cal: np.ndarray, start, end) -> np.ndarray:
    """Number of calendar days d with start < d <= end (vectorised; NaT -> -1)."""
    s = np.asarray(start, dtype="datetime64[ns]")
    e = np.asarray(end, dtype="datetime64[ns]")
    i0 = np.searchsorted(cal, s, side="right")
    i1 = np.searchsorted(cal, e, side="right")
    out = (i1 - i0).astype(np.int64)
    out[np.isnat(s) | np.isnat(e)] = -1
    return out


def tdays_inclusive(cal: np.ndarray, start, end) -> np.ndarray:
    """Number of calendar days d with start <= d <= end; 0 when end < start; NaT -> 0."""
    s = np.asarray(start, dtype="datetime64[ns]")
    e = np.asarray(end, dtype="datetime64[ns]")
    i0 = np.searchsorted(cal, s, side="left")
    i1 = np.searchsorted(cal, e, side="right")
    out = np.maximum(i1 - i0, 0).astype(np.int64)
    out[np.isnat(s) | np.isnat(e)] = 0
    return out


# --------------------------------------------------------------------------------------
# events (eventEndDate)
# --------------------------------------------------------------------------------------
def load_events() -> tuple[pd.DataFrame, str]:
    e = pd.read_parquet(mb.EVENTS_PATH)
    e["tradingItemId"] = pd.to_numeric(e.tradingItemId, errors="coerce").astype("int64")
    st = mb.EVENTS_PATH.stat()
    sha = hashlib.sha256(mb.EVENTS_PATH.read_bytes()).hexdigest()[:12]
    version = (f"{mb.EVENTS_PATH}|mtime={dt.datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}|rows={len(e)}|sha256={sha}")
    if e.tradingItemId.duplicated().any():
        e = e.sort_values(["tradingItemId", "endDate"]).drop_duplicates("tradingItemId", keep="last")
    e["endDate"] = mb.to_ns(e.endDate)
    return e, version


# --------------------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------------------
def classify(frame: pd.DataFrame, cal: np.ndarray, as_of: pd.Timestamp) -> pd.DataFrame:
    """Pure function: frame has firstTradeDate, ciqLastCloseDate, miLastCloseDate, securityEndDate,
    tradingItemStatusId, eventEndDate, eventActionCode, eventHasSuccessor -> adds lastTradeDate, lastTradeSource,
    status, endReason, evidence, plus helper columns (gapMin, gapMax, markerValid...)."""
    f = frame.copy()
    as_of = pd.Timestamp(as_of)
    cands = pd.concat([f.ciqLastCloseDate.rename("ciq"), f.miLastCloseDate.rename("mi"), f.eventEndDate.rename("event")], axis=1)
    has_any = cands.notna().any(axis=1)
    last = cands.min(axis=1)
    # source = first of (ciq, mi, event) equal to the min (ties -> ciq)
    src = pd.Series("", index=f.index, dtype=object)
    for name in ("event", "mi", "ciq"):        # later assignment wins -> ciq has priority on ties
        hit = cands[name].notna() & (cands[name] == last)
        src[hit] = name
    no_price = ~has_any & f.securityEndDate.notna()
    last = last.where(has_any, f.securityEndDate)
    src[no_price] = "enddate"
    src[~has_any & f.securityEndDate.isna()] = ""
    f["lastTradeDate"] = last.astype("datetime64[ns]")
    f["lastTradeSource"] = src

    both_max = pd.concat([f.ciqLastCloseDate, f.miLastCloseDate], axis=1).max(axis=1)
    f["gapMin"] = tdays_between(cal, f.lastTradeDate.to_numpy(), np.full(len(f), as_of.to_datetime64()))
    f["gapMax"] = tdays_between(cal, both_max.to_numpy(), np.full(len(f), as_of.to_datetime64()))
    recent_min = has_any & (f.gapMin <= RECENT_TDAYS)
    both_silent = has_any & (f.gapMax > RECENT_TDAYS)          # neither ciq nor mi has a close in the last 10 tdays

    sec_end = f.securityEndDate
    status = f.tradingItemStatusId
    # securityEndDate counts as an end marker only when it is not in the future and the item did not trade after it
    f["secEndValid"] = sec_end.notna() & (sec_end <= as_of) & (f.lastTradeDate.isna() | (f.lastTradeDate <= sec_end))
    f["tradedAfterSecEnd"] = sec_end.notna() & f.lastTradeDate.notna() & (f.lastTradeDate > sec_end)
    f["futureSecEnd"] = sec_end.notna() & (sec_end > as_of)
    # lead list: securityenddate / status 8, 11, bankruptcy / events.  Status 2 (in bankruptcy) is a state in which the
    # stock can still trade (BLIB.Q), so it corroborates an end only once the item has been silent > RECENT_TDAYS.
    f["statusEnd"] = (status.isin(END_STATUSES).fillna(False) | (status.isin(BANKRUPT_STATUSES).fillna(False) & ~recent_min)).astype(bool)
    # a terminal status with a close on asOf itself contradicts itself -> not a marker (listed as exception)
    f["statusEndPricedToday"] = f.statusEnd & has_any & (f.gapMin == 0)
    f["eventValid"] = f.eventEndDate.notna()
    marker = f.secEndValid | f.eventValid | (f.statusEnd & ~f.statusEndPricedToday)

    # lead decision 2026-09-16: silence alone (both sources >10 tdays without a close) never confirms an end;
    # without a marker the item is `provisional/data_gap` (illiquid OTC names trade intermittently)
    st = np.where(~has_any & ~f.securityEndDate.notna(), "provisional",
         np.where(~has_any, "confirmed",
         np.where(marker, "confirmed",
         np.where(recent_min, "active", "provisional"))))
    f["status"] = st
    f["bothSilent"] = both_silent

    # endReason
    ev = f.eventActionCode.fillna("")
    reason = np.full(len(f), "unknown", dtype=object)
    is_active = f.status == "active"
    is_prov = f.status == "provisional"
    merger = (status == 8) | ev.isin(EVENT_MERGER) | f.eventHasSuccessor.fillna(False).astype(bool)
    bankrupt = status.isin(BANKRUPT_STATUSES).fillna(False) | ev.isin(EVENT_BANKRUPT) | (ev == "LIQ")
    other = f.secEndValid | status.isin({4, 5, 11}).fillna(False) | ev.isin({"N/C", "PVT", "SEC", "OTH", "DFF", "SPN"})
    reason[other.to_numpy()] = "delist_other"
    reason[bankrupt.to_numpy()] = "bankruptcy"
    reason[merger.to_numpy()] = "merger"           # confirmed without any hint keeps the default 'unknown'
    reason[is_prov.to_numpy()] = "data_gap"
    reason[is_active.to_numpy()] = "active"
    f["endReason"] = reason

    def fmt(x):
        return "" if pd.isna(x) else pd.Timestamp(x).strftime("%Y-%m-%d")

    f["evidence"] = [
        f"asOf={fmt(as_of)} ciq={fmt(a)} mi={fmt(b)} event={fmt(c)}{('(' + str(code) + ')') if isinstance(code, str) and code else ''} "
        f"secEnd={fmt(d)} status={'' if pd.isna(s) else int(s)} gapMin={g1} gapMax={g2} marker={'Y' if m else 'N'}"
        for a, b, c, code, d, s, g1, g2, m in zip(f.ciqLastCloseDate, f.miLastCloseDate, f.eventEndDate, ev, sec_end, status,
                                                   f.gapMin, f.gapMax, marker)]
    return f


LIFECYCLE_COLS = ["tradingItemId", "firstTradeDate", "firstPriceDate", "firstTradeSource", "ciqLastCloseDate", "miLastCloseDate",
                  "securityEndDate", "tradingItemStatusId", "eventEndDate", "lastTradeDate", "lastTradeSource", "endReason", "status",
                  "observedAt", "evidence"]


def assemble(ids: np.ndarray, cur: pd.DataFrame, ciq: pd.DataFrame, mi: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    c = ciq.set_index("tradingItemId").reindex(ids)
    m = mi.set_index("tradingItemId").reindex(ids)
    k = cur.set_index("tradingItemId").reindex(ids)
    e = events.set_index("tradingItemId").reindex(ids)
    return pd.DataFrame({
        "tradingItemId": ids.astype(np.int64),
        "firstTradeDate": c.first_close.to_numpy(),
        "ciqLastCloseDate": c.last_close.to_numpy(),
        "miLastCloseDate": m.last_close.to_numpy(),
        "securityEndDate": k.securityEndDate.to_numpy(),
        "tradingItemStatusId": k.tradingItemStatusId.astype("Int64").to_numpy(),
        "eventEndDate": e.endDate.to_numpy(),
        "eventActionCode": e.actionCode.to_numpy(),
        "eventHasSuccessor": e.successorTradingItemId.notna().to_numpy(),
        "ciqLastRow": c.last_row.to_numpy(),
        "miLastRow": m.last_row.to_numpy(),
        "miFirstCloseDate": m.first_close.to_numpy(),
        "nCloseCiq": c.n_close.fillna(0).astype("int64").to_numpy(),
        "nCloseMi": m.n_close.fillna(0).astype("int64").to_numpy(),
    })


def exceptions(f: pd.DataFrame, cur: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Rows worth a human look; one row per (tradingItemId, kind)."""
    k = cur.set_index("tradingItemId")
    rows = []

    def add(mask, kind, detail):
        for tid, d in zip(f.tradingItemId[mask], detail[mask] if hasattr(detail, "__getitem__") and not isinstance(detail, str) else [detail] * int(mask.sum())):
            rows.append((int(tid), kind, d))

    both = f.ciqLastCloseDate.notna() & f.miLastCloseDate.notna()
    diff = (f.ciqLastCloseDate - f.miLastCloseDate).dt.days
    m = both & (diff.abs() > END_MARKER_TOL_DAYS)
    add(m, "ciq_mi_last_close_diff_gt5d", ("ciq-mi days=" + diff.astype("Int64").astype(str)))
    se = f.securityEndDate.notna() & f.lastTradeDate.notna()
    d2 = (f.securityEndDate - f.lastTradeDate).dt.days
    add(se & (d2 > END_MARKER_TOL_DAYS), "securityEndDate_gt5d_after_lastTrade", "secEnd-last days=" + d2.astype("Int64").astype(str))
    add(se & (d2 < -END_MARKER_TOL_DAYS), "securityEndDate_gt5d_before_lastTrade", "secEnd-last days=" + d2.astype("Int64").astype(str))
    add(f.tradedAfterSecEnd, "traded_after_securityEndDate", "secEnd-last days=" + d2.astype("Int64").astype(str))
    add(f.futureSecEnd, "future_securityEndDate", "secEnd=" + f.securityEndDate.dt.strftime("%Y-%m-%d").fillna(""))
    add(f.statusEndPricedToday, "terminal_status_but_priced_on_asOf", "status=" + f.tradingItemStatusId.astype(str))
    # rolling marker: securityEndDate within the last 5 trading days of asOf while the item still traded on/after it
    recent_end = f.securityEndDate.notna() & (f.securityEndDate >= as_of - pd.Timedelta(days=7)) & f.tradedAfterSecEnd
    add(recent_end, "rolling_end_marker", "secEnd=" + f.securityEndDate.dt.strftime("%Y-%m-%d").fillna(""))
    ev = f.eventEndDate.notna() & f.ciqLastCloseDate.notna()
    d3 = (f.ciqLastCloseDate - f.eventEndDate).dt.days
    add(ev & (d3.abs() > END_MARKER_TOL_DAYS), "event_vs_ciq_last_close_gt5d", "ciq-event days=" + d3.astype("Int64").astype(str))
    # ciq history starts long before mi (ciq stitched an older line onto this id: RIG, CCO, FERG ...)
    fd = (f.miFirstCloseDate - f.firstTradeDate).dt.days
    add(f.miFirstCloseDate.notna() & f.firstTradeDate.notna() & (fd > 250), "ciq_first_close_before_mi_gt250d",
        "mi-ciq first days=" + fd.astype("Int64").astype(str))
    add(f.ciqLastCloseDate.isna(), "no_ciq_close_at_all", "nCloseMi=" + f.nCloseMi.astype(str))
    add(f.miLastCloseDate.isna() & f.ciqLastCloseDate.notna(), "no_mi_close_at_all", "nCloseCiq=" + f.nCloseCiq.astype(str))
    add((f.status == "provisional") & ~f.bothSilent & f.ciqLastCloseDate.notna(), "provisional_one_source_silent", f.evidence)
    add((f.status == "provisional") & f.bothSilent, "provisional_both_silent_no_marker", f.evidence)
    # ciq rows continue after the last close (null-close tail)
    tail = f.ciqLastRow.notna() & f.ciqLastCloseDate.notna() & ((f.ciqLastRow - f.ciqLastCloseDate).dt.days > END_MARKER_TOL_DAYS)
    add(tail, "ciq_null_close_tail_gt5d", "rows-close days=" + (f.ciqLastRow - f.ciqLastCloseDate).dt.days.astype("Int64").astype(str))
    ex = pd.DataFrame(rows, columns=["tradingItemId", "kind", "detail"])
    if ex.empty:
        return ex
    ex = ex.merge(f[["tradingItemId", "firstTradeDate", "ciqLastCloseDate", "miLastCloseDate", "eventEndDate", "securityEndDate",
                     "tradingItemStatusId", "lastTradeDate", "lastTradeSource", "status", "endReason"]], on="tradingItemId", how="left")
    ex["symbol"] = ex.tradingItemId.map(k.symbol)
    ex["companyName"] = ex.tradingItemId.map(k.companyName)
    ex["exchangeId"] = ex.tradingItemId.map(k.exchangeId)
    cols = ["tradingItemId", "symbol", "companyName", "exchangeId", "kind", "detail", "firstTradeDate", "ciqLastCloseDate",
            "miLastCloseDate", "eventEndDate", "securityEndDate", "tradingItemStatusId", "lastTradeDate", "lastTradeSource",
            "status", "endReason"]
    return ex[cols].sort_values(["kind", "tradingItemId"]).reset_index(drop=True)


def self_test(f: pd.DataFrame, cal: np.ndarray, as_of: pd.Timestamp) -> dict:
    """(1) items whose last close is the trading day before asOf, moved artificially to asOf-3 trading days,
    must stay `active`; (2) the same items moved to asOf-15 trading days with no marker must NOT stay active
    (shows the test can fail); (3) a synthetic merged item (secEnd = last close = asOf-2) must be `confirmed/merger`."""
    as_of = pd.Timestamp(as_of)
    i = int(np.searchsorted(cal, as_of.to_datetime64(), side="right")) - 1
    d_prev, d_m3, d_m15 = [pd.Timestamp(cal[i - k]) for k in (1, 3, 15)]
    # test items carry no marker at all: no end date, and no terminal / bankruptcy status either (a still-trading item with status 2
    # legitimately confirms once its closes stop for RECENT_TDAYS -> it would break test 2 by construction; D2 daily run 2026-09-18)
    no_status_marker = ~f.tradingItemStatusId.isin(sorted(END_STATUSES | BANKRUPT_STATUSES)).fillna(False).astype(bool)
    base = f[(f.status == "active") & (f.lastTradeDate == d_prev) & f.securityEndDate.isna() & f.eventEndDate.isna() & no_status_marker]
    base = base.head(200).copy()
    res = {"n_test_items": int(len(base)), "asOf": str(as_of.date()), "d_prev": str(d_prev.date()),
           "d_m3": str(d_m3.date()), "d_m15": str(d_m15.date())}
    if len(base) == 0:
        res["skipped"] = "no active item with last close on asOf-1"
        return res
    cols = ["tradingItemId", "firstTradeDate", "ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "tradingItemStatusId",
            "eventEndDate", "eventActionCode", "eventHasSuccessor"]
    t1 = base[cols].copy()
    t1["ciqLastCloseDate"] = d_m3
    t1["miLastCloseDate"] = d_m3
    r1 = classify(t1, cal, as_of)
    res["moved_to_asOf_minus3_still_active"] = int((r1.status == "active").sum())
    res["test1_pass"] = bool((r1.status == "active").all())
    t2 = t1.copy()
    t2["ciqLastCloseDate"] = d_m15
    t2["miLastCloseDate"] = d_m15
    r2 = classify(t2, cal, as_of)
    res["moved_to_asOf_minus15_status_counts"] = r2.status.value_counts().to_dict()
    res["test2_can_fail"] = bool((r2.status == "provisional").all())  # flips to provisional (never confirmed) -> detector is live
    t3 = t1.head(20).copy()
    d_m2 = pd.Timestamp(cal[i - 2])
    t3["ciqLastCloseDate"] = d_m2
    t3["miLastCloseDate"] = d_m2
    t3["securityEndDate"] = d_m2
    t3["tradingItemStatusId"] = 8
    r3 = classify(t3, cal, as_of)
    res["synthetic_merger_confirmed"] = int(((r3.status == "confirmed") & (r3.endReason == "merger")).sum())
    res["test3_pass"] = bool(((r3.status == "confirmed") & (r3.endReason == "merger")).all())
    # rolling-marker counterexample: secEnd = asOf-2 but the item still closes on asOf-1 -> must stay active
    t4 = t1.head(20).copy()
    t4["ciqLastCloseDate"] = d_prev
    t4["miLastCloseDate"] = d_prev
    t4["securityEndDate"] = d_m2
    r4 = classify(t4, cal, as_of)
    res["rolling_marker_stays_active"] = int((r4.status == "active").sum())
    res["test4_pass"] = bool((r4.status == "active").all())
    res["all_pass"] = bool(res["test1_pass"] and res["test2_can_fail"] and res["test3_pass"] and res["test4_pass"])
    return res


def step_lifecycle(ids: np.ndarray, cur: pd.DataFrame, cal: np.ndarray, as_of_price: pd.Timestamp,
                   observed_at: pd.Timestamp) -> pd.DataFrame:
    ciq = pd.read_parquet(mb.WORK / "ciq_agg.parquet")
    mi = pd.read_parquet(mb.WORK / "mi_agg.parquet")
    events, ev_version = load_events()
    frame = assemble(ids, cur, ciq, mi, events)
    f = classify(frame, cal, as_of_price)
    f["firstPriceDate"] = f.firstTradeDate.astype("datetime64[ns]")     # ciq first close (stitched past included)
    f["firstTradeSource"] = "ciq"
    f["observedAt"] = pd.Series(pd.Timestamp(observed_at), index=f.index).astype("datetime64[ns]")
    ev_tag = ev_version.split("sha256=")[-1]
    f.loc[f.eventEndDate.notna(), "evidence"] = f.loc[f.eventEndDate.notna(), "evidence"] + " eventsFile=sha256:" + ev_tag
    for c in ("firstTradeDate", "firstPriceDate", "ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "eventEndDate", "lastTradeDate"):
        f[c] = f[c].astype("datetime64[ns]")
    out = f[LIFECYCLE_COLS].sort_values("tradingItemId").reset_index(drop=True)
    mb.write_parquet(out, mb.MASTER_DIR / "lifecycle.parquet")
    f.to_parquet(mb.WORK / "lifecycle_full.parquet", **mb.PARQUET_KW)

    inst_path = mb.MASTER_DIR / "instruments.parquet"
    cur_x = cur.copy()
    if inst_path.exists():
        names = pd.read_parquet(inst_path, columns=["tradingItemId", "companyName"]).set_index("tradingItemId").companyName
        cur_x["companyName"] = cur_x.tradingItemId.map(names)
    else:
        cur_x["companyName"] = None
    ex = exceptions(f, cur_x, as_of_price)
    ex.to_csv(mb.QA_DIR / "lifecycle_exceptions.csv", index=False)
    st = self_test(f, cal, as_of_price)
    dist = f.groupby(["status", "endReason"]).size().rename("n").reset_index()
    src = f.groupby(["status", "lastTradeSource"]).size().rename("n").reset_index()
    summary = {"asOf_ciq_max_pricingdate": str(pd.Timestamp(as_of_price).date()), "observedAt": str(pd.Timestamp(observed_at).date()),
               "events_source": ev_version, "n": int(len(out)),
               "status_counts": out.status.value_counts().to_dict(),
               "status_endReason": dist.to_dict(orient="records"), "status_lastTradeSource": src.to_dict(orient="records"),
               "exceptions_by_kind": ex.kind.value_counts().to_dict() if len(ex) else {},
               "self_test": st,
               "end_statuses_used": sorted(END_STATUSES), "recent_tdays": RECENT_TDAYS}
    mb.save_json(summary, mb.QA_DIR / "lifecycle_summary.json")
    log.info("lifecycle: %s; self-test all_pass=%s", summary["status_counts"], st.get("all_pass"))
    return out


# --------------------------------------------------------------------------------------
# Post-lineage: lift stitched new ids' firstTradeDate; same-securityId dual listings -> tradable_exclusions
# --------------------------------------------------------------------------------------
def next_tday(cal: np.ndarray, d) -> pd.Timestamp:
    """First calendar day strictly after d."""
    i = int(np.searchsorted(cal, np.datetime64(pd.Timestamp(d), "ns"), side="right"))
    return pd.Timestamp(cal[min(i, len(cal) - 1)])


def ratio_constancy(db: DB, a: int, b: int, max_levels: int = 6, tol: float = 0.001) -> float:
    """Share of common close dates covered by <= max_levels constant as-quoted close ratios (each within +-tol)."""
    df = db.query(f"""
        select a.priceclose * a.adjustmentfactor as ca, b.priceclose * b.adjustmentfactor as cb
        from {SCHEMA}.ciqpriceequity a join {SCHEMA}.ciqpriceequity b
          on b.tradingitemid = %s and b.pricingdate = a.pricingdate and b.priceclose is not null and b.priceclose > 0
        where a.tradingitemid = %s and a.priceclose is not null and a.priceclose > 0""", (int(b), int(a)))
    if df.empty:
        return 0.0
    lr = np.log(df.ca.astype(float).to_numpy() / df.cb.astype(float).to_numpy())
    n = len(lr)
    covered = np.zeros(n, dtype=bool)
    for _ in range(max_levels):
        rest = lr[~covered]
        if len(rest) == 0:
            break
        bins = np.round(rest / tol).astype(np.int64)
        vals, counts = np.unique(bins, return_counts=True)
        center = vals[np.argmax(counts)] * tol
        covered |= np.abs(lr - center) <= tol
    return float(covered.mean())


def apply_lineage_first_dates(cal: np.ndarray, db: DB | None = None) -> pd.DataFrame:
    """For lineage pairs relation in {relist, rename}, confidence in {high, medium}, overlapDays > 0 and both ids in
    the set: newId.firstTradeDate = max(ciq first close, oldId.lastTradeDate + 1 tday, newId miFirstCloseDate);
    firstPriceDate keeps the ciq first close, firstTradeSource in {'ciq','lineage','mi'} names the winning bound."""
    lc = pd.read_parquet(mb.MASTER_DIR / "lifecycle.parquet")
    lin = pd.read_parquet(mb.WORK / "lineage_full.parquet")
    mi = pd.read_parquet(mb.WORK / "mi_agg.parquet").set_index("tradingItemId")
    ids = set(lc.tradingItemId)
    k = lc.set_index("tradingItemId")
    # eligible: same-line pair corroborated by Compustat periods or events (sequential ids); securityId-only pairs are
    # simultaneous dual listings and go to tradable_exclusions instead.  "Price windows overlap" is judged on the
    # lifecycle windows [firstTradeDate, lastTradeDate] (a 0 common-close count can still overlap in time).
    seq = lin.sources.str.contains("gvkeyiid") | lin.sources.str.contains("events")
    win_lo = np.maximum(lin.oldId.map(k.firstTradeDate).to_numpy(dtype="datetime64[ns]"), lin.newId.map(k.firstTradeDate).to_numpy(dtype="datetime64[ns]"))
    win_hi = np.minimum(lin.oldId.map(k.lastTradeDate).to_numpy(dtype="datetime64[ns]"), lin.newId.map(k.lastTradeDate).to_numpy(dtype="datetime64[ns]"))
    win_overlap = pd.Series((win_lo <= win_hi) & ~np.isnat(win_lo) & ~np.isnat(win_hi), index=lin.index)
    base = lin[lin.relation.isin(["relist", "rename"]) & lin.confidence.isin(["high", "medium"]) & seq & win_overlap
               & lin.oldId.isin(ids) & lin.newId.isin(ids)].copy()
    # a stitched copy has identical as-quoted closes on the common dates; two different lines that merely share a
    # Compustat gvkey/iid (MCO/DNB) do not -> only stitched copies are lifted, the rest are listed
    stitched = (base.overlapDays.fillna(0) > 0) & (base.equalShare.fillna(0) >= STITCH_MIN_EQUAL_SHARE)
    # a copy restated on one side (later split / exchange ratio) has piecewise-constant close ratios instead of equal
    # closes (RDC 0.71, ATK->OA 0.52, MOS 0.66): accept when <= 6 constant ratio levels cover >= 90% of common dates
    base["ratioConstShare"] = np.nan
    base["stitchTest"] = np.where(stitched, "equal_closes", "")
    if db is not None:
        cand = base[~stitched & (base.overlapDays.fillna(0) > 0)]
        for r in cand.itertuples():
            share = ratio_constancy(db, int(r.oldId), int(r.newId))
            base.at[r.Index, "ratioConstShare"] = share
            if share >= STITCH_MIN_EQUAL_SHARE:
                stitched.at[r.Index] = True
                base.at[r.Index, "stitchTest"] = "constant_ratio"
    not_lifted = base[~stitched].copy()
    not_lifted["reason"] = np.where(base.overlapDays.fillna(0) == 0, "no_common_close_dates", "prices_differ_on_common_dates")[~stitched.to_numpy()]
    sel = base[stitched].copy()
    # lineage.parquet: pairs that share a Compustat line but trade at different prices are two distinct securities ->
    # confidence 'low' with the test result in evidence (relation kept: it is what the source says)
    diff = not_lifted[not_lifted.reason == "prices_differ_on_common_dates"]
    if len(diff):
        lp = pd.read_parquet(mb.MASTER_DIR / "lineage.parquet")
        key = pd.MultiIndex.from_arrays([lp.oldId, lp.newId])
        for r in diff.itertuples():
            hit = key.get_indexer([(int(r.oldId), int(r.newId))])
            if hit[0] >= 0 and "two distinct lines" not in str(lp.at[lp.index[hit[0]], "evidence"]):
                i = lp.index[hit[0]]
                lp.at[i, "confidence"] = "low"
                lp.at[i, "evidence"] = (str(lp.at[i, "evidence"]) + f" | prices differ on {int(r.overlapDays)} common dates "
                                        f"(equalShare {r.equalShare:.2f}, ratioConstShare {r.ratioConstShare:.2f}): two distinct lines, not lifted")[:600]
        mb.write_parquet(lp, mb.MASTER_DIR / "lineage.parquet")
        log.info("lineage: %d same-line pairs downgraded to confidence=low (prices differ)", len(diff))
    # a new id may have several stitched predecessors -> the latest old lastTradeDate wins
    bounds = {}
    swapped = []
    for r in sel.itertuples():
        old_id, new_id = int(r.oldId), int(r.newId)
        old_last, new_last = k.lastTradeDate.get(old_id), k.lastTradeDate.get(new_id)
        if pd.isna(old_last) or pd.isna(new_last):
            continue
        if old_last > new_last:            # the "old" id outlives the "new" one -> the later-ending id is the continuation
            old_id, new_id, old_last = new_id, old_id, new_last
            swapped.append((int(r.oldId), int(r.newId)))
        b = next_tday(cal, old_last)
        prev = bounds.get(new_id)
        if prev is None or b > prev[0]:
            bounds[new_id] = (b, old_id)
    rows = []
    lc = lc.set_index("tradingItemId")
    for new_id, (b, old_id) in bounds.items():
        ciq_first = lc.at[new_id, "firstPriceDate"]
        mi_first = mi.first_close.get(new_id, pd.NaT)
        cands = {"ciq": ciq_first, "lineage": b}
        if pd.notna(mi_first):
            cands["mi"] = pd.Timestamp(mi_first)
        best = max(cands.values())
        src = "ciq" if best == cands["ciq"] else ("lineage" if best == cands["lineage"] else "mi")
        last = lc.at[new_id, "lastTradeDate"]
        first_after_last = bool(pd.notna(last) and best > last)
        rows.append({"tradingItemId": new_id, "oldId": old_id, "ciqFirst": ciq_first, "oldLastPlus1": b, "miFirst": mi_first,
                     "firstTradeDate": best, "firstTradeSource": src, "lastTradeDate": last,
                     "gapDaysToOld": int(tdays_inclusive(cal, np.array([b]), np.array([best]))[0]) - 1 if best > b else 0,
                     "firstAfterLast": first_after_last, "applied": bool(src != "ciq" and not first_after_last)})
        if src != "ciq" and not first_after_last:      # a lift beyond the item's own end (A->B->A period patterns) is not applied
            lc.at[new_id, "firstTradeDate"] = best
            lc.at[new_id, "firstTradeSource"] = src
    lc = lc.reset_index()
    lc["firstTradeDate"] = lc.firstTradeDate.astype("datetime64[ns]")
    out = lc[LIFECYCLE_COLS].sort_values("tradingItemId").reset_index(drop=True)
    mb.write_parquet(out, mb.MASTER_DIR / "lifecycle.parquet")
    adj = pd.DataFrame(rows)
    adj.to_csv(mb.QA_DIR / "lifecycle_first_date_adjustments.csv", index=False)
    nl_cols = ["oldId", "oldSymbol", "newId", "newSymbol", "relation", "confidence", "sources", "overlapDays", "overlapEqualDays", "equalShare",
               "ratioConstShare", "oldFirst", "oldLast", "newFirst", "newLast", "reason"]
    base[["oldId", "oldSymbol", "newId", "newSymbol", "sources", "overlapDays", "overlapEqualDays", "equalShare", "ratioConstShare", "stitchTest"]].to_csv(
        mb.QA_DIR / "lineage_stitch_tests.csv", index=False)
    not_lifted[[c for c in nl_cols if c in not_lifted.columns]].to_csv(mb.QA_DIR / "lineage_not_lifted.csv", index=False)
    # residual overlap over the same-line pairs after the lift
    k = out.set_index("tradingItemId")
    res = []
    for r in lin[lin.relation.isin(["relist", "rename"]) & lin.oldId.isin(ids) & lin.newId.isin(ids)].itertuples():
        lo = max(k.firstTradeDate.get(r.oldId), k.firstTradeDate.get(r.newId))
        hi = min(k.lastTradeDate.get(r.oldId), k.lastTradeDate.get(r.newId))
        if pd.notna(lo) and pd.notna(hi) and lo <= hi:
            res.append({"oldId": int(r.oldId), "newId": int(r.newId), "relation": r.relation, "confidence": r.confidence,
                        "overlapFrom": lo, "overlapTo": hi, "overlapTradingDays": int(tdays_inclusive(cal, np.array([lo]), np.array([hi]))[0]),
                        "priceOverlapDays": r.overlapDays})
    res = pd.DataFrame(res, columns=["oldId", "newId", "relation", "confidence", "overlapFrom", "overlapTo", "overlapTradingDays", "priceOverlapDays"])
    res.to_csv(mb.QA_DIR / "lineage_residual_overlap.csv", index=False)
    summary = {"pairs_window_overlap": int(len(base)), "pairs_stitched_copy": int(len(sel)),
               "stitch_test_counts": base.stitchTest.replace("", "not_stitched").value_counts().to_dict(),
               "pairs_not_lifted": not_lifted.reason.value_counts().to_dict() if len(not_lifted) else {},
               "pairs_role_swapped": len(swapped), "stitch_min_equal_share": STITCH_MIN_EQUAL_SHARE,
               "new_ids_adjusted": int(adj.applied.sum()) if len(adj) else 0,
               "by_source": adj[adj.applied].firstTradeSource.value_counts().to_dict() if len(adj) else {},
               "kept_ciq_first": int((adj.firstTradeSource == "ciq").sum()) if len(adj) else 0,
               "skipped_first_after_last": int(adj.firstAfterLast.sum()) if len(adj) else 0,
               "skipped_first_after_last_ids": adj.loc[adj.firstAfterLast, ["tradingItemId", "oldId"]].to_dict(orient="records") if len(adj) else [],
               "gap_days_between_old_and_new_total": int(adj.gapDaysToOld.sum()) if len(adj) else 0,
               "gap_days_gt0_ids": int((adj.gapDaysToOld > 0).sum()) if len(adj) else 0,
               "residual_same_line_overlap_pairs": int(len(res)), "residual_same_line_overlap_days": int(res.overlapTradingDays.sum()) if len(res) else 0,
               "residual_top": res.sort_values("overlapTradingDays", ascending=False).head(15).to_dict(orient="records")}
    mb.save_json(summary, mb.QA_DIR / "lifecycle_first_date_summary.json")
    log.info("lineage first-date lift: %d new ids adjusted %s; residual same-line overlap pairs %d (%d days)",
             summary["new_ids_adjusted"], summary["by_source"], summary["residual_same_line_overlap_pairs"], summary["residual_same_line_overlap_days"])
    return out


def step_exclusions(db: DB, cur: pd.DataFrame, cal: np.ndarray) -> pd.DataFrame:
    """Same securityId, two trading items tradable on the same day -> exclude the one with the smaller dollar volume
    (sum of ciq priceclose*volume over the overlap; raw close x raw volume = ciq close x ciq volume) for the overlap window.
    Output master/tradable_exclusions.parquet: tradingItemId, fromDate, toDate, reason, preferredItemId."""
    import master_qa as mq

    inst = pd.read_parquet(mb.MASTER_DIR / "instruments.parquet")
    lc = pd.read_parquet(mb.MASTER_DIR / "lifecycle.parquet")
    pairs = mq.same_security_overlap(inst, lc, cal)
    k = cur.set_index("tradingItemId")
    rows, detail = [], []
    for r in pairs.itertuples():
        dv = db.query(f"""select tradingitemid, sum(priceclose * volume) as dollar_volume, count(*) as n_days,
                                 sum(volume) as shares
                          from {SCHEMA}.ciqpriceequity
                          where tradingitemid = any(%s) and pricingdate between %s and %s and priceclose is not null
                          group by 1""", ([int(r.idA), int(r.idB)], pd.Timestamp(r.overlapFrom).date(), pd.Timestamp(r.overlapTo).date()))
        dv = dv.set_index("tradingitemid")
        va = float(dv.dollar_volume.get(r.idA, 0) or 0)
        vb = float(dv.dollar_volume.get(r.idB, 0) or 0)
        basis = "dollar_volume"
        if va == vb:   # tie (both zero): prefer the non-OTC listing, then more priced days, then the smaller id
            otc = {134, 133}
            sa, sb = int(k.exchangeId.get(r.idA) in otc), int(k.exchangeId.get(r.idB) in otc)
            na, nb = int(dv.n_days.get(r.idA, 0) or 0), int(dv.n_days.get(r.idB, 0) or 0)
            preferred = r.idA if (sa, -na, r.idA) < (sb, -nb, r.idB) else r.idB
            basis = "tie:exchange_then_days"
        else:
            preferred = r.idA if va > vb else r.idB
        excluded = r.idB if preferred == r.idA else r.idA
        rows.append({"tradingItemId": int(excluded), "fromDate": pd.Timestamp(r.overlapFrom), "toDate": pd.Timestamp(r.overlapTo),
                     "reason": "same_security_dual_listing", "preferredItemId": int(preferred)})
        detail.append({"securityId": r.securityId, "excludedId": int(excluded), "excludedSymbol": k.symbol.get(excluded), "excludedExchange": k.exchangeId.get(excluded),
                       "preferredItemId": int(preferred), "preferredSymbol": k.symbol.get(preferred), "preferredExchange": k.exchangeId.get(preferred),
                       "fromDate": r.overlapFrom, "toDate": r.overlapTo, "overlapTradingDays": r.overlapTradingDays,
                       "dollarVolumeExcluded": va if excluded == r.idA else vb, "dollarVolumePreferred": vb if excluded == r.idA else va,
                       "pricedDaysExcluded": int(dv.n_days.get(excluded, 0) or 0), "pricedDaysPreferred": int(dv.n_days.get(preferred, 0) or 0), "basis": basis})
    ex = pd.DataFrame(rows, columns=["tradingItemId", "fromDate", "toDate", "reason", "preferredItemId"])
    ex["tradingItemId"] = ex.tradingItemId.astype("int64")
    ex["preferredItemId"] = ex.preferredItemId.astype("int64")
    ex["fromDate"] = ex.fromDate.astype("datetime64[ns]")
    ex["toDate"] = ex.toDate.astype("datetime64[ns]")
    ex = ex.sort_values(["tradingItemId", "fromDate"]).reset_index(drop=True)
    mb.write_parquet(ex, mb.MASTER_DIR / "tradable_exclusions.parquet")
    pd.DataFrame(detail).to_csv(mb.QA_DIR / "tradable_exclusions_detail.csv", index=False)
    log.info("tradable_exclusions: %d rows for %d same-security pairs", len(ex), len(pairs))
    return ex


# --------------------------------------------------------------------------------------
# rc2 / R3: firstTradeDate lifts implied by lineage v2 (stitched copies rc1 did not lift, aux predecessors)
# --------------------------------------------------------------------------------------
def lineage_v2_first_date_patch(lineage_v2: pd.DataFrame, lifecycle: pd.DataFrame, lifecycle_aux: pd.DataFrame | None,
                                reclass: pd.DataFrame | None, cal: np.ndarray) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    """Same rule as apply_lineage_first_dates (new id firstTradeDate = old lastTradeDate + 1 trading day when the new id carries a
    stitched copy of the old line), applied to (a) reclassified pairs whose copy rc1 did not lift and (b) same-line links whose old
    id is an aux instrument (TEVA ADR -> ordinary), and to aux successors of in-set lines.  Returns (patched lifecycle, patch rows,
    patched lifecycle_aux)."""
    lc = lifecycle.set_index("tradingItemId")
    la = lifecycle_aux.set_index("tradingItemId") if lifecycle_aux is not None and len(lifecycle_aux) else None
    rows = []
    cand = lineage_v2[lineage_v2.relation.isin(["rename", "relist"]) & lineage_v2.confidence.isin(["high", "medium"])]
    stitched = set()
    if reclass is not None and len(reclass):
        stitched = set((int(a), int(b)) for a, b, s in zip(reclass.oldId, reclass.newId, reclass.stitchedCopy) if bool(s))
    for r in cand.itertuples():
        old, new = int(r.oldId), int(r.newId)
        new_tab = "lc" if new in lc.index else ("la" if la is not None and new in la.index else None)
        if new_tab is None:
            continue
        old_last = lc.lastTradeDate.get(old, pd.NaT)
        src = "lineage"
        if pd.isna(old_last) and la is not None and old in la.index:
            old_last, src = la.lastTradeDate.get(old, pd.NaT), "lineage_aux"
        elif (old, new) not in stitched and new_tab == "lc":
            continue
        if pd.isna(old_last):
            continue
        bound = next_tday(cal, old_last)
        tab = lc if new_tab == "lc" else la
        cur_first, cur_last = tab.firstTradeDate.get(new), tab.lastTradeDate.get(new)
        if pd.isna(cur_first) or bound <= cur_first or (pd.notna(cur_last) and bound > cur_last):
            continue
        rows.append({"tradingItemId": new, "table": "lifecycle" if new_tab == "lc" else "lifecycle_aux", "oldId": old, "firstTradeDateBefore": cur_first,
                     "firstTradeDate": bound, "firstTradeSource": src, "oldLastTradeDate": old_last, "ratio": r.ratio,
                     "linkClass": getattr(r, "linkClass", ""), "evidence": str(r.evidence)[:200]})
    patch = pd.DataFrame(rows, columns=["tradingItemId", "table", "oldId", "firstTradeDateBefore", "firstTradeDate", "firstTradeSource",
                                        "oldLastTradeDate", "ratio", "linkClass", "evidence"])

    def apply(frame: pd.DataFrame, rows_: pd.DataFrame) -> pd.DataFrame:
        if frame is None or not len(rows_):
            return frame
        k = frame.set_index("tradingItemId")
        for r in rows_.itertuples():
            k.at[r.tradingItemId, "firstTradeDate"] = pd.Timestamp(r.firstTradeDate)
            k.at[r.tradingItemId, "firstTradeSource"] = r.firstTradeSource
            k.at[r.tradingItemId, "evidence"] = (str(k.at[r.tradingItemId, "evidence"]) + f" | R3 firstTradeDate lift {pd.Timestamp(r.firstTradeDateBefore).date()}->{pd.Timestamp(r.firstTradeDate).date()} from {r.oldId} ({r.firstTradeSource})")[:600]
        k = k.reset_index()
        k["firstTradeDate"] = k.firstTradeDate.astype("datetime64[ns]")
        return k[LIFECYCLE_COLS]

    out = apply(lifecycle.copy(), patch[patch.table == "lifecycle"])
    out_aux = apply(lifecycle_aux.copy() if lifecycle_aux is not None else None, patch[patch.table == "lifecycle_aux"])
    return out[LIFECYCLE_COLS], patch, out_aux
