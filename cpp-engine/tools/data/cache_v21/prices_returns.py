#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/prices_returns.py - derived/returns_total.parquet v2: cash-style holding total return (rc2 BRIEF section 3-2).

Contract (v2, rc2 section 3-2): rows = base_data (id, date) with close notna; columns date, tradingItemId, ret (float64),
prevDate (timestamp), retFlag (int8). Holding one OLD share at prevDate (the same id's previous observed row, missing
days skipped), apply the corporate-action ledger events with prevDate < exDate <= date in date order:

    shares = 1; cash = 0; child = 0
    for e in events:  cash += shares x cashPerShare_e ; child += shares x childValuePerOldShare_e ; shares x= parentFactor_e
    ret = (shares x close_t + cash + child) / close_prev - 1

Legs of the same exDate: value legs (cash, stock, rights; parentFactor 1) before quantity legs, so a same-day split never
scales the cash/child amounts (they are per OLD share, section 3-1). No event in (prevDate, date] -> ret is literally
close_t / close_prev - 1 (bit-identical, gate G-CASH). First observed row of an id: NaN / NaT, unless a ledger
rename/relist row links the id to an old id (childTradingItemId == this id): then prev = the old id's last raw close
strictly before the first observed date, shares start at that row's parentFactor (rc2 section 3-2).

Lineage-linked first rows (rework F1): the holder of one OLD share at the old id's last close gets, in order, (a) the OLD
id's ledger events with prevDay < exDate <= new first day (cash/child per old share, quantity legs multiply shares),
(b) the conversion to new-id shares: the product of the old id's quantity legs in that gap when there are any, else the
lineage ratio (a ratio that conflicts with the old quantity legs by > 1e-4 relative -> retFlag 4, reason ratio_conflict), (c) the
NEW id's own events with exDate in the same gap on the converted share count, skipping legs that duplicate an old-id gap leg
(same exDate, legType, child, amount). Link plausibility (rework F4): |first ret| > 0.5 -> retFlag 4, old id still quoted
on/after the new id's first day (overlap) -> retFlag >= 3, a new-id leg applied on a linked first row with a conversion
ratio != 1 (basis ambiguity, MTCH-style) -> retFlag >= 3; all listed by returns_v2_qa in lineage_links.csv.
Links come from master/lineage.parquet (R3 lineage v2: relation rename/relist, chainGapTradingDays <= 5, ratio NaN ->
1.0 estimated); without a lineage file the ledger's own rename/relist rows are used.

retFlag: 0 price_only (also split-only rows and plain linked rows), 1 cash leg, 2 distribution leg (stock/rights with a
priced child), 3 estimate (isEstimate / status estimated / childValueSource event_value|vendor_implied), 4 unresolved
(a leg with status unresolved or a value leg without an amount -> the value is incomplete; the price part is kept;
also link ratio conflict / implausible linked first return / rows after a stale ledger's asOf, see `stale_after`).
Precedence per row: 4 > 3 > 2 > 1 > 0.

Ledger rows applied: eventType in APPLY_TYPES (cash_dividend, special_dividend, stock_dividend, split, spin_off, rights,
warrant, other) EXCEPT rows with source == corporate_actions (rework F2: merger_cash / merger_stock / `other` PVT
settlement legs whose cash is the deal price belong to the engine events path, P6; applying them would double count the
deal price with the last close). rename/relist rows are used only for linking. Merger settlement days therefore carry the
price return only (flag 0), as in rc1. Vendor factors (factors/dividend.parquet) are NOT used (P5). `--legacy` keeps the
rc1 vendor-factor computation (adj = close/splitFactor x divAdjFactor, 4-column schema) for comparison.
`stale_after=<date>` (update_daily with a carried-forward ledger) marks every row after that date retFlag 4: the ledger
has no information there, so the value is unverified rather than price-only.
Stitched history: the vendor appends the trading id's history to successor ids. R1's final ledger carries those legs on
the successor ids too (source = stitched_copy, keeperEventId, exportExclude = true for the engine export) and they are
applied like any event (XRX 2665260 and 610592433 both get the 2017 CNDT leg). For a ledger without such rows, R1's
`ca_ledger_stitched_copies.csv` (next to the ledger, auto-detected) is mirrored onto the successor ids instead
(`mirror_stitched_copies`, `history_*` reasons only).

Processing is row group by row group (one id chunk per group, ids never straddle groups) so RSS stays small.

CLI:
  prices_returns.py --stage <dir> --ledger <ca_ledger.parquet> [--lineage <lineage.parquet>] [--stale-after YYYY-MM-DD] [--out ...]
  prices_returns.py --stage <dir> --legacy                                    (rc1 vendor-factor total return)
  prices_returns.py --self-test
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
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import rss_gb  # noqa: E402
from prices_factors import FactorLookup, check_intervals  # noqa: E402

DEFAULT_STAGE = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T2")
RET_SCHEMA = pa.schema([("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()), ("ret", pa.float64()),
                        ("prevDate", pa.timestamp("ns"))])
RET_SCHEMA_V2 = pa.schema([("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()), ("ret", pa.float64()),
                           ("prevDate", pa.timestamp("ns")), ("retFlag", pa.int8())])
FLAG_PRICE, FLAG_CASH, FLAG_DIST, FLAG_EST, FLAG_UNRES = 0, 1, 2, 3, 4
FLAG_NAMES = {0: "price_only", 1: "cash", 2: "distribution", 3: "estimate", 4: "unresolved"}
APPLY_TYPES = ("cash_dividend", "special_dividend", "stock_dividend", "split", "spin_off", "rights", "warrant", "other")
EXCLUDED_SOURCES = ("corporate_actions",     # settlement legs (engine events path, P6) -- rework F2
                    "lifecycle")             # D1 (lead decision 2026-09-21): co_acthist-stale termination marker rows (eventType other, no value) —
                                             # display-only for the engine export (markerReason coacthist_stale_termination); the last-trade-day
                                             # return keeps its flag like the ~8k historical terminations without terms
LINK_TYPES = ("rename", "relist")
VALUE_LEGS = ("stock", "rights")
MAX_CHAIN_GAP = 5                       # lineage v2 chainGapTradingDays above this -> not chained (R3 rule)
LINK_RET_SUSPECT = 0.5                  # |linked first return| above this -> retFlag 4 (rework F4)
RATIO_CONFLICT_TOL = 1e-4               # lineage ratio vs old-id quantity product: sources round differently (0.0666667 vs 0.066667)
MIRROR_TAIL_TOL = 5e-3                  # rc2f: old id's last close within this of the new id's same-day close -> mirror tail, conversion ratio not applied
DAY_OFFSET = 30000                      # 1900-01-01 = -25567 days since 1970 -> keys stay >= 0
DAY_BITS = 20
NAT = np.datetime64("NaT", "ns")
log = logging.getLogger("cache_v21.prices_returns")


def _days(x) -> np.ndarray:
    return np.asarray(x).astype("datetime64[D]").astype(np.int64)


def _keys(ids, days) -> np.ndarray:
    """(id, day) -> one sortable int64 key. Days are packed in 20 bits (rework F9: guarded; NaT days are min int64)."""
    d = np.asarray(days, dtype=np.int64) + DAY_OFFSET
    if len(d) and (d.min() < 0 or d.max() >= (1 << DAY_BITS)):
        raise ValueError(f"date outside the packable range (NaT or beyond year 2841): days {d.min() - DAY_OFFSET}..{d.max() - DAY_OFFSET}")
    return (np.asarray(ids, dtype=np.int64) << DAY_BITS) | d


# --------------------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------------------
def leg_flags(df: pd.DataFrame) -> np.ndarray:
    """Per ledger row retFlag contribution (see module doc)."""
    leg = df.legType.astype("object").fillna("").to_numpy()
    status = df.status.astype("object").fillna("ok").to_numpy()
    cps = df.cashPerShare.to_numpy(dtype=np.float64)
    cval = df.childValuePerOldShare.to_numpy(dtype=np.float64)
    src = df.childValueSource.astype("object").fillna("unknown").to_numpy() if "childValueSource" in df else np.full(len(df), "unknown")
    est = df.isEstimate.fillna(False).to_numpy(dtype=bool) if "isEstimate" in df else np.zeros(len(df), bool)
    is_value = np.isin(leg, VALUE_LEGS)
    is_cash = leg == "cash"
    unres = (status == "unresolved") | (is_value & ~np.isfinite(cval)) | (is_cash & ~np.isfinite(cps))
    estim = est | (status == "estimated") | np.isin(src, ["event_value", "vendor_implied"])
    f = np.zeros(len(df), dtype=np.int8)
    f[is_cash] = FLAG_CASH
    f[is_value] = FLAG_DIST
    f[estim] = FLAG_EST
    f[unres] = FLAG_UNRES
    return f


def applied_mask(df: pd.DataFrame, lifecycle: pd.DataFrame | None = None, legacy: bool = False) -> np.ndarray:
    """Ledger rows the returns generator applies: APPLY_TYPES and not from an excluded source (rework F2).
    (rc2f R-X0 P0-1: the settlement-cash rule — corporate_actions cash legs with exDate <= lastTradeDate applied as cash — was removed;
    such rows are corrected per event by ledger_overrides `set eventType=special_dividend` when the price actually dropped, so the
    generator (APPLY_TYPES), the engine export and the events file agree. `lifecycle`/`legacy` are accepted for signature stability.)"""
    m = df.eventType.isin(APPLY_TYPES).to_numpy()
    if "source" in df:
        m &= ~df.source.astype("object").fillna("").isin(EXCLUDED_SOURCES).to_numpy()
    return m


class Ledger:
    """Applied events sorted (tradingItemId, exDate, value legs first) as flat arrays + link map for rename/relist."""

    def __init__(self, df: pd.DataFrame, lifecycle: pd.DataFrame | None = None, legacy_rules: bool = False):
        need = {"tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "parentFactor", "childTradingItemId", "childValuePerOldShare", "status"}
        missing = need - set(df.columns)
        if missing:
            raise ValueError(f"ledger missing columns {sorted(missing)}")
        df = df.copy()
        df["tradingItemId"] = df.tradingItemId.astype("int64")
        df["exDate"] = pd.to_datetime(df.exDate).astype("datetime64[ns]")
        if df.exDate.isna().any():
            raise ValueError(f"ledger has {int(df.exDate.isna().sum())} rows with NaT exDate")
        pf = pd.to_numeric(df.parentFactor).astype("float64").fillna(1.0)
        if ((pf <= 0) | ~np.isfinite(pf)).any():
            raise ValueError("ledger parentFactor must be finite and > 0")
        df["parentFactor"] = pf
        m = applied_mask(df, lifecycle=lifecycle, legacy=legacy_rules)
        self.n_excluded_source = int((df.eventType.isin(APPLY_TYPES) & ~m).sum())
        self.n_settlement_cash_applied = 0        # R-X0 P0-1: rule removed
        a = df[m].copy()
        a["_q"] = (a.parentFactor != 1.0).astype(np.int8)           # quantity legs after value legs on the same day
        # rework F10: a value leg may carry the parent conversion (section 3-1), but not next to another quantity leg
        # on the same day (the conversion would be applied twice) -> explicit error instead of a silent double count
        vq = a[(a._q == 1) & a.legType.isin(VALUE_LEGS)]
        if len(vq):
            k = set(zip(vq.tradingItemId, vq.exDate))
            others = a[(a._q == 1) & ~a.index.isin(vq.index)]
            clash = [x for x in zip(others.tradingItemId, others.exDate) if x in k]
            if clash:
                raise ValueError(f"ledger: value leg with parentFactor != 1 next to another quantity leg on the same day, e.g. {clash[:3]}")
        a = a.sort_values(["tradingItemId", "exDate", "_q"], kind="stable")
        self.n = len(a)
        self.ids = a.tradingItemId.to_numpy(np.int64)
        self.ex_days = _days(a.exDate.to_numpy())
        self.keys = _keys(self.ids, self.ex_days)
        self.cps = np.nan_to_num(a.cashPerShare.to_numpy(np.float64), nan=0.0)
        self.cval = np.nan_to_num(a.childValuePerOldShare.to_numpy(np.float64), nan=0.0)
        self.pf = a.parentFactor.to_numpy(np.float64)
        self.flag = leg_flags(a)
        self.leg = a.legType.astype("object").fillna("").to_numpy()
        self.event_type = a.eventType.to_numpy()
        self.child = pd.to_numeric(a.childTradingItemId, errors="coerce").fillna(-1).to_numpy(np.int64)
        # link rows from the ledger's own rename/relist rows (fallback when no lineage file is given)
        l = df[df.eventType.isin(LINK_TYPES) & df.childTradingItemId.notna()].copy()
        l["childTradingItemId"] = l.childTradingItemId.astype("int64")
        l["_flag"] = leg_flags(l)
        l["_rank"] = l.status.map({"ok": 0, "estimated": 1, "unresolved": 2}).fillna(3)
        l["ratioAssumed"] = l.status.astype(str).eq("estimated") | l.isEstimate.fillna(False).astype(bool)
        l["chainGap"] = -1
        self.n_links_multi_old = int(l.childTradingItemId.duplicated().sum())
        self.links = l.sort_values(["childTradingItemId", "_rank", "exDate"], ascending=[True, True, False]).drop_duplicates("childTradingItemId")[
            ["childTradingItemId", "tradingItemId", "parentFactor", "_flag", "exDate", "ratioAssumed", "chainGap"]].rename(
            columns={"childTradingItemId": "newId", "tradingItemId": "oldId", "_flag": "flag"}).reset_index(drop=True)
        self.links["linkSource"] = "ledger"
        self.links_excluded = pd.DataFrame(columns=["newId", "oldId", "parentFactor", "chainGap", "reason"])

    def use_lineage(self, lineage: pd.DataFrame, legacy: bool = False) -> None:
        """Replace the link map with lineage v2 (R3): relation rename/relist, chainGapTradingDays <= MAX_CHAIN_GAP (+ chainable unless legacy)."""
        self.links, self.links_excluded, self.n_links_multi_old = lineage_links(lineage, legacy=legacy)

    def gap_events(self, tid: int, day_lo: int, day_hi: int) -> slice:
        """Applied events of `tid` with day_lo < exDay <= day_hi (positions in the flat arrays)."""
        lo = np.searchsorted(self.keys, _keys([tid], [day_lo])[0], side="right")
        hi = np.searchsorted(self.keys, _keys([tid], [day_hi])[0], side="right")
        return slice(int(lo), int(hi))

    def slice(self, id_lo: int, id_hi: int) -> slice:
        lo = np.searchsorted(self.ids, id_lo, side="left")
        hi = np.searchsorted(self.ids, id_hi, side="right")
        return slice(lo, hi)


STITCHED_REASONS = ("history_appended_to_successor", "history_duplicate_no_trading_id")   # mirrored; transition-leg copies are handled by the link path


def mirror_stitched_copies(df: pd.DataFrame, csv: Path) -> tuple[pd.DataFrame, dict]:
    """R1 hands over `ca_ledger_stitched_copies.csv` (copy id, exDate, legType, keeperId, keeperEventId, reason): legs the
    ledger keeps only on the TRADING id although the vendor appended that history to a successor id. For reasons in
    STITCHED_REASONS the keeper's ledger row is cloned onto the copy id (same amounts/status/source; notes record the
    keeper eventId), so the appended history of the successor id carries the same holding returns as the trading id
    (oracle: XRX 2665260 and 610592433 both get the CNDT leg). `successor_copy_of_transition_leg` rows are NOT mirrored:
    the linked first row applies the old id's gap legs (rework F1). Copies whose key already exists are skipped."""
    sc = pd.read_csv(csv)
    st = {"file": str(csv), "rows": int(len(sc)), "mirrored": 0, "skipped_transition_leg": 0, "skipped_keeper_missing": 0, "skipped_existing": 0}
    if len(sc) == 0 or "eventId" not in df:
        return df, st
    reason = sc.reason.astype(str).str.split(":").str[0]
    take = sc[reason.isin(STITCHED_REASONS)].copy()
    st["skipped_transition_leg"] = int(len(sc) - len(take))
    keep = df.set_index("eventId")
    take = take[take.keeperEventId.isin(keep.index)]
    st["skipped_keeper_missing"] = int(len(sc) - st["skipped_transition_leg"] - len(take))
    if len(take) == 0:
        return df, st
    rows = keep.loc[take.keeperEventId.to_numpy()].reset_index()
    rows["tradingItemId"] = take.tradingItemId.to_numpy(np.int64)
    rows["notes"] = rows.notes.astype("object").fillna("").astype(str) + ";stitched_copy_of:" + take.keeperEventId.to_numpy().astype(str)
    rows["eventId"] = rows.tradingItemId.astype(str) + "_" + take.keeperEventId.to_numpy().astype(str) + "_stitched"
    rows["_stitched"] = True
    rows["_effSource"] = rows.source.astype("object").to_numpy(dtype=object)
    key = ["tradingItemId", "exDate", "legType", "childTradingItemId"]
    existing = set(zip(df.tradingItemId.astype("int64"), pd.to_datetime(df.exDate), df.legType.astype(str), df.childTradingItemId.astype("object").where(df.childTradingItemId.notna(), -1).astype("int64")))
    rk = list(zip(rows.tradingItemId.astype("int64"), pd.to_datetime(rows.exDate), rows.legType.astype(str), rows.childTradingItemId.astype("object").where(rows.childTradingItemId.notna(), -1).astype("int64")))
    m = np.array([k not in existing for k in rk], dtype=bool)
    st["skipped_existing"] = int((~m).sum())
    rows = rows[m]
    st["mirrored"] = int(len(rows))
    out = df.copy()
    if "_stitched" not in out:
        out["_stitched"] = False
    if "_effSource" not in out:
        out["_effSource"] = out.source.astype("object").to_numpy(dtype=object)
    return pd.concat([out, rows[out.columns]], ignore_index=True), st


def load_ledger(path: Path, stitched_copies: Path | bool | None = None) -> tuple[pd.DataFrame, dict] | pd.DataFrame:
    """Ledger frame (+ mirrored stitched copies). stitched_copies: path, None = auto (`ca_ledger_stitched_copies.csv` next to
    the ledger when present), False = off. Returns the frame; `load_ledger_info(path, ...)` returns (frame, stats)."""
    return load_ledger_info(path, stitched_copies)[0]


def load_ledger_info(path: Path, stitched_copies: Path | bool | None = None) -> tuple[pd.DataFrame, dict]:
    path = Path(path)
    df = pd.read_parquet(path)
    if "childValueSource" not in df:
        df["childValueSource"] = "unknown"
    if "isEstimate" not in df:
        df["isEstimate"] = False
    st = {"file": None, "rows": 0, "mirrored": 0, "in_ledger": 0}
    # ledger-native stitched copies (R1 final: source = stitched_copy, keeperEventId, exportExclude): applied like any
    # event; the effective source for the QA cash label is the keeper's source
    src = df.source.astype("object").fillna("") if "source" in df else pd.Series("", index=df.index)
    native = (src == "stitched_copy").to_numpy()
    df["_stitched"] = native
    df["_effSource"] = src.to_numpy(dtype=object)
    if native.any():
        st["in_ledger"] = int(native.sum())
        if "keeperEventId" in df and "eventId" in df:
            keeper_src = df.set_index("eventId").source.astype("object")
            keeper_src = keeper_src[~keeper_src.index.duplicated()]
            ks = df.loc[native, "keeperEventId"].map(keeper_src)
            df.loc[native, "_effSource"] = ks.fillna("stitched_copy").to_numpy(dtype=object)
        stitched_copies = False                                        # never mirror on top of ledger-native copies
    if stitched_copies is None:
        cand = path.with_name("ca_ledger_stitched_copies.csv")
        stitched_copies = cand if cand.exists() else False
    if stitched_copies:
        df, st2 = mirror_stitched_copies(df, Path(stitched_copies))
        st.update(st2)
    df["_effSource"] = df["_effSource"].where(df["_effSource"].notna(), df.source if "source" in df else "")
    return df, st


def lineage_links(lineage: pd.DataFrame, legacy: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """master/lineage.parquet (rc1 or R3 v2) -> link map (newId, oldId, parentFactor, flag, exDate, ratioAssumed, chainGap,
    linkSource). relation rename/relist only; chainGapTradingDays > MAX_CHAIN_GAP excluded (returned separately);
    ratio NaN -> 1.0 with flag 3 (estimate); several old ids per new id -> smallest chain gap, then confidence, then
    latest effectiveDate (count returned, rework F11).
    rc2f (X5 NEW-X5-1, lead decision): only rows with `chainable == True` link (R3 F7: high/medium, ratio known, gap <= 5; rc2f
    lineage_overrides set chainable); an unlinked new id keeps a NaN first row. ratioAssumed also covers ratioSource
    assumed_1to1 / stitched_1to1 / rc2f_estimate* (estimate flag 3). `legacy=True` restores the rc2 behaviour (bit-identical gate)."""
    l = lineage[lineage.relation.isin(LINK_TYPES)].copy()
    l["chainGap"] = pd.to_numeric(l["chainGapTradingDays"], errors="coerce").fillna(-1).astype(int) if "chainGapTradingDays" in l else -1
    excl = l[l.chainGap > MAX_CHAIN_GAP]
    l = l[l.chainGap <= MAX_CHAIN_GAP].copy()
    excl_nc = l.iloc[0:0]
    if not legacy and "chainable" in l.columns:
        nc = ~l.chainable.fillna(False).astype(bool)
        excl_nc = l[nc]
        l = l[~nc].copy()
    l["ratioAssumed"] = l.ratio.isna()
    if not legacy and "ratioSource" in l.columns:
        src = l.ratioSource.fillna("").astype(str)
        l["ratioAssumed"] = l.ratioAssumed | src.isin(["assumed_1to1", "stitched_1to1"]) | src.str.startswith("rc2f_estimate")
    l["parentFactor"] = pd.to_numeric(l.ratio).astype("float64").fillna(1.0)
    if ((l.parentFactor <= 0) | ~np.isfinite(l.parentFactor)).any():
        raise ValueError("lineage ratio must be finite and > 0 (or NaN)")
    l["flag"] = np.where(l.ratioAssumed, FLAG_EST, FLAG_PRICE).astype(np.int8)
    l["_conf"] = l.confidence.map({"high": 0, "medium": 1, "low": 2}).fillna(3)
    n_multi = int(l.newId.duplicated().sum())
    l = l.sort_values(["newId", "chainGap", "_conf", "effectiveDate"], ascending=[True, True, True, False]).drop_duplicates("newId")
    # rc2f-E (lead decision): lineage_overrides `linkVerified` = X5 verified a genuine first-day market move (de-SPAC etc.) -> the link row keeps the
    # regular flag instead of the |ret| > LINK_RET_SUSPECT -> 4 rule
    l["linkVerified"] = l.linkVerified.map(lambda x: str(x).strip().lower() in ("true", "1", "yes")) if ("linkVerified" in l.columns and not legacy) else False
    out = l[["newId", "oldId", "parentFactor", "flag", "effectiveDate", "ratioAssumed", "chainGap", "linkVerified"]].rename(columns={"effectiveDate": "exDate"}).reset_index(drop=True)
    out["newId"] = out.newId.astype("int64"); out["oldId"] = out.oldId.astype("int64")
    out["linkSource"] = "lineage"
    ex = pd.DataFrame({"newId": excl.newId.astype("int64"), "oldId": excl.oldId.astype("int64"), "parentFactor": excl.ratio, "chainGap": excl.chainGap,
                       "reason": "chain_gap>" + str(MAX_CHAIN_GAP)}).reset_index(drop=True)
    if len(excl_nc):
        ex2 = pd.DataFrame({"newId": excl_nc.newId.astype("int64"), "oldId": excl_nc.oldId.astype("int64"), "parentFactor": excl_nc.ratio, "chainGap": excl_nc.chainGap,
                            "reason": "not_chainable"}).reset_index(drop=True)
        ex = pd.concat([ex, ex2], ignore_index=True)
    return out, ex, n_multi


def _gap_apply(ledger: Ledger, tid: int, day_lo: int, day_hi: int) -> dict:
    """Apply `tid`'s ledger events with day_lo < exDay <= day_hi to one share (section 3-2 loop); returns the aggregates
    and the leg keys (for duplicate detection on the linked first row)."""
    sl = ledger.gap_events(tid, day_lo, day_hi)
    shares, cash, child, flag, has_qty = 1.0, 0.0, 0.0, 0, False
    keys = []
    for k in range(sl.start, sl.stop):
        cash += shares * ledger.cps[k]
        child += shares * ledger.cval[k]
        shares *= ledger.pf[k]
        has_qty |= ledger.pf[k] != 1.0
        flag = max(flag, int(ledger.flag[k]))
        keys.append(_leg_key(ledger, k))
    return {"n": sl.stop - sl.start, "cash": cash, "child": child, "pf": shares, "hasQty": has_qty, "flag": flag, "keys": keys,
            "legs": "|".join(f"{ledger.event_type[k]}:{ledger.leg[k]}:{ledger.cps[k]:g}:{ledger.cval[k]:g}:{ledger.pf[k]:g}" for k in range(sl.start, sl.stop))}


def _leg_key(ledger: Ledger, k: int) -> tuple:
    child = int(ledger.child[k]) if ledger.child[k] >= 0 else -1
    return (int(ledger.ex_days[k]), str(ledger.leg[k]), child, round(float(ledger.cps[k]), 6), round(float(ledger.cval[k]), 6), round(float(ledger.pf[k]), 9))


def resolve_links(ledger: Ledger, base: Path, lifecycle: pd.DataFrame | None = None, legacy: bool = False) -> pd.DataFrame:
    """newId -> (oldId, linkDay, prevClose, prevDay, conversion, gap events of the old id, flags). prev = old id's last finite close
    strictly before the link row. Rows without such a close are returned with prevClose NaN (reported,
    not linked). Rework F1: the old id's ledger events with prevDay < exDate <= linkDay are pre-applied to one old
    share (gapCash/gapChild per old share, gapPF = product of its quantity legs); the conversion convPF = gapPF when the
    old id has quantity legs in the gap (a lineage ratio that differs by > RATIO_CONFLICT_TOL -> ratioConflict, retFlag 4; an assumed
    ratio is simply superseded), else the lineage ratio.
    rc2f (X5 NEW-X5-5, lead decision): the old id's last trade day is capped at lifecycle.lastTradeDate (mirror tails cut by X4).
    When the old id still trades on/after the new id's first print (when-issued overlap) the link row moves to the FIRST new-id
    row AFTER the old id's last trade day (linkMode after_old_last): prev = old last close, gap legs = old id's legs in
    (old last day, link day]; earlier new-id rows keep their own price returns (first row NaN). No new-id row after the old
    id's last day -> not linked (state old_outlives_new). `legacy=True`: rc2 behaviour (link at the first row, overlap flag 3)."""
    L = ledger.links
    cols = ["newId", "oldId", "newFirstDay", "linkDay", "linkMode", "prevDay", "prevClose", "parentFactor", "flag", "overlap", "ratioAssumed", "chainGap", "linkSource",
            "gapN", "gapCash", "gapChild", "gapPF", "gapHasQty", "gapFlag", "gapLegs", "convPF", "ratioConflict", "gapKeys", "mirrorTail", "linkVerified"]
    if len(L) == 0:
        return pd.DataFrame(columns=cols)
    if "linkVerified" not in L.columns:
        L = L.assign(linkVerified=False)
    ids = np.unique(np.r_[L.newId.to_numpy(np.int64), L.oldId.to_numpy(np.int64)])
    t = pq.read_table(base, columns=["tradingItemId", "date", "close"], filters=[("tradingItemId", "in", ids.tolist())])
    d = t.to_pandas()
    d = d[np.isfinite(d.close.to_numpy())].sort_values(["tradingItemId", "date"])
    if lifecycle is not None and not legacy and len(d):
        # old-id prints after lifecycle.lastTradeDate are not trading (mirror tails, post-lifecycle quotes): drop them for the link decision
        ltd = pd.to_datetime(d.tradingItemId.map(lifecycle.set_index("tradingItemId").lastTradeDate)).to_numpy().astype("datetime64[ns]")
        keep = pd.isna(ltd) | (d.date.to_numpy().astype("datetime64[ns]") <= ltd)
        d = d[keep]
    first = d.groupby("tradingItemId").date.min()
    last = d.groupby("tradingItemId").date.max()
    r = L.copy()
    r["newFirstDate"] = r.newId.map(first)
    r = r[r.newFirstDate.notna()].copy()
    rid = d.tradingItemId.to_numpy(np.int64); rday = _days(d.date.to_numpy()); rclose = d.close.to_numpy(np.float64)
    rkeys = _keys(rid, rday)
    r["newFirstDay"] = _days(r.newFirstDate.to_numpy())
    old_last_day = _days(r.oldId.map(last).to_numpy().astype("datetime64[ns]"))
    overlap = old_last_day >= r.newFirstDay.to_numpy(np.int64)
    link_day = r.newFirstDay.to_numpy(np.int64).copy()
    mode = np.full(len(r), "first_row", dtype=object)
    if not legacy and overlap.any():
        # first new-id row strictly after the old id's last trade day
        qn = _keys(r.newId.to_numpy(np.int64)[overlap], old_last_day[overlap])
        pn = np.searchsorted(rkeys, qn, side="right")
        pnc = np.minimum(pn, len(rkeys) - 1)
        has_next = (pn < len(rkeys)) & (rid[pnc] == r.newId.to_numpy(np.int64)[overlap])
        ld = np.where(has_next, rday[pnc], -1)
        link_day[overlap] = ld
        mode[overlap] = np.where(has_next, "after_old_last", "old_outlives_new")
    r["linkDay"] = link_day
    r["linkMode"] = mode
    q = _keys(r.oldId.to_numpy(np.int64), np.where(link_day >= 0, link_day, 0))
    pos = np.searchsorted(rkeys, q, side="left") - 1
    ok = (pos >= 0) & (link_day >= 0)
    p = np.maximum(pos, 0)
    ok &= rid[p] == r.oldId.to_numpy(np.int64)
    r["prevClose"] = np.where(ok, rclose[p], np.nan)
    r["prevDay"] = np.where(ok, rday[p], -1)
    r["overlap"] = overlap if legacy else (overlap & (mode != "after_old_last"))
    # rc2f (R-X0 P2-9): mirror tail — the old id's last close equals the new id's close on the same day (±MIRROR_TAIL_TOL): the tail is a vendor
    # copy of the new line in the new line's units, so the conversion ratio must not be applied again (convPF 1, linkMode after_old_last_mirror)
    r["mirrorTail"] = False
    if not legacy and (mode == "after_old_last").any():
        sel = np.flatnonzero((mode == "after_old_last") & ok)
        if len(sel):
            qn = _keys(r.newId.to_numpy(np.int64)[sel], r.prevDay.to_numpy(np.int64)[sel])
            pn = np.searchsorted(rkeys, qn, side="left")
            pnc = np.minimum(pn, len(rkeys) - 1)
            hit = (pn < len(rkeys)) & (rkeys[pnc] == qn)
            new_close = np.where(hit, rclose[pnc], np.nan)
            old_close = r.prevClose.to_numpy(np.float64)[sel]
            mirror = hit & np.isfinite(new_close) & (np.abs(old_close / new_close - 1.0) <= MIRROR_TAIL_TOL)
            mt = r.mirrorTail.to_numpy().copy(); mt[sel] = mirror; r["mirrorTail"] = mt
            mode2 = r.linkMode.to_numpy().copy(); mode2[sel[mirror]] = "after_old_last_mirror"; r["linkMode"] = mode2
            # rc2f-E (X5 hand-over): is the mirrored tail in NEW units (the new series is continuous across the link day -> convPF 1) or in OLD
            # units (the new series steps by ~1/ratio at the link day: the reverse split happens at the relist -> convPF = ratio)? Decide by the
            # new id's own step across the link: old units when |ratio x step - 1| <= |step - 1|
            ql = _keys(r.newId.to_numpy(np.int64)[sel], r.linkDay.to_numpy(np.int64)[sel])
            pl = np.searchsorted(rkeys, ql, side="left"); plc = np.minimum(pl, len(rkeys) - 1)
            hitl = (pl < len(rkeys)) & (rkeys[plc] == ql)
            link_close = np.where(hitl, rclose[plc], np.nan)
            step = np.where(hit & hitl & (new_close > 0), link_close / np.where(new_close > 0, new_close, np.nan), np.nan)
            ratio_sel = r.parentFactor.to_numpy(np.float64)[sel]
            old_units = np.isfinite(step) & (np.abs(ratio_sel * step - 1.0) <= np.abs(step - 1.0))
            mo = np.zeros(len(r), dtype=bool); mo[sel] = mirror & old_units; r["_mirrorOldUnits"] = mo
    if "ratioAssumed" not in r:
        r["ratioAssumed"] = False
    if "chainGap" not in r:
        r["chainGap"] = -1
    if "linkSource" not in r:
        r["linkSource"] = "ledger"
    # old-id events inside the link gap (rework F1)
    gaps = [(_gap_apply(ledger, int(o), int(pd_), int(f)) if pd_ >= 0 else {"n": 0, "cash": 0.0, "child": 0.0, "pf": 1.0, "hasQty": False, "flag": 0, "keys": [], "legs": ""})
            for o, pd_, f in zip(r.oldId.to_numpy(np.int64), r.prevDay.to_numpy(np.int64), r.linkDay.to_numpy(np.int64))]
    r["gapN"] = [g["n"] for g in gaps]; r["gapCash"] = [g["cash"] for g in gaps]; r["gapChild"] = [g["child"] for g in gaps]
    r["gapPF"] = [g["pf"] for g in gaps]; r["gapHasQty"] = [g["hasQty"] for g in gaps]; r["gapFlag"] = [g["flag"] for g in gaps]
    r["gapLegs"] = [g["legs"] for g in gaps]; r["gapKeys"] = [g["keys"] for g in gaps]
    lpf = r.parentFactor.to_numpy(np.float64); gpf = r.gapPF.to_numpy(np.float64); hq = r.gapHasQty.to_numpy(bool)
    r["convPF"] = np.where(hq, gpf, lpf)
    r["ratioConflict"] = hq & ~r.ratioAssumed.to_numpy(bool) & (np.abs(gpf - lpf) > RATIO_CONFLICT_TOL * np.maximum(1.0, lpf))
    if "mirrorTail" in r and r.mirrorTail.to_numpy(bool).any():
        # mirror tail already in new-line units -> convPF 1 ... unless the lineage ratio is SOURCED (ciqsplit / co_acthist / rc2f_override, not assumed)
        # and != 1: then the copied tail is in OLD units and the conversion (reverse split at the relist) happens at the link — rc2f-E X5 hand-over
        # (General DataComm 0.20 -> 2.75 with 0.1: +37.5%, not +1275%). The new id's own link-day quantity leg equal to convPF is applied once
        # (returns_for_group_v2 dedup), so a sourced ratio never double-counts.
        sourced_ratio = ~r.ratioAssumed.to_numpy(bool) & (np.abs(lpf - 1.0) > 1e-9)
        old_units = r["_mirrorOldUnits"].to_numpy(bool) if "_mirrorOldUnits" in r else np.zeros(len(r), dtype=bool)
        mt = r.mirrorTail.to_numpy(bool) & ~hq & ~(sourced_ratio & old_units)
        r.loc[mt, "convPF"] = 1.0                       # (the old id's own quantity legs in the gap still apply)
    return r[cols].reset_index(drop=True)


# --------------------------------------------------------------------------------------
# v2 group computation
# --------------------------------------------------------------------------------------
def returns_for_group_v2(ids: np.ndarray, dates: np.ndarray, close: np.ndarray, ledger: Ledger,
                         links: pd.DataFrame | None = None, stale_after_day: int | None = None) -> tuple[pd.DataFrame, dict]:
    """One sorted (id, date) block with close notna -> (frame(date, tradingItemId, ret, prevDate, retFlag), stats).
    stats["link_results"]: per linked first row {newId: (ret, flag, suspect)}."""
    n = len(ids)
    ids = np.asarray(ids, dtype=np.int64)
    dates = np.asarray(dates).astype("datetime64[ns]")
    close = np.asarray(close, dtype=np.float64)
    st = {"events_in_range": 0, "events_applied": 0, "events_after_last_obs": 0, "events_before_first_obs": 0, "events_on_first_row": 0,
          "linked_rows": 0, "events_applied_via_link": 0, "events_dedup_link": 0, "events_dedup_link_conversion": 0, "links_ratio_conflict": 0, "links_suspect_return": 0, "links_verified": 0,
          "links_overlap_flagged": 0, "links_newid_legs_with_conversion": 0, "rows_stale_flagged": 0, "link_results": {}}
    if n == 0:
        return pd.DataFrame({"date": dates, "tradingItemId": ids, "ret": np.zeros(0), "prevDate": dates, "retFlag": np.zeros(0, np.int8)}), st
    same = np.r_[False, ids[1:] == ids[:-1]]
    close_prev = np.r_[np.nan, close[:-1]]
    close_prev[~same] = np.nan
    prev_day = np.r_[-1, _days(dates)[:-1]]
    prev_day[~same] = -1
    row_days = _days(dates)
    shares = np.ones(n); cash = np.zeros(n); child = np.zeros(n)
    flag = np.zeros(n, dtype=np.int8); has_ev = np.zeros(n, dtype=bool)
    linked = np.zeros(n, dtype=bool)
    link_keys: dict[int, set] = {}
    link_rows: dict[int, int] = {}
    link_cpf: dict[int, float] = {}
    link_verified: dict[int, bool] = {}
    # lineage-linked first rows: prev = old id's last close; old-id gap events pre-applied (rework F1); conversion convPF
    if links is not None and len(links):
        lk = links[np.isfinite(links.prevClose.to_numpy(np.float64))].set_index("newId")
        if "linkDay" in lk.columns:
            # link row = (newId, linkDay): the first row (linkMode first_row) or the first row after the old id's last trade (after_old_last)
            in_blk = lk[(lk.index >= int(ids[0])) & (lk.index <= int(ids[-1]))]
            rk = _keys(ids, row_days)
            jj_link = np.searchsorted(rk, _keys(in_blk.index.to_numpy(np.int64), in_blk.linkDay.to_numpy(np.int64)), side="left")
            okl = (jj_link < n)
            okl &= rk[np.minimum(jj_link, n - 1)] == _keys(in_blk.index.to_numpy(np.int64), in_blk.linkDay.to_numpy(np.int64))
            link_rows_idx = jj_link[okl]
            hit = np.zeros(len(link_rows_idx), dtype=bool) | True
        else:
            first_idx = np.flatnonzero(~same)
            hit = np.isin(ids[first_idx], lk.index.to_numpy())
            link_rows_idx = first_idx[hit]
        for i in link_rows_idx:
            row = lk.loc[ids[i]]
            close_prev[i] = float(row.prevClose); prev_day[i] = int(row.prevDay)
            cash[i] = float(row.gapCash); child[i] = float(row.gapChild); shares[i] = float(row.convPF)
            f = max(int(row.flag) if not (bool(row.gapHasQty) and bool(row.ratioAssumed)) else 0, int(row.gapFlag))
            if bool(row.ratioConflict):
                f = FLAG_UNRES; st["links_ratio_conflict"] += 1
            flag[i] = max(flag[i], f); has_ev[i] = True; linked[i] = True
            st["events_applied_via_link"] += int(row.gapN)
            link_keys[int(ids[i])] = set(row.gapKeys); link_rows[int(ids[i])] = int(i)
            link_cpf[int(ids[i])] = float(row.convPF)
            link_verified[int(ids[i])] = bool(getattr(row, "linkVerified", False))
        st["linked_rows"] = int(len(link_rows_idx))
    # events of this block
    sl = ledger.slice(int(ids[0]), int(ids[-1]))
    if sl.stop > sl.start:
        ev_ids = ledger.ids[sl]; ev_keys = ledger.keys[sl]
        row_keys = _keys(ids, row_days)
        j = np.searchsorted(row_keys, ev_keys, side="left")
        in_range = (j < n)
        jj = np.minimum(j, n - 1)
        in_range &= ids[jj] == ev_ids
        present = np.isin(ev_ids, ids)
        st["events_in_range"] = int(present.sum())
        st["events_after_last_obs"] = int((present & ~in_range).sum())
        # the target row must have a prev (same id) or be a linked row with exDate after the old id's prev day (a linked row after a
        # when-issued overlap ignores the new id's legs dated on/before the old id's last trade day: the holder still held the old share)
        can = in_range & ((same[jj] & ~linked[jj]) | (linked[jj] & (ledger.ex_days[sl] > prev_day[jj])))
        st["events_before_first_obs"] = int((in_range & ~can & (ledger.ex_days[sl] < row_days[jj])).sum())
        st["events_on_first_row"] = int((in_range & ~can & (ledger.ex_days[sl] >= row_days[jj])).sum())
        # new-id legs on a linked first row that duplicate an old-id gap leg (same exDate/leg/child/amount) apply once
        if link_keys:
            for k in np.flatnonzero(can & linked[jj]):
                if _leg_key(ledger, sl.start + int(k)) in link_keys.get(int(ev_ids[k]), ()):
                    can[k] = False; st["events_dedup_link"] += 1
                    continue
                # rc2f-E (X5 hand-over, WTW 317560689): a NEW-id quantity leg dated on the link day whose parentFactor equals the link conversion
                # (the lineage ratio) is the same reverse split the link already applies -> once, not twice (rc2 in-series row: leg once)
                cpf_k = float(link_cpf.get(int(ev_ids[k]), 1.0))
                if str(ledger.leg[sl.start + int(k)]) == "quantity" and ledger.ex_days[sl.start + int(k)] == row_days[jj[k]] and abs(cpf_k - 1.0) > 1e-9 \
                        and abs(float(ledger.pf[sl.start + int(k)]) - cpf_k) <= RATIO_CONFLICT_TOL * max(1.0, cpf_k):
                    can[k] = False; st["events_dedup_link_conversion"] += 1
        idx = np.flatnonzero(can)
        st["events_applied"] = int(len(idx))
        new_legs_on_link: dict[int, int] = {}
        if link_rows and len(idx):
            for k in idx[linked[jj[idx]]]:
                new_legs_on_link[int(ev_ids[k])] = new_legs_on_link.get(int(ev_ids[k]), 0) + 1
        if len(idx):
            jsel = jj[idx]
            # rank within the same target row, keeping ledger order (exDate, value legs before quantity legs)
            start = np.r_[True, jsel[1:] != jsel[:-1]]
            grp_start = np.maximum.accumulate(np.where(start, np.arange(len(jsel)), 0))
            rank = np.arange(len(jsel)) - grp_start
            cps = ledger.cps[sl][idx]; cval = ledger.cval[sl][idx]; pf = ledger.pf[sl][idx]; fl = ledger.flag[sl][idx]
            for r in range(int(rank.max()) + 1):
                m = rank == r
                rows = jsel[m]
                cash[rows] += shares[rows] * cps[m]
                child[rows] += shares[rows] * cval[m]
                shares[rows] *= pf[m]
                flag[rows] = np.maximum(flag[rows], fl[m])
            has_ev[jsel] = True
    ret = close / close_prev - 1.0                                      # bit-identical on no-event rows
    if has_ev.any():
        e = has_ev
        ret[e] = (shares[e] * close[e] + cash[e] + child[e]) / close_prev[e] - 1.0
    ret[~np.isfinite(close_prev)] = np.nan
    # link plausibility (rework F4): implausible first return -> 4, old id still quoted (overlap) -> >= 3
    if link_rows:
        ov = lk.overlap.to_dict(); cpf = lk.convPF.to_dict()
        for new_id, i in link_rows.items():
            suspect = ""
            if np.isfinite(ret[i]) and abs(ret[i]) > LINK_RET_SUSPECT and link_verified.get(new_id, False):
                suspect = "link_verified"; st["links_verified"] += 1            # rc2f-E: X5-verified genuine first-day move -> regular flag
            elif np.isfinite(ret[i]) and abs(ret[i]) > LINK_RET_SUSPECT:
                flag[i] = FLAG_UNRES; suspect = "link_implausible_return"; st["links_suspect_return"] += 1
            elif bool(ov.get(new_id, False)):
                flag[i] = max(flag[i], FLAG_EST); suspect = "overlap"; st["links_overlap_flagged"] += 1
            n_new = new_legs_on_link.get(new_id, 0) if sl.stop > sl.start else 0
            if n_new and float(cpf.get(new_id, 1.0)) != 1.0:
                # a leg recorded on the NEW id on its first day is applied per NEW share (after the conversion); a ledger
                # that wrote it per OLD share (MTCH-style) would be off by the ratio -> never a clean value: retFlag >= 3
                flag[i] = max(flag[i], FLAG_EST)
                suspect = (suspect + ";" if suspect else "") + "newid_leg_with_conversion"; st["links_newid_legs_with_conversion"] += 1
            st["link_results"][new_id] = (float(ret[i]), int(flag[i]), suspect, n_new)
    if stale_after_day is not None:
        m = row_days > stale_after_day
        flag[m] = FLAG_UNRES; st["rows_stale_flagged"] = int(m.sum())
    prev_date = np.where(prev_day >= 0, prev_day.astype("datetime64[D]").astype("datetime64[ns]"), NAT)
    df = pd.DataFrame({"date": dates, "tradingItemId": ids, "ret": ret, "prevDate": prev_date, "retFlag": flag})
    return df, st


# --------------------------------------------------------------------------------------
# rc1 legacy path (vendor-factor total return), kept verbatim for --legacy
# --------------------------------------------------------------------------------------
def adjusted_close(ids: np.ndarray, dates: np.ndarray, close: np.ndarray, split: FactorLookup, div: FactorLookup) -> np.ndarray:
    """adj = close / splitFactor x divAdjFactor in float64."""
    sf = split(ids, dates)
    df = div(ids, dates)
    return close.astype(np.float64) / sf * df


def returns_for_group(ids: np.ndarray, dates: np.ndarray, close: np.ndarray, split: FactorLookup,
                      div: FactorLookup) -> pd.DataFrame:
    """rc1: one sorted (id, date) block with close notna -> frame(date, tradingItemId, ret, prevDate)."""
    adj = adjusted_close(ids, dates, close, split, div)
    same = np.r_[False, ids[1:] == ids[:-1]]
    prev_adj = np.r_[np.nan, adj[:-1]]
    ret = np.where(same, adj / prev_adj - 1.0, np.nan)
    prev_date = np.r_[np.datetime64("NaT", "ns"), dates[:-1]]
    prev_date = np.where(same, prev_date, np.datetime64("NaT", "ns"))
    return pd.DataFrame({"date": dates, "tradingItemId": ids, "ret": ret, "prevDate": prev_date})


QF_PRICE_OVERRIDE_EST = 32               # base_data.qualityFlag bit written by rc2f_apply for ESTIMATED price corrections (R-Y2 P1 ①)
_EST_PX: dict = {}                       # per row group: boolean mask of estimated-price rows (filled by _iter_groups, read by build_returns_v2)


def _iter_groups(base: Path):
    """Yield (rg, ids, dates, close) per row group with close notna, checking the (id, date) order and id chunking.
    Rows whose qualityFlag == QF_PRICE_OVERRIDE_EST are recorded in _EST_PX[rg] (rc2f estimated price corrections)."""
    pf = qf_file = pq.ParquetFile(base)
    has_qf = "qualityFlag" in pf.schema_arrow.names
    last_id = -1
    _EST_PX.clear()
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=["date", "tradingItemId", "close"] + (["qualityFlag"] if has_qf else []))
        close = t["close"].to_numpy(zero_copy_only=False)
        keep = np.isfinite(close)
        ids = t["tradingItemId"].to_numpy()[keep]
        dates = t["date"].to_numpy().astype("datetime64[ns]")[keep]
        close = close[keep]
        if has_qf:
            est = t["qualityFlag"].to_numpy().astype(np.int64)[keep] == QF_PRICE_OVERRIDE_EST
            if est.any():
                _EST_PX[rg] = est
        if len(ids) == 0:
            yield rg, pf.num_row_groups, t.num_rows, ids, dates, close
            continue
        if ids[0] <= last_id:
            raise RuntimeError(f"row group {rg} starts with id {ids[0]} <= previous group's last id {last_id}")
        if not ((np.diff(ids) > 0) | ((np.diff(ids) == 0) & (np.diff(dates.astype("int64")) > 0))).all():
            raise RuntimeError(f"row group {rg} is not sorted by (tradingItemId, date)")
        last_id = int(ids[-1])
        yield rg, pf.num_row_groups, t.num_rows, ids, dates, close


def build_returns_legacy(stage: Path, out: Path | None = None) -> dict:
    """rc1 vendor-factor total return (adj = close/splitFactor x divAdjFactor), 4-column schema."""
    t0 = time.time()
    base = stage / "base_data.parquet"
    out = out or (stage / "derived" / "returns_total.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    sp = pd.read_parquet(stage / "factors" / "split.parquet")
    dv = pd.read_parquet(stage / "factors" / "dividend.parquet")
    for name, f in (("split", sp), ("dividend", dv)):
        chk = check_intervals(f)
        if not chk["ok"]:
            raise RuntimeError(f"{name} factor table fails the interval check: {chk['violations']}")
    split, div = FactorLookup(sp), FactorLookup(dv)
    tmp = out.with_name(out.name + ".tmp")
    writer = pq.ParquetWriter(tmp, RET_SCHEMA, compression="zstd", write_statistics=True, use_dictionary=False)
    n_in = n_out = n_first = 0
    for rg, n_rg, n_rows, ids, dates, close in _iter_groups(base):
        n_in += n_rows
        if len(ids) == 0:
            continue
        df = returns_for_group(ids, dates, close, split, div)
        n_first += int(df.ret.isna().sum())
        n_out += len(df)
        writer.write_table(pa.Table.from_pandas(df, schema=RET_SCHEMA, preserve_index=False), row_group_size=len(df))
        if (rg + 1) % 10 == 0:
            log.info("returns(legacy): %d/%d row groups, %d rows (rss %.1f GB)", rg + 1, n_rg, n_out, rss_gb())
    writer.close()
    os.replace(tmp, out)
    info = {"convention": "vendor-factor total return (rc1 legacy)", "rows_in": n_in, "rows_out": n_out, "first_rows_nan": n_first,
            "bytes": out.stat().st_size, "seconds": round(time.time() - t0, 1), "peak_rss_gb": round(rss_gb(), 2),
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    log.info("returns_total: %s", info)
    return info


# --------------------------------------------------------------------------------------
# v2 build
# --------------------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(32 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build_returns_v2(stage: Path, ledger_path: Path, out: Path | None = None, lineage_path: Path | None = None,
                     stale_after: str | pd.Timestamp | None = None, links_out: Path | None = None,
                     stitched_copies: Path | bool | None = None, lifecycle_path: Path | None = None, legacy_links: bool = False) -> dict:
    t0 = time.time()
    stage = Path(stage)
    base = stage / "base_data.parquet"
    out = out or (stage / "derived" / "returns_total.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        raise FileNotFoundError(f"corporate-action ledger not found: {ledger_path}")
    ldf, stitched = load_ledger_info(ledger_path, stitched_copies)
    lifecycle_df = None
    if lifecycle_path is not None and not legacy_links:
        lifecycle_path = Path(lifecycle_path)
        if not lifecycle_path.exists():
            raise FileNotFoundError(f"lifecycle file not found: {lifecycle_path}")
        lifecycle_df = pd.read_parquet(lifecycle_path, columns=["tradingItemId", "lastTradeDate"])
    ledger = Ledger(ldf, lifecycle=lifecycle_df, legacy_rules=legacy_links)
    if lineage_path is not None:
        lineage_path = Path(lineage_path)
        if not lineage_path.exists():
            raise FileNotFoundError(f"lineage file not found: {lineage_path}")
        ledger.use_lineage(pd.read_parquet(lineage_path), legacy=legacy_links)
    links = resolve_links(ledger, base, lifecycle=lifecycle_df, legacy=legacy_links)
    stale_day = None if stale_after is None else int(_days(np.array([pd.Timestamp(stale_after)], dtype="datetime64[ns]"))[0])
    tmp = out.with_name(out.name + ".tmp")
    writer = pq.ParquetWriter(tmp, RET_SCHEMA_V2, compression="zstd", write_statistics=True, use_dictionary=False)
    n_in = n_out = n_first = 0
    tot = {"events_in_range": 0, "events_applied": 0, "events_after_last_obs": 0, "events_before_first_obs": 0, "events_on_first_row": 0,
           "linked_rows": 0, "events_applied_via_link": 0, "events_dedup_link": 0, "events_dedup_link_conversion": 0, "links_ratio_conflict": 0, "links_suspect_return": 0, "links_verified": 0,
           "links_overlap_flagged": 0, "links_newid_legs_with_conversion": 0, "rows_stale_flagged": 0}
    link_results = {}
    flags = np.zeros(5, dtype=np.int64)
    seen_ids = 0
    n_est_px_rows = 0
    for rg, n_rg, n_rows, ids, dates, close in _iter_groups(base):
        n_in += n_rows
        if len(ids) == 0:
            continue
        df, st = returns_for_group_v2(ids, dates, close, ledger, links, stale_day)
        if rg in _EST_PX:
            # R-Y2 P1 ①: a return computed from an ESTIMATED price (this row's close or the previous row's close of the same id) is an estimate:
            # retFlag >= 3 (never lowers 4). A link row whose prev is the old id's close is not touched by the new id's flag.
            est = _EST_PX[rg]
            prev_est = np.r_[False, est[:-1]] & (np.r_[True, ids[1:] == ids[:-1]] & np.r_[False, np.ones(len(ids) - 1, dtype=bool)])
            m = (est | prev_est) & df.ret.notna().to_numpy()
            if m.any():
                fl = df.retFlag.to_numpy().copy(); fl[m] = np.maximum(fl[m], FLAG_EST); df["retFlag"] = fl
                n_est_px_rows += int(m.sum())
        for k in tot:
            tot[k] += st[k]
        link_results.update(st["link_results"])
        n_first += int(df.ret.isna().sum())
        n_out += len(df)
        flags += np.bincount(df.retFlag.to_numpy(), minlength=5)[:5]
        seen_ids += int((np.r_[True, ids[1:] != ids[:-1]]).sum())
        writer.write_table(pa.Table.from_pandas(df, schema=RET_SCHEMA_V2, preserve_index=False), row_group_size=len(df))
        if (rg + 1) % 10 == 0:
            log.info("returns(v2): %d/%d row groups, %d rows (rss %.1f GB)", rg + 1, n_rg, n_out, rss_gb())
    writer.close()
    os.replace(tmp, out)
    if links_out is not None and len(links):
        lt = links.drop(columns=["gapKeys"]).copy()
        lt["retFirst"] = lt.newId.map(lambda i: link_results.get(int(i), (np.nan, -1, "", 0))[0])
        lt["flagFirst"] = lt.newId.map(lambda i: link_results.get(int(i), (np.nan, -1, "", 0))[1])
        lt["suspect"] = lt.newId.map(lambda i: link_results.get(int(i), (np.nan, -1, "", 0))[2])
        lt["newLegsApplied"] = lt.newId.map(lambda i: link_results.get(int(i), (np.nan, -1, "", 0))[3])
        lt["state"] = np.where(np.isfinite(lt.prevClose.to_numpy(np.float64)), "linked", np.where(lt.linkMode == "old_outlives_new", "unresolvable_old_outlives_new", "unresolvable_no_old_close_before_first"))
        lt["newFirstDate"] = lt.newFirstDay.astype("datetime64[D]"); lt["prevDate"] = np.where(lt.prevDay >= 0, lt.prevDay.astype("datetime64[D]"), np.datetime64("NaT"))
        lt["linkDate"] = np.where(lt.linkDay >= 0, lt.linkDay.astype("datetime64[D]"), np.datetime64("NaT"))
        ex = ledger.links_excluded.copy()
        if len(ex):
            ex["state"] = ex.reason
        Path(links_out).parent.mkdir(parents=True, exist_ok=True)
        pd.concat([lt.drop(columns=["newFirstDay", "prevDay", "linkDay"]), ex], ignore_index=True).to_csv(links_out, index=False)
    n_ledger_ids_absent = int(len(np.setdiff1d(np.unique(ledger.ids), _base_ids(base)))) if ledger.n else 0
    linked_ok = np.isfinite(links.prevClose.to_numpy(np.float64)) if len(links) else np.zeros(0, bool)
    info = {"convention": "cash-style holding total return v2 (rc2 section 3-2)", "ledger": str(ledger_path), "ledger_sha256": _sha256(ledger_path),
            "ledger_rows": int(len(ldf)) - int(stitched.get("mirrored", 0)), "stitched_copies": stitched, "ledger_rows_applied_types": int(ledger.n), "ledger_rows_excluded_source_corporate_actions": int(ledger.n_excluded_source),
            "lineage": str(lineage_path) if lineage_path is not None else None, "lineage_sha256": _sha256(lineage_path) if lineage_path is not None else None,
            "link_source": "lineage" if lineage_path is not None else "ledger rename/relist rows",
            "link_rule": "legacy(rc2: first row, chainable ignored)" if legacy_links else "rc2f(chainable only; when-issued overlap -> first new-id row after the old id's lifecycle-capped last trade)",
            "lifecycle": str(lifecycle_path) if lifecycle_df is not None else None,
            "links_not_chainable_excluded": int((ledger.links_excluded.reason == "not_chainable").sum()) if len(ledger.links_excluded) else 0,
            "links_after_old_last": int((links.linkMode == "after_old_last").sum()) if len(links) else 0,
            "links_old_outlives_new": int((links.linkMode == "old_outlives_new").sum()) if len(links) else 0,
            "links_after_old_last_mirror": int((links.linkMode == "after_old_last_mirror").sum()) if len(links) else 0,
            "settlement_cash_rows_applied": int(ledger.n_settlement_cash_applied),
            "ledger_link_rows": int(len(ledger.links)), "links_chain_gap_excluded": int(len(ledger.links_excluded)), "links_multi_old_dropped": int(ledger.n_links_multi_old),
            "links_resolved": int(linked_ok.sum()), "links_unresolved_no_prev_close": int((~linked_ok).sum()),
            "links_with_overlap": int((links.overlap.to_numpy(bool) & linked_ok).sum()) if len(links) else 0,
            "links_with_old_gap_events": int((links.gapN.to_numpy() > 0)[linked_ok].sum()) if len(links) else 0,
            "stale_after": None if stale_after is None else str(pd.Timestamp(stale_after).date()),
            "rows_on_estimated_prices_flagged": int(n_est_px_rows),
            "rows_in": n_in, "rows_out": n_out, "ids": seen_ids, "first_rows_nan": n_first, "events": tot,
            "ledger_ids_not_in_base_data": n_ledger_ids_absent,
            "retFlag": {FLAG_NAMES[i]: int(flags[i]) for i in range(5)},
            "bytes": out.stat().st_size, "seconds": round(time.time() - t0, 1), "peak_rss_gb": round(rss_gb(), 2),
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    log.info("returns_total v2: %s", info)
    return info


def _base_ids(base: Path) -> np.ndarray:
    pf = pq.ParquetFile(base)
    out = []
    for rg in range(pf.num_row_groups):
        out.append(np.unique(pf.read_row_group(rg, columns=["tradingItemId"])["tradingItemId"].to_numpy()))
    return np.unique(np.concatenate(out)) if out else np.zeros(0, np.int64)


def build_returns(stage: Path, out: Path | None = None, ledger: Path | None = None, legacy: bool = False, lineage: Path | None = None,
                  stale_after=None, links_out: Path | None = None, stitched_copies: Path | bool | None = None, lifecycle: Path | None = None,
                  legacy_links: bool = False) -> dict:
    """Entry point used by publish.py / update_daily.py. v2 needs the ledger (default <stage>/events/ca_ledger.parquet)
    and takes the lineage file for first-row links (default <stage>/master/lineage.parquet when present, else the
    ledger's rename/relist rows); legacy=True runs the rc1 vendor-factor computation instead (never a silent fallback)."""
    stage = Path(stage)
    if legacy:
        return build_returns_legacy(stage, out)
    ledger = Path(ledger) if ledger is not None else stage / "events" / "ca_ledger.parquet"
    if not ledger.exists():
        raise FileNotFoundError(f"returns v2 needs the corporate-action ledger (rc2 section 3-1); not found: {ledger}. "
                                f"Pass ledger=<path> or legacy=True for the rc1 vendor-factor computation.")
    if lineage is None and (stage / "master" / "lineage.parquet").exists():
        lineage = stage / "master" / "lineage.parquet"
    if lifecycle is None and (stage / "master" / "lifecycle.parquet").exists():
        lifecycle = stage / "master" / "lifecycle.parquet"
    return build_returns_v2(stage, ledger, out, lineage_path=lineage, stale_after=stale_after, links_out=links_out, stitched_copies=stitched_copies,
                            lifecycle_path=lifecycle, legacy_links=legacy_links)


# --------------------------------------------------------------------------------------
# self-test: synthetic fixture (v2 + legacy)
# --------------------------------------------------------------------------------------
def _ledger_frame(rows: list[dict]) -> pd.DataFrame:
    base = {"eventId": None, "tradingItemId": 0, "exDate": pd.NaT, "eventType": "cash_dividend", "legType": "cash", "cashPerShare": np.nan,
            "cashCurrency": "USD", "fxToUSD": 1.0, "parentFactor": 1.0, "childTradingItemId": pd.NA, "childRatio": np.nan,
            "childValueSource": "unknown", "childValuePerOldShare": np.nan, "subscriptionPrice": np.nan, "expiryDate": pd.NaT,
            "exerciseRatio": np.nan, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT, "source": "test",
            "evidence": "", "status": "ok", "isEstimate": False, "repairOf": None, "notes": ""}
    df = pd.DataFrame([{**base, **r} for r in rows] if rows else [], columns=list(base))
    df["exDate"] = pd.to_datetime(df.exDate)
    df["childTradingItemId"] = df.childTradingItemId.astype("Int64")
    return df


def self_test() -> list[dict]:
    from prices_factors import SRC_DIV, SRC_SPLIT, build_intervals, dividend_windows, split_windows

    res = []
    T = pd.Timestamp

    def rec(name, ok, got, expected=None):
        res.append({"case": name, "ok": bool(ok), "got": got, "expected": expected})

    # fixture: raw closes 100, 102, (missing 01-07), 104 | 2:1 split 01-08 raw 52 | 53 | dividend 1.0 ex 01-10 raw 52.5
    dates = pd.to_datetime(["2020-01-03", "2020-01-06", "2020-01-08", "2020-01-09", "2020-01-10"]).to_numpy().astype("datetime64[ns]")
    raw = np.array([100.0, 102.0, 52.0, 53.0, 52.5], dtype=np.float32)
    ids = np.ones(5, dtype=np.int64)
    led = Ledger(_ledger_frame([
        {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "split", "legType": "quantity", "parentFactor": 2.0},
        {"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0},
    ]))
    df, st = returns_for_group_v2(ids, dates, raw, led)
    got = df.ret.to_numpy(); c = raw.astype(np.float64)
    rec("split day: 2 x 52 / 102 - 1 (+1.96%), retFlag 0", abs(got[2] - (2 * c[2] / c[1] - 1)) < 1e-15 and df.retFlag[2] == 0, float(got[2]), 2 * c[2] / c[1] - 1)
    rec("dividend day cash-style (52.5 + 1)/53 - 1 exactly, retFlag 1", got[4] == (c[4] + 1.0) / c[3] - 1.0 and df.retFlag[4] == 1, float(got[4]), (c[4] + 1.0) / c[3] - 1.0)
    rec("no-event rows bit-identical to close ratio", np.array_equal(got[[1, 3]], c[[1, 3]] / c[[0, 2]] - 1.0), got[[1, 3]].tolist())
    rec("first row NaN/NaT, prevDate of 01-08 is 01-06 (missing day skipped)", np.isnan(got[0]) and pd.isna(df.prevDate[0]) and df.prevDate[2] == T("2020-01-06"), str(df.prevDate[2]))
    rec("stats: 2 events applied", st["events_applied"] == 2 and st["events_after_last_obs"] == 0, st)
    # same-day dividend + split: cash per OLD share, then quantity
    led2 = Ledger(_ledger_frame([
        {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "split", "legType": "quantity", "parentFactor": 2.0},
        {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 3.0},
    ]))
    df2, _ = returns_for_group_v2(ids, dates, raw, led2)
    rec("dividend + split same day: (2 x 52 + 3)/102 - 1 (cash not scaled by the split)", abs(df2.ret[2] - ((2 * c[2] + 3.0) / c[1] - 1)) < 1e-15 and df2.retFlag[2] == 1, float(df2.ret[2]), (2 * c[2] + 3.0) / c[1] - 1)
    # spin-off + unresolved leg + estimate flag precedence
    led3 = Ledger(_ledger_frame([
        {"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 0.5, "childValuePerOldShare": 10.0, "childValueSource": "child_close_ex"},
        {"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 8, "childRatio": 1.0, "childValuePerOldShare": np.nan, "status": "unresolved"},
        {"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.5, "isEstimate": True, "status": "estimated"},
    ]))
    df3, _ = returns_for_group_v2(ids, dates, raw, led3)
    rec("spin-off: (53 + 10)/52 - 1, retFlag 2", abs(df3.ret[3] - ((c[3] + 10.0) / c[2] - 1)) < 1e-15 and df3.retFlag[3] == 2, float(df3.ret[3]))
    rec("unresolved stock leg + estimated cash: value = price + 0.5 only, retFlag 4 (precedence over 3)", abs(df3.ret[4] - ((c[4] + 0.5) / c[3] - 1)) < 1e-15 and df3.retFlag[4] == 4, (float(df3.ret[4]), int(df3.retFlag[4])))
    # gap spanning two events: rows 01-06 and 01-09 only (01-08 missing): split 01-08 then dividend 01-09 (per new share)
    ids_g = np.array([1, 1, 1], dtype=np.int64)
    dates_g = pd.to_datetime(["2020-01-06", "2020-01-09", "2020-01-10"]).to_numpy().astype("datetime64[ns]")
    raw_g = np.array([102.0, 53.0, 52.5], dtype=np.float32)
    led4 = Ledger(_ledger_frame([
        {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "split", "legType": "quantity", "parentFactor": 2.0},
        {"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0},
    ]))
    df4, st4 = returns_for_group_v2(ids_g, dates_g, raw_g, led4)
    rec("gap spanning split (01-08) then dividend (01-09): (2 x 53 + 2 x 1)/102 - 1", abs(df4.ret[1] - ((2 * 53.0 + 2.0) / 102.0 - 1)) < 1e-15 and df4.retFlag[1] == 1 and df4.prevDate[1] == T("2020-01-06"), float(df4.ret[1]), (2 * 53.0 + 2.0) / 102.0 - 1)
    # event after the last observation is dropped and counted; event on the first row without link is counted
    led5 = Ledger(_ledger_frame([
        {"tradingItemId": 1, "exDate": "2020-01-03", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0},
        {"tradingItemId": 1, "exDate": "2020-02-01", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0},
        {"tradingItemId": 2, "exDate": "2020-01-06", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0},
    ]))
    df5, st5 = returns_for_group_v2(ids, dates, raw, led5)
    rec("event on first row (no link) and after last obs are dropped + counted; other ids untouched", st5["events_applied"] == 0 and st5["events_on_first_row"] == 1 and st5["events_before_first_obs"] == 0 and st5["events_after_last_obs"] == 1 and st5["events_in_range"] == 2 and (df5.retFlag == 0).all(), st5)
    # lineage-linked first row: new id 2 starts 01-09 (close 26.0), old id 1 last close 52 (01-08), lineage ratio 0.5
    import tempfile
    ids_l = np.array([1, 1, 1, 2, 2], dtype=np.int64)
    dates_l = pd.to_datetime(["2020-01-03", "2020-01-06", "2020-01-08", "2020-01-09", "2020-01-10"]).to_numpy().astype("datetime64[ns]")
    raw_l = np.array([100.0, 102.0, 52.0, 104.0, 105.0], dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmp:
        bp = Path(tmp) / "base_data.parquet"
        pd.DataFrame({"tradingItemId": ids_l, "date": dates_l, "close": raw_l}).to_parquet(bp, index=False)
        lin = pd.DataFrame({"oldId": [1], "newId": [2], "relation": ["rename"], "effectiveDate": [T("2020-01-09")], "ratio": [0.5], "evidence": ["t"], "confidence": ["high"], "chainGapTradingDays": [0]})
        led6 = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "rename", "legType": "quantity", "parentFactor": 1.0, "childTradingItemId": 2}]))
        led6.use_lineage(lin)
        links = resolve_links(led6, bp)
        df6, st6 = returns_for_group_v2(ids_l, dates_l, raw_l, led6, links)
        rec("linked first row (lineage ratio 0.5): 0.5 x 104 / 52 - 1 with prevDate 01-08, retFlag 0; ledger rename row unused", abs(df6.ret[3] - (0.5 * 104.0 / 52.0 - 1)) < 1e-15 and df6.prevDate[3] == T("2020-01-08") and df6.retFlag[3] == 0 and st6["linked_rows"] == 1 and st6["events_applied"] == 0 and np.isnan(df6.ret[0]),
            (float(df6.ret[3]), str(df6.prevDate[3])), 0.5 * 104.0 / 52.0 - 1)
        # rework F1: old id's events in the link gap (dividend 1.0 ex 01-09 on old id 1, split 0.5 on old id 1) are applied first
        led7 = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "special_dividend", "legType": "cash", "cashPerShare": 1.0},
                                     {"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "split", "legType": "quantity", "parentFactor": 0.5}]))
        led7.use_lineage(lin)
        links7 = resolve_links(led7, bp)
        df7, st7 = returns_for_group_v2(ids_l, dates_l, raw_l, led7, links7)
        rec("F1: old-id gap dividend 1.0 (old share) + old split 0.5 == lineage ratio -> (0.5 x 104 + 1)/52 - 1, flag 1, no conflict", abs(df7.ret[3] - ((0.5 * 104.0 + 1.0) / 52.0 - 1)) < 1e-15 and df7.retFlag[3] == 1 and st7["events_applied_via_link"] == 2 and st7["links_ratio_conflict"] == 0,
            (float(df7.ret[3]), int(df7.retFlag[3]), st7["events_applied_via_link"]), (0.5 * 104.0 + 1.0) / 52.0 - 1)
        # F1 conflict: lineage says 1.0 but the old id's split says 0.5 -> ledger product used, flag 4
        lin_c = lin.assign(ratio=[1.0])
        led8 = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "split", "legType": "quantity", "parentFactor": 0.5}]))
        led8.use_lineage(lin_c)
        df8, st8 = returns_for_group_v2(ids_l, dates_l, raw_l, led8, resolve_links(led8, bp))
        rec("F1: lineage ratio 1.0 vs old split 0.5 -> 0.5 used, retFlag 4 (ratio_conflict)", abs(df8.ret[3] - (0.5 * 104.0 / 52.0 - 1)) < 1e-15 and df8.retFlag[3] == 4 and st8["links_ratio_conflict"] == 1, (float(df8.ret[3]), int(df8.retFlag[3])))
        # F1 dedup: the same spin leg recorded on both ids applies once; new-id-only leg applies on the converted shares
        led9 = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 1.0, "childValuePerOldShare": 10.0, "childValueSource": "child_close_ex"},
                                     {"tradingItemId": 2, "exDate": "2020-01-09", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 1.0, "childValuePerOldShare": 10.0, "childValueSource": "child_close_ex"},
                                     {"tradingItemId": 2, "exDate": "2020-01-09", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 2.0}]))
        led9.use_lineage(lin)
        df9, st9 = returns_for_group_v2(ids_l, dates_l, raw_l, led9, resolve_links(led9, bp))
        rec("F1: duplicated spin leg applied once + new-id dividend on converted shares: (0.5 x 104 + 10 + 0.5 x 2)/52 - 1, flag 3 (newid_leg_with_conversion)", abs(df9.ret[3] - ((0.5 * 104.0 + 10.0 + 0.5 * 2.0) / 52.0 - 1)) < 1e-15 and df9.retFlag[3] == 3 and st9["events_dedup_link"] == 1 and st9["links_newid_legs_with_conversion"] == 1,
            (float(df9.ret[3]), int(df9.retFlag[3]), st9["events_dedup_link"]))
        # F4: implausible linked first return -> flag 4 ; chain gap > 5 -> not chained (NaN)
        raw_big = np.array([100.0, 102.0, 0.5, 104.0, 105.0], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_l, "date": dates_l, "close": raw_big}).to_parquet(bp, index=False)
        led10 = Ledger(_ledger_frame([])); led10.use_lineage(lin)
        df10, st10 = returns_for_group_v2(ids_l, dates_l, raw_big, led10, resolve_links(led10, bp))
        rec("F4: 0.5 x 104 / 0.5 - 1 = +10300% -> retFlag 4 (link_implausible_return)", df10.retFlag[3] == 4 and st10["links_suspect_return"] == 1 and abs(df10.ret[3] - 103.0) < 1e-12, (float(df10.ret[3]), int(df10.retFlag[3])))
        led11 = Ledger(_ledger_frame([])); led11.use_lineage(lin.assign(chainGapTradingDays=[6]))
        df11, st11 = returns_for_group_v2(ids_l, dates_l, raw_big, led11, resolve_links(led11, bp))
        rec("chainGapTradingDays 6 > 5 -> link excluded, first row NaN", np.isnan(df11.ret[3]) and len(led11.links_excluded) == 1 and st11["linked_rows"] == 0, (float(df11.ret[3]), len(led11.links_excluded)))
        # rc2f (X5 NEW-X5-1): chainable == False -> not linked (first row NaN), reason not_chainable; legacy flag ignores the column
        led13 = Ledger(_ledger_frame([])); led13.use_lineage(lin.assign(chainable=[False]))
        df13, st13 = returns_for_group_v2(ids_l, dates_l, raw_big, led13, resolve_links(led13, bp))
        led13l = Ledger(_ledger_frame([])); led13l.use_lineage(lin.assign(chainable=[False]), legacy=True)
        df13l, _ = returns_for_group_v2(ids_l, dates_l, raw_big, led13l, resolve_links(led13l, bp, legacy=True))
        rec("rc2f: chainable False -> not linked (NaN, reason not_chainable); --legacy-links still links", np.isnan(df13.ret[3]) and list(led13.links_excluded.reason) == ["not_chainable"] and st13["linked_rows"] == 0 and np.isfinite(df13l.ret[3]),
            (float(df13.ret[3]), list(led13.links_excluded.reason), float(df13l.ret[3])))
        # rc2f: ratioSource rc2f_estimate -> estimate flag 3 on the link row (ratio known); rc2f_override -> flag 0
        pd.DataFrame({"tradingItemId": ids_l, "date": dates_l, "close": raw_l}).to_parquet(bp, index=False)
        led14 = Ledger(_ledger_frame([])); led14.use_lineage(lin.assign(chainable=[True], ratioSource=["rc2f_estimate:X5-1"]))
        df14, _ = returns_for_group_v2(ids_l, dates_l, raw_l, led14, resolve_links(led14, bp))
        rec("rc2f: ratioSource rc2f_estimate -> link row retFlag 3 (estimate)", df14.retFlag[3] == 3 and np.isfinite(df14.ret[3]), (float(df14.ret[3]), int(df14.retFlag[3])))
        # rc2f (X5 NEW-X5-5): when-issued overlap — old id 1 trades 01-03..01-09 (last close 52 on 01-09), new id 2 prints from 01-08 (WI);
        # link row = first new-id row after 01-09 = 01-10: prev = 52, old dividend 1.0 ex 01-10 applied, ratio 0.5 -> (0.5 x 105 + 1)/52 - 1;
        # 01-08 (first row) NaN, 01-09 = own price return; legacy: linked at 01-08 with the overlap flag 3
        ids_w = np.array([1, 1, 1, 2, 2, 2], dtype=np.int64)
        dates_w = pd.to_datetime(["2020-01-03", "2020-01-06", "2020-01-09", "2020-01-08", "2020-01-09", "2020-01-10"]).to_numpy().astype("datetime64[ns]")
        raw_w = np.array([100.0, 102.0, 52.0, 104.0, 104.5, 105.0], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_w}).to_parquet(bp, index=False)
        lin_w = lin.assign(effectiveDate=[T("2020-01-10")], chainable=[True])
        led15 = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0}]))
        led15.use_lineage(lin_w)
        links15 = resolve_links(led15, bp)
        df15, st15 = returns_for_group_v2(ids_w, dates_w, raw_w, led15, links15)
        exp15 = (0.5 * 105.0 + 1.0) / 52.0 - 1
        rec("rc2f WI overlap: link at the first new-id row after the old id's last trade (01-10), prev 52, old gap dividend applied; 01-08 NaN, 01-09 own return",
            links15.linkMode.iloc[0] == "after_old_last" and abs(df15.ret[5] - exp15) < 1e-15 and df15.prevDate[5] == T("2020-01-09") and df15.retFlag[5] == 1
            and np.isnan(df15.ret[3]) and abs(df15.ret[4] - (104.5 / 104.0 - 1)) < 1e-15 and df15.retFlag[4] == 0 and st15["linked_rows"] == 1 and st15["events_applied_via_link"] == 1,
            (links15.linkMode.iloc[0], float(df15.ret[5]), str(df15.prevDate[5]), float(df15.ret[4])), exp15)
        led15l = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0}]))
        led15l.use_lineage(lin_w, legacy=True)
        df15l, st15l = returns_for_group_v2(ids_w, dates_w, raw_w, led15l, resolve_links(led15l, bp, legacy=True))
        rec("legacy links: WI pair linked at the new id's first row (01-08, prev 01-06) with overlap flag 3; 01-10 plain", df15l.prevDate[3] == T("2020-01-06") and df15l.retFlag[3] == 3 and df15l.retFlag[5] == 0 and abs(df15l.ret[5] - (105.0 / 104.5 - 1)) < 1e-15,
            (str(df15l.prevDate[3]), int(df15l.retFlag[3]), float(df15l.ret[5])))
        # rc2f (R-X0 P2-9): mirror tail — old id's last close 104.5 == new id's 01-09 close -> convPF 1 (ratio 0.5 ignored), linkMode after_old_last_mirror
        raw_m = np.array([100.0, 102.0, 104.5, 104.0, 104.5, 105.0], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_m}).to_parquet(bp, index=False)
        led18 = Ledger(_ledger_frame([])); led18.use_lineage(lin_w)
        links18 = resolve_links(led18, bp)
        df18, _ = returns_for_group_v2(ids_w, dates_w, raw_m, led18, links18)
        rec("rc2f mirror tail: old last close == new same-day close -> linkMode after_old_last_mirror, convPF 1, link ret 105/104.5 - 1",
            links18.linkMode.iloc[0] == "after_old_last_mirror" and links18.convPF.iloc[0] == 1.0 and abs(df18.ret[5] - (105.0 / 104.5 - 1)) < 1e-15,
            (links18.linkMode.iloc[0], float(links18.convPF.iloc[0]), float(df18.ret[5])), 105.0 / 104.5 - 1)
        # rc2f-E (X5 hand-over, 7 cases): mirror tail in OLD units — the new series steps by ~1/ratio at the link day (reverse split at the relist):
        # convPF = the sourced ratio, not 1. General DataComm 0.20 -> 2.75 with 0.1: +37.5%; Atlantic Coast 1.87 -> 10.05 with 0.196002: +5.3%;
        # Dickie Walker 0.35 -> 2.18 with 0.15625: -2.7%; MFC 1.75 -> 8.875 with 0.2: +1.4%; Aptorum 0.70 -> 8.83 with 0.1: +26%
        for nm, ratio, c_old, c_new, exp in (("GDC", 0.1, 0.20, 2.75, 0.375), ("ACFC", 0.196002, 1.87, 10.05, 0.053), ("DWM", 0.15625, 0.35, 2.18, -0.027),
                                             ("MFC", 0.2, 1.75, 8.875, 0.014), ("APM", 0.1, 0.70, 8.83, 0.26)):
            raw_o = np.array([c_old * 1.02, c_old * 1.01, c_old, c_old * 1.01, c_old, c_new], dtype=np.float32)      # old: 3 prints; new: mirror of old (01-08, 01-09) then the relist print
            pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_o}).to_parquet(bp, index=False)
            led_o = Ledger(_ledger_frame([])); led_o.use_lineage(lin_w.assign(ratio=[ratio], ratioSource=["ciqsplit"]))
            links_o = resolve_links(led_o, bp)
            df_o, _ = returns_for_group_v2(ids_w, dates_w, raw_o, led_o, links_o)
            got_o = float(df_o.ret[5])
            rec(f"rc2f-E mirror tail in old units ({nm}): convPF = sourced ratio {ratio} -> link ret {exp:+.1%} (X5 expected)",
                links_o.linkMode.iloc[0] == "after_old_last_mirror" and abs(float(links_o.convPF.iloc[0]) - ratio) < 1e-12 and abs(got_o - (ratio * c_new / c_old - 1)) < 1e-6 and abs(got_o - exp) < 0.0055,
                (links_o.linkMode.iloc[0], float(links_o.convPF.iloc[0]), round(got_o, 4)), exp)
        # rc2f-E (WTW 317560689): the new id's own link-day quantity leg equal to the conversion is applied once (rc2 in-series row -2.2%): old
        # 2666305 last 2016-01-04 (mirror), new steps 1/0.377501 at the link with a ledger split row on the NEW id at the link day -> not twice
        raw_ww = np.array([47.0, 46.5, 46.0, 46.5, 46.0, 46.0 / 0.377501 * 0.978], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_ww}).to_parquet(bp, index=False)
        led_ww = Ledger(_ledger_frame([{"tradingItemId": 2, "exDate": "2020-01-10", "eventType": "split", "legType": "quantity", "parentFactor": 0.377501}]))
        led_ww.use_lineage(lin_w.assign(ratio=[0.377501], ratioSource=["ciqsplit"]))
        links_ww = resolve_links(led_ww, bp)
        df_ww, st_ww = returns_for_group_v2(ids_w, dates_w, raw_ww, led_ww, links_ww)
        exp_ww = 0.377501 * float(raw_ww[5]) / float(raw_ww[2]) - 1.0
        rec("rc2f-E WTW: new-id link-day split leg == convPF -> applied once (link ret = ratio x close / old last - 1 ~ -2.2%, not -62%)",
            abs(float(df_ww.ret[5]) - exp_ww) < 1e-6 and st_ww["events_dedup_link_conversion"] == 1 and df_ww.retFlag[5] < 4,
            (round(float(df_ww.ret[5]), 4), st_ww["events_dedup_link_conversion"], int(df_ww.retFlag[5])), round(exp_ww, 4))
        # rc2f-E linkVerified: |ret| > 0.5 on a verified link keeps the regular flag (0) and counts links_verified; unverified -> 4
        raw_v = np.array([10.0, 10.2, 10.1, 10.2, 10.1, 16.0], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_v}).to_parquet(bp, index=False)
        for lv, exp_flag in ((True, 0), (False, 4)):
            led_v = Ledger(_ledger_frame([])); led_v.use_lineage(lin_w.assign(ratio=[1.0], ratioSource=["rc2f_override:X5-9"], linkVerified=[lv]))
            links_v = resolve_links(led_v, bp)
            df_v, st_v = returns_for_group_v2(ids_w, dates_w, raw_v, led_v, links_v)
            rec(f"rc2f-E linkVerified={lv}: +58.4% first-day move -> flag {exp_flag}, links_verified {int(lv)}",
                int(df_v.retFlag[5]) == exp_flag and st_v["links_verified"] == int(lv) and st_v["links_suspect_return"] == int(not lv) and abs(float(df_v.ret[5]) - (16.0 / 10.1 - 1)) < 1e-6,
                (int(df_v.retFlag[5]), st_v["links_verified"], st_v["links_suspect_return"]), exp_flag)
        # rc2f-E linkVerified on the after_old_last path (WI overlap: old prints through 01-09, new from 01-08 -> link row 01-10): old last 52 (units
        # 1:2 ratio 0.5), new 01-10 close 80 -> 0.5 x 80 / 52 - 1 = -23% (fine) ; with new close 200 -> +92% -> flag 4 unless verified
        raw_ao = np.array([100.0, 102.0, 52.0, 104.0, 104.5, 200.0], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_ao}).to_parquet(bp, index=False)
        for lv, exp_flag in ((True, 0), (False, 4)):
            led_ao = Ledger(_ledger_frame([])); led_ao.use_lineage(lin_w.assign(ratio=[0.5], ratioSource=["rc2f_override:X5-8"], linkVerified=[lv]))
            links_ao = resolve_links(led_ao, bp)
            df_ao, st_ao = returns_for_group_v2(ids_w, dates_w, raw_ao, led_ao, links_ao)
            rec(f"rc2f-E linkVerified={lv} on an after_old_last link: 0.5 x 200 / 52 - 1 = +92% -> flag {exp_flag}",
                links_ao.linkMode.iloc[0] == "after_old_last" and int(df_ao.retFlag[5]) == exp_flag and st_ao["links_verified"] == int(lv) and abs(float(df_ao.ret[5]) - (0.5 * 200.0 / 52.0 - 1)) < 1e-6,
                (links_ao.linkMode.iloc[0], int(df_ao.retFlag[5]), st_ao["links_verified"], round(float(df_ao.ret[5]), 4)), exp_flag)
        pd.DataFrame({"tradingItemId": ids_w, "date": dates_w, "close": raw_w}).to_parquet(bp, index=False)
        # rc2f: lifecycle caps the old id's last trade day: lastTradeDate 01-06 -> old prints after it ignored -> link at 01-08 (first new row after 01-06), prev = 102 (01-06)
        lc_w = pd.DataFrame({"tradingItemId": [1, 2], "lastTradeDate": [T("2020-01-06"), pd.NaT]})
        led16 = Ledger(_ledger_frame([])); led16.use_lineage(lin_w)
        links16 = resolve_links(led16, bp, lifecycle=lc_w)
        df16, _ = returns_for_group_v2(ids_w, dates_w, raw_w, led16, links16)
        rec("rc2f: lifecycle lastTradeDate caps the old id (mirror tail cut) -> no overlap left: link at the first row 01-08, prev 102 (01-06), 0.5 x 104/102 - 1", links16.linkMode.iloc[0] == "first_row" and df16.prevDate[3] == T("2020-01-06") and abs(df16.ret[3] - (0.5 * 104.0 / 102.0 - 1)) < 1e-15,
            (links16.linkMode.iloc[0], str(df16.prevDate[3]), float(df16.ret[3])), 0.5 * 104.0 / 102.0 - 1)
        # rc2f: the old id outlives the new id -> not linked (state old_outlives_new), first row NaN
        ids_o = np.array([1, 1, 1, 2, 2], dtype=np.int64)
        dates_o = pd.to_datetime(["2020-01-03", "2020-01-08", "2020-01-10", "2020-01-08", "2020-01-09"]).to_numpy().astype("datetime64[ns]")
        raw_o = np.array([100.0, 102.0, 52.0, 104.0, 105.0], dtype=np.float32)
        pd.DataFrame({"tradingItemId": ids_o, "date": dates_o, "close": raw_o}).to_parquet(bp, index=False)
        led17 = Ledger(_ledger_frame([])); led17.use_lineage(lin_w)
        links17 = resolve_links(led17, bp)
        df17, st17 = returns_for_group_v2(ids_o, dates_o, raw_o, led17, links17)
        rec("rc2f: old id outlives the new id -> no link (old_outlives_new), new first row NaN", links17.linkMode.iloc[0] == "old_outlives_new" and np.isnan(links17.prevClose.iloc[0]) and np.isnan(df17.ret[3]) and st17["linked_rows"] == 0,
            (links17.linkMode.iloc[0], float(df17.ret[3])))
    # rc2f (X3 (d)): corporate_actions cash with exDate <= lifecycle.lastTradeDate is applied as a cash leg (flag 1); > lastTradeDate stays excluded; legacy excludes both
    lc_s = pd.DataFrame({"tradingItemId": [1, 2], "lastTradeDate": [T("2020-01-10"), pd.NaT]})
    led_s = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 1.35, "source": "corporate_actions", "notes": "settlement_like_reclassified(cash 1.35 >= 0.9 x P_prev)"},
                                  {"tradingItemId": 1, "exDate": "2020-01-13", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 50.0, "source": "corporate_actions", "notes": "settlement_like_reclassified"},
                                  {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 40.0, "source": "corporate_actions", "notes": "actionCode=ACQ"}]), lifecycle=lc_s)
    df_s, st_s = returns_for_group_v2(ids, dates, raw, led_s)
    led_sl = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 1.35, "source": "corporate_actions", "notes": "settlement_like_reclassified"}]), lifecycle=lc_s, legacy_rules=True)
    df_sl, _ = returns_for_group_v2(ids, dates, raw, led_sl)
    rec("R-X0 P0-1: corporate_actions cash legs (settlement-like reclassified or events legs) are never applied by the generator, with or without lifecycle",
        led_s.n == 0 and df_s.ret[3] == c[3] / c[2] - 1.0 and df_s.retFlag[3] == 0 and led_sl.n == 0 and df_sl.ret[3] == c[3] / c[2] - 1.0,
        (led_s.n, float(df_s.ret[3]), int(df_s.retFlag[3]), led_sl.n))
    # F2: corporate_actions-sourced rows (PVT / merger settlement) are never applied
    led12 = Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "other", "legType": "cash", "cashPerShare": 52.0, "source": "corporate_actions"},
                                  {"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 52.0, "source": "corporate_actions"},
                                  {"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0, "source": "ciqdividend"}]))
    df12, st12 = returns_for_group_v2(ids, dates, raw, led12)
    rec("F2: PVT/merger cash from corporate_actions excluded (no deal-price double count); ciqdividend leg still applied", led12.n_excluded_source == 1 and led12.n == 1 and df12.ret[4] == (c[4] + 1.0) / c[3] - 1.0, (led12.n_excluded_source, led12.n, float(df12.ret[4])))
    # F10: value leg with parentFactor != 1 next to another quantity leg on the same day -> explicit error
    try:
        Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childValuePerOldShare": 1.0, "parentFactor": 0.5},
                              {"tradingItemId": 1, "exDate": "2020-01-09", "eventType": "split", "legType": "quantity", "parentFactor": 0.5}]))
        rec("F10: double conversion on one day rejected", False, "no error")
    except ValueError as e:
        rec("F10: double conversion on one day rejected", "parentFactor != 1 next to another quantity leg" in str(e), str(e)[:60])
    # stale ledger marker: rows after the date get flag 4
    df13, st13 = returns_for_group_v2(ids, dates, raw, led, stale_after_day=int(_days(np.array(["2020-01-08"], dtype="datetime64[ns]"))[0]))
    rec("stale_after 01-08: rows 01-09/01-10 flagged 4, earlier rows unchanged", df13.retFlag.tolist() == [0, 0, 0, 4, 4] and st13["rows_stale_flagged"] == 2 and df13.ret[2] == got[2], df13.retFlag.tolist())
    # negative control: forgetting the split row -> -49% jump
    df7, _ = returns_for_group_v2(ids, dates, raw, Ledger(_ledger_frame([{"tradingItemId": 1, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0}])))
    rec("negative control: no split row -> -49% fake jump visible", df7.ret[2] < -0.4, float(df7.ret[2]))
    # legacy path still reproduces the rc1 factor formula on the same fixture
    sc = pd.DataFrame({"tradingitemid": [1], "splitdate": ["2020-01-08"], "nextsplitdate": ["2079-06-05"], "cumulativesplitfactor": [1.0], "latestsplitfactor": [2.0]})
    dv = pd.DataFrame({"tradingitemid": [1, 1], "fromdate": ["2019-01-01", "2020-01-10"], "todate": ["2020-01-09", None], "divadjfactor": [1 - 1.0 / 53.0, 1.0]})
    split = FactorLookup(build_intervals(split_windows(sc), [1], SRC_SPLIT, T("2026-09-16"), pre_first="pre"))
    div = FactorLookup(build_intervals(dividend_windows(dv), [1], SRC_DIV, T("2026-09-16")))
    lg = returns_for_group(ids, dates, raw, split, div)
    rec("legacy: dividend day = factor method P_t/(P_prev-D)-1; differs from cash-style by ~(D/P)r", abs(lg.ret[4] - (52.5 / 52.0 - 1)) < 1e-6 and 1e-6 < abs(lg.ret[4] - got[4]) < 1e-3, (float(lg.ret[4]), float(got[4])))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=Path, default=DEFAULT_STAGE, help="dir with base_data.parquet (+ factors/ for --legacy)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--ledger", type=Path, default=None, help="events/ca_ledger.parquet (default <stage>/events/ca_ledger.parquet)")
    ap.add_argument("--lineage", type=Path, default=None, help="master/lineage.parquet for first-row links (default <stage>/master/lineage.parquet if present, else the ledger's rename/relist rows)")
    ap.add_argument("--stale-after", default=None, help="YYYY-MM-DD: the ledger covers events only up to this date; later rows get retFlag 4")
    ap.add_argument("--links-out", type=Path, default=None, help="write the resolved link table (csv)")
    ap.add_argument("--stitched-copies", default=None, help="R1 ca_ledger_stitched_copies.csv to mirror keeper legs onto successor ids (default: auto next to the ledger; 'off' disables)")
    ap.add_argument("--legacy", action="store_true", help="rc1 vendor-factor total return (adj = close/sf x df)")
    ap.add_argument("--lifecycle", type=Path, default=None, help="master/lifecycle.parquet: caps the old id's last trade day for links (default <stage>/master/lifecycle.parquet if present)")
    ap.add_argument("--legacy-links", action="store_true", help="rc2 link rule (first row, chainable ignored, no lifecycle cap) — bit-identity gate")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)
    pa.set_cpu_count(args.threads)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"], "| got", r["got"], "" if r["expected"] is None else f"| expected {r['expected']}")
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0
    sc = False if args.stitched_copies == "off" else (Path(args.stitched_copies) if args.stitched_copies else None)
    info = build_returns(args.stage, args.out, ledger=args.ledger, legacy=args.legacy, lineage=args.lineage, stale_after=args.stale_after, links_out=args.links_out, stitched_copies=sc,
                         lifecycle=args.lifecycle, legacy_links=args.legacy_links)
    work = args.stage.parent / (args.stage.name + "_work")
    if work.exists():
        (work / "returns_info.json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
