#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/returns_ledger_adapter.py - R2 provisional corporate-action ledger (rc2 BRIEF section 3-1 schema).

Stand-in for R1's `events/ca_ledger.parquet` so the v2 returns generator (prices_returns.py) can be developed and
gated before R1 lands. Built ONLY from existing rc1 build tables (no DB access):

  cash rows       dividends/dividends_long.parquet: cash types (isCash, plus 16/24 = Canadian 10% variants of 15/23),
                  pre-tax amount = grossAmount if present else divAmount, USD = local / fxRate (P1/P2, section 3-1:
                  "gross first, else divAmount"). Where gross and divAmount disagree (>0.1%; 1,269 vendor rows, both
                  directions: CRH net-vs-gross, BCE CAD-vs-USD, PFE/VER stale gross) the vendor divAdjFactor step is
                  used only as a TIE-BREAK between the two vendor amounts (P5: cross-check use), never as the value.
                  One row per (id, exDate): the legs of the same day are summed (evidence lists the dividendIds).
  split rows      factors/split.parquet interval boundaries where the factor changes: parentFactor = f_prev / f_next.
  stock rows      distributions/distributions.parquet kind=stock: child = newTradingItemId, childRatio = ratioNewPerOld
                  (per pre-event raw share; the proto table's checkValueFrac is defined that way), value = childRatio x
                  child raw close on exDate from base_data (P10 (1); a later first print is never pulled back).
                  status ok / value_mismatch minor(<=0.10 vs vendor implied) -> ok; major / ratio_unknown / no_price /
                  new_id_unknown -> unresolved (value NaN, kept visible). Sibling rule: an unresolved leg whose company has
                  an ok leg on another line for the same exDate/leg borrows child+ratio (TYC 314828302 <- 34969540).
  manual_override HON 2618950 2026-06-29 -> HONA 2005551375 x0.5 (BRIEF rc2 section 2, GS observation -204 -> -102 HON
                  + -102 HONA; the proto row is ratio_unknown).
  rename/relist   master/lineage.parquet relation rename/relist: parentFactor = ratio (NaN -> 1.0, estimated). Used by
                  the generator only to link the new id's first row to the old id's last close (section 3-2).
  merger rows     are NOT emitted (engine events file owns settlement; returns_total never included it in rc1 either).

exDate on a non-trading day (calendar.parquet) is moved to the next trading day (notes record it).

CLI:
  returns_ledger_adapter.py --build <rc1 build dir> --out <ledger.parquet> [--base-data <path>]
  returns_ledger_adapter.py --self-test
"""
from __future__ import annotations

import argparse
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
from prices_factors import FactorLookup  # noqa: E402

log = logging.getLogger("cache_v21.returns_ledger_adapter")

LEDGER_COLUMNS = ["eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "cashCurrency", "fxToUSD",
                  "parentFactor", "childTradingItemId", "childRatio", "childValueSource", "childValuePerOldShare",
                  "subscriptionPrice", "expiryDate", "exerciseRatio", "payDate", "recordDate", "announcedDate",
                  "source", "evidence", "status", "isEstimate", "repairOf", "notes"]
LEDGER_SCHEMA = pa.schema([
    ("eventId", pa.string()), ("tradingItemId", pa.int64()), ("exDate", pa.timestamp("ns")), ("eventType", pa.string()),
    ("legType", pa.string()), ("cashPerShare", pa.float64()), ("cashCurrency", pa.string()), ("fxToUSD", pa.float64()),
    ("parentFactor", pa.float64()), ("childTradingItemId", pa.int64()), ("childRatio", pa.float64()),
    ("childValueSource", pa.string()), ("childValuePerOldShare", pa.float64()), ("subscriptionPrice", pa.float64()),
    ("expiryDate", pa.timestamp("ns")), ("exerciseRatio", pa.float64()), ("payDate", pa.timestamp("ns")),
    ("recordDate", pa.timestamp("ns")), ("announcedDate", pa.timestamp("ns")), ("source", pa.string()),
    ("evidence", pa.string()), ("status", pa.string()), ("isEstimate", pa.bool_()), ("repairOf", pa.string()),
    ("notes", pa.string())])
CASH_EXTRA_TYPES = (16, 24)          # Canadian 10% withholding variants (same semantics as 15/23 which are isCash)
SPECIAL_TYPES = (4,)                 # "extra, special, participating dividends, arrears, and liquidations"
GROSS_DIV_TOL = 1e-3                 # relative: gross vs divAmount disagreement worth a tie-break
TIEBREAK_TOL = 0.02                  # relative: vendor-implied D within 2% of a candidate -> that candidate
CURRENCY_USD = 160
MANUAL_STOCK_OVERRIDES = [
    # parent, exDate, child, ratio per old share, evidence
    (2618950, "2026-06-29", 2005551375, 0.5, "BRIEF rc2 section 2: GS TRS HON 2026-06-29 -204 -> -102 HON + -102 HONA (0.5 HONA per old HON, 1:2 reverse split)"),
]


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _empty_ledger() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in zip(LEDGER_COLUMNS, [
        "object", "int64", "datetime64[ns]", "object", "object", "float64", "object", "float64", "float64", "Int64", "float64",
        "object", "float64", "float64", "datetime64[ns]", "float64", "datetime64[ns]", "datetime64[ns]", "datetime64[ns]",
        "object", "object", "object", "bool", "object", "object"])})


def finalize_ledger(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing columns with nulls, set dtypes, eventId, sort (tradingItemId, exDate, legType, child)."""
    base = _empty_ledger()
    for c in LEDGER_COLUMNS:
        if c not in df.columns:
            df[c] = pd.Series([None] * len(df), index=df.index, dtype=base[c].dtype) if len(df) else base[c]
    df = df[LEDGER_COLUMNS].copy()
    df["tradingItemId"] = df.tradingItemId.astype("int64")
    df["exDate"] = pd.to_datetime(df.exDate).astype("datetime64[ns]")
    for c in ("expiryDate", "payDate", "recordDate", "announcedDate"):
        df[c] = pd.to_datetime(df[c]).astype("datetime64[ns]")
    for c in ("cashPerShare", "fxToUSD", "parentFactor", "childRatio", "childValuePerOldShare", "subscriptionPrice", "exerciseRatio"):
        df[c] = pd.to_numeric(df[c]).astype("float64")
    df["parentFactor"] = df.parentFactor.fillna(1.0)
    df["childTradingItemId"] = df.childTradingItemId.astype("Int64")
    df["isEstimate"] = df.isEstimate.fillna(False).astype(bool)
    df["status"] = df.status.fillna("ok")
    for c in ("cashCurrency", "childValueSource", "source", "evidence", "repairOf", "notes", "eventType", "legType"):
        df[c] = df[c].astype("object").where(df[c].notna(), None)
    df["childValueSource"] = df.childValueSource.where(df.childValueSource.notna(), "unknown")
    df = df.sort_values(["tradingItemId", "exDate", "legType", "childTradingItemId"], kind="stable").reset_index(drop=True)
    seq = df.groupby(["tradingItemId", "exDate", "eventType"]).cumcount()
    df["eventId"] = (df.tradingItemId.astype(str) + "_" + df.exDate.dt.strftime("%Y-%m-%d") + "_" + df.eventType + "_" + seq.astype(str))
    return df


def write_ledger(df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, schema=LEDGER_SCHEMA, preserve_index=False), out, compression="zstd")


def check_ledger_invariants(df: pd.DataFrame) -> dict:
    key = ["tradingItemId", "exDate", "legType", "childTradingItemId"]
    dup = int(df.duplicated(key).sum())
    bad_pf = int((~np.isfinite(df.parentFactor) | (df.parentFactor <= 0)).sum())
    bad_status = int((~df.status.isin(["ok", "estimated", "unresolved"])).sum())
    ok_stock_nan = int(((df.legType == "stock") & (df.status == "ok") & df.childValuePerOldShare.isna()).sum())
    ok_cash_nan = int(((df.legType == "cash") & (df.status == "ok") & df.cashPerShare.isna()).sum())
    return {"ok": dup == 0 and bad_pf == 0 and bad_status == 0 and ok_stock_nan == 0 and ok_cash_nan == 0,
            "duplicate_keys": dup, "bad_parentFactor": bad_pf, "bad_status": bad_status,
            "ok_stock_without_value": ok_stock_nan, "ok_cash_without_amount": ok_cash_nan, "rows": int(len(df))}


class TradingCalendar:
    """calendar.parquet -> next trading day on/after a date (dates before the calendar start are kept as is)."""

    def __init__(self, cal: pd.DataFrame):
        d = pd.to_datetime(cal[cal.isTradingDay].date).sort_values().to_numpy().astype("datetime64[ns]")
        self.days = d
        self.start, self.end = d[0], d[-1]

    def roll(self, dates: pd.Series) -> tuple[pd.Series, np.ndarray]:
        """Return (rolled dates, moved mask). Dates after the calendar end stay as is."""
        x = pd.to_datetime(dates).to_numpy().astype("datetime64[ns]")
        pos = np.searchsorted(self.days, x, side="left")
        inside = (x >= self.start) & (x <= self.end)
        rolled = x.copy()
        p = np.minimum(pos, len(self.days) - 1)
        rolled[inside] = self.days[p[inside]]
        moved = inside & (rolled != x)
        return pd.Series(rolled, index=dates.index), moved


class CloseLookup:
    """(id, date) -> raw close from base_data for a set of ids (float64), plus previous observed close."""

    def __init__(self, base_data: Path, ids):
        ids = np.unique(np.asarray(list(ids), dtype=np.int64))
        t = pq.read_table(base_data, columns=["tradingItemId", "date", "close"],
                          filters=[("tradingItemId", "in", ids.tolist())]) if len(ids) else None
        if t is None or t.num_rows == 0:
            self.ids = np.zeros(0, np.int64); self.days = np.zeros(0, np.int64); self.close = np.zeros(0); self.keys = np.zeros(0, np.int64)
            return
        df = t.to_pandas()
        df = df[np.isfinite(df.close.to_numpy())].sort_values(["tradingItemId", "date"])
        self.ids = df.tradingItemId.to_numpy(np.int64)
        self.days = df.date.to_numpy().astype("datetime64[D]").astype(np.int64)
        self.close = df.close.to_numpy().astype(np.float64)
        self.keys = (self.ids << 20) | (self.days + 30000)

    def _pos(self, ids, dates, side):
        days = np.asarray(dates).astype("datetime64[D]").astype(np.int64) + 30000
        q = (np.asarray(ids, dtype=np.int64) << 20) | days
        return np.searchsorted(self.keys, q, side=side)

    def at(self, ids, dates) -> np.ndarray:
        """Close exactly on (id, date); NaN if no row."""
        if len(self.keys) == 0:
            return np.full(len(np.asarray(ids)), np.nan)
        pos = self._pos(ids, dates, "left")
        ok = pos < len(self.keys)
        p = np.minimum(pos, len(self.keys) - 1)
        days = np.asarray(dates).astype("datetime64[D]").astype(np.int64) + 30000
        q = (np.asarray(ids, dtype=np.int64) << 20) | days
        ok &= self.keys[p] == q
        out = np.full(len(p), np.nan)
        out[ok] = self.close[p[ok]]
        return out

    def before(self, ids, dates) -> tuple[np.ndarray, np.ndarray]:
        """Last observed close strictly before (id, date): (close, day int) ; NaN/-1 if none."""
        n = len(np.asarray(ids))
        if len(self.keys) == 0:
            return np.full(n, np.nan), np.full(n, -1, np.int64)
        pos = self._pos(ids, dates, "left") - 1
        ok = pos >= 0
        p = np.maximum(pos, 0)
        ok &= self.ids[p] == np.asarray(ids, dtype=np.int64)
        out = np.full(n, np.nan); day = np.full(n, -1, np.int64)
        out[ok] = self.close[p[ok]]; day[ok] = self.days[p[ok]]
        return out, day


# --------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------
def cash_rows(dv: pd.DataFrame, cal: TradingCalendar, base_data: Path | None, div_factor: FactorLookup | None) -> pd.DataFrame:
    d = dv[(dv.isCash | dv.dividendTypeId.isin(CASH_EXTRA_TYPES)) & dv.divAmount.notna()].copy()
    d["local"] = d.grossAmount.where(d.grossAmount.notna(), d.divAmount)
    fx = d.fxRate.copy()
    usd_ccy = d.currencyId.isna() | (d.currencyId == CURRENCY_USD)
    fx = fx.where(fx.notna(), np.where(usd_ccy, 1.0, np.nan))
    d["fx"] = fx
    d["usd"] = d.local / d.fx
    d["gross_ne_div"] = d.grossAmount.notna() & ((d.grossAmount - d.divAmount).abs() > GROSS_DIV_TOL * d.divAmount.abs())
    d["exDate0"] = d.exDate
    d["exDate"], moved = cal.roll(d.exDate)
    d["moved"] = moved
    # tie-break gross vs divAmount with the vendor dividend-factor step (cross-check use only)
    d["tiebreak"] = ""
    amb = d[d.gross_ne_div & d.usd.notna()]
    if len(amb) and base_data is not None and div_factor is not None:
        cl = CloseLookup(base_data, amb.tradingItemId.unique())
        p_prev, prev_day = cl.before(amb.tradingItemId.to_numpy(), amb.exDate.to_numpy())
        have = np.isfinite(p_prev)
        implied = np.full(len(amb), np.nan)
        if have.any():
            ids = amb.tradingItemId.to_numpy()[have]
            df_ex = div_factor(ids, amb.exDate.to_numpy()[have], strict=False)
            df_prev = div_factor(ids, prev_day[have].astype("datetime64[D]").astype("datetime64[ns]"), strict=False)
            implied[have] = p_prev[have] * (1.0 - df_prev / df_ex)   # vendor: df_prev/df_ex = 1 - D/P_prev
        g_usd = (amb.grossAmount / amb.fx).to_numpy(); v_usd = (amb.divAmount / amb.fx).to_numpy()
        pick_div = np.isfinite(implied) & (np.abs(implied - v_usd) <= TIEBREAK_TOL * np.abs(v_usd)) & ~(np.abs(implied - g_usd) <= TIEBREAK_TOL * np.abs(g_usd))
        pick_gross = np.isfinite(implied) & (np.abs(implied - g_usd) <= TIEBREAK_TOL * np.abs(g_usd))
        note = np.where(pick_div, "gross_ne_div: vendor step matches divAmount -> divAmount",
                        np.where(pick_gross, "gross_ne_div: vendor step matches grossAmount -> gross",
                                 "gross_ne_div: vendor step matches neither -> gross (contract default), estimated"))
        d.loc[amb.index, "tiebreak"] = note
        d.loc[amb.index[pick_div], "usd"] = v_usd[pick_div]
    d["is_est"] = d.tiebreak.str.contains("neither", regex=False)
    d["is_special"] = d.dividendTypeId.isin(SPECIAL_TYPES)
    d["unres"] = d.usd.isna()
    g = d.groupby(["tradingItemId", "exDate"], sort=True)
    out = pd.DataFrame({
        "cashPerShare": g.usd.sum(min_count=1),
        "cashCurrency": g.currencyId.first().map(lambda c: "USD" if pd.isna(c) or c == CURRENCY_USD else f"ccy{int(c)}"),
        "fxToUSD": g.fx.first().map(lambda f: np.nan if pd.isna(f) else 1.0 / f),
        "payDate": g.payDate.first(), "recordDate": g.recordDate.first(), "announcedDate": g.announcedDate.first(),
        "evidence": g.dividendId.apply(lambda s: "ciqdividend dividendId=" + ",".join(str(int(x)) for x in s)),
        "n_special": g.is_special.sum(), "n_est": g.is_est.sum(), "n_unres": g.unres.sum(), "n_moved": g.moved.sum(),
        "tiebreak": g.tiebreak.apply(lambda s: "; ".join(x for x in s if x)),
        "types": g.dividendTypeId.apply(lambda s: "+".join(str(int(x)) for x in s)),
        "ex0": g.exDate0.first(),
    }).reset_index()
    out["eventType"] = np.where(out.n_special > 0, "special_dividend", "cash_dividend")
    out["legType"] = "cash"
    out["parentFactor"] = 1.0
    out["source"] = "ciqdividend"
    out["status"] = np.where(out.n_unres > 0, "unresolved", np.where(out.n_est > 0, "estimated", "ok"))
    out["isEstimate"] = out.n_est > 0
    notes = "types=" + out.types
    notes = notes + np.where(out.n_moved > 0, "; exDate moved to next trading day from " + out.ex0.dt.strftime("%Y-%m-%d"), "")
    notes = notes + np.where(out.tiebreak != "", "; " + out.tiebreak, "")
    notes = notes + np.where(out.n_unres > 0, "; no FX rate for non-USD amount (unresolved)", "")
    out["notes"] = notes
    out.loc[out.n_unres > 0, "cashPerShare"] = np.nan
    return out.drop(columns=["n_special", "n_est", "n_unres", "n_moved", "tiebreak", "types", "ex0"])


def split_rows(sp: pd.DataFrame) -> pd.DataFrame:
    s = sp.sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    same = s.tradingItemId.to_numpy()[1:] == s.tradingItemId.to_numpy()[:-1]
    f_prev = s.factor.to_numpy()[:-1]; f_next = s.factor.to_numpy()[1:]
    change = same & (f_prev != f_next)
    idx = np.flatnonzero(change) + 1
    out = pd.DataFrame({
        "tradingItemId": s.tradingItemId.to_numpy()[idx], "exDate": s.validFrom.to_numpy()[idx],
        "parentFactor": f_prev[idx - 1] / f_next[idx - 1],
        "source": s.source.to_numpy()[idx],
        "evidence": ["factors/split validFrom=%s factor %g -> %g (%s)" % (pd.Timestamp(v).date(), a, b, src)
                     for v, a, b, src in zip(s.validFrom.to_numpy()[idx], f_prev[idx - 1], f_next[idx - 1], s.source.to_numpy()[idx])],
    })
    out["eventType"] = "split"; out["legType"] = "quantity"; out["status"] = "ok"; out["isEstimate"] = False
    out["notes"] = "parentFactor = splitFactor_prev / splitFactor_next"
    return out


def stock_rows(dist: pd.DataFrame, cal: TradingCalendar, base_data: Path, instruments: pd.DataFrame | None) -> pd.DataFrame:
    s = dist[dist.kind == "stock"].copy()
    s["exDate0"] = s.exDate
    s["exDate"], moved = cal.roll(s.exDate)
    s["moved"] = moved
    s["child"] = s.newTradingItemId.astype("Int64")
    s["ratio"] = pd.to_numeric(s.ratioNewPerOld).astype("float64")
    s["mm"] = s.mismatchClass.astype("object").where(s.mismatchClass.notna(), "")
    s["valuable"] = (s.status == "ok") | ((s.status == "value_mismatch") & s.mm.str.startswith("minor"))
    s["borrowed"] = ""
    # sibling rule: same company, same exDate/leg, another line has a valuable row
    if instruments is not None:
        comp = instruments.set_index("tradingItemId").companyId
        s["companyId"] = s.tradingItemId.map(comp)
        good = s[s.valuable & s.child.notna() & s.ratio.notna() & s.companyId.notna()]
        gkey = good.set_index(["companyId", "exDate", "leg"])[["child", "ratio", "tradingItemId", "status"]]
        gkey = gkey[~gkey.index.duplicated()]
        need = s[~s.valuable & s.companyId.notna()]
        for i, r in need.iterrows():
            k = (r.companyId, r.exDate, r.leg)
            if k in gkey.index and gkey.loc[k, "tradingItemId"] != r.tradingItemId:
                gr = gkey.loc[k]
                s.loc[i, "child"] = gr.child; s.loc[i, "ratio"] = gr.ratio; s.loc[i, "valuable"] = True
                s.loc[i, "borrowed"] = f"child/ratio borrowed from sibling line {int(gr.tradingItemId)} (status {gr.status}); own status {r.status}"
    # manual overrides (documented in BRIEF rc2 section 2)
    for pid, ex, cid, ratio, ev in MANUAL_STOCK_OVERRIDES:
        m = (s.tradingItemId == pid) & (s.exDate == pd.Timestamp(ex)) & (s.leg == 0)
        if m.any():
            s.loc[m, ["child", "ratio", "valuable"]] = [cid, ratio, True]
            s.loc[m, "borrowed"] = "manual_override: " + ev
        else:
            s = pd.concat([s, pd.DataFrame([{"tradingItemId": pid, "exDate": pd.Timestamp(ex), "exDate0": pd.Timestamp(ex), "leg": 0, "child": cid,
                                             "ratio": ratio, "valuable": True, "borrowed": "manual_override: " + ev, "status": "ok", "mm": "",
                                             "moved": False, "eventClass": "spinoff", "source": "manual_override", "evidence": ev,
                                             "ratioBasis": "manual", "impliedValueFrac": np.nan, "checkValueFrac": np.nan,
                                             "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT}])], ignore_index=True)
    # child close on exDate (raw, base_data); never a later print
    val_idx = s.index[s.valuable & s.child.notna()]
    child_close = np.full(len(s), np.nan)
    if len(val_idx):
        cl = CloseLookup(base_data, s.loc[val_idx, "child"].astype("int64").unique())
        child_close[s.index.get_indexer(val_idx)] = cl.at(s.loc[val_idx, "child"].astype("int64").to_numpy(), s.loc[val_idx, "exDate"].to_numpy())
    s["childClose"] = child_close
    s["value"] = s.ratio * s.childClose
    resolved = s.valuable & s.child.notna() & np.isfinite(s.value.to_numpy())
    s["status_out"] = np.where(resolved, "ok", "unresolved")
    amb = s.ratioBasis.astype("object").fillna("").str.contains("ambiguous")
    s.loc[resolved & amb, "status_out"] = "estimated"
    s["isEstimate"] = resolved & amb
    s.loc[~resolved, "value"] = np.nan
    src = s.source.astype("object").fillna("distributions")
    src = np.where(s.borrowed.str.startswith("manual_override"), "manual_override",
                   np.where(s.borrowed != "", "distributions", np.where(src.str.startswith("web"), "distributions", "distributions")))
    reason = np.where(resolved, "",
                      np.where(~s.valuable, "proto status " + s.status.astype(str) + np.where(s.mm != "", " (" + s.mm + ")", ""),
                               np.where(s.child.isna(), "child id unknown", "child has no raw close on exDate (P10: later print not used)")))
    notes = ("proto status=" + s.status.astype(str) + "; ratioBasis=" + s.ratioBasis.astype("object").fillna("").astype(str)
             + "; eventClass=" + s.eventClass.astype("object").fillna("").astype(str)
             + "; vendor implied frac=" + s.impliedValueFrac.map(lambda x: "nan" if pd.isna(x) else f"{x:.4f}")
             + "; proto check frac=" + s.checkValueFrac.map(lambda x: "nan" if pd.isna(x) else f"{x:.4f}"))
    notes = notes + np.where(s.moved, "; exDate moved to next trading day from " + s.exDate0.dt.strftime("%Y-%m-%d"), "")
    notes = notes + np.where(s.borrowed != "", "; " + s.borrowed, "")
    notes = notes + np.where(reason != "", "; unresolved: " + reason, "")
    ev = s.evidence.astype("object").fillna("").astype(str).str.slice(0, 300)
    ev = np.where(s.borrowed.str.startswith("manual_override"), s.borrowed.str.replace("manual_override: ", "", regex=False), ev)
    out = pd.DataFrame({
        "tradingItemId": s.tradingItemId.astype("int64"), "exDate": s.exDate,
        "eventType": np.where(s.eventClass.astype("object").fillna("").isin(["spinoff", "unknown", ""]), "spin_off", "other"),
        "legType": "stock", "parentFactor": 1.0, "childTradingItemId": s.child, "childRatio": s.ratio,
        "childValueSource": np.where(resolved, "child_close_ex", "unknown"), "childValuePerOldShare": s.value,
        "payDate": s.payDate, "recordDate": s.recordDate, "announcedDate": s.announcedDate,
        "source": src, "evidence": ev, "status": s.status_out, "isEstimate": s.isEstimate, "notes": notes,
    })
    # a proto event may list the same child twice (dup legs) -> keep the first resolved one
    out = out.sort_values(["tradingItemId", "exDate", "childTradingItemId", "status"]).drop_duplicates(["tradingItemId", "exDate", "childTradingItemId"], keep="first")
    return out


def lineage_rows(lin: pd.DataFrame, cal: TradingCalendar) -> pd.DataFrame:
    l = lin[lin.relation.isin(["rename", "relist"])].copy()
    l["exDate"], moved = cal.roll(l.effectiveDate)
    est = l.ratio.isna()
    out = pd.DataFrame({
        "tradingItemId": l.oldId.astype("int64"), "exDate": l.exDate, "eventType": l.relation, "legType": "quantity",
        "parentFactor": l.ratio.fillna(1.0), "childTradingItemId": l.newId.astype("Int64"), "childRatio": np.nan,
        "childValueSource": "unknown", "source": "lineage", "evidence": l.evidence.astype(str).str.slice(0, 300),
        "status": np.where(est, "estimated", "ok"), "isEstimate": est,
        "notes": "confidence=" + l.confidence.astype(str) + np.where(est, "; ratio unknown -> parentFactor 1.0 assumed", "")
                 + np.where(moved, "; effectiveDate moved to next trading day", ""),
    })
    return out


def build_provisional_ledger(build: Path, base_data: Path | None = None, out: Path | None = None) -> tuple[pd.DataFrame, dict]:
    t0 = time.time()
    build = Path(build)
    base_data = Path(base_data) if base_data else build / "base_data.parquet"
    cal = TradingCalendar(pd.read_parquet(build / "calendar.parquet"))
    dv = pd.read_parquet(build / "dividends" / "dividends_long.parquet")
    dist = pd.read_parquet(build / "distributions" / "distributions.parquet")
    sp = pd.read_parquet(build / "factors" / "split.parquet")
    lin = pd.read_parquet(build / "master" / "lineage.parquet")
    inst_p = build / "master" / "instruments.parquet"
    inst = pd.read_parquet(inst_p, columns=["tradingItemId", "companyId"]) if inst_p.exists() else None
    div_p = build / "factors" / "dividend.parquet"
    div_lookup = FactorLookup(pd.read_parquet(div_p)) if div_p.exists() else None
    parts = {"cash": cash_rows(dv, cal, base_data, div_lookup), "split": split_rows(sp),
             "stock": stock_rows(dist, cal, base_data, inst), "lineage": lineage_rows(lin, cal)}
    led = finalize_ledger(pd.concat(parts.values(), ignore_index=True))
    # only ids of the symbol set matter for returns; keep everything but count
    inv = check_ledger_invariants(led)
    info = {"rows": {k: int(len(v)) for k, v in parts.items()}, "total": int(len(led)), "invariants": inv,
            "status": led.groupby(["eventType", "status"]).size().to_dict(), "seconds": round(time.time() - t0, 1)}
    info["status"] = {f"{a}/{b}": int(n) for (a, b), n in info["status"].items()}
    if out is not None:
        write_ledger(led, Path(out))
        Path(out).with_suffix(".json").write_text(json.dumps(info, indent=1, default=str))
    return led, info


# --------------------------------------------------------------------------------------
# self-test on tiny synthetic tables
# --------------------------------------------------------------------------------------
def self_test() -> list[dict]:
    import tempfile
    T = pd.Timestamp
    res = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "dividends").mkdir(); (tmp / "distributions").mkdir(); (tmp / "factors").mkdir(); (tmp / "master").mkdir()
        days = pd.bdate_range("2020-01-01", "2020-03-31")
        cal = pd.DataFrame({"date": days, "nClose": 10, "rollingMedian": 10.0, "isTradingDay": True})
        cal.loc[cal.date == T("2020-01-20"), "isTradingDay"] = False        # holiday
        cal.to_parquet(tmp / "calendar.parquet", index=False)
        base = []
        for tid, px in ((1, 100.0), (2, 50.0), (3, 20.0)):
            for i, d in enumerate(days):
                if d == T("2020-01-20"):
                    continue
                base.append({"date": d, "tradingItemId": tid, "close": np.float32(px + i * 0.1)})
        pd.DataFrame(base).to_parquet(tmp / "base_data.parquet", index=False)
        dv = pd.DataFrame([
            {"dividendId": 1, "tradingItemId": 1, "exDate": T("2020-01-10"), "divAmount": 1.0, "grossAmount": np.nan, "netAmount": np.nan, "currencyId": 160.0, "fxRate": 1.0, "dividendTypeId": 1, "isCash": True, "isStock": False, "payDate": T("2020-02-01"), "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"dividendId": 2, "tradingItemId": 1, "exDate": T("2020-01-10"), "divAmount": 0.5, "grossAmount": np.nan, "netAmount": np.nan, "currencyId": 160.0, "fxRate": 1.0, "dividendTypeId": 4, "isCash": True, "isStock": False, "payDate": T("2020-02-01"), "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"dividendId": 3, "tradingItemId": 1, "exDate": T("2020-01-18"), "divAmount": 0.2775, "grossAmount": 0.37, "netAmount": 0.2775, "currencyId": 160.0, "fxRate": 1.0, "dividendTypeId": 1, "isCash": True, "isStock": False, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"dividendId": 4, "tradingItemId": 2, "exDate": T("2020-02-03"), "divAmount": 1.38, "grossAmount": np.nan, "netAmount": np.nan, "currencyId": 27.0, "fxRate": 1.38, "dividendTypeId": 1, "isCash": True, "isStock": False, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"dividendId": 5, "tradingItemId": 2, "exDate": T("2020-02-10"), "divAmount": np.nan, "grossAmount": np.nan, "netAmount": np.nan, "currencyId": np.nan, "fxRate": np.nan, "dividendTypeId": 20, "isCash": False, "isStock": True, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"dividendId": 6, "tradingItemId": 3, "exDate": T("2020-03-02"), "divAmount": 2.0, "grossAmount": np.nan, "netAmount": np.nan, "currencyId": 27.0, "fxRate": np.nan, "dividendTypeId": 16, "isCash": False, "isStock": False, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
        ])
        dv.to_parquet(tmp / "dividends" / "dividends_long.parquet", index=False)
        dist = pd.DataFrame([
            {"tradingItemId": 2, "exDate": T("2020-02-10"), "kind": "stock", "leg": 0, "status": "ok", "newTradingItemId": 3, "ratioNewPerOld": 0.5, "ratioBasis": "per_share_text", "mismatchClass": None, "eventClass": "spinoff", "source": "ciqdividend20", "evidence": "x", "impliedValueFrac": 0.2, "checkValueFrac": 0.2, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"tradingItemId": 2, "exDate": T("2020-03-16"), "kind": "stock", "leg": 0, "status": "ratio_unknown", "newTradingItemId": None, "ratioNewPerOld": np.nan, "ratioBasis": None, "mismatchClass": None, "eventClass": "unknown", "source": "ciqdividend20", "evidence": "", "impliedValueFrac": 0.1, "checkValueFrac": np.nan, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
            {"tradingItemId": 1, "exDate": T("2020-03-16"), "kind": "stock", "leg": 0, "status": "ok", "newTradingItemId": 3, "ratioNewPerOld": 0.1, "ratioBasis": "per_share_text", "mismatchClass": None, "eventClass": "spinoff", "source": "ciqdividend20", "evidence": "y", "impliedValueFrac": 0.05, "checkValueFrac": 0.05, "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT},
        ])
        dist["newTradingItemId"] = dist.newTradingItemId.astype("Int64")
        dist.to_parquet(tmp / "distributions" / "distributions.parquet", index=False)
        sp = pd.DataFrame({"tradingItemId": [1, 1, 2], "validFrom": [T("1900-01-01"), T("2020-02-20"), T("1900-01-01")],
                           "validTo": [T("2020-02-20"), pd.NaT, pd.NaT], "factor": [2.0, 1.0, 1.0],
                           "source": ["synthetic_pre_first", "ciqsplitcache", "synthetic_no_window"], "snapshotDate": T("2026-09-16")})
        sp.to_parquet(tmp / "factors" / "split.parquet", index=False)
        pd.DataFrame({"oldId": [1], "newId": [4], "relation": ["rename"], "effectiveDate": [T("2020-04-01")], "ratio": [np.nan],
                      "evidence": ["test"], "confidence": ["medium"]}).to_parquet(tmp / "master" / "lineage.parquet", index=False)
        pd.DataFrame({"tradingItemId": [1, 2, 3], "companyId": [10, 20, 30]}).to_parquet(tmp / "master" / "instruments.parquet", index=False)
        led, info = build_provisional_ledger(tmp, tmp / "base_data.parquet", None)

    def rec(name, ok, got):
        res.append({"case": name, "ok": bool(ok), "got": got})

    c = led[(led.tradingItemId == 1) & (led.exDate == T("2020-01-10")) & (led.legType == "cash")]
    rec("two cash legs same day summed (1.0 + 0.5), special_dividend", len(c) == 1 and abs(c.cashPerShare.iloc[0] - 1.5) < 1e-12 and c.eventType.iloc[0] == "special_dividend",
        c[["cashPerShare", "eventType"]].to_dict("records"))
    c = led[(led.tradingItemId == 1) & (led.legType == "cash") & (led.exDate == T("2020-01-21"))]
    rec("exDate on holiday 01-18(Sat)/01-20(holiday) -> 01-21; gross 0.37 preferred over net 0.2775 (no factor table -> gross)",
        len(c) == 1 and abs(c.cashPerShare.iloc[0] - 0.37) < 1e-12 and "moved" in c.notes.iloc[0], c[["cashPerShare", "notes"]].to_dict("records"))
    c = led[(led.tradingItemId == 2) & (led.legType == "cash")]
    rec("CAD 1.38 / fx 1.38 -> USD 1.0", len(c) == 1 and abs(c.cashPerShare.iloc[0] - 1.0) < 1e-12, c.cashPerShare.tolist())
    c = led[(led.tradingItemId == 3) & (led.legType == "cash")]
    rec("type 16 without fx -> unresolved cash row (NaN, visible)", len(c) == 1 and c.status.iloc[0] == "unresolved" and np.isnan(c.cashPerShare.iloc[0]), c[["status", "cashPerShare"]].to_dict("records"))
    s = led[led.legType == "quantity"]
    rec("split boundary 2020-02-20 factor 2->1 : parentFactor 2.0 on id 1 only", len(s[s.eventType == "split"]) == 1 and abs(s[s.eventType == "split"].parentFactor.iloc[0] - 2.0) < 1e-12 and s[s.eventType == "split"].tradingItemId.iloc[0] == 1,
        s[s.eventType == "split"][["tradingItemId", "exDate", "parentFactor"]].to_dict("records"))
    st = led[(led.tradingItemId == 2) & (led.legType == "stock") & (led.exDate == T("2020-02-10"))]
    i = list(days).index(T("2020-02-10")) - 1  # one day removed before (01-20) -> index shift handled by close value
    exp = 0.5 * float(np.float32(20.0 + list(days).index(T("2020-02-10")) * 0.1))
    rec("spin-off value = ratio x child raw close on exDate", len(st) == 1 and abs(st.childValuePerOldShare.iloc[0] - exp) < 1e-9 and st.status.iloc[0] == "ok" and st.childValueSource.iloc[0] == "child_close_ex",
        st[["childValuePerOldShare", "status", "childValueSource"]].to_dict("records") + [exp])
    st = led[(led.tradingItemId == 2) & (led.legType == "stock") & (led.exDate == T("2020-03-16"))]
    rec("ratio_unknown proto row -> unresolved, NaN value, kept", len(st) == 1 and st.status.iloc[0] == "unresolved" and np.isnan(st.childValuePerOldShare.iloc[0]), st[["status", "notes"]].to_dict("records"))
    ln = led[led.eventType == "rename"]
    rec("rename row: parentFactor 1.0 assumed (ratio NaN) -> estimated", len(ln) == 1 and ln.parentFactor.iloc[0] == 1.0 and ln.status.iloc[0] == "estimated" and ln.childTradingItemId.iloc[0] == 4, ln[["parentFactor", "status"]].to_dict("records"))
    rec("invariants hold (unique key, statuses, no ok rows without values)", info["invariants"]["ok"], info["invariants"])
    # negative control: a duplicated key must be caught
    bad = pd.concat([led, led.iloc[[0]]], ignore_index=True)
    rec("negative control: duplicated key detected", not check_ledger_invariants(bad)["ok"], check_ledger_invariants(bad)["duplicate_keys"])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", type=Path, help="rc1 build dir (base_data.parquet, dividends/, distributions/, factors/, master/, calendar.parquet)")
    ap.add_argument("--base-data", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"], "| got", r["got"])
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0
    if not args.build or not args.out:
        ap.error("--build and --out are required")
    _, info = build_provisional_ledger(args.build, args.base_data, args.out)
    print(json.dumps(info, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
