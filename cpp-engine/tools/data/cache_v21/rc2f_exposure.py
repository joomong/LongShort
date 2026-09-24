#!/home/jgo/usr/venv-3.11/bin/python
"""rc2f Y3 — 전체 노출 보고서 + 최종 수치 정정 (발행본 기준, 읽기 전용).

입력(모두 읽기 전용): 발행본 root(derived/returns_total, universe/membership, events/corporate_actions, master/lifecycle·lineage·current·
aux_instruments, events/ca_ledger, manifest.json, qa/rc2f/recount/{flag4_after.csv, returns_diff.parquet, summary.json},
qa/rc2f/registry/registry_final.csv), rc2 root(derived/returns_total), 라이브 북(combo000014 *_pos_values.parquet 마지막 파일).

출력(--out): exposure_report.md + csv (모든 표는 이 스크립트가 계산한 값).
  1. cells_by_universe_year.csv / cells_totals.csv / lineage_link_rows.csv — retFlag 0~4 + 첫 행 NaN(연결 불가 / 겹침 링크 / 계보 없음) 셀 집계(유니버스 당일 멤버십 × 연도)
  2. flag4_584_rows.csv / flag4_584_summary.csv / flag4_584_by_cause.csv / flag4_new.csv / flag4_published_coverage.csv — 원래 flag 4 584행 정정 표(발행본에서 재계산) + 새 flag 4 + 레지스트리 커버
  3. unresolved_by_class_category.csv / unresolved_category_rules.csv / unresolved_cases_ENDDATE_GAP_PRICE.csv — 자료 부족 사건 범주화
  4. book_exposure_list.csv / book_exposure_summary.csv / book_exposure_monthly.csv / book_settlements.csv — 라이브 북 교차
  5. number_reconciliation.csv — manifest · recount · registry · 문서 숫자 대조

사용: rc2f_exposure.py --build <발행본 root> --rc2 <rc2 root> --out <dir> [--book <glob>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

DAY_SHIFT = 16  # key = tradingItemId << 16 | day(1970 기준 일수 < 65536)
CATS = ["flag0", "flag1", "flag2", "flag3", "flag4", "nan_first_unlinked", "nan_first_overlap", "nan_first_nolineage"]
CAT_LABEL = {"flag0": "flag 0 가격만", "flag1": "flag 1 현금", "flag2": "flag 2 분배", "flag3": "flag 3 추정", "flag4": "flag 4 미해결",
             "nan_first_unlinked": "첫 행 NaN(연결 불가)", "nan_first_overlap": "첫 행 NaN(겹침 링크, 링크 행은 뒤)", "nan_first_nolineage": "첫 행 NaN(계보 없음: 신규 상장·인수사)"}
MAIN_KINDS = ["flag3_estimate", "flag4_unresolved", "nan_cell", "default_settlement(조건 없음)", "settlement_known(조건 있음)"]
NO_COMMA = {"year", "tradingItemId", "caseId", "successorTradingItemId", "childTradingItemId", "day", "oldId", "newId", "caseIds"}
BOOK_GLOB = "/home/base/portfolio/portfolio_v1_0/combo/combo_export/combo000014/*_pos_values.parquet"

# 자료 부족 사건 residualReason 범주(정규식, 위에서부터 첫 일치). (범주, 정규식, 뜻)
REASON_RULES = [
    ("sanity_unit_mismatch", r"^sanity:", "합병 조건이 시사하는 수익률이 가격과 모순(단위/승계사 불일치 의심) → 조건 미채택"),
    ("partial_terms", r"^partial_terms", "합병 대가 일부(우선주·다중 발행사 leg)에 가격 원천 없음"),
    ("election_proration", r"election|proration", "선택형/비례배정 합병: 무선택 기본값·최종 비례 결과가 xfeed 에 없음"),
    ("shell_rto", r"shell|\bRTO\b|reverse-merger", "셸/역합병(RTO): 전환 비율 원천 없음, 양쪽 호가 비교 불가"),
    ("successor_no_ratio_or_price", r"successor known .* no exchange ratio|no successor security priced|successor .* not priced|ratio known but no successor|successor known .* ratio sources", "승계사는 알지만 교환비(1:1 가정이 가격과 모순) 또는 승계사 가격이 없음"),
    ("ratio_conflict", r"ratio_conflict|ratio conflict|contradicted by (the )?(price|quote)|contradicted by prices", "ciqsplit·가격·계보 비율이 서로 모순, 판정할 날짜 있는 원천 없음"),
    ("ratio_undocumented", r"ratio undocumented|exchange ratio undocumented|undocumented ratio|ratio not (found|retrieved)|ratio has no dated source|price-implied .* not adopted|ratio ambiguous|no exchange ratio|without an exchange ratio", "교환/전환 비율이 xfeed·공시에 없음(가격 역산 비율은 채택 안 함)"),
    ("rights_warrant_terms", r"rights terms|subscription price|warrant|497AD|424B", "권리/워런트 조건(구주당 수·행사가·만기) 미확보 → 배정일 가치 없음"),
    ("child_unpriced", r"child.*(not priced|unpriced|not in base_data|not in xfeed|unlisted)|children unlisted|no priced security|spin-out legs .* missing|in-kind component|TSXV", "분배 자회사/증권 가격 없음(비상장·xfeed 없음)"),
    ("holdrs_passthrough", r"HOLDRs", "HOLDRs 구성 증권/비율 미확인"),
    ("fx_amount_missing", r"fx/amount missing", "빌드 시점 외화 금액/환율 없음"),
    ("amount_implausible", r"implausible", "배당 금액/가격 단위 비현실, 검증 원천 없음"),
    ("bankruptcy_suspension_liquidation", r"bankruptcy|suspended|CCAA|liquidation|SPAC", "파산·거래정지·청산: 구주 결과/청산 분배액 미문서화"),
    ("price_unit_issue", r"unit (error|issue)|head price unit", "새 id 머리 구간 가격 단위 오류 의심(원천 미확정)"),
    ("terms_not_found", r"terms not (found|retrieved|retrievable|in xfeed)|no terms|without terms|no per-share terms|no filings|filing not retrievable|not documented in xfeed|not in xfeed|not in reachable filings|no public source|no matching distribution|no ex-date warrant|not identified|prospectus not retrieved|no filings found", "사건 조건 자체를 xfeed·공시에서 찾지 못함(전 2001 EDGAR 미검색 포함)"),
    ("vendor_consistent_price_deviates", r"vendor record consistent", "벤더 분할 기록은 일관되나 분할 후 가격 수준이 pro-forma 대비 25% 초과 이탈, 독립 원천 없음"),
    ("factor_step_no_record", r"factor_step_without|vendor_factor_step_without|erratic_vendor_factor", "분할 기록 없는 벤더 계수 스텝(희소/불규칙 호가)"),
    ("ca_record_price_unchanged", r"CA record vs unchanged", "CA 기록은 있으나 호가 단위 변화 없음, 독립 주식수 원천 없음"),
    ("split_unverifiable_quotes", r"split not reflected|split direction|split magnitude|forward split|quotes sparse|thin OTC|sparse OTC|tick floor|ratio or direction unverifiable|quote gap|unquoted_at_exDate|vendor split (record|rate) contradicted|not comparable", "희소/OTC 호가라 분할 비율·방향을 검증 못 함(호가 공백·틱 바닥)"),
    ("stitched_series_step", r"stitched|in-series step|split step inside|split at the boundary", "이어붙인 시계열 내부의 미표현 스텝/경계 분할(원천 미확정)"),
    ("old_id_relation_unverified", r"old-id tail|old id continues|relation (unverified|ambiguous)|no structural link|no own print|not a clean copy|no prices and no structural", "옛 id 꼬리/관계 미확인(연결 근거 없음)"),
    ("scope_other_task", r"outside X3 scope|X4 scope|X1 domain|X1/lead|owned by X2|not expressible|^see \d+|^same as \d+|R1/X0|duplicate vendor row|inert duplicate|per-share settlement allocation", "다른 태스크/규칙 영역 또는 표현 불가(중복 행 등)"),
    ("other", r".", "위 규칙 미일치(보고서에 원문 나열)"),
]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _days(ts) -> np.ndarray:
    return (np.asarray(ts, dtype="datetime64[ns]").astype("datetime64[D]").astype("int64")).astype(np.int64)


def _key(ids: np.ndarray, days: np.ndarray) -> np.ndarray:
    return (ids.astype(np.int64) << DAY_SHIFT) | days.astype(np.int64)


def md_table(df: pd.DataFrame, index: bool = True, fmt: dict | None = None) -> str:
    d = df.copy()
    if index:
        d = d.reset_index()
    cols = list(d.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for _, row in d.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if v is pd.NA or v is None:
                cells.append("")
                continue
            if fmt and c in fmt:
                cells.append(fmt[c](v))
            elif isinstance(v, (bool, np.bool_)):
                cells.append("Y" if bool(v) else "N")
            elif isinstance(v, (float, np.floating)):
                if pd.isna(v):
                    cells.append("")
                elif str(c) in NO_COMMA:
                    cells.append(str(int(v)) if float(v).is_integer() else f"{v:.6g}")
                else:
                    cells.append(f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.4g}")
            elif isinstance(v, (int, np.integer)):
                cells.append(str(int(v)) if str(c) in NO_COMMA else f"{int(v):,}")
            elif isinstance(v, (pd.Timestamp, np.datetime64)):
                cells.append("" if pd.isna(v) else pd.Timestamp(v).strftime("%Y-%m-%d"))
            else:
                cells.append("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
class Returns:
    """returns_total long 파일(id, date 정렬) + 파생 배열."""

    def __init__(self, path: Path, with_prev: bool = True):
        cols = ["date", "tradingItemId", "ret", "retFlag"] + (["prevDate"] if with_prev else [])
        t = pq.read_table(path, columns=cols)
        self.rows = t.num_rows
        self.ids = t.column("tradingItemId").to_numpy().astype(np.int64)
        self.days = _days(t.column("date").to_numpy())
        self.ret = t.column("ret").to_numpy(zero_copy_only=False).astype(np.float64)
        self.flag = t.column("retFlag").to_numpy().astype(np.int8)
        self.prev = _days(t.column("prevDate").to_numpy()) if with_prev else None
        del t
        self.key = _key(self.ids, self.days)
        if not np.all(np.diff(self.key) > 0):
            raise SystemExit(f"{path}: not sorted by (tradingItemId, date) or duplicate keys")
        self.first = np.ones(self.rows, dtype=bool)
        self.first[1:] = self.ids[1:] != self.ids[:-1]
        self.year = (self.days.astype("datetime64[D]").astype("datetime64[Y]").astype(np.int64) + 1970).astype(np.int16)
        self.isnan = np.isnan(self.ret)

    def lookup(self, ids: np.ndarray, days: np.ndarray):
        k = _key(np.asarray(ids, dtype=np.int64), np.asarray(days, dtype=np.int64))
        pos = np.searchsorted(self.key, k)
        posc = np.minimum(pos, self.rows - 1)
        hit = self.key[posc] == k
        return hit, posc

    def link_rows(self) -> np.ndarray:
        """링크 행: 첫 행인데 ret 있음(prev = 옛 id) 또는 첫 행 아닌데 prevDate ≠ 직전 자기 행 날짜."""
        lk = np.zeros(self.rows, dtype=bool)
        lk[self.first] = ~self.isnan[self.first]
        nf = ~self.first
        idx = np.nonzero(nf)[0]
        lk[idx] = self.prev[idx] != self.days[idx - 1]
        return lk

    def interval_mask(self, ids: np.ndarray, from_days: np.ndarray, to_days: np.ndarray) -> np.ndarray:
        """[from, to] 양끝 포함 구간 목록 → 행 bool(loader._interval_mask 와 같은 규약)."""
        lo = np.searchsorted(self.key, _key(ids, from_days), side="left")
        hi = np.searchsorted(self.key, _key(ids, to_days), side="right")
        diff = np.zeros(self.rows + 1, dtype=np.int32)
        np.add.at(diff, lo, 1)
        np.add.at(diff, hi, -1)
        return np.cumsum(diff[:-1]) > 0


def load_membership(root: Path) -> pd.DataFrame:
    m = pd.read_parquet(root / "universe" / "membership.parquet")
    m["fromDay"] = _days(m.fromDate.values)
    m["toDay"] = _days(m.toDate.values)
    return m


def universe_masks(R: Returns, mb: pd.DataFrame, names=("Top500", "Top1000", "ALL")) -> dict:
    out = {}
    for u in names:
        s = mb[mb.universe == u]
        out[u] = R.interval_mask(s.tradingItemId.to_numpy(np.int64), s.fromDay.to_numpy(np.int64), s.toDay.to_numpy(np.int64))
    out["ALL_ROWS"] = np.ones(R.rows, dtype=bool)
    return out


# --------------------------------------------------------------------------------------
def section1_cells(R: Returns, root: Path, masks: dict, out: Path, rc2_counts: pd.DataFrame | None):
    """셀 범주 × 유니버스 × 연도."""
    ln = pd.read_parquet(root / "master" / "lineage.parquet", columns=["oldId", "newId", "relation", "chainable"])
    led = pd.read_parquet(root / "events" / "ca_ledger.parquet", columns=["tradingItemId", "childTradingItemId", "eventType", "status"])
    lk = led[led.eventType.isin(["relist", "rename"])].copy()
    lk["childTradingItemId"] = lk.childTradingItemId.astype("int64")
    lk = lk.merge(ln[["oldId", "newId", "chainable"]].drop_duplicates(["oldId", "newId"]), left_on=["tradingItemId", "childTradingItemId"], right_on=["oldId", "newId"], how="left")
    last_mask = np.ones(R.rows, dtype=bool)
    last_mask[:-1] = R.ids[1:] != R.ids[:-1]
    first_day = pd.Series(R.days[R.first], index=R.ids[R.first])
    last_day = pd.Series(R.days[last_mask], index=R.ids[last_mask])
    first_ret_nan = pd.Series(R.isnan[R.first], index=R.ids[R.first])
    first_prev = pd.Series(R.prev[R.first], index=R.ids[R.first])
    lk["oldLast"] = lk.tradingItemId.map(last_day)
    lk["newFirst"] = lk.childTradingItemId.map(first_day)
    lk["newLast"] = lk.childTradingItemId.map(last_day)
    lk["newFirstNaN"] = lk.childTradingItemId.map(first_ret_nan)
    lk["firstLinked"] = (lk.childTradingItemId.map(first_prev) == lk.oldLast) & (lk.newFirstNaN == False)
    lk["overlap"] = lk.oldLast >= lk.newFirst
    # 겹침 링크: chainable 이고 옛 id 가 새 id 첫 행 이후에도 거래 → 링크 행 = 옛 id 마지막 거래 뒤 첫 새 id 행(있어야 함)
    lk["overlapLinked"] = (lk.chainable == True) & lk.overlap & (lk.newLast > lk.oldLast)
    cont_new = np.unique(np.concatenate([ln[ln.relation.isin(["relist", "rename"])].newId.to_numpy(np.int64), lk.childTradingItemId.to_numpy(np.int64)]))
    overlap_new = np.unique(lk[lk.overlapLinked].childTradingItemId.to_numpy(np.int64))
    cat = np.full(R.rows, -1, dtype=np.int8)
    for f in range(5):
        cat[(R.flag == f) & ~R.isnan] = f
    nf = R.first & R.isnan
    has_cont = np.isin(R.ids, cont_new)
    is_overlap = np.isin(R.ids, overlap_new)
    cat[nf & is_overlap] = 6                      # 겹침 링크: 링크 행이 뒤에 있음(첫 행 NaN 은 설계)
    cat[nf & has_cont & ~is_overlap] = 5          # 연결 불가: 계보(relist/rename) 있으나 첫 행도 뒤 행도 연결 안 됨
    cat[nf & ~has_cont & ~is_overlap] = 7         # 계보 없음: 신규 상장/인수사 첫 행
    assert (cat >= 0).all(), "uncategorised rows"
    nan_nonfirst = int((R.isnan & ~R.first).sum())
    years = np.arange(int(R.year.min()), int(R.year.max()) + 1)
    ny = len(years)
    rows = []
    totals = []
    for u, m in masks.items():
        idx = (R.year[m].astype(np.int64) - years[0]) * len(CATS) + cat[m].astype(np.int64)
        c = np.bincount(idx, minlength=ny * len(CATS)).reshape(ny, len(CATS))
        for yi, y in enumerate(years):
            rows.append({"universe": u, "year": int(y), **{CATS[j]: int(c[yi, j]) for j in range(len(CATS))}, "total": int(c[yi].sum())})
        totals.append({"universe": u, **{CATS[j]: int(c[:, j].sum()) for j in range(len(CATS))}, "total": int(c.sum())})
    by_year = pd.DataFrame(rows)
    tot = pd.DataFrame(totals).set_index("universe")
    tot["nan_first_all"] = tot[["nan_first_unlinked", "nan_first_overlap", "nan_first_nolineage"]].sum(axis=1)
    if rc2_counts is not None:
        for c in ["flag3", "flag4", "nan_first_all"]:
            tot[f"rc2_{c}"] = rc2_counts[c].reindex(tot.index)
    by_year.to_csv(out / "cells_by_universe_year.csv", index=False)
    tot.to_csv(out / "cells_totals.csv")
    lk.to_csv(out / "lineage_link_rows.csv", index=False)
    info = {"rows": int(R.rows), "ids": int(R.first.sum()), "flag_counts": {int(f): int((R.flag == f).sum()) for f in range(5)},
            "nan_rows": int(R.isnan.sum()), "nan_first_rows": int(nf.sum()), "nan_nonfirst_rows": nan_nonfirst,
            "ledger_link_rows(relist/rename)": int(len(lk)), "ledger_link_rows_chainable": int((lk.chainable == True).sum()),
            "links_first_row_linked(첫 행 ret 있음, prevDate = 옛 id 마지막)": int(lk.firstLinked.sum()),
            "links_overlap_linked(chainable·겹침·옛 id 마지막 뒤 새 id 행 있음)": int(lk.overlapLinked.sum()),
            "links_chainable_not_linked": int(((lk.chainable == True) & ~lk.firstLinked & ~lk.overlapLinked).sum()),
            "links_not_chainable": int((lk.chainable != True).sum()),
            "lineage_continuation_newIds(relist/rename ∪ ledger link child)": int(len(cont_new)),
            "nan_first_unlinked_ids": int((cat == 5).sum()), "nan_first_overlap_ids": int((cat == 6).sum()), "nan_first_nolineage_ids": int((cat == 7).sum()),
            "flag4_nan": int((R.isnan & (R.flag == 4)).sum()), "flag3_nan": int((R.isnan & (R.flag == 3)).sum())}
    return by_year, tot, cat, info


def rc2_universe_counts(rc2_root: Path, mb: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    R2 = Returns(rc2_root / "derived" / "returns_total.parquet", with_prev=False)
    masks = universe_masks(R2, mb)
    nf = R2.first & R2.isnan
    rows = []
    for u, m in masks.items():
        rows.append({"universe": u, "flag3": int(((R2.flag == 3) & ~R2.isnan & m).sum()), "flag4": int(((R2.flag == 4) & ~R2.isnan & m).sum()),
                     "nan_first_all": int((nf & m).sum()), "total": int(m.sum())})
    info = {"rows": int(R2.rows), "flag_counts": {int(f): int((R2.flag == f).sum()) for f in range(5)}, "nan_rows": int(R2.isnan.sum()), "nan_first_rows": int(nf.sum())}
    return pd.DataFrame(rows).set_index("universe"), info, R2


# --------------------------------------------------------------------------------------
def section2_flag4(R: Returns, R2: Returns, root: Path, masks: dict, out: Path) -> dict:
    rec = root / "qa" / "rc2f" / "recount"
    f4 = pd.read_csv(rec / "flag4_after.csv")
    f4["date"] = pd.to_datetime(f4["date"])
    ids = f4.tradingItemId.to_numpy(np.int64)
    days = _days(f4.date.values)
    # rc2 확인: 584행 전부 rc2 flag 4
    hit2, pos2 = R2.lookup(ids, days)
    rc2_flag4 = int((hit2 & (R2.flag[pos2] == 4)).sum())
    # 발행본 재계산
    hit, pos = R.lookup(ids, days)
    ret_after = np.where(hit, R.ret[pos], np.nan)
    flag_after = np.where(hit, R.flag[pos], -1)
    state = np.where(~hit, "row_removed", np.where(np.isnan(ret_after), "nan(unlinked)", np.where(flag_after == 4, "still_flag4", "flag" + pd.Series(flag_after).astype(str).to_numpy())))
    f4["retAfter_pub"] = ret_after
    f4["retFlagAfter_pub"] = flag_after
    f4["stateAfter_pub"] = state
    f4["matches_recount_csv"] = f4.stateAfter_pub.to_numpy() == f4.stateAfter.astype(str).to_numpy()
    f4["top500_sameday"] = masks["Top500"][pos] & hit
    f4["top1000_sameday"] = masks["Top1000"][pos] & hit
    grp = {"flag0": "정상 분류 전환(0/1/2)", "flag1": "정상 분류 전환(0/1/2)", "flag2": "정상 분류 전환(0/1/2)", "flag3": "추정 전환(3)",
           "nan(unlinked)": "잘못된 연결 제거(NaN)", "still_flag4": "잔여 still_flag4", "row_removed": "행 삭제"}
    f4["group"] = f4.stateAfter_pub.map(grp).fillna("?")
    f4["causeClass"] = f4.cause.astype(str).str.split(":").str[0]
    f4.to_csv(out / "flag4_584_rows.csv", index=False)
    summ = f4.groupby(["group", "stateAfter_pub"]).size().rename("rows").reset_index()
    summ["top500_sameday"] = f4.groupby(["group", "stateAfter_pub"]).top500_sameday.sum().to_numpy()
    by_cause = f4.groupby(["group", "cause"]).size().rename("rows").reset_index()
    summ.to_csv(out / "flag4_584_summary.csv", index=False)
    by_cause.to_csv(out / "flag4_584_by_cause.csv", index=False)
    # 새 flag 4: returns_diff 재계산 + 발행본 대조
    diff = pd.read_parquet(rec / "returns_diff.parquet")
    new4 = diff[(diff.flagNew == 4) & (diff.flagOld != 4)].copy()
    new4["how1"] = new4.how.str.split(";").str[0]
    n_ids = new4.tradingItemId.to_numpy(np.int64)
    n_days = new4.day.to_numpy(np.int64)
    h, p = R.lookup(n_ids, n_days)
    new4["pub_flag"] = np.where(h, R.flag[p], -1)
    new4["pub_ret"] = np.where(h, R.ret[p], np.nan)
    new4["top500_sameday"] = masks["Top500"][p] & h
    new4["top1000_sameday"] = masks["Top1000"][p] & h
    h2, p2 = R2.lookup(n_ids, n_days)
    new4["rc2_flag_check"] = np.where(h2, R2.flag[p2], -1)
    new4 = new4.sort_values(["how1", "tradingItemId", "date"])
    new4[["tradingItemId", "date", "retOld", "retNew", "flagOld", "flagNew", "kind", "how1", "how", "attributedTo", "attributedPrimary", "pub_flag", "pub_ret", "rc2_flag_check", "top500_sameday", "top1000_sameday"]].to_csv(out / "flag4_new.csv", index=False)
    new4_by_how = new4.groupby("how1").agg(rows=("day", "size"), flagOld0=("flagOld", lambda s: int((s == 0).sum())), flagOld1=("flagOld", lambda s: int((s == 1).sum())),
                                           flagOld3=("flagOld", lambda s: int((s == 3).sum())), top500_sameday=("top500_sameday", "sum"),
                                           overrides=("attributedPrimary", lambda s: ", ".join(f"{k}:{v}" for k, v in s.value_counts().head(3).items()) + (" …" if s.nunique() > 3 else ""))).sort_values("rows", ascending=False)
    # 발행본 flag 4 집합 = still_flag4 ∪ new4 ?
    pub4_keys = R.key[(R.flag == 4)]
    still_keys = _key(ids[state == "still_flag4"], days[state == "still_flag4"])
    new_keys = _key(n_ids, n_days)
    union = np.union1d(still_keys, new_keys)
    inter = np.intersect1d(still_keys, new_keys)
    missing = np.setdiff1d(pub4_keys, union)
    extra = np.setdiff1d(union, pub4_keys)
    check = {"published_flag4": int(len(pub4_keys)), "still_flag4": int(len(still_keys)), "new_flag4": int(len(new_keys)), "still∩new": int(len(inter)),
             "published − (still ∪ new)": int(len(missing)), "(still ∪ new) − published": int(len(extra)),
             "new4_pub_flag_all_4": bool((new4.pub_flag == 4).all()), "new4_rc2_flag_matches_flagOld": bool((new4.rc2_flag_check == new4.flagOld).all()),
             "rc2_584_all_flag4": rc2_flag4, "recount_csv_state_mismatch": int((~f4.matches_recount_csv).sum()),
             "added_rows_with_flag4": int(((diff.kind == "added") & (diff.flagNew == 4)).sum()), "removed_rows_with_flagOld4": int(((diff.kind == "removed") & (diff.flagOld == 4)).sum())}
    # 발행본 flag 4 → 출처(잔여/새) + 레지스트리 사건 커버
    pub4 = pd.DataFrame({"key": pub4_keys})
    pub4["tradingItemId"] = np.right_shift(pub4_keys, DAY_SHIFT)
    pub4["day"] = np.bitwise_and(pub4_keys, (1 << DAY_SHIFT) - 1)
    pub4["date"] = pd.to_datetime(pub4.day, unit="D")
    pub4["origin"] = np.where(np.isin(pub4_keys, still_keys), "still_flag4(584 잔여)", np.where(np.isin(pub4_keys, new_keys), "new_flag4(rc2f 새 강등)", "?"))
    pub4["top500_sameday"] = masks["Top500"][np.searchsorted(R.key, pub4_keys)]
    pub4["top1000_sameday"] = masks["Top1000"][np.searchsorted(R.key, pub4_keys)]
    return {"f4": f4, "summ": summ, "by_cause": by_cause, "new4": new4, "new4_by_how": new4_by_how, "check": check, "pub4": pub4,
            "transition": pd.crosstab(f4.retFlag, f4.stateAfter_pub)}


def registry_cover(pub4: pd.DataFrame, reg: pd.DataFrame, root: Path, out: Path, new4: pd.DataFrame | None = None) -> pd.DataFrame:
    """발행본 flag 4 셀마다 레지스트리 사건 커버: 같은 id(또는 lineage 상대 id) 의 사건 창(impactFrom−3d .. impactTo+3d) → finalStatus; 없으면 ±30d; 없으면 no_case."""
    rg = reg.copy()
    rg["from"] = pd.to_datetime(rg.impactFrom.fillna(rg.eventDate))
    rg["to"] = pd.to_datetime(rg.impactTo.fillna(rg.eventDate))
    ln = pd.read_parquet(root / "master" / "lineage.parquet", columns=["oldId", "newId"])
    partners: dict[int, set] = {}
    for o, n in zip(ln.oldId.astype(int), ln.newId.astype(int)):
        partners.setdefault(n, set()).add(o)
        partners.setdefault(o, set()).add(n)
    rank = {"unresolved_insufficient": 0, "estimate_evidence": 1, "confirmed_normal": 2, "resolved_evidence": 3}
    byid = {k: v for k, v in rg.groupby("tradingItemId")}
    cover, via, case, reason, fix, finding = [], [], [], [], [], []
    for r in pub4.itertuples():
        tid = int(r.tradingItemId)
        found = None
        for pad, tag in ((3, "window±3d"), (30, "±30d")):
            for cid, vtag in [(tid, "self")] + [(p, "lineage_partner") for p in sorted(partners.get(tid, ()))]:
                c = byid.get(cid)
                if c is None:
                    continue
                w = c[(c["from"] - pd.Timedelta(days=pad) <= r.date) & (r.date <= c["to"] + pd.Timedelta(days=pad))]
                if len(w):
                    w = w.assign(rk=w.finalStatus.map(rank)).sort_values("rk")
                    found = (str(w.finalStatus.iloc[0]), f"{vtag}:{tag}", ";".join(w.caseId.astype(str)), str(w.residualReason.iloc[0])[:160],
                             str(w.fix.iloc[0])[:120] if pd.notna(w.fix.iloc[0]) else "", str(w.finding.iloc[0])[:160] if pd.notna(w.finding.iloc[0]) else "")
                    break
            if found:
                break
        if not found:
            found = ("no_case", "id_has_cases" if tid in byid else "no_case_for_id", "", "", "", "")
        cover.append(found[0]); via.append(found[1]); case.append(found[2]); reason.append(found[3]); fix.append(found[4]); finding.append(found[5])
    pub4 = pub4.copy()
    pub4["registryCover"] = cover
    pub4["coverVia"] = via
    pub4["caseIds"] = case
    pub4["residualReason"] = reason
    pub4["caseFix"] = fix
    pub4["caseFinding"] = finding
    if new4 is not None:
        n = new4[["tradingItemId", "day", "how1", "attributedPrimary", "flagOld", "retOld", "retNew"]].rename(columns={"how1": "newHow", "attributedPrimary": "newOverride"})
        pub4 = pub4.merge(n, on=["tradingItemId", "day"], how="left")
        pub4["newHow"] = pub4.newHow.fillna("(still_flag4)")
    pub4.drop(columns=["key"]).to_csv(out / "flag4_published_coverage.csv", index=False)
    return pub4


# --------------------------------------------------------------------------------------
def categorize_reason(s: str) -> str:
    s = "" if pd.isna(s) else str(s)
    for name, rx, _ in REASON_RULES:
        if re.search(rx, s, flags=re.I):
            return name
    return "other"


def section3_unresolved(reg: pd.DataFrame, book_long: pd.DataFrame, out: Path) -> dict:
    u = reg[reg.finalStatus == "unresolved_insufficient"].copy()
    u["category"] = u.residualReason.map(categorize_reason)
    tab = pd.crosstab(u.category, u.primaryClass)
    tab["total"] = tab.sum(axis=1)
    tab = tab.sort_values("total", ascending=False)
    tab.loc["total"] = tab.sum()
    tab.to_csv(out / "unresolved_by_class_category.csv")
    rules = pd.DataFrame([{"category": n, "regex": rx, "meaning": mean, "cases": int((u.category == n).sum())} for n, rx, mean in REASON_RULES])
    rules.to_csv(out / "unresolved_category_rules.csv", index=False)
    other = u[u.category == "other"][["caseId", "primaryClass", "tradingItemId", "symbol", "residualReason"]]
    # 사건별 표: ENDDATE / GAP / PRICE
    held_ids = set(book_long.tradingItemId.unique())
    bk = {k: v for k, v in book_long.groupby("tradingItemId")}
    sel = u[u.primaryClass.isin(["ENDDATE", "GAP", "PRICE"])].copy()
    sel["impactFrom"] = pd.to_datetime(sel.impactFrom.fillna(sel.eventDate))
    sel["impactTo"] = pd.to_datetime(sel.impactTo.fillna(sel.eventDate))
    ever, win, absw = [], [], []
    for r in sel.itertuples():
        tid = int(r.tradingItemId)
        ever.append(tid in held_ids)
        b = bk.get(tid)
        if b is None:
            win.append(False); absw.append(0.0); continue
        w = b[(b.bookDate >= r.impactFrom - pd.Timedelta(days=7)) & (b.bookDate <= r.impactTo + pd.Timedelta(days=7))]
        win.append(bool(len(w))); absw.append(float(w.pos.abs().sum()))
    sel["bookEverHeld"] = ever
    sel["bookHeldInWindow(±7d)"] = win
    sel["bookAbsPosInWindow"] = absw
    sel["reason_short"] = sel.residualReason.astype(str).str.replace(r"\s+", " ", regex=True).str[:110]
    cols = ["caseId", "primaryClass", "tradingItemId", "symbol", "eventDate", "impactFrom", "impactTo", "category", "reason_short", "everTop500", "bookEverHeld", "bookHeldInWindow(±7d)", "bookAbsPosInWindow", "maxRetFlagAfter", "ledgerStatusAfter"]
    sel = sel.sort_values(["primaryClass", "eventDate"])
    sel[cols + ["residualReason"]].to_csv(out / "unresolved_cases_ENDDATE_GAP_PRICE.csv", index=False)
    by_class = u.groupby("primaryClass").agg(cases=("caseId", "size"), flag4After=("flag4After", lambda s: int((s == True).sum())),
                                             everTop500=("everTop500", lambda s: int((s == True).sum())), bookEverHeld=("tradingItemId", lambda s: int(s.isin(held_ids).sum())))
    by_class.loc["total"] = by_class.sum()
    return {"u": u, "tab": tab, "rules": rules, "other": other, "sel": sel[cols], "by_class": by_class}


# --------------------------------------------------------------------------------------
def load_book(pattern: str):
    fs = sorted(glob.glob(pattern))
    if not fs:
        raise SystemExit(f"no book files: {pattern}")
    pv = pd.read_parquet(fs[-1])
    pv.index = pd.to_datetime(pv.index)
    pv = pv.sort_index().fillna(0.0)
    pv.columns = pv.columns.astype(int)
    long = pv.stack()
    long = long[long != 0].rename("pos").reset_index()
    long.columns = ["bookDate", "tradingItemId", "pos"]
    return fs[-1], pv, long


def section4_book(R: Returns, root: Path, masks: dict, cat: np.ndarray, book_file: str, pv: pd.DataFrame, long: pd.DataFrame, asof: pd.Timestamp, reg: pd.DataFrame, out: Path) -> dict:
    cur = pd.read_parquet(root / "master" / "current.parquet", columns=["tradingItemId", "symbol"])
    aux = pd.read_parquet(root / "master" / "aux_instruments.parquet", columns=["tradingItemId", "symbol"])
    sym = pd.concat([cur, aux]).drop_duplicates("tradingItemId").set_index("tradingItemId").symbol
    lc = pd.read_parquet(root / "master" / "lifecycle.parquet", columns=["tradingItemId", "lastTradeDate", "status", "endReason"]).set_index("tradingItemId")
    ev = pd.read_parquet(root / "events" / "corporate_actions.parquet")
    ev["unconditioned"] = ev.settlementReturn.isna() & (ev.successorTradingItemId.isna() | (ev.successorTradingItemId.fillna(0) <= 0))
    held_ids = np.unique(long.tradingItemId.to_numpy(np.int64))
    # 보유 셀(held-into): bookDate 이후 첫 행 = 그 포지션이 버는 수익률 셀
    sub = np.isin(R.ids, held_ids)
    r_ids, r_days, r_flag, r_ret, r_prev, r_cat = R.ids[sub], R.days[sub], R.flag[sub], R.ret[sub], R.prev[sub], cat[sub]
    r_key = _key(r_ids, r_days)
    r_top500 = masks["Top500"][sub]
    b_ids = long.tradingItemId.to_numpy(np.int64)
    b_days = _days(long.bookDate.values)
    pos_next = np.searchsorted(r_key, _key(b_ids, b_days), side="right")   # 첫 행 with key > (id, bookDate)
    posc = np.minimum(pos_next, len(r_key) - 1)
    has_next = (pos_next < len(r_key)) & (r_ids[posc] == b_ids)
    L = long.copy()
    L["symbol"] = L.tradingItemId.map(sym)
    L["beyondCache"] = L.bookDate >= asof
    L["cellDate"] = pd.NaT
    L.loc[has_next, "cellDate"] = pd.to_datetime(r_days[posc[has_next]], unit="D")
    L["cellPrevDate"] = pd.NaT
    L.loc[has_next, "cellPrevDate"] = pd.to_datetime(r_prev[posc[has_next]], unit="D")
    L["ret"] = np.where(has_next, r_ret[posc], np.nan)
    L["retFlag"] = np.where(has_next, r_flag[posc], -1)
    L["cellCat"] = np.where(has_next, np.array(CATS)[np.maximum(r_cat[posc], 0)], "no_cell")
    L["cellTop500"] = np.where(has_next, r_top500[posc], False)
    L["gapDaysToCell"] = np.where(has_next, r_days[posc] - b_days, -1)
    # 같은 날 셀(same-day, 참고): date == bookDate
    hit_s, pos_s = R.lookup(b_ids, b_days)
    L["sameDayFlag"] = np.where(hit_s, R.flag[pos_s], -1)
    L["sameDayNaN"] = hit_s & R.isnan[pos_s]
    L["sameDayMissing"] = ~hit_s
    L = L.join(lc, on="tradingItemId")
    L["afterLastTrade"] = L.bookDate > L.lastTradeDate
    L["onLastTrade"] = L.bookDate == L.lastTradeDate
    # 종류 판정
    evi = ev.set_index("tradingItemId")
    def kind(row):
        if row.beyondCache:
            return "beyond_cache(asOf 이후)"
        if row.cellCat == "flag3":
            return "flag3_estimate"
        if row.cellCat == "flag4":
            return "flag4_unresolved"
        if row.cellCat.startswith("nan"):
            return "nan_cell"
        if row.cellCat == "no_cell":
            e = evi.loc[row.tradingItemId] if row.tradingItemId in evi.index else None
            if isinstance(e, pd.DataFrame):
                e = e.iloc[-1]
            if e is None:
                return "no_cell_no_event"
            if bool(e.unconditioned):
                return "default_settlement(조건 없음)"
            return "settlement_known(조건 있음)"
        if row.gapDaysToCell > 7:
            return "ok_gap>7d"
        return "ok"
    L["kind"] = [kind(r) for r in L.itertuples()]
    L["absPos"] = L.pos.abs()
    expo = L[~L.kind.isin(["ok", "beyond_cache(asOf 이후)"])].copy()
    # 정산 조건 표(보유 id 의 events 행, 북 기간)
    evh = ev[ev.tradingItemId.isin(held_ids) & (ev.endDate >= long.bookDate.min() - pd.Timedelta(days=30))].copy()
    evh["symbol"] = evh.tradingItemId.map(sym)
    last_pos = []
    for r in evh.itertuples():
        b = long[(long.tradingItemId == r.tradingItemId) & (long.bookDate <= r.endDate)]
        last_pos.append(float(b.sort_values("bookDate").pos.iloc[-1]) if len(b) else 0.0)
    evh["posAtEnd"] = last_pos
    evh["stalePosAfterEnd"] = [float(long[(long.tradingItemId == r.tradingItemId) & (long.bookDate > r.endDate)].pos.abs().sum()) for r in evh.itertuples()]
    evh = evh[["tradingItemId", "symbol", "endDate", "settlementDate", "actionCode", "settlementReturn", "cashPerShare", "successorTradingItemId", "successorRatio", "confidence", "cacheUnitStatus", "unconditioned", "posAtEnd", "stalePosAfterEnd"]]
    evh.to_csv(out / "book_settlements.csv", index=False)
    # 보유 id 의 조건 없는 events 전체(날짜 무관)
    ev_unc_held = ev[ev.tradingItemId.isin(held_ids) & ev.unconditioned]
    # 요약
    gross = L[~L.beyondCache].absPos.sum()
    summ = expo.groupby("kind").agg(cells=("pos", "size"), days=("bookDate", "nunique"), ids=("tradingItemId", "nunique"), sumAbsPos=("absPos", "sum"), top500Cells=("cellTop500", "sum"))
    for k in MAIN_KINDS:
        if k not in summ.index:
            summ.loc[k] = [0, 0, 0, 0.0, 0]
    summ = summ.loc[[k for k in MAIN_KINDS if k in summ.index] + [k for k in summ.index if k not in MAIN_KINDS]]
    summ["shareOfGross"] = summ.sumAbsPos / gross if gross else np.nan
    base = pd.DataFrame({"cells": [int((~L.beyondCache).sum()), int(L.beyondCache.sum())], "days": [int(L[~L.beyondCache].bookDate.nunique()), int(L[L.beyondCache].bookDate.nunique())],
                         "ids": [int(L[~L.beyondCache].tradingItemId.nunique()), int(L[L.beyondCache].tradingItemId.nunique())], "sumAbsPos": [float(gross), float(L[L.beyondCache].absPos.sum())]},
                        index=["(기준) 캐시 범위 내 보유 셀 전체", "(참고) beyond_cache(asOf 이후) 보유 셀"])
    summ_all = pd.concat([base, summ])
    for c in ("cells", "days", "ids", "top500Cells"):
        summ_all[c] = summ_all[c].astype("Int64")
    summ_all.to_csv(out / "book_exposure_summary.csv")
    L["month"] = L.bookDate.dt.to_period("M").astype(str)
    mon = L[~L.beyondCache].groupby("month").agg(heldCells=("pos", "size"), grossAbsPos=("absPos", "sum"))
    for k in [k for k in MAIN_KINDS] + sorted(set(expo.kind.unique()) - set(MAIN_KINDS)):
        e = expo[expo.kind == k].groupby(expo[expo.kind == k].bookDate.dt.to_period("M").astype(str)).agg(n=("pos", "size"), a=("absPos", "sum"))
        mon[f"{k}_cells"] = e.n.reindex(mon.index).fillna(0).astype(int)
        mon[f"{k}_absPos"] = e.a.reindex(mon.index).fillna(0.0)
    mon.to_csv(out / "book_exposure_monthly.csv")
    cols = ["bookDate", "tradingItemId", "symbol", "pos", "kind", "cellDate", "cellPrevDate", "ret", "retFlag", "cellCat", "cellTop500", "gapDaysToCell", "lastTradeDate", "endReason", "onLastTrade", "afterLastTrade"]
    expo.sort_values(["kind", "bookDate", "tradingItemId"])[cols].to_csv(out / "book_exposure_list.csv", index=False)
    sameday = {"sameDay_flag3": int((L.sameDayFlag == 3).sum()), "sameDay_flag4": int((L.sameDayFlag == 4).sum()), "sameDay_nan": int(L.sameDayNaN.sum()),
               "sameDay_missing_row(캐시 범위 내)": int((L.sameDayMissing & ~L.beyondCache).sum())}
    info = {"book_file": book_file, "book_dates": int(pv.shape[0]), "book_ids_columns": int(pv.shape[1]), "held_ids(nonzero)": int(len(held_ids)),
            "held_cells": int(len(L)), "date_range": f"{pv.index.min().date()}..{pv.index.max().date()}", "asOf": str(asof.date()),
            "held_ids_not_in_returns": int(len(set(held_ids) - set(np.unique(r_ids)))), "sumAbsPos_all": float(L.absPos.sum()),
            "afterLastTrade_cells(캐시 범위 내)": int((L.afterLastTrade & ~L.beyondCache).sum()), "unconditioned_events_for_held_ids(any date)": int(len(ev_unc_held)), **sameday}
    return {"L": L, "expo": expo, "summ": summ_all, "mon": mon, "evh": evh, "info": info, "ev_unc_held": ev_unc_held}


# --------------------------------------------------------------------------------------
def section5_numbers(root: Path, R_info: dict, R2_info: dict, s2: dict, tot: pd.DataFrame, reg: pd.DataFrame, out: Path) -> pd.DataFrame:
    man = json.load(open(root / "manifest.json"))
    rr = man["steps"]["calendar"]["returns_rebuilt"]
    mrf = rr["retFlag"]
    rt_step = man["steps"]["returns_total"]
    rec = json.load(open(root / "qa" / "rc2f" / "recount" / "summary.json"))
    pub = R_info["flag_counts"]
    man_map = {"0": "price_only", "1": "cash", "2": "distribution", "3": "estimate", "4": "unresolved"}
    rows = []
    man_total = sum(mrf.values())
    rows.append({"항목": "returns 행 수", "manifest(returns_rebuilt, 꼬리 제거 전)": man_total, "recount summary(rc2f)": rec["returns"]["rows_new"], "발행본 재계산": R_info["rows"],
                 "차이": man_total - R_info["rows"], "판정": f"발행본 = manifest − dropped_post_lifecycle {rt_step['dropped_post_lifecycle']} (manifest steps.returns_total rows_in {rt_step['rows_in']} → rows_out {rt_step['rows_out']})"})
    for f in range(5):
        m = mrf[man_map[str(f)]]
        rows.append({"항목": f"retFlag {f}", "manifest(returns_rebuilt, 꼬리 제거 전)": m, "recount summary(rc2f)": rec["returns"]["retFlag_new"][str(f)], "발행본 재계산": pub[f],
                     "차이": m - pub[f], "판정": "발행본·recount 일치; manifest 초과분은 lifecycle 꼬리(lastTradeDate 이후) 제거 행" if rec["returns"]["retFlag_new"][str(f)] == pub[f] else "불일치"})
    dropped_sum = sum(mrf[man_map[str(f)]] - pub[f] for f in range(5))
    rows.append({"항목": "flag 별 차이 합", "manifest(returns_rebuilt, 꼬리 제거 전)": dropped_sum, "recount summary(rc2f)": "", "발행본 재계산": rt_step["dropped_post_lifecycle"], "차이": dropped_sum - rt_step["dropped_post_lifecycle"],
                 "판정": "= dropped_post_lifecycle 이면 manifest retFlag 는 꼬리 제거 전 집계로 설명됨"})
    c = s2["check"]
    f4 = rec["flag4_584"]
    n_removed = int((s2["f4"].group == "행 삭제").sum())
    rows.append({"항목": "584 잔여(resolved 아님)", "manifest(returns_rebuilt, 꼬리 제거 전)": "", "recount summary(rc2f)": f4["residual"], "발행본 재계산": c["still_flag4"] + n_removed,
                 "차이": f4["residual"] - (c["still_flag4"] + n_removed), "판정": f"잔여 = still_flag4 {c['still_flag4']} + row_removed {n_removed}; flag 4 총수 계산에는 still_flag4 {c['still_flag4']} 만 들어감(삭제 행은 발행본에 없음)"})
    rows.append({"항목": "새 flag 4", "manifest(returns_rebuilt, 꼬리 제거 전)": "", "recount summary(rc2f)": rec["retFlag_transition"]["new_flag4_cells"], "발행본 재계산": c["new_flag4"],
                 "차이": rec["retFlag_transition"]["new_flag4_cells"] - c["new_flag4"], "판정": "문서 §10-3 의 138(=70+54+14) 은 이전 빌드 값; 최종 recount·발행본은 139(ledger_set(target) 55)"})
    rows.append({"항목": "발행본 flag 4 총수", "manifest(returns_rebuilt, 꼬리 제거 전)": mrf["unresolved"], "recount summary(rc2f)": rec["returns"]["retFlag_new"]["4"], "발행본 재계산": c["published_flag4"],
                 "차이": mrf["unresolved"] - c["published_flag4"], "판정": f"발행본 {c['published_flag4']} = still {c['still_flag4']} + new {c['new_flag4']} (집합 동일: 누락 {c['published − (still ∪ new)']}·초과 {c['(still ∪ new) − published']}); manifest 412 는 꼬리 제거 전(2행 초과)"})
    rows.append({"항목": "Top500 당일 flag 4", "manifest(returns_rebuilt, 꼬리 제거 전)": "", "recount summary(rc2f)": "문서 16 → 1", "발행본 재계산": f"rc2 {int(tot.loc['Top500', 'rc2_flag4'])} → rc2f {int(tot.loc['Top500', 'flag4'])}",
                 "차이": "", "판정": "발행본·rc2 재계산(universe/membership 당일 구간, 양끝 포함)"})
    fs = reg.finalStatus.value_counts()
    rows.append({"항목": "registry_final 사건 수", "manifest(returns_rebuilt, 꼬리 제거 전)": "", "recount summary(rc2f)": f"registry cases {rec['registry']['cases']}(v0)" if isinstance(rec.get("registry"), dict) and "cases" in rec["registry"] else "",
                 "발행본 재계산": int(len(reg)), "차이": "", "판정": f"resolved_evidence {fs.get('resolved_evidence', 0)} · confirmed_normal {fs.get('confirmed_normal', 0)} · estimate_evidence {fs.get('estimate_evidence', 0)} · unresolved_insufficient {fs.get('unresolved_insufficient', 0)} · not_investigated {fs.get('not_investigated', 0)} = {int(fs.sum())}"})
    # 원장 status: 전체 vs stitched_copy 제외
    led = pd.read_parquet(root / "events" / "ca_ledger.parquet", columns=["status", "source"])
    st_all = led.status.value_counts().to_dict()
    st_ex = led[led.source != "stitched_copy"].status.value_counts().to_dict()
    rows.append({"항목": "원장 status(ok/estimated/unresolved)", "manifest(returns_rebuilt, 꼬리 제거 전)": f"ledger_rows {rr['ledger_rows']}", "recount summary(rc2f)": f"{rec['ledger']['status_new']} (stitched_copy 제외)",
                 "발행본 재계산": f"전체 {len(led)}: {st_all}; stitched_copy 제외 {len(led[led.source != 'stitched_copy'])}: {st_ex}", "차이": "", "판정": "문서 §10-3 '473,770행 ok 471,376·est 1,485·unres 909' 는 전체 행 기준, recount 는 stitched_copy 제외 기준(둘 다 맞음, 기준 병기 필요)"})
    rows.append({"항목": "rc2 returns 행/flag", "manifest(returns_rebuilt, 꼬리 제거 전)": "", "recount summary(rc2f)": f"{rec['returns']['rows_rc2']} / {rec['returns']['retFlag_rc2']}", "발행본 재계산": f"{R2_info['rows']} / {R2_info['flag_counts']}",
                 "차이": rec["returns"]["rows_rc2"] - R2_info["rows"], "판정": "rc2 발행본(.prev) 재계산과 recount 의 rc2 기준 일치 여부"})
    df = pd.DataFrame(rows)
    df.to_csv(out / "number_reconciliation.csv", index=False)
    return df


# --------------------------------------------------------------------------------------
def write_report(out: Path, args, R_info, R2_info, by_year, tot, s2, s3, s4, s5, cover):
    L = []
    A = L.append
    A(f"# rc2f 전체 노출 보고서 (Y3) — {dt.datetime.now():%Y-%m-%d %H:%M}")
    A("")
    A(f"발행본 `{args.build}` (build_id {json.load(open(Path(args.build) / 'manifest.json'))['build_id']}), rc2 `{args.rc2}`, 라이브 북 `{s4['info']['book_file']}`. 모든 숫자는 `rc2f_exposure.py` 출력(csv 동봉).")
    A("")
    A("## 0. 발행본 returns_total 기준 수치")
    A("")
    A(md_table(pd.DataFrame([{"항목": k, "값": (json.dumps(v) if isinstance(v, dict) else v)} for k, v in R_info.items()]), index=False))
    A("")
    A("첫 행 NaN 범주 정의(원장 relist/rename 링크 행 × lineage.chainable × returns 첫/마지막 행으로 판정, `lineage_link_rows.csv`): "
      "`nan_first_unlinked` = 첫 행 NaN 이고 계보(relist/rename)의 newId 인데 첫 행 링크도 겹침 링크도 없음(연결 불가: chainable 아님·chain gap·prev close 없음 등); "
      "`nan_first_overlap` = chainable 링크인데 옛 id 가 새 id 첫 행 이후에도 거래(미러 꼬리 등) → 링크는 옛 id 마지막 거래 뒤 첫 새 id 행에서 이뤄지고 첫 행 NaN 은 설계; "
      "`nan_first_nolineage` = 계보 없음(신규 상장·인수사 등). 첫 행 링크 = 첫 행 ret 있음·prevDate = 옛 id 마지막 날짜.")
    A("")
    rr = json.load(open(Path(args.build) / "manifest.json"))["steps"]["calendar"]["returns_rebuilt"]
    A(f"manifest 대조: links_resolved {rr['links_resolved']} = 첫 행 링크 {R_info['links_first_row_linked(첫 행 ret 있음, prevDate = 옛 id 마지막)']} + after_old_last {rr['links_after_old_last'] + rr['links_after_old_last_mirror']}(24+689); "
      f"본 스크립트의 겹침 링크 후보 {R_info['links_overlap_linked(chainable·겹침·옛 id 마지막 뒤 새 id 행 있음)']} 는 미러 링크 행이 일반 행과 구분되지 않아 데이터로만 판정한 상한 — 차이(최대 {R_info['links_overlap_linked(chainable·겹침·옛 id 마지막 뒤 새 id 행 있음)'] - rr['links_after_old_last'] - rr['links_after_old_last_mirror']} id)는 실제로는 연결 불가일 수 있음(nan_first_unlinked 하한 {R_info['nan_first_unlinked_ids']}).")
    A("")
    A("## 1. 셀 집계 — 유니버스(당일 멤버십) × 연도")
    A("")
    A("### 1-1. 총계(유니버스별; rc2 비교 열은 rc2 발행본 재계산)")
    A("")
    A(md_table(tot))
    A("")
    for u in ["Top500", "Top1000", "ALL", "ALL_ROWS"]:
        A(f"### 1-2. {u} 연도별" + (" (returns_total 전체 행)" if u == "ALL_ROWS" else " (universe/membership 당일 구간)"))
        A("")
        d = by_year[by_year.universe == u].drop(columns=["universe"]).set_index("year")
        A(md_table(d))
        A("")
    A("## 2. 원래 flag 4 584행 정정 표 (발행본에서 재계산)")
    A("")
    A(md_table(pd.DataFrame([{"검산": k, "값": v} for k, v in s2["check"].items()]), index=False))
    A("")
    A("### 2-1. 584행 → 발행본 상태")
    A("")
    g = s2["f4"].groupby("group").agg(rows=("tradingItemId", "size"), top500_sameday=("top500_sameday", "sum")).reindex(["정상 분류 전환(0/1/2)", "추정 전환(3)", "잘못된 연결 제거(NaN)", "잔여 still_flag4", "행 삭제"]).fillna(0).astype(int)
    g.loc["합계"] = g.sum()
    A(md_table(g))
    A("")
    A(md_table(s2["summ"], index=False))
    A("")
    A("### 2-2. 584행 상태 × 원인(rc2 flag4_rows_with_cause)")
    A("")
    A(md_table(s2["by_cause"].pivot_table(index="cause", columns="group", values="rows", fill_value=0, aggfunc="sum").assign(total=lambda d: d.sum(axis=1)).sort_values("total", ascending=False)))
    A("")
    A("### 2-3. 새 flag 4 (rc2 에서 flag 4 아니었던 셀; recount returns_diff 재계산, 발행본 flag 대조)")
    A("")
    A(md_table(s2["new4_by_how"]))
    A("")
    c = s2["check"]
    A(f"검산: 발행본 flag 4 {c['published_flag4']} = still_flag4 {c['still_flag4']} + new {c['new_flag4']} (교집합 {c['still∩new']}, 누락 {c['published − (still ∪ new)']}, 초과 {c['(still ∪ new) − published']}); 새 flag 4 셀의 발행본 flag 전부 4: {c['new4_pub_flag_all_4']}; rc2 584행 전부 flag 4: {c['rc2_584_all_flag4']}/584; recount csv stateAfter 와 불일치 {c['recount_csv_state_mismatch']}.")
    A("")
    A("### 2-4. 발행본 flag 4 셀의 출처·레지스트리 커버(같은 id, 사건 창 ±3d)")
    A("")
    A(md_table(pd.crosstab(cover.origin, cover.registryCover).assign(total=lambda d: d.sum(axis=1))))
    A("")
    A(md_table(pd.crosstab(cover.origin, cover.coverVia).assign(total=lambda d: d.sum(axis=1))))
    A("")
    A(md_table(pd.crosstab(cover.origin, cover.top500_sameday.map({True: "Top500 당일", False: "비 Top500"})).assign(total=lambda d: d.sum(axis=1))))
    A("")
    t5 = cover[cover.top500_sameday]
    if len(t5):
        A("Top500 당일 flag 4 셀:")
        A("")
        A(md_table(t5[["tradingItemId", "date", "origin", "registryCover", "caseIds", "residualReason"]], index=False))
        A("")
    A(md_table(pd.crosstab(cover.newHow, cover.registryCover).assign(total=lambda d: d.sum(axis=1))))
    A("")
    A("### 2-5. 검토 필요: 사건 판정이 resolved_evidence/confirmed_normal/no_case 인데 flag 4 로 남은 셀(rc2f 새 강등)")
    A("")
    A("링크 행 |ret|>0.5 규칙(rule:link_row_after_old_last)·lifecycle successor 전환 등으로 강등된 셀인데, 커버하는 사건은 '해결'로 기록됨 → 사건 판정(1:1 비율 등)과 셀 결과가 어긋남. 리드/X5·X4 재검토 대상(본 보고서는 목록만).")
    A("")
    rv = cover[cover.origin.str.startswith("new") & cover.registryCover.isin(["resolved_evidence", "confirmed_normal", "no_case"])].sort_values(["registryCover", "newHow", "date"])
    A(md_table(rv[["tradingItemId", "date", "newHow", "newOverride", "flagOld", "retOld", "retNew", "registryCover", "coverVia", "caseIds", "caseFix"]], index=False))
    A("")
    A("## 3. 자료 부족(unresolved_insufficient) 사건 — 클래스 × 사유 범주")
    A("")
    A(md_table(s3["by_class"]))
    A("")
    A(md_table(s3["tab"]))
    A("")
    A("### 3-1. 범주 규칙(residualReason 정규식, 위에서부터 첫 일치; 대소문자 무시)")
    A("")
    A(md_table(s3["rules"], index=False))
    A("")
    if len(s3["other"]):
        A("### 3-2. 규칙 미일치(other) 원문")
        A("")
        A(md_table(s3["other"], index=False))
        A("")
    A("### 3-3. ENDDATE · GAP · PRICE 사건별 표 (라이브 북 보유 여부 포함)")
    A("")
    A(md_table(s3["sel"], index=False))
    A("")
    A("## 4. 라이브 북(combo000014 목표 포지션) 교차")
    A("")
    A(md_table(pd.DataFrame([{"항목": k, "값": v} for k, v in s4["info"].items()]), index=False))
    A("")
    A("규약: 포지션(bookDate d, id) 이 버는 셀 = 같은 id 의 d 이후 첫 returns_total 행(held-into; prevDate = d 가 정상). 셀이 없으면(id 마지막 관측 ≤ d) events 정산 조건으로 분류: "
      "settlementReturn 있음 → `settlement_known`, settlementReturn NaN·successor 없음 → `default_settlement(조건 없음)`. asOf(2026-09-15) 이후 bookDate 는 캐시 밖(`beyond_cache`, 노출 집계 제외). 참고로 같은 날(date = d) 셀 기준 flag 3/4/NaN 수는 위 표 `sameDay_*`.")
    A("")
    A("### 4-1. 노출 합계(종류별; |포지션| USD)")
    A("")
    A(md_table(s4["summ"]))
    A("")
    A("### 4-2. 노출 목록")
    A("")
    e = s4["expo"]
    if len(e):
        A(md_table(e.sort_values(["kind", "bookDate"])[["bookDate", "tradingItemId", "symbol", "pos", "kind", "cellDate", "ret", "retFlag", "lastTradeDate", "endReason", "onLastTrade", "afterLastTrade"]], index=False))
    else:
        A("(없음)")
    A("")
    A("### 4-3. 보유 id 의 events 정산 조건(북 기간 종료 id)")
    A("")
    A(md_table(s4["evh"], index=False))
    A("")
    nc = s4["evh"][s4["evh"].cacheUnitStatus.astype(str).str.contains("no_cache_successor")]
    if len(nc):
        A(f"주: cacheUnitStatus `no_cache_successor_at_settlement` {len(nc)}건({', '.join(nc.symbol.astype(str))}) 은 승계사가 정산 bar 에 캐시에 없어 엔진이 주식 leg 를 가격으로 못 매기고 settlementReturn(return_col) 로 정산 — 조건은 있으나 승계사 가격 기반이 아님.")
        A("")
    A("### 4-4. 월별")
    A("")
    A(md_table(s4["mon"]))
    A("")
    A("## 5. 숫자 불일치 점검·정정")
    A("")
    A(md_table(s5, index=False))
    A("")
    A("정정 결론: ① manifest `steps.calendar.returns_rebuilt.retFlag`(412 등) 는 lifecycle 꼬리 제거 **전** 집계 — 발행본 returns_total 의 최종 수치는 recount summary·본 재계산(410 등). "
      "② 584행 '잔여 272' = still_flag4 271 + 행 삭제 1; flag 4 총수 410 = 271 + 새 139. ③ 문서 §10-3 의 '새 flag 4 138(70+54+14)' 은 이전 빌드 값 → 139(70+55+14) 로 정정. "
      "④ 원장 status 는 전체 행 기준(문서)과 stitched_copy 제외 기준(recount) 두 가지가 병존 — 기준을 병기.")
    A("")
    (out / "exposure_report.md").write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", type=Path, default=Path("/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d"))
    ap.add_argument("--rc2", type=Path, default=Path("/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d.prev-20260917T0530Z-rc2"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--book", default=BOOK_GLOB)
    args = ap.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    root = args.build
    man = json.load(open(root / "manifest.json"))
    asof = pd.Timestamp(man["asOfDate"])
    mb = load_membership(root)
    mb2 = load_membership(args.rc2)
    if not mb.sort_values(["universe", "tradingItemId", "fromDate"]).reset_index(drop=True).equals(mb2.sort_values(["universe", "tradingItemId", "fromDate"]).reset_index(drop=True)):
        log("WARNING: rc2 membership differs from rc2f membership")
    log("rc2 returns")
    rc2_tot, R2_info, R2 = rc2_universe_counts(args.rc2, mb)
    log("rc2f returns")
    R = Returns(root / "derived" / "returns_total.parquet")
    masks = universe_masks(R, mb)
    log("section 1")
    by_year, tot, cat, R_info = section1_cells(R, root, masks, out, rc2_tot)
    log("section 2")
    s2 = section2_flag4(R, R2, root, masks, out)
    del R2
    reg = pd.read_csv(root / "qa" / "rc2f" / "registry" / "registry_final.csv", low_memory=False)
    cover = registry_cover(s2["pub4"], reg, root, out, s2["new4"])
    log("book")
    book_file, pv, long = load_book(args.book)
    log("section 3")
    s3 = section3_unresolved(reg, long, out)
    log("section 4")
    s4 = section4_book(R, root, masks, cat, book_file, pv, long, asof, reg, out)
    log("section 5")
    s5 = section5_numbers(root, R_info, R2_info, s2, tot, reg, out)
    write_report(out, args, R_info, R2_info, by_year, tot, s2, s3, s4, s5, cover)
    json.dump({"R_info": R_info, "R2_info": R2_info, "check_584": s2["check"], "book_info": s4["info"], "at": dt.datetime.now().isoformat(timespec="seconds"),
               "build": str(root), "rc2": str(args.rc2)}, open(out / "exposure_info.json", "w"), indent=1, ensure_ascii=False, default=str)
    log("done", out / "exposure_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
