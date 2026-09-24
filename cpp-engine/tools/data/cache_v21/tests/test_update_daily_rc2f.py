#!/home/jgo/usr/venv-3.11/bin/python
"""D2 tests: rc2f rules of the daily update (update_daily_rc2f.py) on synthetic fixtures, no DB.

  /home/jgo/usr/venv-3.11/bin/python -m pytest tests/test_update_daily_rc2f.py -q
  /home/jgo/usr/venv-3.11/bin/python tests/test_update_daily_rc2f.py [-k name]

Cases (expected values computed first, then compared):
  price     apply_price_to_rows changes only fresh rows inside a window (multiply, flag 8/32), integrity gate passes / fails on a lost row
  split     reapply_split_overrides on a vendor table is idempotent (second application on the result = no_op / same factors), a
            missing boundary -> rejected -> G-OVR regression against a previous 'applied' log
  lineage   merge_lineage_daily keeps the previous v2 rows, appends new v1 pairs (chainable by the formula, ratio NaN -> False),
            reapply_lineage_overrides: carried when already applied, re-applied (drop) when the v1 candidate re-creates a dropped pair,
            set_ratio on a pair that vanished -> rejected
  apply log compare_apply_logs: regression (applied -> rejected) fails, applied -> carried passes, RULE-* missing ignored
  attribution returns_diff + attribution windows: a changed cell in the refetch window / next to a price revision / after a ledger
            revision is attributed, an unrelated changed cell is not (gate FAIL)
  exposure  live-book cross: a held id with a flag 4 asOf row -> hit (warning; blocking only with --strict-exposure)
  events    events_daily_merge carries the R3-reclassed / override-patched rows, takes fresh rows for the rest
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

try:
    import pytest
except ImportError:
    pytest = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import update_daily_rc2f as dr  # noqa: E402
import rc2f_apply as ra  # noqa: E402
import rc2f_recount as rr  # noqa: E402
import update_daily as ud  # noqa: E402

T = pd.Timestamp
_WORK = None


def workdir() -> Path:
    global _WORK
    if _WORK is None:
        _WORK = Path(tempfile.mkdtemp(prefix="d2_test_"))
    return _WORK


def _ovr_frame(rows: list[dict], kind: str) -> pd.DataFrame:
    """Parsed override frame the way load_overrides yields it (x_tid, x_isEstimate, x_row, x_file)."""
    df = pd.DataFrame(rows).astype(str)
    df["x_row"] = np.arange(2, len(df) + 2)
    df["x_file"] = f"/tmp/{kind}_overrides.csv"
    for c in ra.REQUIRED_COLS[kind]:
        if c not in df.columns:
            df[c] = ""
    df["x_tid"] = [ra.parse_int(v) for v in df.tradingItemId]
    df["x_isEstimate"] = [bool(ra.parse_bool(v)) if str(v).strip() else False for v in df.isEstimate]
    return df


def _base_rows(tid: int, dates, close: float = 10.0) -> pd.DataFrame:
    d = pd.to_datetime(list(dates))
    return pd.DataFrame({"date": d, "tradingItemId": tid, "open": close, "high": close, "low": close, "close": close, "vwap": close, "volume": 100.0,
                         "source": np.int8(1), "qualityFlag": np.int8(0)})


def _write_base(df: pd.DataFrame, path: Path) -> None:
    import prices_build as pb
    t = ud.base_from_pandas(df.sort_values(["tradingItemId", "date"]).reset_index(drop=True))
    pq.write_table(t, path, compression="zstd")


# --------------------------------------------------------------------------------------
def test_price_overrides_fresh_rows_and_integrity():
    days = pd.bdate_range("2004-08-02", "2004-08-20")
    df = _base_rows(4204343, days, close=2.0)
    win = dr.price_windows(_ovr_frame([{"overrideId": "X1-2100", "caseId": "c", "tradingItemId": "4204343", "dateFrom": "2004-08-10", "dateTo": "2004-08-12", "op": "multiply",
                                        "value": "0.5", "fields": "", "evidence": "e", "source": "s", "isEstimate": "true", "owner": "o"}], "price"))
    fresh = np.zeros(len(df), dtype=bool)
    fresh[days.get_loc(T("2004-08-11"))] = True           # only 08-11 is a fresh vendor row
    alog = ra.ApplyLog()
    out, n = dr.apply_price_to_rows(df, win, fresh, alog)
    assert n == 1
    i = days.get_loc(T("2004-08-11"))
    assert out.close.iloc[i] == 1.0 and out.open.iloc[i] == 1.0 and out.qualityFlag.iloc[i] == ra.QF_PRICE_OVERRIDE_EST
    j = days.get_loc(T("2004-08-10"))
    assert out.close.iloc[j] == 2.0 and out.qualityFlag.iloc[j] == 0      # stored row (not fresh) untouched
    assert alog.rows["X1-2100"]["status"] == "applied"
    # integrity: every stored row in the window must carry 8/32 (or 4)
    w = workdir()
    df2 = df.copy()
    m = (df2.date >= T("2004-08-10")) & (df2.date <= T("2004-08-12"))
    df2.loc[m, "qualityFlag"] = ra.QF_PRICE_OVERRIDE
    _write_base(df2, w / "base_ok.parquet")
    g = dr.price_override_integrity(w / "base_ok.parquet", win, ud.read_rows_filtered)
    assert g["pass"] and g["rows_in_windows"] == 3 and g["bad_rows"] == 0
    df2.loc[df2.date == T("2004-08-11"), "qualityFlag"] = 0               # a lost override
    _write_base(df2, w / "base_bad.parquet")
    g = dr.price_override_integrity(w / "base_bad.parquet", win, ud.read_rows_filtered)
    assert not g["pass"] and g["bad_rows"] == 1 and g["bad_examples"][0]["date"] == "2004-08-11"
    # rows_in_windows mask
    m2 = dr.rows_in_windows(df.tradingItemId.to_numpy(), df.date.to_numpy(), win)
    assert m2.sum() == 3


# --------------------------------------------------------------------------------------
def _split_vendor(tid: int) -> pd.DataFrame:
    return pd.DataFrame({"tradingItemId": tid, "validFrom": pd.to_datetime(["1900-01-01", "2010-03-01", "2015-06-01"]),
                         "validTo": pd.to_datetime(["2010-03-01", "2015-06-01", None]), "factor": [4.0, 2.0, 1.0], "source": "ciqsplitcache",
                         "snapshotDate": T("2026-09-21")})


def test_split_overrides_idempotent_and_regression():
    import ledger_build as lb
    w = workdir()
    tid = 111
    days = pd.bdate_range("2009-01-01", "2016-12-31")
    # raw closes continuous through 2010-03-01 (the vendor boundary there is an artifact) and a real 2:1 step at 2015-06-01
    close = np.where(days < T("2015-06-01"), 20.0, 10.0)
    df = _base_rows(tid, days)
    df["close"] = close; df["open"] = close; df["high"] = close; df["low"] = close; df["vwap"] = close
    _write_base(df, w / "base_split.parquet")
    cal = lb.Calendar(days.to_numpy().astype("datetime64[D]"))
    ovr = _ovr_frame([{"overrideId": "X1-1", "caseId": "c", "tradingItemId": str(tid), "exDate": "2010-03-01", "action": "drop", "newFactor": "", "newExDate": "",
                       "evidence": "e", "source": "s", "isEstimate": "false", "owner": "o"},
                      {"overrideId": "X1-2", "caseId": "c", "tradingItemId": str(tid), "exDate": "2015-06-01", "action": "confirm", "newFactor": "", "newExDate": "",
                       "evidence": "e", "source": "s", "isEstimate": "false", "owner": "o"}], "split")
    alog = ra.ApplyLog()
    sp1, info1 = dr.reapply_split_overrides(_split_vendor(tid), ovr, w / "base_split.parquet", cal, alog)
    lg1 = alog.frame().set_index("overrideId")
    assert lg1.at["X1-1", "status"] == "applied" and lg1.at["X1-2", "status"] == "no_op", lg1.status.to_dict()
    assert len(sp1) == 2 and np.allclose(sorted(sp1.factor), [1.0, 2.0])          # boundary dropped: 4/2 -> merged interval factor 2
    # day 2: the vendor table is rebuilt identically -> the same application, same result (idempotent on the vendor table)
    alog2 = ra.ApplyLog()
    sp2, _ = dr.reapply_split_overrides(_split_vendor(tid), ovr, w / "base_split.parquet", cal, alog2)
    assert sp2[["tradingItemId", "validFrom", "validTo", "factor", "source"]].equals(sp1[["tradingItemId", "validFrom", "validTo", "factor", "source"]])
    cmp = dr.compare_apply_logs(alog2.frame(), alog.frame())
    assert cmp["pass"] and not cmp["status_changed"]
    # regression: the vendor removed the boundary itself -> drop rejected (boundary_not_found) -> G-OVR fails against the previous 'applied'
    vend = _split_vendor(tid)
    vend = vend[vend.validFrom != T("2010-03-01")].copy()
    vend.loc[vend.validTo == T("2010-03-01"), "validTo"] = T("2015-06-01")
    vend.loc[vend.validFrom == T("1900-01-01"), "factor"] = 2.0
    alog3 = ra.ApplyLog()
    sp3, _ = dr.reapply_split_overrides(vend, ovr, w / "base_split.parquet", cal, alog3)
    lg3 = alog3.frame().set_index("overrideId")
    assert lg3.at["X1-1", "status"] == "rejected"
    cmp3 = dr.compare_apply_logs(alog3.frame(), alog.frame())
    assert not cmp3["pass"] and len(cmp3["regressions"]) == 1 and cmp3["regressions"][0]["overrideId"] == "X1-1"


# --------------------------------------------------------------------------------------
def _lifecycle(rows: dict) -> pd.DataFrame:
    return pd.DataFrame({"tradingItemId": list(rows), "firstTradeDate": [pd.Timestamp(v[0]) for v in rows.values()],
                         "lastTradeDate": [pd.Timestamp(v[1]) if v[1] else pd.NaT for v in rows.values()], "status": "confirmed"})


def test_lineage_daily_merge_and_overrides():
    cal_days = pd.bdate_range("2019-01-01", "2026-12-31").to_numpy().astype("datetime64[ns]")
    lc = _lifecycle({1: ("2019-01-02", "2020-06-30"), 2: ("2020-07-01", None), 3: ("2019-01-02", "2021-03-31"), 4: ("2021-04-01", None), 5: ("2019-01-02", "2022-01-31"), 6: ("2022-02-01", None)})
    prev = pd.DataFrame([{"oldId": 1, "newId": 2, "relation": "rename", "effectiveDate": T("2020-07-01"), "ratio": 1.0, "evidence": "v2", "confidence": "high", "ratioSource": "rc2f_override:X5-1",
                          "linkClass": "simple_rename", "reviewFlag": "", "sources": "gvkeyiid|rc2f", "version": "rc2f", "chainGapTradingDays": 0, "chainable": True, "linkVerified": True}])
    v1 = pd.DataFrame([{"oldId": 1, "newId": 2, "relation": "rename", "effectiveDate": T("2020-07-01"), "ratio": np.nan, "evidence": "v1", "confidence": "medium"},
                       {"oldId": 3, "newId": 4, "relation": "relist", "effectiveDate": T("2021-04-01"), "ratio": np.nan, "evidence": "v1 new", "confidence": "medium"},
                       {"oldId": 5, "newId": 6, "relation": "rename", "effectiveDate": T("2022-02-01"), "ratio": 1.0, "evidence": "v1 dropped before", "confidence": "high"}])
    ln, info = dr.merge_lineage_daily(prev, v1, lc, cal_days)
    assert info["new_pairs"] == 2 and len(ln) == 3
    k = ln.set_index(["oldId", "newId"])
    assert k.at[(1, 2), "ratio"] == 1.0 and bool(k.at[(1, 2), "linkVerified"]) and str(k.at[(1, 2), "ratioSource"]) == "rc2f_override:X5-1"   # carried, override kept
    assert not bool(k.at[(3, 4), "chainable"]) and str(k.at[(3, 4), "version"]) == "v1-daily"       # ratio NaN -> first row NaN (not chainable)
    assert bool(k.at[(5, 6), "chainable"]) and int(k.at[(5, 6), "chainGapTradingDays"]) == 0
    ovr = _ovr_frame([{"overrideId": "X5-1", "caseId": "c", "tradingItemId": "1", "oldId": "1", "newId": "2", "action": "set_ratio", "ratio": "1.0", "effectiveDate": "", "relation": "", "chainable": "",
                       "evidence": "e", "source": "s", "isEstimate": "false", "owner": "o"},
                      {"overrideId": "X5-2", "caseId": "c", "tradingItemId": "5", "oldId": "5", "newId": "6", "action": "drop", "ratio": "", "effectiveDate": "", "relation": "", "chainable": "",
                       "evidence": "e", "source": "s", "isEstimate": "false", "owner": "o"},
                      {"overrideId": "X5-3", "caseId": "c", "tradingItemId": "7", "oldId": "7", "newId": "8", "action": "set_ratio", "ratio": "2.0", "effectiveDate": "", "relation": "", "chainable": "",
                       "evidence": "e", "source": "s", "isEstimate": "false", "owner": "o"}], "lineage")
    alog = ra.ApplyLog()
    ln2 = dr.reapply_lineage_overrides(ovr, ln, lc.set_index("tradingItemId"), cal_days, alog)
    lg = alog.frame().set_index("overrideId")
    assert lg.at["X5-1", "status"] == "carried"
    assert lg.at["X5-2", "status"] == "applied" and not ((ln2.oldId == 5) & (ln2.newId == 6)).any()       # v1 re-created the dropped pair -> dropped again
    assert lg.at["X5-3", "status"] == "rejected"
    # second day: the drop is now carried (pair absent), nothing else changes
    alog2 = ra.ApplyLog()
    ln3 = dr.reapply_lineage_overrides(ovr, ln2, lc.set_index("tradingItemId"), cal_days, alog2)
    assert alog2.frame().set_index("overrideId").at["X5-2", "status"] == "carried" and len(ln3) == len(ln2)
    cmp = dr.compare_apply_logs(alog2.frame(), alog.frame())
    assert cmp["regressions"] == [] and [r["overrideId"] for r in cmp["rejected"]] == ["X5-3"] and not cmp["pass"]   # a rejected override is never silent: gate FAIL until fixed


def test_compare_apply_logs_rules():
    prev = pd.DataFrame([{"overrideId": "A-1", "kind": "split", "action": "drop", "status": "applied", "reason": ""},
                         {"overrideId": "A-2", "kind": "price", "action": "multiply", "status": "applied", "reason": ""},
                         {"overrideId": "RULE-X4-scope:9", "kind": "rule", "action": "settlementReturnScope", "status": "applied", "reason": ""}])
    cur = pd.DataFrame([{"overrideId": "A-1", "kind": "split", "action": "drop", "status": "rejected", "reason": "reject:boundary_not_found"},
                        {"overrideId": "A-2", "kind": "price", "action": "multiply", "status": "carried", "reason": ""}])
    c = dr.compare_apply_logs(cur, prev)
    assert not c["pass"] and [r["overrideId"] for r in c["regressions"]] == ["A-1"] and c["missing_from_current"] == []
    cur2 = cur.copy(); cur2.loc[0, "status"] = "no_op"
    c2 = dr.compare_apply_logs(cur2, prev)
    assert c2["pass"] and [r["overrideId"] for r in c2["status_changed"]] == ["A-1"]


# --------------------------------------------------------------------------------------
class _Ctx:
    """Minimal update_daily context for attribute_returns_changes / exposure_daily."""

    def __init__(self, root: Path, prev_root: Path, asof, refresh_dates, args=None):
        self.out = ud.OutBuild(root)
        self.out.mkdirs()
        self.prev = ud.PrevBuild(prev_root)
        self.asof = pd.Timestamp(asof)
        self.prev_asof = self.prev.as_of()
        self.refresh_dates = [pd.Timestamp(d) for d in refresh_dates]
        self.new_ids = []
        self.aux_new_ids = []
        self.aux_ids = np.zeros(0, dtype=np.int64)
        self.ledger_stale_after = None
        self.observed = pd.Timestamp("2026-09-21")
        self.build_id = "test"
        self.read_rows = ud.read_rows_filtered

        class A:
            strict_exposure = False
            ret_abs_max = 50.0
        self.args = args or A()


def _returns(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["tradingItemId", "date", "ret", "retFlag"])
    df["date"] = pd.to_datetime(df.date)
    df["prevDate"] = df.groupby("tradingItemId").date.shift(1)
    df["retFlag"] = df.retFlag.astype(np.int8)
    return df.sort_values(["tradingItemId", "date"]).reset_index(drop=True)


def _stage(root: Path, returns: pd.DataFrame, lifecycle: pd.DataFrame, calendar_days, lineage: pd.DataFrame, ledger: pd.DataFrame | None = None, published=False) -> Path:
    if published:
        (root / "derived").mkdir(parents=True, exist_ok=True); (root / "master").mkdir(exist_ok=True); (root / "events").mkdir(exist_ok=True); (root / "factors").mkdir(exist_ok=True)
        base, ret_p, lc_p, ln_p, led_p = root / "base_data.parquet", root / "derived" / "returns_total.parquet", root / "master" / "lifecycle.parquet", root / "master" / "lineage.parquet", root / "events" / "ca_ledger.parquet"
    else:
        for d in ("T2/derived", "T1/master", "T6/events", "T2/factors"):
            (root / d).mkdir(parents=True, exist_ok=True)
        base, ret_p, lc_p, ln_p, led_p = root / "T2" / "base_data.parquet", root / "T2" / "derived" / "returns_total.parquet", root / "T1" / "master" / "lifecycle.parquet", root / "T1" / "master" / "lineage.parquet", root / "T6" / "events" / "ca_ledger.parquet"
    ids = returns.tradingItemId.unique()
    _write_base(pd.concat([_base_rows(int(t), returns[returns.tradingItemId == t].date) for t in ids]), base)
    returns.to_parquet(ret_p, index=False)
    lifecycle.to_parquet(lc_p, index=False)
    lineage.to_parquet(ln_p, index=False)
    if ledger is not None:
        ledger.to_parquet(led_p, index=False)
    cal = pd.DataFrame({"date": pd.to_datetime(calendar_days), "nClose": 100, "rollingMedian": 100.0, "isTradingDay": True})
    cal.to_parquet(root / "calendar.parquet", index=False)
    return root


def _ledger(rows: list[dict]) -> pd.DataFrame:
    base = {c: None for c in ["eventId", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "cashPerShare", "parentFactor", "childRatio", "childValueSource",
                              "childValuePerOldShare", "status", "isEstimate", "source", "payDate", "repairOf", "keeperEventId", "notes"]}
    out = []
    for r in rows:
        d = dict(base); d.update(r); out.append(d)
    df = pd.DataFrame(out)
    df["exDate"] = pd.to_datetime(df.exDate); df["payDate"] = pd.to_datetime(df.payDate)
    df["childTradingItemId"] = pd.array(df.childTradingItemId, dtype="Int64")
    for c in ("cashPerShare", "parentFactor", "childRatio", "childValuePerOldShare"):
        df[c] = pd.to_numeric(df[c]).astype(float)
    df["isEstimate"] = df.isEstimate.fillna(False).astype(bool)
    for c in ("eventId", "eventType", "legType", "childValueSource", "status", "source", "repairOf", "keeperEventId", "notes"):
        df[c] = df[c].fillna("").astype(str)
    return df


def test_returns_attribution_gate():
    w = workdir() / "attr"
    days = pd.bdate_range("2026-09-01", "2026-09-16")
    lc = _lifecycle({10: ("2020-01-02", None), 20: ("2020-01-02", None)})
    ln = pd.DataFrame(columns=dr.V2_COLS)
    # previous build (asOf 09-15): plain returns; new build (asOf 09-16): + one appended day, one cell revised on 09-08 (price revision),
    # one cell changed on 09-03 by a new ledger cash row (exDate 09-03), and one UNRELATED change on 09-02 of id 20
    def rets(asof, changes):
        rows = []
        for tid in (10, 20):
            for d in days[days <= asof]:
                rows.append((tid, d, 0.001 + changes.get((tid, d), 0.0), 1 if (tid, d) in changes and changes[(tid, d)] > 0.01 else 0))
        return _returns(rows)
    prev_r = rets(T("2026-09-15"), {})
    new_r = rets(T("2026-09-16"), {(10, T("2026-09-08")): 0.005, (10, T("2026-09-03")): 0.02, (20, T("2026-09-02")): 0.003})
    led_prev = _ledger([{"eventId": "E1", "tradingItemId": 10, "exDate": "2026-08-20", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.1, "parentFactor": 1.0, "status": "ok", "source": "ciqdividend"}])
    led_new = pd.concat([led_prev, _ledger([{"eventId": "E2", "tradingItemId": 10, "exDate": "2026-09-03", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.2, "parentFactor": 1.0, "status": "ok", "source": "ciqdividend"}])], ignore_index=True)
    prev_root = _stage(w / "prev", prev_r, lc, days, ln, led_prev, published=True)
    new_root = _stage(w / "new", new_r, lc, days, ln, led_new)
    ctx = _Ctx(new_root, prev_root, "2026-09-16", days[-5:])
    price_rev = pd.DataFrame({"tradingItemId": [10], "date": [T("2026-09-08")], "field": ["close"], "old": [10.0], "new": [10.05], "observedAt": [T("2026-09-21")], "reason": ["vendor_refetch"]})
    alog = ra.ApplyLog()
    res = dr.attribute_returns_changes(ctx, prev_root / "events" / "ca_ledger.parquet", new_root / "T6" / "events" / "ca_ledger.parquet", dr.compare_apply_logs(alog.frame(), None), alog,
                                       ln, ln, None, price_rev, None)
    # changed cells: 09-16 appended x2 (window), 09-08 (price revision), 09-03 (ledger), 09-02 id 20 (unrelated)
    assert res["cells_changed"] == 5, res
    assert res["unattributed"] == 1 and res["unattributed_examples"][0]["tradingItemId"] == 20 and not res["pass"], res
    assert res["ledger_rows_changed"] == 1
    un = pd.read_csv(new_root / "T2" / "qa" / "returns_attr_unattributed.csv")
    assert len(un) == 1 and str(un.date.iloc[0]).startswith("2026-09-02")
    # the same diff without the unrelated change passes
    new_r2 = rets(T("2026-09-16"), {(10, T("2026-09-08")): 0.005, (10, T("2026-09-03")): 0.02})
    new_r2.to_parquet(new_root / "T2" / "derived" / "returns_total.parquet", index=False)
    res2 = dr.attribute_returns_changes(ctx, prev_root / "events" / "ca_ledger.parquet", new_root / "T6" / "events" / "ca_ledger.parquet", dr.compare_apply_logs(alog.frame(), None), alog,
                                        ln, ln, None, price_rev, None)
    assert res2["pass"] and res2["cells_changed"] == 4 and set(res2["by_attribution"]) == {"new_date", "price_revision", "ledger_revision"}, res2["by_attribution"]
    assert res2["outliers"]["pass"] and res2["outliers"]["new_historical_outliers"] == 0
    # a historical cell that becomes a 10x move (raw/factor basis mix, P0-1 class) -> G-RET-OUTLIER fails even though the cell is attributed
    new_r3 = rets(T("2026-09-16"), {(10, T("2026-09-08")): 9.0, (10, T("2026-09-03")): 0.02})
    new_r3.to_parquet(new_root / "T2" / "derived" / "returns_total.parquet", index=False)
    res3 = dr.attribute_returns_changes(ctx, prev_root / "events" / "ca_ledger.parquet", new_root / "T6" / "events" / "ca_ledger.parquet", dr.compare_apply_logs(alog.frame(), None), alog,
                                        ln, ln, None, price_rev, None)
    assert res3["pass"] and not res3["outliers"]["pass"] and res3["outliers"]["new_historical_outliers"] == 1 and res3["outliers"]["examples"][0]["date"] == "2026-09-08"


def test_exposure_book_cross():
    w = workdir() / "expo"
    days = pd.bdate_range("2026-09-01", "2026-09-16")
    lc = _lifecycle({10: ("2020-01-02", None), 20: ("2020-01-02", None), 30: ("2020-01-02", "2026-09-10")})
    rows = []
    for tid in (10, 20):
        for d in days:
            rows.append((tid, d, 0.001, 4 if (tid == 20 and d == T("2026-09-16")) else 0))
    r = _returns(rows)
    ln = pd.DataFrame(columns=dr.V2_COLS)
    root = _stage(w / "new", r, lc, days, ln)
    prev_root = _stage(w / "prev", r, lc, days, ln, published=True)
    pd.DataFrame({"tradingItemId": [10, 20, 30], "symbol": ["AAA", "BBB", "CCC"]}).to_parquet(root / "T1" / "master" / "current.parquet", index=False)
    book = pd.DataFrame({10: [100.0, 100.0], 20: [0.0, -50.0], 30: [10.0, 10.0]}, index=pd.to_datetime(["2026-09-15", "2026-09-16"]))
    (w / "book").mkdir(exist_ok=True)
    book.to_parquet(w / "book" / "20260916_pos_values.parquet")
    ev_cols = ["tradingItemId", "endDate", "settlementDate", "actionCode", "settlementReturn", "successorTradingItemId", "cacheUnitStatus", "endDateSource"]
    ev_prev = pd.DataFrame([[30, T("2026-09-10"), T("2026-09-11"), "OTH", np.nan, pd.NA, "no_terms", "local"]], columns=ev_cols)
    ev_new = pd.concat([ev_prev, pd.DataFrame([[10, T("2026-09-16"), T("2026-09-17"), "OTH", np.nan, pd.NA, "no_terms", "local"]], columns=ev_cols)], ignore_index=True)
    ctx = _Ctx(root, prev_root, "2026-09-16", days[-5:])
    res = dr.exposure_daily(ctx, ev_prev, ev_new, None, None, str(w / "book" / "*_pos_values.parquet"))
    assert res["asof_flag4"] == 1 and res["book"]["held_ids"] == 3
    kinds = sorted(res["book"]["hit_kinds"])
    assert kinds == ["asof_flag4", "held_after_lastTradeDate", "new_unconditioned_settlement"], kinds     # id 20 flag 4, id 10 new settlement w/o terms, id 30 held after its last trade
    assert res["warning"] and res["pass"] and not res["blocking"]
    assert (root / "qa" / "exposure_daily.md").exists()

    class A:
        strict_exposure = True
        ret_abs_max = 50.0
    ctx2 = _Ctx(root, prev_root, "2026-09-16", days[-5:], args=A())
    res2 = dr.exposure_daily(ctx2, ev_prev, ev_new, None, None, str(w / "book" / "*_pos_values.parquet"))
    assert not res2["pass"] and res2["blocking"]


def test_events_daily_merge_carries_patched_rows():
    cols = ["tradingItemId", "endDate", "settlementDate", "actionCode", "settlementReturn", "suggestedReturn", "cashPerShare", "successorTradingItemId", "successorRatio", "acquirerName",
            "source", "confidence", "actionText", "matchMethod", "endDateSource", "endDateConflict", "dbEndDateReason", "endDateOriginal", "endDateGapDays", "reviewFlag", "endDateBasis",
            "acquirerPriceDate", "hasCashLeg", "hasStockLeg", "cacheUnitStatus", "overrideSource", "cashPerShareCache", "successorRatioCache", "cacheUnitFactorTarget",
            "cacheUnitFactorSuccessor", "targetCacheMark", "targetCacheMarkDate", "cacheUnitFactorTargetDate", "successorCacheClose", "successorCacheCloseDate", "settlementReturnScope"]

    def row(tid, end, src="co_acthist", ovr="", end_src="db"):
        d = {c: None for c in cols}
        d.update({"tradingItemId": tid, "endDate": T(end), "settlementDate": T(end) + pd.Timedelta(days=1), "actionCode": "ACQ", "settlementReturn": 0.1, "suggestedReturn": 0.1, "cashPerShare": 1.0,
                  "successorTradingItemId": pd.NA, "successorRatio": np.nan, "source": src, "confidence": "high", "matchMethod": "x", "endDateSource": end_src, "endDateConflict": False,
                  "dbEndDateReason": "", "endDateOriginal": T(end), "endDateGapDays": 0, "reviewFlag": "", "endDateBasis": "raw", "hasCashLeg": True, "hasStockLeg": False,
                  "cacheUnitStatus": "ok", "overrideSource": ovr, "cashPerShareCache": 1.0, "successorRatioCache": np.nan, "cacheUnitFactorTarget": 1.0, "cacheUnitFactorSuccessor": np.nan,
                  "targetCacheMark": 10.0, "successorCacheClose": np.nan, "settlementReturnScope": "full"})
        return d
    prev = pd.DataFrame([row(1, "2020-01-10"), row(2, "2021-05-05", ovr="rc2f:X4-1", end_src="override"), row(3, "2019-02-02", src="reclass_ciqsplit"), row(4, "2018-01-01")])
    fresh = pd.DataFrame([row(1, "2020-01-10"), row(2, "2021-05-20"), row(3, "2019-02-02"), row(5, "2026-09-10")])      # id 2 vendor says another date; id 4 vanished; id 5 new
    ev, info = dr.events_daily_merge(fresh, prev)
    k = ev.set_index("tradingItemId")
    assert set(k.index) == {1, 2, 3, 4, 5}
    assert k.at[2, "endDate"] == T("2021-05-05") and k.at[2, "overrideSource"] == "rc2f:X4-1"       # override-patched row carried
    assert k.at[3, "source"] == "reclass_ciqsplit"                                                    # R3 reclass carried
    assert info["carried_rows"] == 2 and info["carried_ids_not_in_fresh"] == 1 and info["fresh_rows_differ_from_carried"] == 1


# --------------------------------------------------------------------------------------
def test_kept_restatement_carries_factors():
    """P0-1: kept raw -> kept intervals (+ new vendor boundary after the previous asOf rescaled onto the carried basis)."""
    import ledger_build as lb
    tid = 777
    prev = pd.DataFrame({"tradingItemId": tid, "validFrom": pd.to_datetime(["1900-01-01", "2024-05-23"]), "validTo": pd.to_datetime(["2024-05-23", None]),
                         "factor": [0.1, 1.0], "source": ["ciqsplitcache", "ciqsplitcache"], "snapshotDate": T("2026-09-15")})
    # vendor restated the whole history (x1e10 unit error) and added a 1:15 reverse split effective 2026-09-17
    vend = pd.DataFrame({"tradingItemId": tid, "validFrom": pd.to_datetime(["1900-01-01", "2024-05-23", "2026-09-17"]), "validTo": pd.to_datetime(["2024-05-23", "2026-09-17", None]),
                         "factor": [1e-11, 1e-10 / 15 * 10, 1.0], "source": "ciqsplitcache", "snapshotDate": T("2026-09-21")})
    vend.loc[1, "factor"] = 1.0 / 15.0
    cal = lb.Calendar(pd.bdate_range("2020-01-01", "2026-12-31").to_numpy().astype("datetime64[D]"))
    # day 1 (asOf 09-16): the new boundary is after the previous asOf 09-15 but also after asOf -> still appended (validFrom > prev asOf); factors of the
    # carried table are rescaled so that raw / factor stays continuous through 09-17 once the 09-17 rows arrive
    carried, vend_k, info = dr.carry_factors_for_kept(prev, vend, {tid}, T("2026-09-15"), cal, T("2026-09-21"))
    assert info["carried_ids"] == 1 and info["new_boundaries_added"] == 1, info
    c = carried.sort_values("validFrom").reset_index(drop=True)
    assert len(c) == 3 and c.validFrom.iloc[2] == T("2026-09-17")
    assert abs(c.factor.iloc[2] - 1.0) < 1e-12 and abs(c.factor.iloc[1] - 1.0 / 15.0) < 1e-12 and abs(c.factor.iloc[0] - 0.1 / 15.0) < 1e-12   # earlier intervals x pf
    assert "daily_kept_add" in str(c.source.iloc[2]) or "daily_kept_add" in str(c.source.iloc[1])
    # side rows follow the carried table
    adj = dr.side_from_intervals([tid, tid, tid], pd.to_datetime(["2020-01-02", "2025-01-02", "2026-09-17"]), carried)
    assert np.allclose(adj, [0.1 / 15.0, 1.0 / 15.0, 1.0])
    # an id without previous intervals cannot be carried
    _, _, info2 = dr.carry_factors_for_kept(prev, vend, {tid, 778}, T("2026-09-15"), cal, T("2026-09-21"))
    assert info2["ids_without_prev_intervals"] == [778]


def _ev_row(tid, end, src="co_acthist", ovr="", end_src="db", succ=pd.NA, ret=0.1):
    cols = ["tradingItemId", "endDate", "settlementDate", "actionCode", "settlementReturn", "suggestedReturn", "cashPerShare", "successorTradingItemId", "successorRatio", "acquirerName",
            "source", "confidence", "actionText", "matchMethod", "endDateSource", "endDateConflict", "dbEndDateReason", "endDateOriginal", "endDateGapDays", "reviewFlag", "endDateBasis",
            "acquirerPriceDate", "hasCashLeg", "hasStockLeg", "cacheUnitStatus", "overrideSource", "cashPerShareCache", "successorRatioCache", "cacheUnitFactorTarget",
            "cacheUnitFactorSuccessor", "targetCacheMark", "targetCacheMarkDate", "cacheUnitFactorTargetDate", "successorCacheClose", "successorCacheCloseDate", "settlementReturnScope"]
    d = {c: None for c in cols}
    d.update({"tradingItemId": tid, "endDate": T(end), "settlementDate": T(end) + pd.Timedelta(days=1), "actionCode": "ACQ", "settlementReturn": ret, "suggestedReturn": ret, "cashPerShare": 1.0,
              "successorTradingItemId": succ, "successorRatio": np.nan, "source": src, "confidence": "high", "matchMethod": "x", "endDateSource": end_src, "endDateConflict": False,
              "dbEndDateReason": "", "endDateOriginal": T(end), "endDateGapDays": 0, "reviewFlag": "", "endDateBasis": "raw", "hasCashLeg": True, "hasStockLeg": False,
              "cacheUnitStatus": "ok", "overrideSource": ovr, "cashPerShareCache": 1.0, "successorRatioCache": np.nan, "cacheUnitFactorTarget": 1.0, "cacheUnitFactorSuccessor": np.nan,
              "targetCacheMark": 10.0, "successorCacheClose": np.nan, "settlementReturnScope": "full"})
    return d


def test_events_delta_only():
    """P0-2: only delta ids (newly ended / lifecycle end changed / revived) take today's T6 row; every other T6 difference is listed, not adopted."""
    w = workdir() / "evdelta"; w.mkdir(parents=True, exist_ok=True)
    prev = pd.DataFrame([_ev_row(1, "2020-01-10"), _ev_row(2, "2021-05-05", ovr="rc2f:X4-1", end_src="override"), _ev_row(3, "2019-02-02", src="master", ret=np.nan), _ev_row(4, "2026-09-10")])
    fresh = pd.DataFrame([_ev_row(1, "2020-01-10"), _ev_row(2, "2021-05-20"), _ev_row(3, "2019-02-02", src="lineage", succ=33), _ev_row(5, "2026-09-16")])   # 3: T6 now sees a successor (P0-2 class); 5 newly ended; 4 revived
    lc_prev = pd.DataFrame({"tradingItemId": [1, 2, 3, 4, 5], "lastTradeDate": pd.to_datetime(["2020-01-10", "2021-05-05", "2019-02-02", "2026-09-10", "2026-09-15"]),
                            "status": ["confirmed", "confirmed", "confirmed", "confirmed", "active"]})
    lc_fresh = lc_prev.copy()
    lc_fresh.loc[lc_fresh.tradingItemId == 4, ["lastTradeDate", "status"]] = [T("2026-09-16"), "active"]        # revived
    lc_fresh.loc[lc_fresh.tradingItemId == 5, ["lastTradeDate", "status"]] = [T("2026-09-15"), "confirmed"]     # ended today
    d = dr.events_delta_ids(lc_prev, lc_fresh, prev, fresh, T("2026-09-15"), T("2026-09-16"), override_ids={2})
    assert d["delta"] == {4, 5} and d["new_ended"] == {5} and d["revived"] == {4}, d
    ev, info = dr.events_delta_merge(fresh, prev, d["delta"], w / "events_transitions.csv")
    k = ev.set_index("tradingItemId")
    assert set(k.index) == {1, 2, 3, 5}                                 # 4 dropped (revived), 5 adopted
    assert k.at[2, "endDate"] == T("2021-05-05") and k.at[2, "overrideSource"] == "rc2f:X4-1"       # override row carried
    assert pd.isna(k.at[3, "successorTradingItemId"]) and k.at[3, "source"] == "master"             # T6's new successor leg NOT adopted
    assert info["adopted_new"] == 1 and info["dropped"] == 1 and info["not_adopted"] == 2 and info["unconditioned_prev"] == 1 and info["unconditioned_new"] == 1
    tr = pd.read_csv(w / "events_transitions.csv")
    assert set(tr.kind) == {"adopted_new", "dropped(no T6 row today)", "not_adopted"} and tr[tr.tradingItemId == 3].fields.str.contains("successorTradingItemId").all()


def test_lineage_text_idempotent_and_first_dates():
    prev = pd.DataFrame([{"oldId": 1, "newId": 2, "relation": "merger", "effectiveDate": T("2020-07-01"), "ratio": 1.0, "evidence": "base | rc2f:X2-91: convert", "confidence": "high", "ratioSource": "rc2f_override",
                          "linkClass": "merger", "reviewFlag": "", "sources": "rc2f", "version": "rc2f", "chainGapTradingDays": pd.NA, "chainable": False, "linkVerified": False}])
    ln = prev.copy(); ln.loc[0, "evidence"] = "base | rc2f:X2-91: convert | rc2f:X2-91: convert"          # a re-applied override appended again
    ln2, n = dr.restore_unchanged_lineage_text(prev, ln)
    assert n == 1 and ln2.evidence.iloc[0] == prev.evidence.iloc[0]
    ln3 = prev.copy(); ln3.loc[0, "ratio"] = 2.0; ln3.loc[0, "evidence"] = "changed"
    ln4, n2 = dr.restore_unchanged_lineage_text(prev, ln3)
    assert n2 == 0 and ln4.evidence.iloc[0] == "changed"                                               # a value change keeps its new text
    # first dates: carried unless inputs changed
    cols = ["tradingItemId", "firstTradeDate", "firstPriceDate", "firstTradeSource", "lastTradeDate"]
    lc_prev = pd.DataFrame([[10, T("2004-09-01"), T("2004-03-23"), "lineage", T("2019-05-01")], [11, T("2010-01-04"), T("2010-01-04"), "ciq", T("2020-01-01")],
                            [12, T("2015-01-05"), T("2014-06-02"), "lineage", pd.NaT], [9, T("2000-01-03"), T("2000-01-03"), "ciq", T("2004-08-31")]], columns=cols)
    lc_new = pd.DataFrame([[10, T("2004-03-23"), T("2004-03-23"), "ciq", T("2019-05-01")], [11, T("2010-01-04"), T("2010-01-04"), "ciq", T("2020-01-01")],
                           [12, T("2014-06-02"), T("2014-06-02"), "ciq", pd.NaT], [9, T("2000-01-03"), T("2000-01-03"), "ciq", T("2004-09-30")]], columns=cols)   # 9 (old of 10) lastTradeDate changed
    lineage = pd.DataFrame([{"oldId": 9, "newId": 10, "relation": "relist"}, {"oldId": 8, "newId": 12, "relation": "rename"}])
    out, info = dr.carry_first_dates(lc_new, lc_prev, lineage, delta_ids=set(), override_ids=set(), new_ids=[])
    k = out.set_index("tradingItemId")
    assert k.at[12, "firstTradeDate"] == T("2015-01-05") and k.at[12, "firstTradeSource"] == "lineage"       # carried (inputs unchanged)
    assert k.at[10, "firstTradeDate"] == T("2004-03-23")                                                        # recomputed: old id 9 lastTradeDate changed
    assert info["carried_first_dates"] == 1 and info["recomputed_reasons"] == {"old id lastTradeDate changed": 1}


def test_double_leg_and_outliers():
    led = _ledger([{"eventId": "A", "tradingItemId": 578353660, "exDate": "2010-11-09", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 115485730, "childRatio": 0.098344, "status": "ok", "source": "manual_override"},   # moved to the link bar (P0-2) -> double
                   {"eventId": "B", "tradingItemId": 115485825, "exDate": "2010-11-10", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 115485730, "childRatio": 0.098344, "status": "ok", "source": "manual_override"},
                   {"eventId": "C", "tradingItemId": 115485825, "exDate": "2012-01-10", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 999, "childRatio": 0.5, "status": "ok", "source": "distributions"}])
    ln = pd.DataFrame([{"oldId": 578353660, "newId": 115485825, "relation": "relist"}])
    lc = pd.DataFrame({"tradingItemId": [578353660, 115485825], "lastTradeDate": pd.to_datetime(["2010-11-08", None])})
    r = dr.double_leg_check(led, ln, lc, pd.bdate_range("2010-01-01", "2013-01-01").to_numpy().astype("datetime64[ns]"))
    assert not r["pass"] and r["double_legs"] == 1 and r["examples"][0]["eventIdOld"] == "A" and r["examples"][0]["eventIdNew"] == "B"
    led2 = led[led.eventId != "B"]
    assert dr.double_leg_check(led2, ln, lc, pd.bdate_range("2010-01-01", "2013-01-01").to_numpy().astype("datetime64[ns]"))["pass"]
    led3 = led.copy(); led3.loc[led3.eventId == "A", "exDate"] = T("2010-11-10")                 # keeper + copy on the SAME date: deduplicated by the generator, not flagged
    assert dr.double_leg_check(led3, ln, lc, pd.bdate_range("2010-01-01", "2013-01-01").to_numpy().astype("datetime64[ns]"))["pass"]
    # outliers: a historical cell that becomes 10x, an |ret|>50 anywhere, a new-date 5x (reported only)
    import rc2f_recount as rr
    days = pd.bdate_range("2026-09-01", "2026-09-16")
    old = {"key": rr._keys(np.full(len(days) - 1, 1), rr._days(days[:-1].to_numpy())), "ret": np.full(len(days) - 1, 0.01), "flag": np.zeros(len(days) - 1, np.int8)}
    ret = np.full(len(days), 0.01); ret[2] = 9.0; ret[-1] = 4.5
    new = {"key": rr._keys(np.full(len(days), 1), rr._days(days.to_numpy())), "ret": ret, "flag": np.zeros(len(days), np.int8)}
    o = dr.returns_outliers(old, new, T("2026-09-15"))
    assert not o["pass"] and o["new_historical_outliers"] == 1 and o["new_date_abs_gt_max"] == 0 and o["new_date_outliers"] == 1
    ret2 = np.full(len(days), 0.01); ret2[-1] = 60.0
    o2 = dr.returns_outliers(old, {**new, "ret": ret2}, T("2026-09-15"))
    assert o2["pass"] and o2["new_date_abs_gt_max"] == 1 and o2["new_historical_outliers"] == 0       # a new-date tick-floor move is reported, not blocking
    o3 = dr.returns_outliers(old, new, T("2026-09-15"), new_ids=[1])                                     # the whole id is new: its history is not 'new historical'
    assert o3["pass"] and o3["new_historical_outliers"] == 0


def test_carry_end_dates_guard():
    cols = ["tradingItemId", "ciqLastCloseDate", "miLastCloseDate", "securityEndDate", "tradingItemStatusId", "lastTradeDate", "lastTradeSource", "status", "endReason", "eventEndDate", "evidence"]
    prev = pd.DataFrame([[1, T("2021-01-06"), T("2021-01-06"), pd.NaT, 15, T("2021-01-06"), "mi", "confirmed", "delist_other", pd.NaT, "p"],
                         [2, T("2026-09-15"), T("2026-09-15"), pd.NaT, 15, T("2026-09-15"), "ciq", "active", "active", pd.NaT, "p"],
                         [3, T("2020-05-05"), T("2020-05-05"), pd.NaT, 15, T("2020-05-05"), "ciq", "provisional", "data_gap", pd.NaT, "p"]], columns=cols)
    fresh = prev.copy()
    fresh.loc[0, ["lastTradeDate", "lastTradeSource", "endReason", "eventEndDate"]] = [T("2020-12-31"), "event", "merger", T("2020-12-31")]     # marker source differs only
    fresh.loc[1, ["ciqLastCloseDate", "miLastCloseDate", "lastTradeDate"]] = [T("2026-09-16"), T("2026-09-16"), T("2026-09-16")]                  # active: fresh
    fresh.loc[2, ["securityEndDate", "status", "endReason", "lastTradeSource"]] = [T("2020-05-06"), "confirmed", "delist_other", "ciq"]             # new marker: input changed
    out, info = dr.carry_end_dates(fresh, prev)
    k = out.set_index("tradingItemId")
    assert info["carried"] == 1 and k.at[1, "lastTradeDate"] == T("2021-01-06") and k.at[1, "endReason"] == "delist_other" and "carried" in k.at[1, "evidence"]
    assert k.at[2, "lastTradeDate"] == T("2026-09-16") and k.at[3, "status"] == "confirmed"


# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    import traceback
    sel = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f) and (sel is None or sel in n)]
    bad = 0
    for n, f in tests:
        try:
            f()
            print("OK  ", n)
        except Exception:  # noqa: BLE001
            bad += 1
            print("FAIL", n)
            traceback.print_exc()
    print(f"{len(tests) - bad}/{len(tests)} passed")
    sys.exit(1 if bad else 0)
