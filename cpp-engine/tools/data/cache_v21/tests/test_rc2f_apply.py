#!/home/jgo/usr/venv-3.11/bin/python
"""rc2f X0 테스트: 교정 CSV 검증기·적용기(rc2f_apply.py), 원장 CSV 교정(ledger_repairs.load_csv_overrides/apply_csv_overrides), 재집계 귀속(rc2f_recount).

  /home/jgo/usr/venv-3.11/bin/python -m pytest tests/test_rc2f_apply.py -q      # pytest 가 있으면
  /home/jgo/usr/venv-3.11/bin/python tests/test_rc2f_apply.py [-k <name>]         # pytest 없이(기존 스위트와 같은 mini runner)
  RC2F_REAL_GATE=1 ... -k real      # 실데이터 게이트(빈 교정 → rc2 와 비트 동일; apply+publish ≈ 2 분; 출력 $RC2F_GATE_DIR(기본 /home/jgo/longshort/rc2f_x0/builds)/build_empty_gate)
  RC2F_SP=<scratchpad>               # rc2f_apply/rc2f_recount 의 SP 경로 재정의(기본 = 이 세션 스크래치패드)

합성 fixture(가격·계수·원장·계보·lifecycle·events)로 종류마다 1건 이상: split drop/set_factor/move_date/add/confirm, price multiply/set_close,
ledger set/drop/add + post(status unresolved·isEstimate), lifecycle settle_cash/convert_successor/last_close/unresolved, lineage set_ratio/drop/add/set_chainable.
각 검사는 기대값을 먼저 계산해 두고 결과와 비교한다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

try:
    import pytest
except ImportError:      # 기존 스위트처럼 pytest 없이도 실행
    pytest = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ledger_build as lb  # noqa: E402
import ledger_repairs as rep  # noqa: E402
import rc2f_apply as ra  # noqa: E402
from prices_factors import check_intervals  # noqa: E402

T = pd.Timestamp
PY = "/home/jgo/usr/venv-3.11/bin/python"


_WORK: Path | None = None


def workdir() -> Path:
    global _WORK
    if _WORK is None:
        _WORK = Path(tempfile.mkdtemp(prefix="rc2f_test_"))
    return _WORK


if pytest is not None:
    @pytest.fixture(scope="module")
    def work():
        return workdir()


# --------------------------------------------------------------------------------------
# 합성 fixture
# --------------------------------------------------------------------------------------
def split_table() -> pd.DataFrame:
    rows = [(1, T("1900-01-01"), T("2020-03-02"), 1.0, "synthetic_pre_first"), (1, T("2020-03-02"), T("2020-06-01"), 0.5, "ciqsplitcache"),
            (1, T("2020-06-01"), pd.NaT, 1.0, "ciqsplitcache"), (2, T("1900-01-01"), pd.NaT, 1.0, "synthetic_no_window"),
            (3, T("1900-01-01"), T("2020-07-01"), 3.0, "synthetic_pre_first"), (3, T("2020-07-01"), pd.NaT, 1.0, "ciqsplitcache")]
    df = pd.DataFrame(rows, columns=["tradingItemId", "validFrom", "validTo", "factor", "source"])
    df["snapshotDate"] = T("2026-09-17")
    for c in ("validFrom", "validTo", "snapshotDate"):
        df[c] = pd.to_datetime(df[c]).astype("datetime64[ns]")
    return df


def base_data(path: Path, cal: pd.DatetimeIndex) -> pd.DataFrame:
    """id 1,2,3 (+ id 3 는 2020-05-20 상장) raw 가격; id 별 row group."""
    rows = []
    for tid in (1, 2, 3):
        for k, d in enumerate(cal):
            if tid == 3 and d < T("2020-05-20"):
                continue
            px = {1: 100.0, 2: 40.0, 3: 20.0}[tid] + 0.5 * k
            rows.append((d, tid, np.float32(px - 1), np.float32(px + 1), np.float32(px - 2), np.float32(px), np.float32(px), 1000.0, 1, 0))
    df = pd.DataFrame(rows, columns=["date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume", "source", "qualityFlag"])
    schema = pa.schema([("date", pa.timestamp("ns")), ("tradingItemId", pa.int64()), ("open", pa.float32()), ("high", pa.float32()), ("low", pa.float32()),
                        ("close", pa.float32()), ("vwap", pa.float32()), ("volume", pa.float64()), ("source", pa.int8()), ("qualityFlag", pa.int8())])
    w = pq.ParquetWriter(path, schema, compression="zstd")
    for tid, g in df.groupby("tradingItemId"):
        w.write_table(pa.Table.from_pandas(g, schema=schema, preserve_index=False), row_group_size=len(g))
    w.close()
    return df


def calendar_obj(cal: pd.DatetimeIndex) -> lb.Calendar:
    return lb.Calendar(cal.to_numpy().astype("datetime64[D]"))


# --------------------------------------------------------------------------------------
# split
# --------------------------------------------------------------------------------------
def _pf(df: pd.DataFrame, tid: int) -> dict:
    """경계별 parentFactor = factorPrev/factorNext."""
    g = df[df.tradingItemId == tid].sort_values("validFrom").reset_index(drop=True)
    return {str(g.validFrom[i].date()): g.factor[i - 1] / g.factor[i] for i in range(1, len(g))}


def test_split_drop_set_move_add_confirm():
    sp = split_table()
    cal = calendar_obj(pd.bdate_range("2020-01-02", "2020-12-31"))
    g = sp[sp.tradingItemId == 1]
    assert _pf(sp, 1) == {"2020-03-02": 2.0, "2020-06-01": 0.5}
    # drop 2020-06-01 (0.5 -> 1): 이전 구간 x2, 병합; 03-02 비율 2 유지, 현재(열린 구간) 계수 1.0 유지
    g2, n, why = ra._split_one(g, dict(oid="X-1", exDate=T("2020-06-01"), action="drop"), cal)
    assert n == 2 and why.startswith("drop"), why
    assert _pf(g2, 1) == {"2020-03-02": 2.0} and g2.factor.tolist() == [2.0, 1.0] and pd.isna(g2.validTo.iloc[-1])
    assert "rc2f_scaled:X-1" in g2.source.iloc[-1] and "|rc2f:X-1" not in g2.source.iloc[-1] and check_intervals(_full(g2, sp))["ok"]   # merged interval: the remaining 03-02 boundary stays a vendor boundary
    # set_factor 03-02 -> 3 (3:1): 이전 구간 x1.5
    g2, n, why = ra._split_one(g, dict(oid="X-2", exDate=T("2020-03-02"), action="set_factor", newFactor=3.0), cal)
    assert n == 1 and _pf(g2, 1) == {"2020-03-02": 3.0, "2020-06-01": 0.5} and g2.factor.tolist() == [1.5, 0.5, 1.0]
    # 같은 값이면 no_op
    _, n0, why0 = ra._split_one(g, dict(oid="X-2b", exDate=T("2020-03-02"), action="set_factor", newFactor=2.0), cal)
    assert n0 == 0 and why0.startswith("no_op")
    # move_date 03-02 -> 03-09
    g2, n, why = ra._split_one(g, dict(oid="X-3", exDate=T("2020-03-02"), action="move_date", newExDate=T("2020-03-09")), cal)
    assert n == 2 and g2.validTo.iloc[0] == T("2020-03-09") and g2.validFrom.iloc[1] == T("2020-03-09") and _pf(g2, 1) == {"2020-03-09": 2.0, "2020-06-01": 0.5}
    # move crossing the next boundary -> reject
    _, n, why = ra._split_one(g, dict(oid="X-3b", exDate=T("2020-03-02"), action="move_date", newExDate=T("2020-06-01")), cal)
    assert n == 0 and why.startswith("reject")
    # add 4:1 at 2020-09-01: 열린 구간 분할, 이전 전부 x4, 경계 비율 2 / 0.5 / 4
    g2, n, why = ra._split_one(g, dict(oid="X-4", exDate=T("2020-09-01"), action="add", newFactor=4.0), cal)
    src_new = g2[g2.validFrom == T("2020-09-01")].source.iloc[0]; src_prev = g2[g2.validTo == T("2020-09-01")].source.iloc[0]
    assert "|rc2f:X-4" in src_new and "rc2f_scaled:X-4" in src_prev and "|rc2f:X-4" not in src_prev      # only the new boundary carries the status tag
    assert n == 4 and _pf(g2, 1) == {"2020-03-02": 2.0, "2020-06-01": 0.5, "2020-09-01": 4.0} and g2.factor.tolist() == [4.0, 2.0, 4.0, 1.0]
    assert check_intervals(_full(g2, sp))["ok"]
    # add at an existing boundary -> reject; confirm -> no change; drop at a non-boundary -> reject
    assert ra._split_one(g, dict(oid="X-5", exDate=T("2020-06-01"), action="add", newFactor=2.0), cal)[2].startswith("reject")
    g3, n, why = ra._split_one(g, dict(oid="X-6", exDate=T("2020-06-01"), action="confirm"), cal)
    assert n == 0 and g3.equals(g.sort_values("validFrom").reset_index(drop=True))
    assert ra._split_one(g, dict(oid="X-7", exDate=T("2020-04-01"), action="drop"), cal)[2].startswith("reject")
    # exDate 가 휴장일(validFrom 토요일 2020-02-29 형)이어도 캘린더 정렬 일치로 찾음
    g_wk = g.copy(); g_wk.loc[g_wk.validFrom == T("2020-03-02"), "validFrom"] = T("2020-02-29"); g_wk.loc[g_wk.validTo == T("2020-03-02"), "validTo"] = T("2020-02-29")
    g2, n, why = ra._split_one(g_wk, dict(oid="X-8", exDate=T("2020-03-02"), action="drop"), cal)
    assert n == 1 and _pf(g2, 1) == {"2020-06-01": 0.5}


def _full(g: pd.DataFrame, sp: pd.DataFrame) -> pd.DataFrame:
    tid = int(g.tradingItemId.iloc[0])
    out = pd.concat([sp[sp.tradingItemId != tid], g[sp.columns]], ignore_index=True).sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    for c in ("validFrom", "validTo", "snapshotDate"):
        out[c] = pd.to_datetime(out[c]).astype("datetime64[ns]")
    return out


def test_split_overrides_table_and_log():
    """apply_split_overrides: T2/aux 표 선택, 로그 status, 계약 검사."""
    sp = split_table()
    cal = calendar_obj(pd.bdate_range("2020-01-02", "2020-12-31"))
    df = pd.DataFrame([dict(overrideId="S-1", caseId="c", x_tid=1, exDate="2020-06-01", action="drop", newFactor="", newExDate="", x_isEstimate=False),
                       dict(overrideId="S-2", caseId="c", x_tid=3, exDate="2020-07-01", action="set_factor", newFactor="0.2", newExDate="", x_isEstimate=True),
                       dict(overrideId="S-3", caseId="c", x_tid=9, exDate="2020-07-01", action="drop", newFactor="", newExDate="", x_isEstimate=False),
                       dict(overrideId="S-4", caseId="c", x_tid=2, exDate="2020-07-01", action="confirm", newFactor="", newExDate="", x_isEstimate=False)])
    alog = ra.ApplyLog()
    res = ra.apply_split_overrides(df, {"T2": (sp[sp.tradingItemId != 3], {1, 2}), "aux": (sp[sp.tradingItemId == 3], {3})}, alog, cal)
    lg = alog.frame().set_index("overrideId")
    # Y2a: without price data the raw-jump check is not possible -> applied_unchecked (the correction is applied all the same)
    assert lg.loc["S-1", "status"] == "applied_unchecked" and lg.loc["S-2", "status"] == "applied_unchecked" and lg.loc["S-3", "status"] == "rejected" and lg.loc["S-4", "status"] == "rejected"
    assert "check=uncheckable: no_price_data" in lg.loc["S-1", "reason"]
    assert res["T2"][1] and res["aux"][1]
    assert _pf(res["T2"][0], 1) == {"2020-03-02": 2.0} and _pf(res["aux"][0], 3) == {"2020-07-01": 0.2}
    assert check_intervals(res["T2"][0])["ok"] and check_intervals(res["aux"][0])["ok"]
    # R-X0 P2-2: isEstimate=true -> the boundary row (sourceNext) carries the rc2f_est tag -> ledger split row estimated
    a3 = res["aux"][0]; a3 = a3[a3.tradingItemId == 3].sort_values("validFrom")
    assert "rc2f_est:S-2" in a3.source.iloc[-1]
    cal2 = calendar_obj(pd.bdate_range("2020-01-02", "2020-12-31"))
    tmp = workdir() / "split_est"; tmp.mkdir(exist_ok=True)
    base_data(tmp / "base.parquet", pd.bdate_range("2020-01-02", "2020-12-31"))
    rows = lb.build_split_rows(res["aux"][0], pd.DataFrame({"splitid": [1], "tradingitemid": [3], "exdate": [T("2020-07-01")], "rate": [0.2], "splittypeid": [12], "appliedflag": [1]}), cal2, lb.RawCloses(tmp / "base.parquet"))
    assert rows.status.iloc[0] == "estimated" and rows.isEstimate.iloc[0] and "rc2f_override_estimate" in rows.notes.iloc[0] and rows.repairOf.iloc[0] == "csv_override:S-2"
    # L1 final (lead decision): a CSV boundary with isEstimate=false is `ok` whatever the builder's price classification says (kept in notes, apply
    # log carries applied_unverified); the factor-artifact list does not demote it either
    t2b = res["T2"][0]; t2b = t2b[t2b.tradingItemId == 1].sort_values("validFrom")         # S-1 dropped 06-01: the remaining 03-02 boundary carries no tag
    sp_csv = split_table(); sp_csv = sp_csv[sp_csv.tradingItemId == 2]
    df_add = pd.DataFrame([dict(overrideId="S-7", caseId="c", x_tid=2, exDate="2020-09-01", action="add", newFactor="2", newExDate="", x_isEstimate=False)])
    res_add = ra.apply_split_overrides(df_add, {"T2": (sp_csv, {2})}, ra.ApplyLog(), cal)
    rows_ok = lb.build_split_rows(res_add["T2"][0], None, cal2, lb.RawCloses(tmp / "base.parquet"))
    r_ok = rows_ok[rows_ok.tradingItemId == 2].iloc[0]
    assert r_ok.status == "ok" and not r_ok.isEstimate and r_ok.repairOf == "csv_override:S-7" and "csv_split_boundary(builder_status=unresolved)" in r_ok.notes and "factor_step_without_price_move" in r_ok.notes
    art_dir = tmp / "root_art" / "qa" / "T2"; art_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"tradingItemId": [2], "date": ["2020-09-01"], "prevDate": ["2020-08-31"], "sf_prev": [1.0], "sf": [0.5], "r_raw": [0.004], "r_adj": [1.008], "category": ["unexplained_step"]}).to_csv(art_dir / "factor_artifacts.csv", index=False)
    kept, _ = rep.split_artifact_check(rows_ok.copy(), None, None, lb.RawCloses(tmp / "base.parquet"), tmp / "root_art", cal2)
    k = kept[kept.tradingItemId == 2].iloc[0]
    assert k.status == "ok" and "artifact_listed(csv_boundary_status_kept)" in k.notes
    # R-X0 2nd review P1-7: with raw closes the applier checks the corrected boundaries against the price move — fixture prices are continuous, so
    # a set_factor 0.2 (jump x pf = 0.2) and an add 2:1 are applied_unverified, while a drop of a boundary with no price jump is verified (applied)
    closes = lb.RawCloses(tmp / "base.parquet")
    df2 = pd.DataFrame([dict(overrideId="S-1", caseId="c", x_tid=1, exDate="2020-06-01", action="drop", newFactor="", newExDate="", x_isEstimate=False),
                        dict(overrideId="S-2", caseId="c", x_tid=3, exDate="2020-07-01", action="set_factor", newFactor="0.2", newExDate="", x_isEstimate=True),
                        dict(overrideId="S-5", caseId="c", x_tid=2, exDate="2020-09-01", action="add", newFactor="2", newExDate="", x_isEstimate=False),
                        dict(overrideId="S-6", caseId="c", x_tid=1, exDate="2020-03-02", action="move_date", newFactor="", newExDate="2020-03-09", x_isEstimate=False)])
    alog2 = ra.ApplyLog()
    ra.apply_split_overrides(df2, {"T2": (sp[sp.tradingItemId != 3], {1, 2}), "aux": (sp[sp.tradingItemId == 3], {3})}, alog2, cal, closes)
    lg2 = alog2.frame().set_index("overrideId")
    assert lg2.loc["S-1", "status"] == "applied" and "check=pass" in lg2.loc["S-1", "reason"] and "raw_jump=" in lg2.loc["S-1", "reason"]      # pass: values logged
    assert lg2.loc["S-2", "status"] == "applied_unverified" and "check=fail" in lg2.loc["S-2", "reason"] and "raw_jump_x_parentFactor=0.2" in lg2.loc["S-2", "reason"]
    assert lg2.loc["S-5", "status"] == "applied_unverified" and "boundary 2020-09-01: raw_jump_x_parentFactor=2" in lg2.loc["S-5", "reason"]
    assert lg2.loc["S-6", "status"] == "applied_unverified" and "boundary 2020-03-09: raw_jump_x_parentFactor=2" in lg2.loc["S-6", "reason"] and "removed_boundary 2020-03-02: raw_jump=" in lg2.loc["S-6", "reason"]   # old date continuous, new date has no price step
    # uncheckable: id 3 lists 2020-05-20 -> a boundary on 2020-05-20 has no raw close within 5 trading days BEFORE it; a boundary inside a print gap
    # (id 1 has no prints 03-04..03-19 in base_gap) has none on/after within 5 trading days -> applied_unchecked with the reason
    b_gap = pd.read_parquet(tmp / "base.parquet"); b_gap = b_gap[~((b_gap.tradingItemId == 1) & (b_gap.date >= T("2020-03-04")) & (b_gap.date < T("2020-03-20")))]
    b_gap.to_parquet(tmp / "base_gap.parquet", index=False)
    df3 = pd.DataFrame([dict(overrideId="U-1", caseId="c", x_tid=3, exDate="2020-05-20", action="add", newFactor="2", newExDate="", x_isEstimate=False),
                        dict(overrideId="U-2", caseId="c", x_tid=1, exDate="2020-03-02", action="move_date", newFactor="", newExDate="2020-03-10", x_isEstimate=False),
                        dict(overrideId="U-3", caseId="c", x_tid=1, exDate="2020-06-01", action="drop", newFactor="", newExDate="", x_isEstimate=False)])
    alog3 = ra.ApplyLog()
    ra.apply_split_overrides(df3, {"T2": (sp[sp.tradingItemId != 3], {1, 2}), "aux": (sp[sp.tradingItemId == 3], {3})}, alog3, cal, lb.RawCloses(tmp / "base_gap.parquet"))
    lg3 = alog3.frame().set_index("overrideId")
    assert lg3.loc["U-1", "status"] == "applied_unchecked" and "uncheckable(no_close_before" in lg3.loc["U-1", "reason"]
    assert lg3.loc["U-2", "status"] == "applied_unchecked" and "boundary 2020-03-10: uncheckable(no_close_on_or_after" in lg3.loc["U-2", "reason"] and "removed_boundary 2020-03-02: raw_jump=" in lg3.loc["U-2", "reason"]   # old boundary checkable (pass), new one not -> uncheckable overall
    assert lg3.loc["U-3", "status"] == "applied" and "check=pass" in lg3.loc["U-3", "reason"]          # prices around 06-01 intact -> pass
    # R-Y2 P2: (i) a move_date INSIDE a print gap (03-04..03-19 has no prints; boundary 03-05 -> 03-12) has no independent old-boundary check ->
    # skipped, the state comes from the new boundary (uncheckable here); (ii) the deferred note when the id had a price override in the run;
    # (iii) a boundary whose validFrom sits on a weekend is still found (pf_at tolerance)
    sp_gap = sp[sp.tradingItemId == 1].copy(); sp_gap.loc[sp_gap.validFrom == T("2020-03-02"), "validFrom"] = T("2020-03-05")
    sp_gap.loc[sp_gap.validTo == T("2020-03-02"), "validTo"] = T("2020-03-05")
    df4 = pd.DataFrame([dict(overrideId="G-1", caseId="c", x_tid=1, exDate="2020-03-05", action="move_date", newFactor="", newExDate="2020-03-12", x_isEstimate=False)])
    alog4 = ra.ApplyLog()
    ra.apply_split_overrides(df4, {"T2": (sp_gap, {1})}, alog4, cal, lb.RawCloses(tmp / "base_gap.parquet"), {1: ["P-9"]})
    lg4 = alog4.frame().set_index("overrideId")
    assert lg4.loc["G-1", "status"] == "applied_unchecked" and "removed_boundary 2020-03-05: skipped(no raw print between" in lg4.loc["G-1", "reason"] \
        and "boundary 2020-03-12: uncheckable" in lg4.loc["G-1", "reason"] and "[re-checked after price override(s) P-9]" in lg4.loc["G-1", "reason"]
    sp_we = sp[sp.tradingItemId == 1].copy(); sp_we.loc[sp_we.validFrom == T("2020-03-02"), "validFrom"] = T("2020-03-01")      # Sunday
    sp_we.loc[sp_we.validTo == T("2020-03-02"), "validTo"] = T("2020-03-01")
    df5 = pd.DataFrame([dict(overrideId="W-1", caseId="c", x_tid=1, exDate="2020-03-02", action="set_factor", newFactor="3", newExDate="", x_isEstimate=False)])
    alog5 = ra.ApplyLog()
    ra.apply_split_overrides(df5, {"T2": (sp_we, {1})}, alog5, cal, closes)
    lg5 = alog5.frame().set_index("overrideId")
    assert lg5.loc["W-1", "status"] in ("applied", "applied_unverified") and "raw_jump_x_parentFactor=" in lg5.loc["W-1", "reason"], lg5.loc["W-1", "reason"]   # pf found despite the weekend validFrom
    # a real price step at the boundary: id 1 halves on 2020-03-02 (matching the fixture's 2:1 step) -> dropping that boundary is applied_unverified,
    # keeping it via move_date is unverified on both dates only for the new one
    b2 = pd.read_parquet(tmp / "base.parquet"); m = (b2.tradingItemId == 1) & (b2.date >= T("2020-03-02"))
    for c in ("open", "high", "low", "close", "vwap"):
        b2.loc[m, c] = (b2.loc[m, c] / 2).astype("float32")
    b2.to_parquet(tmp / "base_step.parquet", index=False)
    alog4 = ra.ApplyLog()
    ra.apply_split_overrides(df2[df2.overrideId == "S-6"].assign(action="drop", newExDate=""), {"T2": (sp[sp.tradingItemId != 3], {1, 2}), "aux": (sp[sp.tradingItemId == 3], {3})}, alog4, cal, lb.RawCloses(tmp / "base_step.parquet"))
    lg4 = alog4.frame().set_index("overrideId")
    assert lg4.loc["S-6", "status"] == "applied_unverified" and "removed_boundary 2020-03-02: raw_jump=0.5" in lg4.loc["S-6", "reason"], lg4.loc["S-6", "reason"]
    assert "x old parentFactor 2 = 1" in lg4.loc["S-6", "reason"]           # the vendor step explained the price move -> the drop is suspicious


# --------------------------------------------------------------------------------------
# price
# --------------------------------------------------------------------------------------
def test_price_overrides_rewrite(work=None):
    work = work or workdir()
    cal = pd.bdate_range("2020-01-02", "2020-12-31")
    src = work / "base_data.parquet"
    base = base_data(src, cal)
    df = pd.DataFrame([dict(overrideId="P-1", caseId="c", x_tid=1, dateFrom="2020-03-02", dateTo="2020-03-04", op="multiply", value="0.5", fields="close"),
                       dict(overrideId="P-2", caseId="c", x_tid=2, dateFrom="2020-02-03", dateTo="2020-02-03", op="set_close", value="12.5", fields=""),
                       dict(overrideId="P-3", caseId="c", x_tid=3, dateFrom="2020-01-02", dateTo="2020-01-10", op="multiply", value="2", fields="")])
    alog = ra.ApplyLog()
    dst = work / "base_out.parquet"
    r = ra.apply_price_overrides(df, src, dst, alog, {1, 2, 3}, "T2")
    assert r["rows_changed"] == 4 and r["rewritten"]
    lg = alog.frame().set_index("overrideId")
    assert lg.loc["P-1", "status"] == "applied" and int(lg.loc["P-1", "affected_rows"]) == 3
    assert lg.loc["P-2", "status"] == "applied" and int(lg.loc["P-2", "affected_rows"]) == 1
    assert lg.loc["P-3", "status"] == "rejected" and lg.loc["P-3", "reason"] == "no_rows_in_range"      # id 3 은 2020-05-20 상장
    out = pd.read_parquet(dst)
    assert len(out) == len(base) and pq.ParquetFile(dst).metadata.num_row_groups == pq.ParquetFile(src).metadata.num_row_groups
    m1 = (out.tradingItemId == 1) & (out.date >= T("2020-03-02")) & (out.date <= T("2020-03-04"))
    b1 = (base.tradingItemId == 1) & (base.date >= T("2020-03-02")) & (base.date <= T("2020-03-04"))
    assert np.allclose(out[m1].close.to_numpy(), base[b1].close.to_numpy() * 0.5) and (out[m1].qualityFlag == ra.QF_PRICE_OVERRIDE).all()
    assert np.array_equal(out[m1].open.to_numpy(), base[b1].open.to_numpy()) and np.array_equal(out[m1].volume.to_numpy(), base[b1].volume.to_numpy())
    m2 = (out.tradingItemId == 2) & (out.date == T("2020-02-03"))
    assert (out[m2][["open", "high", "low", "close", "vwap"]].to_numpy() == np.float32(12.5)).all() and int(out[m2].qualityFlag.iloc[0]) == ra.QF_PRICE_OVERRIDE
    untouched = ~(m1 | m2)
    for c in ("open", "high", "low", "close", "vwap", "volume", "qualityFlag"):
        assert np.array_equal(out[untouched][c].to_numpy(), base[~(b1 | ((base.tradingItemId == 2) & (base.date == T("2020-02-03"))))][c].to_numpy()), c
    # R-Y2 P1 ①: an ESTIMATED price correction (isEstimate true) writes qualityFlag 32, the returns rows on those prices (and the next row, whose
    # prev close is estimated) carry retFlag >= 3, and a ledger split row corroborated with them is estimated
    import prices_returns as prm
    df_e = pd.DataFrame([dict(overrideId="P-4", caseId="c", x_tid=1, dateFrom="2020-06-01", dateTo="2020-06-03", op="multiply", value="0.5", fields="", x_isEstimate=True)])
    alog_e = ra.ApplyLog(); dst_e = work / "base_est.parquet"
    ra.apply_price_overrides(df_e, src, dst_e, alog_e, {1, 2, 3}, "T2")
    out_e = pd.read_parquet(dst_e)
    me = (out_e.tradingItemId == 1) & (out_e.date >= T("2020-06-01")) & (out_e.date <= T("2020-06-03"))
    assert (out_e[me].qualityFlag == ra.QF_PRICE_OVERRIDE_EST).all() and "estimate" in alog_e.frame().reason.iloc[0]
    stage_e = work / "stage_est"; (stage_e / "events").mkdir(parents=True, exist_ok=True)
    shutil.copy2(dst_e, stage_e / "base_data.parquet")
    lb.write_ledger(lb.finalize([_frame([])]) if False else _frame([]).assign(eventId=pd.Series(dtype=str)), stage_e / "events" / "ca_ledger.parquet")
    info_e = prm.build_returns_v2(stage_e, stage_e / "events" / "ca_ledger.parquet", stage_e / "returns.parquet")
    rt = pd.read_parquet(stage_e / "returns.parquet"); r1 = rt[rt.tradingItemId == 1].set_index("date")
    assert int(r1.at[T("2020-06-01"), "retFlag"]) >= 3 and int(r1.at[T("2020-06-03"), "retFlag"]) >= 3 and int(r1.at[T("2020-06-04"), "retFlag"]) >= 3 \
        and int(r1.at[T("2020-06-05"), "retFlag"]) == 0 and int(r1.at[T("2020-05-29"), "retFlag"]) == 0 and info_e["rows_on_estimated_prices_flagged"] == 4
    est_csv = work / "price_override_estimates.csv"
    pd.DataFrame([{"tradingItemId": 1, "dateFrom": "2020-06-01", "dateTo": "2020-06-03", "overrideId": "P-4"}]).to_csv(est_csv, index=False)
    sp_rows = _frame([dict(tradingItemId=1, exDate=T("2020-06-01"), eventType="split", legType="quantity", parentFactor=0.5, source="ciqsplitcache", status="ok", notes="raw_jump_x_pf=1.0"),
                      dict(tradingItemId=1, exDate=T("2020-09-01"), eventType="split", legType="quantity", parentFactor=2.0, source="ciqsplitcache", status="ok", notes="")])
    marked, n_m = lb.mark_estimated_price_split_rows(sp_rows, pd.read_csv(est_csv, dtype=str, keep_default_na=False), lb.RawCloses(dst_e))
    assert n_m == 1 and marked.status.iloc[0] == "estimated" and bool(marked.isEstimate.iloc[0]) and "corroborated_with_estimated_prices:P-4" in marked.notes.iloc[0] \
        and "price_override_estimate:P-4" in marked.repairOf.iloc[0] and marked.status.iloc[1] == "ok"


# --------------------------------------------------------------------------------------
# value (C5)
# --------------------------------------------------------------------------------------
def test_value_overrides_apply(work=None):
    work = work or workdir()
    cal = pd.bdate_range("2020-01-02", "2020-12-31")
    full = work / "base_val_full.parquet"
    base = base_data(full, cal)
    gone = (base.tradingItemId == 2) & (base.date == T("2020-12-31"))           # id 2 last day row missing (B4-like)
    src = work / "base_val.parquet"
    schema = pq.ParquetFile(full).schema_arrow
    w = pq.ParquetWriter(src, schema, compression="zstd")
    for tid, g in base[~gone].groupby("tradingItemId"):
        w.write_table(pa.Table.from_pandas(g, schema=schema, preserve_index=False), row_group_size=len(g))
    w.close()
    lc = lifecycle_table().set_index("tradingItemId")
    c = dict(caseId="c", source="xfeed:miprice")
    df = pd.DataFrame([dict(overrideId="VO-1", x_tid=1, date="2020-03-02", field="volume", value="123456", **c),
                       dict(overrideId="VO-2", x_tid=1, date="2020-03-02", field="close", value="7.5", **c),
                       dict(overrideId="VO-3", x_tid=2, date="2020-12-31", field="close", value="41.0", **c),          # new row (inside lifecycle)
                       dict(overrideId="VO-4", x_tid=2, date="2020-12-31", field="volume", value="0", **c),            # same new row
                       dict(overrideId="VO-5", x_tid=3, date="2020-05-19", field="close", value="5", **c),             # before firstTradeDate -> rejected
                       dict(overrideId="VO-6", x_tid=1, date="2020-07-04", field="close", value="5", **c)])            # not in the calendar below -> rejected
    cal_days = cal[cal != T("2020-07-04")].to_numpy(dtype="datetime64[ns]")
    alog = ra.ApplyLog()
    dst = work / "base_val_out.parquet"
    r = ra.apply_value_overrides(df, src, dst, alog, {1, 2, 3}, "T2", lc, cal_days)
    assert r["rows_set"] == 1 and r["rows_new"] == 1 and r["rewritten"], r
    lg = alog.frame().set_index("overrideId")
    assert (lg.loc[["VO-1", "VO-2", "VO-3", "VO-4"], "status"] == "applied").all() and (lg.loc[["VO-5", "VO-6"], "status"] == "rejected").all(), lg
    assert "lifecycle" in lg.loc["VO-5", "reason"] and "trading day" in lg.loc["VO-6", "reason"]
    out = pd.read_parquet(dst)
    assert len(out) == int((~gone).sum()) + 1
    assert (out.sort_values(["tradingItemId", "date"]).index == out.index).all()                          # (id, date) order kept
    k = out[(out.tradingItemId == 1) & (out.date == T("2020-03-02"))].iloc[0]
    kb = base[(base.tradingItemId == 1) & (base.date == T("2020-03-02"))].iloc[0]
    assert k.volume == 123456.0 and k.close == np.float32(7.5) and k.open == kb.open and int(k.qualityFlag) == int(kb.qualityFlag)   # existing row: flag kept
    n = out[(out.tradingItemId == 2) & (out.date == T("2020-12-31"))].iloc[0]
    assert n.close == np.float32(41.0) and n.volume == 0.0 and np.isnan(n.open) and np.isnan(n.vwap) and int(n.qualityFlag) == ra.QF_VALUE_OVERRIDE and int(n.source) == ra.SOURCE_MI
    rest = out[~(((out.tradingItemId == 1) & (out.date == T("2020-03-02"))) | ((out.tradingItemId == 2) & (out.date == T("2020-12-31"))))].reset_index(drop=True)
    exp = base[~gone & ~((base.tradingItemId == 1) & (base.date == T("2020-03-02")))].reset_index(drop=True)
    for col in exp.columns:
        assert np.array_equal(rest[col].to_numpy(), exp[col].to_numpy()), col
    # empty -> nothing rewritten (cmd_apply keeps the links: bit identical)
    assert ra.apply_value_overrides(df.iloc[:0], src, work / "base_val_none.parquet", ra.ApplyLog(), {1, 2, 3}, "T2", lc, cal_days)["rewritten"] is False
    assert not (work / "base_val_none.parquet").exists()


def test_value_overrides_null_preserved(work=None):
    """C5b P2: row group rewrite keeps the original nulls of cells that were not set."""
    work = work or workdir()
    cal = pd.bdate_range("2020-01-02", "2020-03-31")
    full = work / "base_null_full.parquet"
    base = base_data(full, cal)
    schema = pq.ParquetFile(full).schema_arrow
    src = work / "base_null.parquet"
    t = pa.Table.from_pandas(base, schema=schema, preserve_index=False)
    nm = np.zeros(len(base), dtype=bool); nm[::5] = True                                 # nulls in open/close/vwap every 5th row
    for f in ("open", "close", "vwap"):
        t = t.set_column(t.schema.get_field_index(f), f, pa.array(base[f].to_numpy(), type=schema.field(f).type, mask=nm))
    pq.write_table(t, src, row_group_size=len(base))
    before = {f: pq.read_table(src).column(f).null_count for f in ra.VALUE_FIELDS}
    tgt = base[nm].iloc[0]                                                                 # set close on a row whose close is null
    df = pd.DataFrame([dict(overrideId="VN-1", x_tid=int(tgt.tradingItemId), date=str(tgt.date.date()), field="close", value="9.5", caseId="c", source="xfeed:ciq")])
    dst = work / "base_null_out.parquet"
    r = ra.apply_value_overrides(df, src, dst, ra.ApplyLog(), set(base.tradingItemId.unique().tolist()), "T2", lifecycle_table().set_index("tradingItemId"), cal.to_numpy(dtype="datetime64[ns]"))
    assert r["rows_set"] == 1 and r["rows_new"] == 0, r
    after = pq.read_table(dst)
    for f in ra.VALUE_FIELDS:
        assert after.column(f).null_count == before[f] - (1 if f == "close" else 0), (f, before[f], after.column(f).null_count)


def test_value_overrides_validator(work=None):
    work = work or workdir()
    ref = synth_ref(work)
    d = work / "vv"; d.mkdir(exist_ok=True)
    common = dict(caseId="1", evidence="ev", source="xfeed:miprice", isEstimate="false", owner="T")
    pd.DataFrame([dict(overrideId="VV-1", tradingItemId=1, date="2020-03-05", field="volume", value="0", **common),       # ok (volume 0 allowed)
                  dict(overrideId="VV-2", tradingItemId=1, date="2020-03-05", field="close", value="10", **common),       # ok (other field)
                  dict(overrideId="VV-3", tradingItemId=1, date="2020-03-05", field="volume", value="5", **common),       # duplicate cell
                  dict(overrideId="VV-4", tradingItemId=1, date="2020-03-07", field="close", value="10", **common),       # saturday
                  dict(overrideId="VV-5", tradingItemId=1, date="2020-03-09", field="shares", value="10", **common),      # bad field
                  dict(overrideId="VV-6", tradingItemId=1, date="2020-03-09", field="close", value="0", **common),        # price <= 0
                  dict(overrideId="VV-7", tradingItemId=1, date="2020-03-09", field="volume", value="nan", **common),     # not finite
                  dict(overrideId="VV-8", tradingItemId=1, date="2020-03-03", field="close", value="10", **common),       # conflicts with price override P
                  dict(overrideId="VV-9", tradingItemId=1, date="2020-03-03", field="volume", value="10", **common),      # volume: no conflict with price override
                  dict(overrideId="VV-10", tradingItemId=3, date="2020-05-19", field="close", value="10", **common),      # outside lifecycle -> warning
                  dict(overrideId="VV-11", tradingItemId=1, date="2020-03-10", field="close", value="1e39", **common)]    # not finite as float32
                 ).to_csv(d / "value_overrides.csv", index=False)
    pd.DataFrame([dict(overrideId="P-1", tradingItemId=1, dateFrom="2020-03-02", dateTo="2020-03-04", op="multiply", value="0.5", fields="close", **common)]).to_csv(d / "price_overrides.csv", index=False)
    _, iss = ra.validate_files(sorted(str(p) for p in d.glob("*.csv")), ref)
    errs = {(r["overrideId"], r["message"]) for r in iss.rows if r["level"] == "ERROR"}
    assert {o for o, _ in errs} == {"VV-3", "VV-4", "VV-5", "VV-6", "VV-7", "VV-8", "VV-11"}, sorted(errs)
    assert any("conflicts with price override" in m for o, m in errs if o == "VV-8") and any("not a trading day" in m for o, m in errs if o == "VV-4")
    assert any(r["overrideId"] == "VV-10" and "outside lifecycle" in r["message"] for r in iss.rows if r["level"] == "WARN")


# --------------------------------------------------------------------------------------
# lineage
# --------------------------------------------------------------------------------------
def lineage_table() -> pd.DataFrame:
    df = pd.DataFrame([dict(oldId=1, newId=2, relation="relist", effectiveDate=T("2020-06-01"), ratio=np.nan, evidence="e", confidence="medium", ratioSource="assumed_1to1",
                            linkClass="relist", reviewFlag="", sources="v1", version="v1-rc1", chainGapTradingDays=0, chainable=False),
                       dict(oldId=5, newId=6, relation="merger", effectiveDate=T("2021-01-04"), ratio=0.5, evidence="e", confidence="high", ratioSource="",
                            linkClass="", reviewFlag="", sources="v1", version="v1-rc1", chainGapTradingDays=pd.NA, chainable=False)])
    df["chainGapTradingDays"] = df.chainGapTradingDays.astype("Int64")
    return df


def lifecycle_table() -> pd.DataFrame:
    return pd.DataFrame([dict(tradingItemId=1, firstTradeDate=T("2020-01-02"), firstPriceDate=T("2020-01-02"), firstTradeSource="ciq", ciqLastCloseDate=T("2020-12-31"),
                              miLastCloseDate=T("2020-12-31"), securityEndDate=pd.NaT, tradingItemStatusId=15, eventEndDate=pd.NaT, lastTradeDate=pd.NaT, lastTradeSource="ciq",
                              endReason="active", status="active", observedAt=T("2026-09-15"), evidence="e"),
                         dict(tradingItemId=2, firstTradeDate=T("2020-01-02"), firstPriceDate=T("2020-01-02"), firstTradeSource="ciq", ciqLastCloseDate=T("2020-12-31"),
                              miLastCloseDate=T("2020-12-31"), securityEndDate=pd.NaT, tradingItemStatusId=15, eventEndDate=pd.NaT, lastTradeDate=T("2020-12-31"), lastTradeSource="ciq",
                              endReason="delist_other", status="confirmed", observedAt=T("2026-09-15"), evidence="e"),
                         dict(tradingItemId=3, firstTradeDate=T("2020-05-20"), firstPriceDate=T("2020-05-20"), firstTradeSource="ciq", ciqLastCloseDate=T("2020-12-31"),
                              miLastCloseDate=T("2020-12-31"), securityEndDate=pd.NaT, tradingItemStatusId=15, eventEndDate=pd.NaT, lastTradeDate=pd.NaT, lastTradeSource="ciq",
                              endReason="active", status="active", observedAt=T("2026-09-15"), evidence="e")])


def test_lineage_overrides():
    ln = lineage_table()
    lc = lifecycle_table().set_index("tradingItemId")
    cal = pd.bdate_range("2020-01-02", "2021-12-31").to_numpy().astype("datetime64[ns]")
    df = pd.DataFrame([dict(overrideId="L-1", caseId="c", oldId="1", newId="2", action="set_ratio", ratio="2.0", effectiveDate="", relation="", chainable="", evidence="e", x_isEstimate=False, x_tid=1),
                       dict(overrideId="L-2", caseId="c", oldId="5", newId="6", action="drop", ratio="", effectiveDate="", relation="", chainable="", evidence="e", x_isEstimate=False, x_tid=5),
                       dict(overrideId="L-3", caseId="c", oldId="2", newId="3", action="add", ratio="1.0", effectiveDate="2020-05-20", relation="rename", chainable="", evidence="e", x_isEstimate=True, x_tid=2),
                       dict(overrideId="L-4", caseId="c", oldId="1", newId="2", action="set_chainable", ratio="", effectiveDate="", relation="", chainable="false", evidence="e", x_isEstimate=False, x_tid=1),
                       dict(overrideId="L-5", caseId="c", oldId="7", newId="8", action="set_ratio", ratio="1.0", effectiveDate="", relation="", chainable="", evidence="e", x_isEstimate=False, x_tid=7)])
    df["linkVerified"] = ["", "", "true", "", ""]            # rc2f-E: L-3 (add) is a verified first-day move
    alog = ra.ApplyLog()
    out = ra.apply_lineage_overrides(df, ln, lc, cal, alog)
    lg = alog.frame().set_index("overrideId")
    assert lg.loc["L-1", "status"] == "applied" and lg.loc["L-2", "status"] == "applied" and lg.loc["L-3", "status"] == "applied" and lg.loc["L-4", "status"] == "applied" and lg.loc["L-5", "status"] == "rejected"
    r12 = out[(out.oldId == 1) & (out.newId == 2)].iloc[0]
    assert r12.ratio == 2.0 and r12.ratioSource == "rc2f_override:L-1" and r12.confidence == "high" and r12.chainable == False and r12.version == "rc2f"   # noqa: E712 (set_chainable false 뒤)
    assert not ((out.oldId == 5) & (out.newId == 6)).any()
    r23 = out[(out.oldId == 2) & (out.newId == 3)].iloc[0]
    assert r23.relation == "rename" and r23.ratioSource == "rc2f_estimate:L-3" and r23.confidence == "medium" and r23.linkClass == "simple_rename" and r23.chainable == True   # noqa: E712
    assert bool(r23.linkVerified) is True and bool(r12.linkVerified) is False and "linkVerified True" in lg.loc["L-3", "reason"] and out.linkVerified.dtype == bool    # rc2f-E column
    df_lv = df.iloc[:1].copy(); df_lv["linkVerified"] = ["true"]
    out_lv = ra.apply_lineage_overrides(df_lv, ln, lc, cal, ra.ApplyLog())
    assert bool(out_lv[(out_lv.oldId == 1) & (out_lv.newId == 2)].linkVerified.iloc[0]) is True     # set_ratio with linkVerified true flips the flag
    # lead decision (X5 handover): set_ratio alone sets chainable True (before set_chainable false, L-1 made 1->2 chainable)
    out_only = ra.apply_lineage_overrides(df.iloc[:1], ln, lc, cal, ra.ApplyLog())
    assert bool(out_only[(out_only.oldId == 1) & (out_only.newId == 2)].chainable.iloc[0]) is True
    assert int(r23.chainGapTradingDays) == 0            # new firstTradeDate 2020-05-20 <= old lastTradeDate 2020-12-31 → gap 0
    # ledger_build.build_lineage_rows: rc2f_estimate → estimated, rc2f_override → ok (옛 id 2 가 전환 뒤 거래를 계속하면 unresolved 이므로 lastTradeDate 를 전환 전날로)
    calo = calendar_obj(pd.bdate_range("2020-01-02", "2021-12-31"))
    lc_cut = lc.copy(); lc_cut.at[2, "lastTradeDate"] = T("2020-05-19")
    rows = lb.build_lineage_rows(out, calo, lc_cut.reset_index())
    assert rows[(rows.tradingItemId == 1)].status.iloc[0] == "ok" and rows[(rows.tradingItemId == 1)].parentFactor.iloc[0] == 2.0
    assert rows[(rows.tradingItemId == 2)].status.iloc[0] == "estimated" and bool(rows[(rows.tradingItemId == 2)].isEstimate.iloc[0])


# --------------------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------------------
def events_table() -> pd.DataFrame:
    cols = ["tradingItemId", "endDate", "settlementDate", "actionCode", "settlementReturn", "suggestedReturn", "cashPerShare", "successorTradingItemId", "successorRatio",
            "acquirerName", "source", "confidence", "actionText", "matchMethod", "endDateSource", "endDateConflict", "dbEndDateReason", "endDateOriginal", "endDateGapDays",
            "reviewFlag", "endDateBasis", "acquirerPriceDate", "hasCashLeg", "hasStockLeg", "cacheUnitStatus", "overrideSource", "cashPerShareCache", "successorRatioCache",
            "cacheUnitFactorTarget", "cacheUnitFactorSuccessor", "targetCacheMark", "targetCacheMarkDate", "cacheUnitFactorTargetDate", "successorCacheClose",
            "successorCacheCloseDate", "settlementReturnScope"]
    row = ra._events_row_defaults(cols)
    row.update(tradingItemId=2, endDate=T("2020-12-31"), endDateOriginal=T("2020-12-31"), settlementDate=T("2021-01-04"), actionCode="LIQ", source="co_acthist",
               endDateSource="db", hasCashLeg=False, hasStockLeg=False, settlementReturn=0.0)
    ev = pd.DataFrame([row])[cols]
    return ra._events_types(ev)


def dates_table() -> pd.DataFrame:
    from master_aux import DATES_COLS
    d = {c: None for c in DATES_COLS}
    d.update(tradingItemId=2, lastTradeDate=T("2020-12-31"), lastTradeSource="ciq", effectiveDate=T("2020-12-31"), effectiveDateSource="ciqsecurity.securityenddate",
             settlementDate=T("2021-01-04"), settlementDateSource="co_acthist.datadate", payDate=pd.NaT, payDateSource="", securityEndDate=pd.NaT, ciqLastCloseDate=T("2020-12-31"),
             miLastCloseDate=T("2020-12-31"), eventEndDate=T("2020-12-31"), actionCode="LIQ", gapEffectiveMinusLastTrade=0, gapSettlementMinusLastTrade=4, mismatchClass="match", everTop500=False)
    return pd.DataFrame([d])[DATES_COLS]


def test_lifecycle_overrides(work=None):
    work = work or workdir()
    cal_idx = pd.bdate_range("2020-01-02", "2020-12-31")
    src = work / "base_lc.parquet"
    base = base_data(src, cal_idx)
    cal = calendar_obj(cal_idx)
    closes = lb.RawCloses(src)
    lc = lifecycle_table().set_index("tradingItemId")
    ev, dates, ln = events_table(), dates_table().set_index("tradingItemId"), lineage_table()
    common = dict(caseId="c", evidence="e", x_isEstimate=False)
    df = pd.DataFrame([
        dict(overrideId="C-1", x_tid=2, lastTradeDate="2020-11-30", effectiveDate="2020-12-15", settlementDate="2020-12-16", payDate="", endReason="cash_merger",
             successorTradingItemId="", successorRatio="", cashPerShare="50.0", treatment="settle_cash", **common),
        dict(overrideId="C-2", x_tid=1, lastTradeDate="2020-10-30", effectiveDate="", settlementDate="", payDate="", endReason="stock_merger",
             successorTradingItemId="3", successorRatio="2.0", cashPerShare="", treatment="convert_successor", **common),
        dict(overrideId="C-3", x_tid=3, lastTradeDate="2020-12-15", effectiveDate="", settlementDate="", payDate="", endReason="halt_suspension",
             successorTradingItemId="", successorRatio="", cashPerShare="", treatment="unresolved", **common),
    ])
    alog = ra.ApplyLog()
    res = ra.apply_lifecycle_overrides(df, lc, None, dates, ev, ev.copy(), ln, closes, cal, alog, set())
    lg = alog.frame().set_index("overrideId")
    assert (lg.status == "applied").all(), lg
    lc2 = res["lifecycle"].set_index("tradingItemId")
    assert lc2.at[2, "lastTradeDate"] == T("2020-11-30") and lc2.at[2, "lastTradeSource"] == "rc2f_override" and lc2.at[2, "endReason"] == "merger"
    assert lc2.at[1, "lastTradeDate"] == T("2020-10-30") and lc2.at[1, "status"] == "confirmed"
    assert lc2.at[3, "lastTradeDate"] == T("2020-12-15") and lc2.at[3, "endReason"] == "delist_other" and lc2.at[3, "status"] == "active"
    e2 = res["events_reclass"].set_index("tradingItemId")
    # settle_cash: endDate = lastTradeDate, endDateOriginal = effectiveDate, cash 50 → R = 50/mark − 1 (mark = 11-30 close)
    mark = float(base[(base.tradingItemId == 2) & (base.date == T("2020-11-30"))].close.iloc[0])
    r2 = e2.loc[2]
    assert r2.endDate == T("2020-11-30") and r2.endDateOriginal == T("2020-12-15") and r2.settlementDate == T("2020-12-16") and r2.endDateSource == "override"
    assert r2.hasCashLeg and not r2.hasStockLeg and r2.cashPerShare == 50.0 and r2.cashPerShareCache == 50.0 and abs(r2.settlementReturn - (50.0 / mark - 1)) < 1e-9
    assert r2.targetCacheMark == np.float32(mark) and r2.targetCacheMarkDate == T("2020-11-30") and r2.cacheUnitStatus == "ok" and r2.actionCode == "ACQ"
    # convert_successor: 정산 bar = 11-02(10-30 다음 거래일), successor close 3 @11-02, R = 2 × succ/mark − 1; events 행 신설; lineage merger 링크 추가
    r1 = e2.loc[1]
    mark1 = float(base[(base.tradingItemId == 1) & (base.date == T("2020-10-30"))].close.iloc[0])
    succ = float(base[(base.tradingItemId == 3) & (base.date == T("2020-11-02"))].close.iloc[0])
    assert r1.endDate == T("2020-10-30") and r1.settlementDate == T("2020-11-02") and r1.hasStockLeg and not r1.hasCashLeg and int(r1.successorTradingItemId) == 3
    assert r1.successorRatio == 2.0 and r1.successorRatioCache == 2.0 and r1.successorCacheClose == np.float32(succ) and r1.successorCacheCloseDate == T("2020-11-02")
    assert abs(r1.settlementReturn - (2.0 * np.float32(succ) / np.float32(mark1) - 1)) < 1e-6 and r1.cacheUnitStatus == "ok"
    ln2 = res["lineage"]
    m = ln2[(ln2.oldId == 1) & (ln2.newId == 3)]
    assert len(m) == 1 and m.relation.iloc[0] == "merger" and m.ratio.iloc[0] == 2.0 and m.effectiveDate.iloc[0] == T("2020-10-30")
    # unresolved (lead mapping): events 행 생성/유지, leg 없음, settlementReturn NaN + reviewFlag rc2f_unresolved; lifecycle_dates settlementDate NaT
    e3 = e2.loc[3]
    assert np.isnan(e3.settlementReturn) and not e3.hasCashLeg and not e3.hasStockLeg and "rc2f_unresolved" in e3.reviewFlag and e3.endDate == T("2020-12-15")
    d2 = res["lifecycle_dates"].set_index("tradingItemId")
    assert 3 in d2.index and pd.isna(d2.at[3, "settlementDate"])
    assert d2.at[2, "lastTradeDate"] == T("2020-11-30") and d2.at[2, "effectiveDate"] == T("2020-12-15") and d2.at[2, "settlementDate"] == T("2020-12-16") and d2.at[2, "payDate"] == T("2020-12-16")
    assert d2.at[2, "lastTradeSource"] == "rc2f_override:C-1" and d2.at[2, "eventEndDate"] == T("2020-11-30") and d2.at[2, "mismatchClass"] == "match"
    assert 1 in d2.index and d2.at[1, "settlementDate"] == T("2020-11-02") and d2.at[1, "actionCode"] == "ACQ"
    # last_close / unresolved on an existing events row
    df2 = pd.DataFrame([dict(overrideId="C-4", x_tid=2, lastTradeDate="", effectiveDate="", settlementDate="", payDate="", endReason="liquidation",
                             successorTradingItemId="", successorRatio="", cashPerShare="", treatment="last_close", **common),
                        dict(overrideId="C-5", x_tid=2, lastTradeDate="", effectiveDate="", settlementDate="", payDate="", endReason="halt_suspension",
                             successorTradingItemId="", successorRatio="", cashPerShare="", treatment="unresolved", **common)])
    # R-X4 re-review: a successor without a ratio must not reach ratio x close (TypeError) — the applier rejects it explicitly (validate rejects it first)
    df_nr = pd.DataFrame([dict(overrideId="C-6", x_tid=2, lastTradeDate="2020-12-15", effectiveDate="", settlementDate="", payDate="", endReason="stock_merger",
                               successorTradingItemId="3", successorRatio="", cashPerShare="", treatment="extend_prices", **common)])
    alog_nr = ra.ApplyLog()
    r6 = ra.apply_lifecycle_overrides(df_nr, lc, None, dates, ev, ev.copy(), ln, closes, cal, alog_nr, set())
    lg6 = alog_nr.frame().set_index("overrideId")
    assert lg6.loc["C-6", "status"] == "rejected" and lg6.loc["C-6", "reason"].startswith("successor_without_ratio"), lg6
    assert r6["lifecycle"].set_index("tradingItemId").at[2, "lastTradeDate"] == T("2020-12-31")      # rejected before touching the lifecycle row
    try:
        ra._patch_events(ev.copy(), 2, dict(lastTradeDate=T("2020-12-15"), effectiveDate=None, settlementDate=None, payDate=None, endReason="stock_merger", successor=3,
                                            successorRatio=None, cashPerShare=None, treatment="convert_successor", evidence="e", lifecycleLast=T("2020-12-31")), closes, cal, "C-6")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "without a positive successorRatio" in str(e)
    r4 = ra.apply_lifecycle_overrides(df2.iloc[:1], lc, None, dates, ev, ev.copy(), ln, closes, cal, ra.ApplyLog(), set())
    e4 = r4["events_reclass"].set_index("tradingItemId").loc[2]
    assert np.isnan(e4.settlementReturn) and not e4.hasCashLeg and not e4.hasStockLeg and e4.cacheUnitStatus == "last_close" and e4.endDate == T("2020-12-31")   # last_close: NaN -> engine 0, status last_close (confirmed, not transitional)
    r5 = ra.apply_lifecycle_overrides(df2.iloc[1:], lc, None, dates, ev, ev.copy(), ln, closes, cal, ra.ApplyLog(), set())
    e5 = r5["events_reclass"].set_index("tradingItemId").loc[2]
    assert np.isnan(e5.settlementReturn) and "rc2f_unresolved" in e5.reviewFlag and not e5.hasCashLeg and pd.isna(r5["lifecycle_dates"].set_index("tradingItemId").at[2, "settlementDate"])


# --------------------------------------------------------------------------------------
# ledger CSV overrides (ledger_repairs)
# --------------------------------------------------------------------------------------
def _frame(rows, extra=()):
    base_cols = [c for c in lb.LEDGER_COLUMNS if c != "eventId"] + list(extra)
    out = []
    for r in rows:
        d = lb._row(**{k: v for k, v in r.items() if k in lb.LEDGER_COLUMNS})
        for k in extra:
            d[k] = r.get(k, np.nan)
        out.append(d)
    return pd.DataFrame(out, columns=base_cols)


def test_ledger_csv_overrides_pre_post(work=None):
    work = work or workdir()
    stock = _frame([dict(tradingItemId=1, exDate=T("2020-04-15"), eventType="spin_off", legType="stock", childTradingItemId=2, childRatio=1.0, source="distributions", status="unresolved", notes="")],
                   extra=("_cashEquiv", "_impliedFrac", "_leg"))
    cash = _frame([dict(tradingItemId=1, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, cashCurrency="USD", fxToUSD=1.0, source="ciqdividend", status="ok", notes=""),
                   dict(tradingItemId=1, exDate=T("2020-08-03"), eventType="cash_dividend", legType="cash", cashPerShare=0.3, cashCurrency="USD", fxToUSD=1.0, source="vendor_implied",
                        childValueSource="vendor_implied", status="estimated", isEstimate=True, notes="vendor_only_step")])
    split = _frame([dict(tradingItemId=1, exDate=T("2020-03-02"), eventType="split", legType="quantity", parentFactor=2.0, source="ciqsplitcache", status="unresolved", notes="factor_step_without_price_move")])
    merger = _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg"))
    linrows = _frame([dict(tradingItemId=1, exDate=T("2020-06-01"), eventType="relist", legType="quantity", parentFactor=1.0, childTradingItemId=2, source="lineage", status="estimated", isEstimate=True, notes="")])
    # 참조 원장(eventId 해석용)
    ref = lb.finalize([cash, split, stock, linrows]); ref.attrs = {}
    ref_p = work / "ref_ledger.parquet"; lb.write_ledger(ref, ref_p)
    eid = {(r.eventType, str(r.exDate.date())): r.eventId for r in ref.itertuples(index=False)}
    csv = work / "ledger_overrides_t.csv"
    rows = [dict(overrideId="G-1", caseId="c1", tradingItemId=1, eventId=eid[("spin_off", "2020-04-15")], action="set", set=json.dumps({"childRatio": 0.5, "childTradingItemId": 3}), isEstimate="false", evidence="e", source="s"),
            dict(overrideId="G-2", caseId="c2", tradingItemId=1, eventId=eid[("cash_dividend", "2020-02-03")], action="set", set=json.dumps({"cashPerShare": 1.25}), isEstimate="false", evidence="e", source="s"),
            dict(overrideId="G-3", caseId="c3", tradingItemId=1, eventId=eid[("cash_dividend", "2020-08-03")], action="set", set=json.dumps({"status": "unresolved"}), isEstimate="false", evidence="e", source="s"),
            dict(overrideId="G-4", caseId="c4", tradingItemId=1, eventId=eid[("split", "2020-03-02")], action="set", set=json.dumps({"status": "ok"}), isEstimate="false", evidence="e", source="s"),
            dict(overrideId="G-5", caseId="c5", tradingItemId=1, eventId="", match_tradingItemId=1, match_exDate="2020-06-01", match_legType="quantity", action="drop", set="", isEstimate="false", evidence="e", source="s"),
            dict(overrideId="G-6", caseId="c6", tradingItemId=1, eventId="", action="add", set=json.dumps({"eventType": "special_dividend", "legType": "cash", "exDate": "2020-09-01", "cashPerShare": 2.0}), isEstimate="true", evidence="e", source="s"),
            dict(overrideId="G-7", caseId="c7", tradingItemId=1, eventId="1_20200101_cash_dividend_9", action="set", set=json.dumps({"cashPerShare": 9}), isEstimate="false", evidence="e", source="s"),
            dict(overrideId="G-8", caseId="c8", tradingItemId=1, eventId="", match_tradingItemId=1, match_exDate="2020-02-03", match_legType="cash", action="set",
                 set=json.dumps({"status": "estimated"}), isEstimate="true", evidence="e", source="s")]
    pd.DataFrame(rows).to_csv(csv, index=False)
    ovr, rejected = rep.load_csv_overrides([csv], ref_p)
    assert [r["overrideId"] for r in rejected] == ["G-7"] and len(ovr) == 7
    assert {o["overrideId"]: o["frame"] for o in ovr} == {"G-1": "stock", "G-2": "cash", "G-3": "gaps", "G-4": "split", "G-5": "lineage", "G-6": "cash", "G-8": "cash"}
    # L1 final: a ledger set/drop whose split boundary was moved by a same-run split move_date is re-keyed to the new date (split_boundary_moves.csv)
    ovr_m, _ = rep.load_csv_overrides([csv], ref_p, split_moves={(1, T("2020-03-02")): (T("2020-03-09"), "S-9")})
    g4 = [o for o in ovr_m if o["overrideId"] == "G-4"][0]
    assert g4["exDate"] == T("2020-03-09") and g4["notes_redirect"].startswith("redirected_by_split_move_date:S-9:2020-03-02->2020-03-09")
    assert [o for o in ovr_m if o["overrideId"] == "G-2"][0]["exDate"] == T("2020-02-03")          # cash legs untouched
    mv = work / "moves.csv"; pd.DataFrame([{"tradingItemId": 1, "exDate": "2020-03-02", "newExDate": "2020-03-09", "overrideId": "S-9"}]).to_csv(mv, index=False)
    assert rep.load_split_moves(mv) == {(1, T("2020-03-02")): (T("2020-03-09"), "S-9")} and rep.load_split_moves(None) == {}
    assert rep.csv_override_ids(ovr) == {1, 2, 3}          # 부모 1, 대상 행의 자회사 2, 새 자회사 3
    results = {}
    fr, reps = rep.apply_csv_overrides({"stock": stock, "merger": merger, "cash": cash, "split": split, "lineage": linrows}, ovr, "pre", results)
    st, ca, sp, li = fr["stock"], fr["cash"], fr["split"], fr["lineage"]
    assert st.childRatio.iloc[0] == 0.5 and int(st.childTradingItemId.iloc[0]) == 3 and st.source.iloc[0] == "manual_override" and "csv_override:G-1" in st.repairOf.iloc[0]
    assert ca[ca.exDate == T("2020-02-03")].cashPerShare.iloc[0] == 1.25 and len(li) == 0 and (ca.exDate == T("2020-09-01")).any()
    added = ca[ca.exDate == T("2020-09-01")].iloc[0]
    assert added.status == "estimated" and bool(added.isEstimate) and added.cashPerShare == 2.0 and added.fxToUSD == 1.0
    assert results["G-5"]["status"] == "applied" and results["G-6"]["status"] == "applied"
    # 평가 단계가 status/값을 덮어쓴 것을 가정(value_children 흉내): G-1 행 ok/child_close_ex 값, G-4 split 은 그대로 unresolved
    st.loc[:, "status"] = "ok"; st.loc[:, "childValueSource"] = "child_close_ex"; st.loc[:, "childValuePerOldShare"] = 4.0
    gaps = _frame([dict(tradingItemId=1, exDate=T("2020-11-02"), eventType="other", legType="cash", source="vendor_implied", status="unresolved", notes="")])
    fr2, reps2 = rep.apply_csv_overrides({"cash": ca, "split": sp, "stock": st, "merger_stock": merger, "merger_cash": merger, "lineage": li, "gaps": gaps}, ovr, "post", results)
    ca2, sp2, st2 = fr2["cash"], fr2["split"], fr2["stock"]
    r3 = ca2[ca2.exDate == T("2020-08-03")].iloc[0]
    assert r3.status == "unresolved" and np.isnan(r3.cashPerShare) and not r3.isEstimate           # unresolved → 값 NaN
    assert sp2.status.iloc[0] == "ok" and not sp2.isEstimate.iloc[0]                                 # G-4 post 재확정
    assert st2.status.iloc[0] == "ok" and st2.childValuePerOldShare.iloc[0] == 4.0                    # G-1 은 값 키를 안 줬으므로 평가값 유지
    r8 = ca2[ca2.exDate == T("2020-02-03")].iloc[0]
    assert r8.status == "estimated" and bool(r8.isEstimate) and r8.cashPerShare == 1.25             # G-8: 같은 행에 status(estimated) — G-2 와 별개 교정(다른 키)
    assert all(results[o]["status"] == "applied" for o in ("G-1", "G-2", "G-3", "G-4", "G-5", "G-6", "G-8")), results
    assert all(r["source"] == "csv_override" for r in reps + reps2) and any(r["field"] == "row" and r["after"] == "dropped" for r in reps)
    # finalize: 태그가 repairOf 에 남고 eventId 로 찾을 수 있음
    led = lb.finalize([ca2, sp2, st2, fr2["lineage"], fr2["gaps"]])
    tagged = led[led.repairOf.fillna("").str.contains("csv_override:G-1")]
    assert len(tagged) == 1 and tagged.eventType.iloc[0] == "spin_off"
    # L1 3rd build (X0 fix): a CSV set carrying a ROUNDED childValuePerOldShare (X2-266: 10.192779 vs the builder's 0.239943 x 42.48 = 10.19277864)
    # must not overwrite the builder's exact child_close_ex value (returns oracle at 1e-9): within 1e-4 relative the builder value stays (check ok),
    # beyond it the CSV value is adopted and the conflict logged
    exact = 0.239943 * 42.48
    base_s = dict(caseId="c", csvIsEstimate=False, evidence="e", source="s", targetEventId="", match_child=2638398, frame="stock", eventType="spin_off", tradingItemId=1, exDate=T("2020-04-15"), legType="stock",
                  targetSource="distributions", targetStatus="ok")
    st_v = _frame([dict(tradingItemId=1, exDate=T("2020-04-15"), eventType="spin_off", legType="stock", childTradingItemId=2638398, childRatio=0.239943, childValuePerOldShare=exact,
                        childValueSource="child_close_ex", source="distributions", status="ok", notes="")], extra=("_cashEquiv", "_impliedFrac", "_leg"))
    ovr_v = [dict(base_s, overrideId="V-1", action="set", set={"childRatio": 0.239943, "childValuePerOldShare": round(exact, 6), "childValueSource": "child_close_ex", "status": "ok", "isEstimate": False})]
    res_v = {}
    fr_pre, _ = rep.apply_csv_overrides({"stock": st_v, "merger": _frame([]), "cash": _frame([]), "split": _frame([]), "lineage": _frame([])}, ovr_v, "pre", res_v)
    st_pre = fr_pre["stock"]; st_pre.loc[:, "childValuePerOldShare"] = exact; st_pre.loc[:, "childValueSource"] = "child_close_ex"; st_pre.loc[:, "status"] = "ok"      # value_children re-valued
    fr_post, reps_v = rep.apply_csv_overrides({"cash": _frame([]), "split": _frame([]), "stock": st_pre, "merger_stock": _frame([]), "merger_cash": _frame([]), "lineage": _frame([]), "gaps": _frame([])}, ovr_v, "post", res_v)
    assert fr_post["stock"].childValuePerOldShare.iloc[0] == exact and "csv_value_check_ok" in res_v["V-1"]["reason"] and not any(r["field"] == "childValuePerOldShare" for r in reps_v)
    # beyond the tolerance the CSV value wins (+ conflict logged)
    ovr_c = [dict(ovr_v[0], overrideId="V-2", set=dict(ovr_v[0]["set"], childValuePerOldShare=11.0))]
    res_c = {}
    fr_pre2, _ = rep.apply_csv_overrides({"stock": st_v.copy(), "merger": _frame([]), "cash": _frame([]), "split": _frame([]), "lineage": _frame([])}, ovr_c, "pre", res_c)
    st_pre2 = fr_pre2["stock"]; st_pre2.loc[:, "childValuePerOldShare"] = exact; st_pre2.loc[:, "childValueSource"] = "child_close_ex"; st_pre2.loc[:, "status"] = "ok"
    fr_post2, reps_c = rep.apply_csv_overrides({"cash": _frame([]), "split": _frame([]), "stock": st_pre2, "merger_stock": _frame([]), "merger_cash": _frame([]), "lineage": _frame([]), "gaps": _frame([])}, ovr_c, "post", res_c)
    assert fr_post2["stock"].childValuePerOldShare.iloc[0] == 11.0 and "csv_value_conflict" in res_c["V-2"]["reason"] and any(r["field"] == "childValuePerOldShare" and "conflicts" in r["evidence"] for r in reps_c)
    # a CSV value with childValueSource event_value (not price-based) is re-asserted as before
    ovr_e = [dict(ovr_v[0], overrideId="V-3", set={"childValuePerOldShare": 9.5, "childValueSource": "event_value", "status": "estimated"})]
    res_e = {}
    fr_pre3, _ = rep.apply_csv_overrides({"stock": st_v.copy(), "merger": _frame([]), "cash": _frame([]), "split": _frame([]), "lineage": _frame([])}, ovr_e, "pre", res_e)
    st_pre3 = fr_pre3["stock"]; st_pre3.loc[:, "childValuePerOldShare"] = exact; st_pre3.loc[:, "childValueSource"] = "child_close_ex"; st_pre3.loc[:, "status"] = "ok"
    fr_post3, _ = rep.apply_csv_overrides({"cash": _frame([]), "split": _frame([]), "stock": st_pre3, "merger_stock": _frame([]), "merger_cash": _frame([]), "lineage": _frame([]), "gaps": _frame([])}, ovr_e, "post", res_e)
    assert fr_post3["stock"].childValuePerOldShare.iloc[0] == 9.5 and fr_post3["stock"].childValueSource.iloc[0] == "event_value"


# --------------------------------------------------------------------------------------
# validator (Ref 를 합성 데이터로 대체)
# --------------------------------------------------------------------------------------
def synth_ref(work: Path) -> ra.Ref:
    ref = ra.Ref.__new__(ra.Ref)
    ref.rc2 = work
    ref._cache = {}
    ref.pending_aux_ids = set(); ref.pending_lineage_ratios = {}; ref.pending_price_ids = set()
    cal = pd.bdate_range("2020-01-02", "2021-12-31")
    ref._cache["calendar"] = calendar_obj(cal)
    lc = lifecycle_table().set_index("tradingItemId")
    ref._cache["lifecycle"] = lc
    ref._cache["ids"] = {1, 2, 3}
    ref._cache["price_range"] = pd.DataFrame({"first": lc.firstPriceDate, "last": lc.ciqLastCloseDate})
    stock = _frame([dict(tradingItemId=1, exDate=T("2020-04-15"), eventType="spin_off", legType="stock", childTradingItemId=2, childRatio=1.0, source="distributions", status="unresolved", notes="")], extra=("_cashEquiv", "_impliedFrac", "_leg"))
    cash = _frame([dict(tradingItemId=1, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, source="ciqdividend", status="ok", notes="")])
    split = _frame([dict(tradingItemId=1, exDate=T("2020-03-02"), eventType="split", legType="quantity", parentFactor=2.0, source="ciqsplitcache", status="ok", notes=""),
                    dict(tradingItemId=1, exDate=T("2020-06-01"), eventType="split", legType="quantity", parentFactor=0.5, source="ciqsplitcache", status="ok", notes="")])
    copy = _frame([dict(tradingItemId=2, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, source="stitched_copy", status="ok", notes="", keeperEventId="1_20200203_cash_dividend_0", exportExclude=True)])
    led = lb.finalize([cash, split, stock, copy]); led.attrs = {}
    ref._cache["ledger"] = led
    ref._cache["split"] = split_table()
    b = lb.split_boundaries(split_table()); al, _ = ref.calendar.align(b.validFrom.to_numpy()); b["exDate"] = al
    ref._cache["split_boundaries"] = b
    ref._cache["lineage"] = lineage_table()
    ref._cache["lifecycle_dates"] = dates_table().set_index("tradingItemId")
    return ref


def test_validator_synthetic(work=None):
    work = work or workdir()
    ref = synth_ref(work)
    base_data(work / "base_v.parquet", pd.bdate_range("2020-01-02", "2020-12-31"))       # Y2a: continuous synthetic prices for the validator's raw-jump check
    ref._cache["closes"] = lb.RawCloses(work / "base_v.parquet"); ref._cache["closes_ids"] = {1, 2, 3}
    d = work / "v"; d.mkdir(exist_ok=True)
    common = dict(caseId="1", evidence="ev", source="s", isEstimate="false", owner="T")
    pd.DataFrame([dict(overrideId="V-1", tradingItemId=1, exDate="2020-06-01", action="drop", newFactor="", newExDate="", checkBasis="", **common),          # ok (raw continuous at the dropped boundary -> pass)
                  dict(overrideId="V-2", tradingItemId=1, exDate="2020-06-01", action="set_factor", newFactor="0.25", newExDate="", checkBasis="", **common),  # dup target
                  dict(overrideId="V-3", tradingItemId=1, exDate="2020-04-01", action="move_date", newFactor="", newExDate="2020-04-08", checkBasis="", **common),  # not a boundary
                  dict(overrideId="V-4", tradingItemId=9, exDate="2020-03-02", action="confirm", newFactor="", newExDate="", checkBasis="", **common),          # bad id
                  dict(overrideId="V-5", tradingItemId=3, exDate="2020-09-01", action="add", newFactor="2", newExDate="", checkBasis="document", **common),   # raw check fails (no price step) but a document basis -> warning only
                  dict(overrideId="V-20", tradingItemId=3, exDate="2020-10-01", action="add", newFactor="2", newExDate="", checkBasis="", **common),          # raw check fails, no basis -> error
                  dict(overrideId="V-21", tradingItemId=3, exDate="2020-11-02", action="add", newFactor="2", newExDate="", checkBasis="raw_jump", **common),  # raw check fails, basis raw_jump -> error
                  dict(overrideId="V-22", tradingItemId=3, exDate="2020-12-01", action="add", newFactor="2", newExDate="", checkBasis="bogus", **common),     # invalid checkBasis -> error
                  dict(overrideId="V-23", tradingItemId=1, exDate="2020-10-01", action="add", newFactor="2", newExDate="", checkBasis="", **common)]).to_csv(d / "split_overrides.csv", index=False)  # id 1 has a price override in this run -> check deferred to apply (warning)
    pd.DataFrame([dict(overrideId="V-6", tradingItemId=1, dateFrom="2020-03-02", dateTo="2020-03-04", op="multiply", value="0.5", fields="close", **common),    # ok
                  dict(overrideId="V-7", tradingItemId=1, dateFrom="2020-03-04", dateTo="2020-03-10", op="multiply", value="2", fields="close,open", **common),  # overlap
                  dict(overrideId="V-8", tradingItemId=2, dateFrom="2022-01-03", dateTo="2022-01-05", op="set_close", value="1", fields="", **common)]).to_csv(d / "price_overrides.csv", index=False)  # out of range
    pd.DataFrame([dict(overrideId="V-9", tradingItemId=1, eventId="1_20200415_spin_off_0", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="set", set=json.dumps({"childRatio": 0.5}), **common),  # ok
                  dict(overrideId="V-10", tradingItemId=2, eventId="2_20200203_cash_dividend_0", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="drop", set="", **common),  # stitched copy
                  dict(overrideId="V-11", tradingItemId=1, eventId="", match_tradingItemId="1", match_exDate="2020-02-03", match_legType="cash", match_childTradingItemId="", action="set", set='{"cashPerShare": "x"}', **common),  # bad value
                  dict(overrideId="V-12", tradingItemId=1, eventId="", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="add", set=json.dumps({"eventType": "cash_dividend", "legType": "cash", "exDate": "2020-02-03", "cashPerShare": 1.0}), **common),  # key exists
                  dict(overrideId="V-13", tradingItemId=1, eventId="", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="add", set=json.dumps({"eventType": "spin_off", "legType": "cash", "exDate": "2020-10-01", "cashPerShare": 1.0}), **common),  # type/leg mismatch
                  dict(overrideId="V-17", tradingItemId=1, eventId="1_20200601_split_0", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="set", set=json.dumps({"exDate": "2020-03-02"}), **common),  # R-X2: set exDate collides with the 03-02 split row
                  dict(overrideId="V-18", tradingItemId=2, eventId="", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="add", set=json.dumps({"eventType": "special_dividend", "legType": "cash", "exDate": "2020-12-01", "cashPerShare": 10.0}), **common)]).to_csv(d / "ledger_overrides.csv", index=False)  # ok itself; same cash as V-14 settle_cash one day later -> V-14 double count
    pd.DataFrame([dict(overrideId="V-14", tradingItemId=2, lastTradeDate="2020-11-30", effectiveDate="", settlementDate="2020-12-02", payDate="", endReason="cash_merger", successorTradingItemId="", successorRatio="", cashPerShare="10", treatment="settle_cash", **common),  # ok
                  dict(overrideId="V-15", tradingItemId=1, lastTradeDate="2020-11-28", effectiveDate="", settlementDate="", payDate="", endReason="stock_merger", successorTradingItemId="3", successorRatio="1", cashPerShare="", treatment="convert_successor", **common),  # saturday + no date
                  dict(overrideId="V-19", tradingItemId=3, lastTradeDate="2020-12-15", effectiveDate="", settlementDate="", payDate="", endReason="stock_merger", successorTradingItemId="2", successorRatio="", cashPerShare="", treatment="extend_prices", **common)]).to_csv(d / "lifecycle_overrides.csv", index=False)  # R-X4 re-review: successor without ratio
    pd.DataFrame([dict(overrideId="V-16", tradingItemId=1, oldId="1", newId="2", action="set_ratio", ratio="2", effectiveDate="", relation="", chainable="", linkVerified="", **common),   # ok
                  dict(overrideId="V-16", tradingItemId=1, oldId="1", newId="2", action="add", ratio="2", effectiveDate="2020-06-01", relation="relist", chainable="", linkVerified="", **common),  # dup id + exists
                  dict(overrideId="V-24", tradingItemId=1, oldId="1", newId="2", action="set_chainable", ratio="", effectiveDate="", relation="", chainable="true", linkVerified="true", **dict(common, evidence="")),   # rc2f-E: verified without evidence -> error
                  dict(overrideId="V-25", tradingItemId=1, oldId="1", newId="2", action="set_chainable", ratio="", effectiveDate="", relation="", chainable="true", linkVerified="maybe", **common)]).to_csv(d / "lineage_overrides.csv", index=False)  # bad bool -> error
    summary, iss = ra.validate_files(sorted(str(p) for p in d.glob("*.csv")), ref)
    errs = {(r["overrideId"], r["message"]) for r in iss.rows if r["level"] == "ERROR"}
    bad_ids = {o for o, _ in errs}
    assert bad_ids == {"V-2", "V-3", "V-4", "V-7", "V-8", "V-11", "V-12", "V-13", "V-14", "V-15", "V-16", "V-17", "V-19", "V-20", "V-21", "V-22", "V-24", "V-25"}, sorted(errs)
    assert any("linkVerified true needs evidence" in m for o, m in errs if o == "V-24") and any(m.startswith("linkVerified:") for o, m in errs if o == "V-25")
    assert any("without successorRatio" in m and "extend_prices" in m for o, m in errs if o == "V-19")
    assert not {"V-1", "V-5", "V-6", "V-9", "V-10", "V-18"} & bad_ids
    # Y2a checkBasis rule: fail + empty/raw_jump basis -> error, fail + document -> warning, invalid enum -> error; the pass case (V-1) is silent
    assert any("raw price check FAILS" in m and "checkBasis is 'empty'" in m for o, m in errs if o == "V-20")
    assert any("raw price check FAILS" in m and "checkBasis is 'raw_jump'" in m for o, m in errs if o == "V-21")
    assert any("checkBasis 'bogus' not in" in m for o, m in errs if o == "V-22")
    warns_all = {(r["overrideId"], r["message"]) for r in iss.rows if r["level"] == "WARN"}
    assert any("raw price check fails" in m and "checkBasis=document" in m for o, m in warns_all if o == "V-5")
    assert not any("raw price check" in m for o, m in list(errs) + list(warns_all) if o == "V-1")
    assert "V-23" not in bad_ids and any("deferred to apply" in m for o, m in warns_all if o == "V-23")        # same-run price override on id 1
    assert any("double count" in m and "V-18" in m for o, m in errs if o == "V-14")          # X3 follow-up: ledger csv dividend cash vs lifecycle settlement cash
    warns = {(r["overrideId"], r["message"]) for r in iss.rows if r["level"] == "WARN"}
    assert any("stitched_copy" in m and "keeper" in m for o, m in warns if o == "V-10") and any("already has" in m for o, m in errs if o == "V-12")
    assert any("collides with existing" in m for o, m in errs if o == "V-17")
    assert any("not a trading day" in m for o, m in errs if o == "V-15") and any("not unique" in m for o, m in errs if o == "V-16")
    # residual settlement (settlement cash < csv dividend cash) is a warning, not an error
    d2 = work / "v2"; d2.mkdir(exist_ok=True)
    pd.DataFrame([dict(overrideId="W-1", tradingItemId=2, eventId="", match_tradingItemId="", match_exDate="", match_legType="", match_childTradingItemId="", action="add", set=json.dumps({"eventType": "special_dividend", "legType": "cash", "exDate": "2020-12-01", "cashPerShare": 10.0}), **common)]).to_csv(d2 / "ledger_overrides.csv", index=False)
    pd.DataFrame([dict(overrideId="W-2", tradingItemId=2, lastTradeDate="2020-11-30", effectiveDate="", settlementDate="2020-12-02", payDate="", endReason="cash_merger", successorTradingItemId="", successorRatio="", cashPerShare="0.5", treatment="settle_cash", **common)]).to_csv(d2 / "lifecycle_overrides.csv", index=False)
    ref2 = synth_ref(work)
    _, iss2 = ra.validate_files(sorted(str(p) for p in d2.glob("*.csv")), ref2)
    assert iss2.n_err == 0 and any(r["overrideId"] == "W-2" and "residual" in r["message"] for r in iss2.rows if r["level"] == "WARN"), iss2.rows


# --------------------------------------------------------------------------------------
# recount: 귀속 로직(합성)
# --------------------------------------------------------------------------------------
def test_recount_diff_and_attribution():
    import rc2f_recount as rc
    days = rc._days(pd.bdate_range("2020-01-02", "2020-01-31").to_numpy().astype("datetime64[ns]"))
    def ret(ids_days, vals, flags):
        ids = np.array([i for i, _ in ids_days]); dd = np.array([d for _, d in ids_days])
        k = rc._keys(ids, dd); o = np.argsort(k)
        return {"key": k[o], "ids": ids[o], "days": dd[o], "ret": np.array(vals, dtype=float)[o], "flag": np.array(flags, dtype=np.int8)[o]}
    cells = [(1, d) for d in days[:10]] + [(2, d) for d in days[:10]]
    old = ret(cells, [0.01] * 20, [0] * 20)
    vals = [0.01] * 20; vals[3] = 0.05; flags = [0] * 20; flags[12] = 4
    new_cells = cells[:-1]                                                        # (2, days[9]) 제거
    new = ret(new_cells, vals[:-1], flags[:-1])
    diff = rc.returns_diff(old, new)
    assert set(diff.kind) == {"value", "flag", "removed"} and len(diff) == 3
    obs = rc.ObsDates(old, new)
    assert obs.first_on_or_after(1, int(days[2]) + 1) == int(days[3]) and obs.first_day(2) == int(days[0]) and obs.first_on_or_after(1, int(days[9]) + 1) is None
    # 귀속: 가격 창(id 1, days[3]) + 원장 변경 행(id 2 exDate days[2] → days[2..3] 창) → days[2] 셀; 제거된 (2, days[9]) 는 lifecycle 창
    alog = pd.DataFrame([dict(overrideId="P-1", caseId="c", kind="price", action="multiply", status="applied", reason="", affected_rows=1, target="", file="", eventIds=""),
                         dict(overrideId="C-1", caseId="c", kind="lifecycle", action="last_close", status="applied", reason="", affected_rows=1, target="2", file="", eventIds=""),
                         dict(overrideId="G-1", caseId="c", kind="ledger", action="set", status="applied", reason="", affected_rows=1, target=f"2@{str(days[2].astype('datetime64[D]'))}/cash", file="", eventIds="")])
    d3 = str(days[3].astype("datetime64[D]"))
    overrides = {"price": pd.DataFrame([dict(overrideId="P-1", tradingItemId="1", dateFrom=d3, dateTo=d3)]),
                 "lifecycle": pd.DataFrame([dict(overrideId="C-1", tradingItemId="2", lastTradeDate=str(days[8].astype("datetime64[D]")), settlementDate="", payDate="", effectiveDate="", successorTradingItemId="")]),
                 "ledger": pd.DataFrame([dict(overrideId="G-1", tradingItemId="2", set="")])}
    empty_ledger = pd.DataFrame(columns=list(dict.fromkeys(["eventId", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "source", "keeperEventId", "repairOf"] + rc.LEDGER_CMP)))
    lineage = pd.DataFrame(columns=["oldId", "newId", "relation", "effectiveDate"])
    lc_old = pd.DataFrame({"tradingItemId": [1, 2], "lastTradeDate": [pd.NaT, pd.Timestamp(days[9].astype("datetime64[D]"))]})
    lc_new = pd.DataFrame({"tradingItemId": [1, 2], "lastTradeDate": [pd.NaT, pd.Timestamp(days[8].astype("datetime64[D]"))]})
    sc = rc.build_scope(alog, overrides, obs, empty_ledger, empty_ledger, lineage, lineage, lc_old, lc_new, None)
    ld = pd.DataFrame([dict(tradingItemId=2, exDate=pd.Timestamp(days[2].astype("datetime64[D]")), eventType="cash_dividend", legType="cash", childTradingItemId=-1, change="changed",
                            columns="cashPerShare", eventIdOld="x", eventIdNew="x", repairOfNew="csv_override:G-1", sourceNew="manual_override", sourceOld="ciqdividend", keeperNew="",
                            cashNew=1.0, ratioNew=float("nan"), statusOld="ok", statusNew="ok")])
    ld = rc.attribute_ledger_rows(ld, sc, empty_ledger, empty_ledger)
    # R-X0 P2-6: every matching attribution is kept; the primary follows the precedence (repairOf tag first)
    assert ld.attributedPrimary.iloc[0] == "G-1" and ld.how.iloc[0].split(";")[0] == "repairOf_tag" and "G-1" in ld.attributedTo.iloc[0].split(";") and ld.nAttributions.iloc[0] >= 1
    # R-X0 P1-5: the same-id window is the event window, not the whole history — a far-away row on id 2 is not attributed to G-1
    far = ld.copy(); far["repairOfNew"] = ""; far["exDate"] = pd.Timestamp(days[9].astype("datetime64[D]")) + pd.Timedelta(days=400); far["sourceNew"] = "ciqdividend"
    far = rc.attribute_ledger_rows(far, sc, empty_ledger, empty_ledger)
    assert "G-1" not in far.attributedTo.iloc[0].split(";"), far.attributedTo.iloc[0]
    near = ld.copy(); near["repairOfNew"] = ""; near["sourceNew"] = "ciqdividend"
    near = rc.attribute_ledger_rows(near, sc, empty_ledger, empty_ledger)
    assert "G-1" in near.attributedTo.iloc[0].split(";") and near.how.iloc[0].startswith("same_id_window:")
    out = rc.attribute_cells(diff, sc, ld, obs)
    got = {(int(r.tradingItemId), int(r.day)): r.attributedPrimary for r in out.itertuples(index=False)}
    assert got[(1, int(days[3]))] == "P-1" and got[(2, int(days[9]))] == "C-1" and got[(2, int(days[2]))] in ("G-1", "C-1"), got
    assert (out.nAttributions >= 1).all() and set(out.columns) >= {"attributedTo", "how", "attributedPrimary", "nAttributions"}
    tr = rc.flag_transition(diff, {0: 10, 1: 2, 4: 1}, {})
    assert tr.shape == (6, 6) and int(tr.to_numpy().sum()) == 13 + int((diff.flagOld < 0).sum())
    # R-X0 2nd review P1-6: an applied_no_effect lineage override (chainGap > 5) still changes the ledger relist row -> it must own a scope
    alog2 = pd.DataFrame([dict(overrideId="N-1", caseId="c", kind="lineage", action="set_ratio", status="applied_no_effect", reason="not_linked(chain_gap 9 > 5)", affected_rows=1, target="", file="", eventIds="")])
    ov2 = {"lineage": pd.DataFrame([dict(overrideId="N-1", oldId="1", newId="2", ratio="2.0", effectiveDate=str(days[2].astype("datetime64[D]")))])}
    lin2 = pd.DataFrame({"oldId": [1], "newId": [2], "relation": ["relist"], "effectiveDate": [pd.Timestamp(days[2].astype("datetime64[D]"))]})
    sc2 = rc.build_scope(alog2, ov2, obs, empty_ledger, empty_ledger, lin2, lin2, lc_old, lc_new, None)
    ld2 = pd.DataFrame([dict(tradingItemId=1, exDate=pd.Timestamp(days[2].astype("datetime64[D]")), eventType="relist", legType="stock", childTradingItemId=2, change="changed",
                             columns="status,isEstimate", eventIdOld="r", eventIdNew="r", repairOfNew="", sourceNew="lineage", sourceOld="lineage", keeperNew="",
                             cashNew=float("nan"), ratioNew=2.0, statusOld="estimated", statusNew="ok", notesNew="")])
    ld2 = rc.attribute_ledger_rows(ld2, sc2, empty_ledger, empty_ledger, rules=False)
    assert ld2.attributedPrimary.iloc[0] == "N-1" and ld2.how.iloc[0].startswith("same_id_window:lineage"), ld2.iloc[0].to_dict()
    # C5b P1: value override creating a new row (1, days[12]) -> added cell + next obs (1, days[13]) prev change, all attributed
    vold = ret([(1, d) for d in days[10:15] if d != days[12]], [0.01] * 4, [0] * 4)
    vnew = ret([(1, d) for d in days[10:15]], [0.01, 0.01, 0.02, 0.03, 0.01], [0] * 5)
    vdiff = rc.returns_diff(vold, vnew)
    assert "added" in set(vdiff.kind) and len(vdiff) == 2, vdiff
    vobs = rc.ObsDates(vold, vnew)
    alog3 = pd.DataFrame([dict(overrideId="V-1", caseId="c", kind="value", action="set_close", status="applied", reason="", affected_rows=1, target="", file="", eventIds="")])
    ov3 = {"value": pd.DataFrame([dict(overrideId="V-1", tradingItemId="1", date=str(days[12].astype("datetime64[D]")), field="close", value="5")])}
    sc3 = rc.build_scope(alog3, ov3, vobs, empty_ledger, empty_ledger, lineage, lineage, lc_old, lc_old, None)
    va = rc.attribute_cells(vdiff, sc3, ld.iloc[:0], vobs)
    assert (va.attributedPrimary == "V-1").all() and (va.attributedTo != "").all(), va
    # 귀속 불가 셀: 창 밖 id 1 days[7]
    stray = ret([(1, int(days[7]))], [0.2], [0])
    d2 = rc.returns_diff(ret([(1, int(days[7]))], [0.1], [0]), stray)
    assert rc.attribute_cells(d2, sc, ld, obs).attributedTo.iloc[0] == ""


# --------------------------------------------------------------------------------------
# 실데이터 게이트(빈 교정 → rc2 와 비트 동일). RC2F_REAL_GATE=1 일 때만(≈2 분).
# --------------------------------------------------------------------------------------
def _real_gate_enabled() -> bool:
    return os.environ.get("RC2F_REAL_GATE") == "1" and (ra.RC2_DEFAULT / "manifest.json").exists()


def test_real_empty_override_bit_identical(work=None):
    work = work or workdir()
    import hashlib
    if not _real_gate_enabled():
        if pytest is not None:
            pytest.skip("set RC2F_REAL_GATE=1 (needs SP/build_rc2_final)")
        print("  (skipped: set RC2F_REAL_GATE=1)")
        return
    # R-X0 P2-5: the build (~3 GB) goes to the large-disk gate dir, not the scratchpad (/ has ~4 GB free)
    out = Path(os.environ.get("RC2F_GATE_DIR", "/home/jgo/longshort/rc2f_x0/builds")) / "build_empty_gate"
    out.parent.mkdir(parents=True, exist_ok=True)
    empty = work / "empty"; empty.mkdir(exist_ok=True)
    cmd = [PY, str(HERE.parent / "rc2f_apply.py"), "apply", "--overrides-glob", str(empty / "*.csv"), "--out", str(out), "--force"]
    assert subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    pub = [PY, str(HERE.parent / "publish.py"), "--stage", str(ra.STAGE_V21_DEFAULT), "--t2", str(out / "T2"), "--rc2-stage", str(out), "--orch", str(ra.SP / "orch_v21"),
           "--orch-rc2", str(ra.SP / "orch_rc2"), "--out", str(out / "cache"), "--build-id", "rc2f-empty-gate", "--no-proto-tables"]
    r = subprocess.run(pub, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 22), b""):
                h.update(b)
        return h.hexdigest()
    same = {}
    for rel in ("derived/returns_total.parquet", "factors/split.parquet", "events/corporate_actions.parquet", "master/lifecycle_dates.parquet",
                "master/lineage.parquet", "master/lifecycle.parquet", "base_data.parquet", "engine/ca_ledger_cache.parquet"):
        same[rel] = sha(ra.RC2_DEFAULT / rel) == sha(out / "cache" / rel)
    assert all(same.values()), same
    # events/ca_ledger.parquet: D1 (2026-09-21) fixed the ciqsplit tie-break (smallest splitid when two vendor split rows share an ex-date) -> one
    # attributed evidence cell differs from the rc2 file (5853213_20040805_split_0: splitId 1923773 -> 308663); values, keys and every other cell are identical
    import ledger_incremental as li
    A, B = pd.read_parquet(ra.RC2_DEFAULT / "events" / "ca_ledger.parquet"), pd.read_parquet(out / "cache" / "events" / "ca_ledger.parquet")
    rev, summ = li.diff_ledgers(A, B, None, [], "2026-09-15", "2026-09-15")
    allowed = {("5853213_20040805_split_0", "evidence")}
    assert summ["added"] == 0 and summ["removed"] == 0 and set(zip(rev.eventId, rev.field)) <= allowed, rev[["eventId", "field", "before", "after"]].head(10).to_string()
    assert rev.source.isin(["rule:ciqsplit_tie_break_smallest_splitid"]).all(), rev.source.unique()




# --------------------------------------------------------------------------------------
# X3 인계: 적용기 idempotency, X3 (d) events 패치, 귀속 규칙(전환일·reassigned 복사본)
# --------------------------------------------------------------------------------------
def test_ledger_csv_idempotency():
    cash = _frame([dict(tradingItemId=1, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, cashCurrency="USD", fxToUSD=1.0, source="ciqdividend", status="ok", notes="")])
    base = dict(caseId="c", csvIsEstimate=False, evidence="e", source="s", targetEventId="", match_child=None, frame="cash", eventType="cash_dividend", tradingItemId=1, exDate=T("2020-02-03"), legType="cash")
    ovr = [dict(base, overrideId="I-1", action="add", set={"eventType": "cash_dividend", "legType": "cash", "exDate": T("2020-02-03"), "cashPerShare": 1.0}),          # same amount → no-op
           dict(base, overrideId="I-2", action="add", set={"eventType": "cash_dividend", "legType": "cash", "exDate": T("2020-02-03"), "cashPerShare": 1.5}),          # different amount → rejected
           dict(base, overrideId="I-3", action="set", set={"status": "unresolved"}, targetSource="vendor_implied", targetStatus="estimated"),                          # marker replaced by a real row → superseded
           dict(base, overrideId="I-4", action="set", set={"cashPerShare": 1.25}, targetSource="ciqdividend", targetStatus="ok")]                                    # same source → applied
    results = {}
    fr, reps = rep.apply_csv_overrides({"stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger": _frame([]), "cash": cash, "split": _frame([]), "lineage": _frame([])}, ovr, "pre", results)
    assert results["I-1"]["status"] == "applied" and results["I-1"]["reason"].startswith("duplicate_of_existing") and results["I-1"]["affected_rows"] == 0
    assert results["I-2"]["status"] == "rejected" and "different amount" in results["I-2"]["reason"]
    assert results["I-3"]["status"] == "applied" and results["I-3"]["reason"].startswith("superseded_by_source:ciqdividend")
    assert results["I-4"]["status"] == "applied" and fr["cash"].cashPerShare.iloc[0] == 1.25 and len(fr["cash"]) == 1
    fr2, _ = rep.apply_csv_overrides({"cash": fr["cash"], "split": _frame([]), "stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger_stock": _frame([]), "merger_cash": _frame([]), "lineage": _frame([]), "gaps": _frame([])}, ovr, "post", results)
    assert fr2["cash"].status.iloc[0] == "ok" and fr2["cash"].cashPerShare.iloc[0] == 1.25          # I-3 (superseded) never demotes the real row


def test_ledger_csv_reclassified_target(work=None):
    """X3 follow-up (MMCE 34438170 2009-10-09 형): rc2 원장에서 settlement-like 규칙이 merger_cash 로 재분류한 ciqdividend 행을 CSV 가
    special_dividend 로 되돌리면 ① pre 단계에서 superseded 로 오판하지 않고(원천 source 동일) ② reconcile_merger_cash_duplicates 가 csv_override 행을
    재분류하지 않으며 ③ 그래도 뒤 단계가 eventType 을 바꾸면 post 단계가 재확정한다."""
    work = work or workdir()
    cal_idx = pd.bdate_range("2020-01-02", "2020-12-31")
    src = work / "base_reclass.parquet"
    base_data(src, cal_idx)
    closes = lb.RawCloses(src)
    ex = T("2020-06-30")
    # regenerated (pre-valuation) frame: the row is still a ciqdividend cash row
    cash = _frame([dict(tradingItemId=2, exDate=ex, eventType="special_dividend", legType="cash", cashPerShare=200.0, cashCurrency="USD", fxToUSD=1.0, source="ciqdividend", status="ok", notes="")])
    lc = pd.DataFrame({"tradingItemId": [2], "firstTradeDate": [T("2020-01-02")], "lastTradeDate": [ex], "endReason": ["bankruptcy"], "status": ["confirmed"]})
    # the reference (rc2) row was reclassified: source corporate_actions, notes settlement_like_reclassified -> targetOrigin ciqdividend
    ref_row = pd.Series({"source": "corporate_actions", "notes": ";settlement_like_reclassified(cash 200 >= 0.9 x P_prev 160, ...)"})
    assert rep._origin_source(ref_row) == "ciqdividend"
    o = dict(overrideId="RC-1", caseId="c", action="set", csvIsEstimate=False, evidence="8-K liquidating distribution", source="s", targetEventId="2_20200630_merger_cash_0",
             match_child=None, frame="merger", eventType="merger_cash", tradingItemId=2, exDate=ex, legType="cash",
             set={"eventType": "special_dividend", "legType": "cash", "cashPerShare": 200.0, "status": "ok", "isEstimate": False},
             targetSource="corporate_actions", targetStatus="ok", targetOrigin=rep._origin_source(ref_row))
    frames = {"stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "cash": cash, "split": _frame([]), "lineage": _frame([])}
    # control: without the origin the same override is (wrongly) superseded by the regenerated ciqdividend row
    o2 = dict(o, overrideId="RC-0", targetOrigin=None)
    r2 = {}
    rep.apply_csv_overrides({k: v.copy() for k, v in frames.items()}, [o2], "pre", r2)
    assert r2["RC-0"]["reason"].startswith("superseded_by_source:ciqdividend")
    results = {}
    fr, _ = rep.apply_csv_overrides(frames, [o], "pre", results)
    assert results["RC-1"]["status"] == "applied" and not results["RC-1"].get("noop") and "superseded" not in results["RC-1"]["reason"], results["RC-1"]
    assert "csv_override:RC-1" in str(fr["cash"].repairOf.iloc[0])
    # (2) the settlement-like rule (cash 200 >= 0.9 x P_prev, lastTradeDate == exDate, bankruptcy) leaves the csv-overridden row alone
    cash2, merger2, dropped, repairs = lb.reconcile_merger_cash_duplicates(fr["cash"], fr["merger"], lc, closes)
    assert cash2.eventType.iloc[0] == "special_dividend" and not repairs and not dropped
    untagged = fr["cash"].copy(); untagged["repairOf"] = None
    cash3, _, _, repairs3 = lb.reconcile_merger_cash_duplicates(untagged, fr["merger"], lc, closes)
    assert cash3.eventType.iloc[0] == "merger_cash" and len(repairs3) == 1          # control: the rule still fires on an untagged row
    # (3) post phase re-asserts a drifted eventType
    drifted = cash2.copy(); drifted["eventType"] = "merger_cash"
    fr3, reps3 = rep.apply_csv_overrides({"cash": drifted, "split": _frame([]), "stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger_stock": _frame([]), "merger_cash": _frame([]), "lineage": _frame([]), "gaps": _frame([])}, [o], "post", results)
    assert fr3["cash"].eventType.iloc[0] == "special_dividend" and "post_reasserted(eventType)" in results["RC-1"]["reason"] and "csv_eventType_reasserted" in fr3["cash"].notes.iloc[0]
    assert any(r["field"] == "eventType" and r["after"] == "special_dividend" for r in reps3)


def test_l1_csv_add_vs_stitched_copy_and_absent_drop():
    """L1 통합 수정 (1): CSV add 가 stitched_copy 와 같은 키·금액이면 CSV 행을 버리고 no-op(duplicate_of_existing) 로 로그; 금액이 다르면 둘 다 남기고 conflict 표시.
    (2): 대상이 귀속 규칙으로 이동/제거된 drop 은 rejected(not_found) 가 아니라 no_op(already_moved_or_removed / target_absent)."""
    led = _frame([dict(tradingItemId=2, exDate=T("2001-03-20"), eventType="cash_dividend", legType="cash", cashPerShare=0.0319, source="manual_override", status="ok", repairOf="csv_override:A-1", notes="mechanismA"),
                  dict(tradingItemId=2, exDate=T("2001-03-20"), eventType="cash_dividend", legType="cash", cashPerShare=0.0319, source="stitched_copy", status="ok", keeperEventId="1_20010320_cash_dividend_0", exportExclude=True, notes="history_reassigned_copy:1"),
                  dict(tradingItemId=2, exDate=T("2002-03-04"), eventType="cash_dividend", legType="cash", cashPerShare=0.05, source="manual_override", status="ok", repairOf="csv_override:A-2", notes=""),
                  dict(tradingItemId=2, exDate=T("2002-03-04"), eventType="cash_dividend", legType="cash", cashPerShare=0.04, source="stitched_copy", status="ok", keeperEventId="1_20020304_cash_dividend_0", exportExclude=True, notes="history_reassigned_copy:1"),
                  dict(tradingItemId=2, exDate=T("2003-03-04"), eventType="cash_dividend", legType="cash", cashPerShare=0.06, source="manual_override", status="ok", repairOf="csv_override:A-3", notes="")])
    led["eventId"] = ["2_20010320_cash_dividend_0", "2_20010320_cash_dividend_1", "2_20020304_cash_dividend_0", "2_20020304_cash_dividend_1", "2_20030304_cash_dividend_0"]
    csv_ovr = [dict(overrideId=o, action="add") for o in ("A-1", "A-2", "A-3")]
    results = {o: dict(overrideId=o, status="applied", reason="added", affected_rows=1) for o in ("A-1", "A-2", "A-3")}
    out, recs = lb.drop_csv_adds_duplicating_copies(led, csv_ovr, results)
    assert list(out.eventId) == ["2_20010320_cash_dividend_1", "2_20020304_cash_dividend_0", "2_20020304_cash_dividend_1", "2_20030304_cash_dividend_0"]
    assert results["A-1"]["status"] == "applied" and results["A-1"].get("noop") and results["A-1"]["affected_rows"] == 0 \
        and results["A-1"]["reason"].startswith("duplicate_of_existing(stitched_copy 2_20010320_cash_dividend_1") and results["A-1"]["eventIds_override"] == "2_20010320_cash_dividend_1"
    assert results["A-2"]["status"] == "applied" and "conflict_with_stitched_copy" in results["A-2"]["reason"] and not results["A-2"].get("noop")
    assert results["A-3"]["reason"] == "added" and len(recs) == 1 and recs[0]["reason"] == "csv_add_duplicate_of_stitched_copy:2_20010320_cash_dividend_1"
    # (2) absent drop target
    base = dict(caseId="c", csvIsEstimate=False, evidence="e", source="s", targetEventId="1_20040220_cash_dividend_0", match_child=None, frame="cash", eventType="cash_dividend",
                tradingItemId=1, exDate=T("2004-02-20"), legType="cash", set={}, targetSource="ciqdividend", targetStatus="ok")
    ovr = [dict(base, overrideId="D-1", action="drop"), dict(base, overrideId="D-2", action="drop", exDate=T("2004-05-20"), targetEventId="1_20040520_cash_dividend_0")]
    frames = {"cash": _frame([]), "split": _frame([]), "stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger_stock": _frame([]), "merger_cash": _frame([]), "lineage": _frame([]), "gaps": _frame([])}
    dropped = [dict(tradingItemId=1, exDate=T("2004-02-20"), eventType="cash_dividend", legType="cash", childKey=-1, cashPerShare=0.1, reason="history_appended_to_successor:2")]
    res = {}
    rep.apply_csv_overrides({"cash": _frame([]), "stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger": _frame([]), "split": _frame([]), "lineage": _frame([])}, ovr, "pre", res)
    assert res["D-1"]["status"] == "rejected" and res["D-2"]["status"] == "rejected"
    _, reps = rep.apply_csv_overrides(frames, ovr, "post", res, dropped=dropped)
    assert res["D-1"]["status"] == "applied" and res["D-1"].get("noop") and res["D-1"]["reason"] == "no_op(already_moved_or_removed: history_appended_to_successor:2)"
    assert res["D-2"]["status"] == "applied" and res["D-2"]["reason"] == "no_op(target_absent_in_rebuild)" and sum(r["after"] == "no-op(absent)" for r in reps) == 2
    # control: a set with an absent target stays rejected ... unless its intended result row already exists (X3 marker replacement whose marker the
    # rebuild no longer creates while the stitched copy carries the real row) -> no-op
    ovr_s = [dict(base, overrideId="S-1", action="set", set={"cashPerShare": 0.2}),
             dict(base, overrideId="S-2", action="set", exDate=T("2009-01-23"), targetEventId="1_20090123_cash_dividend_0", targetSource="vendor_implied",
                  set={"exDate": T("2009-01-14"), "cashPerShare": 0.1025, "status": "ok"})]
    res_s = {}
    rep.apply_csv_overrides({"cash": _frame([]), "stock": _frame([], extra=("_cashEquiv", "_impliedFrac", "_leg")), "merger": _frame([]), "split": _frame([]), "lineage": _frame([])}, ovr_s, "pre", res_s)
    frames_s = dict(frames); frames_s["cash"] = _frame([dict(tradingItemId=1, exDate=T("2009-01-14"), eventType="cash_dividend", legType="cash", cashPerShare=0.1025, source="stitched_copy", status="ok", notes="")])
    rep.apply_csv_overrides(frames_s, ovr_s, "post", res_s, dropped=dropped)
    assert res_s["S-1"]["status"] == "rejected" and res_s["S-1"]["reason"] == "not_found(pre+post)"
    assert res_s["S-2"]["status"] == "applied" and res_s["S-2"].get("noop") and res_s["S-2"]["reason"].startswith("no_op(target_absent; result_row_exists: cash:1@2009-01-14/cash cash 0.1025")


def test_stitched_copies_map_tagged_keeper():
    """R-X0 P1-1: keeper 행의 금액(또는 자회사)이 CSV 교정으로 바뀌면 드롭 기록(옛 값)과 값으로 매칭되지 않음 → csv_override 태그 keeper 로 매칭해
    복사본이 keeper 의 새 값으로 재생성된다(태그 없으면 고아 → keeperEventId 빈 값)."""
    dropped = pd.DataFrame([dict(tradingItemId=1, exDate=T("2014-06-02"), eventType="cash_dividend", legType="cash", childKey=-1, cashPerShare=0.5, childRatio=np.nan,
                                 source="ciqdividend", status="ok", reason="history_appended_to_successor:2", notes="", evidence="e"),
                            dict(tradingItemId=1, exDate=T("2014-09-02"), eventType="spin_off", legType="stock", childKey=9, cashPerShare=np.nan, childRatio=0.5,
                                 source="distributions", status="ok", reason="history_appended_to_successor:2", notes="", evidence="e")])
    keeper = _frame([dict(tradingItemId=2, exDate=T("2014-06-02"), eventType="cash_dividend", legType="cash", cashPerShare=1.0, source="manual_override", status="ok", repairOf="csv_override:K-1", notes="csv_override"),
                     dict(tradingItemId=2, exDate=T("2014-09-02"), eventType="spin_off", legType="stock", childTradingItemId=9, childRatio=0.25, source="manual_override", status="ok", repairOf="csv_override:K-2", notes="csv_override")])
    keeper["eventId"] = ["2_20140602_cash_dividend_0", "2_20140902_spin_off_0"]
    m = lb.stitched_copies_map(dropped, keeper)
    assert list(m.keeperId) == [2, 2] and list(m.keeperEventId) == ["2_20140602_cash_dividend_0", "2_20140902_spin_off_0"]
    copies = lb.make_stitched_copies(keeper, m)
    assert len(copies) == 2 and list(copies.tradingItemId) == [1, 1] and copies.cashPerShare.iloc[0] == 1.0 and int(copies.childTradingItemId.iloc[1]) == 9 \
        and set(copies.source) == {"stitched_copy"} and copies.exportExclude.all() and list(copies.keeperEventId) == list(m.keeperEventId)
    untagged = keeper.copy(); untagged["repairOf"] = None
    m0 = lb.stitched_copies_map(dropped, untagged)
    assert list(m0.keeperEventId) == ["", "2_20140902_spin_off_0"]        # control: cash amount changed without a tag -> orphan; stock leg (same child, ratio changed) still matches by child id
    # R-X0 2nd review P2-13: the CSV moved the keeper's exDate (06-02 -> 06-04): the copy dropped under the old date follows the move
    moved = keeper.copy(); moved.loc[moved.index[0], "exDate"] = T("2014-06-04"); moved.loc[moved.index[0], "eventId"] = "2_20140604_cash_dividend_0"
    m1 = lb.stitched_copies_map(dropped, moved, {(2, T("2014-06-02"), "cash"): (T("2014-06-04"), "cash")})
    assert list(m1.keeperEventId) == ["2_20140604_cash_dividend_0", "2_20140902_spin_off_0"] and list(m1.keeperMissingReason) == ["", ""]
    c1 = lb.make_stitched_copies(moved, m1)
    assert c1.exDate.iloc[0] == T("2014-06-04") and int(c1.tradingItemId.iloc[0]) == 1
    assert list(lb.stitched_copies_map(dropped, moved).keeperEventId) == ["", "2_20140902_spin_off_0"]      # without the move map -> orphan
    # L1 fix 5: the keeper re-keyed to another legType/eventType by the CSV (AET form: spin_off stock -> other cash, 12-15 -> 12-14) is not a valid
    # keeper for a stock copy; the map records keeperMissingReason so ledger_qa Q9b treats it as a deliberate change
    rek = keeper.copy(); rek.loc[rek.index[1], ["exDate", "legType", "eventType", "childTradingItemId", "childRatio", "cashPerShare"]] = [T("2014-09-01"), "cash", "other", None, np.nan, 35.0]
    rek.loc[rek.index[1], "eventId"] = "2_20140901_other_0"
    m2 = lb.stitched_copies_map(dropped, rek, {(2, T("2014-09-02"), "stock"): (T("2014-09-01"), "cash")})
    assert m2.keeperEventId.iloc[1] == "2_20140901_other_0" and m2.keeperMissingReason.iloc[1] == ""       # tagged keeper found at the new key -> copy regenerated as the keeper's new row
    rek2 = rek.copy(); rek2.loc[rek2.index[1], "repairOf"] = None
    m3 = lb.stitched_copies_map(dropped, rek2, {(2, T("2014-09-02"), "stock"): (T("2014-09-01"), "cash")})
    assert m3.keeperEventId.iloc[1] == "" and m3.keeperMissingReason.iloc[1] == "csv_rekeyed_keeper:2014-09-01/cash"


def test_attribution_transition_and_reassigned_copy():
    """X3 (c): 승계 id 가 exDate 전에 이미 거래 중이면 승계 id 의 사건이 유지되고 옛 id 복사본은 드롭(rc2 규칙은 옛 id 에 유지);
    X3 (a): 이어붙인 이력의 행을 tradable 이웃으로 옮기면 원래 id 에 복사 요청(history_reassigned_copy) 기록."""
    lc = pd.DataFrame({"tradingItemId": [1, 2, 3, 4], "firstTradeDate": [T("2010-01-04"), T("2014-12-16"), T("2015-01-02"), T("2010-01-04")],
                       "lastTradeDate": [T("2014-12-15"), pd.NaT, pd.NaT, T("2014-12-31")], "status": ["confirmed", "active", "active", "confirmed"]})
    ln = pd.DataFrame({"oldId": [1, 4], "newId": [2, 3], "relation": ["relist", "rename"], "effectiveDate": [T("2014-12-16"), T("2015-01-02")], "ratio": [1.0, 1.0],
                       "evidence": ["e", "e"], "confidence": ["high", "high"]})
    rows = _frame([dict(tradingItemId=1, exDate=T("2014-12-17"), eventType="cash_dividend", legType="cash", cashPerShare=0.82, source="ciqdividend", status="ok", notes=""),   # CCI: old id copy
                   dict(tradingItemId=2, exDate=T("2014-12-17"), eventType="cash_dividend", legType="cash", cashPerShare=0.82, source="ciqdividend", status="ok", notes=""),   # successor's own row
                   dict(tradingItemId=3, exDate=T("2014-06-02"), eventType="cash_dividend", legType="cash", cashPerShare=0.5, source="ciqdividend", status="ok", notes="")],   # appended history on 3 (4 tradable, no row)
                  extra=("_cashEquiv", "_impliedFrac", "_leg"))
    out, dropped = lb.attribute_to_trading_id(rows, lc, ln, None, rc2_rules=False)
    kept = out[out.exDate == T("2014-12-17")]
    assert list(kept.tradingItemId) == [2] and any(d["reason"].startswith("history_appended_to_successor:2") for d in dropped)
    # R-X0 final check P1-8: the X3-c holder rule is CASH-only. A holder STOCK leg (both ids carry the record, successor traded before ex) follows the
    # rc2 transition rule: the old id keeps it (valued with the old id's prices), the successor's copy is dropped -> nothing is exported on the successor
    # (a successor-keyed value leg is unresolved with the successor's already-ex prices and the engine would book it to the successor's holders: CBS/GGP)
    rows_s = _frame([dict(tradingItemId=1, exDate=T("2014-12-17"), eventType="spin_off", legType="stock", childTradingItemId=9, childRatio=0.5, source="distributions", status="ok", notes=""),
                     dict(tradingItemId=2, exDate=T("2014-12-17"), eventType="spin_off", legType="stock", childTradingItemId=9, childRatio=0.5, source="distributions", status="ok", notes="")],
                    extra=("_cashEquiv", "_impliedFrac", "_leg"))
    for rules in (False, True):
        out_s2, dropped_s2 = lb.attribute_to_trading_id(rows_s, lc, ln, None, rc2_rules=rules)
        assert list(out_s2.tradingItemId) == [1] and "transition_day_leg" in out_s2.notes.iloc[0] and "_priceId" not in out_s2.columns \
            and any(d["reason"] == "successor_copy_of_transition_leg:kept_on_old_id_1" and d["tradingItemId"] == 2 for d in dropped_s2), (rules, out_s2.notes.iloc[0], dropped_s2)
    # ... while the holder CASH leg (CCI form, `kept` above) stays with the successor under the new rules (rc2 rules: old id, checked below)
    assert list(kept.tradingItemId) == [2]
    # R-X0 2nd review P0-2: reassign branch (successor traded before ex but has NO record of the event) keeps the transition-window leg on the
    # old id exactly as rc2 does — moving it would add the child value to the successor's own row whose previous (when-issued) close is already ex
    rows_r = _frame([dict(tradingItemId=1, exDate=T("2014-12-17"), eventType="spin_off", legType="stock", childTradingItemId=9, childRatio=0.5,
                          childValuePerOldShare=4.0, childValueSource="child_close_ex", source="distributions", status="ok", notes="")], extra=("_cashEquiv", "_impliedFrac", "_leg"))
    for rules in (False, True):
        out_r, dropped_r = lb.attribute_to_trading_id(rows_r, lc, ln, None, rc2_rules=rules)
        assert list(out_r.tradingItemId) == [1] and "transition_day_leg" in out_r.notes.iloc[0] and not dropped_r, (rules, out_r.notes.iloc[0], dropped_r)
    # returns check: successor 2 prints (when-issued, ex prices) 12-16..12-18, old id 1 last trade 12-15; leg kept on id 1 -> the successor's own
    # 12-17 row is a plain price return; had the leg been moved to id 2 the same row would carry +4.0 (double count)
    import prices_returns as prm
    import test_returns_v2 as trv
    ids, dates, closes = [1, 1, 2, 2, 2], ["2014-12-12", "2014-12-15", "2014-12-16", "2014-12-17", "2014-12-18"], [24.0, 24.0, 20.0, 20.5, 21.0]
    lin = [{"oldId": 1, "newId": 2, "relation": "relist", "effectiveDate": "2014-12-16", "ratio": 1.0, "chainable": True, "chainGapTradingDays": 0}]
    lc_r = pd.DataFrame({"tradingItemId": [1, 2], "firstTradeDate": [T("2014-12-12"), T("2014-12-16")], "lastTradeDate": [T("2014-12-15"), pd.NaT], "status": ["confirmed", "active"]})

    def returns_with(ledger_rows):
        led = prm.Ledger(trv.finalize_ledger(pd.DataFrame(ledger_rows)), lc_r)
        led.use_lineage(trv.lineage_frame(lin))
        with tempfile.TemporaryDirectory() as tmp:
            bp = Path(tmp) / "base_data.parquet"
            pd.DataFrame({"tradingItemId": np.array(ids, dtype=np.int64), "date": pd.to_datetime(dates), "close": np.array(closes, dtype=np.float32)}).to_parquet(bp, index=False)
            links = prm.resolve_links(led, bp, lc_r)
        df, st = prm.returns_for_group_v2(np.array(ids, dtype=np.int64), pd.to_datetime(dates).to_numpy().astype("datetime64[ns]"), np.array(closes, dtype=np.float32), led, links)
        return df
    leg = {"tradingItemId": 1, "exDate": "2014-12-17", "eventType": "spin_off", "legType": "stock", "childTradingItemId": 9, "childRatio": 0.5, "childValuePerOldShare": 4.0, "source": "distributions"}
    df_ok = returns_with([leg])
    assert abs(df_ok.ret[3] - (20.5 / 20.0 - 1.0)) < 1e-6 and df_ok.retFlag[3] == 0, df_ok.to_string()          # successor's own 12-17 row untouched
    df_bad = returns_with([dict(leg, tradingItemId=2)])                                                          # what the withdrawn move produced
    assert abs(df_bad.ret[3] - ((20.5 + 4.0) / 20.0 - 1.0)) < 1e-6 and df_bad.ret[3] > df_ok.ret[3] + 0.15
    moved = out[out.exDate == T("2014-06-02")]
    assert list(moved.tradingItemId) == [4] and "reassigned_from=3" in moved.notes.iloc[0]
    assert any(d["reason"] == "history_reassigned_copy:4" and d["tradingItemId"] == 3 for d in dropped)
    out_l, dropped_l = lb.attribute_to_trading_id(rows, lc, ln, None, rc2_rules=True)
    kept_l = out_l[out_l.exDate == T("2014-12-17")]
    assert list(kept_l.tradingItemId) == [1] and "transition_day_leg" in kept_l.notes.iloc[0] and any(d["reason"].startswith("successor_copy_of_transition_leg") for d in dropped_l)
    assert not any(d["reason"].startswith("history_reassigned_copy") for d in dropped_l)
    # same-day transition (IAC→MTCH form: successor first day == exDate) still stays on the old id under both rules
    lc2 = lc.copy(); lc2.loc[lc2.tradingItemId == 2, "firstTradeDate"] = T("2014-12-17")
    out_s, dropped_s = lb.attribute_to_trading_id(rows.iloc[:2], lc2, ln, None, rc2_rules=False)
    assert list(out_s.tradingItemId) == [1] and "transition_day_leg" in out_s.notes.iloc[0]
    # when-issued successor (VIA.B→CBS.B form): successor first trade before the old id's last trade, ex = next trading day after the old last -> stays on the old id
    cal = calendar_obj(pd.bdate_range("2010-01-04", "2015-12-31"))
    lc3 = lc.copy(); lc3.loc[lc3.tradingItemId == 2, "firstTradeDate"] = T("2014-12-08"); lc3.loc[lc3.tradingItemId == 1, "lastTradeDate"] = T("2014-12-16")
    rows3 = rows.iloc[:2].copy(); rows3["exDate"] = T("2014-12-17")
    out_w, dropped_w = lb.attribute_to_trading_id(rows3, lc3, ln, None, rc2_rules=False, cal=cal)
    assert list(out_w.tradingItemId) == [1] and "transition_day_leg" in out_w.notes.iloc[0] and any(d["reason"].startswith("successor_copy_of_transition_leg") for d in dropped_w)


def test_vendor_gap_marker_window():
    """X3 (b): (prevDate, date] 거래일 창 — 희소 종목의 실제 exDate(prevDate 와 date 사이) 를 ±7 달력일 창이 놓치는 경우."""
    cal = calendar_obj(pd.bdate_range("2020-01-02", "2020-12-31"))
    dv = pd.DataFrame({"tradingItemId": [1], "exDate": [T("2020-03-05")], "divAmount": [0.2], "dividendTypeId": [1], "isCash": [True], "isStock": [False]})
    lc = lifecycle_table()
    d = workdir() / "gap"; d.mkdir(exist_ok=True)
    # cell: prevDate 2020-02-20, date 2020-03-20 (sparse), vendor step implies 0.2; ciqdividend exDate 03-05 is 15 days before date (outside ±7) but inside (prevDate, date]
    pd.DataFrame({"tradingItemId": [1], "date": ["2020-03-20"], "prevDate": ["2020-02-20"], "vendorImpliedD": [0.2]}).to_csv(d / "cand.csv", index=False)
    g_new = lb.vendor_gap_markers(d / "cand.csv", dv, cal, lc, None, None, None, rc2_rules=False)
    g_old = lb.vendor_gap_markers(d / "cand.csv", dv, cal, lc, None, None, None, rc2_rules=True)
    assert len(g_new) == 0 and g_new.attrs["counts"]["with_source"] == 1
    assert len(g_old) == 1 and g_old.status.iloc[0] == "unresolved"          # rc2: marker created (source missed)
    # L1 fix 4: a candidate without prevDate (older lists) gets the cell's interval from raw closes — id 1 has no prints 03-04..03-19 (sparse),
    # the vendor step shows on 03-20 while ciqdividend ex is 03-05 (15 days): with closes the source is found, without it a marker is created
    src = d / "base_sparse.parquet"
    b = base_data(src, pd.bdate_range("2020-01-02", "2020-12-31"))
    b = b[~((b.tradingItemId == 1) & (b.date >= T("2020-03-04")) & (b.date < T("2020-03-20")))]        # last print before the step: 03-03
    b.to_parquet(src, index=False)
    pd.DataFrame({"tradingItemId": [1], "date": ["2020-03-20"], "vendorImpliedD": [0.2]}).to_csv(d / "cand_noprev.csv", index=False)
    g_fill = lb.vendor_gap_markers(d / "cand_noprev.csv", dv, cal, lc, None, None, None, rc2_rules=False, closes=lb.RawCloses(src))
    assert len(g_fill) == 0 and g_fill.attrs["counts"]["with_source"] == 1 and str(g_fill.attrs["candidates"].prevDate.iloc[0])[:10] == "2020-03-03" \
        and g_fill.attrs["candidates"].prevDateSource.iloc[0] == "raw_closes"
    g_nofill = lb.vendor_gap_markers(d / "cand_noprev.csv", dv, cal, lc, None, None, None, rc2_rules=False)
    assert len(g_nofill) == 1




def test_extend_prices_terms_and_scope(work=None):
    """R-X4 §4-3: extend_prices 행이 조건(successor/cash)을 실으면 새 lastTradeDate 에 2단계 적용; §4-2: 정산 bar 뒤 7일 안의 value leg 는 정산 bar 로 정렬;
    (d) settlementReturnScope 라벨(정산 bar 의 원장 value leg → events_legs_only); §4-9 검증기 successorRatio vs lineage 비율 충돌."""
    work = work or workdir()
    cal_idx = pd.bdate_range("2020-01-02", "2020-12-31")
    src = work / "base_ext.parquet"
    base = base_data(src, cal_idx)
    cal = calendar_obj(cal_idx)
    closes = lb.RawCloses(src)
    lc = lifecycle_table().set_index("tradingItemId")
    lc.at[2, "lastTradeDate"] = T("2020-10-30")
    ev, dates, ln = events_table(), dates_table().set_index("tradingItemId"), lineage_table()
    ev.loc[ev.index[0], ["endDate", "settlementDate"]] = [T("2020-10-30"), T("2020-11-02")]
    common = dict(caseId="c", evidence="e", x_isEstimate=False)
    df = pd.DataFrame([dict(overrideId="E-1", x_tid=2, lastTradeDate="2020-11-30", effectiveDate="", settlementDate="", payDate="", endReason="stock_merger",
                            successorTradingItemId="3", successorRatio="2.0", cashPerShare="", treatment="extend_prices", **common)])
    alog = ra.ApplyLog()
    res = ra.apply_lifecycle_overrides(df, lc, None, dates, ev, ev.copy(), ln, closes, cal, alog, set(), cal_days=cal_idx.to_numpy().astype("datetime64[ns]"), lineage_touched=set())
    e2 = res["events_reclass"].set_index("tradingItemId").loc[2]
    mark = float(base[(base.tradingItemId == 2) & (base.date == T("2020-11-30"))].close.iloc[0]); succ = float(base[(base.tradingItemId == 3) & (base.date == T("2020-12-01"))].close.iloc[0])
    assert e2.endDate == T("2020-11-30") and e2.settlementDate == T("2020-12-01") and e2.hasStockLeg and int(e2.successorTradingItemId) == 3 and e2.successorRatio == 2.0
    assert abs(e2.settlementReturn - (2.0 * np.float32(succ) / np.float32(mark) - 1)) < 1e-6 and "extend_prices->convert_successor" in e2.reviewFlag
    m = res["lineage"]; m = m[(m.oldId == 2) & (m.newId == 3)]
    assert len(m) == 1 and m.ratio.iloc[0] == 2.0 and m.relation.iloc[0] == "merger"
    assert res["lifecycle"].set_index("tradingItemId").at[2, "lastTradeDate"] == T("2020-11-30") and alog.frame().status.tolist() == ["applied"]
    # chainGap recompute: a relist link 2->3 with gap > 5 becomes chainable when the extended lastTradeDate closes the gap (not touched by lineage overrides)
    ln2 = ln.copy(); ln2.loc[len(ln2)] = dict(oldId=2, newId=3, relation="relist", effectiveDate=T("2020-12-01"), ratio=1.0, evidence="e", confidence="high", ratioSource="",
                                              linkClass="relist", reviewFlag="chain_gap=99", sources="v1", version="v1-rc1", chainGapTradingDays=99, chainable=False)
    lc3 = lc.copy(); lc3.at[3, "firstTradeDate"] = T("2020-12-01")
    res2 = ra.apply_lifecycle_overrides(df.assign(successorTradingItemId="", successorRatio=""), lc3, None, dates, ev, ev.copy(), ln2, closes, cal, ra.ApplyLog(), set(),
                                        cal_days=cal_idx.to_numpy().astype("datetime64[ns]"), lineage_touched=set())
    r23 = res2["lineage"]; r23 = r23[(r23.oldId == 2) & (r23.newId == 3) & (r23.relation == "relist")].iloc[0]
    assert int(r23.chainGapTradingDays) == 0 and bool(r23.chainable) is True
    # §4-2: value leg on the old id dated 2 trading days after the settlement bar -> moved to the bar; 10 days after -> left
    lc4 = lifecycle_table(); lc4.loc[lc4.tradingItemId == 2, "lastTradeDate"] = T("2020-10-30"); lc4.loc[lc4.tradingItemId == 2, "status"] = "confirmed"
    stock = _frame([dict(tradingItemId=2, exDate=T("2020-11-04"), eventType="spin_off", legType="stock", childTradingItemId=3, childRatio=0.5, source="distributions", status="ok", notes=""),
                    dict(tradingItemId=2, exDate=T("2020-11-20"), eventType="spin_off", legType="stock", childTradingItemId=3, childRatio=0.1, source="distributions", status="ok", notes=""),
                    dict(tradingItemId=1, exDate=T("2020-11-04"), eventType="spin_off", legType="stock", childTradingItemId=3, childRatio=0.5, source="distributions", status="ok", notes="")],
                   extra=("_cashEquiv", "_impliedFrac", "_leg"))
    ev4 = pd.DataFrame({"tradingItemId": [2], "hasCashLeg": [True], "hasStockLeg": [False]})
    out, reps = lb.align_legs_to_settlement_bar(stock, lc4, ev4, cal, closes)
    assert out.exDate.tolist() == [T("2020-11-02"), T("2020-11-20"), T("2020-11-04")] and len(reps) == 1 and "exDate_aligned_to_settlement_bar" in out.notes.iloc[0]
    # R-X0 final check P2-17: the child has no close on the settlement bar -> the leg is NOT moved (it would become unresolved and be lost in the export)
    b_gap = pd.read_parquet(src); b_gap = b_gap[~((b_gap.tradingItemId == 3) & (b_gap.date == T("2020-11-02")))]
    b_gap.to_parquet(work / "base_lc_gap.parquet", index=False)
    out_g, reps_g = lb.align_legs_to_settlement_bar(stock, lc4, ev4, cal, lb.RawCloses(work / "base_lc_gap.parquet"))
    assert out_g.exDate.tolist() == [T("2020-11-04"), T("2020-11-20"), T("2020-11-04")] and "settlement_bar_alignment_skipped(no child 3 close on 2020-11-02" in out_g.notes.iloc[0]
    assert len(reps_g) == 1 and reps_g[0]["after"].startswith("kept(") and "NOT moved" in reps_g[0]["evidence"]
    # (d) scope: ledger value leg on the settlement bar -> events row full -> events_legs_only; the 11-20 leg is listed as after the bar
    led = _frame([dict(tradingItemId=2, exDate=T("2020-11-02"), eventType="spin_off", legType="stock", childTradingItemId=3, childRatio=0.5, source="distributions", status="ok", notes=""),
                  dict(tradingItemId=2, exDate=T("2020-11-20"), eventType="spin_off", legType="stock", childTradingItemId=3, childRatio=0.1, source="distributions", status="ok", notes="")])
    led["eventId"] = ["2_20201102_spin_off_0", "2_20201120_spin_off_0"]
    ev5 = ra._events_types(ev); ev5.loc[ev5.index[0], ["hasStockLeg", "endDate", "settlementDate"]] = [True, T("2020-10-30"), T("2020-11-02")]
    ev6, warn, n = ra.label_settlement_scope(ev5, led, lc4, cal, alog6 := ra.ApplyLog())
    assert n == 1 and ev6.settlementReturnScope.iloc[0] == "events_legs_only" and "multi_leg_split_path" in ev6.reviewFlag.iloc[0] and len(warn) == 1 and warn.eventId.iloc[0] == "2_20201120_spin_off_0"
    assert alog6.frame().overrideId.tolist() == ["RULE-X4-scope:2"]
    # §4-9 validator: lifecycle successorRatio 2.0 vs same-run lineage set_ratio 1.0 -> error; vs rc2 lineage merger ratio 0.5 (pair 5->6) -> error
    ref = synth_ref(work)
    d = work / "v9"; d.mkdir(exist_ok=True)
    common_v = dict(caseId="1", evidence="ev", source="s", isEstimate="false", owner="T")
    pd.DataFrame([dict(overrideId="Q-1", tradingItemId=1, oldId="1", newId="2", action="set_ratio", ratio="1.0", effectiveDate="", relation="", chainable="", **common_v)]).to_csv(d / "lineage_overrides.csv", index=False)
    pd.DataFrame([dict(overrideId="Q-2", tradingItemId=1, lastTradeDate="2020-11-30", effectiveDate="", settlementDate="2020-12-02", payDate="", endReason="stock_merger", successorTradingItemId="2", successorRatio="2.0", cashPerShare="", treatment="convert_successor", **common_v),
                  dict(overrideId="Q-3", tradingItemId=2, lastTradeDate="2020-11-30", effectiveDate="", settlementDate="2020-12-02", payDate="", endReason="stock_merger", successorTradingItemId="3", successorRatio="1.0", cashPerShare="", treatment="convert_successor", **common_v)]).to_csv(d / "lifecycle_overrides.csv", index=False)
    lin = ref._cache["lineage"]; lin.loc[len(lin)] = dict(oldId=2, newId=3, relation="merger", effectiveDate=T("2020-12-01"), ratio=0.5, evidence="e", confidence="high", ratioSource="", linkClass="", reviewFlag="", sources="v1", version="v1-rc1", chainGapTradingDays=pd.NA, chainable=False)
    lin["chainGapTradingDays"] = lin.chainGapTradingDays.astype("Int64")
    summary, iss = ra.validate_files(sorted(str(p) for p in d.glob("*.csv")), ref)
    errs = {(r["overrideId"], r["message"]) for r in iss.rows if r["level"] == "ERROR"}
    assert any(o == "Q-2" and "conflicts with lineage override Q-1" in m for o, m in errs) and any(o == "Q-3" and "conflicts with rc2 lineage ratio 0.5" in m for o, m in errs), sorted(errs)


TESTS = [test_split_drop_set_move_add_confirm, test_split_overrides_table_and_log, test_price_overrides_rewrite, test_value_overrides_apply, test_value_overrides_null_preserved, test_value_overrides_validator, test_lineage_overrides, test_lifecycle_overrides,
         test_ledger_csv_overrides_pre_post, test_ledger_csv_idempotency, test_ledger_csv_reclassified_target, test_l1_csv_add_vs_stitched_copy_and_absent_drop, test_stitched_copies_map_tagged_keeper, test_attribution_transition_and_reassigned_copy,
         test_vendor_gap_marker_window, test_extend_prices_terms_and_scope, test_validator_synthetic, test_recount_diff_and_attribution, test_real_empty_override_bit_identical]


def main(argv=None) -> int:
    import argparse
    import traceback
    import warnings
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default=None)
    a = ap.parse_args(argv)
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
    w = max(len(r[0]) for r in rows) if rows else 10
    print(f"\n{'test':{w}s}  result  note")
    for name, res, note in rows:
        print(f"{name:{w}s}  {res:6s}  {note}")
    n_fail = sum(r[1] == "FAIL" for r in rows)
    print(f"\n{len(rows) - n_fail}/{len(rows)} passed")
    if _WORK is not None:
        shutil.rmtree(_WORK, ignore_errors=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
