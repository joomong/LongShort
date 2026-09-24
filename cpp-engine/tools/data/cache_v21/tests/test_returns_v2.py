#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v2.1 rc2 R2: returns_total v2 (현금식 보유 총수익률) 합성 fixture 테스트. pytest 없이도 직접 실행 가능.

  /home/jgo/usr/venv-3.11/bin/python tests/test_returns_v2.py [--keep DIR] [-k NAME]

케이스: 단순 현금배당, 배당+분할 같은 날, 자회사 종가 있는 스핀오프, 두 사건에 걸친 결측일 갭, lineage 연결 첫 행(rework F1: 옛 id 갭 사건·비율 충돌·
복제 leg 1회, F4: 비현실 수익률 flag 4·chain gap 제외), corporate_actions 원천 제외(F2), 파일 단위 build_returns(lineage v2, stale 마커 F5) 와
returns_v2_qa.run_gates 의 합성 빌드 게이트(G-LINK 반례, 원장 공백 후보 F7, 분할비 오염, 원장 없음).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import prices_factors as pfm  # noqa: E402
import prices_returns as prm  # noqa: E402
import returns_v2_qa as qa  # noqa: E402

T = pd.Timestamp
_WORK: Path | None = None
LEDGER_COLUMNS = ["eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "cashCurrency", "fxToUSD", "parentFactor", "childTradingItemId",
                  "childRatio", "childValueSource", "childValuePerOldShare", "subscriptionPrice", "expiryDate", "exerciseRatio", "payDate", "recordDate",
                  "announcedDate", "source", "evidence", "status", "isEstimate", "repairOf", "notes"]
LEDGER_SCHEMA = pa.schema([
    ("eventId", pa.string()), ("tradingItemId", pa.int64()), ("exDate", pa.timestamp("ns")), ("eventType", pa.string()), ("legType", pa.string()),
    ("cashPerShare", pa.float64()), ("cashCurrency", pa.string()), ("fxToUSD", pa.float64()), ("parentFactor", pa.float64()), ("childTradingItemId", pa.int64()),
    ("childRatio", pa.float64()), ("childValueSource", pa.string()), ("childValuePerOldShare", pa.float64()), ("subscriptionPrice", pa.float64()),
    ("expiryDate", pa.timestamp("ns")), ("exerciseRatio", pa.float64()), ("payDate", pa.timestamp("ns")), ("recordDate", pa.timestamp("ns")),
    ("announcedDate", pa.timestamp("ns")), ("source", pa.string()), ("evidence", pa.string()), ("status", pa.string()), ("isEstimate", pa.bool_()),
    ("repairOf", pa.string()), ("notes", pa.string())])


def finalize_ledger(rows: pd.DataFrame) -> pd.DataFrame:
    """rc2 section 3-1 ledger frame from partial rows (test-local; independent of the provisional adapter)."""
    base = {"eventId": None, "cashPerShare": np.nan, "cashCurrency": "USD", "fxToUSD": 1.0, "parentFactor": 1.0, "childTradingItemId": pd.NA, "childRatio": np.nan,
            "childValueSource": "unknown", "childValuePerOldShare": np.nan, "subscriptionPrice": np.nan, "expiryDate": pd.NaT, "exerciseRatio": np.nan,
            "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT, "source": "ciqdividend", "evidence": "test", "status": "ok", "isEstimate": False,
            "repairOf": None, "notes": ""}
    recs = [{k: v for k, v in r.items() if not (isinstance(v, float) and np.isnan(v)) and v is not None} for r in rows.to_dict("records")]
    df = pd.DataFrame([{**base, **r} for r in recs], columns=LEDGER_COLUMNS)
    df["exDate"] = pd.to_datetime(df.exDate); df["tradingItemId"] = df.tradingItemId.astype("int64")
    df["childTradingItemId"] = df.childTradingItemId.astype("Int64"); df["parentFactor"] = df.parentFactor.astype(float).fillna(1.0)
    df["isEstimate"] = df.isEstimate.fillna(False).astype(bool)
    df["childValueSource"] = np.where(df.childValuePerOldShare.notna() & (df.childValueSource == "unknown"), "child_close_ex", df.childValueSource)
    df["eventId"] = df.tradingItemId.astype(str) + "_" + df.exDate.dt.strftime("%Y-%m-%d") + "_" + df.eventType + "_" + df.groupby(["tradingItemId", "exDate", "eventType"]).cumcount().astype(str)
    return df


def write_ledger(df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, schema=LEDGER_SCHEMA, preserve_index=False), out, compression="zstd")


def lineage_frame(rows: list[dict]) -> pd.DataFrame:
    base = {"ratio": np.nan, "evidence": "test", "confidence": "high", "chainGapTradingDays": 0}
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["effectiveDate"] = pd.to_datetime(df.effectiveDate)
    return df


def workdir() -> Path:
    global _WORK
    if _WORK is None:
        _WORK = Path(tempfile.mkdtemp(prefix="cache_v21_returns_v2_"))
    return _WORK


def ledger_rows(rows: list[dict]) -> pd.DataFrame:
    return finalize_ledger(pd.DataFrame(rows))


def group(ids, dates, closes, rows, links=None):
    led = prm.Ledger(ledger_rows(rows))
    df, st = prm.returns_for_group_v2(np.array(ids, dtype=np.int64), pd.to_datetime(dates).to_numpy().astype("datetime64[ns]"),
                                      np.array(closes, dtype=np.float32), led, links)
    return df, st


# --------------------------------------------------------------------------------------
# unit cases on returns_for_group_v2
# --------------------------------------------------------------------------------------
def test_plain_dividend():
    """현금배당일 ret = (P_t + D)/P_prev - 1 정확(비트), 다른 날은 종가비율과 비트 동일, retFlag 1."""
    df, st = group([1, 1, 1], ["2020-01-06", "2020-01-07", "2020-01-08"], [50.0, 49.5, 50.5],
                   [{"tradingItemId": 1, "exDate": "2020-01-07", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.75}])
    c = np.array([50.0, 49.5, 50.5], dtype=np.float32).astype(np.float64)
    assert df.ret[1] == (c[1] + 0.75) / c[0] - 1.0 and df.retFlag[1] == 1
    assert df.ret[2] == c[2] / c[1] - 1.0 and df.retFlag[2] == 0
    assert np.isnan(df.ret[0]) and pd.isna(df.prevDate[0]) and st["events_applied"] == 1
    # 반례: 벤더식 P_t/(P_prev - D) - 1 은 같은 값이 아님
    assert abs(df.ret[1] - (c[1] / (c[0] - 0.75) - 1.0)) > 1e-6


def test_dividend_and_split_same_day():
    """같은 날 현금배당 + 2:1 분할: 현금은 구주 기준(분할로 스케일되지 않음), 수량은 그 뒤 곱함."""
    rows = [{"tradingItemId": 1, "exDate": "2020-01-07", "eventType": "split", "legType": "quantity", "parentFactor": 2.0},
            {"tradingItemId": 1, "exDate": "2020-01-07", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0}]
    df, _ = group([1, 1], ["2020-01-06", "2020-01-07"], [100.0, 51.0], rows)
    assert abs(df.ret[1] - ((2.0 * 51.0 + 1.0) / 100.0 - 1.0)) < 1e-15 and df.retFlag[1] == 1
    # 반례: 원장 순서를 뒤집어도(분할 행이 먼저) 결과가 같아야 함(생성기가 value leg 를 먼저 적용)
    df2, _ = group([1, 1], ["2020-01-06", "2020-01-07"], [100.0, 51.0], rows[::-1])
    assert df2.ret[1] == df.ret[1]
    # 반례: 현금이 분할 후 주식수로 곱해지면 (2 x 51 + 2 x 1)/100 - 1 이 되어 틀림
    assert abs(df.ret[1] - ((2.0 * 51.0 + 2.0) / 100.0 - 1.0)) > 1e-6


def test_spinoff_with_child_close():
    """스핀오프: child 가치 = childValuePerOldShare(비율 x 자회사 exDate 종가), retFlag 2; 미해결 leg 는 4."""
    rows = [{"tradingItemId": 1, "exDate": "2020-01-07", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9,
             "childRatio": 0.5, "childValuePerOldShare": 0.5 * 40.0, "childValueSource": "child_close_ex"}]
    df, _ = group([1, 1], ["2020-01-06", "2020-01-07"], [100.0, 82.0], rows)
    assert abs(df.ret[1] - ((82.0 + 20.0) / 100.0 - 1.0)) < 1e-15 and df.retFlag[1] == 2
    rows_u = [dict(rows[0], childValuePerOldShare=np.nan, childValueSource="unknown", status="unresolved")]
    dfu, _ = group([1, 1], ["2020-01-06", "2020-01-07"], [100.0, 82.0], rows_u)
    assert dfu.ret[1] == 82.0 / 100.0 - 1.0 and dfu.retFlag[1] == 4
    rows_e = [dict(rows[0], childValueSource="vendor_implied", isEstimate=True, status="estimated")]
    dfe, _ = group([1, 1], ["2020-01-06", "2020-01-07"], [100.0, 82.0], rows_e)
    assert dfe.ret[1] == df.ret[1] and dfe.retFlag[1] == 3


def test_gap_spanning_two_events():
    """결측일 갭(01-07, 01-08 무행) 안의 분할(01-07)과 배당(01-08, 신주 기준)을 날짜순으로 누적."""
    rows = [{"tradingItemId": 1, "exDate": "2020-01-07", "eventType": "split", "legType": "quantity", "parentFactor": 3.0},
            {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.2}]
    df, st = group([1, 1], ["2020-01-06", "2020-01-09"], [90.0, 31.0], rows)
    assert abs(df.ret[1] - ((3.0 * 31.0 + 3.0 * 0.2) / 90.0 - 1.0)) < 1e-15 and df.retFlag[1] == 1
    assert df.prevDate[1] == T("2020-01-06") and st["events_applied"] == 2
    # 반례: 순서를 바꾸면(배당이 분할 전) 현금이 1 x 0.2 라 값이 다름
    rows_r = [dict(rows[0], exDate="2020-01-08"), dict(rows[1], exDate="2020-01-07")]
    dfr, _ = group([1, 1], ["2020-01-06", "2020-01-09"], [90.0, 31.0], rows_r)
    assert abs(dfr.ret[1] - ((3.0 * 31.0 + 0.2) / 90.0 - 1.0)) < 1e-15 and dfr.ret[1] != df.ret[1]


def linked_group(ids, dates, closes, rows, lineage_rows):
    """returns_for_group_v2 with links resolved from a temp base_data (lineage v2 rows)."""
    led = prm.Ledger(ledger_rows(rows))
    led.use_lineage(lineage_frame(lineage_rows))
    with tempfile.TemporaryDirectory() as tmp:
        bp = Path(tmp) / "base_data.parquet"
        pd.DataFrame({"tradingItemId": np.array(ids, dtype=np.int64), "date": pd.to_datetime(dates), "close": np.array(closes, dtype=np.float32)}).to_parquet(bp, index=False)
        links = prm.resolve_links(led, bp)
    df, st = prm.returns_for_group_v2(np.array(ids, dtype=np.int64), pd.to_datetime(dates).to_numpy().astype("datetime64[ns]"), np.array(closes, dtype=np.float32), led, links)
    return df, st, links


def test_lineage_linked_first_row():
    """rename 으로 연결된 새 id 첫 행: prev = 옛 id 마지막 종가, shares = lineage ratio; 연결 없으면 NaN; 새 id 첫날 사건은 신주 기준."""
    ids, dates, closes = [1, 1, 2, 2], ["2020-01-06", "2020-01-07", "2020-01-08", "2020-01-09"], [21.0, 20.0, 41.0, 42.0]
    lin = [{"oldId": 1, "newId": 2, "relation": "rename", "effectiveDate": "2020-01-08", "ratio": 0.5}]
    df, st, links = linked_group(ids, dates, closes, [], lin)
    assert abs(df.ret[2] - (0.5 * 41.0 / 20.0 - 1.0)) < 1e-15 and df.prevDate[2] == T("2020-01-07") and df.retFlag[2] == 0
    assert st["linked_rows"] == 1 and st["events_applied"] == 0 and np.isnan(df.ret[0]) and df.ret[3] == 42.0 / 41.0 - 1.0
    df_nolink, _ = group(ids, dates, closes, [], None)
    assert np.isnan(df_nolink.ret[2])
    # 연결 첫 행에 걸린 새 id 의 배당(옛 id 마지막 종가일 이후)은 신주 기준으로 적용됨: 현금 = ratio x D; 변환 ≠ 1 이면 기준 모호 → flag ≥ 3
    rows2 = [{"tradingItemId": 2, "exDate": "2020-01-08", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 1.0}]
    df2, st2, _ = linked_group(ids, dates, closes, rows2, lin)
    assert abs(df2.ret[2] - ((0.5 * 41.0 + 0.5 * 1.0) / 20.0 - 1.0)) < 1e-15 and df2.retFlag[2] == 3 and st2["events_applied"] == 1 and st2["links_newid_legs_with_conversion"] == 1
    df2b, st2b, _ = linked_group(ids, dates, [21.0, 20.0, 20.5, 21.0], rows2, [dict(lin[0], ratio=1.0)])               # 변환 1 이면 flag 1 그대로
    assert abs(df2b.ret[2] - ((20.5 + 1.0) / 20.0 - 1.0)) < 1e-15 and df2b.retFlag[2] == 1 and st2b["links_newid_legs_with_conversion"] == 0
    # 반례(rework F1): 옛 id 의 갭 사건(특별배당 3.0, 분할 0.5)은 구주 기준으로 먼저 적용 -> (0.5 x 41 + 3)/20 - 1; 수정 전 생성기는 이를 버렸다
    rows3 = [{"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "special_dividend", "legType": "cash", "cashPerShare": 3.0},
             {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "split", "legType": "quantity", "parentFactor": 0.5}]
    df3, st3, links3 = linked_group(ids, dates, closes, rows3, lin)
    assert abs(df3.ret[2] - ((0.5 * 41.0 + 3.0) / 20.0 - 1.0)) < 1e-15 and df3.retFlag[2] == 1
    assert st3["events_applied_via_link"] == 2 and st3["links_ratio_conflict"] == 0 and links3.gapN.iloc[0] == 2
    assert abs(df3.ret[2] - (0.5 * 41.0 / 20.0 - 1.0)) > 0.1                       # 갭 사건을 버리면 15% 낮다
    # 반례(rework F1): lineage ratio 1.0 vs 옛 id 분할 0.5 -> 원장 곱 사용 + retFlag 4 (ratio_conflict)
    df4, st4, links4 = linked_group(ids, dates, closes, rows3, [dict(lin[0], ratio=1.0)])
    assert abs(df4.ret[2] - ((0.5 * 41.0 + 3.0) / 20.0 - 1.0)) < 1e-15 and df4.retFlag[2] == 4 and bool(links4.ratioConflict.iloc[0])
    # 옛 id·새 id 에 복제된 같은 stock leg 는 한 번만
    rows5 = [{"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 1.0, "childValuePerOldShare": 4.0},
             {"tradingItemId": 2, "exDate": "2020-01-08", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 1.0, "childValuePerOldShare": 4.0}]
    df5, st5, _ = linked_group(ids, dates, closes, rows5, lin)
    assert abs(df5.ret[2] - ((0.5 * 41.0 + 4.0) / 20.0 - 1.0)) < 1e-15 and st5["events_dedup_link"] == 1 and df5.retFlag[2] == 2
    # rework F4: 연결 첫 행 |ret| > 50% -> retFlag 4 ; chainGapTradingDays > 5 -> 연결 안 함
    df6, st6, _ = linked_group(ids, dates, [21.0, 20.0, 410.0, 420.0], [], lin)
    assert df6.retFlag[2] == 4 and st6["links_suspect_return"] == 1 and abs(df6.ret[2] - (0.5 * 410.0 / 20.0 - 1.0)) < 1e-12
    df7, st7, _ = linked_group(ids, dates, closes, [], [dict(lin[0], chainGapTradingDays=6)])
    assert np.isnan(df7.ret[2]) and st7["linked_rows"] == 0


def test_corporate_actions_source_excluded():
    """rework F2: source=corporate_actions(merger_cash/merger_stock/other PVT 정산) 행은 적용하지 않음 -> 정산일 수익률 = 가격 비율."""
    ids, dates, closes = [1, 1, 1], ["2020-01-06", "2020-01-07", "2020-01-08"], [45.95, 45.9, 45.98]
    rows = [{"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "other", "legType": "cash", "cashPerShare": 46.0, "source": "corporate_actions"},
            {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 46.0, "source": "corporate_actions"}]
    df, st = group(ids, dates, closes, rows)
    c = np.array(closes, dtype=np.float32).astype(np.float64)
    assert df.ret[2] == c[2] / c[1] - 1.0 and df.retFlag[2] == 0 and st["events_applied"] == 0
    # 반례: 같은 행이 ciqdividend 원천이면(진짜 특별배당) 적용된다 -> 수정 전 생성기는 PVT 를 이렇게 이중계산했다
    df2, _ = group(ids, dates, closes, [dict(rows[0], eventType="special_dividend", source="ciqdividend")])
    assert abs(df2.ret[2] - ((c[2] + 46.0) / c[1] - 1.0)) < 1e-15 and df2.ret[2] > 0.99


# --------------------------------------------------------------------------------------
# file-level: synthetic build -> build_returns (v2 + legacy) -> run_gates
# --------------------------------------------------------------------------------------
def make_build(root: Path) -> dict:
    """2 row group base_data(ids 1,2 | 3,9), factors, legacy returns(rc1), calendar, ledger. Returns expectations."""
    for d in ("factors", "derived", "events", "master", "universe"):
        (root / d).mkdir(parents=True, exist_ok=True)
    days = pd.bdate_range("2020-01-01", "2020-02-28")
    rows = []
    px = {1: 100.0, 2: 50.0, 3: 20.0, 9: 45.0, 5: 30.0, 6: 60.0, 7: 10.0, 8: 100.0, 11: 40.0, 12: 41.0}   # child 45 (not proportional to the vendor 0.4 x P_prev valuation)
    for tid in (1, 2, 3, 9, 5, 6, 7, 8, 11, 12):
        for i, d in enumerate(days):
            if tid == 11 and d > T("2020-02-10"):
                continue                                                # old line 11 trades through 02-10 (when-issued overlap with 12)
            if tid == 12 and d < T("2020-02-05"):
                continue                                                # new line 12 prints when-issued from 02-05
            if tid == 1 and d in (T("2020-01-15"), T("2020-01-16")):
                continue                                                # gap for id 1
            if tid == 9 and d < T("2020-01-20"):
                continue                                                # child 9 starts on the spin-off day
            if tid in (5, 7) and d > T("2020-02-03"):
                continue                                                # old lines 5 and 7 end 02-03
            if tid in (6, 8) and d < T("2020-02-04"):
                continue                                                # new lines 6 (F1: 0.5 ratio + old dividend) and 8 (F4: implausible) start 02-04
            p = px[tid] * (1 + 0.001 * i)
            if tid == 1 and d >= T("2020-01-15"):
                p = p / 2.0                                             # 2:1 split effective 01-15 (inside the gap)
            if tid == 2 and d >= T("2020-01-20"):
                p = p * 0.6                                             # spin-off day drop
            rows.append({"date": d, "tradingItemId": tid, "open": p, "high": p, "low": p, "close": np.float32(p), "vwap": p, "volume": 1000.0, "source": 1, "qualityFlag": 0})
    base = pd.DataFrame(rows).sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    for c in ("open", "high", "low", "close", "vwap"):
        base[c] = base[c].astype(np.float32)
    base["source"] = base.source.astype(np.int8); base["qualityFlag"] = base.qualityFlag.astype(np.int8)
    schema = pa.schema([("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()), ("open", pa.float32()), ("high", pa.float32()), ("low", pa.float32()),
                        ("close", pa.float32()), ("vwap", pa.float32()), ("volume", pa.float64()), ("source", pa.int8()), ("qualityFlag", pa.int8())])
    w = pq.ParquetWriter(root / "base_data.parquet", schema, compression="zstd")
    for ids in ((1, 2), (3, 5, 6, 7, 8, 9, 11, 12)):
        part = base[base.tradingItemId.isin(ids)]
        w.write_table(pa.Table.from_pandas(part, schema=schema, preserve_index=False), row_group_size=len(part))
    w.close()
    snap = T("2026-09-16")
    sc = pd.DataFrame({"tradingitemid": [1], "splitdate": ["2020-01-15"], "nextsplitdate": ["2079-06-05"], "cumulativesplitfactor": [1.0], "latestsplitfactor": [2.0]})
    split = pfm.build_intervals(pfm.split_windows(sc), [1, 2, 3, 9, 5, 6, 7, 8, 11, 12], pfm.SRC_SPLIT, snap, pre_first="pre")
    # vendor dividend factors: id 3 dividend 0.5 on 01-10 (P_prev = 20 x 1.006), id 2 spin-off valued by the vendor at 0.4 x P_prev
    p3 = float(np.float32(20.0 * (1 + 0.001 * list(days).index(T("2020-01-09")))))
    p2 = float(np.float32(50.0 * (1 + 0.001 * list(days).index(T("2020-01-17")))))
    dv = pd.DataFrame({"tradingitemid": [3, 3, 2, 2], "fromdate": ["2019-01-01", "2020-01-10", "2019-01-01", "2020-01-20"],
                       "todate": ["2020-01-09", None, "2020-01-19", None], "divadjfactor": [1 - 0.5 / p3, 1.0, 1 - 0.4, 1.0]})
    div = pfm.build_intervals(pfm.dividend_windows(dv), [1, 2, 3, 9, 5, 6, 7, 8, 11, 12], pfm.SRC_DIV, snap)
    split.to_parquet(root / "factors" / "split.parquet", index=False); div.to_parquet(root / "factors" / "dividend.parquet", index=False)
    cal = pd.DataFrame({"date": days, "nClose": 4, "rollingMedian": 4.0, "isTradingDay": True}); cal.to_parquet(root / "calendar.parquet", index=False)
    pd.DataFrame({"universe": ["Top500"], "tradingItemId": [2], "fromDate": [days[0]], "toDate": [days[-1]]}).to_parquet(root / "universe" / "membership.parquet", index=False)
    # rc1 legacy returns from the same base
    prm.build_returns(root, root / "derived" / "returns_total.parquet", legacy=True)
    child_close = float(np.float32(45.0 * (1 + 0.001 * list(days).index(T("2020-01-20")))))
    led = ledger_rows([
        {"tradingItemId": 1, "exDate": "2020-01-15", "eventType": "split", "legType": "quantity", "parentFactor": 2.0},
        {"tradingItemId": 3, "exDate": "2020-01-10", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.5},
        {"tradingItemId": 2, "exDate": "2020-01-20", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 0.5,
         "childValuePerOldShare": 0.5 * child_close, "childValueSource": "child_close_ex"},
        {"tradingItemId": 3, "exDate": "2020-02-10", "eventType": "relist", "legType": "quantity", "parentFactor": 1.0, "childTradingItemId": 9, "source": "lineage"},
        # F1: old line 5 pays a special dividend 2.0 on the new line's first day (gap event) and converts 0.5 : 1 (split row on the old id)
        {"tradingItemId": 5, "exDate": "2020-02-04", "eventType": "special_dividend", "legType": "cash", "cashPerShare": 2.0},
        {"tradingItemId": 5, "exDate": "2020-02-04", "eventType": "split", "legType": "quantity", "parentFactor": 0.5, "source": "ciqsplitcache"},
        # F2: PVT settlement cash on id 3's row 02-14 sourced from corporate_actions (must not be applied)
        {"tradingItemId": 3, "exDate": "2020-02-14", "eventType": "other", "legType": "cash", "cashPerShare": 20.0, "source": "corporate_actions"},
        {"tradingItemId": 3, "exDate": "2020-02-14", "eventType": "merger_cash", "legType": "cash", "cashPerShare": 20.0, "source": "corporate_actions"},
        {"tradingItemId": 11, "exDate": "2020-02-11", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.3},     # rc2f: old-id leg inside the WI link gap (02-10, 02-11]
        {"tradingItemId": 12, "exDate": "2020-02-06", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 9.0},     # rc2f: new-id leg during the WI overlap (not the old holder's)
    ])
    write_ledger(led, root / "events" / "ca_ledger.parquet")
    lineage_frame([{"oldId": 3, "newId": 9, "relation": "relist", "effectiveDate": "2020-01-20", "ratio": 20.0 / 45.0},           # 3 keeps trading after 9 starts: legacy = linked + overlap; rc2f = old outlives new -> not linked
                   {"oldId": 5, "newId": 6, "relation": "rename", "effectiveDate": "2020-02-04", "ratio": 0.5},                    # F1
                   {"oldId": 7, "newId": 8, "relation": "rename", "effectiveDate": "2020-02-04", "ratio": 1.0},                    # F4: 10 -> 100 = +900%
                   {"oldId": 1, "newId": 2, "relation": "relist", "effectiveDate": "2020-01-03", "ratio": np.nan, "chainGapTradingDays": 9},  # excluded
                   {"oldId": 11, "newId": 12, "relation": "rename", "effectiveDate": "2020-02-11", "ratio": 1.0}]                 # rc2f: when-issued overlap -> link at the first 12 row after 11's last trade (02-11)
                  ).to_parquet(root / "master" / "lineage.parquet", index=False)
    # rc2f: lifecycle caps the old id's last trade day for the link rule (here equal to the last print)
    pd.DataFrame({"tradingItemId": [1, 2, 3, 9, 5, 6, 7, 8, 11, 12], "lastTradeDate": [pd.NaT, pd.NaT, pd.NaT, pd.NaT, T("2020-02-03"), pd.NaT, T("2020-02-03"), pd.NaT, T("2020-02-10"), pd.NaT],
                  "firstTradeDate": [days[0]] * 10, "status": ["active"] * 10}).to_parquet(root / "master" / "lifecycle.parquet", index=False)
    return {"p3": p3, "p2": p2, "child_close": child_close, "days": days}


def test_build_returns_file_and_gates():
    """파일 단위: v2 스키마(retFlag int8), row group 보존, 행 = close notna, 게이트 전부 PASS, vendor_diff 분류가 기대와 일치."""
    root = workdir() / "build"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    exp = make_build(root)
    out = root / "derived" / "returns_total_v2.parquet"
    info = prm.build_returns(root, out, ledger=root / "events" / "ca_ledger.parquet", lineage=root / "master" / "lineage.parquet")
    pf = pq.ParquetFile(out)
    assert [str(f.type) for f in pf.schema_arrow] == ["timestamp[ns]", "int64", "double", "timestamp[ns]", "int8"]
    assert pf.num_row_groups == 2 and info["rows_out"] == pq.ParquetFile(root / "base_data.parquet").metadata.num_rows
    r = pd.read_parquet(out)
    base = pd.read_parquet(root / "base_data.parquet")
    # id 3 dividend day
    x = r[(r.tradingItemId == 3) & (r.date == T("2020-01-10"))].iloc[0]
    c_t = float(base[(base.tradingItemId == 3) & (base.date == T("2020-01-10"))].close.iloc[0]); c_p = exp["p3"]
    assert x.ret == (c_t + 0.5) / c_p - 1.0 and x.retFlag == 1
    # id 1 split inside the gap: prevDate 01-14, ret = 2 x P_t / P_prev - 1, flag 0
    x = r[(r.tradingItemId == 1) & (r.date == T("2020-01-17"))].iloc[0]
    c_t = float(base[(base.tradingItemId == 1) & (base.date == T("2020-01-17"))].close.iloc[0]); c_p = float(base[(base.tradingItemId == 1) & (base.date == T("2020-01-14"))].close.iloc[0])
    assert x.prevDate == T("2020-01-14") and abs(x.ret - (2.0 * c_t / c_p - 1.0)) < 1e-15 and x.retFlag == 0
    # id 2 spin-off: (P_t + 0.5 x child)/P_prev - 1, flag 2
    x = r[(r.tradingItemId == 2) & (r.date == T("2020-01-20"))].iloc[0]
    c_t = float(base[(base.tradingItemId == 2) & (base.date == T("2020-01-20"))].close.iloc[0])
    assert abs(x.ret - ((c_t + 0.5 * exp["child_close"]) / exp["p2"] - 1.0)) < 1e-15 and x.retFlag == 2
    # relist link 3 -> 9 (rc2f rule): 3 keeps trading through the end -> no new-id row after the old id's last trade -> NOT linked (first row NaN, old_outlives_new)
    x = r[r.tradingItemId == 9].sort_values("date").iloc[0]
    assert pd.isna(x.ret) and pd.isna(x.prevDate) and x.retFlag == 0 and info["links_resolved"] == 3 and info["links_old_outlives_new"] == 1 and info["links_after_old_last"] == 1
    # rc2f when-issued overlap 11 -> 12: link row = first 12 row after 11's last trade (02-10) = 02-11; prev = 11's 02-10 close; 11's 02-11 dividend
    # (old-share leg in the gap) applied; 12's 02-06 dividend belongs to the WI holders (own row, not the link); 12's first row (02-05) NaN
    g12 = r[r.tradingItemId == 12].sort_values("date")
    c11 = float(base[(base.tradingItemId == 11) & (base.date == T("2020-02-10"))].close.iloc[0]); c12 = float(base[(base.tradingItemId == 12) & (base.date == T("2020-02-11"))].close.iloc[0])
    x = g12[g12.date == T("2020-02-11")].iloc[0]
    assert x.prevDate == T("2020-02-10") and abs(x.ret - ((c12 + 0.3) / c11 - 1.0)) < 1e-15 and x.retFlag == 1
    assert pd.isna(g12.ret.iloc[0]) and g12.date.iloc[0] == T("2020-02-05")
    x6 = g12[g12.date == T("2020-02-06")].iloc[0]; c12a = float(base[(base.tradingItemId == 12) & (base.date == T("2020-02-05"))].close.iloc[0]); c12b = float(base[(base.tradingItemId == 12) & (base.date == T("2020-02-06"))].close.iloc[0])
    assert x6.prevDate == T("2020-02-05") and abs(x6.ret - ((c12b + 9.0) / c12a - 1.0)) < 1e-15 and x6.retFlag == 1
    x10 = g12[g12.date == T("2020-02-10")].iloc[0]
    assert x10.prevDate == T("2020-02-07") and x10.retFlag == 0
    # legacy link rule (--legacy-links): rc2 behaviour — 9 linked at its first row with the overlap flag 3, 12 linked at its first row 02-05
    out_l = root / "derived" / "returns_total_v2_legacylinks.parquet"
    info_l = prm.build_returns(root, out_l, ledger=root / "events" / "ca_ledger.parquet", lineage=root / "master" / "lineage.parquet", legacy_links=True)
    rl = pd.read_parquet(out_l)
    x = rl[rl.tradingItemId == 9].sort_values("date").iloc[0]
    c9 = float(base[(base.tradingItemId == 9) & (base.date == T("2020-01-20"))].close.iloc[0])
    c3 = float(base[(base.tradingItemId == 3) & (base.date == T("2020-01-17"))].close.iloc[0])
    assert x.prevDate == T("2020-01-17") and abs(x.ret - ((20.0 / 45.0) * c9 / c3 - 1.0)) < 1e-15 and x.retFlag == 3 and info_l["links_resolved"] == 4
    xl = rl[rl.tradingItemId == 12].sort_values("date").iloc[0]
    assert xl.prevDate == T("2020-02-04") and xl.retFlag == 3 and info_l["link_rule"].startswith("legacy")
    same_rows = rl.merge(r, on=["tradingItemId", "date"], suffixes=("_l", "_n"))
    diff_rows = same_rows[~((same_rows.ret_l == same_rows.ret_n) | (same_rows.ret_l.isna() & same_rows.ret_n.isna())) | (same_rows.retFlag_l != same_rows.retFlag_n)]
    assert set(zip(diff_rows.tradingItemId, diff_rows.date.dt.strftime("%Y-%m-%d"))) == {(9, "2020-01-20"), (12, "2020-02-05"), (12, "2020-02-11")}, diff_rows
    # F1: new line 6 first row = (0.5 x close6 + 2.0) / close5_prev - 1 (old dividend on the old share, conversion 0.5 = lineage ratio), flag 1
    x = r[r.tradingItemId == 6].sort_values("date").iloc[0]
    c6 = float(base[(base.tradingItemId == 6) & (base.date == T("2020-02-04"))].close.iloc[0]); c5 = float(base[(base.tradingItemId == 5) & (base.date == T("2020-02-03"))].close.iloc[0])
    assert x.prevDate == T("2020-02-03") and abs(x.ret - ((0.5 * c6 + 2.0) / c5 - 1.0)) < 1e-15 and x.retFlag == 1 and info["events"]["events_applied_via_link"] == 3
    # F4: new line 8 first row +900% -> retFlag 4
    x = r[r.tradingItemId == 8].sort_values("date").iloc[0]
    assert x.retFlag == 4 and x.ret > 8.0 and info["events"]["links_suspect_return"] == 1 and info["links_chain_gap_excluded"] == 1
    # F2: id 3's 02-14 row is the plain price return (PVT cash not applied)
    x = r[(r.tradingItemId == 3) & (r.date == T("2020-02-14"))].iloc[0]
    c_t = float(base[(base.tradingItemId == 3) & (base.date == T("2020-02-14"))].close.iloc[0]); c_p = float(base[(base.tradingItemId == 3) & (base.date == T("2020-02-13"))].close.iloc[0])
    assert x.ret == c_t / c_p - 1.0 and x.retFlag == 0 and info["ledger_rows_excluded_source_corporate_actions"] == 1   # the `other` PVT row (merger_cash is outside APPLY_TYPES anyway)
    assert info["ledger_sha256"] and info["lineage_sha256"]
    assert info["retFlag"] == {"price_only": int((r.retFlag == 0).sum()), "cash": 4, "distribution": 1, "estimate": 0, "unresolved": 1}
    # gates on the synthetic build (lineage passed as in the generator)
    G = qa.run_gates(root, out, root / "events" / "ca_ledger.parquet", root / "qa", rc1=root / "derived" / "returns_total.parquet", lineage=root / "master" / "lineage.parquet")
    assert G["G-CASH"]["pass"] and G["G-CASH"]["mismatch"] == 0 and G["G-SPLIT"]["pass"] and G["G-SPLIT"]["rows_checked"] == 1
    assert G["G-NAN"]["pass"] and G["G-NAN"]["nan_rows"] == 8          # ids 1, 2, 3, 5, 7, 11 first rows + 9 (old outlives new) + 12 (WI first row); 6, 8 linked at first rows, 12 at 02-11
    assert G["G-LINK"]["pass"] and G["G-LINK"]["links_recomputed"] == 3 and G["G-LINK"]["with_old_gap_events"] == 2 and G["G-LINK"]["suspect"] == {"": 2, "link_implausible_return": 1}
    lk = pd.read_csv(root / "qa" / "lineage_links.csv")
    assert set(lk.state) == {"linked", "chain_gap>5", "unresolvable_old_outlives_new"} and (lk[lk.state == "linked"].match).all()
    assert lk[lk.newId == 12].linkMode.iloc[0] == "after_old_last" and str(lk[lk.newId == 12].linkDate.iloc[0])[:10] == "2020-02-11"
    # legacy gate run must also pass and reproduce the rc2 link set (9 linked with overlap)
    Gl0 = qa.run_gates(root, out_l, root / "events" / "ca_ledger.parquet", root / "qa_legacy", rc1=root / "derived" / "returns_total.parquet", lineage=root / "master" / "lineage.parquet", legacy_links=True)
    assert Gl0["G-LINK"]["pass"] and Gl0["G-LINK"]["links_recomputed"] == 4 and Gl0["G-LINK"]["suspect"] == {"": 1, "overlap": 2, "link_implausible_return": 1}
    assert G["G-VENDOR"]["unexplained"] == 0
    # 반례(rework F3): 연결 첫 행 값을 오염시키면 G-LINK 가 잡는다 (다른 게이트는 그 행을 보지 않음)
    bad_v2 = root / "derived" / "returns_total_v2_badlink.parquet"
    rb = r.copy(); i6 = rb.index[(rb.tradingItemId == 6)][0]; rb.loc[i6, "ret"] = rb.loc[i6, "ret"] + 0.01
    wb = pq.ParquetWriter(bad_v2, pf.schema_arrow, compression="zstd")
    for ids_ in ((1, 2), (3, 5, 6, 7, 8, 9, 11, 12)):
        part = rb[rb.tradingItemId.isin(ids_)]
        wb.write_table(pa.Table.from_pandas(part, schema=pf.schema_arrow, preserve_index=False), row_group_size=len(part))
    wb.close()
    Gl = qa.run_gates(root, bad_v2, root / "events" / "ca_ledger.parquet", root / "qa_badlink", rc1=root / "derived" / "returns_total.parquet", lineage=root / "master" / "lineage.parquet")
    assert not Gl["G-LINK"]["pass"] and Gl["G-LINK"]["value_or_flag_mismatch"] == 1 and (root / "qa_badlink" / "glink_mismatch.csv").exists()
    vd = pd.read_parquet(root / "qa" / "vendor_diff.parquet")
    cls = dict(zip(zip(vd.tradingItemId, vd.date.dt.strftime("%Y-%m-%d")), vd["class"]))
    assert cls == {(3, "2020-01-10"): "cash_formula", (2, "2020-01-20"): "distribution_formula", (12, "2020-02-06"): "repaired_factor", (12, "2020-02-11"): "link_row"}, cls
    assert bool(vd[vd.tradingItemId == 2].Top500.iloc[0]) and not bool(vd[vd.tradingItemId == 3].Top500.iloc[0])
    # 반례 1: 원장에서 배당 행을 빼면 vendor step 만 남아 unresolved 로 분류(unexplained 아님), G-CASH 는 여전히 통과(사건 없는 날 항등)
    led = pd.read_parquet(root / "events" / "ca_ledger.parquet")
    bad = led[led.eventType != "cash_dividend"]
    write_ledger(bad, root / "events" / "ca_ledger_bad.parquet")
    out_b = root / "derived" / "returns_total_v2_bad.parquet"
    prm.build_returns(root, out_b, ledger=root / "events" / "ca_ledger_bad.parquet", lineage=root / "master" / "lineage.parquet")
    Gb = qa.run_gates(root, out_b, root / "events" / "ca_ledger_bad.parquet", root / "qa_bad", rc1=root / "derived" / "returns_total.parquet", lineage=root / "master" / "lineage.parquet")
    assert (root / "qa_bad" / "ledger_gap_candidates.csv").exists()          # rework F7: the removed dividend shows up as a ledger gap candidate
    vdb = pd.read_parquet(root / "qa_bad" / "vendor_diff.parquet")
    assert Gb["G-CASH"]["pass"] and Gb["G-VENDOR"]["unexplained"] == 0
    assert vdb[(vdb.tradingItemId == 3)]["class"].iloc[0] == "unresolved" and "vendor dividend step without any ledger event" in vdb[(vdb.tradingItemId == 3)].reason.iloc[0]
    # 반례 2: 분할비가 틀린 원장(3.0) -> G-SPLIT 실패 + 목록
    bad2 = led.copy(); bad2.loc[bad2.eventType == "split", "parentFactor"] = 3.0
    write_ledger(bad2, root / "events" / "ca_ledger_bad2.parquet")
    out_b2 = root / "derived" / "returns_total_v2_bad2.parquet"
    prm.build_returns(root, out_b2, ledger=root / "events" / "ca_ledger_bad2.parquet", lineage=root / "master" / "lineage.parquet")
    Gb2 = qa.run_gates(root, out_b2, root / "events" / "ca_ledger_bad2.parquet", root / "qa_bad2", rc1=root / "derived" / "returns_total.parquet", lineage=root / "master" / "lineage.parquet")
    assert not Gb2["G-SPLIT"]["pass"] and (root / "qa_bad2" / "gsplit_mismatch.csv").exists()
    # 반례 3: 원장 없이 v2 호출은 명시적 오류(조용한 fallback 없음), legacy 는 4 컬럼
    try:
        prm.build_returns(root, root / "derived" / "x.parquet", ledger=root / "events" / "missing.parquet")
        raise AssertionError("missing ledger accepted")
    except FileNotFoundError:
        pass
    assert pq.ParquetFile(root / "derived" / "returns_total.parquet").schema_arrow.names == ["date", "tradingItemId", "ret", "prevDate"]
    # rework F5: stale ledger marker -> rows after the date retFlag 4, info records it
    out_s = root / "derived" / "returns_total_v2_stale.parquet"
    info_s = prm.build_returns(root, out_s, ledger=root / "events" / "ca_ledger.parquet", lineage=root / "master" / "lineage.parquet", stale_after="2020-02-14")
    rs = pd.read_parquet(out_s)
    assert info_s["stale_after"] == "2020-02-14" and (rs[rs.date > T("2020-02-14")].retFlag == 4).all() and (rs[rs.date <= T("2020-02-14")].retFlag == r[r.date <= T("2020-02-14")].retFlag.to_numpy()).all()
    assert info_s["events"]["rows_stale_flagged"] == int((rs.date > T("2020-02-14")).sum()) > 0


def test_legacy_matches_rc1_formula():
    """--legacy: adj = close/sf x df 의 rc1 산식과 동일(합성 빌드의 벤더 계수로 재계산)."""
    root = workdir() / "build"
    if not (root / "base_data.parquet").exists():
        root.mkdir(parents=True, exist_ok=True); make_build(root)
    r = pd.read_parquet(root / "derived" / "returns_total.parquet")
    base = pd.read_parquet(root / "base_data.parquet")
    sf = pfm.FactorLookup(pd.read_parquet(root / "factors" / "split.parquet")); df_ = pfm.FactorLookup(pd.read_parquet(root / "factors" / "dividend.parquet"))
    for tid, g in base.groupby("tradingItemId"):
        adj = g.close.to_numpy().astype(np.float64) / sf(g.tradingItemId.to_numpy(), g.date.to_numpy()) * df_(g.tradingItemId.to_numpy(), g.date.to_numpy())
        manual = adj[1:] / adj[:-1] - 1.0
        got = r[r.tradingItemId == tid].sort_values("date").ret.to_numpy()[1:]
        assert np.allclose(got, manual, atol=1e-15, rtol=0), tid


def test_update_daily_ledger_staleness():
    """rework residual P1-a: 원장 asOf 미상(sidecar 에 asOf 없음) = stale, sidecar asOf < 빌드 asOf = stale, >= 면 통과; sidecar 파싱."""
    import update_daily as ud
    T15, T16 = T("2026-09-15"), T("2026-09-16")
    stale, after, why = ud.ledger_staleness(None, T16, T15)
    assert stale and after == T15 and "unknown" in why
    stale, after, why = ud.ledger_staleness(T15, T16, T15)
    assert stale and after == T15 and "older" in why
    assert ud.ledger_staleness(T16, T16, T15) == (False, None, "") and ud.ledger_staleness(T("2026-09-17"), T16, T15)[0] is False
    with tempfile.TemporaryDirectory() as tmp:
        led = Path(tmp) / "ca_ledger.parquet"; led.write_bytes(b"")
        side = Path(tmp) / "ca_ledger_build_info.json"
        assert ud.ledger_asof(led) is None                                            # no sidecar
        side.write_text(json.dumps({"file": {"rows": 1}, "at": "2026-09-17 10:19:02"}))
        assert ud.ledger_asof(led) is None                                            # R1's current sidecar shape: no asOf key -> unknown
        side.write_text(json.dumps({"asOf": "2026-09-16"}))
        assert ud.ledger_asof(led) == T16
        side.write_text(json.dumps({"inputs": {"asOfDate": "2026-09-15"}}))
        assert ud.ledger_asof(led) == T15


def test_stitched_copies_mirroring():
    """R1 `ca_ledger_stitched_copies.csv`: keeper leg(거래 id) 를 이어붙인 승계 id 에 복제(history_*), transition-leg 복사본은 제외,
    keeper 없는 행·이미 있는 키는 건너뜀; 복제된 leg 로 승계 id 의 과거 행 수익률이 현금식이 된다."""
    rows = ledger_rows([{"tradingItemId": 1, "exDate": "2020-01-07", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.75},
                        {"tradingItemId": 1, "exDate": "2020-01-08", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 0.5, "childValuePerOldShare": 3.0},
                        {"tradingItemId": 2, "exDate": "2020-01-07", "eventType": "cash_dividend", "legType": "cash", "cashPerShare": 0.75}])
    with tempfile.TemporaryDirectory() as tmp:
        led = Path(tmp) / "ca_ledger.parquet"; write_ledger(rows, led)
        sc = Path(tmp) / "ca_ledger_stitched_copies.csv"
        pd.DataFrame([{"tradingItemId": 2, "exDate": "2020-01-07", "legType": "cash", "childKey": -1, "cashPerShare": 0.75, "childRatio": np.nan, "keeperId": 1, "keeperEventId": rows.eventId.iloc[0], "reason": "history_appended_to_successor:1"},
                      {"tradingItemId": 2, "exDate": "2020-01-08", "legType": "stock", "childKey": 9, "cashPerShare": np.nan, "childRatio": 0.5, "keeperId": 1, "keeperEventId": rows.eventId.iloc[1], "reason": "history_appended_to_successor:1"},
                      {"tradingItemId": 3, "exDate": "2020-01-08", "legType": "stock", "childKey": 9, "cashPerShare": np.nan, "childRatio": 0.5, "keeperId": 1, "keeperEventId": rows.eventId.iloc[1], "reason": "successor_copy_of_transition_leg:kept_on_old_id_1"},
                      {"tradingItemId": 4, "exDate": "2020-01-08", "legType": "stock", "childKey": 9, "cashPerShare": np.nan, "childRatio": 0.5, "keeperId": 1, "keeperEventId": "1_missing", "reason": "history_appended_to_successor:1"}]).to_csv(sc, index=False)
        df, st = prm.load_ledger_info(led)                           # auto-detected next to the ledger
        assert st["mirrored"] == 1 and st["skipped_existing"] == 1 and st["skipped_transition_leg"] == 1 and st["skipped_keeper_missing"] == 1, st
        assert len(df) == 4 and df[df._stitched].tradingItemId.tolist() == [2] and "stitched_copy_of:" in df[df._stitched].notes.iloc[0]
        df_off, st_off = prm.load_ledger_info(led, stitched_copies=False)
        assert st_off["mirrored"] == 0 and len(df_off) == 3
        # the mirrored spin leg makes id 2's 01-08 row cash-style (flag 2), as on the trading id
        led2 = prm.Ledger(df)
        out, _ = prm.returns_for_group_v2(np.array([2, 2, 2], dtype=np.int64), pd.to_datetime(["2020-01-06", "2020-01-07", "2020-01-08"]).to_numpy().astype("datetime64[ns]"),
                                          np.array([10.0, 9.5, 7.0], dtype=np.float32), led2)
        c = np.array([10.0, 9.5, 7.0], dtype=np.float32).astype(np.float64)
        assert abs(out.ret[2] - ((c[2] + 3.0) / c[1] - 1.0)) < 1e-15 and out.retFlag[2] == 2 and out.ret[1] == (c[1] + 0.75) / c[0] - 1.0
        # ledger-native stitched copies (R1 final: source=stitched_copy + keeperEventId): applied like any event, never mirrored twice,
        # effective source for the QA cash label = keeper's source
        nat = rows.copy()
        extra = rows.iloc[[1]].copy(); extra["tradingItemId"] = 2; extra["source"] = "stitched_copy"; extra["keeperEventId"] = rows.eventId.iloc[[1]].to_numpy()
        extra["eventId"] = extra.eventId + "_sc"; extra["exportExclude"] = True
        nat["keeperEventId"] = None; nat["exportExclude"] = False
        nat = pd.concat([nat, extra], ignore_index=True)
        led_nat = Path(tmp) / "ca_ledger_native.parquet"; nat.to_parquet(led_nat, index=False)
        assert (Path(tmp) / "ca_ledger_stitched_copies.csv").exists()                            # csv still next to the ledgers: must NOT be mirrored on top
        dfn, stn = prm.load_ledger_info(led_nat)
        assert stn["in_ledger"] == 1 and stn["mirrored"] == 0 and len(dfn) == 4 and dfn[dfn._stitched]._effSource.tolist() == ["ciqdividend"]   # keeper source (test helper default)
        assert dfn[~dfn._stitched]._effSource.tolist() == dfn[~dfn._stitched].source.tolist()
        outn, _ = prm.returns_for_group_v2(np.array([2, 2, 2], dtype=np.int64), pd.to_datetime(["2020-01-06", "2020-01-07", "2020-01-08"]).to_numpy().astype("datetime64[ns]"),
                                           np.array([10.0, 9.5, 7.0], dtype=np.float32), prm.Ledger(dfn))
        assert abs(outn.ret[2] - ((c[2] + 3.0) / c[1] - 1.0)) < 1e-15 and outn.retFlag[2] == 2 and outn.ret[1] == (c[1] + 0.75) / c[0] - 1.0
        # QA summary labels a stitched cash copy by the keeper's source (ciqdividend) -> cash_formula/repaired_factor, not unexplained
        nat2 = pd.concat([nat, rows.iloc[[0]].assign(tradingItemId=3, source="stitched_copy", keeperEventId=rows.eventId.iloc[0], eventId=rows.eventId.iloc[0] + "_sc3", exportExclude=True)], ignore_index=True)
        led_nat2 = Path(tmp) / "ca_ledger_native2.parquet"; nat2.to_parquet(led_nat2, index=False)
        dfn2, _ = prm.load_ledger_info(led_nat2)
        summ = qa.ledger_interval_summary(dfn2, np.array([3]), prm._days(np.array(["2020-01-06"], dtype="datetime64[ns]")), prm._days(np.array(["2020-01-07"], dtype="datetime64[ns]")))
        assert summ.n_cash.iloc[0] == 1 and summ.n_cash_ciqdividend.iloc[0] == 1 and summ.n_stitched.iloc[0] == 1


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=None)
    ap.add_argument("-k", default=None)
    a = ap.parse_args(argv)
    global _WORK
    if a.keep:
        _WORK = Path(a.keep); _WORK.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in TESTS:
        if a.k and a.k not in t.__name__:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                t()
            rows.append((t.__name__, "PASS", ""))
        except Exception as e:  # noqa: BLE001
            rows.append((t.__name__, "FAIL", f"{type(e).__name__}: {e}"))
            traceback.print_exc()
    w = max(len(r[0]) for r in rows)
    print(f"\n{'test':{w}s}  result  note")
    for name, res, note in rows:
        print(f"{name:{w}s}  {res:6s}  {note}")
    n_fail = sum(r[1] == "FAIL" for r in rows)
    print(f"\n{len(rows) - n_fail}/{len(rows)} passed")
    if not a.keep and _WORK is not None:
        shutil.rmtree(_WORK, ignore_errors=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
