#!/home/jgo/usr/venv-3.11/bin/python
"""R1 기업행동 원장 테스트(합성 fixture). pytest 또는 직접 실행:

  /home/jgo/usr/venv-3.11/bin/python tests/test_ledger.py [--keep DIR]

합성 캐시(base_data + factors/split + factors/dividend + calendar + master 최소) 위에 ledger_build 로 원장을 만들고
loader.export_ca_ledger 로 엔진 단위 환산을 검사한다. 시나리오(부모 P=1, 자회사 C=2, H=3):
  2020-02-03  P 현금배당 1.0 (분할 전, sf=1.0)
  2020-03-02  P 2:1 분할 (raw 반토막, parentFactor 2)
  2020-04-01  P 현금배당 1.0 (분할 뒤, sf=0.5 → cashCache 2.0)
  2020-04-15  P → C 스핀 0.5/구주 (C 는 2020-07-01 3:1 분할 → exDate sf_child 3.0 → childRatioCache 3.0)
  2020-05-01  P 현금 0.4 + C 0.1 같은 날(두 행)
  2020-06-01  P 1:2 역분할 + H 0.5/구주 (HON 형: childRatioCache 1.0)
단위 항등: cashCache × 현재주식수 == cashRaw × 구주수, childRatioCache × 현재 부모주 == childRatio × 구주 × sf_child.
각 검사는 반례가 실패로 판정되는지 먼저 보인다.
"""
from __future__ import annotations

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
import ledger_build as lb  # noqa: E402
import ledger_repairs as rep  # noqa: E402
from ledger_qa import q1_unique_key, q2_schema, q3_split_repro, q4_cash_sum, q5_same_day, q6_calendar  # noqa: E402
from loader import CA_EXPORT_COLUMNS, CacheV21, ContractError  # noqa: E402

T = pd.Timestamp
_WORK: Path | None = None
_FIX: Path | None = None
_LEDGER: pd.DataFrame | None = None


def workdir() -> Path:
    global _WORK
    if _WORK is None:
        _WORK = Path(tempfile.mkdtemp(prefix="cache_v21_ledger_test_"))
    return _WORK


def _write(df: pd.DataFrame, path: Path, schema: pa.Schema | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    pq.write_table(t, path, compression="zstd")


def make_cache(root: Path) -> dict:
    """합성 캐시. raw close = 현재기준 가격 × sf(구간 계수). 반환: 진짜 경로·계수."""
    cal = pd.bdate_range("2020-01-02", "2020-12-31")
    cur = {1: 100.0 + 0.1 * np.arange(len(cal)), 2: 4.0 + 0.005 * np.arange(len(cal)), 3: 60.0 + 0.02 * np.arange(len(cal))}
    win = {  # (validFrom, validTo, factor)
        1: [(T("1900-01-01"), T("2020-03-02"), 1.0), (T("2020-03-02"), T("2020-06-01"), 0.5), (T("2020-06-01"), None, 1.0)],
        2: [(T("1900-01-01"), T("2020-07-01"), 3.0), (T("2020-07-01"), None, 1.0)],
        3: [(T("1900-01-01"), None, 1.0)],
    }

    def sf_at(tid, d):
        for vf, vt, f in win[tid]:
            if d >= vf and (vt is None or d < vt):
                return f
        raise KeyError

    # 부모(1)의 가격은 스핀 배당락일에 분배 가치(구주 1주당 = ratio × 자회사 raw 종가)만큼 빠진다(R1c ② 가격 정합; 현재기준 단위 = 값 / sf_prev)
    spins = [(T("2020-04-15"), 2, 0.5), (T("2020-05-01"), 2, 0.1), (T("2020-06-01"), 3, 0.5)]
    drop_cur = np.zeros(len(cal))
    for ex, child, ratio in spins:
        k = cal.get_loc(ex)
        v = ratio * float(np.float32(cur[child][k] * sf_at(child, ex)))
        drop_cur[k:] += v / sf_at(1, cal[k - 1])
    cur[1] = cur[1] - drop_cur

    rows = []
    for tid in (1, 2, 3):
        for k, d in enumerate(cal):
            if tid == 3 and d < T("2020-05-20"):          # H 는 2020-05-20 상장(스핀 전 when-issued 없음)
                continue
            sf = sf_at(tid, d)
            raw = cur[tid][k] * sf
            rows.append((d, tid, np.float32(raw), np.float32(raw), np.float32(raw), np.float32(raw), np.float32(raw), 1000.0 / sf, 1, 0))
    base = pd.DataFrame(rows, columns=["date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume", "source", "qualityFlag"])
    base = base.sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    schema = pa.schema([("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()), ("open", pa.float32()), ("high", pa.float32()),
                        ("low", pa.float32()), ("close", pa.float32()), ("vwap", pa.float32()), ("volume", pa.float64()),
                        ("source", pa.int8()), ("qualityFlag", pa.int8())])
    _write(base, root / "base_data.parquet", schema)
    srows = [(tid, vf, vt, f, "ciqsplitcache" if vf != T("1900-01-01") else "synthetic_pre_first", T("2026-09-17"))
             for tid, w in win.items() for vf, vt, f in w]
    fschema = pa.schema([("tradingItemId", pa.int64()), ("validFrom", pa.timestamp("ns")), ("validTo", pa.timestamp("ns")),
                         ("factor", pa.float64()), ("source", pa.string()), ("snapshotDate", pa.timestamp("ns"))])
    _write(pd.DataFrame(srows, columns=["tradingItemId", "validFrom", "validTo", "factor", "source", "snapshotDate"]), root / "factors" / "split.parquet", fschema)
    _write(pd.DataFrame([(tid, T("1900-01-01"), None, 1.0, "synthetic_no_window", T("2026-09-17")) for tid in (1, 2, 3)],
                        columns=["tradingItemId", "validFrom", "validTo", "factor", "source", "snapshotDate"]), root / "factors" / "dividend.parquet", fschema)
    _write(pd.DataFrame({"date": cal, "nClose": 3, "rollingMedian": 3.0, "isTradingDay": True}), root / "calendar.parquet")
    _write(pd.DataFrame({"tradingItemId": [1, 2, 3], "firstTradeDate": [T("2020-01-02"), T("2020-01-02"), T("2020-05-20")],
                         "lastTradeDate": [pd.NaT] * 3, "firstPriceDate": [T("2020-01-02"), T("2020-01-02"), T("2020-05-20")],
                         "ciqLastCloseDate": [cal[-1]] * 3, "status": ["active"] * 3}), root / "master" / "lifecycle.parquet")
    _write(pd.DataFrame({"tradingItemId": [1, 2, 3]}), root / "master" / "instruments.parquet")
    _write(pd.DataFrame({"universe": ["Top500"], "tradingItemId": [1], "fromDate": [cal[0]], "toDate": [cal[-1]]}), root / "universe" / "membership.parquet")
    _write(pd.DataFrame(columns=["oldId", "newId", "relation", "effectiveDate", "ratio", "evidence", "confidence"]).astype(
        {"oldId": "int64", "newId": "int64", "relation": "string", "effectiveDate": "datetime64[ns]", "ratio": "float64", "evidence": "string", "confidence": "string"}),
        root / "master" / "lineage.parquet")
    return {"cal": cal, "cur": cur, "win": win, "sf_at": sf_at}


def build_ledger(root: Path, info: dict) -> pd.DataFrame:
    """ledger_build 함수들로 합성 원천 → 원장(events/ca_ledger.parquet)."""
    cal = lb.Calendar.from_root(root)
    closes = lb.RawCloses(root / "base_data.parquet")
    dv = pd.DataFrame([
        dict(dividendId=1, tradingItemId=1, exDate=T("2020-02-03"), divAmount=1.0, dividendTypeId=1, supplementalTypeName="Regular cash dividend"),
        dict(dividendId=2, tradingItemId=1, exDate=T("2020-04-01"), divAmount=1.0, dividendTypeId=1, supplementalTypeName="Regular cash dividend"),
        dict(dividendId=3, tradingItemId=1, exDate=T("2020-05-01"), divAmount=0.4, dividendTypeId=1, supplementalTypeName="Regular cash dividend"),
        dict(dividendId=4, tradingItemId=1, exDate=T("2020-05-01"), divAmount=0.1, dividendTypeId=4, supplementalTypeName="Special dividend"),
        dict(dividendId=5, tradingItemId=1, exDate=T("2020-04-15"), divAmount=np.nan, dividendTypeId=20, supplementalTypeName=None),
        dict(dividendId=6, tradingItemId=1, exDate=T("2020-06-01"), divAmount=np.nan, dividendTypeId=20, supplementalTypeName=None),
    ])
    for c, v in (("payDate", pd.NaT), ("recordDate", pd.NaT), ("announcedDate", pd.NaT), ("netAmount", np.nan), ("grossAmount", np.nan),
                 ("currencyId", 160.0), ("fxRate", 1.0), ("dividendTypeName", "x"), ("isCash", False), ("isStock", False)):
        dv[c] = v
    dv["isCash"] = dv.dividendTypeId.isin([1, 4])
    dv["isStock"] = dv.dividendTypeId == 20
    dist = pd.DataFrame([
        dict(tradingItemId=1, exDate=T("2020-04-15"), kind="stock", leg=0, newTradingItemId=2, ratioNewPerOld=0.5, eventClass="spinoff", status="ok",
             source="ciqdividend20", evidence="TEST C", newIdMethod="test", mismatchClass=None, ratioBasis="per_share_text", cashEquivalentText=np.nan, impliedValueFracExCash=np.nan),
        dict(tradingItemId=1, exDate=T("2020-05-01"), kind="stock", leg=0, newTradingItemId=2, ratioNewPerOld=0.1, eventClass="spinoff", status="ok",
             source="co_acthist", evidence="TEST C2", newIdMethod="test", mismatchClass=None, ratioBasis="per_share_text", cashEquivalentText=np.nan, impliedValueFracExCash=np.nan),
        dict(tradingItemId=1, exDate=T("2020-06-01"), kind="stock", leg=0, newTradingItemId=3, ratioNewPerOld=0.5, eventClass="spinoff", status="ok",
             source="ciqdividend20", evidence="TEST H", newIdMethod="test", mismatchClass=None, ratioBasis="per_share_text", cashEquivalentText=np.nan, impliedValueFracExCash=np.nan),
    ])
    split = pd.read_parquet(root / "factors" / "split.parquet")
    ciqsplit = pd.DataFrame({"splitid": [1, 2, 3], "tradingitemid": [1, 1, 2], "exdate": [T("2020-03-02"), T("2020-06-01"), T("2020-07-01")],
                             "rate": [2.0, 0.5, 3.0], "splittypeid": [12, 34, 12], "appliedflag": [1, 1, 1]})
    lc = pd.read_parquet(root / "master" / "lifecycle.parquet")
    ln = pd.read_parquet(root / "master" / "lineage.parquet")
    cash, _ = lb.build_cash_rows(dv, cal, {160: "USD"}, rep.gross_first)
    sp = lb.build_split_rows(split, ciqsplit, cal, closes)
    st = lb.build_stock_rows(dist, dv, cal, lc, ln)
    st = lb.value_children(st, closes, None, cash, sp, {1, 2, 3})
    sp = lb.reconcile_split_same_day(sp, st, cash, closes)
    led = lb.finalize([cash, sp, st])
    led.attrs = {}
    lb.write_ledger(led, root / "events" / "ca_ledger.parquet")
    return led


def fixture() -> tuple[Path, pd.DataFrame, dict]:
    global _FIX, _LEDGER, _INFO
    if _FIX is None:
        _FIX = workdir() / "fx"
        _INFO = make_cache(_FIX)
        _LEDGER = build_ledger(_FIX, _INFO)
    return _FIX, _LEDGER, _INFO


# ----------------------------------------------------------------------------------------------------------
def test_ledger_rows_and_invariants():
    """원장 행 구성(현금 3, 분할 3, 스핀 3)과 QA 불변식(Q1~Q6) 통과, 반례 실패."""
    root, L, info = fixture()
    cal = lb.Calendar.from_root(root)
    assert L.eventType.value_counts().to_dict() == {"cash_dividend": 2, "special_dividend": 1, "split": 3, "spin_off": 3}, L.eventType.value_counts().to_dict()
    assert L.eventId.is_unique and list(L.columns) == lb.LEDGER_COLUMNS
    split = pd.read_parquet(root / "factors" / "split.parquet")
    dv = pd.DataFrame({"dividendId": [1, 2, 3, 4], "tradingItemId": 1, "exDate": pd.to_datetime(["2020-02-03", "2020-04-01", "2020-05-01", "2020-05-01"]),
                       "divAmount": [1.0, 1.0, 0.4, 0.1], "netAmount": np.nan, "grossAmount": np.nan, "currencyId": 160.0, "fxRate": 1.0,
                       "dividendTypeId": [1, 1, 1, 4], "supplementalTypeName": "Regular cash dividend"})
    for fn, args in ((q1_unique_key, (L,)), (q2_schema, (L,)), (q3_split_repro, (L, split, cal)), (q4_cash_sum, (L, dv, cal, None)),
                     (q5_same_day, (L,)), (q6_calendar, (L, cal))):
        r = fn(*args)
        assert r["ok"], (fn.__name__, r)
    # 같은 날 정기 + 특별 → 한 행(합산 0.5, special_dividend, 두 dividendId)
    d = L[(L.exDate == T("2020-05-01")) & (L.legType == "cash")]
    assert len(d) == 1 and abs(d.cashPerShare.iloc[0] - 0.5) < 1e-12 and d.eventType.iloc[0] == "special_dividend" and "3,4" in d.evidence.iloc[0]
    # 같은 날 현금 + 주식 = 두 행
    assert set(L[L.exDate == T("2020-05-01")].legType) == {"cash", "stock"}
    # HON 형: 역분할 행 parentFactor 0.5 + 스핀 행 childRatio 0.5, 둘 다 같은 날, 값 = 0.5 × H raw close
    h = L[L.exDate == T("2020-06-01")]
    assert set(h.legType) == {"quantity", "stock"} and h[h.legType == "quantity"].parentFactor.iloc[0] == 0.5
    k = info["cal"].get_loc(T("2020-06-01"))
    assert abs(h[h.legType == "stock"].childValuePerOldShare.iloc[0] - 0.5 * np.float32(info["cur"][3][k])) < 1e-6
    assert (h.status == "ok").all(), h[["eventId", "status", "notes"]].to_dict("records")   # 같은 날 스핀이 raw 갭을 설명
    # 반례: 분할 행 비율을 바꾸면 Q3 실패, 현금을 바꾸면 Q4 실패
    bad = L.copy(); bad.loc[bad.legType == "quantity", "parentFactor"] = 1.7
    assert not q3_split_repro(bad, split, cal)["ok"]
    bad = L.copy(); bad.loc[bad.legType == "cash", "cashPerShare"] += 0.01
    assert not q4_cash_sum(bad, dv, cal, None)["ok"]


def test_export_unit_identities():
    """export_ca_ledger 단위 환산 항등: cashCache × 현재주식수 == cashRaw × 구주수; childRatioCache × 현재 부모주 == childRatio × 구주 × sf_child."""
    root, L, info = fixture()
    cv = CacheV21(root)
    out = workdir() / "export" / "engine" / "ca_ledger_cache.parquet"
    m = cv.export_ca_ledger(out)
    E = pd.read_parquet(out)
    assert list(E.columns) == CA_EXPORT_COLUMNS and m["rows"] == len(L) == len(E) and len(m["sha256"]) == 64 and m["ledger_rows"] == len(L)
    assert E.tradingItemId.dtype == object and E.tradingItemId.iloc[0] == "1" and str(E.exDate.dtype) == "datetime64[ns]"
    assert (E.parentFactorCache == 1.0).all()
    sf_at = info["sf_at"]
    old_shares = 10.0

    def row(ex, leg):
        x = E[(E.exDate == T(ex)) & (E.legType == leg)]
        assert len(x) == 1, (ex, leg, len(x))
        return x.iloc[0]

    def prev_day(ex):
        cal = info["cal"]
        return cal[cal.get_loc(T(ex)) - 1]

    # (a) 분할 전 배당(sf 1.0): cashCache 1.0 ; (b) 분할 후 배당(sf 0.5): cashCache 2.0 = 1.0 / 0.5
    for ex, raw in (("2020-02-03", 1.0), ("2020-04-01", 1.0), ("2020-05-01", 0.5)):
        r = row(ex, "cash")
        sf_before = sf_at(1, prev_day(ex))
        assert abs(r.cashPerShareCache - raw / sf_before) < 1e-12, (ex, r.cashPerShareCache, raw, sf_before)
        cur_shares = old_shares * sf_before                     # 구주 old_shares = 현재 기준 old_shares × sf
        assert abs(r.cashPerShareCache * cur_shares - raw * old_shares) < 1e-9, "cash unit identity"
    assert abs(row("2020-04-01", "cash").cashPerShareCache - 2.0) < 1e-12
    # (c) 분할 후 스핀: childRatioCache = 0.5 × sf_C(2020-04-15)=3.0 / sf_P(before)=0.5 = 3.0
    r = row("2020-04-15", "stock")
    assert abs(r.childRatioCache - 0.5 * sf_at(2, T("2020-04-15")) / sf_at(1, prev_day("2020-04-15"))) < 1e-12 and abs(r.childRatioCache - 3.0) < 1e-12
    cur_parent = old_shares * sf_at(1, prev_day("2020-04-15"))
    assert abs(r.childRatioCache * cur_parent - 0.5 * old_shares * sf_at(2, T("2020-04-15"))) < 1e-9, "child unit identity"
    k = info["cal"].get_loc(T("2020-04-15"))
    assert abs(r.childCloseAtExCache - np.float32(np.float32(info["cur"][2][k] * 3.0) / 3.0)) < 1e-4 and r.childValueSource == "child_close_ex" and not r.isEstimate
    assert abs(r.parentCloseBeforeCache - cv.get_panel("close", "split_adj").loc[prev_day("2020-04-15"), 1]) < 1e-9
    assert abs(r.parentCloseAtExCache - cv.get_panel("close", "split_adj").loc[T("2020-04-15"), 1]) < 1e-9
    # (d) HON 형 같은 날 역분할 + 스핀: sf_before 0.5 / sf_ex 1.0 → childRatioCache = 0.5 × 1.0 / 0.5 = 1.0, 분할 행 parentFactorCache 1.0
    r = row("2020-06-01", "stock")
    assert abs(r.childRatioCache - 1.0) < 1e-12 and row("2020-06-01", "quantity").parentFactorCache == 1.0
    assert m["value_legs_on_same_day_split"] == 1
    # (e) 같은 날 현금 + 주식 두 행, 키 유일
    assert len(E[E.exDate == T("2020-05-01")]) == 2
    assert not E.duplicated(["tradingItemId", "exDate", "legType", "childTradingItemId"]).any()
    # 반례: 분할 행 parentFactor 를 바꾼 원장은 거부(계수 테이블과 불일치)
    bad = L.copy()
    bad.loc[bad.legType == "quantity", "parentFactor"] = 3.0
    bad_root = workdir() / "fx_bad"
    shutil.copytree(root, bad_root, dirs_exist_ok=True)
    lb.write_ledger(bad, bad_root / "events" / "ca_ledger.parquet")
    try:
        CacheV21(bad_root).export_ca_ledger(workdir() / "export_bad.parquet")
        raise AssertionError("tampered parentFactor accepted")
    except ContractError:
        pass
    try:
        cv.export_ca_ledger(workdir() / "x.parquet", basis="raw")
        raise AssertionError("basis=raw accepted")
    except ValueError:
        pass


def test_export_child_fallbacks_and_exclusions():
    """자회사 시세 없음 + ③④ 추정 → childCloseAtExCache 환산·isEstimate; 패널 밖 자회사 → NaN; merger/rename 행 제외; export_wide 통합."""
    root, L, info = fixture()
    extra = L.iloc[:0].copy()
    rows = [
        # H(3) 는 2020-05-20 상장: 2020-05-15 스핀은 exDate 시세 없음 → event_value 30.0/0.1 = 300 raw → /sf_H(1.0) = 300
        dict(eventId="1_20200515_spin_off_0", tradingItemId=1, exDate=T("2020-05-15"), eventType="spin_off", legType="stock", parentFactor=1.0,
             childTradingItemId=3, childRatio=0.1, childValueSource="event_value", childValuePerOldShare=30.0, source="distributions", status="estimated",
             isEstimate=True, evidence="", notes=""),
        # 패널 밖 자회사 99 → childRatioCache/childCloseAtExCache NaN (distribution_unpriced)
        dict(eventId="1_20200520_spin_off_0", tradingItemId=1, exDate=T("2020-05-20"), eventType="spin_off", legType="stock", parentFactor=1.0,
             childTradingItemId=99, childRatio=0.2, childValueSource="unknown", childValuePerOldShare=np.nan, source="distributions", status="unresolved",
             isEstimate=False, evidence="", notes=""),
        # 제외 대상
        dict(eventId="1_20200701_merger_cash_0", tradingItemId=1, exDate=T("2020-07-01"), eventType="merger_cash", legType="cash", parentFactor=1.0,
             cashPerShare=50.0, childValueSource="unknown", source="corporate_actions", status="ok", isEstimate=False, evidence="", notes=""),
        dict(eventId="1_20200701_relist_0", tradingItemId=1, exDate=T("2020-07-01"), eventType="relist", legType="quantity", parentFactor=1.0,
             childTradingItemId=2, childValueSource="unknown", source="lineage", status="ok", isEstimate=False, evidence="", notes=""),
        # 패널 범위 밖 exDate → 버림
        dict(eventId="1_20301231_cash_dividend_0", tradingItemId=1, exDate=T("2030-12-31"), eventType="cash_dividend", legType="cash", parentFactor=1.0,
             cashPerShare=1.0, childValueSource="unknown", source="ciqdividend", status="ok", isEstimate=False, evidence="", notes=""),
        # 패널에 없는 부모 → 버림
        dict(eventId="7_20200203_cash_dividend_0", tradingItemId=7, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", parentFactor=1.0,
             cashPerShare=1.0, childValueSource="unknown", source="ciqdividend", status="ok", isEstimate=False, evidence="", notes=""),
    ]
    extra = pd.concat([extra, pd.DataFrame(rows)], ignore_index=True)
    for c in lb.LEDGER_COLUMNS:
        if c not in extra.columns:
            extra[c] = None
    L2 = lb.finalize([L, extra])
    root2 = workdir() / "fx2"
    shutil.copytree(root, root2, dirs_exist_ok=True)
    lb.write_ledger(L2, root2 / "events" / "ca_ledger.parquet")
    cv = CacheV21(root2)
    out = workdir() / "export2"
    man = cv.export_wide(out, fields=("close",), basis="split_adj", masks=(), returns=False)
    assert "engine/ca_ledger_cache.parquet" in man["files"] and man["ca_ledger"]["ledger_rows"] == len(L2)
    E = pd.read_parquet(out / "engine" / "ca_ledger_cache.parquet")
    assert not E.eventType.isin(("merger_cash", "merger_stock", "rename", "relist")).any()
    # rc2f Y1: 금액 없는 현금 leg·자회사 미확인 leg 는 더 이상 `dropped` 가 아니라 표시용 행(manifest `marker_rows`); 그 밖의 제외 사유는 같은 키로 계속 검증
    assert man["ca_ledger"]["dropped"] == {"excluded_event_types": 2, "stitched_copies_excluded": 0, "events_successor_duplicate": 0,
                                            "events_cash_duplicate": 0, "cash_leg_after_parent_lastTradeDate": 0,
                                            "parent_not_in_panel": 1, "exDate_outside_panel": 1, "no_prior_parent_close": 0, "parent_close_stale_before_exDate": 0}
    mk = man["ca_ledger"]["marker_rows"]
    zero = {"no_cash_amount": 0, "no_child": 0, "coacthist_stale_termination": 0}         # D1: third reason (lead 2026-09-21)
    assert mk["exported"] == 0 and mk["by_reason"] == zero and mk["in_ledger_by_reason"] == zero and mk["dropped_key_conflict"] == 0
    assert "markerReason" in E.columns and E.markerReason.isna().all() and "childValuePerShareCache" in E.columns
    assert len(E) == len(L2) - 4
    r = E[E.eventId == "1_20200515_spin_off_0"].iloc[0]
    assert abs(r.childCloseAtExCache - 300.0) < 1e-9 and bool(r.isEstimate) and r.childValueSource == "event_value"
    assert abs(r.childRatioCache - 0.1 * 1.0 / 0.5) < 1e-12          # sf_H 1.0, sf_P(before) 0.5
    assert abs(r.childValuePerShareCache - 30.0 / 0.5) < 1e-12        # Y1-B: childValuePerOldShare / sf_P(before)
    r = E[E.eventId == "1_20200520_spin_off_0"].iloc[0]
    assert np.isnan(r.childRatioCache) and np.isnan(r.childCloseAtExCache) and r.childTradingItemId == "99" and r.status == "unresolved"
    assert pd.isna(r.markerReason) and np.isnan(r.childValuePerShareCache)   # named child outside the panel, no value: unpriced, not a marker
    assert man["ca_ledger"]["child_close_estimated_fallback"] == 1 and man["ca_ledger"]["child_close_nan"] == 1
    # raw basis export: 원장 export 는 건너뛰고 manifest 에 기록
    man_raw = CacheV21(root2).export_wide(workdir() / "export_raw", fields=("close",), basis="raw", masks=(), returns=False)
    assert "engine/ca_ledger_cache.parquet" not in man_raw["files"] and "skipped" in man_raw["ca_ledger"]
    # 두 번 export 하면 sha256 동일
    m2 = CacheV21(root2).export_ca_ledger(workdir() / "export3.parquet")
    assert m2["sha256"] == man["files"]["engine/ca_ledger_cache.parquet"]["sha256"]


def test_repairs_gross_first_and_overrides():
    """세전 규칙(세후 감지 → gross, HOLDRs net 유지, 비정상 gross 무시) 과 수동 교정의 반례."""
    c = pd.DataFrame({"dividendId": [1, 2, 3, 4], "tradingItemId": [1, 1, 1, 1], "exDate": pd.to_datetime(["2020-01-06"] * 4),
                      "divAmount": [0.2775, 0.01, 0.06, 0.5], "netAmount": [0.2775, np.nan, np.nan, np.nan], "grossAmount": [0.37, 0.012, 0.6, np.nan],
                      "supplementalTypeName": ["Regular cash dividend", "Net amount", "Regular cash dividend", "Regular cash dividend"],
                      "companyName": ["CRH plc", "Utilities HOLDRs Trust", "Flagstar Bancorp", "Y Corp"]})
    out, repairs = rep.gross_first(c)
    assert out.amountLocal.tolist() == [0.37, 0.01, 0.06, 0.5], out.amountLocal.tolist()
    assert len(repairs) == 1 and repairs[0]["field"] == "cashPerShare" and repairs[0]["after"] == 0.37 and out.repairOf.iloc[0] == "after_tax_amount:1:2020-01-06"
    assert out.amountNote.tolist()[1].startswith("net_of_trust_fees") and out.amountNote.tolist()[2].startswith("gross_amount_implausible")
    # 수동 교정 대상 행이 없으면 건너뛰고 로그(row not_found), add=True 면 추가
    empty = pd.DataFrame(columns=lb.LEDGER_COLUMNS[1:] + ["_cashEquiv", "_impliedFrac", "_leg"])
    stock, merger, cash, split, rows = rep.apply_overrides(empty.copy(), empty.copy(), empty.copy(), empty.copy(), {}, None)
    kinds = pd.DataFrame(rows).field.value_counts().to_dict()
    assert kinds.get("row", 0) == sum(o["frame"] != "lineage" for o in rep.MANUAL_OVERRIDES)   # lineage 프레임 없음 → 건너뜀, 나머지 not_found/added/skipped(drop)
    assert len(merger) == 1 and merger.iloc[0].childTradingItemId == 2605788 and merger.iloc[0].childRatio == 0.7   # CTRA→DVN 추가
    # 확장 세전 규칙(F3): 역전 net(R2), net==gross<div(R3), 트러스트 net 유지(R4), rescinded 제외(R0)
    c2 = pd.DataFrame({"dividendId": [1, 2, 3, 4, 5], "tradingItemId": 1, "exDate": pd.to_datetime(["2020-01-06"] * 5),
                       "divAmount": [0.045, 0.8325, 0.012, 0.6, 0.2], "netAmount": [0.06, 0.61195, 0.014, np.nan, np.nan], "grossAmount": [np.nan, 0.61195, np.nan, np.nan, np.nan],
                       "supplementalTypeName": ["Regular cash dividend", "Regular cash dividend", "Net amount", "Dividend rescinded", "Regular cash dividend"],
                       "companyName": ["Chicago Bridge & Iron", "BCE Inc.", "Utilities HOLDRs Trust", "X", "Y"]})
    out2, rep2 = rep.gross_first(c2)
    assert out2.amountRule.tolist() == ["R2_inverted_net", "R3_net_eq_gross", "R4_trust_net", "R5_default"] and len(out2) == 4
    assert out2.amountLocal.tolist() == [0.06, 0.61195, 0.012, 0.2]
    assert {r["field"] for r in rep2} == {"cashPerShare", "row"} and sum(str(r["after"]) == "excluded" for r in rep2) == 1 and len(rep2) == 3
    # R1b(리뷰 F3②, R1b 재작업): 'Net amount' ADR — gross 있음·net NaN·div/gross 0.6~0.99·비-트러스트 → gross(AU 0.100348/0.118057);
    # 같은 모양이라도 트러스트는 net 유지(R4), div/gross < 0.6 은 벤더 오류(R5)
    c3 = pd.DataFrame({"dividendId": [1, 2, 3], "tradingItemId": 1, "exDate": pd.to_datetime(["2020-01-06"] * 3),
                       "divAmount": [0.100348, 0.100348, 0.05], "netAmount": [np.nan] * 3, "grossAmount": [0.118057, 0.118057, 0.118057],
                       "supplementalTypeName": ["Net amount"] * 3, "companyName": ["AngloGold Ashanti", "Utilities HOLDRs Trust", "Z"]})
    out3, rep3 = rep.gross_first(c3)
    assert out3.amountRule.tolist() == ["R1b_after_tax_net_nan", "R4_trust_net", "R5_gross_implausible"], out3.amountRule.tolist()
    assert out3.amountLocal.tolist() == [0.118057, 0.100348, 0.05] and out3.repairOf.iloc[0] == "after_tax_amount:1:2020-01-06"
    assert len(rep3) == 1 and rep3[0]["after"] == 0.118057 and "R1b" in rep3[0]["evidence"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=None)
    ap.add_argument("-k", default=None)
    a = ap.parse_args(argv)
    global _WORK
    if a.keep:
        _WORK = Path(a.keep)
        _WORK.mkdir(parents=True, exist_ok=True)
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
