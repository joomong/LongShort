#!/home/jgo/usr/venv-3.11/bin/python
"""thin_build.py 합성 단위 테스트(TASKS.md T1 A7). pytest 로도, 직접 실행(자체 러너)으로도 돈다.

  /home/jgo/usr/venv-3.11/bin/python tests/test_thin_build.py

합성 발행본 하나(10 거래일, 종목 ~25개)를 임시 폴더에 쓰고 thin_build.build 를 한 번 돌린 뒤 경우별로 검사한다.
기대값은 엔진 규칙(float32 분할조정 close → double)으로 손으로 계산한다.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import thin_build  # noqa: E402

N = float("nan")
DATES = pd.bdate_range("2021-01-04", periods=10)            # bar 0..9
D = {k: d for k, d in enumerate(DATES)}

# 종목별 raw close (bar 0..9). NaN = 시세 없음.
PX = {
    100: [10, 10, 10, 10, 9.5, 9.6, 9.7, 9.8, 9.9, 10],    # 현금배당 부모(bar 4 ex)
    110: [20, 20, 20, 20, 16, 16.5, 17, 17, 17, 17],       # 분사 부모(bar 4 ex, 자회사 111 시세 있음)
    111: [N, N, N, N, 8, 8.1, 8.2, 8.3, 8.4, 8.5],          # 분사 자회사
    120: [30, 30, 30, 30, 28, 28, 28, 28, 28, 28],          # 대기 자회사 부모(bar 4 ex)
    121: [N, N, N, N, N, N, 5, 5, 5, 5],                    # 대기 자회사(첫 시세 bar 6)
    130: [40, 40, 40, 40, 38, 38, 38, 38, 38, 38],          # value-only 부모(bar 4 ex, 자회사 패널 밖)
    140: [50, 51, 52, 53, 54, 55, 56, 57, 58, 59],          # 현금합병: lastTradeDate bar 5 → cut, 정산 bar 6
    150: [60, 60, 60, 60, 61, 62, N, N, N, N],              # 주식합병 대상(정산 bar 6)
    151: [30, 30, 30, 30, 30, 31, 32, 33, 34, 35],          # 승계사
    160: [70, 70, 70, 70, 70, 72, N, N, N, N],              # 혼합합병 대상
    170: [80, 80, 80, 80, 80, 81, N, N, N, N],              # 승계사 시세 없음 → 파일 수익률
    171: [9, 9, 9, 9, 9, 9, N, 9, 9, 9],                    # 승계사(bar 6 무시세)
    180: [90, 90, 90, 90, 90, 91, N, N, N, N],              # 조건 없음 → R=0
    190: [11, 11, 11, 11, 11, 12, N, N, N, N],              # 같은 bar 정산 승계사를 가진 대상
    191: [22, 22, 22, 22, 22, 23, N, N, N, N],              # 승계사도 bar 6 정산
    200: [12, 12, 12, 12, 12, 12, 12, 12.5, N, N],          # 종료일(bar 3) 뒤 시세 지속 → 정산 bar 8
    210: [13, 13, N, 13, 13, 13.5, N, N, N, N],             # 종료 전 동결(bar 2), 종료일 bar 4 → 정산 bar 6
    220: [14, 14, 14, 14, 14, 15, N, N, N, N],              # 정산 bar 레그(events 현금 있음: 원장 현금 보류)
    221: [15, 15, 15, 15, 15, 16, N, N, N, N],              # 정산 bar 레그(events 현금 없음: 원장 현금 기장)
    222: [N, N, N, N, N, N, 3, 3, 3, 3],                    # 정산 bar 자회사(시세 있음)
    223: [7, 7, 7, 7, 7, 7, 7.5, 7.5, 7.5, 7.5],            # 220 의 승계사(원장 leg 도 이름으로 가리킴 → 보류)
    230: [16, 16, 16, 16, 16, 16, 16, 16, 16, 16],          # 권리 부모
    231: [N, N, N, 1, 1.1, 1.2, 1.3, N, N, N],             # 권리(만료 bar 4, 다음 bar 5 이상 첫 무시세 bar 7)
    240: [100, 100, 100, 100, 50, 51, 52, 53, 54, 55],      # 순수 2:1 분할(bar 4)
    250: [17, 17, N, 18, 18, 18, 18, 18, 18, 18],           # 공백일(bar 2)
    260: [5, 5, 5, 5, 4, 4, 4, 4, 4, 4],                    # v2 가 자회사 가치를 빼먹음 → v2_diff
    261: [N, N, N, N, 2, 2, 2, 2, 2, 2],
    270: [8, 8, 8, 8, N, N, N, N, N, N],                    # endDate 당일(bar 4) 무시세 → 정산 bar 5
    280: [6, 6, 6, 6, 6, 6, N, N, N, N],                    # 승계사 281 이 bar 3 에 은퇴 → 이전 불가
    281: [3, 3, 3, N, 3, 3, 3, 3, 3, 3],                    # bar 3 정산 뒤 시세 재개(데이터 충돌)
    290: [19, 19, 19, 19, 19, 20, N, N, N, N],              # 현금합병 + 정산 bar value-only leg
    300: [21, 21, 21, 21, N, N, 22, 22, 22, 22],            # 무시세 ex-date(bar 4) 현금 leg → 재개 bar 6
    310: [25, 25, 25, 25, 24, 24, 24, 24, 24, 24],          # 대기 자회사 LedgerValue 경로(bar 4 ex)
    311: [N, N, N, N, N, N, 2.5, 2.5, 2.5, 2.5],
    320: [26, 26, 26, 26, 25, 25, 25, 25, 25, 25],          # FirstQuote + 원장 추정값(자회사는 이전에 관측됨)
    321: [4, 4, N, N, N, N, 4.4, 4.4, 4.4, 4.4],
    330: [27, 27, 27, 27, 26, 26, 26, 26, 26, 26],          # 같은 자회사 332 를 같은 bar 에 분배하는 두 부모
    331: [28, 28, 28, 28, 27, 27, 27, 27, 27, 27],
    332: [N, N, N, N, N, N, 2.2, 2.2, 2.2, 2.2],
    340: [29, 29, 29, 29, 29, 29, 29, 29, 29, 29],          # 권리 341: 만료(bar 4) 뒤 시세 지속 → 첫 무시세 bar 7
    341: [N, N, N, 1, 1, 1, 1, N, N, N],
    342: [31, 31, 31, 31, 31, 31, 31, 31, 31, 31],          # 권리 343: events 정산(bar 5)이 만료(bar 6)보다 먼저
    343: [N, N, N, 1, 1, N, N, N, N, N],
    344: [32, 32, 32, 32, 32, 32, 32, 32, 32, 32],          # 권리 345: 시세가 한 번도 없음 → mark 없음, 만료 없음
    345: [N, N, N, N, N, N, N, N, N, N],
}
SPLITS = {240: [("1900-01-01", D[4], 2.0), (D[4], None, 1.0)]}
LAST_TRADE = {140: D[5]}


def _f(i, bar):
    """엔진 패널 값: float32(raw/f) → double."""
    raw = PX[i][bar]
    f = 1.0
    for a, b, fac in SPLITS.get(i, [("1900-01-01", None, 1.0)]):
        if pd.Timestamp(a) <= D[bar] and (b is None or D[bar] < pd.Timestamp(b)):
            f = fac
    return float(np.float32(np.float64(np.float32(raw)) / f))


def _events():
    rows = [
        # id, endDate, settlementReturn, successor, cash, ratio, hasCash, hasStock
        (140, D[4], N, None, 60.0, N, True, False),
        (150, D[5], N, 151, N, 2.0, False, True),
        (160, D[5], N, 151, 50.0, 1.0, True, True),
        (170, D[5], 0.1, 171, N, 1.0, False, True),
        (180, D[5], N, None, N, N, False, False),
        (190, D[5], N, 191, N, 1.0, False, True),
        (191, D[5], N, None, N, N, False, False),
        (200, D[3], N, None, 12.0, N, True, False),
        (210, D[4], N, None, 14.0, N, True, False),
        (220, D[5], N, 223, 1.0, 2.0, True, True),
        (221, D[5], N, None, 16.0, N, True, False),
        (270, D[4], N, None, 9.0, N, True, False),
        (280, D[5], N, 281, N, 1.0, False, True),
        (281, D[2], N, None, N, N, False, False),
        (290, D[5], N, None, 21.0, N, True, False),
        (343, D[3], N, None, 0.5, N, True, False),
    ]
    df = pd.DataFrame(rows, columns=["tradingItemId", "endDate", "settlementReturn", "successorTradingItemId",
                                     "cashPerShareCache", "successorRatioCache", "hasCashLeg", "hasStockLeg"])
    df.loc[df.tradingItemId == 221, "hasCashLeg"] = False    # 221: events 현금 없음(원장 현금 기장)
    df.loc[df.tradingItemId == 221, "cashPerShareCache"] = N
    df.loc[df.tradingItemId == 221, "settlementReturn"] = N
    df["successorTradingItemId"] = df["successorTradingItemId"].astype("Int64")
    df["cacheUnitStatus"] = "ok"
    return df


def _ledger():
    def leg(pid, bar, typ, cash=N, child=None, ratio=N, src="child_close_ex", value=N, expiry=None, eid=None, cclose=N):
        return dict(tradingItemId=str(pid), exDate=D[bar], legType=typ, cashPerShareCache=cash,
                    childTradingItemId=None if child is None else str(child), childRatioCache=ratio,
                    childCloseAtExCache=cclose, childValueSource=src, childValuePerShareCache=value,
                    eventId=eid or f"{pid}_{bar}_{typ}", expiryDate=expiry)
    rows = [
        leg(100, 4, "cash", cash=0.5),
        leg(110, 4, "stock", child=111, ratio=0.5),
        leg(120, 4, "stock", child=121, ratio=1.0),
        leg(130, 4, "stock", child=999, ratio=1.0, src="event_value", value=2.0),
        leg(220, 6, "cash", cash=0.25),                                  # events 현금과 같은 bar → 보류
        leg(220, 6, "stock", child=223, ratio=2.0, eid="220_6_succ"),    # events 승계사 → 보류
        leg(220, 6, "stock", child=222, ratio=1.0, eid="220_6_child"),   # 기장 + 자회사 행
        leg(221, 6, "cash", cash=0.5),                                   # 기장
        leg(230, 3, "rights", child=231, ratio=1.0, expiry=D[4]),
        leg(240, 4, "quantity"),
        leg(260, 4, "stock", child=261, ratio=1.0),
        leg(290, 6, "stock", child=998, ratio=1.0, src="event_value", value=1.0),
        leg(300, 4, "cash", cash=0.5),
        leg(310, 4, "stock", child=311, ratio=1.0, src="wi_close", cclose=2.0),
        leg(320, 4, "stock", child=321, ratio=1.0, src="wi_close", cclose=3.0),
        leg(330, 4, "stock", child=332, ratio=1.0, src="wi_close", cclose=2.0),
        leg(331, 4, "stock", child=332, ratio=1.0, src="wi_close", cclose=2.0),
        leg(340, 3, "rights", child=341, ratio=1.0, expiry=D[4]),
        leg(342, 3, "rights", child=343, ratio=1.0, expiry=D[5]),
        leg(344, 3, "rights", child=345, ratio=1.0, expiry=D[4]),
    ]
    return pd.DataFrame(rows)


def _v2():
    """v2: 시세 있는 날의 가격 수익률(prevDate = 직전 관측). 사건일은 원장 식으로 덮어쓴다."""
    out = []
    for i in PX:
        prev = None
        for bar in range(10):
            if np.isnan(PX[i][bar]) or (i in LAST_TRADE and D[bar] > LAST_TRADE[i]):
                continue
            ret = N if prev is None else _f(i, bar) / _f(i, prev) - 1.0
            out.append((D[bar], i, ret, D[prev] if prev is not None else pd.NaT, 0))
            prev = bar
    df = pd.DataFrame(out, columns=["date", "tradingItemId", "ret", "prevDate", "retFlag"])

    def put(i, bar, val):
        df.loc[(df.tradingItemId == i) & (df.date == D[bar]), "ret"] = val
    put(100, 4, (_f(100, 4) + 0.5) / _f(100, 3) - 1.0)
    put(110, 4, (_f(110, 4) + 0.5 * _f(111, 4)) / _f(110, 3) - 1.0)
    put(130, 4, (_f(130, 4) + 2.0) / _f(130, 3) - 1.0)
    df.loc[df.tradingItemId.isin([100, 110, 130]) & (df.date == D[4]), "retFlag"] = 1
    # 260 은 자회사 가치를 빼먹은 값(불일치 → invariant_mismatch)
    return df


def make_source(root: Path) -> None:
    for sub in ("factors", "derived", "engine", "master", "universe"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    rows = []
    for i, px in PX.items():
        for bar, c in enumerate(px):
            rows.append((D[bar], i, c, c, c, c, c, 1000.0))
    base = pd.DataFrame(rows, columns=["date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume"])
    for c in ("open", "high", "low", "close", "vwap"):
        base[c] = base[c].astype(np.float32)
    base.sort_values(["tradingItemId", "date"]).to_parquet(root / "base_data.parquet", index=False)
    sp = []
    for i in PX:
        for a, b, fac in SPLITS.get(i, [("1900-01-01", None, 1.0)]):
            sp.append((i, pd.Timestamp(a), pd.Timestamp(b) if b is not None else pd.NaT, fac))
    pd.DataFrame(sp, columns=["tradingItemId", "validFrom", "validTo", "factor"]).to_parquet(root / "factors" / "split.parquet", index=False)
    _v2().to_parquet(root / "derived" / "returns_total.parquet", index=False)
    mc = base[["date", "tradingItemId"]].copy()
    mc["marketCap_USD"] = 1e6
    mc.iloc[::2].to_parquet(root / "derived" / "marketcap.parquet", index=False)
    pd.DataFrame({"date": DATES, "isTradingDay": True}).to_parquet(root / "calendar.parquet", index=False)
    lc = pd.DataFrame({"tradingItemId": list(PX), "lastTradeDate": [LAST_TRADE.get(i, pd.NaT) for i in PX]})
    lc["lastTradeDate"] = pd.to_datetime(lc["lastTradeDate"])
    lc.to_parquet(root / "master" / "lifecycle.parquet", index=False)
    pd.DataFrame({"tradingItemId": list(PX), "symbol": [f"S{i}" for i in PX]}).to_parquet(root / "master" / "current.parquet", index=False)
    pd.DataFrame({"tradingItemId": list(PX), "companyName": [f"C{i}" for i in PX]}).to_parquet(root / "master" / "instruments.parquet", index=False)
    pd.DataFrame({"universe": "ALL", "tradingItemId": list(PX), "fromDate": DATES[0], "toDate": DATES[-1]}).to_parquet(
        root / "universe" / "membership.parquet", index=False)
    _events().to_parquet(root / "engine" / "events_cache.parquet", index=False)
    _ledger().to_parquet(root / "engine" / "ca_ledger_cache.parquet", index=False)
    (root / "events").mkdir(exist_ok=True)
    raw = _ledger()[["tradingItemId", "exDate"]].astype({"tradingItemId": np.int64})
    raw["exportExclude"] = False
    raw.to_parquet(root / "events" / "ca_ledger.parquet", index=False)
    pd.DataFrame({"tradingItemId": [998, 110], "symbol": ["AUX998", "DUP"], "companyName": ["Aux 998", "dup"]}).to_parquet(
        root / "master" / "aux_instruments.parquet", index=False)
    (root / "manifest.json").write_text(json.dumps({"build_id": "synthetic"}))


_STATE: dict = {}


def built():
    if not _STATE:
        tmp = Path(tempfile.mkdtemp(prefix="thin_build_test_"))
        make_source(tmp / "src")
        thin_build.build(tmp / "src", tmp / "out", tmp / "work")
        _STATE.update(tmp=tmp, out=tmp / "out",
                      rt=pd.read_parquet(tmp / "out" / "returns_total.parquet"),
                      ret=pd.read_parquet(tmp / "out" / "returns.parquet"),
                      ev=pd.read_parquet(tmp / "out" / "events.parquet"),
                      base=pd.read_parquet(tmp / "out" / "base_data.parquet"),
                      tr=pd.read_csv(tmp / "work" / "transitional.csv", float_precision="round_trip"),
                      manifest=json.loads((tmp / "out" / "manifest.json").read_text()))
    return _STATE


def rt(i, bar):
    df = built()["rt"]
    v = df[(df.tradingItemId == i) & (df.date == D[bar])]["returns_total"]
    return None if v.empty else float(v.iloc[0])


def events_of(i):
    df = built()["ev"]
    return df[df.sourceId == i]


def trans_of(i, kind):
    df = built()["tr"]
    return df[(df.tradingItemId == i) & (df.kind == kind)]


def close(a, b, tol=0.0):
    assert a is not None and b is not None and abs(a - b) <= tol, (a, b)


# ── 경우별 테스트 ─────────────────────────────────────────────────────────────
def test_cash_dividend_no_row():
    assert events_of(100).empty
    m = _f(100, 3)
    assert rt(100, 4) == (0.0 + 0.5 / m) + (_f(100, 4) / m - 1.0)      # 엔진 순서: 현금 leg → 가격


def test_spinoff_quoted_child():
    e = events_of(110)
    assert len(e) == 1 and not e.sourceEnds.iloc[0] and e.destinationId.iloc[0] == 111 and e.date.iloc[0] == D[4]
    assert e.valueRatio.iloc[0] == 0.5 * _f(111, 4) / _f(110, 3)
    m = _f(110, 3)
    assert rt(110, 4) == (0.0 + 0.5 * _f(111, 4) / m) + (_f(110, 4) / m - 1.0)
    close(rt(110, 4), (_f(110, 4) + 0.5 * _f(111, 4)) / m - 1.0, 1e-15)


def test_pending_child():
    assert events_of(120).empty
    t = trans_of(120, "pending_child")
    assert len(t) == 1 and t.date.iloc[0] == str(D[4].date()) and "child=121" in t.detail.iloc[0]
    assert "engine_path=first_quote" in t.detail.iloc[0] and f"engine_date={D[6].date()}" in t.detail.iloc[0]
    assert f"engine_value={1.0 * _f(121, 6) / _f(120, 3)!r}" in t.detail.iloc[0] and t.value_used.iloc[0] == 0.0
    m = _f(120, 3)
    assert rt(120, 4) == (0.0 + 0.0) + (_f(120, 4) / m - 1.0)          # 추정값 없음 → 0


def test_value_only():
    assert events_of(130).empty
    assert built()["tr"].query("tradingItemId == 130").empty
    m = _f(130, 3)
    assert rt(130, 4) == (0.0 + 2.0 / m) + (_f(130, 4) / m - 1.0)


def test_cash_merger_with_lifecycle_cut():
    b = built()["base"]
    assert b[b.tradingItemId == 140].date.max() == D[5]               # lastTradeDate 이후 행 제거
    e = events_of(140)
    assert len(e) == 1 and e.sourceEnds.iloc[0] and pd.isna(e.destinationId.iloc[0]) and e.date.iloc[0] == D[6]
    assert rt(140, 6) == 0.0 + 60.0 / _f(140, 5) - 1.0


def test_stock_merger():
    m, s = _f(150, 5), _f(151, 6)
    e = events_of(150)
    assert len(e) == 1 and e.sourceEnds.iloc[0] and e.destinationId.iloc[0] == 151
    assert e.valueRatio.iloc[0] == 2.0 * s / m
    assert rt(150, 6) == 2.0 * s / m + 0.0 - 1.0


def test_mixed_merger():
    m, s = _f(160, 5), _f(151, 6)
    e = events_of(160)
    assert e.destinationId.iloc[0] == 151 and e.valueRatio.iloc[0] == 1.0 * s / m
    assert rt(160, 6) == s / m + 50.0 / m - 1.0
    close(1.0 + rt(160, 6), 50.0 / m + e.valueRatio.iloc[0], 1e-15)


def test_unquoted_successor_file_return():
    e = events_of(170)
    assert pd.isna(e.destinationId.iloc[0])
    assert rt(170, 6) == float(np.float32(0.1))
    assert len(trans_of(170, "explicit_file_return")) == 1


def test_no_terms_default_zero():
    assert rt(180, 6) == 0.0 and len(trans_of(180, "default_settlement")) == 1
    assert pd.isna(events_of(180).destinationId.iloc[0])


def test_successor_settling_same_bar():
    e = events_of(190)
    assert pd.isna(e.destinationId.iloc[0])                             # 191 도 bar 6 에 은퇴 → 이전 불가
    assert rt(190, 6) == 0.0 and len(trans_of(190, "default_settlement")) == 1


def test_quotes_after_end_date():
    e = events_of(200)
    assert e.date.iloc[0] == D[8]                                       # 종료일 bar 3, 첫 무시세 bar 8
    assert rt(200, 8) == 12.0 / _f(200, 7) - 1.0


def test_frozen_before_end():
    e = events_of(210)
    assert e.date.iloc[0] == D[6]                                       # bar 2 공백은 종료 전 동결(정산 아님)
    assert rt(210, 6) == 14.0 / _f(210, 5) - 1.0


def test_settlement_bar_legs():
    m, s = _f(220, 5), _f(223, 6)
    e = events_of(220).sort_values("eventId")
    ends = e[e.sourceEnds]
    kids = e[~e.sourceEnds]
    assert len(ends) == 1 and ends.destinationId.iloc[0] == 223 and ends.valueRatio.iloc[0] == 2.0 * s / m
    assert len(kids) == 1 and kids.destinationId.iloc[0] == 222 and kids.valueRatio.iloc[0] == 1.0 * _f(222, 6) / m
    R = 2.0 * s / m + 1.0 / m - 1.0
    assert rt(220, 6) == R + 1.0 * _f(222, 6) / m                       # 원장 현금·승계사 leg 는 보류
    assert rt(221, 6) == 0.0 + 0.5 / _f(221, 5)                         # events 현금 없음 → 원장 현금 기장(R=0 기본)


def test_rights_expiry():
    e = events_of(231)
    assert len(e) == 1 and e.sourceEnds.iloc[0] and pd.isna(e.destinationId.iloc[0]) and e.date.iloc[0] == D[7]
    assert e.eventId.iloc[0].endswith("_expiry")
    assert rt(231, 7) == -1.0
    k = events_of(230)
    assert len(k) == 1 and k.destinationId.iloc[0] == 231 and k.valueRatio.iloc[0] == 1.0 * _f(231, 3) / _f(230, 2)


def test_split_day_signal_return_zero():
    r = built()["ret"]
    v = r[(r.tradingItemId == 240) & (r.date == D[4])]["returns"]
    assert len(v) == 1 and v.iloc[0] == 0.0
    b = built()["base"]
    assert float(b[(b.tradingItemId == 240) & (b.date == D[4])].close.iloc[0]) == 50.0   # raw 유지


def test_gap_day_signal_return_absent():
    r = built()["ret"]
    days = set(r[r.tradingItemId == 250].date)
    assert D[2] not in days and D[3] not in days and D[4] in days and D[1] in days


def test_v2_diff_category():
    vd = pd.read_csv(built()["tmp"] / "work" / "v2_diff.csv")
    row = vd[(vd.tradingItemId == 260) & (vd.date == str(D[4].date()))]
    assert len(row) == 1 and row.category.iloc[0] == "other"
    fz = vd[vd.tradingItemId == 300]
    assert set(fz.category) == {"frozen_exdate_leg"} and len(fz) == 1   # 재개일은 v2 와 같다


def test_every_end_row_has_returns_total_and_manifest():
    ev = built()["ev"]
    for r in ev[ev.sourceEnds].itertuples():
        assert rt(int(r.sourceId), list(DATES).index(r.date)) is not None
    man = built()["manifest"]
    assert man["layout"] == "thin-v1" and set(man["transitional"]) == set(thin_build.TRANSITIONAL_KINDS)
    assert set(built()["tr"].kind) <= set(thin_build.TRANSITIONAL_KINDS)


def test_endDate_day_unquoted():
    e = events_of(270)
    assert e.date.iloc[0] == D[5]                                       # endDate 당일 bar 4 는 동결, 정산은 다음 bar
    assert rt(270, 4) is None and rt(270, 5) == 9.0 / _f(270, 3) - 1.0


def test_successor_retired_earlier():
    e = events_of(280)
    assert pd.isna(e.destinationId.iloc[0]) and rt(280, 6) == 0.0     # 281 은 bar 3 에 은퇴(시세 재개와 무관)
    assert rt(281, 4) is None                                           # 은퇴 뒤 행 없음


def test_value_only_on_settlement_bar():
    m = _f(290, 5)
    assert rt(290, 6) == (0.0 + (21.0 / m - 1.0)) + 1.0 / m
    assert (events_of(290).sourceEnds).all()


def test_frozen_exdate_leg_and_resume():
    m = _f(300, 3)
    assert rt(300, 4) == 0.0 + 0.5 / m                                  # 무시세 ex-date 에 leg 만
    assert rt(300, 5) is None
    assert rt(300, 6) == _f(300, 6) / m - 1.0                           # 재개일은 가격만


def test_no_row_on_first_observation():
    assert rt(111, 4) is None and rt(111, 5) == _f(111, 5) / _f(111, 4) - 1.0
    assert rt(100, 0) is None


def test_pending_child_ledger_value_path():
    m = _f(310, 3)
    assert events_of(310).empty
    assert rt(310, 4) == (0.0 + 1.0 * 2.0 / m) + (_f(310, 4) / m - 1.0)
    t = trans_of(310, "pending_child")
    assert len(t) == 1 and "engine_path=ledger_value" in t.detail.iloc[0] and t.value_used.iloc[0] == 2.0 / m
    assert rt(311, 6) is None                                           # 자회사 첫 관측일 행 없음


def test_master_includes_aux():
    ms = pd.read_parquet(built()["out"] / "master.parquet")
    assert 998 in set(ms.tradingItemId) and ms.tradingItemId.is_unique
    assert ms[ms.tradingItemId == 110].symbol.iloc[0] == "S110"


def test_child_equal_parent_raises():
    tmp = Path(tempfile.mkdtemp(prefix="thin_build_bad_"))
    try:
        make_source(tmp / "src")
        led = pd.read_parquet(tmp / "src" / "engine" / "ca_ledger_cache.parquet")
        led.loc[led.tradingItemId == "110", "childTradingItemId"] = "110"
        led.to_parquet(tmp / "src" / "engine" / "ca_ledger_cache.parquet", index=False)
        try:
            thin_build.build(tmp / "src", tmp / "out", tmp / "work")
        except ValueError as e:
            assert "own child" in str(e)
        else:
            raise AssertionError("no error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_first_quote_with_ledger_estimate():
    m = _f(320, 3)
    t = trans_of(320, "pending_child")
    assert len(t) == 1 and "engine_path=first_quote" in t.detail.iloc[0]         # 321 은 bar 0~1 에 관측됨
    assert f"engine_date={D[6].date()}" in t.detail.iloc[0] and f"engine_value={1.0 * _f(321, 6) / m!r}" in t.detail.iloc[0]
    assert t.value_used.iloc[0] == 1.0 * 3.0 / m                                  # ex-date 값 = 원장 추정
    assert rt(320, 4) == (0.0 + 3.0 / m) + (_f(320, 4) / m - 1.0)


def test_same_child_two_parents_engine_order():
    a, b = trans_of(330, "pending_child"), trans_of(331, "pending_child")
    assert "engine_path=ledger_value" in a.detail.iloc[0]                          # 앞선 (bar, parent) 가 원장값 mark 를 잡음
    assert "engine_path=first_quote" in b.detail.iloc[0] and f"engine_date={D[6].date()}" in b.detail.iloc[0]
    assert a.value_used.iloc[0] == 2.0 / _f(330, 3) and b.value_used.iloc[0] == 2.0 / _f(331, 3)


def test_rights_expiry_mark_and_priority():
    e = events_of(341)
    assert len(e) == 1 and e.date.iloc[0] == D[7] and e.eventId.iloc[0].endswith("_expiry")   # 만료 뒤 시세가 있으면 첫 무시세 bar
    assert rt(341, 7) == -1.0 and rt(341, 5) is not None
    e = events_of(343)
    assert len(e) == 1 and e.date.iloc[0] == D[5] and e.eventId.iloc[0].endswith("_end")      # events 정산 우선
    assert rt(343, 5) == 0.5 / _f(343, 4) - 1.0
    assert events_of(345).empty                                                    # mark 없음 → 만료 없음


def main() -> int:
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    if _STATE:
        shutil.rmtree(_STATE["tmp"], ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
