#!/usr/bin/env python3
"""Build `Distributions/` for cache_v2: cash dividends, stock distributions (spin-offs) and splits.

Contract (BRIEF_cache_v2.md, "Distributions contract"): one row per (tradingItemId, exDate, kind[, leg]),
kind = cash | stock | split.  Stock rows carry the new security, the as-traded ratio parsed from
`co_acthist` SPN text, the ratio converted to cache units (current share basis) and two independent
value checks:

  * impliedValueFrac  = 1 - f_before / f_after at the `ciqpriceequitydivadjfactor` window boundary
                        within +-4 days of the ex-date (the vendor's own view of the value given up),
  * checkValueFrac    = ratioCacheUnit * newClose(exDate) / oldClose(exDate-1) from the v2 closes
                        (what the market actually priced the distribution at).

Sources (all read-only): xfeed `xfl_target.{ciqdividend, ciqsplit, ciqpriceequitydivadjfactor,
co_acthist, ciqgvkeyiid, ciqtradingitem, ciqsecurity, ciqcompany, ciqexchangerate, ciqpriceequity,
sec_idhist}` through `build_events.connect()` (read-only session), the v2 cache
(`BaseData/adjustmentFactor.parquet`, `BaseData/close.parquet`, `TopUniverseV2/*`) once T1 has
written `MANIFEST.json`, and hand-entered web research (`Distributions/research_overrides.csv`).

Stages (each resumable through done-markers in the local stage directory):
  db        pull every DB table once, parse the spin-off text, resolve the new trading item,
            compute the DB-side value check (as-traded closes) -> stage/*.parquet
  cache     wait for T1's MANIFEST.json (poll), convert to cache units with the v2 adjustment
            factor, compute checkValueFrac from v2 closes, attach v2 universe flags
  research  merge `research_overrides.csv` (web-sourced ratios / ids) and write research_log.md
  write     distributions.parquet + distributions_report.md (+ research_log.md)

Usage:
  build_distributions.py --stage db
  build_distributions.py --stage cache --poll-minutes 5 --poll-hours 4
  build_distributions.py --stage all
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_events as be  # noqa: E402  (read-only DB session, text parsing helpers)

# --------------------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------------------
DEFAULT_ROOT = "/home/jgo/61/data2/temp_stock/cache_v2_cash_1d"
DEFAULT_STAGE = ("/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/"
                 "cache_v2_stage/distributions")
DEFAULT_LEAD_LIST = ("/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/"
                     "spinoff_factor_check.csv")
V1_UNIVERSE_DIR = "/home/base/cache/cache_v1_cash_1d/TopUniverseV1"
SCHEMA = be.SCHEMA_NAME
US_EXCHANGES = be.US_EXCHANGES
USD = be.USD_CURRENCY
MAIN_EXCHG_CODES = ("11", "12", "14")           # sec_idhist EXCHG: NYSE, AMEX, NASDAQ
START_DATE = pd.Timestamp("2000-01-01")
# Cash dividend types (coordinator update 2026-09-14): the brief's core codes 1,4,5,7,8,10 plus the
# withholding/fund variants 6,15,17,18,19,23 that the audit confirmed as per-share cash to the holder.
# 11/20/21/22/31/33 stay out (20 is the spin-off candidate source). T1's DivData uses the same list.
CASH_CORE_TYPES = (1, 4, 5, 7, 8, 10)
CASH_VARIANT_TYPES = (6, 15, 17, 18, 19, 23)
BRIEF_CASH_TYPES = CASH_CORE_TYPES          # kept as the name used throughout
EXTRA_CASH_TYPES = CASH_VARIANT_TYPES
CASH_TYPES = CASH_CORE_TYPES + CASH_VARIANT_TYPES
EXCLUDED_DIV_TYPES = (11, 20, 21, 22, 31, 33)
STOCK_DIV_TYPES = (11, 20, 21, 22, 25, 30, 31, 32)  # never a cash amount
FACTOR_WINDOW_DAYS = 4          # divAdjFactor boundary search around the ex-date
NEW_CLOSE_MAX_DAYS = 7          # first close of the new security on/after the ex-date
MATCH_WINDOW_DAYS = 7           # ciqdividend type-20 row <-> co_acthist SPN row
VALUE_TOL = 0.03                # |implied - check| above this is value_mismatch
LEAD_MISMATCH_TOL = 0.03

STOCK_COLUMNS = [
    "tradingItemId", "exDate", "kind", "leg", "symbol", "companyName",
    "newTradingItemId", "newSymbol", "newCompanyName", "ratioNewPerOld", "ratioCacheUnit",
    "impliedValueFrac", "impliedValueFracExCash", "checkValueFrac", "status", "source",
    "evidence", "confidence",
    "newIdMethod", "eventClass", "gvkeyaqc", "newtic1", "newconm", "cashEquivalentText",
    "actionDate", "divExDate", "newCloseDate", "oldCloseBeforeDate",
    "adjFactorOldBefore", "adjFactorNewAtEx", "oldCloseBefore", "newCloseAtEx",
    "ratioCacheUnitDb", "checkValueFracDb", "oldCloseBeforeDb", "newCloseAtExDb",
    "adjFactorOldBeforeDb", "adjFactorNewAtExDb", "cacheUnitSource",
    "coincidentCashUSD", "Top500", "Top1000", "universeSource", "parseNote",
    "ratioBasis", "ratioNewPerOldText", "splitRateAtEx", "factorBoundaryDate", "dropValueFrac", "vendorCashEquivFrac",
    "checkValueFracTotal", "mismatchGap", "mismatchClass", "statusNote", "newTradingItemIdDb", "inV2Master", "newInV2Master",
]
CASH_COLUMNS = [
    "tradingItemId", "exDate", "kind", "leg", "symbol", "companyName",
    "cashPerShareUSD", "cashPerShareCacheUnit", "cashPerShareLocal", "currencyId", "fxToUSD",
    "payDate", "recordDate", "announcedDate", "dividendTypeId", "dividendTypeIds", "nRows",
    "inBriefCashTypes", "cashTypeClass", "source", "adjFactorAtEx", "adjFactorDate", "cashPerShareCacheUnitDb", "adjFactorAtExDb",
    "oldCloseBefore", "checkValueFrac", "cacheUnitSource", "status", "confidence",
]
SPLIT_COLUMNS = [
    "tradingItemId", "exDate", "kind", "leg", "symbol", "companyName",
    "rate", "splitTypeId", "appliedFlag", "payDate", "recordDate", "announcedDate", "source",
    "status", "confidence",
]

_LOG = None


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    if _LOG is not None:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def done(path: Path) -> bool:
    return path.exists() and path.with_suffix(path.suffix + ".done").exists()


def mark(path: Path) -> None:
    path.with_suffix(path.suffix + ".done").write_text(dt.datetime.now().isoformat())


def save(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    mark(path)
    log(f"wrote {path.name}: {len(frame)} rows")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# Stage db: pull everything once
# --------------------------------------------------------------------------------------
def fetch_candidate_items(conn) -> pd.DataFrame:
    """(current main-exchange USD common) UNION (ever NYSE/AMEX/NASDAQ per sec_idhist, USD common)."""
    base = f"""
        SELECT t.tradingitemid, t.securityid, s.companyid, t.tickersymbol, t.exchangeid, t.currencyid,
               t.primaryflag AS primaryflagti, s.primaryflag, c.companyname, s.securitystartdate,
               s.securityenddate
        FROM {SCHEMA}.ciqtradingitem t
        JOIN {SCHEMA}.ciqsecurity s ON s.securityid = t.securityid
        JOIN {SCHEMA}.ciqsecuritysubtype sub ON sub.securitysubtypeid = s.securitysubtypeid
             AND sub.securitysubtypename = 'Common Stock'
        LEFT JOIN {SCHEMA}.ciqcompany c ON c.companyid = s.companyid
        WHERE t.currencyid = {USD} AND """
    rule_a = be.query(conn, base + "t.exchangeid IN %s", (tuple(US_EXCHANGES),))
    ever = be.query(conn, f"""
        SELECT DISTINCT g.objectid FROM {SCHEMA}.sec_idhist h
        JOIN {SCHEMA}.ciqgvkeyiid g ON g.gvkey = h.gvkey AND g.iid = h.iid
        WHERE h.item = 'EXCHG' AND h.itemvalue IN %s""", (MAIN_EXCHG_CODES,))
    ever_ids = [int(x) for x in ever["objectid"].dropna().unique()]
    frames = [rule_a.assign(inV1Rule=True)]
    for chunk in be.chunked(ever_ids, 20000):
        frames.append(be.query(conn, base + "t.tradingitemid = ANY(%s)", (chunk,)).assign(inV1Rule=False))
    items = pd.concat(frames, ignore_index=True)
    items["everMainExchange"] = items["tradingitemid"].isin(ever_ids)
    items = (items.sort_values(["tradingitemid", "inV1Rule"], ascending=[True, False])
             .drop_duplicates("tradingitemid").reset_index(drop=True))
    items["inV1Rule"] = items["tradingitemid"].isin(rule_a["tradingitemid"])
    return items.rename(columns={"tradingitemid": "tradingItemId", "tickersymbol": "symbol",
                                 "companyname": "companyName"})


def fetch_table_for_items(conn, sql: str, ids, chunk=20000) -> pd.DataFrame:
    frames = [be.query(conn, sql, (chunk_ids,)) for chunk_ids in be.chunked(ids, chunk)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_fx(conn, currency_ids) -> pd.DataFrame:
    if not currency_ids:
        return pd.DataFrame(columns=["currencyid", "pricedate", "priceclose"])
    fx = be.query(conn, f"""
        SELECT currencyid, pricedate, priceclose FROM {SCHEMA}.ciqexchangerate
        WHERE snapid = 6 AND currencyid = ANY(%s) AND pricedate >= '1999-12-01'""",
        ([int(c) for c in currency_ids],))
    fx["pricedate"] = pd.to_datetime(fx["pricedate"])
    fx["priceclose"] = fx["priceclose"].astype(float)
    return fx.sort_values(["currencyid", "pricedate"])


def fx_to_usd(fx: pd.DataFrame, currency: int, when: pd.Timestamp) -> float:
    """USD per unit of `currency` on `when` (latest quote on or before), v1 convention
    marketCap_USD = local * rate(USD) / rate(local) with rate(USD) == 1."""
    rows = fx[fx["currencyid"] == currency]
    if rows.empty:
        return np.nan
    pos = rows["pricedate"].searchsorted(when, side="right") - 1
    if pos < 0:
        return np.nan
    rate = float(rows["priceclose"].iloc[pos])
    return 1.0 / rate if rate > 0 else np.nan


def fetch_prices_window(conn, requests) -> dict:
    """requests: (tid, lo, hi) -> {tid: DataFrame(date, close_cur, adj, close_at)} sorted by date.

    close_cur = ciqpriceequity.priceclose (current share basis, what cache_v2 stores),
    close_at  = priceclose * adjustmentfactor (as traded that day).
    """
    from psycopg2.extras import execute_values

    out = {}
    if not requests:
        return out
    for chunk in be.chunked(requests, 2000):
        with conn.cursor() as cur:
            execute_values(cur, f"""
                SELECT v.tid, p.pricingdate, p.priceclose, p.adjustmentfactor
                FROM (VALUES %s) AS v(tid, lo, hi)
                JOIN {SCHEMA}.ciqpriceequity p
                  ON p.tradingitemid = v.tid AND p.pricingdate >= v.lo AND p.pricingdate <= v.hi
                WHERE p.priceclose IS NOT NULL AND p.priceclose > 0""",
                chunk, template="(%s::int,%s::timestamp,%s::timestamp)", page_size=2000)
            for tid, when, close, factor in cur.fetchall():
                adj = float(factor) if factor is not None and float(factor) > 0 else 1.0
                out.setdefault(int(tid), []).append((pd.Timestamp(when), float(close), adj, float(close) * adj))
    return {tid: pd.DataFrame(sorted(set(rows)), columns=["date", "close_cur", "adj", "close_at"])
            for tid, rows in out.items()}


def fetch_cash_exdate_prices(conn, pairs, log_every=20) -> pd.DataFrame:
    """(tid, exdate) -> adjustmentfactor at the ex-date row (or the next priced day within 5 days)
    and the last as-traded close strictly before the ex-date. Used for the DB-side cache-unit
    conversion of cash dividends when the v2 cache is not available yet."""
    from psycopg2.extras import execute_values

    frames = []
    chunks = list(be.chunked(pairs, 10000))
    t0 = time.time()
    for i, chunk in enumerate(chunks):
        with conn.cursor() as cur:
            execute_values(cur, f"""
                SELECT v.tid, v.d,
                       (SELECT p.adjustmentfactor FROM {SCHEMA}.ciqpriceequity p
                         WHERE p.tradingitemid = v.tid AND p.pricingdate >= v.d
                           AND p.pricingdate <= v.d + interval '5 days' AND p.priceclose IS NOT NULL
                         ORDER BY p.pricingdate LIMIT 1) AS adj_at_ex,
                       (SELECT q.priceclose * COALESCE(q.adjustmentfactor, 1) FROM {SCHEMA}.ciqpriceequity q
                         WHERE q.tradingitemid = v.tid AND q.pricingdate < v.d AND q.priceclose IS NOT NULL
                           AND q.pricingdate >= v.d - interval '20 days'
                         ORDER BY q.pricingdate DESC LIMIT 1) AS close_before_at
                FROM (VALUES %s) AS v(tid, d)""",
                chunk, template="(%s::int,%s::timestamp)", page_size=10000)
            rows = cur.fetchall()
        frames.append(pd.DataFrame(rows, columns=["tradingItemId", "exDate", "adjFactorAtExDb", "closeBeforeAtDb"]))
        if (i + 1) % log_every == 0:
            log(f"  cash ex-date prices: {i + 1}/{len(chunks)} chunks, {time.time() - t0:.0f}s")
    out = pd.concat(frames, ignore_index=True)
    out["exDate"] = pd.to_datetime(out["exDate"])
    for col in ("adjFactorAtExDb", "closeBeforeAtDb"):
        out[col] = out[col].astype(float)
    return out


def stage_db(conn, stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    f_items = stage / "items.parquet"
    if not done(f_items):
        t0 = time.time()
        items = fetch_candidate_items(conn)
        log(f"candidate items: {len(items)} (inV1Rule {int(items.inV1Rule.sum())}, "
            f"everMainExchange {int(items.everMainExchange.sum())}) {time.time() - t0:.0f}s")
        save(items, f_items)
    items = pd.read_parquet(f_items)
    ids = [int(x) for x in items["tradingItemId"]]

    f_div = stage / "dividends_raw.parquet"
    if not done(f_div):
        t0 = time.time()
        div = fetch_table_for_items(conn, f"""
            SELECT dividendid, tradingitemid, exdate, paydate, recorddate, announceddate, divamount,
                   currencyid, dividendtypeid, supplementaltypeid, divfreqtypeid, netamount, grossamount
            FROM {SCHEMA}.ciqdividend WHERE tradingitemid = ANY(%s) AND exdate >= '{START_DATE:%Y-%m-%d}'""", ids)
        for col in ("exdate", "paydate", "recorddate", "announceddate"):
            div[col] = pd.to_datetime(div[col])
        for col in ("divamount", "netamount", "grossamount"):
            div[col] = div[col].astype(float)
        log(f"ciqdividend rows: {len(div)} {time.time() - t0:.0f}s")
        save(div, f_div)

    f_split = stage / "splits_raw.parquet"
    if not done(f_split):
        sp = fetch_table_for_items(conn, f"""
            SELECT splitid, tradingitemid, exdate, paydate, recorddate, announceddate, rate,
                   splittypeid, appliedflag
            FROM {SCHEMA}.ciqsplit WHERE tradingitemid = ANY(%s) AND exdate >= '{START_DATE:%Y-%m-%d}'""", ids)
        for col in ("exdate", "paydate", "recorddate", "announceddate"):
            sp[col] = pd.to_datetime(sp[col])
        sp["rate"] = sp["rate"].astype(float)
        save(sp, f_split)

    f_adj = stage / "divadj_raw.parquet"
    if not done(f_adj):
        adj = fetch_table_for_items(conn, f"""
            SELECT tradingitemid, fromdate, todate, divadjfactor
            FROM {SCHEMA}.ciqpriceequitydivadjfactor WHERE tradingitemid = ANY(%s)""", ids)
        adj["fromdate"] = pd.to_datetime(adj["fromdate"])
        adj["todate"] = pd.to_datetime(adj["todate"])
        adj["divadjfactor"] = adj["divadjfactor"].astype(float)
        save(adj.sort_values(["tradingitemid", "fromdate"]), f_adj)

    f_gmap = stage / "gvkeyiid.parquet"
    if not done(f_gmap):
        gmap = be.fetch_gvkey_map(conn, ids=ids)
        save(gmap, f_gmap)
    gmap = pd.read_parquet(f_gmap)

    f_spn = stage / "spn_raw.parquet"
    if not done(f_spn):
        gvkeys = sorted({str(g) for g in gmap["gvkey"].dropna().unique()})
        frames = [be.query(conn, f"""
            SELECT gvkey, iid, datadate, actioncd, actiontxt, gvkeyaqc, newconm, newtic1, oldconm, oldtic1,
                   newcusip, newexchg1
            FROM {SCHEMA}.co_acthist WHERE gvkey = ANY(%s) AND datadate >= '{START_DATE - pd.Timedelta(days=30):%Y-%m-%d}'""",
                                (chunk,)) for chunk in be.chunked(gvkeys, 5000)]
        acts = pd.concat(frames, ignore_index=True)
        acts["datadate"] = pd.to_datetime(acts["datadate"])
        acts = acts.merge(gmap[["objectid", "gvkey", "iid"]].drop_duplicates(), on=["gvkey", "iid"], how="inner")
        acts = acts.rename(columns={"objectid": "tradingItemId"})
        acts["tradingItemId"] = acts["tradingItemId"].astype("int64")
        log(f"co_acthist rows mapped: {len(acts)} (SPN {int((acts.actioncd == 'SPN').sum())})")
        save(acts, f_spn)

    f_fx = stage / "fx.parquet"
    if not done(f_fx):
        div = pd.read_parquet(f_div)
        ccy = sorted({int(c) for c in div["currencyid"].dropna().unique() if int(c) != USD})
        save(fetch_fx(conn, ccy), f_fx)

    # ---- stock events: union of ciqdividend type-20 rows and co_acthist SPN rows -------------
    f_stock = stage / "stock_events_db.parquet"
    if not done(f_stock):
        stock = build_stock_events(conn, stage, items, gmap)
        save(stock, f_stock)

    # ---- DB-side ex-date factor and prior close for every cash row (fallback for cache units) --
    f_cashpx = stage / "cash_exdate_prices.parquet"
    if not done(f_cashpx):
        div = pd.read_parquet(f_div)
        cash = div[div["dividendtypeid"].isin(BRIEF_CASH_TYPES + EXTRA_CASH_TYPES) & div["divamount"].notna()]
        pairs = sorted({(int(t), d.to_pydatetime()) for t, d in zip(cash["tradingitemid"], cash["exdate"])})
        log(f"cash (item, exdate) pairs for DB ex-date prices: {len(pairs)}")
        t0 = time.time()
        save(fetch_cash_exdate_prices(conn, pairs), f_cashpx)
        log(f"cash ex-date prices done {time.time() - t0:.0f}s")


# --------------------------------------------------------------------------------------
# Spin-off text parsing (reuses build_events regex building blocks)
# --------------------------------------------------------------------------------------
_UNIT2 = r"(?:SHARES?|SHS|SH|UNITS?|GDRS?|ADSS?|ADRS?)"
_FRAC = r"(\d+)\s*/\s*(\d+)"
_WORD_FRAC = r"((?:ONE|A|TWO|THREE)\s+(?:HALF|THIRDS?|QUARTERS?|FOURTHS?|FIFTHS?|TENTHS?))"
_WORD_FRACTIONS = {"HALF": 0.5, "THIRD": 1.0 / 3.0, "THIRDS": 1.0 / 3.0, "QUARTER": 0.25, "QUARTERS": 0.25,
                   "FOURTH": 0.25, "FOURTHS": 0.25, "FIFTH": 0.2, "FIFTHS": 0.2, "TENTH": 0.1, "TENTHS": 0.1}
WORD_NUMBERS2 = dict(be.WORD_NUMBERS, ELEVEN=11.0, TWELVE=12.0, THIRTEEN=13.0, FOURTEEN=14.0, FIFTEEN=15.0, SIXTEEN=16.0,
                     SEVENTEEN=17.0, EIGHTEEN=18.0, NINETEEN=19.0, TWENTY=20.0, THIRTY=30.0, FORTY=40.0, FIFTY=50.0, HUNDRED=100.0)
_WORD_ALT2 = "|".join(sorted(WORD_NUMBERS2, key=len, reverse=True))
_RECEIVE_ANY = re.compile(r"\b(?:RECEIVED|RECEIVE|RECEIVES|RECEIVING|RECIEVE|RECIEVED|RECEVED|WILL\s+RECEIVE|"
                          r"EXCHANGED\s+FOR|DISTRIBUTED|DISTRIBUTION\s+OF)\b")
# Denominator phrases ("FOR EVERY 3 SHARES HELD", "FOR TWO COM SHS HELD", "PER HPE SHARE") are not legs.
_DENOM2 = re.compile(r"(?:FOR|PER)\s+(?:EACH|EVERY|EVER)?\s*(?:(" + _WORD_ALT2 + r"|\d+(?:\.\d+)?)\s+)?"
                     r"(?:[A-Z&'.\-]+\s+){0,6}?" + _UNIT2 + r"\b(?:\s+(?:OF\s+[A-Z0-9&'.,\- ]{2,60}?\s+)?HELD)?")
_DENOM_SIMPLE = re.compile(r"(?:FOR|PER)\s+(?:(?:EACH|EVERY|EVER)\s+)?(" + _WORD_ALT2 + r"|\d+(?:\.\d+)?)\s+"
                           r"(?:[A-Z&'.\-]+\s+){0,4}?" + _UNIT2 + r"\b")
_ADJUSTED = re.compile(r"\d+(?:\.\d+)?\s+" + _UNIT2 + r"\s+ADJUSTED\b")
_MOD2 = (r"(?:NEW\s+|COMMON\s+|COM\s+|STOCK\s+|ORD\s+|ORDINARY\s+|CLASS\s+[A-Z]\s+|CL\s+[A-Z]\s+|SERIES\s+[A-Z]\s+|"
         r"SER\s+[A-Z]\s+|POST[- ]SPLIT\s+|PRE[- ]SPLIT\s+|PRE[- ]REVERSE[- ]SPLIT\s+|SPLIT[- ]ADJUSTED\s+){0,3}")
_LEG = re.compile(r"(?:" + be.NUM + r"|\b(" + _WORD_ALT2 + r")\b|" + _FRAC + r"|\b" + _WORD_FRAC + r"\b)\s*" +
                  be._QUAL + _MOD2 + _UNIT2 +
                  r"\b(?:\s+OF\s+" + be._NAME + r"(?=\s+FOR\b|\s+PER\b|\s+IN\s+EXCHANGE\b|\s+AND\b|,|\.|$))?")
_LEG_NAME_UNIT = re.compile(be.NUM + r"\s+(?:OF\s+AN?\s+)?([A-Z][A-Z0-9&'./\- ]{2,60}?)\s+" + _UNIT2 +
                            r"\s+(?:PER|FOR)\b")
_CASH_EQUIV = re.compile(r"(?:CASH\s+EQUIV(?:ALENT|ANLENT)?\.?|C/?E|CE)\s*(?:OF|:|=)?\s*\$?\s*" + be.NUM)
_CASH_EQUIV2 = re.compile(r"CASH\s+EQUIVALENT\s+(?:HAS\s+BEEN\s+PLACED\s+ON\s+THE\s+.+?\s+RECORD\s+OF\s+)\$?\s*" + be.NUM)
_TICKER_PAREN = re.compile(r"\(([A-Z]{1,6}(?:\.[A-Z])?)\)")
_TICKER_LIKE = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")
_STOP_WORDS = {"INC", "INC.", "CORP", "CORP.", "CO", "CO.", "LLC", "LP", "L.P.", "PLC", "LTD", "LTD.", "HOLDINGS",
               "HOLDING", "GROUP", "COMPANY", "CORPORATION", "THE", "NEW", "TRUST", "SPN", "-SPN", "&", "AND",
               "CLASS", "A", "B", "C", "COMMON", "STOCK", "SERIES", "SER", "CL", "INTERNATIONAL", "INTL", "INT",
               "NV", "N.V.", "SA", "AG", "LIMITED", "OLD", "REDH", "SPINCO"}


def name_tokens(name) -> list:
    if not isinstance(name, str):
        return []
    text = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    return [t for t in text.split() if t not in _STOP_WORDS]


def names_match(a, b) -> bool:
    """Same company when the first significant token(s) agree ("MICROFOCUS INT" ~ "MICRO FOCUS INTERNATIONAL")."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    if ta[0] == tb[0]:
        return True
    ja, jb = "".join(ta[:2]), "".join(tb[:2])
    return len(ja) >= 4 and len(jb) >= 4 and (ja.startswith(jb) or jb.startswith(ja))


def parse_spinoff_text(text) -> dict:
    """Legs (ratio new per old, name) of a distribution described in a co_acthist SPN text."""
    out = {"legs": [], "cashEquivalent": np.nan, "note": "", "tickers": []}
    if not isinstance(text, str) or not text.strip():
        out["note"] = "no_text"
        return out
    body = be.normalise(text)
    out["tickers"] = _TICKER_PAREN.findall(body)
    equiv = _CASH_EQUIV2.search(body) or _CASH_EQUIV.search(body)
    if equiv:
        try:
            out["cashEquivalent"] = be._value(equiv.group(1))
        except ValueError:
            pass
    body = body.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
    body = be._WS.sub(" ", body)
    if be._ELECTION.search(body):
        out["note"] = "election"
    anchors = list(_RECEIVE_ANY.finditer(body))
    clause = body[anchors[-1].end():] if anchors else body
    # Cut the clause at the cash-equivalent sentence so figures there are never read as ratios.
    cut = re.search(r"RESULTING\s+IN|CASH\s+EQUIV|C/E\s*=|\bCE\s+OF\b|\bC/P\s+OF\b", clause)
    if cut:
        clause = clause[:cut.start()]
    excluded = [m.span() for m in _DENOM2.finditer(clause)] + [m.span() for m in _ADJUSTED.finditer(clause)]
    denominators = []
    for m in _DENOM_SIMPLE.finditer(clause):
        token = m.group(1)
        denominators.append((m.start(), WORD_NUMBERS2.get(token) or float(token)))
    legs = []
    for m in _LEG.finditer(clause):
        if any(lo <= m.start() < hi for lo, hi in excluded):
            continue
        if clause[:m.start()].rstrip().endswith("$"):
            continue
        if m.group(2):
            ratio = WORD_NUMBERS2[m.group(2)]
        elif m.group(3):
            ratio = float(m.group(3)) / float(m.group(4)) if float(m.group(4)) > 0 else np.nan
        elif m.group(5):
            first, second = m.group(5).split()
            ratio = {"ONE": 1.0, "A": 1.0, "TWO": 2.0, "THREE": 3.0}[first] * _WORD_FRACTIONS[second]
        else:
            ratio = be._value(m.group(1))
        name = (m.group(6) or "").strip(" ,.") or None
        legs.append((ratio, name, m.start()))
    if not legs:
        for m in _LEG_NAME_UNIT.finditer(clause):
            if clause[:m.start()].rstrip().endswith("$"):
                continue
            legs.append((be._value(m.group(1)), m.group(2).strip(" ,."), m.start()))
    if not legs:
        for rx in (be._STOCK_TICKER, be._STOCK_TICKER_UNIT, be._STOCK_NAME_UNIT):
            m = next((x for x in rx.finditer(clause) if not clause[:x.start()].rstrip().endswith("$")), None)
            if m:
                legs.append((be._value(m.group(1)), m.group(2).strip(" ,."), m.start()))
                break
    # A nameless leg next to a named one is a restatement ("... FOR EVERY ONE SHARE HELD"), not a security.
    if any(name for _, name, _ in legs):
        legs = [leg for leg in legs if leg[1]]
    # Each leg takes the denominator phrase that follows it before the next leg ("1 SHARE OF X FOR EVERY
    # 10 SHARES AND 1 SHARE OF Y FOR EVERY 3 SHARES"); a trailing shared phrase covers the legs before it.
    parsed = []
    starts = sorted(pos for _, _, pos in legs)
    for ratio, name, pos in legs:
        nxt = next((p for p in starts if p > pos), len(clause) + 1)
        own = [d for p, d in denominators if pos < p < nxt]
        shared = [d for p, d in denominators if p > pos]
        divisor = own[0] if own else (shared[0] if shared else 1.0)
        if np.isfinite(ratio) and divisor > 0:
            ratio = ratio / divisor
        if not (np.isfinite(ratio) and 0 < ratio <= be.MAX_RATIO):
            continue
        parsed.append({"ratio": float(ratio), "name": name})
    out["legs"] = parsed
    if not parsed:
        out["note"] = (out["note"] + ";" if out["note"] else "") + "unparsed"
    elif len(parsed) > 1:
        out["note"] = (out["note"] + ";" if out["note"] else "") + f"multi_leg:{len(parsed)}"
    return out


# --------------------------------------------------------------------------------------
# New-security resolution (4-tier, mirrors build_events.resolve_acquirers with DB prices)
# --------------------------------------------------------------------------------------
class ItemInfo:
    """Cached ciqtradingitem/ciqsecurity/ciqcompany lookups for candidate trading items."""

    def __init__(self, conn, items: pd.DataFrame):
        self.conn = conn
        self.cache = {}
        self.candidate = set(int(x) for x in items["tradingItemId"])
        self.ever_main = set(int(x) for x in items.loc[items["everMainExchange"], "tradingItemId"])
        self.gvkey_items = {}
        self.name_cache = {}
        self.ticker_cache = {}
        self.price_cache = {}

    def prices(self, ids, ex: pd.Timestamp) -> dict:
        """Price windows [ex-1d, ex+NEW_CLOSE_MAX_DAYS] per item, fetched once per (item, ex)."""
        need = [(int(o), ex - pd.Timedelta(days=1), ex + pd.Timedelta(days=NEW_CLOSE_MAX_DAYS))
                for o in ids if (int(o), ex) not in self.price_cache]
        if need:
            got = fetch_prices_window(self.conn, need)
            for o, _, _ in need:
                self.price_cache[(o, ex)] = got.get(o)
        return {int(o): self.price_cache.get((int(o), ex)) for o in ids}

    def load(self, ids) -> None:
        need = [int(i) for i in ids if int(i) not in self.cache]
        if not need:
            return
        for chunk in be.chunked(need, 5000):
            frame = be.query(self.conn, f"""
                SELECT t.tradingitemid, t.tickersymbol, t.exchangeid, t.currencyid, t.primaryflag AS ti_primary,
                       s.primaryflag AS sec_primary, s.securitysubtypeid, s.securitystartdate, s.securityenddate,
                       s.companyid, c.companyname
                FROM {SCHEMA}.ciqtradingitem t
                JOIN {SCHEMA}.ciqsecurity s ON s.securityid = t.securityid
                LEFT JOIN {SCHEMA}.ciqcompany c ON c.companyid = s.companyid
                WHERE t.tradingitemid = ANY(%s)""", (chunk,))
            for row in frame.itertuples(index=False):
                self.cache[int(row.tradingitemid)] = row._asdict()
            for tid in chunk:
                self.cache.setdefault(tid, None)

    def get(self, tid):
        self.load([tid])
        return self.cache.get(int(tid))

    def company(self, tid):
        row = self.get(tid)
        return int(row["companyid"]) if row and pd.notna(row.get("companyid")) else None

    def items_of_gvkey(self, gvkey: str) -> list:
        key = str(gvkey).strip()
        if key not in self.gvkey_items:
            g = be.fetch_gvkey_map(self.conn, gvkeys=[key])
            self.gvkey_items[key] = sorted({int(x) for x in g["objectid"].dropna()})
            self.load(self.gvkey_items[key])
        return list(self.gvkey_items[key])


def company_items_by_name(conn, name: str, info: ItemInfo) -> list:
    """USD trading items of companies whose name starts with the leg's first tokens."""
    tokens = name_tokens(name)
    if not tokens:
        return []
    prefixes = [" ".join(tokens[:2])] if len(tokens) > 1 else []
    prefixes.append(tokens[0] + " " if len(tokens[0]) < 4 else tokens[0])
    for prefix in prefixes:
        if prefix in info.name_cache:
            if info.name_cache[prefix]:
                return list(info.name_cache[prefix])
            continue
        rows = be.query(conn, f"""
            SELECT t.tradingitemid FROM {SCHEMA}.ciqcompany c
            JOIN {SCHEMA}.ciqsecurity s ON s.companyid = c.companyid
            JOIN {SCHEMA}.ciqtradingitem t ON t.securityid = s.securityid
            WHERE upper(c.companyname) LIKE %s AND t.currencyid = {USD} AND s.securitysubtypeid IN (1, 2)
            LIMIT 300""", (prefix + "%",))
        ids = [int(x) for x in rows["tradingitemid"]]
        info.name_cache[prefix] = ids
        if ids:
            info.load(ids)
            return ids
    return []


def items_by_ticker(conn, ticker: str, info: ItemInfo) -> list:
    if not isinstance(ticker, str) or not ticker.strip():
        return []
    key = ticker.upper().strip()
    if key in info.ticker_cache:
        return list(info.ticker_cache[key])
    rows = be.query(conn, f"""
        SELECT t.tradingitemid FROM {SCHEMA}.ciqtradingitem t
        JOIN {SCHEMA}.ciqsecurity s ON s.securityid = t.securityid
        WHERE upper(t.tickersymbol) = %s AND t.currencyid = {USD} AND s.securitysubtypeid IN (1, 2)
        LIMIT 100""", (ticker.upper().strip(),))
    ids = [int(x) for x in rows["tradingitemid"]]
    info.ticker_cache[key] = ids
    info.load(ids)
    return ids


def pick_new_item(conn, options: list, info: ItemInfo, ex: pd.Timestamp, ticker=None, exclude_company=None) -> tuple:
    """Choose one trading item among `options` for the security distributed on `ex`.

    Tier 1: USD common, on a US exchange now or ever, priced within NEW_CLOSE_MAX_DAYS of ex.
    Tier 2: USD common priced in the window. Tier 3: USD priced in the window. Tier 4: USD.
    Ties: candidate set, primary flags, ticker match, lowest id. The parent's own company is never a
    candidate (a spin-off is another security by definition).
    """
    options = [int(o) for o in options if exclude_company is None or info.company(o) != exclude_company]
    if not options:
        return None, "unresolved"
    prices = info.prices(options, ex)

    def row(tid):
        return info.get(tid) or {}

    def usd(tid):
        return row(tid).get("currencyid") == USD

    def common(tid):
        return row(tid).get("securitysubtypeid") in (1, 2)

    def us(tid):
        return row(tid).get("exchangeid") in US_EXCHANGES or tid in info.ever_main or tid in info.candidate

    def priced(tid):
        frame = prices.get(tid)
        if frame is None or frame.empty:
            return False
        win = frame[(frame["date"] >= ex) & (frame["date"] <= ex + pd.Timedelta(days=NEW_CLOSE_MAX_DAYS))]
        return not win.empty

    tiers = (lambda t: usd(t) and common(t) and us(t) and priced(t),
             lambda t: usd(t) and common(t) and priced(t),
             lambda t: usd(t) and priced(t),
             lambda t: usd(t))
    usable, tier_hit = [], None
    for k, keep in enumerate(tiers):
        narrowed = [t for t in options if keep(t)]
        if narrowed:
            usable, tier_hit = narrowed, k + 1
            break
    if not usable:
        return None, "non_usd_or_unpriced"
    if len(usable) == 1:
        return usable[0], f"tier{tier_hit}_unique"
    named = [t for t in usable if isinstance(ticker, str) and ticker.strip() and
             str(row(t).get("tickersymbol") or "").upper().strip() == ticker.upper().strip()]
    if len(named) == 1:
        return named[0], f"tier{tier_hit}_ticker"

    def score(tid):
        r = row(tid)
        return (tid in info.candidate, r.get("ti_primary") == 1 and r.get("sec_primary") == 1,
                r.get("exchangeid") in US_EXCHANGES, -tid)

    best = max(usable, key=score)
    return best, f"tier{tier_hit}_scored({len(usable)})"


def resolve_leg(conn, info: ItemInfo, leg: dict, n_legs: int, ex: pd.Timestamp, parent_company, text_tickers) -> tuple:
    """(newTradingItemId, method, extraLeg) for one parsed leg.

    The vendor's gvkeyaqc names *one* new company per row; when the text names another company
    (RTX 2020: text = Otis, gvkeyaqc = Carrier) the named company wins and the keyed one becomes an
    extra leg with an unknown ratio, so nothing is silently attributed to the wrong security.
    """
    name, newconm, newtic1, aqc = leg.get("name"), leg.get("newconm"), leg.get("newtic1"), leg.get("gvkeyaqc")
    aqc = aqc.strip() if isinstance(aqc, str) and aqc.strip() else None
    ticker_like = bool(name) and bool(_TICKER_LIKE.fullmatch(name))
    aqc_items = info.items_of_gvkey(aqc) if aqc else []
    aqc_names = {info.get(t)["companyname"] for t in aqc_items if info.get(t) and info.get(t).get("companyname")}
    matches_aqc = (name is None) or names_match(name, newconm) or \
        (ticker_like and isinstance(newtic1, str) and name == newtic1.upper().strip()) or \
        any(names_match(name, n) for n in aqc_names)
    attempts = []
    if aqc_items and matches_aqc:
        attempts.append(("gvkeyaqc", aqc_items, newtic1))
    if name and ticker_like:
        attempts.append((f"ticker:{name}", items_by_ticker(conn, name, info), name))
    if name and (not ticker_like or len(name) >= 4):
        attempts.append(("name", company_items_by_name(conn, name, info), None))
    if aqc_items and not matches_aqc:
        attempts.append(("gvkeyaqc_name_mismatch", aqc_items, newtic1))
    if isinstance(newtic1, str) and newtic1.strip() and matches_aqc:
        attempts.append((f"ticker:{newtic1}", items_by_ticker(conn, newtic1, info), newtic1))
    chosen, method = None, "unresolved"
    for label, options, tk in attempts:
        if not options:
            continue
        pick, how = pick_new_item(conn, options, info, ex, ticker=tk, exclude_company=parent_company)
        if pick is not None:
            chosen, method = pick, f"{label}:{how}"
            break
    extra = None
    if chosen is not None and aqc_items and not matches_aqc and not method.startswith("gvkeyaqc"):
        keyed_company = {info.company(t) for t in aqc_items} - {None}
        if info.company(chosen) not in keyed_company:
            pick, how = pick_new_item(conn, aqc_items, info, ex, ticker=newtic1, exclude_company=parent_company)
            if pick is not None and info.company(pick) != info.company(chosen):
                extra = {"ratio": np.nan, "name": newconm, "newTradingItemId": pick, "method": f"gvkeyaqc_extra:{how}",
                         "gvkeyaqc": aqc, "newtic1": newtic1, "newconm": newconm}
    return chosen, method, extra


def build_stock_events(conn, stage: Path, items: pd.DataFrame, gmap: pd.DataFrame) -> pd.DataFrame:
    div = pd.read_parquet(stage / "dividends_raw.parquet")
    acts = pd.read_parquet(stage / "spn_raw.parquet")
    adj = pd.read_parquet(stage / "divadj_raw.parquet")
    splits = pd.read_parquet(stage / "splits_raw.parquet")
    item_info = items.set_index("tradingItemId")
    t20 = div[div["dividendtypeid"] == 20][["tradingitemid", "exdate", "dividendid", "paydate", "recorddate"]].copy()
    t20 = t20.rename(columns={"tradingitemid": "tradingItemId", "exdate": "divExDate"})
    t20["tradingItemId"] = t20["tradingItemId"].astype("int64")
    t20 = t20.sort_values(["tradingItemId", "divExDate", "dividendid"]).drop_duplicates(["tradingItemId", "divExDate"])
    spn = acts[acts["actioncd"] == "SPN"].copy()
    log(f"stock events: type-20 rows {len(t20)} ({t20.tradingItemId.nunique()} items), SPN rows {len(spn)} "
        f"({spn.tradingItemId.nunique()} items)")

    # Every SPN row attaches to the nearest type-20 row of the same item within MATCH_WINDOW_DAYS
    # (many-to-one: the vendor stores one SPN row per new security); the rest are SPN-only events.
    events = {}
    for r in t20.itertuples(index=False):
        events[(int(r.tradingItemId), pd.Timestamp(r.divExDate))] = {
            "tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.divExDate), "divExDate": pd.Timestamp(r.divExDate),
            "source": "ciqdividend20", "spn": []}
    t20_by_item = {tid: sorted(pd.Timestamp(d) for d in g["divExDate"]) for tid, g in t20.groupby("tradingItemId")}
    n_attached = 0
    for s in spn.itertuples():
        tid, when = int(s.tradingItemId), pd.Timestamp(s.datadate)
        dates = t20_by_item.get(tid, [])
        near = [(abs((d - when).days), d) for d in dates if abs((d - when).days) <= MATCH_WINDOW_DAYS]
        if near:
            key = (tid, min(near)[1])
            events[key]["spn"].append(s.Index)
            n_attached += 1
        else:
            key = (tid, when)
            events.setdefault(key, {"tradingItemId": tid, "exDate": when, "divExDate": pd.NaT, "source": "co_acthist", "spn": []})
            events[key]["spn"].append(s.Index)
    n_t20_text = sum(1 for e in events.values() if e["source"] == "ciqdividend20" and e["spn"])
    n_t20_only = sum(1 for e in events.values() if e["source"] == "ciqdividend20" and not e["spn"])
    n_spn_only = sum(1 for e in events.values() if e["source"] == "co_acthist")
    log(f"stock events after pairing: {len(events)} (type-20 with SPN text {n_t20_text}, type-20 only {n_t20_only}, "
        f"SPN only {n_spn_only}; SPN rows attached {n_attached})")

    other_by_item = {tid: g for tid, g in acts[acts["actioncd"] != "SPN"].groupby("tradingItemId")}
    info = ItemInfo(conn, items)
    # A previous run's rows are reused for every event whose parsed legs did not change, so a parser
    # or resolver fix only re-queries the events it touches.
    prev_path = stage / "stock_events_prev.parquet"
    prev = {}
    if prev_path.exists():
        for key, g in pd.read_parquet(prev_path).groupby(["tradingItemId", "exDate"]):
            prev[(int(key[0]), pd.Timestamp(key[1]))] = g
        log(f"resolution cache: {len(prev)} events from {prev_path.name}")
    n_reused = 0
    rows = []
    t0 = time.time()
    for k, key in enumerate(sorted(events)):
        e = events[key]
        tid, ex = e["tradingItemId"], e["exDate"]
        parent_company = int(item_info.at[tid, "companyid"]) if tid in item_info.index and pd.notna(item_info.at[tid, "companyid"]) else None
        texts, legs, notes, tickers, cash_eq = [], [], [], [], np.nan
        gvkeyaqc = newtic1 = newconm = None
        act_dates = []
        for idx in e["spn"]:
            s = spn.loc[idx]
            act_dates.append(pd.Timestamp(s.datadate))
            if isinstance(s.actiontxt, str) and s.actiontxt.strip() and s.actiontxt not in texts:
                texts.append(s.actiontxt)
            parsed = parse_spinoff_text(s.actiontxt)
            for leg in parsed["legs"]:
                legs.append(dict(leg, gvkeyaqc=s.gvkeyaqc, newtic1=s.newtic1, newconm=s.newconm))
            if not parsed["legs"] and isinstance(s.gvkeyaqc, str) and s.gvkeyaqc.strip():
                # No ratio in the text but the vendor keyed the new company: keep it as a ratio-less leg.
                legs.append({"ratio": np.nan, "name": s.newconm, "gvkeyaqc": s.gvkeyaqc, "newtic1": s.newtic1, "newconm": s.newconm})
            if parsed["note"]:
                notes.append(parsed["note"])
            tickers.extend(parsed["tickers"])
            if np.isfinite(parsed["cashEquivalent"]) and not np.isfinite(cash_eq):
                cash_eq = parsed["cashEquivalent"]
            gvkeyaqc = gvkeyaqc or s.gvkeyaqc
            newtic1 = newtic1 or s.newtic1
            newconm = newconm or s.newconm
        event_class = "spinoff" if e["spn"] else "unknown"
        other_text = None
        if not e["spn"]:
            g = other_by_item.get(tid)
            if g is not None:
                near = g[(g["datadate"] - ex).abs().dt.days <= 10]
                if not near.empty:
                    o = near.iloc[0]
                    other_text = f"{o.actioncd} {o.datadate:%Y-%m-%d}: {o.actiontxt}"
                    if o.actioncd in be.ACQUISITION_CODES:
                        event_class = "merger"
                    elif o.actioncd == "N/C":
                        event_class = "name_change"
                    else:
                        event_class = f"other:{o.actioncd}"
            end = item_info.at[tid, "securityenddate"] if tid in item_info.index else pd.NaT
            if event_class == "unknown" and pd.notna(end) and abs((pd.Timestamp(end) - ex).days) <= 10:
                event_class = "security_end"
        # De-duplicate legs (the same text is stored once per new security).
        uniq = []
        for leg in legs:
            same = [u for u in uniq if (u["name"] or "") == (leg["name"] or "") and
                    ((np.isnan(u["ratio"]) and np.isnan(leg["ratio"])) or abs((u["ratio"] or 0) - (leg["ratio"] or 0)) < 1e-12)]
            if same:
                continue
            # A ratio-less keyed leg duplicates a parsed leg of the same company.
            if np.isnan(leg["ratio"]) and any(names_match(leg["name"], u["name"]) for u in uniq if u["name"]):
                continue
            uniq.append(leg)
        legs = uniq
        base = {"tradingItemId": tid, "exDate": ex, "divExDate": e["divExDate"],
                "actionDate": min(act_dates) if act_dates else pd.NaT, "source": e["source"],
                "eventClass": event_class, "spnText": " | ".join(texts) if texts else (other_text or ""),
                "gvkeyaqc": gvkeyaqc, "newtic1": newtic1, "newconm": newconm, "cashEquivalentText": cash_eq,
                "parseNote": ";".join(notes), "nLegsText": len(legs), "tickersInText": ",".join(dict.fromkeys(tickers))}
        if not legs:
            rows.append(dict(base, leg=0, ratioNewPerOld=np.nan, newName=None, newTradingItemId=None, newIdMethod="unresolved"))
            continue
        cached = prev.get((tid, ex))
        if cached is not None and "ratioNewPerOldText" in cached.columns:
            old_sig = sorted((round(float(r), 8), str(n or "")) for r, n in zip(cached["ratioNewPerOldText"], cached["newName"])
                             if np.isfinite(r))
            new_sig = sorted((round(float(l["ratio"]), 8), str(l["name"] or "")) for l in legs if np.isfinite(l["ratio"]))
            old_keyed = sorted(str(n or "") for r, n in zip(cached["ratioNewPerOldText"], cached["newName"]) if not np.isfinite(r))
            new_keyed = sorted(str(l["name"] or "") for l in legs if not np.isfinite(l["ratio"]))
            weak = cached["newIdMethod"].astype(str).str.contains("name_mismatch|unresolved|tier4|gvkeyaqc_extra", regex=True).any()
            if old_sig == new_sig and (old_keyed == new_keyed or not new_keyed) and len(cached) == len(legs) and not weak:
                for c in cached.sort_values("leg").itertuples():
                    rows.append(dict(base, leg=int(c.leg), ratioNewPerOld=float(c.ratioNewPerOldText), newName=c.newName,
                                     newTradingItemId=int(c.newTradingItemId) if pd.notna(c.newTradingItemId) else None,
                                     newIdMethod=c.newIdMethod))
                n_reused += 1
                continue
        out_legs = []
        for leg in legs:
            chosen, method, extra = resolve_leg(conn, info, leg, len(legs), ex, parent_company, tickers)
            out_legs.append((leg["ratio"], leg["name"], chosen, method))
            if extra is not None and not any(o[2] == extra["newTradingItemId"] for o in out_legs):
                out_legs.append((np.nan, extra["name"], extra["newTradingItemId"], extra["method"]))
        # The same new security twice (keyed leg == parsed leg resolved by name): keep the one with a ratio.
        final = []
        for ratio, name, chosen, method in out_legs:
            dup = next((i for i, f in enumerate(final) if chosen is not None and f[2] == chosen), None)
            if dup is None:
                final.append((ratio, name, chosen, method))
            elif np.isnan(final[dup][0]) and np.isfinite(ratio):
                final[dup] = (ratio, name, chosen, method)
            elif np.isfinite(ratio) and np.isfinite(final[dup][0]):
                # Two named legs cannot be the same security: keep the leg, drop the wrong id.
                final.append((ratio, name, None, f"unresolved(dup_of_leg{dup}:{method})"))
        for li, (ratio, name, chosen, method) in enumerate(final):
            rows.append(dict(base, leg=li, ratioNewPerOld=ratio, newName=name, newTradingItemId=chosen, newIdMethod=method))
        if (k + 1) % 100 == 0:
            log(f"  resolved {k + 1}/{len(events)} events {time.time() - t0:.0f}s")
    stock = pd.DataFrame(rows)
    log(f"resolution: {n_reused} events reused from the previous run, {len(events) - n_reused} resolved now, {time.time() - t0:.0f}s")
    stock["newTradingItemId"] = stock["newTradingItemId"].astype("Int64")
    info.load([int(x) for x in stock["newTradingItemId"].dropna().unique()])
    stock["newSymbol"] = stock["newTradingItemId"].map(lambda t: (info.get(t) or {}).get("tickersymbol") if pd.notna(t) else None)
    stock["newCompanyName"] = stock["newTradingItemId"].map(lambda t: (info.get(t) or {}).get("companyname") if pd.notna(t) else None)
    stock["symbol"] = stock["tradingItemId"].map(item_info["symbol"])
    stock["companyName"] = stock["tradingItemId"].map(item_info["companyName"])

    # DB-side prices: old item around ex (before / at), new item first close on/after ex.
    reqs = []
    for r in stock.itertuples():
        reqs.append((int(r.tradingItemId), r.exDate - pd.Timedelta(days=15), r.exDate + pd.Timedelta(days=3)))
        if pd.notna(r.newTradingItemId):
            reqs.append((int(r.newTradingItemId), r.exDate - pd.Timedelta(days=1), r.exDate + pd.Timedelta(days=NEW_CLOSE_MAX_DAYS)))
    prices = fetch_prices_window(conn, sorted(set(reqs)))
    cols = {c: [] for c in ("oldCloseBeforeDate", "oldCloseBeforeDb", "adjFactorOldBeforeDb", "oldCloseBeforeCur",
                            "oldCloseAtExDb", "oldCloseAtExCur", "newCloseDateDb", "newCloseAtExDb", "adjFactorNewAtExDb")}
    for r in stock.itertuples():
        old = prices.get(int(r.tradingItemId))
        before = old[old["date"] < r.exDate] if old is not None else None
        at = old[old["date"] >= r.exDate] if old is not None else None
        if before is not None and not before.empty:
            b = before.iloc[-1]
            cols["oldCloseBeforeDate"].append(b["date"]); cols["oldCloseBeforeDb"].append(b["close_at"])
            cols["adjFactorOldBeforeDb"].append(b["adj"]); cols["oldCloseBeforeCur"].append(b["close_cur"])
        else:
            cols["oldCloseBeforeDate"].append(pd.NaT); cols["oldCloseBeforeDb"].append(np.nan)
            cols["adjFactorOldBeforeDb"].append(np.nan); cols["oldCloseBeforeCur"].append(np.nan)
        cols["oldCloseAtExDb"].append(at.iloc[0]["close_at"] if at is not None and not at.empty else np.nan)
        cols["oldCloseAtExCur"].append(at.iloc[0]["close_cur"] if at is not None and not at.empty else np.nan)
        new = prices.get(int(r.newTradingItemId)) if pd.notna(r.newTradingItemId) else None
        win = new[(new["date"] >= r.exDate) & (new["date"] <= r.exDate + pd.Timedelta(days=NEW_CLOSE_MAX_DAYS))] \
            if new is not None else None
        if win is not None and not win.empty:
            n = win.iloc[0]
            cols["newCloseDateDb"].append(n["date"]); cols["newCloseAtExDb"].append(n["close_at"])
            cols["adjFactorNewAtExDb"].append(n["adj"])
        else:
            cols["newCloseDateDb"].append(pd.NaT); cols["newCloseAtExDb"].append(np.nan)
            cols["adjFactorNewAtExDb"].append(np.nan)
    for c, v in cols.items():
        stock[c] = v
    stock["dropValueFrac"] = 1.0 - stock["oldCloseAtExCur"] / stock["oldCloseBeforeCur"]
    stock["vendorCashEquivFrac"] = stock["cashEquivalentText"] / stock["oldCloseBeforeDb"]

    # Vendor implied value from the divAdjFactor boundary, and the coincident cash dividend.
    adj_by_item = {tid: g.sort_values("fromdate").reset_index(drop=True) for tid, g in adj.groupby("tradingitemid")}
    cash = div[div["dividendtypeid"].isin(BRIEF_CASH_TYPES + EXTRA_CASH_TYPES) & div["divamount"].notna()]
    cash_by_item = {tid: g for tid, g in cash.groupby("tradingitemid")}
    implied, implied_ex, coincident, boundary = [], [], [], []
    for r in stock.itertuples():
        f_before, f_after, when = factor_boundary(adj_by_item.get(int(r.tradingItemId)), r.exDate)
        value = 1.0 - f_before / f_after if np.isfinite(f_before) and np.isfinite(f_after) and f_after > 0 else np.nan
        implied.append(value)
        boundary.append(when)
        g = cash_by_item.get(int(r.tradingItemId))
        amt = 0.0
        if g is not None:
            near = g[(g["exdate"] - r.exDate).abs().dt.days <= FACTOR_WINDOW_DAYS]
            amt = float(near["divamount"].sum()) if not near.empty else 0.0
        coincident.append(amt)
        if np.isfinite(value) and amt > 0 and np.isfinite(r.oldCloseBeforeDb) and r.oldCloseBeforeDb > 0:
            implied_ex.append(value - amt / r.oldCloseBeforeDb)
        else:
            implied_ex.append(value)
    stock["impliedValueFrac"] = implied
    stock["impliedValueFracExCash"] = implied_ex
    stock["coincidentCashUSD"] = coincident
    stock["factorBoundaryDate"] = pd.to_datetime(boundary)

    # Same-day split: the text's "per share held" may be per pre-split (ITT) or per post-split (EXPE)
    # share. The vendor's own factor decides which basis reproduces the value; nothing else changes.
    split_by_item = {tid: g for tid, g in splits.groupby("tradingitemid")}
    stock["splitRateAtEx"] = np.nan
    stock["ratioBasis"] = "per_share_text"
    stock["ratioNewPerOldText"] = stock["ratioNewPerOld"]
    stock["checkValueFracDb"] = stock["ratioNewPerOld"] * stock["newCloseAtExDb"] / stock["oldCloseBeforeDb"]
    for (tid, ex), g in stock.groupby(["tradingItemId", "exDate"]):
        sp = split_by_item.get(int(tid))
        if sp is None:
            continue
        near = sp[(sp["exdate"] - ex).abs().dt.days <= 1]
        if near.empty or not np.isfinite(near.iloc[0]["rate"]) or near.iloc[0]["rate"] <= 0:
            continue
        rate = float(near.iloc[0]["rate"])
        stock.loc[g.index, "splitRateAtEx"] = rate
        checks = g["checkValueFracDb"].astype(float)
        implied_value = g["impliedValueFracExCash"].iloc[0]
        if checks.isna().any() or not np.isfinite(implied_value):
            stock.loc[g.index, "ratioBasis"] = "per_share_text(split_unverified)"
            continue
        pre, post = float(checks.sum()), float(checks.sum() * rate)
        if abs(pre - implied_value) <= VALUE_TOL:
            stock.loc[g.index, "ratioBasis"] = "pre_split_share(verified)"
        elif abs(post - implied_value) <= VALUE_TOL or abs(post - implied_value) < abs(pre - implied_value) / 2:
            stock.loc[g.index, "ratioBasis"] = "post_split_share(inferred)"
            stock.loc[g.index, "ratioNewPerOld"] = g["ratioNewPerOld"] * rate
            stock.loc[g.index, "checkValueFracDb"] = checks * rate
        else:
            stock.loc[g.index, "ratioBasis"] = "per_share_text(split_ambiguous)"
    stock["ratioCacheUnitDb"] = stock["ratioNewPerOld"] * stock["adjFactorNewAtExDb"] / stock["adjFactorOldBeforeDb"]
    stock["universeSource"] = "none"
    stock["Top500"] = False
    stock["Top1000"] = False
    stock = attach_v1_universe(stock)
    return stock


def factor_boundary(frame, ex: pd.Timestamp) -> tuple:
    """(f_before, f_after, boundary date) at the divAdjFactor window boundary nearest to `ex` (+-4 days)."""
    if frame is None or frame.empty:
        return np.nan, np.nan, pd.NaT
    starts = frame["fromdate"]
    gaps = (starts - ex).dt.days.abs()
    hits = frame[(gaps <= FACTOR_WINDOW_DAYS) & (starts != starts.min())]
    if hits.empty:
        return np.nan, np.nan, pd.NaT
    i = int((hits["fromdate"] - ex).dt.days.abs().idxmin())
    if i == 0:
        return np.nan, np.nan, pd.NaT
    return float(frame.at[i - 1, "divadjfactor"]), float(frame.at[i, "divadjfactor"]), pd.Timestamp(frame.at[i, "fromdate"])


def universe_membership(mask_path: str, pairs: list) -> dict:
    """{(tid, exDate): bool} membership on the last mask date strictly before exDate."""
    out = {}
    try:
        table = pq.read_table(mask_path, memory_map=True)
    except Exception as exc:  # noqa: BLE001
        log(f"universe mask unreadable {mask_path}: {exc}")
        return out
    names = table.schema.names
    index_name = next((n for n in names if n.startswith("__index_level") or n == "date"), None)
    dates = pd.to_datetime(table.column(index_name).to_numpy()) if index_name else None
    if dates is None:
        return out
    wanted = sorted({str(int(t)) for t, _ in pairs})
    present = [c for c in wanted if c in names]
    if not present:
        return out
    sub = table.select(present)
    for tid, ex in pairs:
        col = str(int(tid))
        if col not in present:
            continue
        pos = dates.searchsorted(ex, side="left") - 1
        if pos < 0:
            continue
        values = np.asarray(sub.column(col).to_numpy(zero_copy_only=False), dtype=float)
        out[(int(tid), ex)] = bool(values[pos] > 0)
    return out


def attach_v1_universe(stock: pd.DataFrame) -> pd.DataFrame:
    pairs = sorted({(int(t), pd.Timestamp(d)) for t, d in zip(stock["tradingItemId"], stock["exDate"])})
    for name in ("Top500", "Top1000"):
        members = universe_membership(f"{V1_UNIVERSE_DIR}/{name}.parquet", pairs)
        stock[name] = [members.get((int(t), pd.Timestamp(d)), False) for t, d in zip(stock["tradingItemId"], stock["exDate"])]
    stock["universeSource"] = "v1"
    return stock


# --------------------------------------------------------------------------------------
# Stage cache: convert to cache units with T1's adjustmentFactor / close
# --------------------------------------------------------------------------------------
class WidePanel:
    """Column-subset reader for the v1/v2 wide layout (index = dates, columns = tradingItemId)."""

    def __init__(self, path: str, columns):
        schema = pq.read_schema(path)
        names = schema.names
        self.index_name = next((n for n in names if n.startswith("__index_level") or n in ("date", "TimeStamp", "index")), None)
        wanted = sorted({str(int(c)) for c in columns})
        self.present = [c for c in wanted if c in names]
        table = pq.read_table(path, columns=[self.index_name] + self.present if self.index_name else self.present)
        self.dates = pd.to_datetime(table.column(self.index_name).to_numpy()) if self.index_name else None
        self.data = {c: np.asarray(table.column(c).to_numpy(zero_copy_only=False), dtype=float) for c in self.present}
        del table

    def has(self, tid) -> bool:
        return str(int(tid)) in self.data

    def at(self, tid, when: pd.Timestamp, direction: str = "exact_or_next", max_days: int = 5) -> tuple:
        """(value, date) at `when`; 'exact_or_next' takes the first finite value on/after `when`
        within max_days, 'before' the last finite value strictly before `when`."""
        col = str(int(tid))
        if col not in self.data or self.dates is None:
            return np.nan, pd.NaT
        values = self.data[col]
        if direction == "before":
            pos = self.dates.searchsorted(when, side="left") - 1
            while pos >= 0 and not np.isfinite(values[pos]):
                pos -= 1
                if pos >= 0 and (when - self.dates[pos]).days > 20:
                    return np.nan, pd.NaT
            return (float(values[pos]), self.dates[pos]) if pos >= 0 else (np.nan, pd.NaT)
        pos = self.dates.searchsorted(when, side="left")
        while pos < len(self.dates) and (self.dates[pos] - when).days <= max_days:
            if np.isfinite(values[pos]):
                return float(values[pos]), self.dates[pos]
            pos += 1
        if direction in ("exact_or_next_or_prev", "nearest"):
            # 'nearest': the last factor before the date (any distance, T1's convention - the shares
            # held on the ex-date were on that basis), else the first one after within 400 days.
            limit = 60 if direction == "exact_or_next_or_prev" else 10 ** 6
            pos = self.dates.searchsorted(when, side="left") - 1
            while pos >= 0 and (when - self.dates[pos]).days <= limit:
                if np.isfinite(values[pos]):
                    return float(values[pos]), self.dates[pos]
                pos -= 1
            if direction == "nearest":
                pos = self.dates.searchsorted(when, side="left")
                while pos < len(self.dates) and (self.dates[pos] - when).days <= 400:
                    if np.isfinite(values[pos]):
                        return float(values[pos]), self.dates[pos]
                    pos += 1
        return np.nan, pd.NaT


def wait_for_manifest(root: Path, poll_minutes: float, poll_hours: float) -> bool:
    manifest = root / "MANIFEST.json"
    deadline = time.time() + poll_hours * 3600
    while True:
        if manifest.exists():
            log(f"MANIFEST.json found: {manifest}")
            return True
        if time.time() >= deadline:
            log(f"MANIFEST.json not found after {poll_hours} h; continuing without the cache")
            return False
        log(f"waiting for {manifest} (next check in {poll_minutes} min)")
        time.sleep(poll_minutes * 60)


def stage_cache(root: Path, stage: Path, poll_minutes: float, poll_hours: float, force: bool = False) -> None:
    f_out = stage / "cache_units.parquet"
    if done(f_out) and not force:
        log("cache stage already done")
        return
    have_cache = wait_for_manifest(root, poll_minutes, poll_hours)
    stock = pd.read_parquet(stage / "stock_events_db.parquet")
    div = pd.read_parquet(stage / "dividends_raw.parquet")
    extra_new = override_ids(root / "Distributions" / "research_overrides.csv")
    cash_items = sorted({int(x) for x in div.loc[div["dividendtypeid"].isin(BRIEF_CASH_TYPES + EXTRA_CASH_TYPES), "tradingitemid"]})
    stock_items = sorted({int(x) for x in stock["tradingItemId"]} | {int(x) for x in stock["newTradingItemId"].dropna()} | set(extra_new))
    result = {"haveCache": have_cache, "manifest": None}
    if not have_cache:
        save(pd.DataFrame({"tradingItemId": [], "exDate": []}), f_out)
        (stage / "cache_status.json").write_text(json.dumps(result))
        return
    result["manifest"] = json.loads((root / "MANIFEST.json").read_text())
    t0 = time.time()
    adj_path, close_path = root / "BaseData" / "adjustmentFactor.parquet", root / "BaseData" / "close.parquet"
    adj = WidePanel(str(adj_path), cash_items + stock_items)
    close = WidePanel(str(close_path), stock_items + cash_items)
    log(f"read v2 adjustmentFactor ({len(adj.present)} cols) and close ({len(close.present)} cols) in {time.time() - t0:.0f}s; "
        f"T={len(adj.dates)} dates {adj.dates.min():%Y-%m-%d}..{adj.dates.max():%Y-%m-%d}")
    # Cash: adjustmentFactor at the ex-date (first priced day on/after within 5 days), close before.
    cash = div[div["dividendtypeid"].isin(BRIEF_CASH_TYPES + EXTRA_CASH_TYPES) & div["divamount"].notna()]
    pairs = sorted({(int(t), pd.Timestamp(d)) for t, d in zip(cash["tradingitemid"], cash["exdate"])})
    splits = pd.read_parquet(stage / "splits_raw.parquet")
    split_dates = {tid: sorted(pd.Timestamp(d) for d in g["exdate"]) for tid, g in splits.groupby("tradingitemid")}
    rows = []
    n_far, n_split_between = 0, 0
    for tid, ex in pairs:
        # Factor on the ex-date row; when the item has no row within 5 days after, the last factor
        # before the ex-date applies (else the first within 400 days after) unless a ciqsplit
        # ex-date lies between the two dates.
        a, a_date = adj.at(tid, ex, direction="nearest")
        if pd.notna(a_date) and a_date != ex:
            lo, hi = min(a_date, ex), max(a_date, ex)
            if any(lo < d <= hi for d in split_dates.get(tid, [])):
                a, a_date = np.nan, pd.NaT
                n_split_between += 1
            elif abs((a_date - ex).days) > 5:
                n_far += 1
        c, c_date = close.at(tid, ex, direction="before")
        rows.append((tid, ex, a, a_date, c, c_date))
    log(f"cash factor lookups: {n_far} taken from a row more than 5 days away, {n_split_between} refused (split in between)")
    cash_units = pd.DataFrame(rows, columns=["tradingItemId", "exDate", "adjFactorAtEx", "adjFactorDate",
                                             "oldCloseBefore", "oldCloseBeforeDate"])
    log(f"cash cache-unit lookups: {len(cash_units)} pairs, adjFactor found {int(cash_units.adjFactorAtEx.notna().sum())}")
    # Stock: old item's factor on the last close before ex (keyed by event), new item's factor on its
    # first close on/after ex (keyed by (new item, ex) so web-resolved ids get the same treatment).
    events = sorted({(int(t), pd.Timestamp(d)) for t, d in zip(stock["tradingItemId"], stock["exDate"])})
    rows = []
    for tid, ex in events:
        old_close, old_date = close.at(tid, ex, direction="before")
        old_adj, _ = adj.at(tid, old_date) if pd.notna(old_date) else (np.nan, pd.NaT)
        rows.append((tid, ex, old_close, old_date, old_adj))
    old_units = pd.DataFrame(rows, columns=["tradingItemId", "exDate", "oldCloseBefore", "oldCloseBeforeDateCache", "adjFactorOldBefore"])
    old_units.to_parquet(stage / "cache_units_old.parquet", index=False)
    new_pairs = {(int(n), pd.Timestamp(d)) for n, d in zip(stock["newTradingItemId"], stock["exDate"]) if pd.notna(n)}
    if extra_new:
        ov = load_overrides(root / "Distributions" / "research_overrides.csv")
        new_pairs |= {(int(n), pd.Timestamp(d)) for n, d in zip(ov["newTradingItemId"], ov["exDate"]) if pd.notna(n)}
    rows = []
    for nid, ex in sorted(new_pairs):
        new_close, new_date = close.at(nid, ex, max_days=NEW_CLOSE_MAX_DAYS)
        new_adj, _ = adj.at(nid, new_date) if pd.notna(new_date) else (np.nan, pd.NaT)
        rows.append((nid, ex, new_close, new_date, new_adj))
    new_units = pd.DataFrame(rows, columns=["newTradingItemId", "exDate", "newCloseAtEx", "newCloseDate", "adjFactorNewAtEx"])
    new_units["newTradingItemId"] = new_units["newTradingItemId"].astype("Int64")
    new_units.to_parquet(stage / "cache_units_new.parquet", index=False)
    log(f"stock cache-unit lookups: {len(old_units)} events, {len(new_units)} (new item, ex) pairs, "
        f"old close found {int(old_units.oldCloseBefore.notna().sum())}, new close found {int(new_units.newCloseAtEx.notna().sum())}")
    # Universe flags from v2 masks.
    pairs = sorted({(int(t), pd.Timestamp(d)) for t, d in zip(stock["tradingItemId"], stock["exDate"])})
    univ = {}
    for name in ("Top500", "Top1000"):
        path = root / "TopUniverseV2" / f"{name}.parquet"
        univ[name] = universe_membership(str(path), pairs) if path.exists() else {}
    pd.DataFrame([{"tradingItemId": t, "exDate": d, "Top500": univ["Top500"].get((t, d)), "Top1000": univ["Top1000"].get((t, d))}
                  for t, d in pairs]).to_parquet(stage / "cache_universe.parquet", index=False)
    save(cash_units, f_out)
    (stage / "cache_status.json").write_text(json.dumps(result, default=str))


# --------------------------------------------------------------------------------------
# Stage research: hand-entered overrides from web research
# --------------------------------------------------------------------------------------
OVERRIDE_COLUMNS = ["tradingItemId", "exDate", "newTradingItemId", "newSymbol", "ratioNewPerOld", "matchLeg",
                    "url", "evidence", "confidence", "resolution", "entered_on"]
# resolved: ratio and new id from the source; confirmed: source agrees with the DB text; ratio_only /
# id_only: one side stays unknown; unresolved: nothing reliable found (logged, nothing applied).
OVERRIDE_RESOLUTIONS = ("resolved", "confirmed", "ratio_only", "id_only", "unresolved")


def load_overrides(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=OVERRIDE_COLUMNS)
    frame = pd.read_csv(path, dtype={"tradingItemId": "int64"}, parse_dates=["exDate"])
    missing = [c for c in OVERRIDE_COLUMNS if c not in frame.columns]
    if missing:
        raise RuntimeError(f"research_overrides.csv lacks {missing}")
    frame["newTradingItemId"] = frame["newTradingItemId"].astype("Int64")
    frame["matchLeg"] = frame["matchLeg"].astype("Int64")
    frame["ratioNewPerOld"] = frame["ratioNewPerOld"].astype(float)
    bad = frame[~frame["resolution"].astype(str).str.lower().isin(OVERRIDE_RESOLUTIONS)]
    if len(bad):
        raise RuntimeError(f"research_overrides.csv has unknown resolution values: {bad.resolution.unique()}")
    return frame


def override_ids(path: Path) -> list:
    frame = load_overrides(path)
    return [int(x) for x in frame["newTradingItemId"].dropna().unique()] if len(frame) else []


# --------------------------------------------------------------------------------------
# Stage write: assemble distributions.parquet + reports
# --------------------------------------------------------------------------------------
def assemble(stage: Path, root: Path, out_dir: Path, lead_list: Path) -> dict:
    items = pd.read_parquet(stage / "items.parquet").set_index("tradingItemId")
    div = pd.read_parquet(stage / "dividends_raw.parquet")
    splits = pd.read_parquet(stage / "splits_raw.parquet")
    fx = pd.read_parquet(stage / "fx.parquet")
    stock = pd.read_parquet(stage / "stock_events_db.parquet")
    cash_px_db = pd.read_parquet(stage / "cash_exdate_prices.parquet") if (stage / "cash_exdate_prices.parquet").exists() else None
    status_path = stage / "cache_status.json"
    cache_state = json.loads(status_path.read_text()) if status_path.exists() else {"haveCache": False}
    have_cache = bool(cache_state.get("haveCache"))
    cash_units = pd.read_parquet(stage / "cache_units.parquet") if have_cache else None
    old_units = pd.read_parquet(stage / "cache_units_old.parquet") if have_cache else None
    new_units = pd.read_parquet(stage / "cache_units_new.parquet") if have_cache else None
    master_ids = set(pd.read_parquet(root / "Master" / "master.parquet", columns=["tradingItemId"])["tradingItemId"].astype("int64")) \
        if (root / "Master" / "master.parquet").exists() else None
    cache_univ = pd.read_parquet(stage / "cache_universe.parquet") if have_cache and (stage / "cache_universe.parquet").exists() else None
    overrides = load_overrides(out_dir / "research_overrides.csv")

    # ---------------- cash ----------------
    cash = div[div["dividendtypeid"].isin(BRIEF_CASH_TYPES + EXTRA_CASH_TYPES) & div["divamount"].notna()].copy()
    cash["currencyid"] = cash["currencyid"].fillna(USD).astype(int)
    cash["fxToUSD"] = 1.0
    non_usd = cash["currencyid"] != USD
    cash.loc[non_usd, "fxToUSD"] = [fx_to_usd(fx, int(c), d) for c, d in zip(cash.loc[non_usd, "currencyid"], cash.loc[non_usd, "exdate"])]
    cash["cashUSD"] = cash["divamount"] * cash["fxToUSD"]
    grouped = cash.sort_values(["tradingitemid", "exdate", "dividendtypeid"]).groupby(["tradingitemid", "exdate"], sort=True)
    agg = grouped.agg(cashPerShareUSD=("cashUSD", "sum"), cashPerShareLocal=("divamount", "sum"),
                      currencyId=("currencyid", "first"), fxToUSD=("fxToUSD", "first"),
                      payDate=("paydate", "first"), recordDate=("recorddate", "first"),
                      announcedDate=("announceddate", "first"), dividendTypeId=("dividendtypeid", "first"),
                      dividendTypeIds=("dividendtypeid", lambda s: "+".join(str(int(x)) for x in s)),
                      nRows=("dividendid", "size"),
                      inBriefCashTypes=("dividendtypeid", lambda s: bool(s.isin(CASH_CORE_TYPES).all()))).reset_index()
    agg["cashTypeClass"] = np.where(agg["inBriefCashTypes"], "core", "withholding_variant")
    agg = agg.rename(columns={"tradingitemid": "tradingItemId", "exdate": "exDate"})
    agg["kind"] = "cash"
    agg["leg"] = 0
    agg["source"] = "ciqdividend"
    agg["symbol"] = agg["tradingItemId"].map(items["symbol"])
    agg["companyName"] = agg["tradingItemId"].map(items["companyName"])
    if cash_px_db is not None:
        agg = agg.merge(cash_px_db.rename(columns={"closeBeforeAtDb": "oldCloseBeforeDb"}), on=["tradingItemId", "exDate"], how="left")
        agg["cashPerShareCacheUnitDb"] = agg["cashPerShareUSD"] / agg["adjFactorAtExDb"]
    else:
        agg["adjFactorAtExDb"] = np.nan
        agg["oldCloseBeforeDb"] = np.nan
        agg["cashPerShareCacheUnitDb"] = np.nan
    if cash_units is not None:
        agg = agg.merge(cash_units[["tradingItemId", "exDate", "adjFactorAtEx", "adjFactorDate", "oldCloseBefore"]], on=["tradingItemId", "exDate"], how="left")
        agg["cashPerShareCacheUnit"] = agg["cashPerShareUSD"] / agg["adjFactorAtEx"]
        agg["checkValueFrac"] = agg["cashPerShareCacheUnit"] / agg["oldCloseBefore"]
        agg["cacheUnitSource"] = np.where(agg["adjFactorAtEx"].notna(), "cache_v2", "none")
    else:
        agg["adjFactorAtEx"] = np.nan
        agg["adjFactorDate"] = pd.NaT
        agg["oldCloseBefore"] = np.nan
        agg["cashPerShareCacheUnit"] = np.nan
        agg["checkValueFrac"] = np.nan
        agg["cacheUnitSource"] = "none"
    agg["status"] = np.where(agg["cashPerShareCacheUnit"].notna(), "ok", "no_price")
    agg["confidence"] = np.where(agg["fxToUSD"].notna() & (agg["fxToUSD"] > 0), "high", "low")
    agg.loc[agg["fxToUSD"].isna(), "status"] = "no_fx"
    cash_out = agg.reindex(columns=CASH_COLUMNS)

    # ---------------- split ----------------
    sp = splits.rename(columns={"tradingitemid": "tradingItemId", "exdate": "exDate", "splittypeid": "splitTypeId",
                                "appliedflag": "appliedFlag", "paydate": "payDate", "recorddate": "recordDate",
                                "announceddate": "announcedDate"}).copy()
    sp = sp.sort_values(["tradingItemId", "exDate", "splitid"]).drop_duplicates(["tradingItemId", "exDate"], keep="last")
    sp["kind"] = "split"
    sp["leg"] = 0
    sp["source"] = "ciqsplit"
    sp["symbol"] = sp["tradingItemId"].map(items["symbol"])
    sp["companyName"] = sp["tradingItemId"].map(items["companyName"])
    sp["status"] = np.where(sp["rate"].notna() & (sp["rate"] > 0), "ok", "ratio_unknown")
    sp["confidence"] = "high"
    split_out = sp.reindex(columns=SPLIT_COLUMNS)

    # ---------------- stock ----------------
    st = stock.copy()
    st["kind"] = "stock"
    st["evidence"] = st["spnText"]
    st["confidence"] = "medium"
    st["ratioSource"] = np.where(st["ratioNewPerOld"].notna(), "co_acthist_text", "none")
    st["newTradingItemId"] = st["newTradingItemId"].astype("Int64")
    # Web research overrides (hand-entered, see research_log.md). Matching order: matchLeg, then the
    # leg already carrying the same new id, then the single leg with an unknown id, else a new leg.
    st["newTradingItemIdDb"] = st["newTradingItemId"]
    n_override = 0
    touched = set()
    for o in overrides.itertuples(index=False):
        tid, ex = int(o.tradingItemId), pd.Timestamp(o.exDate)
        ev = st[(st["tradingItemId"] == tid) & (st["exDate"] == ex)]
        if ev.empty:
            log(f"override for unknown event ({tid}, {ex:%Y-%m-%d}) ignored")
            continue
        new_id = int(o.newTradingItemId) if pd.notna(o.newTradingItemId) else None
        res = str(o.resolution).strip().lower()
        target = None
        if pd.notna(o.matchLeg):
            m = ev[ev["leg"] == int(o.matchLeg)]
            target = m.index[0] if len(m) else None
        if target is None and new_id is not None:
            m = ev[ev["newTradingItemId"] == new_id]
            target = m.index[0] if len(m) else None
        free = ~ev.index.isin(list(touched))
        if target is None:
            m = ev[ev["newTradingItemId"].isna() & free]
            target = m.index[0] if len(m) == 1 else None
        weak = ev["newIdMethod"].astype(str).str.contains("tier4|tier3|scored|name_mismatch|unresolved", regex=True)
        if target is None and pd.notna(o.ratioNewPerOld):
            m = ev[weak & free & ((ev["ratioNewPerOld"].astype(float) - float(o.ratioNewPerOld)).abs() < 1e-9)]
            target = m.index[0] if len(m) else None
        if target is None and len(ev) == 1 and weak.iloc[0] and free[0]:
            target = ev.index[0]
        if target is None and res == "unresolved":
            target = ev.index[0]
        if target is None:
            row = ev.iloc[0].copy()
            row["leg"] = int(ev["leg"].max()) + 1
            row["ratioNewPerOld"] = np.nan
            row["ratioNewPerOldText"] = np.nan
            row["newTradingItemId"] = pd.NA
            row["newTradingItemIdDb"] = pd.NA
            row["newIdMethod"] = "unresolved"
            row["newName"] = o.newSymbol
            row["newSymbol"] = o.newSymbol
            row["newCompanyName"] = None
            for c in ("newCloseAtExDb", "adjFactorNewAtExDb", "ratioCacheUnitDb", "checkValueFracDb"):
                row[c] = np.nan
            row["newCloseDateDb"] = pd.NaT
            st = pd.concat([st, row.to_frame().T], ignore_index=True)
            st["newTradingItemId"] = st["newTradingItemId"].astype("Int64")
            st["newTradingItemIdDb"] = st["newTradingItemIdDb"].astype("Int64")
            target = st.index[-1]
        touched.add(target)
        manual_db = str(o.url).startswith("co_acthist")
        note = f" || {'DB text (manual)' if manual_db else 'web'}: {o.evidence} [{o.url}] ({res})"
        st.at[target, "evidence"] = str(st.at[target, "evidence"] or "") + note
        if res == "unresolved":
            st.at[target, "confidence"] = "low"
            continue
        if pd.notna(o.ratioNewPerOld):
            st.at[target, "ratioNewPerOld"] = float(o.ratioNewPerOld)
            st.at[target, "ratioSource"] = "co_acthist_text_manual" if manual_db else "web"
            st.at[target, "ratioBasis"] = "per_pre_event_share(override)"
        if new_id is not None:
            st.at[target, "newTradingItemId"] = new_id
            st.at[target, "newIdMethod"] = "co_acthist_manual" if manual_db else "web"
            if isinstance(o.newSymbol, str):
                st.at[target, "newSymbol"] = o.newSymbol
        elif res == "ratio_only" and re.search(r"tier4|tier3|scored|name_mismatch", str(st.at[target, "newIdMethod"])):
            # The source says which security it is and xfeed has no line for it: drop the guessed id.
            st.at[target, "newTradingItemId"] = pd.NA
            st.at[target, "newIdMethod"] = "unresolved(no_xfeed_line)"
            st.at[target, "newSymbol"] = o.newSymbol if isinstance(o.newSymbol, str) else None
        st.at[target, "source"] = "co_acthist" if manual_db else f"web:{o.url}"
        st.at[target, "confidence"] = str(o.confidence)
        n_override += 1
    # A ratio-less keyed leg (gvkeyaqc_extra) that now shares its new id with a resolved leg is redundant.
    keyed = st["ratioNewPerOld"].astype(float).isna() & st["newTradingItemId"].notna()
    drop = []
    for i in st.index[keyed]:
        same = st[(st["tradingItemId"] == st.at[i, "tradingItemId"]) & (st["exDate"] == st.at[i, "exDate"]) &
                  (st["newTradingItemId"] == st.at[i, "newTradingItemId"]) & st["ratioNewPerOld"].astype(float).notna()]
        if len(same):
            drop.append(i)
    if drop:
        st = st.drop(index=drop).reset_index(drop=True)
        log(f"dropped {len(drop)} ratio-less keyed legs duplicated by a resolved leg")
    # Rows appended through to_frame().T are object-typed: restore the frame's dtypes.
    st["tradingItemId"] = st["tradingItemId"].astype("int64")
    st["exDate"] = pd.to_datetime(st["exDate"])
    st["leg"] = st["leg"].astype("int64")
    for c in ("ratioNewPerOld", "ratioNewPerOldText", "impliedValueFrac", "impliedValueFracExCash", "oldCloseBeforeDb",
              "adjFactorOldBeforeDb", "newCloseAtExDb", "adjFactorNewAtExDb", "ratioCacheUnitDb", "checkValueFracDb",
              "coincidentCashUSD", "dropValueFrac", "vendorCashEquivFrac", "cashEquivalentText", "splitRateAtEx"):
        st[c] = pd.to_numeric(st[c], errors="coerce").astype(float)
    for c in ("factorBoundaryDate", "oldCloseBeforeDate", "newCloseDateDb", "divExDate", "actionDate"):
        st[c] = pd.to_datetime(st[c])
    for c in ("Top500", "Top1000"):
        st[c] = st[c].fillna(False).astype(bool)
    st["newTradingItemId"] = st["newTradingItemId"].astype("Int64")
    st["newTradingItemIdDb"] = st["newTradingItemIdDb"].astype("Int64")
    # DB-side check columns are only valid for the id the DB stage priced.
    changed = st["newTradingItemId"].astype("Int64").fillna(-1) != st["newTradingItemIdDb"].astype("Int64").fillna(-1)
    for c in ("newCloseAtExDb", "adjFactorNewAtExDb"):
        st.loc[changed, c] = np.nan
    st["ratioCacheUnitDb"] = st["ratioNewPerOld"].astype(float) * st["adjFactorNewAtExDb"].astype(float) / st["adjFactorOldBeforeDb"].astype(float)
    st["checkValueFracDb"] = st["ratioNewPerOld"].astype(float) * st["newCloseAtExDb"].astype(float) / st["oldCloseBeforeDb"].astype(float)
    # Cache units (v2) or DB fallback marker.
    if old_units is not None and new_units is not None:
        st = st.merge(old_units, on=["tradingItemId", "exDate"], how="left")
        st["newTradingItemId"] = st["newTradingItemId"].astype("Int64")
        st = st.merge(new_units, on=["newTradingItemId", "exDate"], how="left")
        st["cacheUnitSource"] = np.where(st["adjFactorOldBefore"].notna() & st["adjFactorNewAtEx"].notna(), "cache_v2", "none")
    else:
        for c in ("oldCloseBefore", "adjFactorOldBefore", "newCloseAtEx", "adjFactorNewAtEx"):
            st[c] = np.nan
        st["newCloseDate"] = pd.NaT
        st["oldCloseBeforeDateCache"] = pd.NaT
        st["cacheUnitSource"] = "none"
    for c in ("ratioNewPerOld", "adjFactorNewAtEx", "adjFactorOldBefore", "newCloseAtEx", "oldCloseBefore",
              "impliedValueFrac", "impliedValueFracExCash"):
        st[c] = st[c].astype(float)
    st["ratioCacheUnit"] = st["ratioNewPerOld"] * st["adjFactorNewAtEx"] / st["adjFactorOldBefore"]
    st["checkValueFrac"] = st["ratioCacheUnit"] * st["newCloseAtEx"] / st["oldCloseBefore"]
    if cache_univ is not None and len(cache_univ):
        cu = cache_univ.set_index(["tradingItemId", "exDate"])
        keys = list(zip(st["tradingItemId"], st["exDate"]))
        t500 = pd.Series([cu["Top500"].get(k) for k in keys], index=st.index)
        t1000 = pd.Series([cu["Top1000"].get(k) for k in keys], index=st.index)
        got = t500.notna()
        st.loc[got, "Top500"] = t500[got].astype(bool)
        st.loc[got, "Top1000"] = t1000[got].fillna(False).astype(bool)
        st.loc[got, "universeSource"] = "v2"
    # Lead's list (Top500/Top1000 by v1 masks with cache drop) as a fallback flag source.
    if lead_list.exists():
        lead = pd.read_csv(lead_list, parse_dates=["date"])
        lk = lead.set_index([lead["tid"].astype("int64"), lead["date"]])
        for name in ("Top500", "Top1000"):
            flags = pd.Series([lk[name].get((int(t), pd.Timestamp(d))) for t, d in zip(st["tradingItemId"], st["exDate"])], index=st.index)
            got = flags.notna() & (st["universeSource"] != "v2")
            st.loc[got, name] = st.loc[got, name].astype(bool) | flags[got].astype(bool)
    # Status.
    status, totals, gaps, classes, notes = [], [], [], [], []
    for r in st.itertuples():
        total, gap, klass, note = np.nan, np.nan, None, None
        if not np.isfinite(r.ratioNewPerOld):
            status.append("ratio_unknown")
        elif pd.isna(r.newTradingItemId):
            status.append("new_id_unknown")
        elif not np.isfinite(r.checkValueFrac):
            status.append("no_price")
        else:
            implied = r.impliedValueFracExCash if np.isfinite(r.impliedValueFracExCash) else r.impliedValueFrac
            # Multi-leg events: the vendor factor covers all legs together.
            total = total_check(st, r)
            if np.isfinite(implied) and np.isfinite(total):
                gap = abs(implied - total)
                if gap > VALUE_TOL:
                    status.append("value_mismatch")
                    klass = "minor(<=0.10)" if gap <= 0.10 else "major(>0.10)"
                else:
                    status.append("ok")
            elif np.isfinite(implied):
                # A sibling leg is unresolved: this leg alone cannot exceed the whole distribution.
                note = "sibling_leg_unresolved"
                if r.checkValueFrac > implied + VALUE_TOL:
                    status.append("value_mismatch")
                    gap = r.checkValueFrac - implied
                    klass = "minor(<=0.10)" if gap <= 0.10 else "major(>0.10)"
                else:
                    status.append("ok")
            else:
                status.append("ok")
                note = "no_vendor_factor"
        totals.append(total); gaps.append(gap); classes.append(klass); notes.append(note)
    st["statusNote"] = notes
    st["status"] = status
    st["checkValueFracTotal"] = totals
    st["mismatchGap"] = gaps
    st["mismatchClass"] = classes
    # Confidence.
    conf = []
    for r in st.itertuples():
        if r.status == "ok":
            implied = r.impliedValueFracExCash if np.isfinite(r.impliedValueFracExCash) else r.impliedValueFrac
            conf.append("high" if np.isfinite(implied) and not r.statusNote else ("medium" if r.confidence != "low" else "low"))
        elif r.status in ("ratio_unknown", "new_id_unknown"):
            conf.append("low")
        else:
            conf.append("low" if r.status == "value_mismatch" else "medium")
    st["confidence"] = conf
    stock_out = st.reindex(columns=STOCK_COLUMNS + ["ratioSource", "spnText", "newName", "nLegsText", "tickersInText"])

    frames = [cash_out, stock_out, split_out]
    out = pd.concat(frames, ignore_index=True, sort=False)
    if master_ids is not None:
        out["inV2Master"] = out["tradingItemId"].astype("int64").isin(master_ids)
        out["newInV2Master"] = out["newTradingItemId"].map(lambda v: bool(int(v) in master_ids) if pd.notna(v) else None).astype("boolean")
    else:
        out["inV2Master"] = pd.NA
        out["newInV2Master"] = pd.NA
    out["tradingItemId"] = out["tradingItemId"].astype("int64")
    out["exDate"] = pd.to_datetime(out["exDate"])
    out["leg"] = out["leg"].astype("int64")
    for c in ("newTradingItemId", "newTradingItemIdDb", "dividendTypeId", "nRows", "splitTypeId", "appliedFlag", "currencyId"):
        out[c] = out[c].astype("Int64")
    for c in ("Top500", "Top1000", "inBriefCashTypes", "inV2Master", "newInV2Master"):
        out[c] = out[c].astype("boolean")
    for c in ("payDate", "recordDate", "announcedDate", "actionDate", "divExDate", "newCloseDate", "oldCloseBeforeDate", "factorBoundaryDate", "adjFactorDate"):
        out[c] = pd.to_datetime(out[c])
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)).astype("string")
    out = out.sort_values(["tradingItemId", "exDate", "kind", "leg"]).reset_index(drop=True)
    dup = out.duplicated(["tradingItemId", "exDate", "kind", "leg"]).sum()
    if dup:
        raise RuntimeError(f"{dup} duplicate (tradingItemId, exDate, kind, leg) rows")
    return {"out": out, "have_cache": have_cache, "cache_state": cache_state, "n_override": n_override,
            "stock": st, "n_items": len(items)}


def total_check(st: pd.DataFrame, r) -> float:
    """checkValueFrac summed over every leg that the same vendor factor boundary covers (all legs of
    one event, and consecutive-day distributions like Cendant 2006-07-31/08-01)."""
    if pd.notna(r.factorBoundaryDate):
        same = st[(st["tradingItemId"] == r.tradingItemId) & (st["factorBoundaryDate"] == r.factorBoundaryDate)]
    else:
        same = st[(st["tradingItemId"] == r.tradingItemId) & (st["exDate"] == r.exDate)]
    values = same["checkValueFrac"].astype(float)
    return float(values.sum()) if values.notna().all() else float(r.checkValueFrac)


# --------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------
VALIDATION = [
    ("COST", 2603626, "2020-12-01", "cash", "special cash $10"),
    ("EBAY", 2608344, "2015-07-20", "stock", "PayPal 1:1, implied ~0.58"),
    ("ITT", 2619996, "2011-11-01", "stock", "Exelis + Xylem 1:1 each, 1-for-2 reverse split same day"),
    ("ITT", 2619996, "2011-11-01", "split", "1-for-2 reverse split"),
    ("Liberty Media", 165754337, "2013-01-14", "stock", "new Liberty Media 1:1 (old item became Starz)"),
    ("DuPont", 383332720, "2025-11-03", "stock", "Qnity 1 per 2"),
    ("NiSource", 2634188, "2015-07-02", "stock", "Columbia Pipeline 1:1"),
    ("TFCF.A (Fox class A)", None, "2019-03-19", "stock", "Fox Corp 1/3 + Disney merger"),
]


def md_table(frame: pd.DataFrame, floatfmt: str = "{:.4f}") -> str:
    if frame.empty:
        return "_(none)_"
    cols = list(frame.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in frame.itertuples(index=False):
        cells = []
        for v in row:
            if isinstance(v, float):
                cells.append("" if np.isnan(v) else floatfmt.format(v))
            elif isinstance(v, (pd.Timestamp, dt.datetime)):
                cells.append("" if pd.isna(v) else f"{v:%Y-%m-%d}")
            else:
                cells.append("" if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA else str(v).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def divdata_crosscheck(root: Path, cash: pd.DataFrame) -> dict:
    """Compare the cash rows with T1's DivData wide files at (exDate, item): same source, same rule."""
    out = {}
    for field, col in (("divAmount_USD", "cashPerShareUSD"), ("divAmountCacheUnit", "cashPerShareCacheUnit")):
        path = root / "DivData" / f"{field}.parquet"
        if not path.exists():
            continue
        table = pq.read_table(str(path))
        names = table.schema.names
        index_name = next((n for n in names if n.startswith("__index_level") or n == "date"), None)
        dates = pd.to_datetime(table.column(index_name).to_numpy())
        cols = [n for n in names if n != index_name]
        col_pos = {c: i for i, c in enumerate(cols)}
        matrix = np.column_stack([np.asarray(table.column(c).to_numpy(zero_copy_only=False), dtype=float) for c in cols]) if cols else np.zeros((len(dates), 0))
        del table
        sub = cash
        pos_d = dates.searchsorted(sub["exDate"].values)
        ok_d = (pos_d < len(dates)) & (dates[np.minimum(pos_d, len(dates) - 1)] == sub["exDate"].values)
        pos_c = np.array([col_pos.get(str(int(t)), -1) for t in sub["tradingItemId"]])
        both = ok_d & (pos_c >= 0)
        mine = sub[col].astype(float).values
        theirs = np.full(len(sub), np.nan)
        theirs[both] = matrix[pos_d[both], pos_c[both]]
        diff = np.abs(mine - theirs)
        agree = both & (diff <= 1e-6 + 1e-6 * np.abs(mine))
        t1_total = int(np.isfinite(matrix).sum() - (matrix == 0).sum()) if matrix.size else 0
        out[field] = {"rows_compared": int(both.sum()), "agree": int(agree.sum()),
                      "disagree": int((both & ~agree & np.isfinite(theirs)).sum()),
                      "t1_nan_where_mine": int((both & ~np.isfinite(theirs)).sum()),
                      "my_rows_not_in_t1_layout": int((~both).sum()), "t1_nonzero_cells": t1_total,
                      "max_abs_diff": float(np.nanmax(diff[both])) if both.any() else float("nan")}
        bad = sub[both & ~agree & np.isfinite(theirs)].assign(t1=theirs[both & ~agree & np.isfinite(theirs)])
        out[field + "_examples"] = bad[["tradingItemId", "symbol", "exDate", col, "t1", "dividendTypeIds"]].head(10)
        del matrix
    return out


def write_report(path: Path, res: dict, out_dir: Path, stage: Path, root: Path, lead_list: Path, fox_note: str) -> None:
    out, st = res["out"], res["stock"]
    lines = ["# Distributions build report (cache_v2)", "",
             f"Built {dt.datetime.now():%Y-%m-%d %H:%M} by build_distributions.py. Output `{out_dir / 'distributions.parquet'}`.",
             f"Cache units: {'v2 cache (adjustmentFactor/close from ' + str(root) + ')' if res['have_cache'] else 'NOT converted - T1 MANIFEST.json absent; cache fields NaN, status=no_price; DB-derived *Db columns filled'}.",
             ""]
    lines += ["## Row counts", "", md_table(out.groupby(["kind", "status"]).size().reset_index(name="rows")), ""]
    lines += [f"Candidate items: {res['n_items']}; items with any row: {out.tradingItemId.nunique()}; "
              f"date range {out.exDate.min():%Y-%m-%d}..{out.exDate.max():%Y-%m-%d}.", ""]
    cash = out[out.kind == "cash"]
    lines += ["## Cash", "",
              f"Cash dividend type set (coordinator update 2026-09-14, same list as T1's DivData): {CASH_TYPES}; "
              f"excluded: {EXCLUDED_DIV_TYPES} (20 = spin-off candidate source). cashTypeClass = core {CASH_CORE_TYPES} | "
              f"withholding_variant {CASH_VARIANT_TYPES} (inBriefCashTypes = core).", "",
              f"Rows {len(cash)} ((item, exDate) sums of {int(cash.nRows.sum())} ciqdividend rows); core types: "
              f"{int(cash.inBriefCashTypes.sum())} rows; withholding variants: {int((~cash.inBriefCashTypes.astype(bool)).sum())} rows; "
              f"non-USD converted with ciqexchangerate snapId 6: {int((cash.currencyId != USD).sum())} rows; no FX: {int((cash.status == 'no_fx').sum())}.",
              "", "Dividend type mix:", "", md_table(cash.groupby("dividendTypeIds").size().reset_index(name="rows").sort_values("rows", ascending=False).head(15)), ""]
    if cash["checkValueFrac"].notna().any():
        big = cash[cash.checkValueFrac > 0.25].sort_values("checkValueFrac", ascending=False)
        lines += [f"Cash rows with dividend > 25% of prior close (cache units): {len(big)}; top 15:", "",
                  md_table(big[["tradingItemId", "symbol", "exDate", "cashPerShareUSD", "cashPerShareCacheUnit", "oldCloseBefore", "checkValueFrac", "dividendTypeIds"]].head(15)), ""]
    try:
        xc = divdata_crosscheck(root, cash)
    except Exception as exc:  # noqa: BLE001
        xc = {"error": str(exc)}
    lines += ["Cross-check against T1's DivData (same (exDate, item) cells, all cash types):", ""]
    for field in ("divAmount_USD", "divAmountCacheUnit"):
        if field in xc:
            lines += [f"* {field}: " + ", ".join(f"{k}={v}" for k, v in xc[field].items())]
            if len(xc[field + "_examples"]):
                lines += ["", md_table(xc[field + "_examples"]), ""]
    if "error" in xc:
        lines += [f"* cross-check failed: {xc['error']}"]
    lines += [""]
    sp = out[out.kind == "split"]
    lines += ["## Split", "", f"Rows {len(sp)}; by type:", "", md_table(sp.groupby("splitTypeId").size().reset_index(name="rows")), ""]
    stock = out[out.kind == "stock"]
    lines += ["## Stock (spin-offs / distributions of another security)", "",
              f"Rows {len(stock)} = {stock.groupby(['tradingItemId', 'exDate']).ngroups} events (multi-leg events carry leg 0,1,...).", "",
              "By status x source (web:<url> grouped as web):", "",
              md_table(stock.assign(src=stock.source.str.split(":").str[0]).groupby(["status", "src"]).size().reset_index(name="rows")), "",
              "By status x universe (membership on the day before exDate):", "",
              md_table(stock.assign(univ=np.where(stock.Top500.fillna(False), "Top500", np.where(stock.Top1000.fillna(False), "Top1000", "other")))
                       .groupby(["univ", "status"]).size().reset_index(name="rows")), "",
              "Resolution of ratio / new id:", "",
              md_table(stock.assign(ratioSrc=stock.ratioSource.fillna("none"), idSrc=stock.newIdMethod.fillna("unresolved").str.split(":").str[0])
                       .groupby(["ratioSrc", "idSrc"]).size().reset_index(name="rows")), "",
              "eventClass of stock rows without SPN text (type-20 only):", "",
              md_table(stock[stock.spnText.fillna("").str.len().eq(0) | ~stock.spnText.fillna("").str.contains("SPUN|SPIN|RECEIV|EXCHANG", regex=True)]
                       .groupby(["eventClass", "status"]).size().reset_index(name="rows")), ""]
    ok = stock[stock.status == "ok"]
    if len(ok):
        d = (ok.impliedValueFracExCash.fillna(ok.impliedValueFrac) - ok.checkValueFrac).abs()
        lines += [f"ok rows: |implied - check| median {d.median():.4f}, p90 {d.quantile(0.9):.4f}, max {d.max():.4f}.", ""]
    mm = stock[stock.status == "value_mismatch"].copy()
    if len(mm):
        mm["implied"] = mm.impliedValueFracExCash.fillna(mm.impliedValueFrac)
        lines += [f"value_mismatch rows ({len(mm)}), largest gaps:", "",
                  md_table(mm.assign(gap=(mm.implied - mm.checkValueFrac).abs()).sort_values("gap", ascending=False)
                           [["tradingItemId", "symbol", "exDate", "leg", "newSymbol", "ratioNewPerOld", "implied", "checkValueFrac", "gap", "Top500", "Top1000"]].head(25)), ""]
    # Lead's list cross-check.
    if lead_list.exists():
        lead = pd.read_csv(lead_list, parse_dates=["date"])
        merged = lead.merge(stock[["tradingItemId", "exDate", "leg", "status", "checkValueFrac", "impliedValueFrac", "ratioNewPerOld", "newSymbol"]],
                            left_on=["tid", "date"], right_on=["tradingItemId", "exDate"], how="left")
        lines += ["## Lead's spin-off list (spinoff_factor_check.csv) cross-check", "",
                  f"Lead rows {len(lead)} (Top500 {int(lead.Top500.sum())}, Top1000 {int(lead.Top1000.sum())}); "
                  f"matched to a stock row: {int(merged.tradingItemId.notna().sum())}.", "",
                  "Status of lead rows (Top500 first):", "",
                  md_table(merged.assign(univ=np.where(merged.Top500, "Top500", np.where(merged.Top1000, "Top1000", "other")),
                                         status=merged.status.fillna("not_in_distributions")).groupby(["univ", "status"]).size().reset_index(name="rows")), ""]
    # Validation cases.
    lines += ["## Validation cases", ""]
    rows = []
    for name, tid, date, kind, note in VALIDATION:
        if tid is None:
            rows.append({"case": name, "tradingItemId": "", "exDate": date, "kind": kind, "expected": note, "found": "see TFCF note below"})
            continue
        hit = out[(out.tradingItemId == tid) & (out.exDate == pd.Timestamp(date)) & (out.kind == kind)]
        if hit.empty:
            rows.append({"case": name, "tradingItemId": tid, "exDate": date, "kind": kind, "expected": note, "found": "MISSING"})
            continue
        for h in hit.itertuples():
            if kind == "cash":
                found = (f"cashPerShareUSD={h.cashPerShareUSD:.4f} cacheUnit={h.cashPerShareCacheUnit if pd.notna(h.cashPerShareCacheUnit) else float('nan'):.4f} "
                         f"type={h.dividendTypeIds} pay={h.payDate:%Y-%m-%d} status={h.status}")
            elif kind == "split":
                found = f"rate={h.rate} type={h.splitTypeId} applied={h.appliedFlag}"
            else:
                found = (f"leg{h.leg}: new={h.newTradingItemId} {h.newSymbol} ({h.newCompanyName}) ratio={h.ratioNewPerOld} "
                         f"ratioCacheUnit={h.ratioCacheUnit if pd.notna(h.ratioCacheUnit) else float('nan'):.4f} "
                         f"implied={h.impliedValueFrac if pd.notna(h.impliedValueFrac) else float('nan'):.4f} "
                         f"check={h.checkValueFrac if pd.notna(h.checkValueFrac) else float('nan'):.4f} "
                         f"(checkDb={h.checkValueFracDb if pd.notna(h.checkValueFracDb) else float('nan'):.4f}) status={h.status} "
                         f"src={h.source} newId={h.newIdMethod}")
            rows.append({"case": name, "tradingItemId": tid, "exDate": date, "kind": kind, "expected": note, "found": found})
    lines += [md_table(pd.DataFrame(rows)), "", "### TFCF.A / Disney 2019 note", "", fox_note, ""]
    lines += ["## Column reference", "",
              "* Common: tradingItemId, exDate, kind (cash|stock|split), leg (0; 1.. for extra securities of one event), symbol, companyName.",
              "* cash: cashPerShareUSD (as paid, FX to USD on exDate), cashPerShareCacheUnit = cashPerShareUSD / adjustmentFactor(exDate) "
              "(v2), cashPerShareLocal, currencyId, fxToUSD, payDate, recordDate, announcedDate, dividendTypeId (first), dividendTypeIds, "
              "nRows, inBriefCashTypes, adjFactorAtEx, oldCloseBefore (v2, current basis), checkValueFrac = cacheUnit/oldCloseBefore, "
              "*Db = same from the DB snapshot, cacheUnitSource, status ok|no_price|no_fx.",
              "* stock: newTradingItemId/newSymbol/newCompanyName, ratioNewPerOld (as traded, per share held before the event), "
              "ratioCacheUnit = ratioNewPerOld * adjFactorNewAtEx / adjFactorOldBefore (old factor on the last close before exDate, "
              "so a same-day reverse split like ITT is handled), impliedValueFrac = 1 - f_before/f_after at the divAdjFactor boundary, "
              "impliedValueFracExCash = same minus a coincident cash dividend / oldClose, checkValueFrac = ratioCacheUnit * newCloseAtEx / "
              "oldCloseBefore (v2 closes; newClose = first close on/after exDate within 7 days, date in newCloseDate), status "
              "ok|ratio_unknown|new_id_unknown|value_mismatch|no_price (mismatch = |implied - sum of legs' check| > 0.03), source "
              "ciqdividend20|co_acthist|web:<url>, evidence (SPN text [+ web note]), confidence, newIdMethod (gvkeyaqc/name/ticker : tier), "
              "eventClass (spinoff|merger|name_change|security_end|other|unknown for type-20 rows without SPN text), cashEquivalentText "
              "(vendor's cash-equivalent figure), coincidentCashUSD, Top500/Top1000 (membership on the last mask date before exDate; "
              "universeSource v1|v2), *Db columns = DB as-traded values (checkValueFracDb = ratio * newClose_at / oldClose_at).",
              "* split: rate (new per old; 0.5 = 1-for-2 reverse), splitTypeId (12 split, 11 stock dividend, 33 subdivision, 34 consolidation, 21), appliedFlag.",
              "", "## Known limits", "",
              "* Ratios come from vendor free text; multi-security texts are parsed leg by leg and unparsed legs stay ratio_unknown.",
              "* impliedValueFrac is the vendor's factor; for multi-leg events it covers all legs, so the mismatch test sums the legs.",
              "* Type-20 rows without SPN text (mergers, reorganisations) are kept with eventClass != spinoff and status ratio_unknown; "
              "settlement of terminated items is the events.parquet contract, not this table.",
              "* Foreign-currency ratios/ADS distributions (e.g. HPE -> Micro Focus ADS) are kept when the new item is a USD listing.",
              ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"wrote {path}")


def write_research_log(path: Path, res: dict, out_dir: Path) -> None:
    st = res["out"]
    stock = st[st.kind == "stock"]
    log_csv = out_dir / "research_log.csv"
    researched = pd.read_csv(log_csv, parse_dates=["exDate"], dtype={"tradingItemId": "int64"}) if log_csv.exists() else pd.DataFrame()
    lines = ["# Spin-off research log (cache_v2 Distributions)", "",
             f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by build_distributions.py from `research_log.csv` (hand-entered, one row per "
             "researched event) and the stock rows of distributions.parquet. Researcher: Claude (T2 distributions builder) with "
             "WebSearch/WebFetch; every web lookup is listed, unresolved events stay unresolved (no ratio is ever invented).", ""]
    if len(researched):
        lines += [f"## Events researched on the web ({len(researched)})", ""]
        for r in researched.sort_values(["Top500", "Top1000", "exDate"], ascending=[False, False, True]).itertuples():
            hit = stock[(stock.tradingItemId == r.tradingItemId) & (stock.exDate == pd.Timestamp(r.exDate))]
            lines += [f"### {r.symbol} ({r.tradingItemId}) {pd.Timestamp(r.exDate):%Y-%m-%d} - {r.resolution}", "",
                      f"* universe: Top500={bool(r.Top500)} Top1000={bool(r.Top1000)}",
                      f"* known from DB: {r.known_from_db}",
                      f"* why researched: {r.reason}",
                      f"* query: {r.query}",
                      f"* source: {r.url}",
                      f"* found: {r.found}",
                      f"* confidence: {r.confidence}",
                      f"* result in table: " + "; ".join(
                          f"leg{h.leg} status={h.status} ratio={h.ratioNewPerOld} new={h.newTradingItemId} {h.newSymbol} "
                          f"implied={'' if pd.isna(h.impliedValueFrac) else f'{h.impliedValueFrac:.3f}'} check={'' if pd.isna(h.checkValueFrac) else f'{h.checkValueFrac:.3f}'}"
                          for h in hit.itertuples()),
                      ""]
    open_rows = stock[stock.status.isin(["ratio_unknown", "new_id_unknown", "value_mismatch"])].copy()
    open_rows["univ"] = np.where(open_rows.Top500.fillna(False), "Top500", np.where(open_rows.Top1000.fillna(False), "Top1000", "other"))
    lines += [f"## Still open after this run ({len(open_rows)} rows)", "",
              md_table(open_rows.groupby(["univ", "status", "eventClass"]).size().reset_index(name="rows")), "",
              "Every open row (universe, then date):", "",
              md_table(open_rows.sort_values(["univ", "exDate"])[["tradingItemId", "symbol", "exDate", "leg", "status", "eventClass",
                                                                   "ratioNewPerOld", "newSymbol", "impliedValueFrac", "checkValueFrac",
                                                                   "Top500", "Top1000", "source"]]), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"wrote {path}")


def stage_write(stage: Path, root: Path, out_dir: Path, lead_list: Path) -> None:
    res = assemble(stage, root, out_dir, lead_list)
    out = res["out"]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "distributions.parquet"
    tmp = stage / "distributions.parquet"
    out.to_parquet(tmp, index=False)
    tmp.replace(target) if tmp.parent == target.parent else target.write_bytes(tmp.read_bytes())
    log(f"wrote {target}: {len(out)} rows, sha256 {sha256(target)[:16]}")
    fox_note_path = out_dir / "tfcf_note.md"
    fox_note = fox_note_path.read_text() if fox_note_path.exists() else "_(tfcf_note.md not written yet)_"
    write_report(out_dir / "distributions_report.md", res, out_dir, stage, root, lead_list, fox_note)
    write_research_log(out_dir / "research_log.md", res, out_dir)
    summary = {"rows": int(len(out)), "byKind": out.groupby("kind").size().to_dict(),
               "byKindStatus": {f"{k}/{s}": int(n) for (k, s), n in out.groupby(["kind", "status"]).size().items()},
               "haveCache": res["have_cache"], "overridesApplied": res["n_override"],
               "sha256": sha256(target), "built": dt.datetime.now().isoformat()}
    (out_dir / "distributions_manifest.json").write_text(json.dumps(summary, indent=2, default=str))
    log(json.dumps(summary["byKindStatus"]))


# --------------------------------------------------------------------------------------
def main() -> int:
    global _LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--stage-dir", default=DEFAULT_STAGE)
    ap.add_argument("--stage", choices=("db", "cache", "write", "all"), default="all")
    ap.add_argument("--poll-minutes", type=float, default=5.0)
    ap.add_argument("--poll-hours", type=float, default=4.0)
    ap.add_argument("--force-cache", action="store_true", help="redo the cache stage even if done")
    ap.add_argument("--rebuild-stock", action="store_true",
                    help="rebuild stock events; the previous file seeds the resolution cache")
    ap.add_argument("--lead-list", default=DEFAULT_LEAD_LIST)
    ap.add_argument("--statement-timeout-ms", type=int, default=1_800_000)
    args = ap.parse_args()
    root, stage, out_dir = Path(args.root), Path(args.stage_dir), Path(args.root) / "Distributions"
    stage.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    _LOG = out_dir / "build.log"
    log(f"=== build_distributions --stage {args.stage} (stage dir {stage})")
    if args.rebuild_stock:
        current = stage / "stock_events_db.parquet"
        if current.exists():
            current.replace(stage / "stock_events_prev.parquet")
            for marker in (stage / "stock_events_db.parquet.done",):
                marker.unlink(missing_ok=True)
            log("stock events: previous output moved to stock_events_prev.parquet (resolution cache)")
    if args.stage in ("db", "all"):
        conn = be.connect(args.statement_timeout_ms)
        try:
            stage_db(conn, stage)
        finally:
            conn.close()
    if args.stage in ("cache", "all"):
        stage_cache(root, stage, args.poll_minutes, args.poll_hours, force=args.force_cache)
    if args.stage in ("write", "all"):
        stage_write(stage, root, out_dir, Path(args.lead_list))
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
