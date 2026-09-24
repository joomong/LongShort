#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v2.1 합성 fixture 생성기(계약 스키마 BRIEF §3 그대로).

종목 6개(시나리오 5개 + 재상장 쌍), 2년(2022-01-03 ~ 2023-12-29, NYSE 휴장일 근사 제거).

| id  | 심볼 | 시나리오 |
|-----|------|----------|
| 101 | SPLT | (a) 2022-07-01 2:1 분할, 2023-03-15 3:2 주식배당. split 구간: pre_first 3.0 → 1.5 → 1.0(열림) |
| 102 | DIVD | (b) 정기배당 0.50 ×4(2022-03-10/06-09/09-08/12-08) + 특별배당 3.00(ex 2023-01-02 = 휴장일). divAdjFactor 스텝 = 1 − D/P_prev |
| 103 | GAPP | (c) 2022-08-08~12 5거래일 무행 후 재개, 2023-02-01 close NaN(qualityFlag 3, 다른 필드는 있음) |
| 104 | MRGD | (d) 합병: lastTradeDate 2023-06-15, 이후 10거래일 승계사 가격 꼬리(102 raw × 0.8)가 base_data 에 남음 |
| 105 | OLDX | (f) 옛 id: 행 2022-01-03~2022-09-30, lifecycle lastTradeDate 2022-09-30 |
| 106 | NEWX | (f) 새 id: 행 전 기간(2022-09-30 이전은 105 와 동일 값 = ciq 이어붙임), lineage relist effectiveDate 2022-10-03. lifecycle firstTradeDate=2022-10-03(source lineage), firstPriceDate=2022-01-03 |
| 전부 | | (e) 계수 구간 열림(validTo NaT)·첫 구간 이전(1900-01-01 synthetic_pre_first / synthetic_no_window) |
| 104 | | (g) master/tradable_exclusions: 2022-03-01~03-31 제외(same_security_dual_listing, preferredItemId 102) |
| 전부 | | (h) universe/membership_v1rule.parquet(Top500 v1rule: 102 가 전 기간), derived/marketcap.parquet(marketCap_USD = raw close × raw sharesOutstanding, 달력 밖 행 1개 포함) |
| 전부 | | (i) master/classification_history: 103 이 2022-07-05 부터 Industrials/Building Products → Information Technology/Software 로 변경(effectiveTo 2022-07-04 = 휴장일, 포함 경계), 104 라벨 null(미분류), 105 current_only(effectiveFrom = 스냅샷일 → pit 전부 NaN), 106 은 2022-10-03 부터 관측 |

변형(variant): 'ok' | 'factor_gap'(101 split 구간에 빈틈) | 'factor_missing_prefirst'(101 pre_first 행 삭제 → 첫 구간 이전 lookup 실패)
| 'factor_overlap'(102 dividend 구간 겹침)

사용: make_fixture.py OUT_DIR [--variant ok]  또는  from make_fixture import make_fixture
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

EPOCH_1900 = pd.Timestamp("1900-01-01")
SNAPSHOT = pd.Timestamp("2026-09-16")
IDS = {101: "SPLT", 102: "DIVD", 103: "GAPP", 104: "MRGD", 105: "OLDX", 106: "NEWX"}
HOLIDAYS = pd.to_datetime([
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05",
    "2022-11-24", "2022-12-26", "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
])
SPLITS_101 = [("2022-07-01", 2.0), ("2023-03-15", 1.5)]                  # (효력일, 비율)
DIVS_102 = [("2022-03-10", 0.50), ("2022-06-09", 0.50), ("2022-09-08", 0.50), ("2022-12-08", 0.50),
            ("2023-01-02", 3.00)]                                          # 마지막은 휴장일(특별배당)
GAP_103 = ("2022-08-08", "2022-08-12")
NULL_CLOSE_103 = "2023-02-01"
LAST_TRADE_104 = "2023-06-15"
TAIL_DAYS_104 = 10
LAST_TRADE_105 = "2022-09-30"
RELIST_106 = "2022-10-03"
EXCLUSION_104 = ("2022-03-01", "2022-03-31")   # tradable_exclusions 구간(포함)
SHARES_ADJ = {101: 50e6, 102: 80e6, 103: 20e6, 104: 30e6, 105: 10e6, 106: 10e6}   # 스냅샷 기준 주수(raw = ÷ sf)
MARKETCAP_OUTSIDE_ROW = ("2022-01-01", 101)     # 달력 밖(토요일) 행 1개: 로더가 버리고 개수만 기록해야 함
CLASS_CHANGE_103 = ("2022-07-04", "2022-07-05")  # (옛 분류 effectiveTo(포함, 휴장일), 새 분류 effectiveFrom)
CLASSIFICATION = {   # tradingItemId: [(sector, industry, effectiveFrom, effectiveTo, source, historyQuality)]
    101: [("Industrials", "Machinery", "2022-01-03", None, "v1_daily_master", "observed")],
    102: [("Consumer Staples", "Food Products", "2022-01-03", None, "v1_daily_master", "observed")],
    103: [("Industrials", "Building Products", "2022-01-03", CLASS_CHANGE_103[0], "v1_daily_master", "observed"),
          ("Information Technology", "Software", CLASS_CHANGE_103[1], None, "v1_daily_master", "observed")],
    104: [(None, None, "2022-01-03", None, "db_current", "current_only")],
    105: [("Financials", "Banks", "2026-09-16", None, "db_current", "current_only")],
    106: [("Financials", "Banks", RELIST_106, None, "v1_daily_master", "observed")],
}
TOP500 = {  # (fromDate, toDate) 포함
    101: [("2022-01-03", "2023-12-29")],
    102: [("2022-06-01", "2022-12-30"), ("2023-03-01", "2023-12-29")],
    103: [("2022-01-03", "2023-12-29")],
    104: [("2022-01-03", LAST_TRADE_104)],
    105: [("2022-01-03", LAST_TRADE_105)],
    106: [(RELIST_106, "2023-12-29")],
}


def calendar() -> pd.DatetimeIndex:
    days = pd.bdate_range("2022-01-03", "2023-12-29")
    return days[~days.isin(HOLIDAYS)]


def _write(df: pd.DataFrame, path: Path, schema: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if schema:
        df = df.astype(schema)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path, compression="zstd")


def _true_paths(cal: pd.DatetimeIndex, rng: np.random.Generator) -> dict[int, pd.DataFrame]:
    """분할조정(스냅샷 기준) '진짜' 가격 경로. 열: close, open, high, low, vwap, volume(조정 주수)."""
    out = {}
    for k, tid in enumerate(sorted(IDS)):
        if tid == 106:
            continue
        n = len(cal)
        lr = rng.normal(0.0003, 0.015, n)
        close = 100.0 * (1 + 0.3 * k) * np.exp(np.cumsum(lr))
        gap_open = rng.normal(0.0, 0.004, n)
        open_ = np.r_[close[0] * (1 + gap_open[0]), close[:-1] * (1 + gap_open[1:])]
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, n)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, n)))
        vwap = lo + (hi - lo) * rng.uniform(0.3, 0.7, n)
        vol = np.exp(rng.normal(np.log(1.0e6 * (1 + k)), 0.3, n)).round()
        out[tid] = pd.DataFrame({"close": close, "open": open_, "high": hi, "low": lo, "vwap": vwap, "volume": vol},
                                index=cal)
    out[106] = out[105].copy()
    out[106].iloc[len(cal) // 2:] *= 1.0   # 전 기간 사용(새 id 는 옛 id 경로를 이어감)
    return out


def split_windows(tid: int) -> list[tuple[pd.Timestamp, pd.Timestamp | None, float, str]]:
    """(validFrom, validTo(None=열림), factor, source). 반개구간."""
    if tid != 101:
        return [(EPOCH_1900, None, 1.0, "synthetic_no_window")]
    # ciqsplitcache: splitdate..nextsplitdate, cumulativesplitfactor = 그 날부터 스냅샷까지 누적
    dates = [pd.Timestamp(d) for d, _ in SPLITS_101]
    ratios = [r for _, r in SPLITS_101]
    win = []
    for i, d in enumerate(dates):
        csf = float(np.prod(ratios[i + 1:]))  # 이 분할 이후의 분할만 남음
        to = dates[i + 1] if i + 1 < len(dates) else None
        win.append((d, to, csf, "ciqsplitcache"))
    pre = float(np.prod(ratios))              # 첫 구간 csf × latestsplitfactor
    return [(EPOCH_1900, dates[0], pre, "synthetic_pre_first")] + win


def dividend_windows(tid: int, raw_close: pd.Series, cal: pd.DatetimeIndex):
    """102 만 배당. 스텝 = 1 − D/P_prev(P_prev = ex 직전 거래일 raw 종가). 반환: (windows, dividends rows)."""
    if tid != 102:
        return [(EPOCH_1900, None, 1.0, "synthetic_no_window")], []
    ex_dates = [pd.Timestamp(d) for d, _ in DIVS_102]
    amounts = [a for _, a in DIVS_102]
    f = 1.0
    windows = [(ex_dates[-1], None, 1.0, "ciqpriceequitydivadjfactor")]
    divs = []
    for i in range(len(ex_dates) - 1, -1, -1):
        ex = ex_dates[i]
        prev_day = cal[cal < ex][-1]
        eff = cal[cal >= ex][0]
        p_prev = float(raw_close.loc[prev_day])
        step = 1.0 - amounts[i] / p_prev
        f = f * step
        start = ex_dates[i - 1] if i > 0 else cal[0]
        windows.append((start, ex, f, "ciqpriceequitydivadjfactor"))
        divs.append({"tradingItemId": tid, "exDate": ex, "effectiveDate": eff, "prevDate": prev_day,
                     "amount": amounts[i], "pPrev": p_prev, "step": step, "factorBefore": f})
    windows.append((EPOCH_1900, cal[0], f, "synthetic_pre_first"))
    windows.sort(key=lambda w: w[0])
    return windows, divs[::-1]


def factor_series(windows, dates: pd.DatetimeIndex) -> np.ndarray:
    """구간 목록을 날짜에 적용(독립 구현: 마스크 대입). 반개구간."""
    out = np.full(len(dates), np.nan)
    for vf, vt, fac, _ in windows:
        m = dates >= vf
        if vt is not None:
            m &= dates < vt
        out[m] = fac
    assert not np.isnan(out).any(), "fixture factor windows leave gaps"
    return out


def make_fixture(root, variant: str = "ok", seed: int = 20260916) -> dict:
    root = Path(root)
    rng = np.random.default_rng(seed)
    cal = calendar()
    truth = _true_paths(cal, rng)

    base_rows, split_rows, div_rows, ret_rows, div_truth, mcap_rows = [], [], [], [], [], []
    lifecycle, adj_true = [], {}
    for tid in sorted(IDS):
        tp = truth[tid]
        # 행 존재 구간
        if tid == 105:
            tp = tp.loc[:LAST_TRADE_105]
        idx = tp.index
        if tid == 103:
            idx = idx[(idx < GAP_103[0]) | (idx > GAP_103[1])]
            tp = tp.loc[idx]
        sw = split_windows(tid)
        sf = factor_series(sw, idx)
        raw = pd.DataFrame(index=idx)
        for c in ("open", "high", "low", "close", "vwap"):
            raw[c] = (tp[c].to_numpy() * sf).astype(np.float32)
        raw["volume"] = (tp["volume"].to_numpy() / sf).astype(np.float64)
        raw["source"] = np.int8(1)
        raw["qualityFlag"] = np.int8(0)
        if tid == 103:
            raw.loc[NULL_CLOSE_103, "close"] = np.nan
            raw.loc[NULL_CLOSE_103, "qualityFlag"] = np.int8(3)
        if tid == 104:
            # 마지막 거래 뒤 승계사(102) 가격 꼬리
            tail = cal[cal > LAST_TRADE_104][:TAIL_DAYS_104]
            succ = truth[102].loc[tail]
            tail_df = pd.DataFrame(index=tail)
            for c in ("open", "high", "low", "close", "vwap"):
                tail_df[c] = (succ[c].to_numpy() * 0.8).astype(np.float32)
            tail_df["volume"] = np.float64(1000.0)
            tail_df["source"] = np.int8(1)
            tail_df["qualityFlag"] = np.int8(2)
            raw = pd.concat([raw.loc[:LAST_TRADE_104], tail_df])
            idx = raw.index
            sf = factor_series(sw, idx)
        dw, divs = dividend_windows(tid, raw["close"], cal)
        div_truth += divs
        df_ = factor_series(dw, idx)
        adj_true[tid] = pd.Series(tp["close"].reindex(idx).to_numpy(), index=idx)

        b = raw.reset_index().rename(columns={"index": "date"})
        b.insert(1, "tradingItemId", np.int64(tid))
        base_rows.append(b)
        for vf, vt, fac, src in sw:
            split_rows.append({"tradingItemId": tid, "validFrom": vf, "validTo": vt, "factor": fac, "source": src,
                               "snapshotDate": SNAPSHOT})
        for vf, vt, fac, src in dw:
            div_rows.append({"tradingItemId": tid, "validFrom": vf, "validTo": vt, "factor": fac, "source": src,
                             "snapshotDate": SNAPSHOT})
        # returns_total: close notna 행, 직전 관측 행 기준
        obs = raw["close"].notna().to_numpy()
        adj = raw["close"].to_numpy().astype(np.float64)[obs] / sf[obs] * df_[obs]
        d_obs = idx[obs]
        ret = np.r_[np.nan, adj[1:] / adj[:-1] - 1.0]
        prev = np.r_[np.array([np.datetime64("NaT")], dtype="datetime64[ns]"), d_obs[:-1].values]
        ret_rows.append(pd.DataFrame({"date": d_obs, "tradingItemId": np.int64(tid), "ret": ret, "prevDate": prev}))

        # marketcap(long): close 있는 행, raw 주수 = 스냅샷 기준 주수 / sf
        shares_raw = SHARES_ADJ[tid] / sf[obs]
        mcap_rows.append(pd.DataFrame({"date": d_obs, "tradingItemId": np.int64(tid),
                                       "marketCap_USD": raw["close"].to_numpy().astype(np.float64)[obs] * shares_raw,
                                       "sharesOutstanding": shares_raw}))

        last_trade = {104: pd.Timestamp(LAST_TRADE_104), 105: pd.Timestamp(LAST_TRADE_105)}.get(tid, idx[-1])
        end_reason = {104: "merger", 105: "delist_other"}.get(tid, "active")
        first_trade = pd.Timestamp(RELIST_106) if tid == 106 else idx[0]      # 새 id: lineage 로 올린 첫 거래일
        lifecycle.append({
            "tradingItemId": tid, "firstTradeDate": first_trade, "firstPriceDate": idx[0],
            "firstTradeSource": "lineage" if tid == 106 else "ciq", "ciqLastCloseDate": raw["close"].last_valid_index(),
            "miLastCloseDate": last_trade, "securityEndDate": last_trade if end_reason != "active" else pd.NaT,
            "tradingItemStatusId": {104: 8, 105: 11}.get(tid, 15), "eventEndDate": last_trade if tid == 104 else pd.NaT,
            "lastTradeDate": last_trade, "lastTradeSource": {104: "event", 105: "mi"}.get(tid, "ciq"),
            "endReason": end_reason, "status": "active" if end_reason == "active" else "confirmed",
            "observedAt": SNAPSHOT, "evidence": "fixture",
        })

    base = pd.concat(base_rows, ignore_index=True).sort_values(["tradingItemId", "date"], kind="stable")
    base = base[["date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume", "source", "qualityFlag"]]
    _write(base, root / "base_data.parquet", {"date": "datetime64[ns]", "tradingItemId": "int64", "open": "float32",
                                              "high": "float32", "low": "float32", "close": "float32", "vwap": "float32",
                                              "volume": "float64", "source": "int8", "qualityFlag": "int8"})

    split = pd.DataFrame(split_rows)
    div = pd.DataFrame(div_rows)
    if variant == "factor_gap":       # 101 pre_first 구간을 한 달 일찍 닫아 빈틈
        m = (split.tradingItemId == 101) & (split.source == "synthetic_pre_first")
        split.loc[m, "validTo"] = pd.Timestamp("2022-06-01")
    elif variant == "factor_missing_prefirst":
        split = split[~((split.tradingItemId == 101) & (split.source == "synthetic_pre_first"))]
    elif variant == "factor_overlap":  # 102 dividend 두 번째 구간 validTo 를 하루 늘려 겹침
        m = (div.tradingItemId == 102) & (div.validFrom == pd.Timestamp(DIVS_102[0][0]))
        div.loc[m, "validTo"] = div.loc[m, "validTo"] + pd.Timedelta(days=1)
    elif variant != "ok":
        raise ValueError(variant)
    fschema = {"tradingItemId": "int64", "validFrom": "datetime64[ns]", "validTo": "datetime64[ns]", "factor": "float64",
               "source": "string", "snapshotDate": "datetime64[ns]"}
    _write(split.sort_values(["tradingItemId", "validFrom"]), root / "factors" / "split.parquet", fschema)
    _write(div.sort_values(["tradingItemId", "validFrom"]), root / "factors" / "dividend.parquet", fschema)

    rets = pd.concat(ret_rows, ignore_index=True).sort_values(["tradingItemId", "date"], kind="stable")
    _write(rets, root / "derived" / "returns_total.parquet",
           {"date": "datetime64[ns]", "tradingItemId": "int64", "ret": "float64", "prevDate": "datetime64[ns]"})

    # master
    ids = sorted(IDS)
    _write(pd.DataFrame({
        "tradingItemId": np.array(ids, dtype=np.int64), "securityId": np.array([i * 10 for i in ids], dtype=np.int64),
        "companyId": np.array([i * 100 for i in ids], dtype=np.int64), "companyName": [f"{IDS[i]} Corp" for i in ids],
        "securitySubTypeId": np.int64(1), "tiCurrencyId": np.int64(160), "inV1Rule": True, "everMainExchange": True,
        "inV1Cache": True, "inProtoV2": True, "addedOn": SNAPSHOT,
    }), root / "master" / "instruments.parquet")
    _write(pd.DataFrame({
        "tradingItemId": np.array(ids, dtype=np.int64), "symbol": [IDS[i] for i in ids], "exchangeId": np.int64(11),
        "exchangeName": "NYSE", "primaryFlag": True, "primaryFlagTi": True, "currentPrimary": True,
        "sector": "Industrials", "simpleIndustryDescription": "Machinery",
        "tradingItemStatusId": [lc["tradingItemStatusId"] for lc in lifecycle],
        "securityStartDate": pd.Timestamp("2000-01-03"), "securityEndDate": [lc["securityEndDate"] for lc in lifecycle],
        "gvkey": [f"{i:06d}" for i in ids], "iid": "01", "asOf": SNAPSHOT,
    }), root / "master" / "current.parquet")
    _write(pd.DataFrame(lifecycle), root / "master" / "lifecycle.parquet")
    cls_rows = []
    for tid, rows in CLASSIFICATION.items():
        for sec, ind, f, t, src, q in rows:
            cls_rows.append({"tradingItemId": tid, "sector": sec, "simpleIndustryDescription": ind,
                             "effectiveFrom": pd.Timestamp(f), "effectiveTo": pd.Timestamp(t) if t else pd.NaT,
                             "observedAt": pd.Timestamp(f), "source": src, "historyQuality": q})
    _write(pd.DataFrame(cls_rows), root / "master" / "classification_history.parquet",
           {"tradingItemId": "int64", "sector": "string", "simpleIndustryDescription": "string",
            "effectiveFrom": "datetime64[ns]", "effectiveTo": "datetime64[ns]", "observedAt": "datetime64[ns]",
            "source": "string", "historyQuality": "string"})
    _write(pd.DataFrame([{"tradingItemId": 104, "fromDate": pd.Timestamp(EXCLUSION_104[0]), "toDate": pd.Timestamp(EXCLUSION_104[1]),
                          "reason": "same_security_dual_listing", "preferredItemId": 102}]).astype(
        {"tradingItemId": "int64", "preferredItemId": "int64", "reason": "string"}), root / "master" / "tradable_exclusions.parquet")
    mcap = pd.concat(mcap_rows, ignore_index=True)
    mcap = pd.concat([mcap, pd.DataFrame([{"date": pd.Timestamp(MARKETCAP_OUTSIDE_ROW[0]), "tradingItemId": MARKETCAP_OUTSIDE_ROW[1],
                                           "marketCap_USD": 1.0, "sharesOutstanding": 1.0}])], ignore_index=True)
    mcap = mcap.sort_values(["tradingItemId", "date"], kind="stable")
    _write(mcap, root / "derived" / "marketcap.parquet", {"date": "datetime64[ns]", "tradingItemId": "int64",
                                                          "marketCap_USD": "float64", "sharesOutstanding": "float64"})
    _write(pd.DataFrame([{"oldId": 105, "newId": 106, "relation": "relist", "effectiveDate": pd.Timestamp(RELIST_106),
                          "ratio": 1.0, "evidence": "fixture", "confidence": "high"}]).astype({"oldId": "int64", "newId": "int64"}),
           root / "master" / "lineage.parquet")

    # universe
    mem = []
    for tid in ids:
        first = pd.Timestamp("2022-01-03")
        last = pd.Timestamp(LAST_TRADE_105) if tid == 105 else pd.Timestamp("2023-12-29")
        mem.append({"universe": "ALL", "tradingItemId": tid, "fromDate": first, "toDate": last})
        for f, t in TOP500.get(tid, []):
            mem.append({"universe": "Top500", "tradingItemId": tid, "fromDate": pd.Timestamp(f), "toDate": pd.Timestamp(t)})
    _write(pd.DataFrame(mem).astype({"tradingItemId": "int64"}), root / "universe" / "membership.parquet")
    # v1rule 변형: Top500 에서 102 만 전 기간(두 구간 → 한 구간), 나머지 동일
    mem_v1 = [m for m in mem if not (m["universe"] == "Top500" and m["tradingItemId"] == 102)]
    mem_v1.append({"universe": "Top500", "tradingItemId": 102, "fromDate": pd.Timestamp("2022-01-03"), "toDate": pd.Timestamp("2023-12-29")})
    _write(pd.DataFrame(mem_v1).astype({"tradingItemId": "int64"}), root / "universe" / "membership_v1rule.parquet")

    # truth (테스트용)
    tdir = root / "truth"
    tdir.mkdir(parents=True, exist_ok=True)
    adj_wide = pd.DataFrame(adj_true).reindex(cal)
    adj_wide.columns = [str(c) for c in adj_wide.columns]
    adj_wide.to_parquet(tdir / "adj_close_true.parquet")
    pd.DataFrame(div_truth).to_csv(tdir / "dividends.csv", index=False)
    params = {
        "variant": variant, "seed": seed, "ids": ids, "n_dates": len(cal), "first_date": str(cal[0].date()),
        "last_date": str(cal[-1].date()), "splits_101": SPLITS_101, "divs_102": DIVS_102, "gap_103": GAP_103,
        "null_close_103": NULL_CLOSE_103, "last_trade_104": LAST_TRADE_104, "tail_days_104": TAIL_DAYS_104,
        "tail_dates_104": [str(d.date()) for d in cal[cal > LAST_TRADE_104][:TAIL_DAYS_104]],
        "last_trade_105": LAST_TRADE_105, "relist_106": RELIST_106, "top500": TOP500, "exclusion_104": EXCLUSION_104,
        "shares_adj": SHARES_ADJ, "marketcap_outside_row": MARKETCAP_OUTSIDE_ROW, "class_change_103": CLASS_CHANGE_103,
        "holidays": [str(d.date()) for d in HOLIDAYS], "n_base_rows": int(len(base)), "n_return_rows": int(len(rets)),
    }
    with open(tdir / "params.json", "w") as fh:
        json.dump(params, fh, indent=1, default=str)
    return params


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--variant", default="ok")
    a = ap.parse_args(argv)
    p = make_fixture(a.out, a.variant)
    print(json.dumps({k: p[k] for k in ("variant", "ids", "n_dates", "n_base_rows", "n_return_rows")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
