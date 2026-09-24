#!/home/jgo/usr/venv-3.11/bin/python
"""D1 원장 증분 테스트(합성 + 실데이터 게이트 축약). pytest 또는 직접 실행:

  /home/jgo/usr/venv-3.11/bin/python tests/test_ledger_incremental.py [--keep DIR] [-k name]

합성(test_ledger.make_cache 의 3 종목 캐시 위에 dividends_long·distributions·events·work 표를 더한 root):
  stage0  = 전체 빌드(캘린더 컷 2020-06-30 → sidecar asOf 06-30)
  step1   = --incremental --prev stage0 --asof 2020-07-31 --source-extract ex1 (+ 교정 CSV):
            추가(창 안·창보다 오래된 late_history·미래 선언·비-USD fx)·변경(재기술)·삭제(같은 날 합산 행 감소)·ciqsplit 추가·
            co_acthist 정지(06-30) → 종료 id 4 표시용 행·경고, revisions 귀속, sidecar asOf 갱신(update_daily stale 검사 통과)
  step2   = 같은 추출·같은 asOf 로 한 번 더 → revisions 0, 교정 결과 동일(idempotent; add 는 한 번만, duplicate add 는 no-op)
  step3   = --source-cut 으로 미래 선언 행·컷 뒤 종가 제외 → 전체 빌드(prepare-full 과 같은 조립)와 비트 동일
실데이터 게이트(선택): D1_GATE_RESULTS=/home/jgo/longshort/daily_D1/gate_results.json 이 있으면 세 날짜 identical 을 확인.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import ledger_build as lb  # noqa: E402
import ledger_incremental as li  # noqa: E402
import test_ledger as tl  # noqa: E402

T = pd.Timestamp
_WORK: Path | None = None
_FIX: dict | None = None


def workdir() -> Path:
    global _WORK
    if _WORK is None:
        _WORK = Path(tempfile.mkdtemp(prefix="cache_v21_ledger_incr_"))
    return _WORK


# ----------------------------------------------------------------------------------------------------------
# synthetic sources
# ----------------------------------------------------------------------------------------------------------
def raw_div_rows(version: int) -> pd.DataFrame:
    """ciqdividend DB-style rows (lower-case). version 0 = as of 2020-06-30, 1 = as of 2020-07-31."""
    rows = [
        dict(dividendid=1, tradingitemid=1, exdate=T("2020-02-03"), divamount=1.0, dividendtypeid=1, currencyid=160, supplementaltypename="Regular cash dividend"),
        dict(dividendid=2, tradingitemid=1, exdate=T("2020-04-01"), divamount=1.0, dividendtypeid=1, currencyid=160, supplementaltypename="Regular cash dividend"),
        dict(dividendid=3, tradingitemid=1, exdate=T("2020-05-01"), divamount=0.4, dividendtypeid=1, currencyid=160, supplementaltypename="Regular cash dividend"),
        dict(dividendid=4, tradingitemid=1, exdate=T("2020-05-01"), divamount=0.1, dividendtypeid=4, currencyid=160, supplementaltypename="Special dividend"),
        dict(dividendid=5, tradingitemid=1, exdate=T("2020-04-15"), divamount=np.nan, dividendtypeid=20, currencyid=160, supplementaltypename=None),
        dict(dividendid=6, tradingitemid=1, exdate=T("2020-06-01"), divamount=np.nan, dividendtypeid=20, currencyid=160, supplementaltypename=None),
        dict(dividendid=9, tradingitemid=3, exdate=T("2020-07-20"), divamount=1.0, dividendtypeid=1, currencyid=27, supplementaltypename="Regular cash dividend"),    # non-USD, future as of 06-30
    ]
    if version >= 1:
        rows = [r for r in rows if r["dividendid"] != 3]                                   # removed (restated history: 05-01 sum 0.5 -> 0.1)
        rows[1] = dict(rows[1], divamount=1.1)                                              # changed (restated)
        rows += [dict(dividendid=7, tradingitemid=1, exdate=T("2020-07-15"), divamount=0.5, dividendtypeid=1, currencyid=160, supplementaltypename="Regular cash dividend"),   # window
                 dict(dividendid=8, tradingitemid=2, exdate=T("2020-03-16"), divamount=0.05, dividendtypeid=1, currencyid=160, supplementaltypename="Regular cash dividend"),  # late history
                 dict(dividendid=10, tradingitemid=1, exdate=T("2020-08-10"), divamount=0.6, dividendtypeid=1, currencyid=160, supplementaltypename="Regular cash dividend")]  # future
    df = pd.DataFrame(rows)
    for c, v in (("paydate", pd.NaT), ("recorddate", pd.NaT), ("announceddate", pd.NaT), ("netamount", np.nan), ("grossamount", np.nan), ("exchangeid", 458.0),
                 ("divfreqtypeid", np.nan), ("supplementaltypeid", np.nan)):
        df[c] = v
    df["dividendtypename"] = df.dividendtypeid.map({1: "No special code applies", 4: "Special", 20: "Stock dividend in stock of another issue"})
    return df


def write_extract(ex_dir: Path, version: int, co_acthist_max: str) -> None:
    ex_dir.mkdir(parents=True, exist_ok=True)
    li._write_df(li.normalize_div(raw_div_rows(version)), ex_dir / "raw_ciqdividend.parquet")
    if version >= 1:      # ex-date rate for the CAD row (v0 had only the provisional 07-17 rate for a row first seen with exDate in the future)
        fx = pd.DataFrame({"currencyid": pd.Series([27, 27, 27], dtype="int64"), "pricedate": pd.to_datetime(["2020-06-25", "2020-07-17", "2020-07-20"]).astype("datetime64[ns]"), "fx": [1.2, 1.25, 1.3]})
    else:
        fx = pd.DataFrame({"currencyid": pd.Series([27, 27], dtype="int64"), "pricedate": pd.to_datetime(["2020-06-25", "2020-07-17"]).astype("datetime64[ns]"), "fx": [1.2, 1.25]})
    li._write_df(fx, ex_dir / "raw_fx.parquet")
    cs = pd.DataFrame({"splitid": [1, 2, 3], "tradingitemid": [1, 1, 2], "exdate": pd.to_datetime(["2020-03-02", "2020-06-01", "2020-07-01"]), "rate": [2.0, 0.5, 3.0],
                       "splittypeid": [12, 34, 12], "appliedflag": [1, 1, 1]})
    if version >= 1:
        cs = pd.concat([cs, pd.DataFrame({"splitid": [4], "tradingitemid": [3], "exdate": pd.to_datetime(["2020-09-01"]), "rate": [2.0], "splittypeid": [12], "appliedflag": [1]})], ignore_index=True)
    for c in ("paydate", "recorddate", "announceddate"):
        cs[c] = pd.NaT
    li._write_df(li.normalize_work("ciqsplit", cs), ex_dir / "raw_ciqsplit.parquet")
    li._write_df(li.normalize_work("ciqsplitcache", pd.DataFrame(columns=list(li.WORK_TABLES["ciqsplitcache"]["dtypes"]))), ex_dir / "raw_ciqsplitcache.parquet")
    li._write_df(li.normalize_work("divcache_noncash", pd.DataFrame(columns=list(li.WORK_TABLES["divcache_noncash"]["dtypes"]))), ex_dir / "raw_divcache_noncash.parquet")
    li._write_df(li.normalize_work("ciqcurrency", pd.DataFrame({"currencyid": [160, 27], "isocode": ["USD", "CAD"]})), ex_dir / "raw_ciqcurrency.parquet")
    li._write_df(pd.DataFrame(columns=["gvkey", "datadate", "actioncd", "actiontxt", "gvkeyaqc", "iid", "newconm", "newtic1", "oldconm", "oldtic1"]).astype({"datadate": "datetime64[ns]"}),
                 ex_dir / "raw_co_acthist_window.parquet")
    (ex_dir / "extract_info.json").write_text(json.dumps({"co_acthist_max_datadate": co_acthist_max, "synthetic": True}))


def make_root(root: Path) -> dict:
    """test_ledger.make_cache + dividends_long/distributions/events/master extras."""
    info = tl.make_cache(root)
    lc = pd.read_parquet(root / "master" / "lifecycle.parquet")
    lc = pd.concat([lc, pd.DataFrame({"tradingItemId": [4, 5], "firstTradeDate": [T("2020-01-02")] * 2, "lastTradeDate": [T("2020-07-10"), T("2020-07-08")], "firstPriceDate": [T("2020-01-02")] * 2,
                                      "ciqLastCloseDate": [T("2020-07-10"), T("2020-07-08")], "status": ["confirmed"] * 2})], ignore_index=True)
    lc["endReason"] = np.where(lc.status == "confirmed", "merger", "active")
    lc.to_parquet(root / "master" / "lifecycle.parquet", index=False)
    # id 4 (ended 2020-07-10 without terms) gets raw closes so the engine export keeps its marker row (parent in panel)
    import pyarrow as pa
    import pyarrow.parquet as pq
    base = pd.read_parquet(root / "base_data.parquet")
    cal4 = [d for d in info["cal"] if d <= T("2020-07-10")]
    add = pd.DataFrame({"date": cal4, "tradingItemId": 4, "open": np.float32(20.0), "high": np.float32(20.0), "low": np.float32(20.0), "close": np.float32(20.0),
                        "vwap": np.float32(20.0), "volume": 1000.0, "source": np.int8(1), "qualityFlag": np.int8(0)})
    base = pd.concat([base, add], ignore_index=True).sort_values(["tradingItemId", "date"]).reset_index(drop=True)
    pq.write_table(pa.Table.from_pandas(base, schema=pq.read_schema(root / "base_data.parquet"), preserve_index=False), root / "base_data.parquet", compression="zstd")
    sp = pd.read_parquet(root / "factors" / "split.parquet")
    sp = pd.concat([sp, pd.DataFrame({"tradingItemId": [4], "validFrom": [T("1900-01-01")], "validTo": [pd.NaT], "factor": [1.0], "source": ["synthetic_no_window"], "snapshotDate": [T("2026-09-17")]})], ignore_index=True)
    pq.write_table(pa.Table.from_pandas(sp, schema=pq.read_schema(root / "factors" / "split.parquet"), preserve_index=False), root / "factors" / "split.parquet", compression="zstd")
    dv = pd.read_parquet(root / "factors" / "dividend.parquet")
    dv = pd.concat([dv, pd.DataFrame({"tradingItemId": [4], "validFrom": [T("1900-01-01")], "validTo": [pd.NaT], "factor": [1.0], "source": ["synthetic_no_window"], "snapshotDate": [T("2026-09-17")]})], ignore_index=True)
    pq.write_table(pa.Table.from_pandas(dv, schema=pq.read_schema(root / "factors" / "dividend.parquet"), preserve_index=False), root / "factors" / "dividend.parquet", compression="zstd")
    pd.DataFrame({"tradingItemId": [1, 2, 3, 4, 5], "companyName": ["Parent Inc", "Child Corp", "Hold Trust", "Gone Inc", "Acquired Inc"]}).to_parquet(root / "master" / "instruments.parquet", index=False)
    pd.DataFrame({"tradingItemId": [1, 2, 3, 4, 5], "symbol": ["P", "C", "H", "G", "Q"], "gvkey": ["000001", "000002", "000003", "000004", "000005"], "iid": ["01"] * 5}).to_parquet(root / "master" / "current.parquet", index=False)
    ex0 = root.parent / "ex0"
    write_extract(ex0, 0, "2020-06-30")
    raw = pd.read_parquet(ex0 / "raw_ciqdividend.parquet")
    refs = li.Refs(pd.read_parquet(root / "master" / "instruments.parquet"), pd.read_parquet(root / "master" / "current.parquet"), pd.read_parquet(ex0 / "raw_fx.parquet"),
                   pd.read_parquet(root / "factors" / "split.parquet"))
    li._write_df(li.build_dividends_scratch(raw, refs, "2020-06-30"), root / "dividends" / "dividends_long.parquet", li.DIV_SCHEMA)
    dist = pd.DataFrame([
        dict(tradingItemId=1, exDate=T("2020-04-15"), kind="stock", leg=0, newTradingItemId=2, ratioNewPerOld=0.5, eventClass="spinoff", status="ok", source="ciqdividend20", evidence="TEST C",
             newIdMethod="test", mismatchClass=None, ratioBasis="per_share_text", cashEquivalentText=np.nan, impliedValueFracExCash=np.nan),
        dict(tradingItemId=1, exDate=T("2020-05-01"), kind="stock", leg=0, newTradingItemId=2, ratioNewPerOld=0.1, eventClass="spinoff", status="ok", source="co_acthist", evidence="TEST C2",
             newIdMethod="test", mismatchClass=None, ratioBasis="per_share_text", cashEquivalentText=np.nan, impliedValueFracExCash=np.nan),
        dict(tradingItemId=1, exDate=T("2020-06-01"), kind="stock", leg=0, newTradingItemId=3, ratioNewPerOld=0.5, eventClass="spinoff", status="ok", source="ciqdividend20", evidence="TEST H",
             newIdMethod="test", mismatchClass=None, ratioBasis="per_share_text", cashEquivalentText=np.nan, impliedValueFracExCash=np.nan)])
    dist["newTradingItemId"] = dist.newTradingItemId.astype("Int64")
    (root / "distributions").mkdir(exist_ok=True)
    dist.to_parquet(root / "distributions" / "distributions.parquet", index=False)
    ev = pd.DataFrame({"tradingItemId": pd.Series([], dtype="int64"), "endDate": pd.Series([], dtype="datetime64[ns]"), "settlementDate": pd.Series([], dtype="datetime64[ns]"),
                       "actionCode": pd.Series([], dtype="object"), "hasCashLeg": pd.Series([], dtype="bool"), "hasStockLeg": pd.Series([], dtype="bool"),
                       "cashPerShare": pd.Series([], dtype="float64"), "successorTradingItemId": pd.Series([], dtype="Int64"), "successorRatio": pd.Series([], dtype="float64"),
                       "confidence": pd.Series([], dtype="object"), "cacheUnitStatus": pd.Series([], dtype="object"), "actionText": pd.Series([], dtype="object"),
                       "source": pd.Series([], dtype="object")})
    (root / "events").mkdir(exist_ok=True)
    ev.to_parquet(root / "events" / "corporate_actions.parquet", index=False)
    (root / "qa" / "T2").mkdir(parents=True, exist_ok=True)
    (root / "qa" / "T7").mkdir(parents=True, exist_ok=True)
    (root / "qa" / "T2" / "factor_artifacts.csv").write_text("tradingItemId,symbol,date,prevDate,r_adj,r_raw,r_total,sf_prev,sf,div_step,category,everTop1000,everTop500\n")
    (root / "qa" / "T7" / "J_gdiv_systematic_scale_ids.csv").write_text("tradingItemId,symbol,company,n_steps_2000plus\n")
    info["ex0"] = ex0
    info["qa_files"] = {"T2/factor_artifacts.csv": root / "qa" / "T2" / "factor_artifacts.csv", "T7/J_gdiv_systematic_scale_ids.csv": root / "qa" / "T7" / "J_gdiv_systematic_scale_ids.csv"}
    return info


def full_stage(root: Path, stage: Path, ex_dir: Path, calendar_cut: str | None, closes_cut: str | None = None, overrides: list | None = None) -> dict:
    """전체 빌드(합성): root 조립(캘린더 컷) + work 표(추출) + lb.build(); sidecar asOf = 캘린더 마지막 거래일."""
    ex = li.load_extract(ex_dir)
    if closes_cut:
        ex = li.cut_extract(ex, closes_cut)
    files = {"calendar": root / "calendar.parquet", "split": root / "factors" / "split.parquet", "dividend_factor": root / "factors" / "dividend.parquet",
             "events": root / "events" / "corporate_actions.parquet", "instruments": root / "master" / "instruments.parquet", "current": root / "master" / "current.parquet",
             "membership": root / "universe" / "membership.parquet", "lineage": root / "master" / "lineage.parquet", "lifecycle": root / "master" / "lifecycle.parquet"}
    refs = li.Refs(pd.read_parquet(files["instruments"]), pd.read_parquet(files["current"]), ex["fx"], pd.read_parquet(files["split"]))
    div, _ = li.refresh_dividends(pd.read_parquet(root / "dividends" / "dividends_long.parquet"), ex["ciqdividend"], refs, closes_cut or calendar_cut or "2020-12-31", None)
    qa_files = {"T2/factor_artifacts.csv": root / "qa" / "T2" / "factor_artifacts.csv", "T7/J_gdiv_systematic_scale_ids.csv": root / "qa" / "T7" / "J_gdiv_systematic_scale_ids.csv"}
    li.assemble_root(stage / "root", files, div, root / "distributions", qa_files, {1, 2, 3, 4, 5}, calendar_cut=calendar_cut)
    (stage / "work").mkdir(parents=True, exist_ok=True)
    for n in li.WORK_TABLES:
        li._write_df(li.normalize_work(n, ex[n]), stage / "work" / f"work_{n}.parquet")
    (stage / "work" / "source_status.json").write_text(json.dumps(li.source_status(ex, closes_cut or calendar_cut or "2020-12-31", None)))
    lb.CLOSES_MAX_DATE = T(closes_cut) if closes_cut else None
    try:
        info = lb.build(stage / "root", stage, root / "base_data.parquet", stage / "events" / "ca_ledger.parquet", pull=False, lineage_path=files["lineage"], lifecycle_path=files["lifecycle"],
                        r3_events=None, gap_candidates=None, overrides=overrides, overrides_ref_ledger=None)
    finally:
        lb.CLOSES_MAX_DATE = None
    return info


def fixture() -> dict:
    global _FIX
    if _FIX is None:
        _neutralize_real_data_hooks()
        w = workdir()
        for d in ("root", "ex0", "ex1", "stage0", "step1", "step2", "step3", "full3", "bad", "ovr"):
            shutil.rmtree(w / d, ignore_errors=True)
        root = w / "root"
        info = make_root(root)
        st0 = w / "stage0"
        b0 = full_stage(root, st0, info["ex0"], calendar_cut="2020-06-30")
        ex1 = w / "ex1"
        write_extract(ex1, 1, "2020-06-30")
        # override CSV (task dir layout <task>/overrides/ledger_overrides.csv, like the rc2f glob)
        ovr = w / "ovr" / "T1" / "overrides"
        ovr.mkdir(parents=True)
        pd.DataFrame([
            dict(overrideId="T1-1", caseId="c1", tradingItemId=1, eventId="", action="add", set=json.dumps({"eventType": "special_dividend", "legType": "cash", "exDate": "2020-09-01", "cashPerShare": 2.0, "cashCurrency": "USD"}),
                 isEstimate="true", evidence="synthetic add", source="test", owner="t"),
            dict(overrideId="T1-2", caseId="c2", tradingItemId=1, eventId="", match_tradingItemId=1, match_exDate="2020-02-03", match_legType="cash", action="set",
                 set=json.dumps({"cashPerShare": 1.25}), isEstimate="false", evidence="synthetic set", source="test", owner="t"),
            dict(overrideId="T1-3", caseId="c3", tradingItemId=1, eventId="", action="add", set=json.dumps({"eventType": "cash_dividend", "legType": "cash", "exDate": "2020-04-01", "cashPerShare": 1.1, "cashCurrency": "USD"}),
                 isEstimate="false", evidence="synthetic duplicate add", source="test", owner="t"),
            dict(overrideId="T1-4", caseId="c4", tradingItemId=5, eventId="", action="add", set=json.dumps({"eventType": "merger_stock", "legType": "stock", "exDate": "2020-07-08", "childTradingItemId": 1, "childRatio": 0.5, "status": "ok"}),
                 isEstimate="false", evidence="synthetic terms row for an ended id (no marker expected)", source="test", owner="t"),
        ]).to_csv(ovr / "ledger_overrides.csv", index=False)
        _FIX = {"root": root, "stage0": st0, "b0": b0, "ex0": info["ex0"], "ex1": ex1, "glob": str(w / "ovr" / "*" / "overrides" / "*.csv"), "cal": info["cal"]}
    return _FIX


def _neutralize_real_data_hooks():
    """합성 fixture 에서는 실데이터 전용 훅(R2 gap 후보 목록·수동 교정표)을 비운다(모듈 전역; 프로세스 안에서만)."""
    import ledger_repairs as rep
    lb.R2_GAP_CANDIDATES = []
    rep.MANUAL_OVERRIDES = []
    rep.VALUE_OVERRIDES = []


def run_incremental(prev: Path, asof: str, out: Path, ex: Path, extra: list | None = None) -> dict:
    fx = fixture()
    _neutralize_real_data_hooks()
    argv = ["--incremental", "--prev", str(prev), "--asof", asof, "--out", str(out), "--source-extract", str(ex), "--calendar", str(fx["root"] / "calendar.parquet"),
            "--overrides-glob", fx["glob"], "--skip-validate", "--no-qa"] + (extra or [])
    rc = lb.main(argv)
    assert rc == 0, rc
    return json.loads((out / li.STAGE_INFO).read_text())


# ----------------------------------------------------------------------------------------------------------
def test_refresh_dividends_diff_and_derived():
    """추가(window/late_history/future)·변경(restated)·삭제·fx·파생 컬럼·inRange 재계산·단계별 == 한 번에."""
    fx = fixture()
    ex0, ex1 = li.load_extract(fx["ex0"]), li.load_extract(fx["ex1"])
    prev = pd.read_parquet(fx["root"] / "dividends" / "dividends_long.parquet")
    refs = li.Refs(pd.read_parquet(fx["root"] / "master" / "instruments.parquet"), pd.read_parquet(fx["root"] / "master" / "current.parquet"), ex1["fx"], pd.read_parquet(fx["root"] / "factors" / "split.parquet"))
    new, ch = li.refresh_dividends(prev, ex1["ciqdividend"], refs, "2020-07-31", "2020-06-30")
    kinds = ch.groupby(["kind", "cls"]).size().to_dict()
    assert kinds[("added", "window")] == 1 and kinds[("added", "late_history")] == 1 and kinds[("added", "future")] == 1 and kinds[("changed", "restated")] == 1 and kinds[("removed", "restated")] == 1, kinds
    assert set(ch[ch.kind == "changed"].field) == {"divAmount"} and ch[ch.kind == "changed"].before.iloc[0] == "1.0" and ch[ch.kind == "changed"].after.iloc[0] == "1.1"
    # R-D1 P1-4: id 9 (CAD, ex 07-20) was stored with the provisional 07-17 rate 1.25 (future row as of 06-30); the ex-date rate 1.3 is now in the FX table -> recomputed
    r9 = new[new.dividendId == 9].iloc[0]
    assert prev[prev.dividendId == 9].fxRate.iloc[0] == 1.25
    assert r9.fxRate == 1.3 and abs(r9.divAmount_USD - 1 / 1.3) < 1e-12 and r9.companyName == "Hold Trust" and r9.symbol == "H"
    fxch = ch[(ch.kind == "derived") & (ch.field == "fxRate")]
    assert len(fxch) == 1 and fxch.key.iloc[0] == "9" and fxch.before.iloc[0] == "1.25" and fxch.after.iloc[0] == "1.3", fxch
    assert new[new.dividendId == 10].inRange.iloc[0] is np.False_ or not bool(new[new.dividendId == 10].inRange.iloc[0])          # future row not in range
    assert bool(new[new.dividendId == 7].inRange.iloc[0]) and not (new.dividendId == 3).any() and new[new.dividendId == 2].divAmount.iloc[0] == 1.1
    assert new[new.dividendId == 2].companyName.iloc[0] == "Parent Inc" and list(new.dividendId) == sorted(new.dividendId, key=lambda k: (int(new.set_index("dividendId").tradingItemId[k]), new.set_index("dividendId").exDate[k], k))
    # stepwise == one-step: (prev -> v0 again -> v1) equals (prev -> v1)
    mid, _ = li.refresh_dividends(prev, ex0["ciqdividend"], refs, "2020-06-30", "2020-06-30")
    two, _ = li.refresh_dividends(mid, ex1["ciqdividend"], refs, "2020-07-31", "2020-06-30")
    assert two.equals(new), "stepwise refresh differs from one-step refresh"
    # scratch (all derived recomputed) agrees on the ledger-relevant columns
    scr = li.build_dividends_scratch(ex1["ciqdividend"], refs, "2020-07-31")
    for c in ("fxRate", "companyName", "symbol", "divAmount", "isCash", "isStock", "inRange"):
        assert li._eq(scr[c], new[c]).all(), c


def test_refresh_work_and_cut():
    """work 표: 추가·변경·삭제 감지, 행 순서 무관(키 정렬); cut_extract 는 사건일 ≤ cut 만."""
    fx = fixture()
    ex0, ex1 = li.load_extract(fx["ex0"]), li.load_extract(fx["ex1"])
    new, ch = li.refresh_work("ciqsplit", ex0["ciqsplit"], ex1["ciqsplit"], "2020-07-31", "2020-06-30")
    assert ch.kind.tolist() == ["added"] and ch.key.tolist() == ["4"] and ch.cls.tolist() == ["future"]
    shuffled = ex1["ciqsplit"].iloc[::-1].reset_index(drop=True)
    new2, _ = li.refresh_work("ciqsplit", ex0["ciqsplit"], shuffled, "2020-07-31", "2020-06-30")
    assert new2.equals(new)
    changed = ex1["ciqsplit"].copy(); changed.loc[changed.splitid == 1, "rate"] = 2.5
    _, ch2 = li.refresh_work("ciqsplit", ex1["ciqsplit"], changed, "2020-07-31", "2020-06-30")
    assert ch2.kind.tolist() == ["changed"] and ch2.field.tolist() == ["rate"] and ch2.cls.tolist() == ["restated"]
    _, ch3 = li.refresh_work("ciqsplit", ex1["ciqsplit"], ex0["ciqsplit"], "2020-07-31", "2020-06-30")
    assert ch3.kind.tolist() == ["removed"]
    cut = li.cut_extract(ex1, "2020-07-31")
    assert cut["ciqdividend"].exDate.max() <= T("2020-07-31") and not (cut["ciqdividend"].dividendId == 10).any() and len(cut["ciqsplit"]) == 3
    st = li.source_status(cut, "2020-07-31", None)
    assert st["stale"]["co_acthist"] and st["co_acthist"]["latest"] == "2020-06-30" and st["ciqdividend"]["latest"] == "2020-07-20" and st["warnings"]
    st2 = li.source_status(cut, "2020-07-10", None)
    assert not st2["stale"]["co_acthist"] and not any("co_acthist stale" in w for w in st2["warnings"]) and any("static sources" in w for w in st2["warnings"])


def test_step1_incremental_revisions_markers_sidecar():
    """step1: 새 원장 = 전체 빌드와 같은 규칙; revisions 귀속; 종료 id 표시용 행; sidecar asOf → update_daily stale 검사."""
    fx = fixture()
    st1 = workdir() / "step1"
    info = run_incremental(fx["stage0"], "2020-07-31", st1, fx["ex1"])
    L = pd.read_parquet(st1 / "events" / "ca_ledger.parquet")
    rev = pd.read_parquet(st1 / "events" / li.REVISIONS_DIR / "date=2020-07-31" / "revisions.parquet")
    src = pd.read_parquet(st1 / "events" / li.REVISIONS_DIR / "date=2020-07-31" / "source_changes.parquet")
    by = rev.groupby(["kind", "source"]).size().to_dict()
    # added: P 07-15, C 03-16, P 08-10 (future) from ciqdividend; T1-1 add (09-01 special) + T1-4 add (merger_stock id 5); marker for ended id 4
    assert by[("added", "ciqdividend")] == 3, by
    assert by[("added", "rule:ended_without_terms_marker")] == 1 and by[("added", "override:csv_applied")] == 2
    added = rev[rev.kind == "added"]
    assert set(added.eventId) >= {"1_20200715_cash_dividend_0", "2_20200316_cash_dividend_0", "1_20200810_cash_dividend_0", "4_20200710_other_0", "1_20200901_special_dividend_0", "5_20200708_merger_stock_0"}
    # H 07-20 (CAD) existed in stage0 with the provisional 07-17 rate 1.25 -> ex-date rate 1.3 now: changed cashPerShare/fxToUSD attributed to the fx recompute
    h_ch = rev[(rev.kind == "changed") & (rev.eventId == "3_20200720_cash_dividend_0")].set_index("field")
    assert abs(float(h_ch.at["cashPerShare", "after"]) - 1 / 1.3) < 1e-12 and abs(float(h_ch.at["cashPerShare", "before"]) - 1 / 1.25) < 1e-12 and h_ch.at["cashPerShare", "source"] == "ciqdividend:fx"
    # changed: 04-01 amount 1.0->1.1 (restated) and 05-01 sum 0.5->0.1 (dividendId 3 removed) -> both attributed to ciqdividend; T1-2 set 02-03 1.0->1.25
    ch = rev[(rev.kind == "changed") & (rev.field == "cashPerShare")].set_index("eventId")
    assert ch.at["1_20200401_cash_dividend_0", "after"] == "1.1" and ch.at["1_20200401_cash_dividend_0", "source"] == "ciqdividend"
    assert ch.at["1_20200501_special_dividend_0", "after"] == "0.1" and ch.at["1_20200501_special_dividend_0", "source"] == "ciqdividend"
    assert ch.at["1_20200203_cash_dividend_0", "after"] == "1.25" and ch.at["1_20200203_cash_dividend_0", "source"] == "override:csv_applied"
    assert int((rev.source == "unattributed").sum()) == 0, rev[rev.source == "unattributed"]
    # marker row: unresolved, no value, source lifecycle, notes tag
    m = L[L.eventId == "4_20200710_other_0"].iloc[0]
    assert m.status == "unresolved" and m.legType == "stock" and pd.isna(m.childTradingItemId) and np.isnan(m.childValuePerOldShare) and lb.ENDED_MARKER_TAG in m.notes and m.source == "lifecycle"
    assert info["ended_without_terms_markers"] == 1 and any("co_acthist stale" in w for w in info["warnings"]) and any("static sources" in w for w in info["warnings"])
    assert not (L.eventId == "5_20200708_other_0").any() and (L.eventId == "5_20200708_merger_stock_0").any()          # R-D1 P2-1: a terms row in the ledger (CSV add) -> no marker
    assert json.loads((st1 / "events" / "ca_ledger_build_info.json").read_text())["ended_markers_skipped_terms_in_ledger"] == 1
    # lead decision 2026-09-21: the marker is excluded from returns v2 (source lifecycle) and exported as a display-only row with its own reason
    import prices_returns as prm
    assert "lifecycle" in prm.EXCLUDED_SOURCES and not prm.applied_mask(L)[(L.source == "lifecycle").to_numpy()].any()
    from loader import CacheV21
    cv = CacheV21(fx["root"], threads=2)
    man = cv.export_ca_ledger(st1 / "engine" / "ca_ledger_cache.parquet", ledger_path=st1 / "events" / "ca_ledger.parquet", events_path=fx["root"] / "events" / "corporate_actions.parquet")
    E = pd.read_parquet(st1 / "engine" / "ca_ledger_cache.parquet")
    mrow = E[E.eventId == "4_20200710_other_0"]
    assert len(mrow) == 1 and mrow.markerReason.iloc[0] == "coacthist_stale_termination" and mrow.status.iloc[0] == "unresolved" and not bool(mrow.isEstimate.iloc[0]) \
        and np.isnan(mrow.childRatioCache.iloc[0]) and pd.isna(mrow.childTradingItemId.iloc[0]), mrow.T
    assert man["marker_rows"]["by_reason"]["coacthist_stale_termination"] == 1 and man["marker_rows"]["in_ledger_by_reason"]["coacthist_stale_termination"] == 1
    import ledger_qa
    assert ledger_qa.q11_export_keys(st1 / "engine" / "ca_ledger_cache.parquet")["ok"]
    # non-USD cash row valued in USD with the ex-date rate
    h = L[L.eventId == "3_20200720_cash_dividend_0"].iloc[0]
    assert h.cashCurrency == "CAD" and abs(h.cashPerShare - 1 / 1.3) < 1e-9 and abs(h.fxToUSD - 1 / 1.3) < 1e-12 and h.status == "ok"
    # source_changes classification
    sc = src.groupby(["table", "kind", "cls"]).size().to_dict()
    assert sc[("ciqdividend", "added", "window")] == 1 and sc[("ciqdividend", "added", "late_history")] == 1 and sc[("ciqdividend", "added", "future")] == 1
    assert sc[("ciqdividend", "changed", "restated")] == 1 and sc[("ciqdividend", "removed", "restated")] == 1 and sc[("ciqsplit", "added", "future")] == 1
    assert sc[("ciqdividend", "derived", "window")] == 1
    # overrides: T1-1 applied once, T1-2 applied, T1-3 duplicate -> no-op (applied, reason duplicate)
    res = pd.read_csv(st1 / "events" / "ca_ledger_csv_overrides.csv", dtype=str, keep_default_na=False).set_index("overrideId")
    assert res.at["T1-1", "status"] == "applied" and res.at["T1-2", "status"] == "applied" and res.at["T1-3", "status"] == "applied" and "duplicate" in res.at["T1-3", "reason"], res.to_dict("index")
    assert int((L.exDate == T("2020-09-01")).sum()) == 1 and int(((L.tradingItemId == 1) & (L.exDate == T("2020-04-01")) & (L.legType == "cash")).sum()) == 1
    # sidecar: asOf advanced to 07-31 and passes the update_daily staleness check; the previous (06-30) is stale against a 07-31 build
    import update_daily as ud
    side = json.loads((st1 / "events" / "ca_ledger_build_info.json").read_text())
    assert side["asOf"] == "2020-07-31" and side["incremental"]["prevAsOf"] == "2020-06-30" and side["incremental"]["revisions"]["unattributed"] == 0
    a1 = ud.ledger_asof(st1 / "events" / "ca_ledger.parquet")
    assert a1 == T("2020-07-31") and ud.ledger_staleness(a1, T("2020-07-31"), T("2020-06-30"))[0] is False
    a0 = ud.ledger_asof(fx["stage0"] / "events" / "ca_ledger.parquet")
    assert a0 == T("2020-06-30") and ud.ledger_staleness(a0, T("2020-07-31"), T("2020-06-30"))[0] is True
    # R-D1 P1-1: a missing qa list is an error, not a warning (the repair checks would silently skip)
    import shutil as _sh
    noqa = workdir() / "noqa_root"
    _sh.rmtree(noqa, ignore_errors=True); _sh.copytree(fx["root"], noqa)
    (noqa / "qa" / "T7" / "J_gdiv_systematic_scale_ids.csv").unlink()
    try:
        run_incremental(st1, "2020-07-31", workdir() / "bad_noqa", fx["ex1"], extra=["--qa-dir", str(noqa / "qa")])
        raise AssertionError("expected abort: qa lists missing")
    except SystemExit as e:
        assert "qa lists missing" in str(e), str(e)
    # staged inputs are never links into a temp root; the state records source sha and the durability flag
    st = json.loads((st1 / li.STAGE_STATE).read_text())
    assert st["files_sha256"]["calendar"] and st["qa_files_sha256"] and st["distributions_sha256"] and isinstance(st["tmp_free"], bool)
    for pth in (st1 / "root").rglob("*"):
        if pth.is_symlink():
            assert not li._is_tmp(pth.resolve()), pth
    # asOf guards: not a trading day / after the calendar / before prev without the flag
    for argv, msg in ((["--asof", "2020-08-01"], "not a trading day"), (["--asof", "2021-01-04"], "after the calendar"), (["--asof", "2020-07-01"], "before the previous asOf")):
        try:
            run_incremental(st1, argv[1], workdir() / "bad", fx["ex1"])
            raise AssertionError("expected abort: " + msg)
        except SystemExit as e:
            assert msg in str(e), (msg, str(e))


def test_step2_idempotent_rerun():
    """같은 추출·같은 asOf 로 다시 실행 → revisions 0, 원장 sha 동일, 교정 결과 동일."""
    fx = fixture()
    st1 = workdir() / "step1"
    if not (st1 / li.STAGE_INFO).exists():
        run_incremental(fx["stage0"], "2020-07-31", st1, fx["ex1"])
    st2 = workdir() / "step2"
    info = run_incremental(st1, "2020-07-31", st2, fx["ex1"])
    assert info["revisions"]["added"] == 0 and info["revisions"]["removed"] == 0 and info["revisions"]["changed_rows"] == 0, info["revisions"]
    assert info["ledger"]["sha256"] == json.loads((st1 / li.STAGE_INFO).read_text())["ledger"]["sha256"]
    r1 = pd.read_csv(st1 / "events" / "ca_ledger_csv_overrides.csv", dtype=str, keep_default_na=False)
    r2 = pd.read_csv(st2 / "events" / "ca_ledger_csv_overrides.csv", dtype=str, keep_default_na=False)
    assert r1[["overrideId", "status", "reason", "affected_rows"]].equals(r2[["overrideId", "status", "reason", "affected_rows"]])
    assert not info["source_changes"] or all(v == 0 for t in info["source_changes"].values() if isinstance(t, dict) for v in t.values()), info["source_changes"]


def test_step3_cut_matches_full_rebuild():
    """--source-cut(원천·캘린더·종가 ≤ asOf) 증분 결과 == 같은 컷의 전체 빌드(prepare-full 과 같은 조립·같은 인자) 비트 동일."""
    fx = fixture()
    st1 = workdir() / "step1"
    if not (st1 / li.STAGE_INFO).exists():
        run_incremental(fx["stage0"], "2020-07-31", st1, fx["ex1"])
    st3 = workdir() / "step3"
    info = run_incremental(st1, "2020-07-31", st3, fx["ex1"], extra=["--source-cut", "2020-07-31"])
    assert info["revisions"]["removed"] >= 1                                  # the future-declared 08-10 row leaves under the cut
    csvs = sorted((st3 / "overrides").glob("*.csv"))
    full = workdir() / "full3"
    full_stage(fx["root"], full, fx["ex1"], calendar_cut="2020-07-31", closes_cut="2020-07-31", overrides=csvs)
    A, B = pd.read_parquet(st3 / "events" / "ca_ledger.parquet"), pd.read_parquet(full / "events" / "ca_ledger.parquet")
    assert len(A) == len(B) and lb.sha256(st3 / "events" / "ca_ledger.parquet") == lb.sha256(full / "events" / "ca_ledger.parquet"), (len(A), len(B))
    assert not ((A.exDate > T("2020-07-31")) & (A.source != "manual_override")).any()      # only the CSV add (09-01) is dated after the cut


def test_diff_ledgers_attribution():
    """diff_ledgers: 추가/삭제/변경/rekeyed 와 귀속(dividendId·splitId·stitched copy·마커·inputs)."""
    rows = [dict(tradingItemId=1, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, source="ciqdividend", status="ok", evidence="dividendId=11;types=1", notes=""),
            dict(tradingItemId=5, exDate=T("2020-03-02"), eventType="split", legType="quantity", parentFactor=2.0, source="ciqsplit", status="ok", evidence="split.validFrom=2020-03-02;splitId=7", notes=""),
            dict(tradingItemId=2, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, source="stitched_copy", status="ok", evidence="copy", notes="", keeperEventId="1_20200203_cash_dividend_0", exportExclude=True)]
    A = lb.finalize([pd.DataFrame([lb._row(**r) for r in rows])]); A.attrs = {}
    rows2 = [dict(rows[0], cashPerShare=1.5), dict(rows[1], status="estimated"), dict(rows[2], cashPerShare=1.5),
             dict(tradingItemId=3, exDate=T("2020-07-10"), eventType="other", legType="stock", source="lifecycle", status="unresolved", evidence="e", notes=lb.ENDED_MARKER_TAG + "=2020-06-30"),
             dict(tradingItemId=1, exDate=T("2020-05-01"), eventType="cash_dividend", legType="cash", cashPerShare=0.2, source="ciqdividend", status="ok", evidence="dividendId=12;types=1", notes="")]
    B = lb.finalize([pd.DataFrame([lb._row(**r) for r in rows2])]); B.attrs = {}
    src = pd.DataFrame([{"table": "ciqdividend", "key": "11", "tradingItemId": 1, "exDate": T("2020-02-03"), "field": "divAmount", "before": "1.0", "after": "1.5", "kind": "changed", "cls": "restated"},
                        {"table": "ciqdividend", "key": "12", "tradingItemId": 1, "exDate": T("2020-05-01"), "field": "row", "before": None, "after": "x", "kind": "added", "cls": "window"}])
    rev, s = li.diff_ledgers(A, B, src, ["closes_cut"], "2020-07-31", "2020-06-30")
    got = {(r.kind, r.eventId or r.eventIdPrev, r.field): r.source for r in rev.itertuples(index=False)}
    # R-D1 P2-2: a removed row that carried a CSV tag is 'superseded_or_dropped', a removed vendor row is attributed by its dividendId
    C = lb.finalize([pd.DataFrame([lb._row(**dict(rows[0], repairOf="csv_override:X-1")), lb._row(**rows[1])])]); C.attrs = {}
    D = lb.finalize([pd.DataFrame([lb._row(**rows[1])])]); D.attrs = {}
    rev_r, _ = li.diff_ledgers(C, D, src, [], "2020-07-31", "2020-06-30")
    assert rev_r.kind.tolist() == ["removed"] and rev_r.source.iloc[0] == "override:csv_superseded_or_dropped"
    rev_r2, _ = li.diff_ledgers(A, D, src, [], "2020-07-31", "2020-06-30")
    assert set(rev_r2[rev_r2.kind == "removed"].source) == {"ciqdividend", "stitched_copy_of:1_20200203_cash_dividend_0"}
    assert got[("changed", "1_20200203_cash_dividend_0", "cashPerShare")] == "ciqdividend"
    assert got[("changed", "2_20200203_cash_dividend_0", "cashPerShare")] == "stitched_copy_of:1_20200203_cash_dividend_0"
    assert got[("changed", "5_20200302_split_0", "status")] == "inputs:closes_cut"
    assert got[("added", "3_20200710_other_0", "row")] == "rule:ended_without_terms_marker" and got[("added", "1_20200501_cash_dividend_0", "row")] == "ciqdividend"
    assert s["added"] == 2 and s["removed"] == 0 and s["changed_rows"] == 3 and s["unattributed"] == 0
    rev2, s2 = li.diff_ledgers(A, A, None, [], "2020-07-31", "2020-06-30")
    assert len(rev2) == 0 and s2["changed_rows"] == 0


def test_real_gate_results():
    """실데이터 게이트 축약: gate_results.json 이 있으면 세 날짜 전부 identical (없으면 skip)."""
    p = Path(os.environ.get("D1_GATE_RESULTS", "/home/jgo/longshort/daily_D1/gate_results.json"))
    if not p.exists():
        print("skip: no gate results at", p)
        return
    res = json.loads(p.read_text())["results"]
    ok = [r for r in res if r.get("full_mode") == "refresh"]
    assert len(ok) >= 3 and all(r["identical"] for r in ok), [(r["date"], r.get("cells_differ"), r.get("only_a"), r.get("only_b")) for r in ok]


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
            rows.append((t.__name__, "FAIL", f"{type(e).__name__}: {str(e)[:300]}"))
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
