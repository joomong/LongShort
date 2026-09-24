#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/ledger_incremental.py - D1: `ledger_build.py --incremental` — 이전 빌드의 원장을 T−1 까지 이어가는 일별 증분.

원리
  원장은 입력의 결정적 함수다(ledger_build.build; 같은 입력 → 같은 sha, 실측 2026-09-21). 증분은 **원천 조회를 증분**으로 하고
  (원장이 직접 읽는 xfeed 원천 = ciqdividend → dividends/dividends_long.parquet, ciqsplit·ciqsplitcache·ciqdividendcache(비현금)·
  ciqcurrency → work/work_*.parquet, co_acthist 창 = 후보 기록), 그 밖의 입력(calendar·factors·events·lineage·lifecycle·base_data·
  universe·qa 목록)은 새 빌드의 파일을 그대로 받으며, 규칙(귀속·stitched copy·표시용 마커·전환일·정산 bar 정렬·reconcile·교정 CSV
  idempotent 재적용)은 전체 빌드와 **같은 코드 경로**(ledger_build.build) 로 다시 계산한다. 이전 원장과 새 원장의 행·값 차이가
  revisions(`events/ca_ledger_revisions/date=<asOf>/revisions.parquet`) 이고, 원천 표의 행·값 차이가 같은 폴더의 source_changes.parquet 다.

원천 조회(읽기 전용 세션, id 청크 ≤ 1000, 동시 1 쿼리)
  ciqdividend      : 종목 집합 전체를 id 청크로 재조회(23 청크·5 초 실측) — 창 [prev asOf−10일, ∞) 의 새/변경 행과 이전 표의 모든 원천 키
                     (dividendId) 재조회를 한 번에 포함한다(창 밖 과거 행의 재기술·삭제·지연 적재도 감지). 저장 표 dividends_long 과
                     dividendId 로 대조 → added / changed(field) / removed 를 기록하고, 변경·추가 행만 파생 컬럼(fxRate·divAmount_USD·
                     adjustmentFactorAtEx·divAmountCacheUnit·isCash·isStock·companyName·symbol)을 다시 계산한다(불변 행은 저장값 유지).
                     inRange 는 매번 전 행 재계산(2000-01-03 ≤ exDate ≤ asOf). fxRate 가 NaN 인 비-USD 행(exDate ≤ asOf)은 환율 재조회.
  ciqsplit·ciqsplitcache·ciqdividendcache(비현금)·ciqcurrency : 전량 재조회 → 키 정렬 → 저장 표와 대조(added/changed/removed).
  co_acthist       : datadate ∈ [prev asOf−10일, asOf] 행을 후보로 기록(원장은 distributions 프로토 표·R3 인계·교정 CSV 로만 읽음; 표는 정적).
  분류(cls)        : window(창 안) / late_history(창보다 오래된 새 행) / restated(창보다 오래된 행의 값 변경·삭제) / future(asOf 뒤 exDate).

스테이지 레이아웃(<out>/; rc2f R1 과 같은 자리 + 상태 파일)
  root/        조립 입력: calendar.parquet, dividends/dividends_long.parquet(갱신본), distributions/(이전 것 링크), factors/{split,dividend}.parquet,
               events/corporate_actions.parquet, master/{instruments,current,lineage,lifecycle,lifecycle_aux}.parquet, universe/membership.parquet, qa/
  work/        work_{ciqsplit,ciqsplitcache,divcache_noncash,ciqcurrency}.parquet(갱신본), gap_candidates_union.csv(누적 carry), co_acthist_window.parquet,
               anchor_closes.parquet(빌드가 만듦; 증분은 매번 새로 읽음)
  overrides/   <task>__ledger_overrides*.csv (glob 사본, rc2f_apply.validate_files 로 검증)
  handover/    for_R1_events.csv, split_boundary_moves.csv, price_override_estimates.csv, overrides_ref_ledger.parquet (이전 스테이지에서 carry)
  events/      ca_ledger.parquet + sidecar(ca_ledger_build_info.json: asOf = --asof, incremental 블록) + repairs/stitched/dropped/... +
               ca_ledger_revisions/date=<asOf>/{revisions.parquet, source_changes.parquet, summary.json}
  qa/ledger/   ledger_qa.md/json (Q1~Q17)      engine/ (선택: loader.export_ca_ledger 호출 확인)
  ledger_incremental_state.json / ledger_incremental_info.json

CLI (ledger_build.py 가 --incremental 일 때 main_incremental 로 위임)
  ledger_build.py --incremental --prev <이전 D1 스테이지 | rc2f R1 스테이지 | 발행본 root> --asof <T−1> --out <stage>
      [--build <update_daily out-stage 또는 발행본>]            새 빌드 입력(calendar·factors·events·master·universe·base_data) 일괄 지정
      [--calendar --split-factors --dividend-factors --events --instruments --current --membership --qa-dir]   개별 지정(기본: --build, 없으면 --prev 의 것)
      [--base-data --aux-base-data --lineage --lifecycle --r3-events --gap-candidates]                        (기본: --build/--prev 의 것)
      [--overrides-glob '<glob>'] [--rc2-ref <rc2 참조 빌드>] [--overrides-ref-ledger] [--split-moves] [--estimated-prices]
      [--no-db] [--source-extract <dir>] [--source-cut <date>] [--no-qa] [--export-root <dir> --export-master-root <dir>]
  ledger_incremental.py extract --ids-from <instruments.parquet> --out <dir> --prev-asof <d> --asof <d>   (원천 추출만; 게이트·테스트용)
  ledger_incremental.py prepare-full --baseline <rc2f R1 스테이지> --extract <dir> --cut <date> --out <stage>  (전체 재생성용 root/work 조립)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_build as lb  # noqa: E402
from db import SCHEMA, rss_gb  # noqa: E402

log = logging.getLogger("cache_v21.ledger_incremental")

USD = 160
FX_SNAP = 6                      # rc1 proto build_cache_v2.FX_SNAP
FX_TOL_DAYS = 10                 # last rate on/before exDate within 10 days (proto)
FX_START = "1999-06-01"
DIV_START = pd.Timestamp("2000-01-03")      # proto inRange start (base_data first date)
WINDOW_BACK_DAYS = 10
CASH_DIV_TYPES = (1, 4, 5, 6, 7, 8, 10, 15, 17, 18, 19, 23)   # proto isCash (build_cache_v2; the ledger's own CASH_TYPES adds 33)
STOCK_DIV_TYPES = (11, 20, 21, 22)
STAGE_STATE = "ledger_incremental_state.json"
STAGE_INFO = "ledger_incremental_info.json"
REVISIONS_DIR = "ca_ledger_revisions"
LEDGER_KEY = ["tradingItemId", "exDate", "eventType", "legType", "childTradingItemId"]
DEFAULT_REF_DIR = Path("/home/jgo/longshort/daily/ref")            # durable home for references that used to live in the session scratchpad (R-D1 P1-1)
DEFAULT_RC2_REF = DEFAULT_REF_DIR / "rc2_ref"                          # slim rc2 reference (make-ref); falls back to the scratchpad build_rc2_final
SCRATCH_RC2_REF = lb.SCRATCH / "build_rc2_final"
RC2_REF_FILES = ("events/ca_ledger.parquet", "master/lifecycle.parquet", "calendar.parquet", "base_data_ids.parquet")   # what rc2f_apply.Ref reads for ledger CSVs
TMP_ROOTS = ("/tmp",)                                                  # inputs resolved under these roots are copied into the stage, never linked
DEFAULT_OVERRIDES_GLOB = str(HERE / "overrides" / "rc2f" / "*" / "*.csv")     # X*/ + any new sub-directory; only ledger_*.csv are the ledger's

DIV_VENDOR_COLS = ["dividendId", "tradingItemId", "exDate", "payDate", "recordDate", "announcedDate", "divAmount", "netAmount", "grossAmount",
                   "currencyId", "exchangeId", "divFreqTypeId", "dividendTypeId", "dividendTypeName", "supplementalTypeId", "supplementalTypeName"]
DIV_DERIVED_COLS = ["companyName", "symbol", "fxRate", "divAmount_USD", "adjustmentFactorAtEx", "divAmountCacheUnit", "isCash", "isStock", "inRange"]
DIV_COLS = DIV_VENDOR_COLS + DIV_DERIVED_COLS
DIV_SCHEMA = pa.schema([
    ("dividendId", pa.int64()), ("tradingItemId", pa.int64()), ("exDate", pa.timestamp("ns")), ("payDate", pa.timestamp("ns")),
    ("recordDate", pa.timestamp("ns")), ("announcedDate", pa.timestamp("ns")), ("divAmount", pa.float64()), ("netAmount", pa.float64()),
    ("grossAmount", pa.float64()), ("currencyId", pa.float64()), ("exchangeId", pa.float64()), ("divFreqTypeId", pa.float64()),
    ("dividendTypeId", pa.int64()), ("dividendTypeName", pa.string()), ("supplementalTypeId", pa.float64()), ("supplementalTypeName", pa.string()),
    ("companyName", pa.string()), ("symbol", pa.string()), ("fxRate", pa.float64()), ("divAmount_USD", pa.float64()),
    ("adjustmentFactorAtEx", pa.float64()), ("divAmountCacheUnit", pa.float64()), ("isCash", pa.bool_()), ("isStock", pa.bool_()), ("inRange", pa.bool_()),
])
DIV_SORT = ["tradingItemId", "exDate", "dividendId"]
SQL_DIV = (f"select d.dividendid, d.tradingitemid, d.exdate, d.paydate, d.recorddate, d.announceddate, d.divamount, d.netamount, d.grossamount, "
           f"d.currencyid, d.exchangeid, d.divfreqtypeid, d.dividendtypeid, dt.dividendtypename, d.supplementaltypeid, st.supplementaltypename "
           f"from {SCHEMA}.ciqdividend d left join {SCHEMA}.ciqdividendtype dt on dt.dividendtypeid = d.dividendtypeid "
           f"left join {SCHEMA}.ciqdividendsupplementaltype st on st.supplementaltypeid = d.supplementaltypeid where d.tradingitemid = any(%s)")
SQL_FX = f"select currencyid, pricedate, priceclose as fx from {SCHEMA}.ciqexchangerate where snapid = %s and pricedate >= %s and currencyid = any(%s)"
SQL_CA = (f"select gvkey, datadate, actioncd, actiontxt, gvkeyaqc, iid, newconm, newtic1, oldconm, oldtic1 from {SCHEMA}.co_acthist "
          f"where datadate >= %s and datadate <= %s")
# work tables: key, date column used by --source-cut, SQL (same as ledger_build.pull_db), dtypes (rc2 work parquet)
WORK_TABLES = {
    "ciqsplit": {"key": ["splitid"], "date": "exdate", "by_id": True,
                 "sql": f"select splitid, tradingitemid, exdate, paydate, recorddate, announceddate, rate, splittypeid, appliedflag from {SCHEMA}.ciqsplit where tradingitemid = any(%s)",
                 "dtypes": {"splitid": "int64", "tradingitemid": "int64", "exdate": "datetime64[ns]", "paydate": "datetime64[ns]", "recorddate": "datetime64[ns]",
                            "announceddate": "datetime64[ns]", "rate": "float64", "splittypeid": "int64", "appliedflag": "int64"}},
    "ciqsplitcache": {"key": ["tradingitemid", "splitdate", "nextsplitdate"], "date": "splitdate", "by_id": True,
                      "sql": f"select tradingitemid, cumulativesplitfactor, latestsplitfactor, splitdate, nextsplitdate from {SCHEMA}.ciqsplitcache where tradingitemid = any(%s)",
                      "dtypes": {"tradingitemid": "int64", "cumulativesplitfactor": "float64", "latestsplitfactor": "float64", "splitdate": "datetime64[ns]", "nextsplitdate": "datetime64[ns]"}},
    "divcache_noncash": {"key": ["tradingitemid", "dividenddate", "nextdividenddate"], "date": "dividenddate", "by_id": True,
                         "sql": (f"select tradingitemid, dividenddate, nextdividenddate, paydate, recorddate, divamount, currencyid, adjustmentfactor, splitadjdivamount, "
                                 f"dividendpaymenttypeid from {SCHEMA}.ciqdividendcache where tradingitemid = any(%s) and dividendpaymenttypeid <> 1"),
                         "dtypes": {"tradingitemid": "int64", "dividenddate": "datetime64[ns]", "nextdividenddate": "datetime64[ns]", "paydate": "datetime64[ns]",
                                    "recorddate": "datetime64[ns]", "divamount": "float64", "currencyid": "int64", "adjustmentfactor": "float64",
                                    "splitadjdivamount": "float64", "dividendpaymenttypeid": "int64"}},
    "ciqcurrency": {"key": ["currencyid"], "date": None, "by_id": False, "sql": f"select currencyid, isocode from {SCHEMA}.ciqcurrency order by 1",
                    "dtypes": {"currencyid": "int64", "isocode": "object"}},
}
ROOT_INPUTS = ("calendar", "split", "dividend_factor", "events", "instruments", "current", "membership", "lineage", "lifecycle")


def sha256(path) -> str:
    return lb.sha256(Path(path))


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _ts(x) -> pd.Timestamp:
    return pd.Timestamp(x).normalize()


def _link(src: Path, dst: Path) -> None:
    """absolute symlink (replace)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    os.symlink(str(Path(src).resolve()), str(dst))


def _is_tmp(path: Path) -> bool:
    r = str(Path(path).resolve())
    return any(r == t or r.startswith(t + "/") for t in TMP_ROOTS)


def _place(src: Path, dst: Path) -> str:
    """stage input: symlink when the resolved source is durable, copy when it lives under a temp root (scratchpad). Returns 'link' / 'copy'."""
    src = Path(src)
    if _is_tmp(src):
        if src.resolve().is_dir():
            if dst.is_symlink() or dst.exists():
                shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
            shutil.copytree(src.resolve(), dst)
        else:
            _copy(src.resolve(), dst)
        return "copy"
    _link(src, dst)
    return "link"


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)


def _write_df(df: pd.DataFrame, path: Path, schema: pa.Schema | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    t = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    pq.write_table(t, tmp, compression="zstd")
    os.replace(tmp, path)


def _eq(a: pd.Series, b: pd.Series) -> np.ndarray:
    """NaN/NaT/None-aware elementwise equality (exact)."""
    a = a.to_numpy(dtype=object) if a.dtype == object else a.to_numpy()
    b = b.to_numpy(dtype=object) if b.dtype == object else b.to_numpy()
    na_a, na_b = pd.isna(a), pd.isna(b)
    both_na = na_a & na_b
    if a.dtype == object or b.dtype == object:
        eq = np.array([(x == y) for x, y in zip(a, b)], dtype=bool) if len(a) else np.zeros(0, dtype=bool)
    else:
        with np.errstate(invalid="ignore"):
            eq = (a == b)
    return (eq & ~na_a & ~na_b) | both_na


def _s(v) -> str | None:
    """stringify a cell for the revisions/source_changes files (None for NaN/NaT)."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or (isinstance(v, (pd.Timestamp, np.datetime64)) and pd.isna(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(v).date())
    if isinstance(v, (float, np.floating)):
        return repr(float(v))
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return str(v)


# --------------------------------------------------------------------------------------
# source extraction (xfeed read-only) and normalisation
# --------------------------------------------------------------------------------------
def normalize_div(raw: pd.DataFrame) -> pd.DataFrame:
    """DB frame (lower-case columns) → dividends_long vendor columns with the proto dtypes."""
    ren = {"dividendid": "dividendId", "tradingitemid": "tradingItemId", "exdate": "exDate", "paydate": "payDate", "recorddate": "recordDate",
           "announceddate": "announcedDate", "divamount": "divAmount", "netamount": "netAmount", "grossamount": "grossAmount", "currencyid": "currencyId",
           "exchangeid": "exchangeId", "divfreqtypeid": "divFreqTypeId", "dividendtypeid": "dividendTypeId", "dividendtypename": "dividendTypeName",
           "supplementaltypeid": "supplementalTypeId", "supplementaltypename": "supplementalTypeName"}
    d = raw.rename(columns=ren)
    for c in DIV_VENDOR_COLS:
        if c not in d.columns:
            d[c] = None
    d = d[DIV_VENDOR_COLS].copy()
    for c in ("dividendId", "tradingItemId", "dividendTypeId"):
        d[c] = d[c].astype("int64")
    for c in ("exDate", "payDate", "recordDate", "announcedDate"):
        d[c] = pd.to_datetime(d[c]).astype("datetime64[ns]")
    for c in ("divAmount", "netAmount", "grossAmount", "currencyId", "exchangeId", "divFreqTypeId", "supplementalTypeId"):
        d[c] = pd.to_numeric(d[c].astype(object).where(d[c].notna(), np.nan), errors="raise").astype("float64")
    for c in ("dividendTypeName", "supplementalTypeName"):
        d[c] = d[c].astype(object).where(d[c].notna(), None)
    if d.dividendId.duplicated().any():
        raise ValueError("ciqdividend extract has duplicate dividendId")
    return d.sort_values(DIV_SORT, kind="stable").reset_index(drop=True)


def normalize_work(name: str, raw: pd.DataFrame) -> pd.DataFrame:
    """ledger_build.pull_db conversions + dtype contract + key sort (deterministic row order)."""
    spec = WORK_TABLES[name]
    d = raw.copy()
    for c, t in spec["dtypes"].items():
        if c not in d.columns:
            d[c] = None
        if t.startswith("datetime64"):
            d[c] = pd.to_datetime(d[c]).astype("datetime64[ns]")
        elif t == "float64":
            d[c] = pd.to_numeric(d[c].astype(object).where(d[c].notna(), np.nan), errors="raise").astype("float64")
        elif t == "int64":
            if d[c].isna().any():
                log.warning("work %s: column %s has nulls -> float64 (contract int64)", name, c)
                d[c] = pd.to_numeric(d[c].astype(object).where(d[c].notna(), np.nan), errors="raise").astype("float64")
            else:
                d[c] = d[c].astype("int64")
        else:
            d[c] = d[c].astype(object).where(d[c].notna(), None)
    d = d[list(spec["dtypes"].keys())]
    if d.duplicated(spec["key"]).any():
        raise ValueError(f"work {name}: duplicate keys {spec['key']}")
    return d.sort_values(spec["key"], kind="stable").reset_index(drop=True)


def extract_sources(ids, out_dir: Path, prev_asof, asof, application_name: str = "cache_v21_D1_ledger") -> dict:
    """xfeed 읽기 전용 세션으로 원장 직접 원천을 <out_dir>/raw_*.parquet 로 추출(id 청크 ≤ 1000, 순차). 반환 info."""
    from db import DB, chunked, wait_for_quiet

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = sorted(set(int(i) for i in ids))
    prev_asof, asof = _ts(prev_asof), _ts(asof)
    info = {"at": _now(), "ids": len(ids), "prevAsOf": str(prev_asof.date()), "asOf": str(asof.date()), "tables": {}, "chunk": 1000}
    db = DB(application_name=application_name)
    try:
        info["wait_for_quiet"] = wait_for_quiet(db)
        t0 = time.time()
        parts = [db.query(SQL_DIV, (c,)) for c in chunked(ids, 1000)]
        div = normalize_div(pd.concat(parts, ignore_index=True)) if parts else normalize_div(pd.DataFrame(columns=[c.lower() for c in DIV_VENDOR_COLS]))
        _write_df(div, out_dir / "raw_ciqdividend.parquet")
        info["tables"]["ciqdividend"] = {"rows": int(len(div)), "queries": len(parts), "seconds": round(time.time() - t0, 1)}
        t0 = time.time()
        ccy = sorted(set(int(c) for c in div.currencyId.dropna()) - {USD})
        if ccy:
            fx = db.query(SQL_FX, (FX_SNAP, FX_START, ccy))
            fx = pd.DataFrame({"currencyid": fx.currencyid.astype("int64"), "pricedate": pd.to_datetime(fx.pricedate).astype("datetime64[ns]"),
                               "fx": pd.to_numeric(fx.fx.astype(object), errors="raise").astype("float64")})
        else:
            fx = pd.DataFrame({"currencyid": pd.Series([], dtype="int64"), "pricedate": pd.Series([], dtype="datetime64[ns]"), "fx": pd.Series([], dtype="float64")})
        fx = fx.sort_values(["currencyid", "pricedate"], kind="stable").reset_index(drop=True)
        _write_df(fx, out_dir / "raw_fx.parquet")
        info["tables"]["fx"] = {"rows": int(len(fx)), "currencies": len(ccy), "seconds": round(time.time() - t0, 1)}
        for name, spec in WORK_TABLES.items():
            t0 = time.time()
            if spec["by_id"]:
                parts = [db.query(spec["sql"], (c,)) for c in chunked(ids, 1000)]
                raw = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=list(spec["dtypes"]))
            else:
                parts = [db.query(spec["sql"])]
                raw = parts[0]
            w = normalize_work(name, raw)
            _write_df(w, out_dir / f"raw_{name}.parquet")
            info["tables"][name] = {"rows": int(len(w)), "queries": len(parts), "seconds": round(time.time() - t0, 1)}
        t0 = time.time()
        mx = db.query(f"select max(datadate) mx from {SCHEMA}.co_acthist").iloc[0, 0]
        info["co_acthist_max_datadate"] = str(pd.Timestamp(mx).date()) if mx is not None and pd.notna(mx) else None
        ca = db.query(SQL_CA, (prev_asof - pd.Timedelta(days=WINDOW_BACK_DAYS), asof))
        ca["datadate"] = pd.to_datetime(ca.datadate).astype("datetime64[ns]")
        _write_df(ca.sort_values(["gvkey", "datadate", "actioncd"], kind="stable").reset_index(drop=True), out_dir / "raw_co_acthist_window.parquet")
        info["tables"]["co_acthist_window"] = {"rows": int(len(ca)), "queries": 1, "seconds": round(time.time() - t0, 1)}
        info["db"] = {"queries": db.n_queries, "seconds": round(db.seconds, 1)}
    finally:
        db.close()
    (out_dir / "extract_info.json").write_text(json.dumps(info, indent=1, default=str))
    return info


def load_extract(ex_dir: Path) -> dict:
    ex_dir = Path(ex_dir)
    out = {"ciqdividend": pd.read_parquet(ex_dir / "raw_ciqdividend.parquet"), "fx": pd.read_parquet(ex_dir / "raw_fx.parquet")}
    for name in WORK_TABLES:
        out[name] = pd.read_parquet(ex_dir / f"raw_{name}.parquet")
    p = ex_dir / "raw_co_acthist_window.parquet"
    out["co_acthist_window"] = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=["gvkey", "datadate", "actioncd", "actiontxt", "gvkeyaqc", "iid", "newconm", "newtic1", "oldconm", "oldtic1"])
    ip = ex_dir / "extract_info.json"
    out["_info"] = json.loads(ip.read_text()) if ip.exists() else {}
    return out


def cut_extract(ex: dict, cut) -> dict:
    """게이트 시뮬레이션: '그 날까지의 원천' = 날짜 컬럼(exDate/exdate/splitdate/dividenddate/pricedate/datadate) ≤ cut 인 행만."""
    cut = _ts(cut)
    out = dict(ex)
    out["ciqdividend"] = ex["ciqdividend"][ex["ciqdividend"].exDate <= cut].reset_index(drop=True)
    out["fx"] = ex["fx"][ex["fx"].pricedate <= cut].reset_index(drop=True)
    for name, spec in WORK_TABLES.items():
        if spec["date"]:
            out[name] = ex[name][ex[name][spec["date"]] <= cut].reset_index(drop=True)
    if len(ex["co_acthist_window"]):
        out["co_acthist_window"] = ex["co_acthist_window"][ex["co_acthist_window"].datadate <= cut].reset_index(drop=True)
    info = dict(ex.get("_info", {}))
    mx = info.get("co_acthist_max_datadate")
    if mx and pd.Timestamp(mx) > cut:
        info["co_acthist_max_datadate"] = str(cut.date())
    out["_info"] = info
    return out


# --------------------------------------------------------------------------------------
# dividends_long refresh (proto contract preserved; derived columns recomputed for added/changed rows only)
# --------------------------------------------------------------------------------------
class Refs:
    """파생 컬럼 참조: 회사명(instruments)·심볼(current)·환율(fx)·분할 구간 계수(factors/split)."""

    def __init__(self, instruments: pd.DataFrame | None, current: pd.DataFrame | None, fx: pd.DataFrame | None, split: pd.DataFrame | None):
        self.company = {} if instruments is None or "companyName" not in instruments.columns else \
            {int(t): (None if pd.isna(n) else str(n)) for t, n in zip(instruments.tradingItemId, instruments.companyName)}
        self.symbol = {} if current is None or "symbol" not in current.columns else \
            {int(t): (None if pd.isna(s) else str(s)) for t, s in zip(current.tradingItemId, current.symbol)}
        self.fx = fx.sort_values("pricedate", kind="stable").reset_index(drop=True) if fx is not None and len(fx) else None
        self.split = None
        if split is not None and len(split):
            s = split[["tradingItemId", "validFrom", "validTo", "factor"]].copy()
            s["validFrom"] = pd.to_datetime(s.validFrom)
            s["validTo"] = pd.to_datetime(s.validTo)
            self.split = {tid: g.sort_values("validFrom") for tid, g in s.groupby("tradingItemId")}

    def fx_at(self, currency: pd.Series, exdate: pd.Series) -> np.ndarray:
        out = np.where(currency.to_numpy(dtype=float) == USD, 1.0, np.nan)
        non = currency.notna().to_numpy() & (currency.to_numpy(dtype=float) != USD) & exdate.notna().to_numpy()
        if non.any() and self.fx is not None:
            sub = pd.DataFrame({"i": np.flatnonzero(non), "currencyid": currency.to_numpy(dtype=float)[non].astype("int64"),
                                "exdate": exdate.to_numpy()[non]}).sort_values("exdate", kind="stable")
            m = pd.merge_asof(sub, self.fx, left_on="exdate", right_on="pricedate", by="currencyid", direction="backward", tolerance=pd.Timedelta(days=FX_TOL_DAYS))
            out[m.i.to_numpy()] = m.fx.to_numpy(dtype=float)
        return out

    def split_factor_at(self, tid: pd.Series, exdate: pd.Series) -> np.ndarray:
        out = np.full(len(tid), np.nan)
        if self.split is None:
            return out
        for k, (t, d) in enumerate(zip(tid.to_numpy(), exdate.to_numpy())):
            g = self.split.get(int(t))
            if g is None or pd.isna(d):
                continue
            d = pd.Timestamp(d)
            hit = g[(g.validFrom <= d) & (g.validTo.isna() | (g.validTo > d))]
            if len(hit):
                out[k] = float(hit.factor.iloc[-1])
        return out


def _derive(d: pd.DataFrame, refs: Refs, name_by_id: dict, sym_by_id: dict, asof) -> pd.DataFrame:
    """파생 컬럼 계산(프로토 build_dividends 와 같은 규칙). name_by_id/sym_by_id: 저장 표의 같은 id 행 값(우선), 없으면 refs."""
    d = d.copy()
    d["companyName"] = [name_by_id.get(int(t), refs.company.get(int(t))) for t in d.tradingItemId]
    d["symbol"] = [sym_by_id.get(int(t), refs.symbol.get(int(t))) for t in d.tradingItemId]
    d["fxRate"] = refs.fx_at(d.currencyId, d.exDate)
    d["divAmount_USD"] = d.divAmount / d.fxRate
    d["adjustmentFactorAtEx"] = refs.split_factor_at(d.tradingItemId, d.exDate)
    d["divAmountCacheUnit"] = d.divAmount_USD / d.adjustmentFactorAtEx
    d["isCash"] = d.dividendTypeId.isin(CASH_DIV_TYPES)
    d["isStock"] = d.dividendTypeId.isin(STOCK_DIV_TYPES)
    d["inRange"] = (d.exDate >= DIV_START) & (d.exDate <= _ts(asof))
    return d[DIV_COLS]


def _finish_div(d: pd.DataFrame, asof) -> pd.DataFrame:
    d = d[DIV_COLS].copy()
    d["inRange"] = (d.exDate >= DIV_START) & (d.exDate <= _ts(asof))
    d["companyName"] = d.companyName.astype(object).where(d.companyName.notna(), None)
    d["symbol"] = d.symbol.astype(object).where(d.symbol.notna(), None)
    for c in ("dividendTypeName", "supplementalTypeName"):
        d[c] = d[c].astype(object).where(d[c].notna(), None)
    for c in ("isCash", "isStock", "inRange"):
        d[c] = d[c].astype(bool)
    return d.sort_values(DIV_SORT, kind="stable").reset_index(drop=True)


def refresh_dividends(prev: pd.DataFrame, raw: pd.DataFrame, refs: Refs, asof, prev_asof=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """저장 표(prev) 를 원천 추출(raw, 정규화됨) 로 갱신. 반환 (새 표, source_changes 행).

    changes 컬럼: table, key, tradingItemId, exDate, field, before, after, kind(added/changed/removed/derived), cls."""
    asof = _ts(asof)
    prev_asof = _ts(prev_asof) if prev_asof is not None else None
    win_lo = (prev_asof - pd.Timedelta(days=WINDOW_BACK_DAYS)) if prev_asof is not None else None
    prev = prev[DIV_COLS].copy()
    raw = raw[DIV_VENDOR_COLS].copy()
    pk = prev.set_index("dividendId", drop=False)
    rk = raw.set_index("dividendId", drop=False)
    if not pk.index.is_unique or not rk.index.is_unique:
        raise ValueError("dividendId must be unique in both the stored table and the extract")
    added = rk.index.difference(pk.index)
    removed = pk.index.difference(rk.index)
    common = pk.index.intersection(rk.index)
    p_c, r_c = pk.loc[common], rk.loc[common]
    changed_mask = np.zeros(len(common), dtype=bool)
    diff_cols = {}
    for c in DIV_VENDOR_COLS:
        if c == "dividendId":
            continue
        ne = ~_eq(p_c[c], r_c[c])
        if ne.any():
            diff_cols[c] = ne
            changed_mask |= ne
    changed = common[changed_mask]

    def cls_of(ex, kind):
        if pd.isna(ex):
            return "unknown"
        ex = pd.Timestamp(ex)
        if ex > asof:
            return "future"
        if win_lo is not None and ex < win_lo:
            return "late_history" if kind == "added" else "restated"
        return "window"

    rows = []
    for k in added:
        r = rk.loc[k]
        rows.append({"table": "ciqdividend", "key": str(int(k)), "tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "field": "row", "before": None,
                     "after": f"type={int(r.dividendTypeId)} amount={_s(r.divAmount)}", "kind": "added", "cls": cls_of(r.exDate, "added")})
    for k in removed:
        r = pk.loc[k]
        rows.append({"table": "ciqdividend", "key": str(int(k)), "tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "field": "row",
                     "before": f"type={int(r.dividendTypeId)} amount={_s(r.divAmount)}", "after": None, "kind": "removed", "cls": cls_of(r.exDate, "removed")})
    for c, ne in diff_cols.items():
        for k in common[ne]:
            rows.append({"table": "ciqdividend", "key": str(int(k)), "tradingItemId": int(pk.at[k, "tradingItemId"]), "exDate": pk.at[k, "exDate"], "field": c,
                         "before": _s(pk.at[k, c]), "after": _s(rk.at[k, c]), "kind": "changed", "cls": cls_of(pk.at[k, "exDate"], "changed")})
    # stored names take precedence for an id that already has rows (proto master names), else master files
    first = prev.sort_values(DIV_SORT, kind="stable").drop_duplicates("tradingItemId")
    name_by_id = {int(t): n for t, n in zip(first.tradingItemId, first.companyName)}
    sym_by_id = {int(t): s for t, s in zip(first.tradingItemId, first.symbol)}
    keep = pk.loc[common[~changed_mask]]
    upd = _derive(rk.loc[changed], refs, name_by_id, sym_by_id, asof) if len(changed) else prev.iloc[:0]
    add = _derive(rk.loc[added], refs, name_by_id, sym_by_id, asof) if len(added) else prev.iloc[:0]
    new = pd.concat([keep[DIV_COLS], upd[DIV_COLS], add[DIV_COLS]], ignore_index=True)
    # R-D1 P1-4: the FX table is refetched in full every run, so every non-USD row's fxRate is recomputed from it (a future ex-date row first seen
    # with a provisional rate converges to the ex-date rate; an FX restatement propagates); changes are recorded as kind=derived (source fx)
    non_usd = new.currencyId.notna() & (new.currencyId != USD)
    if non_usd.any() and refs.fx is not None:
        fxv = refs.fx_at(new.loc[non_usd, "currencyId"], new.loc[non_usd, "exDate"])
        old_fx = new.loc[non_usd, "fxRate"].to_numpy(dtype=float)
        changed_fx = ~((old_fx == fxv) | (np.isnan(old_fx) & np.isnan(fxv)))
        touched = new.index[non_usd][changed_fx]
        for i, v, o in zip(touched, fxv[changed_fx], old_fx[changed_fx]):
            rows.append({"table": "ciqdividend", "key": str(int(new.at[i, "dividendId"])), "tradingItemId": int(new.at[i, "tradingItemId"]), "exDate": new.at[i, "exDate"],
                         "field": "fxRate", "before": _s(o), "after": _s(v), "kind": "derived", "cls": cls_of(new.at[i, "exDate"], "changed")})
        if len(touched):
            new.loc[touched, "fxRate"] = fxv[changed_fx]
            new.loc[touched, "divAmount_USD"] = new.loc[touched, "divAmount"] / new.loc[touched, "fxRate"]
            new.loc[touched, "divAmountCacheUnit"] = new.loc[touched, "divAmount_USD"] / new.loc[touched, "adjustmentFactorAtEx"]
    new = _finish_div(new, asof)
    ch = pd.DataFrame(rows, columns=["table", "key", "tradingItemId", "exDate", "field", "before", "after", "kind", "cls"])
    log.info("dividends_long refresh: prev %d -> new %d (added %d, changed rows %d, removed %d, fx recomputed %d)", len(prev), len(new), len(added), len(changed), len(removed),
             int((ch.kind == "derived").sum()) if len(ch) else 0)
    return new, ch


def build_dividends_scratch(raw: pd.DataFrame, refs: Refs, asof) -> pd.DataFrame:
    """게이트 (B): 파생 컬럼을 저장값 없이 전부 다시 계산(프로토 build_dividends 규칙; 이름은 master 파일)."""
    return _finish_div(_derive(raw[DIV_VENDOR_COLS].copy(), refs, {}, {}, asof), asof)


def refresh_work(name: str, prev: pd.DataFrame | None, raw: pd.DataFrame, asof, prev_asof=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """work 표(전량 재조회) 대조 → (새 표 = 정규화·키 정렬한 raw, changes)."""
    spec = WORK_TABLES[name]
    new = normalize_work(name, raw)
    rows = []
    if prev is None or not len(prev):
        for r in new.itertuples(index=False):
            rows.append({"table": name, "key": "|".join(_s(getattr(r, k)) or "" for k in spec["key"]), "tradingItemId": int(getattr(r, "tradingitemid", -1)) if "tradingitemid" in new.columns else -1,
                         "exDate": getattr(r, spec["date"]) if spec["date"] else pd.NaT, "field": "row", "before": None, "after": "new table", "kind": "added", "cls": "initial"})
        return new, pd.DataFrame(rows, columns=["table", "key", "tradingItemId", "exDate", "field", "before", "after", "kind", "cls"])
    prev = normalize_work(name, prev)
    kp = prev.set_index(spec["key"], drop=False)
    kn = new.set_index(spec["key"], drop=False)
    added = kn.index.difference(kp.index)
    removed = kp.index.difference(kn.index)
    common = kp.index.intersection(kn.index)
    asof = _ts(asof)
    win_lo = (_ts(prev_asof) - pd.Timedelta(days=WINDOW_BACK_DAYS)) if prev_asof is not None else None

    def keystr(k):
        return "|".join(_s(x) or "" for x in (k if isinstance(k, tuple) else (k,)))

    def cls_of(d, kind):
        if d is None or pd.isna(d):
            return "unknown"
        d = pd.Timestamp(d)
        if d > asof:
            return "future"
        if win_lo is not None and d < win_lo:
            return "late_history" if kind == "added" else "restated"
        return "window"

    def rec(frame, k, kind, field, before, after):
        r = frame.loc[k]
        d = r[spec["date"]] if spec["date"] else pd.NaT
        rows.append({"table": name, "key": keystr(k), "tradingItemId": int(r["tradingitemid"]) if "tradingitemid" in frame.columns else -1, "exDate": d, "field": field,
                     "before": before, "after": after, "kind": kind, "cls": cls_of(d, kind)})

    for k in added:
        rec(kn, k, "added", "row", None, "row")
    for k in removed:
        rec(kp, k, "removed", "row", "row", None)
    if len(common):
        p_c, n_c = kp.loc[common], kn.loc[common]
        for c in new.columns:
            if c in spec["key"]:
                continue
            ne = ~_eq(p_c[c], n_c[c])
            for k in common[ne]:
                rec(kp, k, "changed", c, _s(kp.at[k, c]), _s(kn.at[k, c]))
    ch = pd.DataFrame(rows, columns=["table", "key", "tradingItemId", "exDate", "field", "before", "after", "kind", "cls"])
    log.info("work %s refresh: prev %d -> new %d (added %d, removed %d, changed cells %d)", name, len(prev), len(new), len(added), len(removed),
             int((ch.kind == "changed").sum()) if len(ch) else 0)
    return new, ch


def co_acthist_candidates(ca: pd.DataFrame, current: pd.DataFrame | None) -> pd.DataFrame:
    """co_acthist 창 행 → 종목 집합(gvkey/iid = master current) 에 해당하는 후보(정보용)."""
    cols = ["table", "key", "tradingItemId", "exDate", "field", "before", "after", "kind", "cls"]
    if ca is None or not len(ca) or current is None or "gvkey" not in current.columns:
        return pd.DataFrame(columns=cols)
    m = {(str(g), str(i)): int(t) for t, g, i in zip(current.tradingItemId, current.gvkey, current.iid) if pd.notna(g)}
    rows = []
    for r in ca.itertuples(index=False):
        tid = m.get((str(r.gvkey), str(r.iid)))
        if tid is None:
            continue
        rows.append({"table": "co_acthist", "key": f"{r.gvkey}|{pd.Timestamp(r.datadate).date()}|{r.actioncd}", "tradingItemId": tid, "exDate": r.datadate, "field": "row",
                     "before": None, "after": f"{r.actioncd}: {str(r.actiontxt)[:200]}", "kind": "candidate", "cls": "window"})
    return pd.DataFrame(rows, columns=cols)


CO_ACTHIST_STALE_DAYS = 30


def source_status(ex: dict, asof, distributions: pd.DataFrame | None = None, dividends: pd.DataFrame | None = None) -> dict:
    """원천별 최신 적재일(D4 인계 (1)): 표마다 asOf 이전 최신 사건일·최대 사건일(미래 선언 포함)·행 수; co_acthist 는 DB max(datadate);
    distributions 는 정적 프로토 표. stale: co_acthist.latest < asOf − 30일 → warnings."""
    asof = _ts(asof)

    def tab(df, dcol, extra=None):
        if df is None or not len(df):
            return {"rows": 0, "latest": None, "max": None}
        d = pd.to_datetime(df[dcol])
        le = d[d <= asof]
        o = {"rows": int(len(df)), "latest": str(le.max().date()) if len(le) and pd.notna(le.max()) else None, "max": str(d.max().date()) if pd.notna(d.max()) else None}
        if extra:
            o.update(extra(df))
        return o

    div = ex["ciqdividend"] if ex is not None else dividends
    st = {"asOf": str(asof.date()), "ciqdividend": tab(div, "exDate", lambda d: {"latestAnnounced": str(pd.to_datetime(d.announcedDate).max().date()) if d.announcedDate.notna().any() else None})}
    if ex is not None:
        st["ciqsplit"] = tab(ex["ciqsplit"], "exdate", lambda d: {"latestAnnounced": str(pd.to_datetime(d.announceddate).max().date()) if d.announceddate.notna().any() else None})
        st["ciqsplitcache"] = tab(ex["ciqsplitcache"], "splitdate")
        st["divcache_noncash"] = tab(ex["divcache_noncash"], "dividenddate")
        st["fx"] = tab(ex["fx"], "pricedate")
        mx = (ex.get("_info") or {}).get("co_acthist_max_datadate")
        st["co_acthist"] = {"rows_in_window": int(len(ex["co_acthist_window"])), "latest": mx, "max": mx}
    if distributions is not None and len(distributions):
        st["distributions"] = tab(distributions, "exDate", lambda d: {"static": True, "note": "rc1 proto table (build_distributions.py 2026-09-14), not refreshed"})
    warnings = []
    ca = st.get("co_acthist", {}).get("latest")
    if ex is not None:
        if ca is None:
            warnings.append("co_acthist: latest datadate unknown")
        elif pd.Timestamp(ca) < asof - pd.Timedelta(days=CO_ACTHIST_STALE_DAYS):
            warnings.append(f"co_acthist stale: latest datadate {ca} < asOf {asof.date()} - {CO_ACTHIST_STALE_DAYS}d (ended ids after {ca} get unresolved marker rows; spin-off/merger terms from co_acthist are missing)")
    # R-D1 P1-2 / P2-5: no refresh path for the research tables — say so on every run (not a stale condition, a design limit)
    warnings.append(STATIC_SOURCES_WARNING)
    st["stale"] = {"co_acthist": bool(ca is not None and pd.Timestamp(ca) < asof - pd.Timedelta(days=CO_ACTHIST_STALE_DAYS))}
    st["warnings"] = warnings
    st["static_sources"] = {"distributions": "rc1 proto table (build_distributions.py 2026-09-14): not refreshed; new spin-off/merger terms enter only via ciqdividend type 20/31 marker rows (unresolved/estimated) or ledger override CSVs",
                            "co_acthist": "window rows are recorded as candidates (source_changes kind=candidate) only; they do not create ledger legs. If the feed resumes, the ended-without-terms markers for ids ended before its new latest datadate disappear while their terms still do not enter — regenerate distributions (build_distributions.py) or add override CSVs first",
                            "aux_dividends": "R3 hand-over rows (for_R1_events.csv, source ciqdividend(aux), 77 aux ids) are carried unchanged; aux ids are outside instruments and not re-queried"}
    return st


STATIC_SOURCES_WARNING = ("static sources: distributions (proto 2026-09-14), co_acthist (candidates only) and R3 aux dividends are not refreshed — new spin-off/merger terms "
                          "enter only via ciqdividend 20/31 markers or ledger override CSVs; when co_acthist resumes, ended-without-terms markers before its new latest datadate "
                          "disappear without their terms entering (see sidecar incremental.sourceStatus.static_sources)")


# --------------------------------------------------------------------------------------
# previous build resolution
# --------------------------------------------------------------------------------------
class Prev:
    """--prev 해석: D1 스테이지(ledger_incremental_state.json) / rc2f R1 스테이지(root/, work/, sidecar inputs) / 발행본(base_data.parquet)."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.ledger = self.path / "events" / "ca_ledger.parquet"
        if not self.ledger.exists():
            raise SystemExit(f"--prev {self.path}: events/ca_ledger.parquet not found")
        self.sidecar_path = self.ledger.with_name("ca_ledger_build_info.json")
        self.sidecar = json.loads(self.sidecar_path.read_text()) if self.sidecar_path.exists() else {}
        inputs = self.sidecar.get("inputs", {}) or {}
        state_p = self.path / STAGE_STATE
        self.state = json.loads(state_p.read_text()) if state_p.exists() else {}
        if self.state:
            self.layout = "d1_stage"
        elif (self.path / "root").exists():
            self.layout = "rc2f_stage"
        elif (self.path / "base_data.parquet").exists():
            self.layout = "published"
        else:
            raise SystemExit(f"--prev {self.path}: not a D1 stage, an rc2f R1 stage (root/) or a published build (base_data.parquet)")
        asof = self.state.get("asOf") or self.sidecar.get("asOf")
        if not asof:
            raise SystemExit(f"--prev {self.path}: previous asOf unknown (sidecar has no asOf key)")
        self.asof = _ts(asof)
        root = self.path / "root" if self.layout in ("d1_stage", "rc2f_stage") else self.path
        self.root = root
        self.dividends = root / "dividends" / "dividends_long.parquet"
        self.distributions_dir = root / "distributions"
        self.qa_dir = root / "qa"
        self.files = {
            "calendar": root / "calendar.parquet", "split": root / "factors" / "split.parquet", "dividend_factor": root / "factors" / "dividend.parquet",
            "events": root / "events" / "corporate_actions.parquet", "instruments": root / "master" / "instruments.parquet",
            "current": root / "master" / "current.parquet", "membership": root / "universe" / "membership.parquet",
            "lineage": Path(inputs["lineage"]) if inputs.get("lineage") and Path(inputs["lineage"]).exists() else root / "master" / "lineage.parquet",
            "lifecycle": Path(inputs["lifecycle"]) if inputs.get("lifecycle") and Path(inputs["lifecycle"]).exists() else root / "master" / "lifecycle.parquet",
        }
        bd = inputs.get("base_data")
        self.base_data = Path(bd) if bd and Path(bd).exists() else (self.path / "base_data.parquet" if self.layout == "published" else None)
        aux = inputs.get("aux_base_data")
        self.aux_base_data = Path(aux) if aux and aux != "None" and Path(aux).exists() else None
        self.work = {n: (self.path / "work" / f"work_{n}.parquet") for n in WORK_TABLES}
        self.work = {n: (p if p.exists() else None) for n, p in self.work.items()}
        gap = self.path / "work" / "gap_candidates_union.csv"
        self.gap_union = gap if gap.exists() else (Path(inputs["gap_candidates"]) if inputs.get("gap_candidates") and Path(inputs["gap_candidates"]).exists() else None)
        ho = self.path / "handover"
        r3 = inputs.get("r3_events")
        self.r3_events = ho / "for_R1_events.csv" if (ho / "for_R1_events.csv").exists() else (Path(r3) if r3 and Path(r3).exists() else None)
        self.split_moves = next((p for p in (ho / "split_boundary_moves.csv", self.path / "split_boundary_moves.csv") if p.exists()), None)
        self.estimated_prices = next((p for p in (ho / "price_override_estimates.csv", self.path / "price_override_estimates.csv") if p.exists()), None)
        ref = self.state.get("overrides_ref_ledger")
        self.overrides_ref_ledger = ho / "overrides_ref_ledger.parquet" if (ho / "overrides_ref_ledger.parquet").exists() else (Path(ref) if ref and Path(ref).exists() else None)
        self.rc2_ref = Path(self.state["rc2_ref"]) if self.state.get("rc2_ref") and Path(self.state["rc2_ref"]).exists() else None
        self.aux_ids_file = self.path / "handover" / "aux_ids.json"
        # source files (before staging) and their sha as recorded by the previous run — used for the inputs-changed attribution (R-D1 P2-4)
        self.source_files = {k: Path(v) for k, v in (self.state.get("files") or {}).items() if v and v != "None"}
        self.source_sha = dict(self.state.get("files_sha256") or {})


# --------------------------------------------------------------------------------------
# stage assembly
# --------------------------------------------------------------------------------------
def _resolve_build_inputs(build: Path | None) -> dict:
    """--build: update_daily out-stage(T1/T2/T5/T6) 또는 발행본 → 입력 파일 경로(있는 것만)."""
    if build is None:
        return {}
    b = Path(build)
    if (b / "T2" / "base_data.parquet").exists():
        cand = {"calendar": b / "calendar.parquet", "split": b / "T2" / "factors" / "split.parquet", "dividend_factor": b / "T2" / "factors" / "dividend.parquet",
                "events": b / "T6" / "events" / "corporate_actions.parquet", "instruments": b / "T1" / "master" / "instruments.parquet",
                "current": b / "T1" / "master" / "current.parquet", "membership": b / "T5" / "universe" / "membership.parquet",
                "lineage": b / "T1" / "master" / "lineage.parquet", "lifecycle": b / "T1" / "master" / "lifecycle.parquet",
                "base_data": b / "T2" / "base_data.parquet", "aux_base_data": b / "R3" / "base_data_aux.parquet", "qa_dir": b / "T2" / "qa"}
    elif (b / "base_data.parquet").exists():
        cand = {"calendar": b / "calendar.parquet", "split": b / "factors" / "split.parquet", "dividend_factor": b / "factors" / "dividend.parquet",
                "events": b / "events" / "corporate_actions.parquet", "instruments": b / "master" / "instruments.parquet",
                "current": b / "master" / "current.parquet", "membership": b / "universe" / "membership.parquet",
                "lineage": b / "master" / "lineage.parquet", "lifecycle": b / "master" / "lifecycle.parquet",
                "base_data": b / "base_data.parquet", "aux_base_data": b / "base_data_aux.parquet", "qa_dir": b / "qa"}
    else:
        raise SystemExit(f"--build {b}: neither T2/base_data.parquet nor base_data.parquet found")
    return {k: v for k, v in cand.items() if v.exists()}


def assemble_root(root: Path, files: dict, dividends_df: pd.DataFrame, distributions_dir: Path, qa_files: dict | None, instrument_ids: set | None,
                  calendar_cut=None) -> dict:
    """<out>/root 조립. files: ROOT_INPUTS → 경로. factors 표는 종목 집합(instruments) 으로 필터(발행본은 aux 계수가 합쳐져 있음: 원장 split 행은 종목 집합만)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    info = {}
    # calendar (materialised when cut, else link/copy)
    if calendar_cut is not None:
        c = pd.read_parquet(files["calendar"])
        c = c[c.date <= _ts(calendar_cut)].reset_index(drop=True)
        _write_df(c, root / "calendar.parquet")
        info["calendar"] = "cut"
    else:
        info["calendar"] = _place(files["calendar"], root / "calendar.parquet")
    for name, rel in (("split", "factors/split.parquet"), ("dividend_factor", "factors/dividend.parquet")):
        src = files[name]
        t = pd.read_parquet(src)
        if instrument_ids is not None and not set(t.tradingItemId.astype(int)) <= instrument_ids:
            t = t[t.tradingItemId.isin(list(instrument_ids))].reset_index(drop=True)
            _write_df(t, root / rel)
            info[name] = "filtered_to_instruments"
        else:
            info[name] = _place(src, root / rel)
    info["events"] = _place(files["events"], root / "events" / "corporate_actions.parquet")
    for name in ("instruments", "current", "lineage", "lifecycle"):
        info[name] = _place(files[name], root / "master" / f"{name}.parquet")
    lca = Path(files["lifecycle"]).with_name("lifecycle_aux.parquet")
    if lca.exists():
        _place(lca, root / "master" / "lifecycle_aux.parquet")
    info["membership"] = _place(files["membership"], root / "universe" / "membership.parquet")
    _write_df(dividends_df, root / "dividends" / "dividends_long.parquet", DIV_SCHEMA)
    if not Path(distributions_dir).exists():
        raise SystemExit(f"distributions table not found: {distributions_dir} (static proto table; carried from --prev)")
    info["distributions"] = _place(distributions_dir, root / "distributions")
    # qa lists (55 dividend-factor-scale ids, 97 G-JUMP artefacts): the repair checks silently skip when they are missing -> required (R-D1 P1-1)
    missing = [rel for rel in QA_FILES if rel not in (qa_files or {})]
    if missing:
        raise SystemExit(f"qa lists missing {missing}: give --qa-dir (or --build with T2/qa|qa/T2, qa/T7) — the ledger would silently differ without them")
    for rel, src in qa_files.items():
        _copy(Path(src).resolve(), root / "qa" / rel)          # small csv, always a real file (no link chains into a scratchpad)
        info[f"qa/{rel}"] = "copy"
    return info


QA_FILES = ("T2/factor_artifacts.csv", "T7/J_gdiv_systematic_scale_ids.csv")     # ledger_repairs reads them from <root>/qa (known G-JUMP artefacts, 55 div-factor-scale ids)


def resolve_qa_files(qa_dir: Path | None, build: Path | None, prev_qa: Path | None) -> dict:
    """root/qa/<rel> 후보: --qa-dir/<rel>, 새 빌드(update_daily stage: T2/qa/factor_artifacts.csv; 발행본: qa/<rel>), 이전 root/qa/<rel>. 먼저 있는 것."""
    out = {}
    for rel in QA_FILES:
        name = Path(rel).name
        cands = []
        if qa_dir is not None:
            cands += [Path(qa_dir) / rel, Path(qa_dir) / name]          # explicit dir: no fallback (a missing list there is an error, R-D1 P1-1)
        else:
            if build is not None:
                b = Path(build)
                cands += [b / "T2" / "qa" / name if rel.startswith("T2/") else b / "T2" / "qa" / rel, b / "qa" / rel]
            if prev_qa is not None:
                cands.append(Path(prev_qa) / rel)                        # carried from the previous stage (a real file there since D1 copies the lists)
        for c in cands:
            if c.exists():
                out[rel] = c.resolve()
                break
    return out


# --------------------------------------------------------------------------------------
# ledger diff -> revisions
# --------------------------------------------------------------------------------------
def _ledger_keys(L: pd.DataFrame) -> pd.Series:
    child = L.childTradingItemId.astype("Int64").astype(object).where(L.childTradingItemId.notna(), "")
    k = L.tradingItemId.astype(str) + "|" + pd.to_datetime(L.exDate).dt.strftime("%Y-%m-%d") + "|" + L.eventType.astype(str) + "|" + L.legType.astype(str) + "|" + child.astype(str)
    dup = k.duplicated(keep=False)
    if dup.any():
        seq = k.groupby(k).cumcount()
        k = k.where(~dup, k + "#" + seq.astype(str))
    return k


def diff_ledgers(prev: pd.DataFrame, new: pd.DataFrame, source_changes: pd.DataFrame | None, inputs_changed: list, asof, prev_asof) -> tuple[pd.DataFrame, dict]:
    """이전/새 원장 대조 → revisions 행(kind added/removed/changed, field, before/after, source 귀속)."""
    prev = prev.copy(); new = new.copy()
    prev["_k"] = _ledger_keys(prev).to_numpy()
    new["_k"] = _ledger_keys(new).to_numpy()
    kp, kn = prev.set_index("_k"), new.set_index("_k")
    added = kn.index.difference(kp.index)
    removed = kp.index.difference(kn.index)
    common = kp.index.intersection(kn.index)
    # source attribution helpers
    div_keys, split_ids, cache_keys, ids_touched, fx_keys = set(), set(), set(), set(), set()
    if source_changes is not None and len(source_changes):
        sc = source_changes
        div_keys = set(sc[(sc.table == "ciqdividend") & (sc.kind != "derived")].key.astype(str))
        fx_keys = set(sc[(sc.table == "ciqdividend") & (sc.kind == "derived")].key.astype(str))
        split_ids = set(sc[sc.table == "ciqsplit"].key.astype(str))
        cache_keys = {(int(t), pd.Timestamp(d)) for t, d in zip(sc[sc.table == "ciqsplitcache"].tradingItemId, sc[sc.table == "ciqsplitcache"].exDate) if pd.notna(d)}
        ids_touched = set(int(t) for t in sc.tradingItemId if t is not None and t >= 0)
    keeper_changed: set = set()

    def attribute(row, prev_row=None, removed: bool = False) -> str:
        ev = str(row.evidence) if pd.notna(row.evidence) else ""
        if prev_row is not None and pd.notna(prev_row.evidence):
            ev += " " + str(prev_row.evidence)                       # a changed row: source keys of the previous version count too (a removed dividendId)
        src = str(row.source)
        if src == "stitched_copy":
            return f"stitched_copy_of:{row.keeperEventId}"
        if lb.ENDED_MARKER_TAG in str(row.notes):
            return "rule:ended_without_terms_marker"
        import re
        tag_new = "csv_override:" in str(row.repairOf)
        tag_prev = prev_row is not None and "csv_override:" in str(prev_row.repairOf)
        if removed:                                                   # R-D1 P2-2: a removed row is the previous version — its CSV tag means the override was superseded/dropped
            if tag_new:
                return "override:csv_superseded_or_dropped"
        elif tag_new and not tag_prev:
            return "override:csv_applied"                             # the row entered the CSV overrides' scope in this run
        elif tag_prev and not tag_new:
            return "override:csv_superseded_or_dropped"               # rc2f_apply rule: a vendor row now carries the event -> the override is a no-op
        if prev_row is not None:
            a, b = set(re.findall(r"splitId=(\d+)", str(prev_row.evidence))), set(re.findall(r"splitId=(\d+)", str(row.evidence)))
            if a and b and a != b and str(row.legType) == "quantity":
                return "rule:ciqsplit_tie_break_smallest_splitid"     # paired ciqsplit rows on one ex-date: deterministic choice (D1) vs the rc2 work-table order
        ids_in_ev = {x for m in re.findall(r"dividendId=([0-9,]+)", ev) for x in m.split(",")}
        if ids_in_ev & div_keys:
            return "ciqdividend"
        if ids_in_ev & fx_keys:
            return "ciqdividend:fx"                                   # R-D1 P1-4: exchange-rate recompute (derived), vendor row unchanged
        if set(re.findall(r"splitId=(\d+)", ev)) & split_ids:
            return "ciqsplit"
        if str(row.legType) == "quantity" and (int(row.tradingItemId), pd.Timestamp(row.exDate)) in cache_keys:
            return "ciqsplitcache"
        if int(row.tradingItemId) in ids_touched:
            return "same_id_source_change"
        if inputs_changed:
            return "inputs:" + ",".join(inputs_changed)
        return "unattributed"

    rows = []
    obs = _now()
    cols = [c for c in lb.LEDGER_COLUMNS if c != "eventId"]
    for k in added:
        r = kn.loc[k]
        rows.append({"kind": "added", "eventId": r.eventId, "eventIdPrev": None, "tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "eventType": r.eventType,
                     "legType": r.legType, "childTradingItemId": None if pd.isna(r.childTradingItemId) else int(r.childTradingItemId), "field": "row", "before": None,
                     "after": f"cash={_s(r.cashPerShare)} pf={_s(r.parentFactor)} childRatio={_s(r.childRatio)} value={_s(r.childValuePerOldShare)} status={r.status}",
                     "source": attribute(r), "status": r.status})
    for k in removed:
        r = kp.loc[k]
        rows.append({"kind": "removed", "eventId": None, "eventIdPrev": r.eventId, "tradingItemId": int(r.tradingItemId), "exDate": r.exDate, "eventType": r.eventType,
                     "legType": r.legType, "childTradingItemId": None if pd.isna(r.childTradingItemId) else int(r.childTradingItemId), "field": "row",
                     "before": f"cash={_s(r.cashPerShare)} pf={_s(r.parentFactor)} childRatio={_s(r.childRatio)} value={_s(r.childValuePerOldShare)} status={r.status}",
                     "after": None, "source": attribute(r, removed=True), "status": r.status})
    n_changed_rows = 0
    if len(common):
        p_c, n_c = kp.loc[common], kn.loc[common]
        any_ne = np.zeros(len(common), dtype=bool)
        per_col = {}
        for c in cols:
            ne = ~_eq(p_c[c], n_c[c])
            if ne.any():
                per_col[c] = ne
                any_ne |= ne
        eid_ne = ~_eq(p_c["eventId"], n_c["eventId"])
        n_changed_rows = int(any_ne.sum())
        for c, ne in per_col.items():
            for k in common[ne]:
                r = kn.loc[k]
                rows.append({"kind": "changed", "eventId": r.eventId, "eventIdPrev": kp.at[k, "eventId"], "tradingItemId": int(r.tradingItemId), "exDate": r.exDate,
                             "eventType": r.eventType, "legType": r.legType, "childTradingItemId": None if pd.isna(r.childTradingItemId) else int(r.childTradingItemId),
                             "field": c, "before": _s(kp.at[k, c]), "after": _s(r[c]), "source": attribute(r, kp.loc[k]), "status": r.status})
        for k in common[eid_ne & ~any_ne]:
            r = kn.loc[k]
            rows.append({"kind": "rekeyed", "eventId": r.eventId, "eventIdPrev": kp.at[k, "eventId"], "tradingItemId": int(r.tradingItemId), "exDate": r.exDate,
                         "eventType": r.eventType, "legType": r.legType, "childTradingItemId": None if pd.isna(r.childTradingItemId) else int(r.childTradingItemId),
                         "field": "eventId", "before": kp.at[k, "eventId"], "after": r.eventId, "source": "eventId_sequence", "status": r.status})
    rev = pd.DataFrame(rows, columns=["kind", "eventId", "eventIdPrev", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "field", "before", "after", "source", "status"])
    rev.insert(0, "asOf", _ts(asof))
    rev.insert(1, "prevAsOf", _ts(prev_asof) if prev_asof is not None else pd.NaT)
    rev.insert(2, "observedAt", pd.Timestamp(obs))
    rev = rev.sort_values(["kind", "tradingItemId", "exDate", "eventType", "legType", "field"], kind="stable").reset_index(drop=True)
    summary = {"added": int(len(added)), "removed": int(len(removed)), "changed_rows": n_changed_rows, "changed_cells": int((rev.kind == "changed").sum()),
               "rekeyed": int((rev.kind == "rekeyed").sum()),
               "by_eventType": {k: {kk: int(vv) for kk, vv in v.items()} for k, v in rev[rev.kind != "rekeyed"].groupby("eventType").kind.value_counts().unstack(fill_value=0).to_dict("index").items()} if len(rev) else {},
               "by_source": rev[rev.kind != "rekeyed"].source.str.split(":").str[0].value_counts().to_dict() if len(rev) else {},
               "unattributed": int((rev.source == "unattributed").sum()) if len(rev) else 0,
               "by_status_added": rev[rev.kind == "added"].status.value_counts().to_dict() if len(rev) else {}}
    return rev, summary


REV_SCHEMA = pa.schema([
    ("asOf", pa.timestamp("ns")), ("prevAsOf", pa.timestamp("ns")), ("observedAt", pa.timestamp("ns")), ("kind", pa.string()), ("eventId", pa.string()),
    ("eventIdPrev", pa.string()), ("tradingItemId", pa.int64()), ("exDate", pa.timestamp("ns")), ("eventType", pa.string()), ("legType", pa.string()),
    ("childTradingItemId", pa.int64()), ("field", pa.string()), ("before", pa.string()), ("after", pa.string()), ("source", pa.string()), ("status", pa.string()),
])
SRC_SCHEMA = pa.schema([("table", pa.string()), ("key", pa.string()), ("tradingItemId", pa.int64()), ("exDate", pa.timestamp("ns")), ("field", pa.string()),
                        ("before", pa.string()), ("after", pa.string()), ("kind", pa.string()), ("cls", pa.string())])


def write_revisions(out_events: Path, asof, rev: pd.DataFrame, src: pd.DataFrame, summary: dict) -> Path:
    d = Path(out_events) / REVISIONS_DIR / f"date={_ts(asof):%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    r = rev.copy()
    r["childTradingItemId"] = r.childTradingItemId.astype("Int64")
    r["exDate"] = pd.to_datetime(r.exDate).astype("datetime64[ns]")
    for c in ("eventId", "eventIdPrev", "before", "after"):
        r[c] = r[c].astype(object).where(r[c].notna(), None)
    _write_df(r, d / "revisions.parquet", REV_SCHEMA)
    s = src.copy() if src is not None else pd.DataFrame(columns=SRC_SCHEMA.names)
    if len(s):
        s["tradingItemId"] = s.tradingItemId.astype("int64")
        s["exDate"] = pd.to_datetime(s.exDate).astype("datetime64[ns]")
        for c in ("before", "after"):
            s[c] = s[c].astype(object).where(s[c].notna(), None)
    _write_df(s[SRC_SCHEMA.names], d / "source_changes.parquet", SRC_SCHEMA)
    (d / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return d


# --------------------------------------------------------------------------------------
# the incremental run
# --------------------------------------------------------------------------------------
def _ledger_csvs(pattern: str) -> list[Path]:
    from rc2f_apply import kind_of
    paths = sorted(Path(p) for p in glob.glob(pattern, recursive=True))
    return [p for p in paths if kind_of(p) == "ledger"]


def _stage_override_copies(csvs: list[Path], out: Path) -> list[Path]:
    d = out / "overrides"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.csv"):
        old.unlink()
    outs = []
    for p in csvs:
        dst = d / (p.parent.name + "__" + p.name) if p.parent.name not in ("overrides",) else d / (p.parent.parent.name + "__" + p.name)
        shutil.copy2(p, dst)
        outs.append(dst)
    return sorted(outs)


def resolve_rc2_ref(explicit: Path | None, from_state: Path | None) -> Path | None:
    """rc2 참조(교정 검증기·eventId 해석): 명시 > 이전 상태 > 내구 slim 참조(DEFAULT_RC2_REF) > 스크래치 build_rc2_final. 없으면 None(검증 시 abort)."""
    for c in (explicit, from_state, DEFAULT_RC2_REF, SCRATCH_RC2_REF):
        if c is not None and Path(c).exists() and (Path(c) / "events" / "ca_ledger.parquet").exists():
            return Path(c).resolve()
    return None


def rc2_ref_sha(rc2_ref: Path | None) -> dict | None:
    if rc2_ref is None or not Path(rc2_ref).exists():
        return None
    return {rel: sha256(Path(rc2_ref) / rel) for rel in RC2_REF_FILES if (Path(rc2_ref) / rel).exists()}


def make_ref(rc2: Path, out: Path) -> dict:
    """rc2 참조 빌드(build_rc2_final, 스크래치) → 내구 slim 참조 디렉토리: 검증기가 읽는 원장·lifecycle·calendar + base_data id 목록(base_data_ids.parquet)."""
    rc2, out = Path(rc2), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for rel in ("events/ca_ledger.parquet", "master/lifecycle.parquet", "calendar.parquet", "manifest.json"):
        if (rc2 / rel).exists():
            _copy(rc2 / rel, out / rel)
    ids = pq.read_table(rc2 / "base_data.parquet", columns=["tradingItemId"]).column(0).unique().to_pylist()
    _write_df(pd.DataFrame({"tradingItemId": pd.Series(sorted(int(i) for i in ids), dtype="int64")}), out / "base_data_ids.parquet")
    info = {"from": str(rc2), "out": str(out), "at": _now(), "files": rc2_ref_sha(out), "base_data_ids": len(ids)}
    (out / "REF_INFO.json").write_text(json.dumps(info, indent=1))
    return info


def validate_overrides(csvs: list[Path], rc2_ref: Path | None) -> dict:
    """rc2f_apply 검증기 재사용(ledger kind). rc2 참조 빌드가 없으면 SystemExit(교정 검증 없이 적용하지 않음)."""
    if not csvs:
        return {"files": 0, "rows": 0, "errors": 0, "warnings": 0}
    if rc2_ref is None or not Path(rc2_ref).exists():
        raise SystemExit(f"ledger override CSVs given but no rc2 reference for the validator: pass --rc2-ref, or create the durable slim reference with "
                         f"`ledger_incremental.py make-ref --rc2 <build_rc2_final> --out {DEFAULT_RC2_REF}`; --skip-validate bypasses (not recommended)")
    import rc2f_apply as ra
    ref = ra.Ref(Path(rc2_ref))
    summary, iss = ra.validate_files([str(p) for p in csvs], ref)
    if iss.n_err:
        for r in iss.rows:
            if r["level"] == "ERROR":
                log.error("override %s row %s %s: %s", r["file"], r["row"], r["overrideId"], r["message"])
        raise SystemExit(f"ledger override validation: {iss.n_err} error(s)")
    return {"files": len(csvs), "rows": int(sum(s.get("rows", 0) for s in summary)), "errors": int(iss.n_err), "warnings": int(iss.n_warn)}


def _opt(a, name: str):
    """명시적으로 준 옵션 값만(ledger_build.py 의 기본값이 있는 옵션은 --incremental 에서 무시)."""
    given = getattr(a, "_given", None)
    if given is not None and f"--{name.replace('_', '-')}" not in given:
        return None
    return getattr(a, name, None)


def run_incremental(a) -> dict:
    t0 = time.time()
    pa.set_cpu_count(8)                     # shared-machine rule (R-D1 P2-3); ledger_build.main sets the same when run as a CLI
    pa.set_io_thread_count(8)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(exist_ok=True)
    fh = logging.FileHandler(out / "logs" / "ledger_incremental.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    prev = Prev(a.prev)
    asof = _ts(a.asof)
    if asof < prev.asof and not getattr(a, "allow_asof_before_prev", False):
        raise SystemExit(f"--asof {asof.date()} is before the previous asOf {prev.asof.date()} (gate bootstrap: --allow-asof-before-prev)")
    cut = _ts(a.source_cut) if getattr(a, "source_cut", None) else None
    if cut is not None and cut != asof:
        raise SystemExit("--source-cut must equal --asof (the cut simulates 'sources known through asOf')")
    build_in = _resolve_build_inputs(getattr(a, "build", None))
    files = {}
    for name in ROOT_INPUTS:
        arg = {"calendar": "calendar", "split": "split_factors", "dividend_factor": "dividend_factors", "events": "events", "instruments": "instruments",
               "current": "current", "membership": "membership", "lineage": "lineage", "lifecycle": "lifecycle"}[name]
        v = _opt(a, arg) if arg in ("lineage", "lifecycle") else getattr(a, arg, None)
        p = Path(v) if v else (build_in.get(name) or prev.files.get(name))
        if p is None or not Path(p).exists():
            raise SystemExit(f"input {name} not found (--{arg.replace('_', '-')}, --build or --prev)")
        files[name] = Path(p).resolve()
    base_data = Path(_opt(a, "base_data")) if _opt(a, "base_data") else (build_in.get("base_data") or prev.base_data)
    if base_data is None or not Path(base_data).exists():
        raise SystemExit("base_data not found (--base-data, --build or --prev sidecar inputs)")
    aux_base = Path(_opt(a, "aux_base_data")) if _opt(a, "aux_base_data") else (build_in.get("aux_base_data") or prev.aux_base_data)
    if aux_base is not None and not Path(aux_base).exists():
        aux_base = None
    qa_files = resolve_qa_files(Path(a.qa_dir) if getattr(a, "qa_dir", None) else None, getattr(a, "build", None), prev.qa_dir)
    r3_events = Path(_opt(a, "r3_events")) if _opt(a, "r3_events") else prev.r3_events
    split_moves = Path(_opt(a, "split_moves")) if _opt(a, "split_moves") else prev.split_moves
    est_prices = Path(_opt(a, "estimated_prices")) if _opt(a, "estimated_prices") else prev.estimated_prices
    ref_ledger = Path(_opt(a, "overrides_ref_ledger")) if _opt(a, "overrides_ref_ledger") else (prev.overrides_ref_ledger or (DEFAULT_RC2_REF / "events" / "ca_ledger.parquet"))
    rc2_ref = resolve_rc2_ref(Path(a.rc2_ref) if getattr(a, "rc2_ref", None) else None, prev.rc2_ref)
    gap_arg = Path(_opt(a, "gap_candidates")) if _opt(a, "gap_candidates") else None
    log.info("incremental: prev %s (%s, asOf %s) -> asOf %s, out %s", prev.path, prev.layout, prev.asof.date(), asof.date(), out)

    # calendar guard: asOf must be a trading day of the (possibly cut) calendar and <= its last day
    cal_df = pd.read_parquet(files["calendar"])
    tdays = pd.to_datetime(cal_df.loc[cal_df.isTradingDay, "date"])
    if cut is not None:
        tdays = tdays[tdays <= cut]
    if not len(tdays) or asof > tdays.max():
        raise SystemExit(f"--asof {asof.date()} is after the calendar's last trading day {tdays.max().date() if len(tdays) else 'n/a'} ({files['calendar']}): the calendar (U2) must be advanced first")
    if asof not in set(tdays):
        raise SystemExit(f"--asof {asof.date()} is not a trading day in {files['calendar']}")

    # ids
    inst = pd.read_parquet(files["instruments"])
    ids = sorted(set(int(i) for i in inst.tradingItemId))
    cur = pd.read_parquet(files["current"])
    aux_ids = set()
    if aux_base is not None:
        aux_ids = set(int(i) for i in pq.read_table(aux_base, columns=["tradingItemId"]).column(0).unique().to_pylist())
    elif prev.aux_ids_file.exists():
        aux_ids = set(json.loads(prev.aux_ids_file.read_text()))
    aux_inst = Path(files["instruments"]).with_name("aux_instruments.parquet")
    if not aux_ids and aux_inst.exists():
        aux_ids = set(int(i) for i in pd.read_parquet(aux_inst, columns=["tradingItemId"]).tradingItemId)

    # ---- sources: extract (db) or load (--source-extract) or carry (--no-db without extract) ----
    prev_div = pd.read_parquet(prev.dividends)
    prev_work = {n: (pd.read_parquet(p) if p is not None else None) for n, p in prev.work.items()}
    src_info = {"mode": None}
    ex = None
    if getattr(a, "source_extract", None):
        ex = load_extract(Path(a.source_extract))
        src_info = {"mode": "extract_dir", "dir": str(a.source_extract)}
    elif not getattr(a, "no_db", False):
        ex_dir = out / "work" / "extract"
        src_info = {"mode": "xfeed", **extract_sources(ids, ex_dir, prev.asof, asof)}
        ex = load_extract(ex_dir)
    else:
        src_info = {"mode": "carried(no-db)"}
        missing = [n for n, w in prev_work.items() if w is None]
        if missing:
            raise SystemExit(f"--no-db without --source-extract: previous work tables missing {missing}")
    if ex is not None and cut is not None:
        ex = cut_extract(ex, cut)
        src_info["cut"] = str(cut.date())
    changes = []
    split_tbl = pd.read_parquet(files["split"])
    if ex is not None:
        refs = Refs(inst, cur, ex["fx"], split_tbl)
        div_new, ch = refresh_dividends(prev_div, ex["ciqdividend"], refs, asof, prev.asof)
        changes.append(ch)
        work_new = {}
        for n in WORK_TABLES:
            w, ch = refresh_work(n, prev_work[n], ex[n], asof, prev.asof)
            work_new[n] = w
            changes.append(ch)
        changes.append(co_acthist_candidates(ex["co_acthist_window"], cur))
        (out / "work").mkdir(parents=True, exist_ok=True)
        _write_df(ex["co_acthist_window"], out / "work" / "co_acthist_window.parquet")
    else:
        div_new = _finish_div(prev_div, asof)
        work_new = {n: normalize_work(n, prev_work[n]) for n in WORK_TABLES}
    src_changes = pd.concat([c for c in changes if len(c)], ignore_index=True) if any(len(c) for c in changes) else pd.DataFrame(columns=SRC_SCHEMA.names)
    dist_df = pd.read_parquet(prev.distributions_dir / "distributions.parquet", columns=["exDate"]) if (prev.distributions_dir / "distributions.parquet").exists() else None
    if ex is not None:
        status = source_status(ex, asof, dist_df)
    else:
        prev_status = json.loads((prev.path / "work" / "source_status.json").read_text()) if (prev.path / "work" / "source_status.json").exists() else {}
        status = source_status(None, asof, dist_df, div_new)
        status["co_acthist"] = prev_status.get("co_acthist", {"latest": None, "max": None})
        status["carried_from"] = str(prev.path / "work" / "source_status.json")
        ca = status["co_acthist"].get("latest")
        status["stale"] = {"co_acthist": bool(ca and pd.Timestamp(ca) < asof - pd.Timedelta(days=CO_ACTHIST_STALE_DAYS))}
        status["warnings"] = ([f"co_acthist stale: latest datadate {ca} < asOf {asof.date()} - {CO_ACTHIST_STALE_DAYS}d (carried status)"] if status["stale"]["co_acthist"] else []) + [STATIC_SOURCES_WARNING]
    for w in status.get("warnings", []):
        log.warning("source status: %s", w)

    # ---- stage: root/work/handover/overrides ----
    root_info = assemble_root(out / "root", files, div_new, prev.distributions_dir, qa_files, set(ids), calendar_cut=cut)
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    for n, w in work_new.items():
        _write_df(w, work / f"work_{n}.parquet")
    if prev.gap_union is not None and Path(prev.gap_union).exists():
        _copy(prev.gap_union, work / "gap_candidates_union.csv")
    for stale in (work / "anchor_closes.parquet",):
        if stale.exists():
            stale.unlink()                     # closes are re-read from the (new) base_data every run
    (work / "source_status.json").write_text(json.dumps(status, indent=1, default=str))
    ho = out / "handover"
    ho.mkdir(exist_ok=True)
    for src, name in ((r3_events, "for_R1_events.csv"), (split_moves, "split_boundary_moves.csv"), (est_prices, "price_override_estimates.csv"),
                      (ref_ledger, "overrides_ref_ledger.parquet")):
        if src is not None and Path(src).exists() and Path(src).resolve() != (ho / name).resolve():
            _copy(Path(src), ho / name)
    (ho / "aux_ids.json").write_text(json.dumps(sorted(aux_ids)))
    r3_events = ho / "for_R1_events.csv" if (ho / "for_R1_events.csv").exists() else None
    split_moves = ho / "split_boundary_moves.csv" if (ho / "split_boundary_moves.csv").exists() else None
    est_prices = ho / "price_override_estimates.csv" if (ho / "price_override_estimates.csv").exists() else None
    ref_ledger = ho / "overrides_ref_ledger.parquet" if (ho / "overrides_ref_ledger.parquet").exists() else None
    csvs_src = _ledger_csvs(a.overrides_glob) if a.overrides_glob else []
    csvs = _stage_override_copies(csvs_src, out)
    ovr_val = validate_overrides(csvs_src, rc2_ref) if not getattr(a, "skip_validate", False) else {"skipped": True, "files": len(csvs)}   # validator keys the kind on the original file names
    ovr_val["staged"] = [str(p) for p in csvs]

    # ---- inputs changed vs the previous run (attribution): compare the SOURCE files (state files_sha256), not the staged/filtered copies (R-D1 P2-4) ----
    inputs_changed = []
    files_sha = {name: sha256(files[name]) for name in ROOT_INPUTS}
    for name in ROOT_INPUTS:
        prev_sha = prev.source_sha.get(name)
        if prev_sha is None:
            pp = prev.files.get(name)
            prev_sha = sha256(pp) if pp is not None and Path(pp).exists() else None
        if prev_sha != files_sha[name]:
            inputs_changed.append(name)
    for name, pp, np_ in (("base_data", prev.base_data, base_data), ("aux_base_data", prev.aux_base_data, aux_base)):
        if (pp is None) != (np_ is None) or (pp is not None and Path(pp).resolve() != Path(np_).resolve() and sha256(pp) != sha256(np_)):
            inputs_changed.append(name)
    if cut is not None and "calendar" not in inputs_changed:
        inputs_changed.append("calendar")
    if getattr(a, "closes_max_date", None) or cut is not None:
        inputs_changed.append("closes_cut")

    # ---- build (same code path as the full build) ----
    if cut is not None:
        lb.CLOSES_MAX_DATE = cut
    t_build = time.time()
    binfo = lb.build(out / "root", out, Path(base_data), out / "events" / "ca_ledger.parquet", pull=False, lineage_path=files["lineage"], lifecycle_path=files["lifecycle"],
                     aux_base_data=aux_base, r3_events=r3_events, gap_candidates=gap_arg, overrides=csvs if csvs else None, overrides_ref_ledger=ref_ledger,
                     rc2_rules=bool(getattr(a, "rc2_rules", False)), split_moves=split_moves, estimated_prices=est_prices, aux_ids=aux_ids or None)
    t_build = time.time() - t_build
    if binfo.get("csv_overrides", {}).get("rejected"):
        log.warning("csv overrides rejected: %d (see events/ca_ledger_csv_overrides.csv)", binfo["csv_overrides"]["rejected"])

    # ---- revisions ----
    prev_L = pd.read_parquet(prev.ledger)
    new_L = pd.read_parquet(out / "events" / "ca_ledger.parquet")
    rev, rsum = diff_ledgers(prev_L, new_L, src_changes, inputs_changed, asof, prev.asof)
    src_summary = {}
    if len(src_changes):
        for (t, k), n in src_changes.groupby(["table", "kind"]).size().items():
            src_summary.setdefault(t, {})[k] = int(n)
        src_summary["by_cls"] = src_changes.cls.value_counts().to_dict()
    rsum.update({"asOf": str(asof.date()), "prevAsOf": str(prev.asof.date()), "prev_ledger": str(prev.ledger), "prev_rows": int(len(prev_L)), "rows": int(len(new_L)),
                 "source_changes": src_summary, "inputs_changed": inputs_changed, "source_mode": src_info.get("mode"), "source_cut": str(cut.date()) if cut is not None else None})
    rev_dir = write_revisions(out / "events", asof, rev, src_changes, rsum)

    # ---- sidecar asOf + incremental block ----
    side_p = out / "events" / "ca_ledger_build_info.json"
    side = json.loads(side_p.read_text())
    side["asOf"] = str(asof.date())
    side["calendarLast"] = str(pd.Timestamp(tdays.max()).date())
    side["incremental"] = {"prev": str(prev.path), "prevLayout": prev.layout, "prevAsOf": str(prev.asof.date()), "prevLedgerSha256": sha256(prev.ledger),
                           "sourceMode": src_info.get("mode"), "sourceCut": str(cut.date()) if cut is not None else None, "sourceChanges": src_summary,
                           "revisions": {k: rsum[k] for k in ("added", "removed", "changed_rows", "changed_cells", "rekeyed", "unattributed")}, "revisionsDir": str(rev_dir),
                           "inputsChanged": inputs_changed, "overrides": ovr_val, "dividendsLongRows": int(len(div_new)), "dividendsLongSha256": sha256(out / "root" / "dividends" / "dividends_long.parquet"),
                           "workSha256": {n: sha256(work / f"work_{n}.parquet") for n in WORK_TABLES}, "rootInfo": root_info,
                           "sourceStatus": status, "warnings": list(status.get("warnings", [])), "endedWithoutTermsMarkers": binfo.get("ended_without_terms_markers", 0)}
    side["inputs"].update({"root": str(out / "root"), "base_data": str(base_data), "aux_base_data": str(aux_base), "lineage": str(files["lineage"]), "lifecycle": str(files["lifecycle"]),
                           "r3_events": str(r3_events), "calendar": str(files["calendar"]), "split": str(files["split"]), "events": str(files["events"]),
                           "overrides_ref_ledger": str(ref_ledger), "split_moves": str(split_moves), "estimated_prices": str(est_prices)})
    side_p.write_text(json.dumps(side, indent=1, default=str))

    # ---- state for the next increment ----
    state = {"asOf": str(asof.date()), "builtAt": _now(), "prev": str(prev.path), "ledger": str(out / "events" / "ca_ledger.parquet"), "ledgerSha256": binfo["file"]["sha256"],
             "overrides_ref_ledger": str(ref_ledger), "overrides_ref_ledger_sha256": sha256(ref_ledger) if ref_ledger else None,
             "rc2_ref": str(rc2_ref) if rc2_ref and Path(rc2_ref).exists() else None, "rc2_ref_sha256": rc2_ref_sha(rc2_ref), "overrides_glob": a.overrides_glob,
             "files": {k: str(v) for k, v in files.items()}, "files_sha256": files_sha, "base_data": str(base_data), "aux_base_data": str(aux_base),
             "qa_files": {k: str(v) for k, v in qa_files.items()}, "qa_files_sha256": {k: sha256(v) for k, v in qa_files.items()},
             "distributions": str(prev.distributions_dir), "distributions_sha256": sha256(prev.distributions_dir / "distributions.parquet") if (prev.distributions_dir / "distributions.parquet").exists() else None,
             "staged_root": {k: v for k, v in root_info.items()},
             "sources_under_tmp": sorted(k for k, v in list(files.items()) + [("distributions", prev.distributions_dir)] if _is_tmp(v)),
             "tmp_free": not any(_is_tmp(q.resolve()) for q in (out / "root").rglob("*") if q.is_symlink()) and (rc2_ref is None or not _is_tmp(rc2_ref))
             and not any(_is_tmp(q.resolve()) for q in (out / "handover").glob("*") if q.is_symlink())}
    (out / STAGE_STATE).write_text(json.dumps(state, indent=1, default=str))

    # ---- QA (Q1~Q17) + optional export ----
    qa_res = None
    export = None
    if getattr(a, "export_root", None):
        from loader import CacheV21
        cv = CacheV21(Path(a.export_root), master_root=Path(a.export_master_root) if getattr(a, "export_master_root", None) else None, threads=8)
        (out / "engine").mkdir(exist_ok=True)
        export = cv.export_ca_ledger(out / "engine" / "ca_ledger_cache.parquet", ledger_path=out / "events" / "ca_ledger.parquet", events_path=files["events"])
        (out / "engine" / "ca_ledger_cache_manifest.json").write_text(json.dumps(export, indent=1, default=str))
        export = {k: v for k, v in export.items() if not isinstance(v, (list, dict))}
    if not getattr(a, "no_qa", False):
        import ledger_qa
        qa_res = ledger_qa.run(out / "events" / "ca_ledger.parquet", out / "root", out / "qa" / "ledger", base_data=base_data)
        qa_res = {k: (v["ok"] if isinstance(v, dict) and "ok" in v else v) for k, v in qa_res.items() if k != "unresolved_reasons"}
    info = {"asOf": str(asof.date()), "prevAsOf": str(prev.asof.date()), "prev": str(prev.path), "out": str(out), "ledger": binfo["file"], "rows_by_status": binfo.get("rows_by_status"),
            "source": src_info, "source_changes": src_summary, "revisions": {k: rsum[k] for k in ("added", "removed", "changed_rows", "changed_cells", "rekeyed", "unattributed", "by_source")},
            "inputs_changed": inputs_changed, "overrides": ovr_val, "csv_overrides": binfo.get("csv_overrides"), "qa": qa_res, "export": export,
            "source_status": status, "warnings": list(status.get("warnings", [])), "ended_without_terms_markers": binfo.get("ended_without_terms_markers", 0),
            "seconds": {"build": round(t_build, 1), "total": round(time.time() - t0, 1)}, "peak_rss_gb": round(rss_gb(), 2), "at": _now()}
    (out / STAGE_INFO).write_text(json.dumps(info, indent=1, default=str))
    log.info("incremental done: ledger %d rows sha %s; revisions +%d −%d ~%d rows; %.0fs, peak RSS %.2f GB", len(new_L), binfo["file"]["sha256"][:12], rsum["added"], rsum["removed"],
             rsum["changed_rows"], time.time() - t0, rss_gb())
    logging.getLogger().removeHandler(fh)
    return info


def add_incremental_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("incremental (D1)")
    g.add_argument("--incremental", action="store_true", help="이전 빌드 원장 + 증분 원천 조회로 --asof 까지 원장 갱신(ledger_incremental.py)")
    g.add_argument("--prev", type=Path, default=None, help="이전 D1 스테이지 / rc2f R1 스테이지 / 발행본 root")
    g.add_argument("--asof", default=None, help="T−1 (거래일)")
    g.add_argument("--build", type=Path, default=None, help="새 빌드(update_daily out-stage 또는 발행본): calendar·factors·events·master·universe·base_data 를 여기서")
    for name in ("calendar", "split-factors", "dividend-factors", "events", "instruments", "current", "membership", "qa-dir"):
        g.add_argument(f"--{name}", type=Path, default=None)
    # (--base-data --aux-base-data --lineage --lifecycle --r3-events --gap-candidates --overrides-ref-ledger --split-moves --estimated-prices --rc2-rules
    #  are ledger_build.py's own options; in --incremental they count only when given explicitly — args._given)
    g.add_argument("--overrides-glob", default=DEFAULT_OVERRIDES_GLOB, help="교정 CSV glob(ledger_*.csv 만 원장에 적용; rc2f_apply 검증기로 검증)")
    g.add_argument("--rc2-ref", type=Path, default=None, help="rc2 참조 빌드(검증기·eventId 해석; 기본 이전 스테이지 상태 또는 build_rc2_final)")
    g.add_argument("--skip-validate", action="store_true")
    g.add_argument("--source-extract", type=Path, default=None, help="미리 추출한 원천(raw_*.parquet) 디렉토리 — xfeed 대신 사용(게이트·테스트)")
    g.add_argument("--source-cut", default=None, help="게이트 시뮬레이션: 원천·캘린더·종가를 이 날짜(= --asof)까지로 자름")
    g.add_argument("--no-qa", action="store_true")
    g.add_argument("--allow-asof-before-prev", action="store_true", help="게이트 부트스트랩: 이전 asOf 보다 이른 --asof 허용(--source-cut 과 함께)")
    g.add_argument("--export-root", type=Path, default=None, help="loader.export_ca_ledger 호출 확인용 root(base_data.parquet + factors/)")
    g.add_argument("--export-master-root", type=Path, default=None)


def main_incremental(a) -> int:
    if a.prev is None or a.asof is None or a.out is None:
        raise SystemExit("--incremental needs --prev, --asof and --out")
    info = run_incremental(a)
    print(json.dumps(info, indent=1, default=str))
    return 0


# --------------------------------------------------------------------------------------
# helpers for the gate: extract only / prepare a full-rebuild root from a baseline + extract + cut
# --------------------------------------------------------------------------------------
def prepare_full(baseline: Path, extract: Path, cut, out: Path, full_mode: str = "refresh", asof=None, overrides_glob: str | None = None) -> dict:
    """전체 재생성용 스테이지: baseline(rc2f R1 스테이지 = 프로토 dividends_long·rc2 work 표) + 추출(cut) 을 **한 번에** 갱신해 root/work 를 만든다
    (증분 체인의 병합 코드를 거치지 않음). full_mode: refresh(프로토 파생값 보존, 추가·변경 행만 재계산) / scratch(전 행 파생 재계산)."""
    base = Prev(baseline)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cut = _ts(cut)
    asof = _ts(asof) if asof is not None else cut
    ex = cut_extract(load_extract(extract), cut)
    inst = pd.read_parquet(base.files["instruments"])
    cur = pd.read_parquet(base.files["current"])
    refs = Refs(inst, cur, ex["fx"], pd.read_parquet(base.files["split"]))
    prev_div = pd.read_parquet(base.dividends)
    if full_mode == "refresh":
        div, _ = refresh_dividends(prev_div, ex["ciqdividend"], refs, asof, None)
    elif full_mode == "scratch":
        div = build_dividends_scratch(ex["ciqdividend"], refs, asof)
    else:
        raise ValueError(full_mode)
    assemble_root(out / "root", base.files, div, base.distributions_dir, resolve_qa_files(None, None, base.qa_dir), set(int(i) for i in inst.tradingItemId), calendar_cut=cut)
    work = out / "work"
    work.mkdir(exist_ok=True)
    for n in WORK_TABLES:
        _write_df(normalize_work(n, ex[n]), work / f"work_{n}.parquet")
    if base.gap_union is not None:
        _copy(base.gap_union, work / "gap_candidates_union.csv")
    for stale in (work / "anchor_closes.parquet",):
        if stale.exists():
            stale.unlink()
    dist_df = pd.read_parquet(base.distributions_dir / "distributions.parquet", columns=["exDate"]) if (base.distributions_dir / "distributions.parquet").exists() else None
    (work / "source_status.json").write_text(json.dumps(source_status(ex, asof, dist_df), indent=1, default=str))
    csvs = _stage_override_copies(_ledger_csvs(overrides_glob), out) if overrides_glob else []
    return {"out": str(out), "cut": str(cut.date()), "full_mode": full_mode, "overrides": [str(c) for c in csvs], "dividends_rows": int(len(div)), "dividends_sha256": sha256(out / "root" / "dividends" / "dividends_long.parquet"),
            "work_sha256": {n: sha256(work / f"work_{n}.parquet") for n in WORK_TABLES}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="xfeed 원천 추출만(raw_*.parquet)")
    e.add_argument("--ids-from", type=Path, required=True, help="instruments.parquet")
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--prev-asof", required=True)
    e.add_argument("--asof", required=True)
    f = sub.add_parser("prepare-full", help="전체 재생성용 root/work 조립(baseline + extract + cut, 한 번에)")
    f.add_argument("--baseline", type=Path, required=True)
    f.add_argument("--extract", type=Path, required=True)
    f.add_argument("--cut", required=True)
    f.add_argument("--asof", default=None)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--full-mode", choices=("refresh", "scratch"), default="refresh")
    f.add_argument("--overrides-glob", default=DEFAULT_OVERRIDES_GLOB)
    m = sub.add_parser("make-ref", help="rc2 참조 빌드 → 내구 slim 참조(검증기용 원장·lifecycle·calendar·base_data id 목록)")
    m.add_argument("--rc2", type=Path, default=SCRATCH_RC2_REF)
    m.add_argument("--out", type=Path, default=DEFAULT_RC2_REF)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "extract":
        ids = pd.read_parquet(a.ids_from, columns=["tradingItemId"]).tradingItemId
        print(json.dumps(extract_sources(ids, a.out, a.prev_asof, a.asof), indent=1, default=str))
        return 0
    if a.cmd == "make-ref":
        print(json.dumps(make_ref(a.rc2, a.out), indent=1, default=str))
        return 0
    if a.cmd == "prepare-full":
        print(json.dumps(prepare_full(a.baseline, a.extract, a.cut, a.out, a.full_mode, a.asof, a.overrides_glob), indent=1, default=str))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
