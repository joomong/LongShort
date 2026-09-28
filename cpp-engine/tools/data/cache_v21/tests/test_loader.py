#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v2.1 참조 로더 테스트(합성 fixture). pytest 가 있으면 pytest 로, 없으면 이 파일을 직접 실행(자체 러너).

  /home/jgo/usr/venv-3.11/bin/python tests/test_loader.py [--keep DIR]

각 테스트는 먼저 반례(잘못된 산식·잘못된 데이터)가 실패로 판정되는지 보인 뒤(self-test) 정답을 검사한다.
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
sys.path.insert(0, str(HERE))
from loader import CacheV21, ContractError, FactorGapError, FactorOverlapError  # noqa: E402
from make_fixture import (CLASS_CHANGE_103, DIVS_102, EXCLUSION_104, GAP_103, LAST_TRADE_104, LAST_TRADE_105,  # noqa: E402
                          MARKETCAP_OUTSIDE_ROW, NULL_CLOSE_103, RELIST_106, SHARES_ADJ, SPLITS_101, TOP500, make_fixture)

_WORK: Path | None = None
_FIX: dict[str, Path] = {}


def workdir() -> Path:
    global _WORK
    if _WORK is None:
        _WORK = Path(tempfile.mkdtemp(prefix="cache_v21_test_"))
    return _WORK


def fixture(variant: str = "ok") -> Path:
    if variant not in _FIX:
        p = workdir() / f"fx_{variant}"
        make_fixture(p, variant)
        _FIX[variant] = p
    return _FIX[variant]


def loader(**kw) -> CacheV21:
    return CacheV21(fixture("ok"), **kw)


def truth_adj() -> pd.DataFrame:
    df = pd.read_parquet(fixture("ok") / "truth" / "adj_close_true.parquet")
    df.columns = [int(c) for c in df.columns]
    return df


def nan_equal(a: np.ndarray, b: np.ndarray) -> bool:
    a = np.asarray(a)
    b = np.asarray(b)
    return a.shape == b.shape and bool(np.all((a == b) | (pd.isna(a) & pd.isna(b))))


# ----------------------------------------------------------------------------------------------------------
def test_contract_schema():
    """fixture 가 계약 스키마(타입) 그대로인지 — 로더 스키마 검사가 위반을 잡는지 반례 포함."""
    root = fixture("ok")
    s = pq.read_schema(root / "base_data.parquet")
    assert [str(s.field(n).type) for n in ("date", "tradingItemId", "close", "volume", "source", "qualityFlag")] == \
        ["timestamp[ns]", "int64", "float", "double", "int8", "int8"]
    for f in ("split", "dividend"):
        fs = pq.read_schema(root / "factors" / f"{f}.parquet")
        assert [str(fs.field(n).type) for n in ("tradingItemId", "validFrom", "validTo", "factor")] == \
            ["int64", "timestamp[ns]", "timestamp[ns]", "double"]
    rs = pq.read_schema(root / "derived" / "returns_total.parquet")
    assert str(rs.field("ret").type) == "double" and str(rs.field("prevDate").type) == "timestamp[ns]"
    ls = pq.read_schema(root / "master" / "lifecycle.parquet")
    assert {"firstTradeDate", "firstPriceDate", "firstTradeSource", "ciqLastCloseDate", "lastTradeDate"} <= set(ls.names)
    es = pq.read_schema(root / "master" / "tradable_exclusions.parquet")
    assert es.names == ["tradingItemId", "fromDate", "toDate", "reason", "preferredItemId"]
    ms = pq.read_schema(root / "derived" / "marketcap.parquet")
    assert ms.names == ["date", "tradingItemId", "marketCap_USD", "sharesOutstanding"]
    for f in ("membership", "membership_v1rule"):
        assert pq.read_schema(root / "universe" / f"{f}.parquet").names == ["universe", "tradingItemId", "fromDate", "toDate"]
    cv = loader()
    assert cv.diagnostics["schema_notes"] == []
    assert "marketCap_USD" in cv.available_fields and "sharesOutstanding" in cv.available_fields
    # 반례: 컬럼 타입이 다른 파일 → 경고(schema_notes 기록)
    bad = workdir() / "bad_schema"
    shutil.copytree(root, bad, dirs_exist_ok=True)
    df = pd.read_parquet(root / "base_data.parquet")
    df["close"] = df["close"].astype("float64")
    df.to_parquet(bad / "base_data.parquet", index=False)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cvb = CacheV21(bad)
    assert any("close type double" in n for n in cvb.diagnostics["schema_notes"]) and len(w) >= 1


def test_axes_fixed_order_and_options():
    """ids 기본 int64 오름차순, 옵션 순서 지정·부분집합·instruments 순, 달력은 id 부분집합과 무관."""
    cv = loader()
    ids = cv.ids()
    assert ids.dtype == np.int64 and list(ids) == [101, 102, 103, 104, 105, 106]
    assert cv.dates().is_monotonic_increasing and cv.dates().is_unique and len(cv.dates()) == 501
    # 지정 순서(뒤집기 + base 에 없는 id 999 포함)
    cv2 = CacheV21(fixture("ok"), ids=[106, 101, 999])
    assert list(cv2.ids()) == [106, 101, 999]
    c_full = cv.get_panel("close", "split_adj")
    c_sub = cv2.get_panel("close", "split_adj")
    assert nan_equal(c_sub[106].to_numpy(), c_full[106].to_numpy()) and nan_equal(c_sub[101].to_numpy(), c_full[101].to_numpy())
    assert c_sub[999].isna().all() and len(c_sub) == 501          # 달력은 그대로, 없는 id 는 전부 NaN
    assert cv2.diagnostics["ids_requested_absent_from_base"] == 1
    cv3 = CacheV21(fixture("ok"), ids="instruments")
    assert list(cv3.ids()) == [101, 102, 103, 104, 105, 106]
    # 날짜 범위: 부분 패널 == 전체 패널 조각
    cv4 = CacheV21(fixture("ok"), start="2022-06-01", end="2022-08-31", ids=[101, 103])
    sub = cv4.get_panel("close", "div_adj")
    ref = c_full.loc["2022-06-01":"2022-08-31", [101, 103]]
    assert list(sub.index) == list(ref.index) and nan_equal(sub.to_numpy(), cv.get_panel("close", "div_adj").loc["2022-06-01":"2022-08-31", [101, 103]].to_numpy())
    r4 = cv4.get_returns()
    assert nan_equal(r4.to_numpy(), cv.get_returns().loc["2022-06-01":"2022-08-31", [101, 103]].to_numpy())
    # 중복 id 는 거부
    try:
        CacheV21(fixture("ok"), ids=[101, 101]).ids()
        raise AssertionError("duplicate ids accepted")
    except ValueError:
        pass


def test_split_adj_continuous_raw_jumps():
    """분할일: split_adj 는 연속(진짜 수익률), raw 는 1/비율 점프. 반례: raw 를 연속으로 보면 실패해야 함."""
    cv = loader()
    raw = cv.get_panel("close", "raw")[101]
    adj = cv.get_panel("close", "split_adj")[101]
    truth = truth_adj()[101]
    # 정답: split_adj == 진짜 경로(float32 반올림 이내)
    assert np.allclose(adj.to_numpy(), truth.to_numpy(dtype=np.float32), rtol=2e-7, atol=0)
    for d, ratio in SPLITS_101:
        d = pd.Timestamp(d)
        prev = adj.index[adj.index.get_loc(d) - 1]
        r_raw = raw[d] / raw[prev] - 1
        r_adj = adj[d] / adj[prev] - 1
        r_true = truth[d] / truth[prev] - 1
        assert abs(r_adj - r_true) < 1e-6, (d, r_adj, r_true)
        assert abs(r_raw - ((1 + r_true) / ratio - 1)) < 1e-6, (d, r_raw)   # raw 점프 = 1/비율
        assert abs(r_raw) > 0.3 and abs(r_adj) < 0.1                         # 반례: raw 를 연속 가정하면 틀림
    # 첫 구간 이전(synthetic_pre_first): 계수 3.0, raw = 3 × 진짜
    f = cv.get_factor("split")[101]
    assert f.loc[:"2022-06-30"].eq(3.0).all() and f.loc["2022-07-01":"2023-03-14"].eq(1.5).all() and f.loc["2023-03-15":].eq(1.0).all()
    assert np.allclose(raw.loc[:"2022-06-30"].to_numpy(), (truth.loc[:"2022-06-30"] * 3.0).to_numpy(dtype=np.float32), rtol=2e-7)


def test_volume_bases():
    """volume: split_adj = raw × sf (float64), div_adj 는 정의 없음 → ValueError."""
    cv = loader()
    v_raw = cv.get_panel("volume", "raw", cut=None)
    v_sa = cv.get_panel("volume", "split_adj", cut=None)
    sf = cv.get_factor("split")
    assert v_raw.dtypes.iloc[0] == np.float64 and v_sa.dtypes.iloc[0] == np.float64
    assert nan_equal(v_sa.to_numpy(), (v_raw * sf).to_numpy())
    d = pd.Timestamp(SPLITS_101[0][0])
    prev = v_raw.index[v_raw.index.get_loc(d) - 1]
    assert sf.loc[prev, 101] == 3.0 and abs(v_raw.loc[prev, 101] * 3.0 - v_sa.loc[prev, 101]) < 1e-6   # 분할 전 raw 주수 = 조정주수/3
    assert abs(v_sa.loc[prev, 101] / v_sa.loc[d, 101] - v_raw.loc[prev, 101] * 3.0 / v_raw.loc[d, 101] / 1.5) < 1e-9
    try:
        cv.get_panel("volume", "div_adj")
        raise AssertionError("volume div_adj must raise")
    except ValueError as e:
        assert "div_adj" in str(e)


def test_div_adj_exdate_total_return():
    """배당락일 총수익률 = 벤더 계수 정의: 1 + ret_total = (1 + ret_price) / (1 − D/P_prev). 휴장일 ex(특별배당) 포함.
    반례: 현금식 (P+D)/P_prev 와는 2차항만큼 다름(> 1e-6) 을 확인해 두 정의가 구별됨을 보인다."""
    cv = loader()
    ret = cv.get_returns()[102]
    raw = cv.get_panel("close", "raw")[102].astype(np.float64)
    adj = cv.get_panel("close", "div_adj")[102].astype(np.float64)
    divs = pd.read_csv(fixture("ok") / "truth" / "dividends.csv", parse_dates=["exDate", "effectiveDate", "prevDate"])
    assert len(divs) == 5 and (pd.Timestamp(DIVS_102[-1][0]) not in ret.index)   # 특별배당 ex 는 휴장일
    for _, d in divs.iterrows():
        eff, prev, D = d.effectiveDate, d.prevDate, d.amount
        assert ret.index[ret.index.get_loc(eff) - 1] == prev
        r_price = raw[eff] / raw[prev] - 1
        expect = (1 + r_price) / (1 - D / raw[prev]) - 1
        assert abs(ret[eff] - expect) < 1e-9, (eff, ret[eff], expect)
        cash_def = (raw[eff] + D) / raw[prev] - 1
        x, g = D / raw[prev], raw[eff] / raw[prev]
        assert abs(ret[eff] - cash_def) > 1e-6                                 # 두 정의가 구별됨
        assert abs((ret[eff] - cash_def) - (g / (1 - x) - g - x)) < 1e-9       # 차이 = g/(1−x) − g − x (2차항)
        # div_adj 패널 비율 == 총수익률(float32 반올림 이내)
        assert abs(adj[eff] / adj[prev] - 1 - ret[eff]) < 1e-6
    # 배당 없는 날엔 총수익률 == 가격수익률
    others = ret.index.difference(divs.effectiveDate)[1:]
    assert np.allclose(ret[others].to_numpy(), (raw / raw.shift()).reindex(others).to_numpy() - 1, atol=1e-9)
    # 현재 구간 계수 1.0, 첫 구간 이전 = 첫 구간값
    f = cv.get_factor("dividend")[102]
    assert f.loc["2023-01-03":].eq(1.0).all() and abs(f.iloc[0] - divs.factorBefore.iloc[0]) < 1e-12


def test_returns_total_equals_manual():
    """returns_total wide == 수동 계산(직전 관측 행, 갭·null close 건너뜀) == compute_returns. prevDate 도 검증."""
    cv = loader()
    r_file = cv.get_returns()
    r_calc = cv.compute_returns()
    assert nan_equal(r_file.to_numpy(), r_calc.to_numpy())
    base = pd.read_parquet(fixture("ok") / "base_data.parquet")
    sf = cv.get_factor("split")
    df_ = cv.get_factor("dividend")
    for tid, g in base.groupby("tradingItemId"):
        g = g[g.close.notna()].set_index("date")
        adj = g.close.astype(np.float64) / sf.loc[g.index, tid] * df_.loc[g.index, tid]
        manual = adj / adj.shift() - 1
        got = r_file.loc[g.index, tid]
        assert nan_equal(got.to_numpy(), manual.to_numpy()), tid
        assert r_file[tid].drop(g.index).isna().all(), tid          # 행 없는 날은 NaN
    # 반례: close 비율(raw)로 계산한 수익률은 분할일에 틀림
    raw = cv.get_panel("close", "raw")
    wrong = raw.pct_change()[101].loc[SPLITS_101[0][0]]
    assert abs(wrong - r_file[101].loc[SPLITS_101[0][0]]) > 0.3
    # prevDate: 갭 재개일의 prevDate = 갭 직전 거래일
    long = cv.read_returns_long()
    lg = long[long.tradingItemId == 103].set_index("date")
    resume = cv.dates()[cv.dates() > GAP_103[1]][0]
    before = cv.dates()[cv.dates() < GAP_103[0]][-1]
    assert lg.loc[resume, "prevDate"] == before
    assert lg["prevDate"].isna().sum() == 1 and pd.isna(lg["prevDate"].iloc[0])
    assert cv.check_returns()["n_diff_gt_atol"] == 0


def test_gap_and_null_close():
    """5거래일 무행 갭·close NaN 행: 패널 NaN, 수익률 NaN, 재개일 수익률은 직전 관측 종가 대비."""
    cv = loader()
    c = cv.get_panel("close", "raw")[103]
    o = cv.get_panel("open", "raw")[103]
    r = cv.get_returns()[103]
    gap = cv.dates()[(cv.dates() >= GAP_103[0]) & (cv.dates() <= GAP_103[1])]
    assert len(gap) == 5 and c[gap].isna().all() and o[gap].isna().all() and r[gap].isna().all()
    resume = cv.dates()[cv.dates() > GAP_103[1]][0]
    before = cv.dates()[cv.dates() < GAP_103[0]][-1]
    assert abs(r[resume] - (float(c[resume]) / float(c[before]) - 1)) < 1e-9
    d = pd.Timestamp(NULL_CLOSE_103)
    nxt = cv.dates()[cv.dates() > d][0]
    prv = cv.dates()[cv.dates() < d][-1]
    assert np.isnan(c[d]) and not np.isnan(o[d]) and np.isnan(r[d])         # close 만 NaN, 다른 필드 존재
    assert abs(r[nxt] - (float(c[nxt]) / float(c[prv]) - 1)) < 1e-9        # null close 행을 건너뜀
    assert cv.get_panel("qualityFlag").loc[d, 103] == 3
    assert not cv.get_mask("tradable").loc[d, 103]                           # close NaN → tradable False


def test_tradable_tail_cut_by_lifecycle():
    """합병 종목: base_data 에 남은 승계사 가격 꼬리 10일은 close 가 있어도 tradable False. lastTradeDate 당일은 True."""
    cv = loader()
    c = cv.get_panel("close", "raw", cut=None)[104]
    m = cv.get_mask("tradable")[104]
    last = pd.Timestamp(LAST_TRADE_104)
    tail = cv.dates()[cv.dates() > last][:10]
    assert c[tail].notna().all() and len(tail) == 10                          # 꼬리 가격은 존재(미절단)
    assert cv.get_panel("close", "raw")[104][tail].isna().all()               # 기본 cut='lifecycle' → 꼬리 NaN
    assert m[last] and not m[tail].any()                                      # 그러나 tradable 아님
    before = m.loc[:last]
    excl = (before.index >= EXCLUSION_104[0]) & (before.index <= EXCLUSION_104[1])
    assert before[~excl].all() and not before[excl].any()                     # 그 전은 exclusions 구간만 빼고 True
    assert not m[cv.dates()[cv.dates() > last][10:]].any()
    # 반례: close notna 만으로 만든 마스크는 꼬리를 True 로 둠(lifecycle 없으면 틀림)
    assert c.notna()[tail].all()
    # 수익률 파일은 lifecycle 마스킹 안 함(publish 에서 적용) → 꼬리에 값 있음
    assert cv.get_returns()[104][tail].notna().all()


def test_relist_overlap_lifecycle_and_lineage_option():
    """옛 id(105)/새 id(106) 겹침: T1 최종 규칙 = 새 id 의 firstTradeDate 를 lineage effectiveDate 로 올림(firstPriceDate 는
    이어붙인 과거 시작). tradable 은 firstTradeDate 기준 → 겹침 구간에 동시 True 0. priced 는 firstPriceDate 기준 → 새 id 도 True.
    apply_lineage 옵션은 같은 결과(무해). 반례: firstTradeDate 를 firstPriceDate 로 되돌린 lifecycle 이면 동시 True 188 일."""
    cv = loader()
    m = cv.get_mask("tradable")
    pr = cv.get_mask("priced")
    last, relist = pd.Timestamp(LAST_TRADE_105), pd.Timestamp(RELIST_106)
    overlap = cv.dates()[cv.dates() <= last]
    assert m.loc[overlap, 105].all() and not m.loc[overlap, 106].any()          # tradable: 옛 id 만
    assert m.loc[cv.dates() >= relist, 106].all() and not m.loc[cv.dates() > last, 105].any()
    assert pr.loc[overlap, 105].all() and pr.loc[overlap, 106].all()            # priced: 둘 다(이어붙인 과거)
    c = cv.get_panel("close", "raw")
    assert nan_equal(c.loc[overlap, 105].to_numpy(), c.loc[overlap, 106].to_numpy())   # 이어붙인 과거 동일
    assert int((m[105] & m[106]).sum()) == 0
    m2 = cv.get_mask("tradable", apply_lineage=True)
    assert nan_equal(m2.to_numpy(), m.to_numpy())
    # 반례: firstTradeDate 를 올리지 않은 lifecycle → 겹침 구간 동시 True, apply_lineage 가 이를 자름
    bad = workdir() / "lc_not_lifted"
    shutil.copytree(fixture("ok"), bad, dirs_exist_ok=True)
    lc = pd.read_parquet(fixture("ok") / "master" / "lifecycle.parquet")
    lc.loc[lc.tradingItemId == 106, "firstTradeDate"] = lc.loc[lc.tradingItemId == 106, "firstPriceDate"]
    lc.to_parquet(bad / "master" / "lifecycle.parquet", index=False)
    cvb = CacheV21(bad)
    mb = cvb.get_mask("tradable")
    assert int((mb[105] & mb[106]).sum()) == len(overlap) == 188
    mb2 = cvb.get_mask("tradable", apply_lineage=True)
    assert int((mb2[105] & mb2[106]).sum()) == 0 and nan_equal(mb2.to_numpy(), m.to_numpy())


def test_universe_membership_bounds():
    """universe 구간(fromDate·toDate 포함) → bool 경계 정확, 여러 구간·교집합 헬퍼."""
    cv = loader()
    u = cv.get_mask("universe:Top500")
    for tid, wins in TOP500.items():
        exp = pd.Series(False, index=cv.dates())
        for f, t in wins:
            exp.loc[f:t] = True
        assert nan_equal(u[tid].to_numpy(), exp.to_numpy()), tid
    f, t = TOP500[102][0]
    f, t = pd.Timestamp(f), pd.Timestamp(t)
    idx = cv.dates()
    assert u.loc[f, 102] and not u.loc[idx[idx < f][-1], 102]
    assert u.loc[t, 102] and not u.loc[idx[idx > t][0], 102]
    both = cv.get_mask("tradable&universe:Top500")
    assert nan_equal(both.to_numpy(), (cv.get_mask("tradable") & u).to_numpy())
    assert nan_equal(cv.tradable_universe("Top500").to_numpy(), both.to_numpy())
    assert nan_equal(cv.get_mask(["tradable", "Top500"]).to_numpy(), both.to_numpy())
    assert cv.get_mask("universe:ALL").loc[:, [101, 102, 103, 104, 106]].all().all()
    try:
        cv.get_mask("universe:Top9")
        raise AssertionError("unknown universe accepted")
    except KeyError:
        pass


def test_halfopen_interval_boundaries():
    """구간 lookup: validFrom 당일 포함, validTo 당일 미포함(다음 구간 값). 점 lookup factor_at 과 패널 일치."""
    cv = loader()
    d = pd.Timestamp(SPLITS_101[1][0])          # 2023-03-15: [2022-07-01, 2023-03-15) 1.5 → [2023-03-15, NaT) 1.0
    day_before = cv.dates()[cv.dates() < d][-1]
    got = cv.factor_at("split", [101, 101, 101, 101], [day_before, d, "2022-06-30", "2022-07-01"])
    assert got.tolist() == [1.5, 1.0, 3.0, 1.5]
    # 비거래일(주말·휴장일)도 lookup 가능(dense)
    assert cv.factor_at("split", [101], ["2022-07-03"]).tolist() == [1.5]
    assert cv.factor_at("split", [101], ["1950-01-01"]).tolist() == [3.0]      # 1900 부터 열림
    assert cv.factor_at("dividend", [102], ["2023-01-02"]).tolist() == [1.0]  # 휴장일 ex 당일부터 새 구간
    # dense 패널 == 행 기반 패널(관측 셀)
    dense = cv.get_factor("dividend", dense=True)
    rows = cv.get_factor("dividend")
    obs = rows.notna()
    assert nan_equal(dense.to_numpy()[obs.to_numpy()], rows.to_numpy()[obs.to_numpy()])
    assert dense.notna().all().all()
    # 반례: 패널 밖 id 는 FactorGapError
    try:
        cv.factor_at("split", [999], ["2022-07-01"])
        raise AssertionError("unknown id accepted")
    except FactorGapError:
        pass


def test_factor_gap_and_overlap_fixtures_raise():
    """빈틈·첫 구간 누락·겹침 fixture 에서 예외. 정상 fixture 는 통과(self-test 대조)."""
    assert CacheV21(fixture("ok")).validate_factors()["split"]["gaps"] == 0
    try:
        CacheV21(fixture("factor_gap")).get_panel("close", "split_adj")
        raise AssertionError("gap accepted")
    except FactorGapError as e:
        assert "gaps" in str(e)
    # 검증 끄면 구간 검사는 통과하지만 lookup 에서 잡힘
    try:
        CacheV21(fixture("factor_gap"), validate=False).get_panel("close", "split_adj")
        raise AssertionError("gap accepted in lookup")
    except FactorGapError as e:
        assert "no interval" in str(e)
    cvm = CacheV21(fixture("factor_missing_prefirst"))
    v = cvm.validate_factors()                                   # 구간 자체는 이어짐(1900 시작 규칙만 위반)
    assert v["split"]["gaps"] == 0 and v["split"]["ids_first_from_after_1900"] == 1
    try:
        cvm.get_panel("close", "split_adj")
        raise AssertionError("pre-first lookup accepted")
    except FactorGapError as e:
        assert "no interval" in str(e)
    assert cvm.get_panel("close", "raw").shape == (501, 6)         # raw 는 계수 불필요
    try:
        CacheV21(fixture("factor_overlap")).get_panel("close", "div_adj")
        raise AssertionError("overlap accepted")
    except FactorOverlapError:
        pass
    assert CacheV21(fixture("factor_overlap"), validate=False).validate_factors(strict=False)["dividend"]["overlaps"] == 1


def test_duplicate_rows_rejected():
    """(id, date) 중복 행·returns 달력 밖 행은 계약 위반으로 거부."""
    bad = workdir() / "bad_dup"
    shutil.copytree(fixture("ok"), bad, dirs_exist_ok=True)
    df = pd.read_parquet(fixture("ok") / "base_data.parquet")
    pd.concat([df, df.iloc[[10]]]).sort_values(["tradingItemId", "date"]).to_parquet(bad / "base_data.parquet", index=False)
    try:
        CacheV21(bad).dates()
        raise AssertionError("duplicate rows accepted")
    except ContractError as e:
        assert "duplicate" in str(e)
    bad2 = workdir() / "bad_ret"
    shutil.copytree(fixture("ok"), bad2, dirs_exist_ok=True)
    r = pd.read_parquet(fixture("ok") / "derived" / "returns_total.parquet")
    r.loc[r.index[5], "date"] = pd.Timestamp("2021-12-31")
    r.to_parquet(bad2 / "derived" / "returns_total.parquet", index=False)
    try:
        CacheV21(bad2).get_returns()
        raise AssertionError("out-of-calendar return accepted")
    except ContractError as e:
        assert "calendar" in str(e)


def test_export_wide_roundtrip():
    """export_wide 파일 왕복: 값 동일, 열 이름 str(id), index 컬럼 '__index_level_0__' timestamp[ns], v1 파일명(VWAP)."""
    cv = loader()
    out = workdir() / "export"
    m = cv.export_wide(out, fields=("open", "high", "low", "close", "vwap", "volume"), basis="split_adj",
                       masks=("tradable", "tradable&universe:Top500"), universes=("Top500",), factors=True)
    files = set(m["files"])
    assert {"BaseData/close.parquet", "BaseData/VWAP.parquet", "BaseData/volume.parquet", "Returns/returns_total.parquet",
            "Mask/tradable.parquet", "Mask/tradable_Top500.parquet", "Universe/Top500.parquet",
            "Factor/splitFactor.parquet", "Factor/divAdjFactor.parquet"} <= files
    s = pq.read_schema(out / "BaseData" / "close.parquet")
    assert "__index_level_0__" in s.names and str(s.field("__index_level_0__").type) == "timestamp[ns]"
    assert s.names[:6] == ["101", "102", "103", "104", "105", "106"] and str(s.field("101").type) == "float"
    back = CacheV21.read_wide(out / "BaseData" / "close.parquet")
    assert list(back.columns) == ["101", "102", "103", "104", "105", "106"] and isinstance(back.index, pd.DatetimeIndex)
    assert nan_equal(back.to_numpy(), cv.get_panel("close", "split_adj").to_numpy())
    assert nan_equal(CacheV21.read_wide(out / "BaseData" / "VWAP.parquet").to_numpy(), cv.get_panel("vwap", "split_adj").to_numpy())
    assert nan_equal(CacheV21.read_wide(out / "BaseData" / "volume.parquet").to_numpy(), cv.get_panel("volume", "split_adj").to_numpy())
    assert nan_equal(CacheV21.read_wide(out / "Returns" / "returns_total.parquet").to_numpy(), cv.get_returns(cut="lifecycle").to_numpy())
    mk = CacheV21.read_wide(out / "Mask" / "tradable.parquet")
    assert mk.dtypes.iloc[0] == bool and nan_equal(mk.to_numpy(), cv.get_mask("tradable").to_numpy())
    assert nan_equal(CacheV21.read_wide(out / "Universe" / "Top500.parquet").to_numpy(), cv.get_mask("universe:Top500").to_numpy())
    assert nan_equal(CacheV21.read_wide(out / "Factor" / "splitFactor.parquet").to_numpy(), cv.get_factor("split").to_numpy())
    meta = json.loads((out / "EXPORT.json").read_text())
    assert meta["n_dates"] == 501 and meta["n_ids"] == 6 and all(len(v["sha256"]) == 64 for v in meta["files"].values())
    # 두 번 export 하면 비트 동일(sha256) — parity 절차의 전제
    m2 = CacheV21(fixture("ok")).export_wide(workdir() / "export2", fields=("close",), basis="split_adj", masks=())
    assert m2["files"]["BaseData/close.parquet"]["sha256"] == m["files"]["BaseData/close.parquet"]["sha256"]
    # compare_wide: 동일 파일 불일치 0, 한 셀 바꾼 파일은 정확히 1 셀 불일치(반례)
    cmp_same = CacheV21.compare_wide(out / "BaseData" / "close.parquet", workdir() / "export2" / "BaseData" / "close.parquet")
    assert cmp_same["cells_mismatch"] == 0 and cmp_same["cells"] == 501 * 6 and cmp_same["symbols_only_a"] == 0
    tampered = CacheV21.read_wide(out / "BaseData" / "close.parquet")
    tampered.iloc[10, 0] = tampered.iloc[10, 0] * (1 + 1e-6)
    tampered.to_parquet(workdir() / "tampered.parquet")
    cmp_bad = CacheV21.compare_wide(out / "BaseData" / "close.parquet", workdir() / "tampered.parquet")
    assert cmp_bad["cells_mismatch"] == 1 and cmp_bad["examples"][0][1] == "101" and 0 < cmp_bad["max_rel_diff"] < 1e-5
    assert CacheV21.compare_wide(out / "BaseData" / "close.parquet", workdir() / "tampered.parquet", rtol=1e-5)["cells_mismatch"] == 0


def test_adjusted_dtype_and_operation_order():
    """parity 규칙: 조정가 = float32(float64 연산). div_adj = (raw×df)/sf 순서. 다른 순서와 float32 차이가 날 수 있음을 인지."""
    cv = loader()
    raw = cv.get_panel("close", "raw", as_array=True, cut=None)
    sf = cv.get_factor("split", as_array=True)
    df_ = cv.get_factor("dividend", as_array=True)
    exp = ((raw.astype(np.float64) * df_) / sf).astype(np.float32)
    got = cv.get_panel("close", "div_adj", as_array=True, cut=None)
    assert got.dtype == np.float32 and nan_equal(got, exp)
    exp_sa = (raw.astype(np.float64) / sf).astype(np.float32)
    assert nan_equal(cv.get_panel("close", "split_adj", as_array=True, cut=None), exp_sa)
    # float32 로 곱하고 나누면(잘못된 dtype 경로) 다른 비트가 나올 수 있음 → 규칙을 고정하는 이유
    wrong = (raw * df_.astype(np.float32)) / sf.astype(np.float32)
    assert wrong.dtype == np.float32
    # (차이가 0 일 수도 있으므로 단언하지 않고 기록만)
    n_diff = int(np.sum((wrong != got) & ~np.isnan(got)))
    test_adjusted_dtype_and_operation_order.note = f"float32-path cells differing from float64-path: {n_diff}"


def test_missing_lifecycle_policy():
    """lifecycle 에 없는 id: open(경고)·exclude·error 정책."""
    bad = workdir() / "lc_missing"
    shutil.copytree(fixture("ok"), bad, dirs_exist_ok=True)
    lc = pd.read_parquet(fixture("ok") / "master" / "lifecycle.parquet")
    lc[lc.tradingItemId != 104].to_parquet(bad / "master" / "lifecycle.parquet", index=False)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        m_open = CacheV21(bad).get_mask("tradable")
    assert any("missing from lifecycle" in str(x.message) for x in w)
    tail = m_open.index[m_open.index > LAST_TRADE_104][:10]
    assert m_open.loc[tail, 104].all()                                        # open: 꼬리가 True 로 남음(위험) → 경고
    m_ex = CacheV21(bad, missing_lifecycle="exclude").get_mask("tradable")
    assert not m_ex[104].any()
    try:
        CacheV21(bad, missing_lifecycle="error").get_mask("tradable")
        raise AssertionError("error policy did not raise")
    except ContractError:
        pass


def test_row_group_pruning_consistency():
    """여러 row group 파일에서 id 부분집합·날짜 범위(비거래일 경계 포함) 로딩 == 전체 로딩의 조각. 빈 선택도 안전."""
    rg = workdir() / "rg_small"
    shutil.copytree(fixture("ok"), rg, dirs_exist_ok=True)
    df = pd.read_parquet(fixture("ok") / "base_data.parquet")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), rg / "base_data.parquet", row_group_size=300, compression="zstd")
    r = pd.read_parquet(fixture("ok") / "derived" / "returns_total.parquet")
    pq.write_table(pa.Table.from_pandas(r, preserve_index=False), rg / "derived" / "returns_total.parquet", row_group_size=300)
    assert pq.ParquetFile(rg / "base_data.parquet").metadata.num_row_groups > 5
    full = CacheV21(fixture("ok"))
    sub = CacheV21(rg, ids=[104, 101], start="2022-07-02", end="2023-06-18")      # 토요일 시작·일요일 끝
    sub.dates()                                                                    # 좌표 로딩(row group 선택 포함)
    assert sub._groups is not None and len(sub._groups) < pq.ParquetFile(rg / "base_data.parquet").metadata.num_row_groups
    ref_dates = full.dates()[(full.dates() >= "2022-07-02") & (full.dates() <= "2023-06-18")]
    assert list(sub.dates()) == list(ref_dates) and sub.dates()[0] == pd.Timestamp("2022-07-05") and sub.dates()[-1] == pd.Timestamp("2023-06-16")
    for f, b in (("close", "div_adj"), ("volume", "split_adj"), ("open", "raw")):
        assert nan_equal(sub.get_panel(f, b).to_numpy(), full.get_panel(f, b).loc[ref_dates, [104, 101]].to_numpy()), (f, b)
    assert nan_equal(sub.get_returns().to_numpy(), full.get_returns().loc[ref_dates, [104, 101]].to_numpy())
    assert nan_equal(sub.get_mask("tradable").to_numpy(), full.get_mask("tradable").loc[ref_dates, [104, 101]].to_numpy())
    # 전체 파일(여러 row group) == 단일 row group 파일
    whole = CacheV21(rg)
    assert nan_equal(whole.get_panel("close", "split_adj").to_numpy(), full.get_panel("close", "split_adj").to_numpy())
    assert nan_equal(whole.get_returns().to_numpy(), full.get_returns().to_numpy())
    # 존재하지 않는 id 만 요청 → 전부 NaN, 예외 없음
    none = CacheV21(rg, ids=[999])
    assert none.get_panel("close", "split_adj").isna().all().all() and none.shape == (501, 1)


def test_exclusions_and_priced_masks():
    """tradable_exclusions: (id, [fromDate, toDate]) 구간 False(양끝 포함), priced 는 영향 없음. 파일 없으면 경고 없이 그대로.
    priced = firstPriceDate ≤ d ≤ ciqLastCloseDate & close notna → 승계사 가격 꼬리 포함, tradable ⊆ priced."""
    cv = loader()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        tr = cv.get_mask("tradable")
        pr = cv.get_mask("priced")
    assert not [x for x in w if "lifecycle" in str(x.message)]
    f, t = pd.Timestamp(EXCLUSION_104[0]), pd.Timestamp(EXCLUSION_104[1])
    idx = cv.dates()
    ex_days = idx[(idx >= f) & (idx <= t)]
    assert len(ex_days) == 23 and not tr.loc[ex_days, 104].any() and pr.loc[ex_days, 104].all()
    assert tr.loc[idx[idx < f][-1], 104] and tr.loc[idx[idx > t][0], 104]                # 경계 바깥은 True
    assert tr.loc[f, 104] == False and tr.loc[t, 104] == False                              # noqa: E712 - 양끝 포함
    assert cv.diagnostics["tradable_excluded_cells"] == 23 and cv.diagnostics["tradable_exclusion_rows"] == 1
    # priced ⊇ tradable, 꼬리(lastTradeDate < d ≤ ciqLastCloseDate)는 priced 만 True
    assert not (tr.to_numpy() & ~pr.to_numpy()).any()
    tail = idx[idx > LAST_TRADE_104][:10]
    assert pr.loc[tail, 104].all() and not tr.loc[tail, 104].any()
    assert not pr.loc[idx[idx > LAST_TRADE_104][10:], 104].any()                             # ciqLastCloseDate 뒤 False
    assert not pr.loc[NULL_CLOSE_103, 103] and pr[105].sum() == 188                          # close NaN → False
    # 다른 id 는 exclusions 영향 없음
    assert (tr.drop(columns=[104]).to_numpy() == loader().get_mask("tradable").drop(columns=[104]).to_numpy()).all()
    # 반례 1: exclusions 파일이 없으면 3 월도 True, 경고 없음
    no_ex = workdir() / "no_exclusions"
    shutil.copytree(fixture("ok"), no_ex, dirs_exist_ok=True)
    (no_ex / "master" / "tradable_exclusions.parquet").unlink()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        tr0 = CacheV21(no_ex).get_mask("tradable")
    assert tr0.loc[ex_days, 104].all() and not w
    assert nan_equal(tr0.drop(columns=[104]).to_numpy(), tr.drop(columns=[104]).to_numpy())
    # 반례 2: toDate 를 하루 앞당긴 파일이면 03-31 이 True 로 돌아옴(포함 경계가 실제로 검사됨)
    ex_short = workdir() / "ex_short"
    shutil.copytree(fixture("ok"), ex_short, dirs_exist_ok=True)
    ex = pd.read_parquet(fixture("ok") / "master" / "tradable_exclusions.parquet")
    ex["toDate"] = ex["toDate"] - pd.Timedelta(days=1)
    ex.to_parquet(ex_short / "master" / "tradable_exclusions.parquet", index=False)
    assert CacheV21(ex_short).get_mask("tradable").loc[t, 104]
    # export 에 priced 마스크 포함
    m = cv.export_wide(workdir() / "export_priced", fields=("close",), masks=("priced", "tradable&priced"))
    assert nan_equal(CacheV21.read_wide(workdir() / "export_priced" / "Mask" / "priced.parquet").to_numpy(), pr.to_numpy())
    assert "Mask/tradable_priced.parquet" in m["files"]


def test_universe_v1rule_and_universe_root():
    """membership_v1rule → 'universe_v1rule:<name>'; universe_root 를 T5 스테이지처럼 다른 디렉토리로 지정 가능."""
    cv = loader()
    u = cv.get_mask("universe:Top500")
    u1 = cv.get_mask("universe_v1rule:Top500")
    assert u1[102].all() and not u[102].all() and int(u1[102].sum()) == 501            # v1rule: 102 전 기간
    assert nan_equal(u.drop(columns=[102]).to_numpy(), u1.drop(columns=[102]).to_numpy())
    assert nan_equal(cv.tradable_universe("Top500", v1rule=True).to_numpy(), (cv.get_mask("tradable") & u1).to_numpy())
    # universe_root 분리(T5 스테이지 모사): root 에 universe/ 가 없어도 universe_root/universe/ 에서 읽음
    t5 = workdir() / "t5_stage"
    shutil.copytree(fixture("ok") / "universe", t5 / "universe", dirs_exist_ok=True)
    no_u = workdir() / "no_universe"
    shutil.copytree(fixture("ok"), no_u, dirs_exist_ok=True)
    shutil.rmtree(no_u / "universe")
    cv2 = CacheV21(no_u, universe_root=t5)
    assert nan_equal(cv2.get_mask("universe_v1rule:Top500").to_numpy(), u1.to_numpy())
    try:
        CacheV21(no_u).get_mask("universe:Top500")
        raise AssertionError("missing universe file accepted")
    except FileNotFoundError:
        pass
    m = cv.export_wide(workdir() / "export_v1rule", fields=("close",), masks=(), universes=("Top500",), universes_v1rule=("Top500",))
    assert {"Universe/Top500.parquet", "Universe_v1rule/Top500.parquet"} <= set(m["files"])
    assert nan_equal(CacheV21.read_wide(workdir() / "export_v1rule" / "Universe_v1rule" / "Top500.parquet").to_numpy(), u1.to_numpy())


def test_marketcap_panel_and_export():
    """derived/marketcap.parquet(long) → get_panel('marketCap_USD'/'sharesOutstanding'): raw 만, 달력 밖 행은 버리고 기록,
    marketCap == raw close × raw shares == 진짜 조정종가 × 조정주수(기준 무관). export 는 v1 이름 BaseData/marketCap_USD.parquet."""
    cv = loader()
    cap = cv.get_panel("marketCap_USD")
    sh = cv.get_panel("sharesOutstanding")
    raw = cv.get_panel("close", "raw").astype(np.float64)
    sf = cv.get_factor("split")
    truth = truth_adj()
    assert cap.dtypes.iloc[0] == np.float64 and cap.shape == (501, 6)
    obs = raw.notna().to_numpy()
    assert (cap.notna().to_numpy() == obs).all()                                     # close 있는 셀만
    assert np.allclose(cap.to_numpy()[obs], (raw * sh).to_numpy()[obs], rtol=1e-12)
    for tid in (101, 102, 104):
        exp = truth[tid].reindex(cap.index) * SHARES_ADJ[tid]                        # 기준 무관: 조정종가 × 조정주수
        got = cap[tid]
        ok = got.notna() & exp.notna()
        if tid == 104:
            ok &= cap.index <= LAST_TRADE_104                                          # 꼬리는 승계사 가격(진짜 경로 아님)
        assert np.allclose(got[ok], exp[ok], rtol=3e-7), tid                            # float32 종가 반올림 이내
        assert np.allclose((sh[tid] * sf[tid])[ok], SHARES_ADJ[tid], rtol=1e-12)        # raw 주수 × sf = 조정주수
    d = pd.Timestamp(SPLITS_101[0][0])
    prev = cap.index[cap.index.get_loc(d) - 1]
    assert abs(sh.loc[d, 101] / sh.loc[prev, 101] - 2.0) < 1e-9                        # 분할일 raw 주수 2 배
    assert cv.diagnostics["marketCap_USD_rows"] == {"rows_kept": 2561, "rows_outside_calendar": 1, "rows_unknown_id": 0}
    assert pd.Timestamp(MARKETCAP_OUTSIDE_ROW[0]) not in cap.index
    for basis in ("split_adj", "div_adj"):
        try:
            cv.get_panel("marketCap_USD", basis)
            raise AssertionError("marketCap basis accepted")
        except ValueError:
            pass
    m = cv.export_wide(workdir() / "export_cap", fields=("close", "marketCap_USD"), basis="split_adj", masks=())
    assert "BaseData/marketCap_USD.parquet" in m["files"] and m["files"]["BaseData/marketCap_USD.parquet"]["dtype"] == "float64"
    assert nan_equal(CacheV21.read_wide(workdir() / "export_cap" / "BaseData" / "marketCap_USD.parquet").to_numpy(), cap.to_numpy())
    # 파일이 없으면 available_fields 에서 빠지고 요청 시 FileNotFoundError
    no_mc = workdir() / "no_marketcap"
    shutil.copytree(fixture("ok"), no_mc, dirs_exist_ok=True)
    (no_mc / "derived" / "marketcap.parquet").unlink()
    cv2 = CacheV21(no_mc)
    assert "marketCap_USD" not in cv2.available_fields
    try:
        cv2.get_panel("marketCap_USD")
        raise AssertionError("missing marketcap accepted")
    except FileNotFoundError:
        pass


def test_group_panels_pit_vs_broadcast():
    """classification_history → sector/industry 코드 패널. broadcast_current = 최신 분류 전 기간 방송(v1 동일),
    pit = effectiveFrom ≤ d ≤ effectiveTo(포함) 인 분류만(관측 이전 NaN). 코드 체계 = build_group_panels.py
    (sector v1 순서 1..11, industry 라벨 정렬 1..K, 미분류 NaN, float32). 반례: 두 모드가 정확히 예상 셀에서만 다름,
    industry 가 두 sector 에 걸치면 오류, 모르는 mode 오류."""
    cv = loader()
    labels = cv.group_labels()
    assert labels["sector"][:3] == ["Energy", "Information Technology", "Industrials"] and len(labels["sector"]) == 11
    assert labels["industry"] == ["Banks", "Building Products", "Food Products", "Machinery", "Software"]
    IT, IND, STAPLES, FIN = 2.0, 3.0, 7.0, 5.0
    bc = cv.get_group_panel("sector", "broadcast_current")
    pit = cv.get_group_panel("sector", "pit")
    ind_bc = cv.get_group_panel("industry", "broadcast_current")
    ind_pit = cv.get_group_panel("industry", "pit")
    idx = cv.dates()
    assert bc.dtypes.iloc[0] == np.float32 and bc.shape == (501, 6) and list(bc.columns) == list(cv.ids())
    # broadcast: 최신 분류를 전 기간(close 유무 무관)
    assert bc[101].eq(IND).all() and bc[102].eq(STAPLES).all() and bc[103].eq(IT).all() and bc[105].eq(FIN).all() and bc[106].eq(FIN).all()
    assert bc[104].isna().all() and ind_bc[104].isna().all()                                    # 라벨 null → 미분류
    assert ind_bc[103].eq(5.0).all() and ind_bc[101].eq(4.0).all()                             # Software=5, Machinery=4
    # pit: 103 은 변경일부터 새 코드, 옛 effectiveTo(휴장일) 포함 경계
    old_to, new_from = pd.Timestamp(CLASS_CHANGE_103[0]), pd.Timestamp(CLASS_CHANGE_103[1])
    before, after = idx[idx <= old_to], idx[idx >= new_from]
    assert len(before) + len(after) == 501 and new_from in idx and old_to not in idx
    assert pit.loc[before, 103].eq(IND).all() and pit.loc[after, 103].eq(IT).all()
    assert ind_pit.loc[before, 103].eq(2.0).all() and ind_pit.loc[after, 103].eq(5.0).all()   # Building Products=2
    assert pit[105].isna().all()                                                               # current_only: 관측이 패널 뒤
    relist = pd.Timestamp(RELIST_106)
    assert pit.loc[idx < relist, 106].isna().all() and pit.loc[idx >= relist, 106].eq(FIN).all()
    assert nan_equal(pit[[101, 102, 104]].to_numpy(), bc[[101, 102, 104]].to_numpy())
    # 반례: 두 모드의 차이 셀 = 103 변경 전 + 105 전부 + 106 관측 전, 정확히 그만큼
    diff = ~((pit.to_numpy() == bc.to_numpy()) | (pit.isna().to_numpy() & bc.isna().to_numpy()))
    assert int(diff.sum()) == len(before) + 501 + int((idx < relist).sum())
    assert cv.diagnostics["group_sector_pit"]["ids_classified"] == 4 and cv.diagnostics["group_sector_broadcast_current"]["ids_classified"] == 5
    # export: 엔진 base.groups 형식
    out = workdir() / "export_groups"
    m = cv.export_wide(out, fields=("close", "marketCap_USD"), masks=("tradable",), groups=True)
    assert {"groups/sector.parquet", "groups/industry.parquet", "groups/sectorIx.parquet", "groups/industryIx.parquet"} <= set(m["files"])
    gs = pq.ParquetFile(out / "groups" / "sector.parquet")
    cs = pq.read_schema(out / "BaseData" / "close.parquet")
    assert gs.schema_arrow.names == cs.names and str(gs.schema_arrow.field("101").type) == "float"
    assert gs.metadata.row_group(0).column(0).compression == "SNAPPY"
    back = CacheV21.read_wide(out / "groups" / "sector.parquet")
    assert nan_equal(back.to_numpy(), bc.to_numpy()) and list(back.columns) == [str(i) for i in cv.ids()]
    six = pq.read_table(out / "groups" / "sectorIx.parquet").to_pandas()
    assert six.columns.tolist() == ["sectorIx"] and six.sectorIx.iloc[0] is None and six.sectorIx.tolist()[1:] == labels["sector"]
    iix = pq.read_table(out / "groups" / "industryIx.parquet").to_pandas()
    assert iix.industryIx.tolist() == [None] + labels["industry"]
    gm = json.loads((out / "groups" / "groups_manifest.json").read_text())
    assert gm["mode"] == "broadcast_current" and gm["labels"] == {"sector": 11, "industry": 5}
    # pit export 옵션
    m2 = cv.export_wide(workdir() / "export_groups_pit", fields=("close",), masks=(), groups=True, groups_mode="pit")
    assert nan_equal(CacheV21.read_wide(workdir() / "export_groups_pit" / "groups" / "sector.parquet").to_numpy(), pit.to_numpy())
    assert m2["files"]["groups/sector.parquet"]["sha256"] != m["files"]["groups/sector.parquet"]["sha256"]
    # 결정적 쓰기: 같은 입력 → 같은 sha256
    m3 = CacheV21(fixture("ok")).export_wide(workdir() / "export_groups2", fields=("close",), masks=(), groups=True)
    assert m3["files"]["groups/sector.parquet"]["sha256"] == m["files"]["groups/sector.parquet"]["sha256"]
    # 반례: industry 가 두 sector 에 걸침 → ContractError; 모르는 mode/kind → 오류
    bad = workdir() / "bad_nesting"
    shutil.copytree(fixture("ok"), bad, dirs_exist_ok=True)
    ch = pd.read_parquet(fixture("ok") / "master" / "classification_history.parquet")
    ch.loc[ch.tradingItemId == 102, "simpleIndustryDescription"] = "Machinery"      # Machinery 가 Industrials·Consumer Staples 둘 다
    ch.to_parquet(bad / "master" / "classification_history.parquet", index=False)
    try:
        CacheV21(bad).get_group_panel("sector")
        raise AssertionError("nesting violation accepted")
    except ContractError as e:
        assert "nested" in str(e)
    for bad_call in (lambda: cv.get_group_panel("sector", "latest"), lambda: cv.get_group_panel("country")):
        try:
            bad_call()
            raise AssertionError("bad group call accepted")
        except (ValueError, KeyError):
            pass
    # 반례: v1 순서에 없는 sector 라벨 → 오류(코드 체계 고정)
    bad2 = workdir() / "bad_sector_label"
    shutil.copytree(fixture("ok"), bad2, dirs_exist_ok=True)
    ch = pd.read_parquet(fixture("ok") / "master" / "classification_history.parquet")
    ch.loc[ch.tradingItemId == 101, "sector"] = "Crypto"
    ch.to_parquet(bad2 / "master" / "classification_history.parquet", index=False)
    try:
        CacheV21(bad2).get_group_panel("sector")
        raise AssertionError("unknown sector label accepted")
    except ContractError as e:
        assert "Crypto" in str(e)


def test_lifecycle_cut_panels_and_export():
    """cut='lifecycle'(기본): lastTradeDate 이후 셀 NaN(엔진 입력 절단본), firstTradeDate 이전은 유지, exclusions 구간은 절단 안 함.
    export 기본 절단·manifest 에 절단 셀 수, --no-cut(cut=None) 이면 값 존재(반례). returns 도 export 에서 절단."""
    cv = loader()
    idx = cv.dates()
    last = pd.Timestamp(LAST_TRADE_104)
    tail = idx[idx > last][:10]
    raw_cut = cv.get_panel("close", "raw")
    raw_full = cv.get_panel("close", "raw", cut=None)
    assert raw_cut[104][tail].isna().all() and raw_full[104][tail].notna().all()
    assert raw_cut.loc[last, 104] == raw_full.loc[last, 104]                                   # lastTradeDate 당일 유지
    # 새 id 106: firstTradeDate(2022-10-03) 이전 이어붙인 과거는 절단하지 않음(워밍업용)
    relist = pd.Timestamp(RELIST_106)
    assert raw_cut.loc[idx < relist, 106].notna().all()
    # exclusions 구간(104, 2022-03)은 절단 안 함 — 마스크로만
    ex_days = idx[(idx >= EXCLUSION_104[0]) & (idx <= EXCLUSION_104[1])]
    assert raw_cut.loc[ex_days, 104].notna().all() and not cv.get_mask("tradable").loc[ex_days, 104].any()
    # 절단 셀 = 104 꼬리 10 개만(105 는 lastTradeDate 뒤 행 없음)
    diff = raw_full.notna().to_numpy() & raw_cut.isna().to_numpy()
    assert int(diff.sum()) == 10 and cv.diagnostics["cut_cells"]["close:raw"] == 10
    assert cv.diagnostics["cut"]["ids_with_cut"] == 2 and cv.diagnostics["cut"]["ids_missing_lifecycle"] == 0
    # 모든 필드·기준에 같은 절단, volume·marketCap·flag 도
    for f, b in (("volume", "split_adj"), ("open", "div_adj"), ("marketCap_USD", "raw")):
        pc = cv.get_panel(f, b)
        assert pc[104][tail].isna().all() and cv.get_panel(f, b, cut=None)[104][tail].notna().all(), (f, b)
    assert (cv.get_panel("qualityFlag")[104][tail] == -1).all() and (cv.get_panel("qualityFlag", cut=None)[104][tail] == 2).all()
    # returns: 기본 미절단(파일 규약), cut 옵션이면 절단
    assert cv.get_returns()[104][tail].notna().all() and cv.get_returns(cut="lifecycle")[104][tail].isna().all()
    # priced 마스크는 절단과 무관(꼬리 True), tradable 은 원래 False
    assert cv.get_mask("priced")[104][tail].all() and not cv.get_mask("tradable")[104][tail].any()
    # export 기본 절단 + manifest 기록
    out = workdir() / "export_cut"
    m = cv.export_wide(out, fields=("close", "volume"), basis="split_adj", masks=("tradable",))
    back = CacheV21.read_wide(out / "BaseData" / "close.parquet")
    assert back["104"][tail].isna().all() and m["cut"]["mode"] == "lifecycle"
    assert m["cut"]["cells_removed"] == {"close": 10, "volume": 10, "returns_total": 10} and m["cut"]["cells_removed_total"] == 30
    assert CacheV21.read_wide(out / "Returns" / "returns_total.parquet")["104"][tail].isna().all()
    # 반례: cut=None(--no-cut) 이면 꼬리 값 존재
    out2 = workdir() / "export_nocut"
    m2 = cv.export_wide(out2, fields=("close",), basis="split_adj", masks=(), cut=None)
    back2 = CacheV21.read_wide(out2 / "BaseData" / "close.parquet")
    assert back2["104"][tail].notna().all() and m2["cut"] == {"mode": None, "cells_removed": {}}
    assert nan_equal(back2.to_numpy(), cv.get_panel("close", "split_adj", cut=None).to_numpy())
    # lifecycle 파일이 없으면 절단 없이 경고
    no_lc = workdir() / "no_lifecycle"
    shutil.copytree(fixture("ok"), no_lc, dirs_exist_ok=True)
    (no_lc / "master" / "lifecycle.parquet").unlink()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        c = CacheV21(no_lc).get_panel("close", "raw")
    assert c[104][tail].notna().all() and any("cut='lifecycle' has no effect" in str(x.message) for x in w)
    try:
        cv.get_panel("close", "raw", cut="events")
        raise AssertionError("bad cut accepted")
    except ValueError:
        pass


def _ledger_fixture(root: Path, extra_rows=None) -> pd.DataFrame:
    """R1 원장(rc2 §3-1 스키마) 합성: 101 의 분할(2022-07-01 2:1) 전 배당·후 스핀(자회사 102), 102 의 분할 후 스핀(자회사 101, 자회사 계수 1.5),
    같은 날 현금+주식(2023-03-15 = 101 3:2 주식배당일), merger/relist 행(export 제외), 패널 밖 부모(999)."""
    T = pd.Timestamp
    NaT = pd.NaT
    rows = [
        # eventId, tid, exDate, eventType, legType, cash, pf, child, ratio, src, value, status, est
        ("101_20220310_cash_dividend_0", 101, "2022-03-10", "cash_dividend", "cash", 1.0, 1.0, None, np.nan, "unknown", np.nan, "ok", False),
        ("101_20220701_split_0", 101, "2022-07-01", "split", "quantity", np.nan, 2.0, None, np.nan, "unknown", np.nan, "ok", False),
        ("101_20220901_spin_off_0", 101, "2022-09-01", "spin_off", "stock", np.nan, 1.0, 102, 0.5, "child_close_ex", 40.0, "ok", False),
        ("102_20220901_spin_off_0", 102, "2022-09-01", "spin_off", "stock", np.nan, 1.0, 101, 0.25, "child_close_ex", 30.0, "ok", False),
        ("101_20230315_stock_dividend_0", 101, "2023-03-15", "stock_dividend", "quantity", np.nan, 1.5, None, np.nan, "unknown", np.nan, "ok", False),
        ("101_20230315_cash_dividend_0", 101, "2023-03-15", "cash_dividend", "cash", 0.3, 1.0, None, np.nan, "unknown", np.nan, "ok", False),
        ("101_20230315_spin_off_0", 101, "2023-03-15", "spin_off", "stock", np.nan, 1.0, 103, 0.2, "child_close_ex", 20.0, "ok", False),
        ("103_20230201_spin_off_0", 103, "2023-02-01", "spin_off", "stock", np.nan, 1.0, 102, 0.1, "event_value", 5.0, "estimated", True),
        ("104_20230616_merger_cash_0", 104, "2023-06-16", "merger_cash", "cash", 80.0, 1.0, None, np.nan, "unknown", np.nan, "ok", False),
        ("105_20221003_relist_0", 105, "2022-10-03", "relist", "quantity", np.nan, 1.0, 106, np.nan, "unknown", np.nan, "ok", False),
        ("999_20220310_cash_dividend_0", 999, "2022-03-10", "cash_dividend", "cash", 1.0, 1.0, None, np.nan, "unknown", np.nan, "ok", False),
        # 종료된 부모(105 lastTradeDate 2022-09-30)의 현금 leg → events 파일 담당, export 제외
        ("105_20221005_cash_dividend_0", 105, "2022-10-05", "cash_dividend", "cash", 2.0, 1.0, None, np.nan, "unknown", np.nan, "ok", False),
        # 금액 없는 현금 leg(벤더 스텝만 있는 표시 행) → 표시용 행으로 export(markerReason no_cash_amount, Y1)
        ("101_20220412_other_0", 101, "2022-04-12", "other", "cash", np.nan, 1.0, None, np.nan, "unknown", np.nan, "unresolved", False),
        # 자회사 패널 밖(999) → childRatioCache NaN 이지만 childRatioRaw 는 보존
        ("102_20230505_spin_off_0", 102, "2023-05-05", "spin_off", "stock", np.nan, 1.0, 999, 0.7, "unknown", np.nan, "unresolved", False),
        # ② 검증된 when-issued 값 → isEstimate false
        ("102_20230606_spin_off_0", 102, "2023-06-06", "spin_off", "stock", np.nan, 1.0, 103, 0.3, "wi_close", 9.0, "ok", False),
    ]
    if extra_rows:
        rows += extra_rows
    df = pd.DataFrame(rows, columns=["eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "parentFactor", "childTradingItemId",
                                     "childRatio", "childValueSource", "childValuePerOldShare", "status", "isEstimate"])
    df["exDate"] = pd.to_datetime(df.exDate)
    df["childTradingItemId"] = df.childTradingItemId.astype("Int64")
    for c, v in (("cashCurrency", "USD"), ("fxToUSD", 1.0), ("subscriptionPrice", np.nan), ("expiryDate", NaT), ("exerciseRatio", np.nan),
                 ("payDate", NaT), ("recordDate", NaT), ("announcedDate", NaT), ("source", "fixture"), ("evidence", ""), ("repairOf", None), ("notes", "")):
        df[c] = v
    df["tradingItemId"] = df.tradingItemId.astype("int64")
    (root / "events").mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), root / "events" / "ca_ledger.parquet")
    return df


def test_export_ca_ledger_units():
    """export_ca_ledger(rc2 §3-3): 단위 환산 항등(cashCache × 현재주식수 == cashRaw × 구주수; childRatioCache × 현재부모주 == childRatio × 구주 × sf_child),
    앵커(split_adj 종가), parentFactorCache 1.0 검사(반례 ContractError), 제외 유형·패널 밖 부모 드롭, 추정 fallback 환산, export_wide 통합·manifest."""
    root = workdir() / "fx_ledger"
    shutil.copytree(fixture("ok"), root, dirs_exist_ok=True)
    L = _ledger_fixture(root)
    cv = CacheV21(root)
    out = workdir() / "export_ledger"
    m = cv.export_wide(out, fields=("close",), basis="split_adj", masks=(), returns=False)
    rel = "engine/ca_ledger_cache.parquet"
    assert rel in m["files"] and m["ca_ledger"]["ledger_rows"] == len(L) and len(m["ca_ledger"]["ledger_sha256"]) == 64
    E = CacheV21.read_wide  # noqa: F841  (wide 리더는 무관; 엔진 원장은 long)
    E = pd.read_parquet(out / rel)
    assert m["ca_ledger"]["dropped"] == {"excluded_event_types": 2, "stitched_copies_excluded": 0, "events_successor_duplicate": 0,
                                          "events_cash_duplicate": 0, "cash_leg_after_parent_lastTradeDate": 1,
                                          "parent_not_in_panel": 1, "exDate_outside_panel": 0, "no_prior_parent_close": 0, "parent_close_stale_before_exDate": 0}
    # 표시용 행(Y1): 금액 없는 현금 leg 는 제거되지 않고 markerReason no_cash_amount, status unresolved, 값 NaN 으로 남는다
    mk = m["ca_ledger"]["marker_rows"]
    assert mk["exported"] == 1 and mk["by_reason"] == {"no_cash_amount": 1, "no_child": 0, "coacthist_stale_termination": 0} \
        and mk["in_ledger_by_reason"] == {"no_cash_amount": 1, "no_child": 0, "coacthist_stale_termination": 0} \
        and mk["excluded_by_other_rules_by_reason"] == {"no_cash_amount": 0, "no_child": 0, "coacthist_stale_termination": 0} and mk["dropped_key_conflict"] == 0   # D1: third reason (lead 2026-09-21)
    assert len(E) == len(L) - 4 and E.tradingItemId.dtype == object and E.eventId.is_unique and "markerReason" in E.columns
    r_mk = E[E.eventId == "101_20220412_other_0"].iloc[0]
    assert r_mk.markerReason == "no_cash_amount" and r_mk.status == "unresolved" and not bool(r_mk.isEstimate) and np.isnan(r_mk.cashPerShareCache) \
        and pd.isna(r_mk.childTradingItemId) and np.isfinite(r_mk.parentCloseAtExCache) and np.isfinite(r_mk.parentCloseBeforeCache)
    # 자회사가 있는 status unresolved 행(102 → 999, 패널 밖)은 표시용 행이 아니다(childTradingItemId 유지, markerReason null)
    assert E.markerReason.notna().sum() == 1 and E[E.eventId == "102_20230505_spin_off_0"].markerReason.isna().all()
    assert m["ca_ledger"]["rows_by_status"].get("unresolved", 0) == 2
    assert "childRatioRaw" in E.columns and E[E.eventId == "102_20230505_spin_off_0"].childRatioRaw.iloc[0] == 0.7 \
        and np.isnan(E[E.eventId == "102_20230505_spin_off_0"].childRatioCache.iloc[0])
    r_wi = E[E.eventId == "102_20230606_spin_off_0"].iloc[0]
    assert not r_wi.isEstimate and r_wi.childValueSource == "wi_close" and r_wi.childCloseAtExCache == close.loc[pd.Timestamp("2023-06-06"), 103] if False else True
    assert not E.duplicated(["tradingItemId", "exDate", "legType", "childTradingItemId"]).any()
    assert (E.parentFactorCache == 1.0).all()
    sf = cv.get_factor("split", dense=True)                    # (date, id) → splitFactor
    close = cv.get_panel("close", "split_adj", cut="lifecycle")
    cal = cv.dates()
    r_wi = E[E.eventId == "102_20230606_spin_off_0"].iloc[0]
    assert not bool(r_wi.isEstimate) and r_wi.childValueSource == "wi_close" and r_wi.childCloseAtExCache == close.loc[pd.Timestamp("2023-06-06"), 103]

    def prev(ex):
        return cal[cal.get_loc(pd.Timestamp(ex)) - 1]

    def row(eid):
        x = E[E.eventId == eid]
        assert len(x) == 1, eid
        return x.iloc[0]

    old = 100.0
    # 분할 전 배당: sf_before = 3.0 (pre_first: 2 × 1.5) → cashCache = 1/3 ; 항등
    r = row("101_20220310_cash_dividend_0")
    sfb = sf.loc[prev("2022-03-10"), 101]
    assert sfb == 3.0 and abs(r.cashPerShareCache - 1.0 / 3.0) < 1e-12 and abs(r.cashPerShareCache * old * sfb - 1.0 * old) < 1e-9
    # 분할 후 스핀(101 → 102): sf_101(before 09-01) = 1.5, sf_102 = 1.0 → childRatioCache = 0.5 × 1.0 / 1.5
    r = row("101_20220901_spin_off_0")
    sfb, sfc = sf.loc[prev("2022-09-01"), 101], sf.loc[pd.Timestamp("2022-09-01"), 102]
    assert sfb == 1.5 and sfc == 1.0 and abs(r.childRatioCache - 0.5 * sfc / sfb) < 1e-12
    assert abs(r.childRatioCache * old * sfb - 0.5 * old * sfc) < 1e-9, "child unit identity"
    assert r.childCloseAtExCache == close.loc[pd.Timestamp("2022-09-01"), 102] and not r.isEstimate
    assert r.parentCloseAtExCache == close.loc[pd.Timestamp("2022-09-01"), 101] and r.parentCloseBeforeCache == close.loc[prev("2022-09-01"), 101]
    # 자회사 쪽 계수 ≠ 1 (102 → 101, sf_101(09-01) = 1.5): childRatioCache = 0.25 × 1.5 / 1.0
    r = row("102_20220901_spin_off_0")
    assert abs(r.childRatioCache - 0.25 * 1.5 / 1.0) < 1e-12
    # 같은 날 주식배당(3:2) + 현금 + 스핀: sf_before 1.5 / sf_ex 1.0 → cashCache 0.3/1.5, childRatioCache 0.2 × 1/1.5, 분할 행 parentFactorCache 1
    assert abs(row("101_20230315_cash_dividend_0").cashPerShareCache - 0.3 / 1.5) < 1e-12
    assert abs(row("101_20230315_spin_off_0").childRatioCache - 0.2 / 1.5) < 1e-12
    assert row("101_20230315_stock_dividend_0").parentFactorCache == 1.0 and m["ca_ledger"]["value_legs_on_same_day_split"] == 2
    # 추정 fallback: 103 의 2023-02-01 은 close NaN(fixture) → 자회사 102 는 시세 있음 → 관측 종가 사용, isEstimate 는 원장값(True) 유지
    r = row("103_20230201_spin_off_0")
    assert r.childCloseAtExCache == close.loc[pd.Timestamp("2023-02-01"), 102] and bool(r.isEstimate) and r.childValueSource == "event_value"
    # 반례 1: quantity 행 parentFactor 를 계수 테이블과 다르게 → ContractError
    bad = L.copy(); bad.loc[bad.eventId == "101_20220701_split_0", "parentFactor"] = 3.0
    pq.write_table(pa.Table.from_pandas(bad, preserve_index=False), root / "events" / "ca_ledger.parquet")
    try:
        CacheV21(root).export_ca_ledger(workdir() / "bad_ledger.parquet")
        raise AssertionError("mismatching parentFactor accepted")
    except ContractError:
        pass
    # 반례 2: 비-quantity 행에 parentFactor ≠ 1 → ContractError
    bad = L.copy(); bad.loc[bad.eventId == "101_20220310_cash_dividend_0", "parentFactor"] = 2.0
    pq.write_table(pa.Table.from_pandas(bad, preserve_index=False), root / "events" / "ca_ledger.parquet")
    try:
        CacheV21(root).export_ca_ledger(workdir() / "bad_ledger2.parquet")
        raise AssertionError("non-quantity parentFactor != 1 accepted")
    except ContractError:
        pass
    # 반례 3: export 키 중복(같은 id·exDate·legType·child 의 stock 행 2개, eventType 만 다름) → ContractError(빌드 실패); 자회사 없는 stock leg 는 표시용 행
    dup_rows = [("101_20220901_other_0", 101, "2022-09-01", "other", "stock", np.nan, 1.0, 102, 0.5, "child_close_ex", 40.0, "ok", False),
                ("101_20230801_spin_off_0", 101, "2023-08-01", "spin_off", "stock", np.nan, 1.0, None, np.nan, "vendor_implied", 7.0, "estimated", True)]
    L3 = _ledger_fixture(root, extra_rows=dup_rows)
    try:
        CacheV21(root).export_ca_ledger(workdir() / "dup_ledger.parquet")
        raise AssertionError("duplicate export key accepted")
    except ContractError:
        pass
    L4 = _ledger_fixture(root, extra_rows=dup_rows[1:])
    m4 = CacheV21(root).export_ca_ledger(workdir() / "nochild_ledger.parquet")
    # 자회사 id 는 없지만 ③④ 추정값(7.0, status estimated)이 있으면 표시용 행이 아니라 값 leg(Y1-B): childValuePerShareCache = 7.0 / sf_before(101, 2023-07-31 = 1.0)
    assert m4["marker_rows"]["by_reason"] == {"no_cash_amount": 1, "no_child": 0, "coacthist_stale_termination": 0} and m4["rows"] == len(L4) - 4 and m4["value_legs_no_child_id_valued"] == 1
    E4 = pd.read_parquet(workdir() / "nochild_ledger.parquet")
    r4 = E4[E4.eventId == "101_20230801_spin_off_0"].iloc[0]
    assert pd.isna(r4.markerReason) and r4.status == "estimated" and bool(r4.isEstimate) and pd.isna(r4.childTradingItemId) \
        and np.isnan(r4.childRatioCache) and np.isnan(r4.childCloseAtExCache) and r4.childValueSource == "vendor_implied" and r4.childValuePerShareCache == 7.0
    # 값 leg 의 childValuePerShareCache = childValuePerOldShare / sf_before (분할 전 스핀: 40.0 / 1.5 → 101 2022-09-01 은 sf_before 1.5)
    assert abs(E4[E4.eventId == "101_20220901_spin_off_0"].childValuePerShareCache.iloc[0] - 40.0 / 1.5) < 1e-12
    assert E4[E4.legType == "cash"].childValuePerShareCache.isna().all()
    # 같은 행이 status unresolved(값 없음)면 표시용 행
    unres = [("101_20230801_spin_off_0", 101, "2023-08-01", "spin_off", "stock", np.nan, 1.0, None, np.nan, "unknown", np.nan, "unresolved", False)]
    _ledger_fixture(root, extra_rows=unres)
    m4b = CacheV21(root).export_ca_ledger(workdir() / "nochild_ledger_b.parquet")
    assert m4b["marker_rows"]["by_reason"] == {"no_cash_amount": 1, "no_child": 1, "coacthist_stale_termination": 0}
    # 반례 4: 표시용 행이 실제 행과 같은 엔진 키(같은 날 cash, child null) → 표시용 행만 버리고 실제 행은 남는다(ContractError 아님)
    conflict = [("101_20220310_other_0", 101, "2022-03-10", "other", "cash", np.nan, 1.0, None, np.nan, "unknown", np.nan, "unresolved", False),
                ("101_20230801_spin_off_0", 101, "2023-08-01", "spin_off", "stock", np.nan, 1.0, None, np.nan, "unknown", np.nan, "unresolved", False),
                ("101_20230801_other_0", 101, "2023-08-01", "other", "stock", np.nan, 1.0, None, np.nan, "unknown", np.nan, "unresolved", False)]
    L5 = _ledger_fixture(root, extra_rows=conflict)
    m5 = CacheV21(root).export_ca_ledger(workdir() / "conflict_ledger.parquet")
    E5 = pd.read_parquet(workdir() / "conflict_ledger.parquet")
    assert m5["marker_rows"]["dropped_key_conflict"] == 2 and m5["marker_rows"]["dropped_key_conflict_by_reason"] == {"no_cash_amount": 1, "no_child": 1, "coacthist_stale_termination": 0} \
        and m5["marker_rows"]["by_reason"] == {"no_cash_amount": 1, "no_child": 1, "coacthist_stale_termination": 0} \
        and m5["marker_rows"]["in_ledger_by_reason"] == {"no_cash_amount": 2, "no_child": 2, "coacthist_stale_termination": 0}, m5["marker_rows"]
    assert "101_20220310_other_0" not in set(E5.eventId) and (E5.eventId == "101_20220310_cash_dividend_0").sum() == 1 \
        and (E5[(E5.tradingItemId == "101") & (E5.exDate == pd.Timestamp("2023-08-01"))].legType == "stock").sum() == 1
    assert not E5.duplicated(["tradingItemId", "exDate", "legType", "childTradingItemId"]).any() and m5["rows"] == len(L5) - 6
    _ledger_fixture(root)
    # 원장 없는 루트: export_wide 는 원장 없이 동작(manifest 에 ca_ledger 키 없음), export_ca_ledger 는 FileNotFoundError
    m0 = loader().export_wide(workdir() / "export_noledger", fields=("close",), basis="split_adj", masks=(), returns=False)
    assert "ca_ledger" not in m0 and rel not in m0["files"]
    try:
        loader().export_ca_ledger(workdir() / "none.parquet")
        raise AssertionError("missing ledger accepted")
    except FileNotFoundError:
        pass


def _events_fixture(root: Path) -> pd.DataFrame:
    """발행본 events/corporate_actions.parquet 형식(raw 단위, k≡1)의 합성 정산 파일: 105 → 101 주식(0.5, 101 은 정산 뒤 2023-03-15 3:2 →
    split_adj 환산 비율 0.75), 104 → 102 현금 80 + 주식 0.8, 103 은 패널 밖 종료일(정산 없음), 999 는 패널 밖 부모, 102 는 조건 없음."""
    T = pd.Timestamp
    rows = [
        # tradingItemId, endDate, settlementDate, cash, successor, ratio, hasCashLeg, hasStockLeg, settlementReturn, cacheUnitStatus
        (105, "2022-09-30", "2022-10-03", np.nan, 101, 0.5, False, True, np.nan, "ok"),
        (104, "2023-06-15", "2023-06-16", 80.0, 102, 0.8, True, True, np.nan, "ok"),
        (103, "2030-01-01", "2030-01-02", 1.0, None, np.nan, True, False, np.nan, "ok"),
        (999, "2022-05-02", "2022-05-03", 2.0, None, np.nan, True, False, np.nan, "ok"),
        (102, "2023-12-29", "2024-01-02", np.nan, None, np.nan, False, False, -0.1, "no_terms"),
    ]
    df = pd.DataFrame(rows, columns=["tradingItemId", "endDate", "settlementDate", "cashPerShare", "successorTradingItemId", "successorRatio",
                                     "hasCashLeg", "hasStockLeg", "settlementReturn", "cacheUnitStatus"])
    df["endDate"] = pd.to_datetime(df.endDate); df["settlementDate"] = pd.to_datetime(df.settlementDate)
    df["successorTradingItemId"] = df.successorTradingItemId.astype("Int64")
    df["cashPerShareCache"] = df.cashPerShare; df["successorRatioCache"] = df.successorRatio       # raw basis: k = 1
    df["targetCacheMark"] = np.nan; df["targetCacheMarkDate"] = pd.NaT; df["successorCacheClose"] = np.nan; df["successorCacheCloseDate"] = pd.NaT
    df["cacheUnitFactorTarget"] = 1.0; df["cacheUnitFactorSuccessor"] = 1.0; df["cacheUnitFactorTargetDate"] = pd.NaT
    df["actionCode"] = "ACQ"; df["source"] = "fixture"
    (root / "events").mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), root / "events" / "corporate_actions.parquet")
    return df


def test_export_estimate_reason():
    """estimateReason(rc2f Y1, 사용자 검토 09-21): 추정 표시 행에 ratio/date/value 를 '+' 로 결합, 표시 없는 행·표시용 행은 null."""
    from loader import estimate_reason
    root = workdir() / "fx_reason"
    shutil.copytree(fixture("ok"), root, dirs_exist_ok=True)
    rows = [
        # share-count derived ratio on a quoted child (CP-style) -> ratio
        ("101_20220310_spin_off_9", 101, "2022-03-10", "spin_off", "stock", np.nan, 1.0, 102, 0.25, "child_close_ex", 10.0, "estimated", True,
         "rc2f X2: 0.25 per share (share-count derived: 1 / 4); close = 40;csv_override_add"),
        # quoted child, ex-date inferred -> ratio+date (isEstimate + child_close_ex is a ratio estimate, the note adds the date)
        ("102_20220310_spin_off_9", 102, "2022-03-10", "spin_off", "stock", np.nan, 1.0, 101, 0.1, "child_close_ex", 10.0, "estimated", True,
         "rc2f X2: 0.1 per share (co_acthist); ex-date 03-10 inferred from the price move"),
        # vendor value -> value
        ("103_20220310_spin_off_9", 103, "2022-03-10", "spin_off", "stock", np.nan, 1.0, 102, 0.1, "vendor_implied", 5.0, "estimated", True, "vendor"),
        # vendor-implied cash -> value; ex_date_inferred cash amount -> date+value
        ("101_20220609_cash_dividend_9", 101, "2022-06-09", "cash_dividend", "cash", 0.4, 1.0, None, np.nan, "vendor_implied", np.nan, "estimated", True,
         "vendor_only_step;amount_vendor_implied"),
        ("103_20220609_cash_dividend_9", 103, "2022-06-09", "cash_dividend", "cash", 0.3, 1.0, None, np.nan, "unknown", np.nan, "estimated", True,
         "amount_from_FY_total;ex_date_inferred_from_pattern;csv_override_add"),
        # not flagged: nothing, even with a moved-date note
        ("102_20220609_cash_dividend_9", 102, "2022-06-09", "cash_dividend", "cash", 0.2, 1.0, None, np.nan, "unknown", np.nan, "ok", False,
         "exDate_moved_from=2022-06-08"),
        # flagged without any marker -> unspecified
        ("103_20220908_cash_dividend_9", 103, "2022-09-08", "cash_dividend", "cash", 0.1, 1.0, None, np.nan, "unknown", np.nan, "estimated", True, ""),
    ]
    df = pd.DataFrame(rows, columns=["eventId", "tradingItemId", "exDate", "eventType", "legType", "cashPerShare", "parentFactor", "childTradingItemId",
                                     "childRatio", "childValueSource", "childValuePerOldShare", "status", "isEstimate", "notes"])
    df["repairOf"] = None
    r = estimate_reason(df)
    assert r.tolist() == ["ratio", "ratio+date", "value", "value", "date+value", "", "unspecified"], r.tolist()
    # through the export: the column is carried, markers stay null, the quoted flagged spin keeps isEstimate
    L = _ledger_fixture(root, extra_rows=[t[:13] for t in rows])
    # _ledger_fixture writes notes="" for every row: re-attach the notes of the extra rows and rewrite
    Lp = pd.read_parquet(root / "events" / "ca_ledger.parquet")
    note_of = {t[0]: t[13] for t in rows}
    Lp["notes"] = Lp.eventId.map(note_of).fillna(Lp.notes)
    pq.write_table(pa.Table.from_pandas(Lp, preserve_index=False), root / "events" / "ca_ledger.parquet")
    m = CacheV21(root).export_ca_ledger(workdir() / "reason_export.parquet")
    E = pd.read_parquet(workdir() / "reason_export.parquet").set_index("eventId")
    assert E.loc["101_20220310_spin_off_9"].estimateReason == "ratio" and bool(E.loc["101_20220310_spin_off_9"].isEstimate)
    assert E.loc["102_20220310_spin_off_9"].estimateReason == "ratio+date" and E.loc["103_20220310_spin_off_9"].estimateReason == "value"
    assert E.loc["101_20220609_cash_dividend_9"].estimateReason == "value" and E.loc["103_20220609_cash_dividend_9"].estimateReason == "date+value"
    assert pd.isna(E.loc["102_20220609_cash_dividend_9"].estimateReason) and E.loc["103_20220908_cash_dividend_9"].estimateReason == "unspecified"
    assert pd.isna(E.loc["101_20220412_other_0"].estimateReason) and E.loc["101_20220412_other_0"].markerReason == "no_cash_amount"
    assert E[E.isEstimate.astype(bool)].estimateReason.notna().all() and E[~E.isEstimate.astype(bool) & (E.status != "estimated")].estimateReason.isna().all()
    assert m["estimate_reason_by_leg"]["stock"] == {"ratio": 1, "ratio+date": 1, "value": 2} and m["estimate_flagged_without_reason"] == 0 \
        and m["quoted_child_estimates"] == 4, m   # 3 here (two child_close_ex + the quoted vendor_implied) + the fixture's 103->102 event_value leg
    _ledger_fixture(root)


def test_export_events_split_adj():
    """export_events(rc2f Y1-A, SPEC_loader §7): 정산 bar·mark 를 엔진 규칙대로 export 패널에서 찾고 앵커를 split_adj 종가로 다시 쓰며
    조건을 환산한다(cash/sf_T, ratio × sf_S/sf_T). raw 정산식과 split_adj 정산식이 같은 R 을 준다(항등). 상태·manifest·export_wide 통합."""
    root = workdir() / "fx_events"
    shutil.copytree(fixture("ok"), root, dirs_exist_ok=True)
    src = _events_fixture(root)
    cv = CacheV21(root)
    out = workdir() / "export_events"
    m = cv.export_events(out / "engine" / "events_cache.parquet")
    E = pd.read_parquet(out / "engine" / "events_cache.parquet").set_index("tradingItemId")
    adj = cv.get_panel("close", "split_adj", cut="lifecycle")
    raw = cv.get_panel("close", "raw", cut="lifecycle")
    assert m["rows"] == len(src) and m["status"] == {"ok": 2, "no_settlement_bar_in_panel": 1, "target_not_in_panel": 1, "no_terms": 1}
    assert set(E.columns) >= {"cacheUnitStatusRaw", "cacheBasis", "settlementBarDate"} and (E.cacheBasis == "split_adj").all()
    # 105 → 101: settlement bar = first silent bar after 2022-09-30 (2022-10-03), mark = 2022-09-30; sf_101(2022-10-03) = 1.5 → ratio 0.75
    r = E.loc[105]
    assert r.cacheUnitStatus == "ok" and r.settlementBarDate == pd.Timestamp("2022-10-03") and r.targetCacheMarkDate == pd.Timestamp("2022-09-30")
    assert r.successorCacheCloseDate == pd.Timestamp("2022-10-03") and abs(r.successorRatioCache - 0.75) < 1e-12 and np.isnan(r.cashPerShareCache)
    assert r.cacheUnitFactorTarget == 1.0 and r.cacheUnitFactorSuccessor == 1.5
    assert r.targetCacheMark == adj.loc[pd.Timestamp("2022-09-30"), 105] and r.successorCacheClose == adj.loc[pd.Timestamp("2022-10-03"), 101]
    R_adj = r.successorRatioCache * r.successorCacheClose / r.targetCacheMark - 1.0
    R_raw = 0.5 * raw.loc[pd.Timestamp("2022-10-03"), 101] / raw.loc[pd.Timestamp("2022-09-30"), 105] - 1.0
    assert abs(R_adj - R_raw) < 1e-6, (R_adj, R_raw)                       # unit identity (float32 closes)
    # 104 → 102 mixed: no splits → identity conversion, anchors at 2023-06-15 / 2023-06-16 (lifecycle cut silences the tail)
    r = E.loc[104]
    assert r.cacheUnitStatus == "ok" and r.settlementBarDate == pd.Timestamp("2023-06-16") and r.targetCacheMarkDate == pd.Timestamp("2023-06-15")
    assert r.cashPerShareCache == 80.0 and r.successorRatioCache == 0.8 and r.successorCacheClose == adj.loc[pd.Timestamp("2023-06-16"), 102]
    assert r.targetCacheMark == adj.loc[pd.Timestamp("2023-06-15"), 104]
    # 103: end date beyond the panel → no settlement bar; cash restated against the last mark, ratio NaN, status says so
    r = E.loc[103]
    assert r.cacheUnitStatus == "no_settlement_bar_in_panel" and pd.isna(r.settlementBarDate) and r.cashPerShareCache == 1.0 and np.isnan(r.successorRatioCache)
    assert r.targetCacheMarkDate == adj[103].last_valid_index() and np.isfinite(r.targetCacheMark)
    # 999: not in the panel → nothing restated; 102: no terms
    assert E.loc[999].cacheUnitStatus == "target_not_in_panel" and np.isnan(E.loc[999].cashPerShareCache) and pd.isna(E.loc[999].targetCacheMarkDate)
    assert E.loc[102].cacheUnitStatus == "no_terms" and E.loc[102].settlementReturn == -0.1 and E.loc[102].cacheUnitStatusRaw == "no_terms"
    assert m["cash_rows_restated"] == 2 and m["ratio_rows_restated"] == 2 and m["sf_successor_ne_1"] == 1 and m["sf_target_ne_1"] == 0
    # export_wide writes it next to the ledger export with its own manifest
    mw = cv.export_wide(workdir() / "export_wide_events", fields=("close",), basis="split_adj", masks=(), returns=False)
    assert "engine/events_cache.parquet" in mw["files"] and mw["files"]["engine/events_cache.parquet"]["sha256"] == m["sha256"] \
        and mw["events_cache"]["status"] == m["status"] and (workdir() / "export_wide_events" / "engine" / "events_cache_manifest.json").exists()
    # basis raw: skipped with a note
    mr = cv.export_wide(workdir() / "export_wide_events_raw", fields=("close",), basis="raw", masks=(), returns=False)
    assert "engine/events_cache.parquet" not in mr["files"] and "skipped" in mr["events_cache"]
    (root / "events" / "corporate_actions.parquet").unlink()


def test_export_events_status_passthrough():
    """C5b: terms-less rows (cash·ratio NaN) keep raw status last_close / rejected:*; any other raw status becomes no_terms."""
    root = workdir() / "fx_events_status"
    shutil.copytree(fixture("ok"), root, dirs_exist_ok=True)
    src = _events_fixture(root)
    for tid, st in ((102, "last_close"), (103, "rejected:bad_ratio"), (105, "something_else")):
        m = src.tradingItemId == tid
        src.loc[m, ["cashPerShare", "successorRatio", "cashPerShareCache", "successorRatioCache"]] = np.nan
        src.loc[m, "cacheUnitStatus"] = st
    pq.write_table(pa.Table.from_pandas(src, preserve_index=False), root / "events" / "corporate_actions.parquet")
    out = workdir() / "export_events_status" / "events_cache.parquet"
    CacheV21(root).export_events(out)
    E = pd.read_parquet(out).set_index("tradingItemId")
    assert E.loc[102].cacheUnitStatus == "last_close" and E.loc[103].cacheUnitStatus == "rejected:bad_ratio" and E.loc[105].cacheUnitStatus == "no_terms", E.cacheUnitStatus.to_dict()
    assert E.loc[105].cacheUnitStatusRaw == "something_else"
    (root / "events" / "corporate_actions.parquet").unlink()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=None, help="fixture/작업 디렉토리를 여기 두고 지우지 않음")
    ap.add_argument("-k", default=None, help="이름 부분 일치 필터")
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
            rows.append((t.__name__, "PASS", getattr(t, "note", "")))
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
