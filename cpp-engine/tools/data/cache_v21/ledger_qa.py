#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/ledger_qa.py - R1: events/ca_ledger.parquet 불변식 self-test·상태 분포표·오라클 행 검사(rc2 BRIEF §3-1, TASKS R1 Goal).

검사(각각 반례 fixture 로 "실패를 판정할 수 있음"을 먼저 보인다: --self-test)
  Q1 unique_key     : merger_* 를 제외한 행에서 (tradingItemId, exDate, legType, childTradingItemId) 유일, 전체에서 (…, eventType) 유일,
                      eventId 유일; 같은 날 현금배당과 금액이 같은 merger_cash 행 없음(진짜 중복은 제거됨, F1)
  Q2 schema         : 계약 컬럼·타입·enum(eventType/legType/status/childValueSource), exDate 자정, 상태·플래그 정합
                      (unresolved 값 행은 NaN, ok stock 행은 값 있음, isEstimate ⇔ estimated 또는 ③④)
  Q3 split_repro    : factors/split.parquet 의 계수 경계(≥ 2000-01-01, 계수 변화) 전수가 quantity 행으로 존재하고
                      parentFactor == factor_prev/factor_next (1e-12); 원장 quantity 행(split/stock_dividend) 도 전부 경계에 대응
  Q4 cash_sum       : dividendId 단위 전수 대조 — 원장 현금 행(evidence 의 dividendId 목록)의 cashPerShare == Σ ciqdividend 세전
                      (ledger_repairs.gross_first 규칙 R0~R5) × fx (1e-6); 원장에 없는 dividendId 는 전부 dropped 파일(계보 복사본·
                      정산 중복·rescinded) 에 있어야 함. 세 값(div/net/gross)이 서로 다른 행은 정책 검토 목록(pretax_policy_review.csv)
  Q9 attribution    : 현금·주식·권리 leg 는 exDate 에 거래되던 id 에만(아니면 notes 표시), lineage 쌍 양쪽에 같은 사건 0(F4)
  Q10 gap_markers   : 벤더 스텝만 있는 셀(R2 목록) 중 ciqdividend 원천 없는 것은 전부 unresolved 표시 행 존재
  Q5 same_day_legs  : 같은 (id, exDate) 에 현금 leg 와 stock leg 가 있으면 두 행(합쳐지지 않음)
  Q6 calendar       : exDate 가 거래일 캘린더 안(범위 안 날짜), 옮긴 행은 notes 에 exDate_moved_from
  Q7 oracle_rows    : DD 2025-11-03 childValuePerOldShare 48.50, HON 2026-06-29 110.095 + split 0.5, EBAY 2015-07-20 PYPL,
                      TYC 314828302 2007-07-02·2012-10-01 두 leg, COST 2020-12-01 cash 10.0, CRH 세전 0.37, PBCT divfactor_scale
                      repairOf, CTRA→DVN 0.7, EXAS 2026-03-24·EA 2026-08-05, FNF→FG 0.06, GME 워런트 조건, CAR/HPQ/XRX/LCAP.A/T 자회사 종가
  Q8 repairs_log    : ca_ledger_repairs.csv 스키마·원장 repairOf 와의 대응
출력: <out>/ledger_qa.md (표), ledger_qa.json, unresolved_top500.csv, status_by_type_universe.csv

CLI:
  ledger_qa.py --ledger <stage>/events/ca_ledger.parquet --root <rc1 root> [--out <stage>/qa]
  ledger_qa.py --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ledger_build import (EVENT_TYPES, LEDGER_COLUMNS, LEG_TYPES, START, STATUSES, VALUE_SOURCES,  # noqa: E402
                          Calendar, split_boundaries, universe_flags)
from ledger_build import MERGER_TYPES, R2_GAP_CANDIDATES, RawCloses, DEFAULT_BASE  # noqa: E402
from ledger_build import (SAME_SECURITY_LINK_OVERRIDES, TRANSITION_COPY_REASON, IMPLAUSIBLE_CHECK_MIN, IMPLAUSIBLE_DROP_MAX,  # noqa: E402
                          IMPLAUSIBLE_JUMP, same_security_pairs, stitched_same_series)
from ledger_repairs import gross_first  # noqa: E402

ORACLES = [
    # (label, tradingItemId, exDate, legType, child or None, field, expected, tol)
    ("DD 2025-11-03 Q 0.5 x 97.00 = 48.50", 383332720, "2025-11-03", "stock", 1956462106, "childValuePerOldShare", 48.50, 1e-6),
    ("HON 2026-06-29 HONA 0.5 x 220.19 = 110.095", 2618950, "2026-06-29", "stock", 2005551375, "childValuePerOldShare", 110.095, 1e-3),
    ("HON 2026-06-29 reverse split parentFactor 0.5", 2618950, "2026-06-29", "quantity", None, "parentFactor", 0.5, 1e-12),
    ("EBAY 2015-07-20 PYPL 1.0 x 40.47", 2608344, "2015-07-20", "stock", 306093931, "childValuePerOldShare", 40.47, 1e-2),
    ("TYC 314828302 2007-07-02 COV leg 0.25", 314828302, "2007-07-02", "stock", 34969538, "childRatio", 0.25, 1e-12),
    ("TYC 314828302 2007-07-02 TEL leg 0.25 (repaired child)", 314828302, "2007-07-02", "stock", 34969541, "childRatio", 0.25, 1e-12),
    ("TYC 314828302 2007-07-02 reverse split 0.25", 314828302, "2007-07-02", "quantity", None, "parentFactor", 0.25, 1e-12),
    ("TYC 314828302 2012-10-01 ADT leg 0.5", 314828302, "2012-10-01", "stock", 217292918, "childRatio", 0.5, 1e-12),
    ("TYC 314828302 2012-10-01 PNR leg 0.239943 (Tyco 8-K 2012-10-01 Ex.99.1; vendor 0.2339943 was a typo, rc2f X2-266)", 314828302, "2012-10-01", "stock", 2638398, "childRatio", 0.239943, 1e-6),
    ("COST 2020-12-01 special 10.0", 2603626, "2020-12-01", "cash", None, "cashPerShare", 10.0, 1e-12),
    ("CRH 2025-03-14 pre-tax 0.37 (vendor 0.2775 after 25% DWT)", 6183504, "2025-03-14", "cash", None, "cashPerShare", 0.37, 1e-12),
    ("CTRA 2026-05-07 -> DVN 0.7", 2597012, "2026-05-07", "stock", 2605788, "childRatio", 0.7, 1e-12),
    ("EXAS cash merger 105 settled 2026-03-24", 2610459, "2026-03-24", "cash", None, "cashPerShare", 105.0, 1e-12),
    ("EA cash merger 210 settled 2026-08-05", 2608773, "2026-08-05", "cash", None, "cashPerShare", 210.0, 1e-12),
    ("FNF 2025-12-17 FG 0.06", 266673936, "2025-12-17", "stock", 1814968902, "childRatio", 0.06, 1e-12),
    ("FNF 2025-12-17 cash 0.52 (same-day cash leg)", 266673936, "2025-12-17", "cash", None, "cashPerShare", 0.52, 1e-12),
    ("BDX 2026-02-10 WAT 0.135", 2593888, "2026-02-10", "stock", 2663488, "childRatio", 0.135, 1e-12),
    ("GME 2025-10-03 warrant 0.1 @ 32", 24268335, "2025-10-03", "rights", 1962567035, "subscriptionPrice", 32.0, 1e-12),
    ("CMCSA 2026-01-05 VSNT 0.04", 2601563, "2026-01-05", "stock", 1967255253, "childRatio", 0.04, 1e-12),
    # rc2 재작업(리뷰 F1/F2/F4, R2 §8-4)
    ("IAC/MTCH 2020-07-01 rename 47439862->433968949 parentFactor 2.1584 (lineage v2, = old-id split step)", 47439862, "2020-07-01", "quantity", 433968949, "parentFactor", 2.1584, 1e-9),
    ("IAC/MTCH 2020-07-01 new IAC leg keyed to the OLD id 47439862, per old share 1.0 x 106.71 (lead decision: transition legs on the settling id)", 47439862, "2020-07-01", "stock", 674599883, "childValuePerOldShare", 106.71, 1e-2),
    ("IAC 2008-08-21 HSNI 0.2 x 15.29", 47439862, "2008-08-21", "stock", 47439823, "childValuePerOldShare", 3.058, 1e-3),
    ("IAC 2008-08-21 ILG 0.2 (parser NaN -> text)", 47439862, "2008-08-21", "stock", 47440078, "childRatio", 0.2, 1e-12),
    ("IAC 2008-08-21 TKTM 0.2", 47439862, "2008-08-21", "stock", 47440956, "childRatio", 0.2, 1e-12),
    ("IAC 2008-08-21 TREE 1/30", 47439862, "2008-08-21", "stock", 47441482, "childRatio", 1.0 / 30, 1e-6),
    ("VIA.B 2006-01-03 relist 2662339->24993622 ratio 0.5", 2662339, "2006-01-03", "quantity", 24993622, "parentFactor", 0.5, 1e-12),
    ("VIA.B 2006-01-03 new Viacom leg keyed to the OLD id 2662339, per old share 0.5 x 41.59", 2662339, "2006-01-03", "stock", 24993638, "childValuePerOldShare", 20.795, 1e-3),
    ("QRTE.A 2014-10-15 Liberty Ventures 0.14217 (R3 hand-over; keyed to the old id LINT.A 204606934, rename 1:1)", 204606934, "2014-10-15", "stock", 215503912, "childRatio", 0.14217, 1e-12),
    ("SLM 2014-05-01 NAVI leg keyed to the OLD id 2644409 (multi-leg split path)", 2644409, "2014-05-01", "stock", None, "childRatio", None, None),
    ("HSH 2014-08-29 merger cash 63.0 kept next to the 0.175 dividend (F1)", 2648908, "2014-08-29", "cash", None, "cashPerShare", 63.0, 1e-12, "merger_cash"),
    ("HSH 2014-08-29 dividend 0.175 kept as its own row (F1)", 2648908, "2014-08-29", "cash", None, "cashPerShare", 0.175, 1e-12, "cash_dividend"),
    ("VVC 2019-02-01 merger cash 72.0 kept (F1)", 2662033, "2019-02-01", "cash", None, "cashPerShare", 72.0, 1e-12, "merger_cash"),
    ("WGL 2018-07-09 merger cash 88.25 kept (F1)", 2663074, "2018-07-09", "cash", None, "cashPerShare", 88.25, 1e-12, "merger_cash"),
    ("DD 2019-06-03 CTVA leg on the trading id 383332720 (F4)", 383332720, "2019-06-03", "stock", 588604057, "childRatio", 1.0 / 3, 1e-6),
    ("Lumina 2005-05-19 leg event_value 3.0/3 on the old id 9897490 (F2a vendor repeats merged; transition leg old-id keyed)", 9897490, "2005-05-19", "stock", 361373573, "childValuePerOldShare", 1.0, 1e-9),
]
ORACLE_ABSENT = [  # (label, id, exDate, legType, child or None) — 존재하면 안 되는 행
    ("IAC/MTCH 2020-07-01 self-conversion leg (47.721) removed", 47439862, "2020-07-01", "stock", None),
    ("MTCH 433968949 2020-07-01 self-conversion leg removed", 433968949, "2020-07-01", "stock", None),
    ("MTCH 433968949 2020-07-01 successor copy of the new-IAC leg removed (old-id keyed)", 433968949, "2020-07-01", "stock", 674599883),
    ("VIA.B 2006-01-03 'CBS CORP' self-conversion leg removed", 2662339, "2006-01-03", "stock", 2596144),
    ("CBS.B 24993622 2006-01-03 successor copy of the new-Viacom leg removed", 24993622, "2006-01-03", "stock", 24993638),

    ("IAC 2008-08-22 vendor repeat removed", 47439862, "2008-08-22", "stock", 47439823),
    ("Lumina 22396289 (successor first bar) copy removed", 22396289, "2005-05-19", "stock", 361373573),
    ("Lumina 22396289 2005-05-20 vendor repeat removed", 22396289, "2005-05-20", "stock", 361373573),
    ("QRTE.A successor-keyed copy removed (rekeyed to LINT.A)", 326004249, "2014-10-15", "stock", 215503912),
    ("2588856 special_dividend 19.0 duplicate of merger cash removed", 2588856, "2012-02-09", "cash", None),
]
ORACLE_STITCHED = [  # (label, copy id, exDate, legType, child, keeper id) — 복사본 존재·keeper 일치·export 제외
    ("DD old id 2606617 2019-06-03 CTVA leg = stitched copy of 383332720 (exportExclude)", 2606617, "2019-06-03", "stock", 588604057, 383332720),
    ("XRX 610592433 2017-01-03 CNDT leg = stitched copy of 2665260 (R2 oracle row)", 610592433, "2017-01-03", "stock", 412909487, 2665260),
]
ORACLE_TYPE = [  # (label, id, exDate, legType, expected eventType)
    ("13287882 2012-06-19 473.48 settlement -> merger_cash (R2 §8-4)", 13287882, "2012-06-19", "cash", "merger_cash"),
    ("6179201 2006-06-02 61.189 liquidation -> merger_cash", 6179201, "2006-06-02", "cash", "merger_cash"),
    ("2666583 2023-02-21 liquidation -> merger_cash", 2666583, "2023-02-21", "cash", "merger_cash"),
    ("28994356 2009-12-10 settlement -> merger_cash", 28994356, "2009-12-10", "cash", "merger_cash"),
]
ORACLE_RETURNS = [  # (label, parent_prev_id, prevDate, apply rows of [(id, exDate)], expected cash-style return, tol) — closes from base_data
    ("DD 2025-11-03 cash-style +1.89%", 383332720, "2025-10-31", [(383332720, "2025-11-03")], 0.0189, 2e-4),
    ("HON 2026-06-29 cash-style -3.54%", 2618950, "2026-06-26", [(2618950, "2026-06-29")], -0.0354, 2e-4),
    ("IAC/MTCH 2020-07-01 cash-style +3.5% (old 323.40 -> 2.1584 MTCH 105.67 + 1 IAC 106.71)", 47439862, "2020-06-30", [(47439862, "2020-07-01"), (433968949, "2020-07-01")], 0.0353, 2e-3),
    ("VIA.B 2006-01-03 cash-style (0.5 x CBS + 0.5 x new Viacom) / 32.60 - 1", 2662339, "2005-12-30", [(2662339, "2006-01-03"), (24993622, "2006-01-03")], None, None),
    ("TYC 314828302 2007-07-02 cash-style ~+1.2%", 314828302, "2007-06-29", [(314828302, "2007-07-02")], 0.012, 5e-3),
]
ORACLE_SOURCES = [  # (label, id, exDate, child, expected childValueSource)
    ("CAR 2006-08-01 H leg by child close", 28942786, "2006-08-01", 28067777, "child_close_ex"),
    ("CAR 2006-08-01 TNL leg by child close", 28942786, "2006-08-01", 28067782, "child_close_ex"),
    ("HPQ 2015-11-02 HPE by child close", 2618412, "2015-11-02", 313064514, "child_close_ex"),
    ("XRX 2017-01-03 CNDT by child close", 2665260, "2017-01-03", 412909487, "child_close_ex"),
    ("LCAP.A 2008-03-04 LMDI.A by child close", 27059655, "2008-03-04", 41863782, "child_close_ex"),
    ("T 2002-11-19 CMCSA by child close", 2586324, "2002-11-19", 2601563, "child_close_ex"),
    ("TYC 314828302 2007-07-02 TEL by child close", 314828302, "2007-07-02", 34969541, "child_close_ex"),
    ("TYC 314828302 2012-10-01 ADT by child close", 314828302, "2012-10-01", 217292918, "child_close_ex"),
    ("GME 2025-10-03 warrant value unknown (no price, undelivered)", 24268335, "2025-10-03", 1962567035, "unknown"),
]


def _md(df: pd.DataFrame, floatfmt: str = "{:.6g}") -> str:
    if df is None or len(df) == 0:
        return "_(없음)_"
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in df.itertuples(index=False):
        cells = []
        for v in r:
            if isinstance(v, float):
                cells.append("NaN" if not np.isfinite(v) else floatfmt.format(v))
            elif isinstance(v, (pd.Timestamp, np.datetime64)):
                cells.append("NaT" if pd.isna(v) else str(pd.Timestamp(v).date()))
            else:
                cells.append(str(v).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# 개별 검사(각각 {"ok", "detail"} 반환)
# --------------------------------------------------------------------------------------
DIVIDEND_TYPES = ("cash_dividend", "special_dividend")


def q1_unique_key(L: pd.DataFrame) -> dict:
    """§3-1 키 (tradingItemId, exDate, legType, childTradingItemId) 유일성 게이트(raw 원장 전체, stitched_copy 포함 — 복사본은 다른 id).
    허용되는 유일한 예외 = 리드 F1 결정: 같은 날 현금배당 + corporate_actions 현금 leg(merger_cash/other) 두 행(금액 다름, eventType 으로 구분).
    그 밖의 키 중복(같은 그룹 안, 벤더 마커 vendor_implied 와 실제 행의 공존(R1b N1), 3 행 이상)은 실패."""
    ck = L.childTradingItemId.astype("Int64").fillna(-1)
    non_merger = ~L.eventType.isin(MERGER_TYPES)
    dup_contract = L[non_merger].assign(ck=ck[non_merger]).duplicated(["tradingItemId", "exDate", "legType", "ck"], keep=False)
    dup_all = L.assign(ck=ck).duplicated(["tradingItemId", "exDate", "eventType", "legType", "ck"], keep=False)
    # 계약 키 전수(merger 그룹 포함): 중복 그룹은 {배당 1 행 + merger_cash/other 1 행, 금액 다름} 만 허용
    Lk = L.assign(ck=ck)
    strict = Lk[Lk.duplicated(["tradingItemId", "exDate", "legType", "ck"], keep=False)]
    n_pairs_ok, bad_groups, marker_dups = 0, [], 0
    for _, g in strict.groupby(["tradingItemId", "exDate", "legType", "ck"], sort=False):
        et = set(g.eventType)
        is_pair = len(g) == 2 and len(et & set(DIVIDEND_TYPES)) == 1 and len(et & {"merger_cash", "other"}) == 1
        amounts = g.cashPerShare.astype(float).to_numpy()
        distinct_amt = bool(np.isfinite(amounts).all() and abs(amounts[0] - amounts[1]) > 1e-6 * max(1.0, abs(amounts[0]))) if len(g) == 2 else False
        has_marker = bool((g.source == "vendor_implied").any())
        if is_pair and distinct_amt and not has_marker:
            n_pairs_ok += 1
        else:
            marker_dups += int(has_marker)
            bad_groups.append(g)
    # F1: 같은 날(±5일) 현금배당과 금액이 같은 merger_cash 는 진짜 중복 → 있으면 안 됨; 금액이 다르면 두 행(허용, 개수만)
    mc = L[(L.eventType == "merger_cash") & (L.legType == "cash")]
    dv = L[L.eventType.isin(["cash_dividend", "special_dividend"])]
    same_amt = 0
    both = 0
    if len(mc) and len(dv):
        m = mc.merge(dv[["tradingItemId", "exDate", "cashPerShare"]], on="tradingItemId", suffixes=("", "_d"))
        m = m[(m.exDate - m.exDate_d).abs().dt.days <= 5]
        both = int(m.drop_duplicates(["tradingItemId", "exDate"]).shape[0])
        same_amt = int((np.abs(m.cashPerShare - m.cashPerShare_d) <= 1e-6 * np.maximum(1.0, m.cashPerShare.abs())).sum())
    bad = pd.concat(bad_groups, ignore_index=True) if bad_groups else pd.DataFrame(columns=list(L.columns) + ["ck"])
    return {"ok": bool(not dup_contract.any() and not dup_all.any() and L.eventId.is_unique and same_amt == 0 and len(bad) == 0),
            "duplicate_contract_key_rows(non_merger)": int(dup_contract.sum()), "duplicate_key_rows_with_eventType": int(dup_all.sum()),
            "contract_key_dup_rows(all rows)": int(len(strict)), "contract_key_pairs_allowed(dividend+merger_cash/other, distinct amounts; F1)": n_pairs_ok,
            "contract_key_dup_rows_not_allowed": int(len(bad)), "vendor_marker_next_to_real_row(N1)": marker_dups,
            "duplicate_eventId": int(L.eventId.duplicated().sum()), "merger_cash_next_to_dividend(distinct amounts, kept)": both,
            "merger_cash_same_amount_as_dividend(true duplicate)": same_amt,
            "examples": pd.concat([L[non_merger][dup_contract.reindex(L.index[non_merger]).fillna(False).to_numpy()], bad[L.columns]], ignore_index=True)[
                ["eventId", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "source"]].head(6).astype(str).to_dict("records")}


def q2_schema(L: pd.DataFrame) -> dict:
    v = {}
    if list(L.columns) != LEDGER_COLUMNS:
        v["columns"] = [c for c in LEDGER_COLUMNS if c not in L.columns] + [c for c in L.columns if c not in LEDGER_COLUMNS]
    for c, t in (("tradingItemId", "int64"), ("exDate", "datetime64[ns]"), ("cashPerShare", "float64"), ("parentFactor", "float64"),
                 ("childRatio", "float64"), ("childValuePerOldShare", "float64"), ("isEstimate", "bool"), ("childTradingItemId", "Int64")):
        if str(L[c].dtype) != t:
            v[f"dtype_{c}"] = str(L[c].dtype)
    for c, allowed in (("eventType", EVENT_TYPES), ("legType", LEG_TYPES), ("status", STATUSES), ("childValueSource", VALUE_SOURCES)):
        bad = set(L[c].dropna()) - set(allowed)
        if bad:
            v[f"enum_{c}"] = sorted(bad)
    if (L.exDate.dt.normalize() != L.exDate).any():
        v["exDate_not_midnight"] = int((L.exDate.dt.normalize() != L.exDate).sum())
    if L.exDate.isna().any():
        v["exDate_nat"] = int(L.exDate.isna().sum())
    if ((L.parentFactor <= 0) | ~np.isfinite(L.parentFactor)).any():
        v["parentFactor_nonpositive"] = int(((L.parentFactor <= 0) | ~np.isfinite(L.parentFactor)).sum())
    value_leg = L.legType.isin(["stock", "rights"])
    ok_no_value = value_leg & (L.status == "ok") & ~np.isfinite(L.childValuePerOldShare)
    if ok_no_value.any():
        v["ok_value_leg_without_value"] = int(ok_no_value.sum())
    unres_with_value = value_leg & (L.status == "unresolved") & np.isfinite(L.childValuePerOldShare)
    if unres_with_value.any():
        v["unresolved_value_leg_with_value"] = int(unres_with_value.sum())
    cash_leg = L.legType == "cash"
    ok_cash_nan = cash_leg & (L.status == "ok") & ~np.isfinite(L.cashPerShare)
    if ok_cash_nan.any():
        v["ok_cash_leg_without_amount"] = int(ok_cash_nan.sum())
    est_mismatch = (L.childValueSource.isin(["event_value", "vendor_implied"]) & ~L.isEstimate)
    if est_mismatch.any():
        v["P10_③④_without_isEstimate"] = int(est_mismatch.sum())
    wi_est = (L.childValueSource == "wi_close") & L.isEstimate
    if wi_est.any():
        v["wi_close_with_isEstimate"] = int(wi_est.sum())
    unres_est = (L.status == "unresolved") & L.isEstimate
    if unres_est.any():
        v["unresolved_with_isEstimate"] = int(unres_est.sum())
    nonvalue_with_child_value = ~value_leg & np.isfinite(L.childValuePerOldShare)
    if nonvalue_with_child_value.any():
        v["non_value_leg_with_child_value"] = int(nonvalue_with_child_value.sum())
    quantity_pf1 = (L.legType == "quantity") & L.eventType.isin(["split", "stock_dividend"]) & (L.parentFactor == 1.0)
    if quantity_pf1.any():
        v["split_row_with_parentFactor_1"] = int(quantity_pf1.sum())
    nonq_pf = (L.legType != "quantity") & (L.parentFactor != 1.0)
    if nonq_pf.any():
        v["non_quantity_row_with_parentFactor_ne_1"] = int(nonq_pf.sum())
    return {"ok": not v, "violations": v}


def q3_split_repro(L: pd.DataFrame, split: pd.DataFrame, cal: Calendar) -> dict:
    b = split_boundaries(split)
    b["pf"] = b.factorPrev / b.factorNext
    ex, _ = cal.align(b.validFrom.to_numpy())
    b["exDate"] = ex
    q = L[(L.legType == "quantity") & L.eventType.isin(["split", "stock_dividend"])][["tradingItemId", "exDate", "parentFactor", "status"]]
    # 같은 날 rename/relist 행이 옛 id 의 분할 스텝을 대표하면(notes represents_split_step) 그 행이 경계를 재현한다(R2-2: 수량 전환 1회)
    rl = L[(L.legType == "quantity") & L.eventType.isin(["rename", "relist"]) & L.notes.str.contains("represents_split_step", na=False)][["tradingItemId", "exDate", "parentFactor", "status"]]
    q, rl = q.copy(), rl.copy()
    q.attrs, rl.attrs = {}, {}
    qq = pd.concat([q, rl], ignore_index=True)
    m = b.merge(qq, on=["tradingItemId", "exDate"], how="outer", indicator=True)
    missing = m[m._merge == "left_only"]
    extra = m[m._merge == "right_only"]
    both = m[m._merge == "both"]
    bad = both[np.abs(both.pf - both.parentFactor) > 1e-12 * np.maximum(1.0, both.pf.abs())]
    return {"ok": bool(len(missing) == 0 and len(extra) == 0 and len(bad) == 0), "boundaries": int(len(b)), "ledger_rows": int(len(q)),
            "boundaries_represented_by_rename_row": int(len(rl)),
            "boundaries_without_row": int(len(missing)), "rows_without_boundary": int(len(extra)), "ratio_mismatch": int(len(bad)),
            "examples": pd.concat([missing.head(3), extra.head(3), bad.head(3)])[["tradingItemId", "exDate", "pf", "parentFactor", "_merge"]].astype(str).to_dict("records"),
            "status_of_split_rows": q.status.value_counts().to_dict()}


def q4_cash_sum(L: pd.DataFrame, dv: pd.DataFrame, cal: Calendar, repairs: pd.DataFrame | None, dropped: pd.DataFrame | None = None,
                out_dir: Path | None = None, stitched: pd.DataFrame | None = None) -> dict:
    """dividendId 단위 전수 대조(F4 재귀속과 무관): 원장 현금 행의 cashPerShare == Σ 그 행 evidence 의 dividendId 들의 세전 금액 × fx.
    원장에 없는 dividendId 는 dropped(계보 복사본·정산 중복) 또는 rescinded 여야 한다. 정책 검토 목록(F14)도 여기서 쓴다."""
    from ledger_build import CASH_TYPES, USD
    c = dv[(dv.exDate >= START) & dv.dividendTypeId.isin(CASH_TYPES) & dv.divAmount.notna() & (dv.divAmount > 0)].copy()
    n_source = int(len(c))
    c, _ = gross_first(c)                       # R0 rescinded 제외, amountLocal·amountRule
    fx = np.where(c.currencyId.fillna(USD).astype(int) == USD, 1.0, 1.0 / c.fxRate.astype(float))
    c["usd"] = c.amountLocal * fx
    exp_by_id = dict(zip(c.dividendId.astype(int), c.usd))
    rule_by_id = dict(zip(c.dividendId.astype(int), c.amountRule))
    cash = L[(L.legType == "cash") & (L.source == "ciqdividend") & L.evidence.str.contains("dividendId=", na=False)]
    cash = pd.concat([cash, L[(L.legType == "cash") & (L.source == "corporate_actions") & L.evidence.str.startswith("dividendId=", na=False)]])   # 정산성 재분류 행
    copies = L[(L.legType == "cash") & (L.source == "stitched_copy") & L.evidence.str.contains("dividendId=", na=False)]                          # 계보 복사본(keeper 와 동일; 존재만 인정)
    # rc2f: CSV 교정(repairOf csv_override:<id>)으로 금액·상태가 바뀐 ciqdividend 행 — 의도된 교정(repairs 로그에 근거)이므로 존재만 인정, 금액 대조 제외
    csv_rep = L[(L.legType == "cash") & (L.source == "manual_override") & L.repairOf.fillna("").astype(str).str.contains("csv_override:") & L.evidence.str.contains("dividendId=", na=False)]
    seen_copy = set()
    for ev_ in pd.concat([copies.evidence, csv_rep.evidence]):
        for x in str(ev_).split("dividendId=")[1].split(";")[0].split(" ")[0].split(","):
            if x.strip().lstrip("-").isdigit():
                seen_copy.add(int(x))
    seen, bad, n_rows = set(), [], 0
    for r in cash.itertuples(index=False):
        ids_txt = str(r.evidence).split("dividendId=")[1].split(";")[0].split(" ")[0]
        if ids_txt == "-1":
            continue
        ids_ = [int(x) for x in ids_txt.split(",") if x.strip().lstrip("-").isdigit()]
        if not ids_:
            continue
        n_rows += 1
        seen.update(ids_)
        exp = float(np.nansum([exp_by_id.get(i, np.nan) for i in ids_]))
        got = float(r.cashPerShare) if pd.notna(r.cashPerShare) else np.nan
        if "per_new_share_conversion=" in str(r.notes):          # 전환일 leg: 새 주 기준으로 환산된 값(F4) → 구주 기준으로 되돌려 대조
            conv = float(str(r.notes).split("per_new_share_conversion=")[1].split("(")[0])
            got = got * conv if np.isfinite(got) else got
        if np.isnan(got) and all(np.isnan(exp_by_id.get(i, np.nan)) for i in ids_):
            continue
        if not (abs(exp - got) <= 1e-6 * max(1.0, abs(exp))):
            bad.append({"eventId": r.eventId, "expected": exp, "got": got, "dividendIds": ids_txt})
    missing = set(exp_by_id) - seen - seen_copy
    dropped_ids = set()
    for fr in (dropped, stitched):                       # 드롭 파일 + 계보 복사본 표(복사본 원천 dividendId 는 keeper 와 다른 벤더 행)
        if fr is not None and len(fr) and "evidence" in fr.columns:
            for ev in fr.evidence.dropna():
                if "dividendId=" in str(ev):
                    for x in str(ev).split("dividendId=")[1].split(";")[0].split(" ")[0].split(","):
                        if x.strip().lstrip("-").isdigit():
                            dropped_ids.add(int(x))
    unexplained_missing = sorted(missing - dropped_ids)
    # F14: 벤더 세 값이 서로 다른 행 → 정책 검토 목록
    review = c[(c.grossAmount.notna() & (np.abs(c.grossAmount - c.divAmount) > 1e-9)) | (c.netAmount.notna() & (np.abs(c.netAmount - c.divAmount) > 1e-9))]
    if out_dir is not None:
        review[["dividendId", "tradingItemId", "symbol", "exDate", "divAmount", "netAmount", "grossAmount", "currencyId", "supplementalTypeName",
                "companyName", "amountLocal", "amountRule", "amountNote"]].to_csv(Path(out_dir) / "pretax_policy_review.csv", index=False)
    return {"ok": bool(len(bad) == 0 and len(unexplained_missing) == 0), "source_rows": n_source, "source_rows_after_rescinded": int(len(c)), "csv_override_cash_rows": int(len(csv_rep)),
            "ledger_cash_rows_with_dividendId": n_rows, "dividendIds_in_ledger": int(len(seen)), "mismatched_rows": int(len(bad)),
            "dividendIds_only_in_stitched_copies": int(len(seen_copy - seen)),
            "dividendIds_missing_from_ledger": int(len(missing)), "of_which_in_dropped_or_stitched_file": int(len(missing & dropped_ids)),
            "unexplained_missing": len(unexplained_missing), "unexplained_examples": unexplained_missing[:5],
            "sum_expected_usd_per_share": round(float(np.nansum(list(exp_by_id.values()))), 4),
            "sum_ledger_cash_rows_with_dividendId": round(float(cash.cashPerShare.sum()), 4),
            "policy_review_rows": int(len(review)), "policy_rules": pd.Series(rule_by_id).value_counts().to_dict(),
            "examples": bad[:5]}


def q5_same_day(L: pd.DataFrame) -> dict:
    k = L.groupby(["tradingItemId", "exDate"]).legType.agg(lambda x: set(x))
    both = k[k.map(lambda s: "cash" in s and "stock" in s)]
    n_rows = L.set_index(["tradingItemId", "exDate"]).loc[both.index].groupby(level=[0, 1]).size() if len(both) else pd.Series(dtype=int)
    return {"ok": bool((n_rows >= 2).all()) if len(both) else True, "events_with_cash_and_stock": int(len(both)),
            "min_rows_in_those_events": int(n_rows.min()) if len(n_rows) else None,
            "examples": [f"{a}_{pd.Timestamp(b).date()}" for a, b in both.index[:5]]}


def q6_calendar(L: pd.DataFrame, cal: Calendar) -> dict:
    d = L.exDate.to_numpy().astype("datetime64[D]")
    inside = (d >= cal.first) & (d <= cal.last)
    on_cal = np.isin(d[inside], cal.days)
    moved = L.notes.str.contains("exDate_moved_from", na=False)
    return {"ok": bool(on_cal.all()), "rows_inside_calendar": int(inside.sum()), "rows_off_calendar": int((~on_cal).sum()),
            "rows_beyond_calendar": int((~inside).sum()), "rows_moved_to_next_trading_day": int(moved.sum())}


def q7_oracles(L: pd.DataFrame) -> dict:
    rows = []
    ok_all = True
    for o in ORACLES:
        label, tid, ex, leg, child, field, expected, tol = o[:8]
        m = (L.tradingItemId == tid) & (L.exDate == pd.Timestamp(ex)) & (L.legType == leg)
        if len(o) > 8:
            m &= L.eventType == o[8]
        if child is not None:
            m &= L.childTradingItemId.astype("Int64").fillna(-1) == child
        x = L[m]
        got = float(x[field].iloc[0]) if len(x) and pd.notna(x[field].iloc[0]) else np.nan
        ok = len(x) >= 1 and (expected is None or (len(x) == 1 and np.isfinite(got) and abs(got - expected) <= tol))
        ok_all &= ok
        rows.append({"oracle": label, "field": field, "expected": expected, "got": got, "status": x.status.iloc[0] if len(x) else "ROW MISSING",
                     "source": x.childValueSource.iloc[0] if len(x) and leg in ("stock", "rights") else "", "ok": "PASS" if ok else "FAIL"})
    for label, tid, ex, child, expected_src in ORACLE_SOURCES:
        m = (L.tradingItemId == tid) & (L.exDate == pd.Timestamp(ex)) & (L.childTradingItemId.astype("Int64").fillna(-1) == child)
        x = L[m]
        got = x.childValueSource.iloc[0] if len(x) else "ROW MISSING"
        ok = len(x) == 1 and got == expected_src
        ok_all &= ok
        rows.append({"oracle": label, "field": "childValueSource", "expected": expected_src, "got": got,
                     "status": x.status.iloc[0] if len(x) else "ROW MISSING",
                     "source": f"value={x.childValuePerOldShare.iloc[0]:.6g}" if len(x) else "", "ok": "PASS" if ok else "FAIL"})
    for label, tid, ex, leg, child in ORACLE_ABSENT:
        m = (L.tradingItemId == tid) & (L.exDate == pd.Timestamp(ex)) & (L.legType == leg)
        if child is not None:
            m &= L.childTradingItemId.astype("Int64").fillna(-1) == child
        else:
            m &= L.childTradingItemId.isna() if leg == "stock" else m
        n = int(m.sum())
        ok = n == 0
        ok_all &= ok
        rows.append({"oracle": label, "field": "row_absent", "expected": 0, "got": n, "status": "", "source": "", "ok": "PASS" if ok else "FAIL"})
    for label, tid, ex, leg, child, keeper in ORACLE_STITCHED:
        m = (L.tradingItemId == tid) & (L.exDate == pd.Timestamp(ex)) & (L.legType == leg) & (L.childTradingItemId.astype("Int64").fillna(-1) == child)
        x = L[m]
        ok = len(x) == 1 and x.source.iloc[0] == "stitched_copy" and bool(x.exportExclude.iloc[0]) and str(x.keeperEventId.iloc[0]).startswith(f"{keeper}_")
        ok_all &= ok
        rows.append({"oracle": label, "field": "stitched_copy", "expected": f"copy of {keeper}, exportExclude", "got": f"{x.source.iloc[0]}/{x.keeperEventId.iloc[0]}" if len(x) else "ROW MISSING",
                     "status": x.status.iloc[0] if len(x) else "", "source": "", "ok": "PASS" if ok else "FAIL"})
    for label, tid, ex, leg, et in ORACLE_TYPE:
        x = L[(L.tradingItemId == tid) & (L.exDate == pd.Timestamp(ex)) & (L.legType == leg)]
        got = ",".join(sorted(set(x.eventType))) if len(x) else "ROW MISSING"
        ok = got == et
        ok_all &= ok
        rows.append({"oracle": label, "field": "eventType", "expected": et, "got": got, "status": x.status.iloc[0] if len(x) else "", "source": "", "ok": "PASS" if ok else "FAIL"})
    # PBCT 215291786: 2000~2007 벤더 스케일 오류 → 현금 행이 실제 사건이며 repairOf divfactor_scale 로 표시
    p = L[(L.tradingItemId == 215291786) & (L.exDate >= "2000-01-01") & (L.exDate <= "2007-04-13") & (L.legType == "cash")]
    ok = len(p) > 0 and p.repairOf.fillna("").str.contains("divfactor_scale").all() and (p.status == "ok").all()
    ok_all &= ok
    rows.append({"oracle": "PBCT 215291786 2000~2007 cash rows from actual events, vendor scale steps logged (repairOf divfactor_scale)",
                 "field": "repairOf", "expected": f"{len(p)} rows tagged", "got": int(p.repairOf.fillna('').str.contains('divfactor_scale').sum()),
                 "status": "ok" if ok else "FAIL", "source": "", "ok": "PASS" if ok else "FAIL"})
    return {"ok": bool(ok_all), "rows": rows}


def q8_repairs(L: pd.DataFrame, repairs: pd.DataFrame | None) -> dict:
    if repairs is None:
        return {"ok": False, "detail": "repairs csv missing"}
    need = ["tradingItemId", "exDate", "field", "before", "after", "evidence", "source"]
    cols_ok = list(repairs.columns) == need
    tagged = L.repairOf.fillna("")
    tags = pd.Series([t for s in tagged if s for t in s.split(";")])
    kinds = tags.str.split(":").str[0].value_counts().to_dict() if len(tags) else {}
    return {"ok": bool(cols_ok), "columns_ok": cols_ok, "repair_rows": int(len(repairs)), "by_field": repairs.field.value_counts().to_dict(),
            "by_source": repairs.source.value_counts().to_dict(), "ledger_rows_with_repairOf": int((tagged != "").sum()), "repairOf_kinds": kinds}


def q7b_oracle_returns(L: pd.DataFrame, closes) -> dict:
    """현금식 수익률 재현(§3-2 정의): prev 구주 1주 → (prevDate, exDate] 의 leg 적용(같은 날 value leg 먼저, quantity 나중; rename 은 새 id 로 연결)."""
    rows = []
    ok_all = True
    for label, pid, prev_date, apply, expected, tol in ORACLE_RETURNS:
        p_prev = float(closes.at([pid], [pd.Timestamp(prev_date)])[0])
        shares, cash, child, cur_id = 1.0, 0.0, 0.0, pid
        detail = []
        for tid, ex in apply:
            x = L[(L.tradingItemId == tid) & (L.exDate == pd.Timestamp(ex)) & ~L.eventType.isin(MERGER_TYPES)]
            for r in x[x.legType.isin(["cash", "stock", "rights"])].itertuples(index=False):
                if r.legType == "cash" and pd.notna(r.cashPerShare):
                    cash += shares * r.cashPerShare
                    detail.append(f"cash {r.cashPerShare:.4g}")
                elif pd.notna(r.childValuePerOldShare):
                    child += shares * r.childValuePerOldShare
                    detail.append(f"{r.eventType} {r.childTradingItemId} {r.childValuePerOldShare:.4g}")
            for r in x[x.legType == "quantity"].itertuples(index=False):
                shares *= r.parentFactor
                detail.append(f"{r.eventType} x{r.parentFactor:g}")
                if r.eventType in ("rename", "relist") and pd.notna(r.childTradingItemId):
                    cur_id = int(r.childTradingItemId)
        ex_last = pd.Timestamp(apply[-1][1])
        p_ex = float(closes.at([cur_id], [ex_last])[0])
        ret = (shares * p_ex + cash + child) / p_prev - 1.0
        ok = np.isfinite(ret) and (expected is None or abs(ret - expected) <= tol)
        ok_all &= ok
        rows.append({"oracle": label, "field": "cash_style_return", "expected": expected if expected is not None else "finite", "got": round(ret, 6),
                     "status": "; ".join(detail)[:120], "source": f"P_prev {p_prev:.4g} -> {cur_id} {p_ex:.4g}", "ok": "PASS" if ok else "FAIL"})
    return {"ok": bool(ok_all), "rows": rows}


def q9_attribution(L: pd.DataFrame, lifecycle: pd.DataFrame, lineage: pd.DataFrame) -> dict:
    """F4: 값 leg 는 exDate 에 거래되던 id 에만(예외는 notes 표시); lineage 쌍 양쪽에 같은 (exDate, legType, 금액/child) 사건 0."""
    lc = lifecycle.set_index("tradingItemId")
    v = L[L.legType.isin(["cash", "stock", "rights"]) & ~L.eventType.isin(MERGER_TYPES) & ~L.source.isin(["stitched_copy", "vendor_implied"])]
    first = v.tradingItemId.map(lc.firstTradeDate)
    last = v.tradingItemId.map(lc.lastTradeDate)
    if "status" in lc.columns:
        last = last.where(v.tradingItemId.map(lc.status) != "active", pd.NaT)
    tradable = (first.isna() | (v.exDate >= first)) & (last.isna() | (v.exDate <= last))
    # L1 fix 3: rows placed by a rc2f CSV correction (repairOf csv_override:<id>; e.g. X4 secondary legs on the settlement bar = day after
    # lastTradeDate, X3 marker replacements on stitched-history ids) are deliberate, evidenced placements -> marked
    marked = v.notes.str.contains("parent_not_tradable_on_exDate|reassigned_from|transition_day_leg|csv_override", na=False) \
        | v.repairOf.fillna("").astype(str).str.contains("csv_override:", regex=False)
    unmarked_nt = int((~tradable & ~marked).sum())
    pairs = lineage[lineage.relation.isin(["rename", "relist", "merger"])][["oldId", "newId"]].drop_duplicates()
    ck = v.childTradingItemId.astype("Int64").fillna(-1)
    key = v.assign(ck=ck, amt=v.cashPerShare.round(4))
    a = key.merge(pairs, left_on="tradingItemId", right_on="oldId")
    b = key.merge(pairs, left_on="tradingItemId", right_on="newId")
    both = a.merge(b, on=["oldId", "newId", "exDate", "legType"], suffixes=("_o", "_n"))
    same = both[((both.legType == "cash") & (np.abs(both.amt_o - both.amt_n) <= 1e-6)) | ((both.legType != "cash") & (both.ck_o == both.ck_n))]
    # 양쪽 id 가 그 날 모두 tradable(동시 상장 라인)이면 각 라인의 정당한 사건; 한쪽이 tradable 하지 않은데 남아 있으면 위반
    def trad(tid, ex):
        f, l = lc.firstTradeDate.get(tid, pd.NaT), lc.lastTradeDate.get(tid, pd.NaT)
        if "status" in lc.columns and lc.status.get(tid, "") == "active":
            l = pd.NaT
        return (pd.isna(f) or ex >= f) and (pd.isna(l) or ex <= l)
    viol = same[np.array([not (trad(int(o), ex) and trad(int(n), ex)) for o, n, ex in zip(same.oldId, same.newId, same.exDate)], dtype=bool)] if len(same) else same
    # R1c ①: merger 링크 쌍의 비거래 id 복사본은 옮기지도 버리지도 않고 표시(parent_not_tradable_on_exDate;no_same_security_lineage_link)해 둔다 — 표시돼 있으면 위반 아님
    rel = lineage.set_index(["oldId", "newId"]).relation.to_dict() if len(lineage) else {}
    ss = {(a, b) for a, b in same_security_pairs(lineage)} | {(b, a) for a, b in same_security_pairs(lineage)}
    csv_placed = viol.iloc[0:0]
    if len(viol):
        is_ss = np.array([(int(o), int(n)) in ss for o, n in zip(viol.oldId, viol.newId)], dtype=bool)
        mk = np.array([("no_same_security_lineage_link" in str(a)) or ("no_same_security_lineage_link" in str(b)) for a, b in zip(viol.notes_o, viol.notes_n)], dtype=bool)
        merger_marked = viol[~is_ss & mk]
        viol = viol[is_ss | ~mk]
        # L1 fix 3: the copy on the non-trading side placed by a rc2f CSV correction (repairOf csv_override:<id>; X3 mechanism A rows on the stitched
        # successor history, X2/X4 secondary legs) is a deliberate, evidenced placement -> listed, not a violation
        if len(viol):
            nt_o = np.array([not trad(int(o), ex) for o, ex in zip(viol.oldId, viol.exDate)], dtype=bool)
            nt_n = np.array([not trad(int(n), ex) for n, ex in zip(viol.newId, viol.exDate)], dtype=bool)
            tag_o = viol.repairOf_o.fillna("").astype(str).str.contains("csv_override:", regex=False).to_numpy() if "repairOf_o" in viol.columns else np.zeros(len(viol), bool)
            tag_n = viol.repairOf_n.fillna("").astype(str).str.contains("csv_override:", regex=False).to_numpy() if "repairOf_n" in viol.columns else np.zeros(len(viol), bool)
            placed = (nt_o & tag_o) | (nt_n & tag_n)
            csv_placed = viol[placed]
            viol = viol[~placed]
    else:
        merger_marked = viol
    return {"ok": bool(unmarked_nt == 0 and len(viol) == 0), "value_leg_rows": int(len(v)), "rows_on_non_trading_id_unmarked": unmarked_nt,
            "rows_on_non_trading_id_marked": int((~tradable & marked).sum()), "reassigned_rows": int(v.notes.str.contains("reassigned_from", na=False).sum()),
            "same_event_on_both_lineage_ids(dual_tradable, legitimate)": int(len(same) - len(viol) - len(merger_marked)), "same_event_with_a_non_trading_id": int(len(viol)),
            "merger_pair_copy_on_non_trading_id_kept_marked(R1c)": int(len(merger_marked)),
            "csv_placed_copy_on_non_trading_id(deliberate, listed)": int(len(csv_placed)),
            "csv_placed_examples": csv_placed[["oldId", "newId", "exDate", "legType"]].head(5).astype(str).to_dict("records") if len(csv_placed) else [],
            "examples": viol[["oldId", "newId", "exDate", "legType"]].head(5).astype(str).to_dict("records")}


def q9b_dropped_copies_have_keeper(stitched: pd.DataFrame | None, dropped: pd.DataFrame | None, repairs: pd.DataFrame | None = None) -> dict:
    """F4 '사건은 정확히 하나의 id 에, 절대 0 개는 아님': 계보 복사본으로 버린 행마다 체인 끝 keeper 에 원장 행이 있어야 한다.
    keeper 행이 없는 경우는 의도적 제거(자기 전환 leg·수동 drop·R3 대체)로만 설명돼야 한다."""
    if stitched is None or not len(stitched):
        return {"ok": True, "dropped_copies": 0}
    no_keeper = stitched[stitched.keeperEventId.fillna("").astype(str) == ""]
    explained = 0
    keys = set()
    if dropped is not None and len(dropped):
        intentional = dropped[dropped.reason.astype(str).str.contains("parent_self_conversion|manual_override|replaced_by_R3|merger_settlement_duplicate", na=False)]
        keys |= set(zip(intentional.tradingItemId.astype(int), pd.to_datetime(intentional.exDate)))
    if repairs is not None and len(repairs):
        md = repairs[(repairs.field == "row") & (repairs.after.astype(str) == "dropped")]        # 수동 교정으로 제거한 행(자기 전환 leg)
        keys |= set(zip(md.tradingItemId.astype(int), pd.to_datetime(md.exDate)))
        # L1 fix 5: a keeper re-keyed by a rc2f CSV set (exDate / legType / eventType changed, e.g. AET 2587380 2000-12-15 spin_off stock -> other cash
        # 12-14) no longer sits at the copy's key; the change is deliberate (csv_override) -> explained
        rk = repairs[(repairs.source.astype(str) == "csv_override") & repairs.field.isin(["exDate", "legType", "eventType"])]
        keys |= set(zip(rk.tradingItemId.astype(int), pd.to_datetime(rk.exDate)))
    if len(no_keeper):
        kmr = no_keeper.keeperMissingReason.fillna("").astype(str) if "keeperMissingReason" in no_keeper.columns else pd.Series("", index=no_keeper.index)
        explained = int(sum(((int(k), pd.Timestamp(e)) in keys) or bool(m) for k, e, m in zip(no_keeper.keeperId, no_keeper.exDate, kmr)))
    return {"ok": bool(len(no_keeper) - explained == 0), "dropped_copies": int(len(stitched)), "keeper_has_event": int((stitched.keeperEventId.fillna("").astype(str) != "").sum()),
            "keeper_missing_intentional_removal": explained, "keeper_missing_unexplained": int(len(no_keeper) - explained),
            "examples": no_keeper.head(5).astype(str).to_dict("records")}


def q10_gap_markers(L: pd.DataFrame, dv: pd.DataFrame, gap_path, dropped: pd.DataFrame | None = None, rc2_rules: bool = False) -> dict:
    """R2 §8-4(4): 벤더 스텝만 있는 셀 중 ciqdividend 원천이 없는 것은 전부 원장에 표시 행(estimated vendor_implied / unresolved other)이 있어야 한다.
    R1b N1: 다른 계보 id 에서 재귀속된 실제 현금 행(source ≠ vendor_implied)이 같은 (id, exDate) 에 있으면 그 행이 담당(마커 없음)이며, 마커가
    실제 행과 공존하면 실패(Q1 도 검사). L1 수정 4: 후보의 prevDate 가 있으면 창 = (prevDate, date+7](원천)·[prevDate, date+3](실제 행);
    rc2f CSV 가 마커를 의도적으로 드롭한 셀(dropped 파일 reason csv_override:<id>, vendor_implied) 은 설명된 것으로 본다."""
    if gap_path is None or not Path(gap_path).exists():
        return {"ok": True, "skipped": "no candidates file"}
    c = pd.read_csv(gap_path)
    idc = "tradingItemId" if "tradingItemId" in c.columns else "id"
    c["date"] = pd.to_datetime(c.date)
    # L1 fix 4: the cell's own interval when known — the same window as ledger_build.vendor_gap_markers; a ledger built with --rc2-rules used ±7 days
    c["prevDate"] = pd.to_datetime(c.prevDate) if ("prevDate" in c.columns and not rc2_rules) else pd.NaT
    markers = L[(L.legType == "cash") & L.notes.str.contains("vendor_only_step", na=False)]
    mk = set(zip(markers.tradingItemId.astype(int), markers.exDate))
    real = L[(L.legType == "cash") & (L.source != "vendor_implied") & (L.source != "stitched_copy")]
    rk = set(zip(real.tradingItemId.astype(int), real.exDate))
    marker_next_to_real = int(sum(k in rk for k in mk))
    est = markers[markers.status == "estimated"]
    bad_est = est[~est.isEstimate | (est.source != "vendor_implied") | ~np.isfinite(est.cashPerShare)]
    bad_unres = markers[(markers.status == "unresolved") & np.isfinite(markers.cashPerShare)]
    by = {tid: g for tid, g in dv.groupby("tradingItemId")}
    csv_dropped = set()
    if dropped is not None and len(dropped) and "reason" in dropped.columns:
        dd = dropped[dropped.reason.astype(str).str.startswith("csv_override:") & (dropped.source.astype(str) == "vendor_implied")]
        csv_dropped = set(zip(dd.tradingItemId.astype(int), pd.to_datetime(dd.exDate)))
    missing, n_src, n_real, n_csv_dropped = [], 0, 0, 0
    real_by = {tid: g.exDate.to_numpy() for tid, g in real.groupby(real.tradingItemId.astype(int))}
    for r in c.itertuples(index=False):
        tid, date = int(getattr(r, idc)), pd.Timestamp(r.date)
        pdt = pd.Timestamp(r.prevDate) if pd.notna(r.prevDate) else pd.NaT
        g = by.get(tid)
        if g is not None:
            near = g[(g.exDate > pdt) & (g.exDate <= date + pd.Timedelta(days=7))] if pd.notna(pdt) else g[(g.exDate - date).abs().dt.days <= 7]
            if len(near):
                n_src += 1
                continue
        days = (date, date + pd.Timedelta(days=1), date + pd.Timedelta(days=2), date + pd.Timedelta(days=3))
        rr = real_by.get(tid)
        if any((tid, d) in rk for d in days) or (pd.notna(pdt) and rr is not None and ((rr >= np.datetime64(pdt)) & (rr <= np.datetime64(date + pd.Timedelta(days=3)))).any()):
            n_real += 1                                  # 재귀속된/교정으로 추가된 실제 현금 행이 담당(N1; 창 = [prevDate, date+3] 또는 date..date+3)
            continue
        if not any((tid, d) in mk for d in days):
            if any((tid, d) in csv_dropped for d in days):
                n_csv_dropped += 1                       # 마커를 rc2f CSV 가 의도적으로 드롭(근거 있는 spurious step)
                continue
            missing.append((tid, str(date.date())))
    return {"ok": len(missing) == 0 and len(bad_est) == 0 and len(bad_unres) == 0 and marker_next_to_real == 0, "candidates": int(len(c)),
            "with_ciqdividend_source": n_src, "covered_by_real_cash_row(N1)": n_real,
            "markers_in_ledger": int(len(markers)), "estimated_vendor_implied": int(len(est)), "unresolved_markers": int((markers.status == "unresolved").sum()),
            "estimated_without_flags": int(len(bad_est)), "unresolved_with_amount": int(len(bad_unres)), "marker_next_to_real_cash_row": marker_next_to_real,
            "candidates_without_marker": len(missing), "marker_dropped_by_csv(deliberate)": n_csv_dropped, "examples": missing[:5]}


def q11_export_keys(export_path) -> dict:
    """엔진 export(engine/ca_ledger_cache.parquet) 키 유일성·bookable 조건: (tradingItemId, exDate, legType, childTradingItemId) 중복 0,
    merger/rename/relist 0. 현금 leg 금액 NaN 은 **표시용 행**(Y1: markerReason no_cash_amount, status unresolved, isEstimate False, 값 NaN)
    일 때만, stock·rights leg child null 은 표시용 행(no_child)이거나 **값 leg**(Y1-B: childValuePerShareCache 유한, status ≠ unresolved,
    childValueSource ∈ {wi_close, event_value, vendor_implied} — 엔진이 자회사 없이 손익으로 기장)일 때만 허용하고 그 밖의 행에서는 0 이어야
    한다. markerReason 컬럼이 없는 옛 export 는 예전 규칙(두 종류 모두 0)."""
    if export_path is None or not Path(export_path).exists():
        return {"ok": True, "skipped": "no export file"}
    E = pd.read_parquet(export_path)
    dup = int(E.duplicated(["tradingItemId", "exDate", "legType", "childTradingItemId"], keep=False).sum())
    excl = int(E.eventType.isin(["merger_cash", "merger_stock", "rename", "relist"]).sum())
    nan_cash = (E.legType == "cash") & ~np.isfinite(E.cashPerShareCache)
    no_child = E.legType.isin(["stock", "rights"]) & E.childTradingItemId.isna()
    out = {"rows": int(len(E)), "duplicate_keys": dup, "excluded_types_present": excl, "parentFactorCache_all_1": bool((E.parentFactorCache == 1.0).all())}
    if "markerReason" not in E.columns:
        out.update({"marker_column": False, "cash_rows_without_amount": int(nan_cash.sum()), "value_rows_without_child": int(no_child.sum())})
        out["ok"] = dup == 0 and excl == 0 and int(nan_cash.sum()) == 0 and int(no_child.sum()) == 0
        return out
    mk = E.markerReason
    is_mk = mk.notna()
    bad_reason = int((is_mk & ~mk.isin(["no_cash_amount", "no_child", "coacthist_stale_termination"])).sum())
    # 표시용 행은 (사유 ↔ 결손) 이 일치하고 status unresolved·isEstimate False·값 NaN 이어야 한다(coacthist_stale_termination 은 child 없는 stock 행, D1)
    reason_mismatch = int(((mk == "no_cash_amount") & ~nan_cash).sum() + ((mk == "no_child") & ~no_child).sum() + ((mk == "coacthist_stale_termination") & ~no_child).sum())
    marker_not_unresolved = int((is_mk & (E.status != "unresolved")).sum())
    marker_flagged_estimate = int((is_mk & E.isEstimate.astype(bool)).sum())
    marker_with_value = int((is_mk & (np.isfinite(E.cashPerShareCache) | np.isfinite(E.childRatioCache) | np.isfinite(E.childCloseAtExCache))).sum())
    # 표시용이 아닌 행은 bookable: 현금 금액 존재; 자회사 id 존재 또는 값 leg(childValuePerShareCache 유한·status ≠ unresolved·추정 tier)
    valued = np.zeros(len(E), dtype=bool)
    if "childValuePerShareCache" in E.columns:
        _v = E.childValuePerShareCache.to_numpy(np.float64)
        valued = (np.isfinite(_v) & (_v > 0) & (E.status != "unresolved").to_numpy()      # engine condition: finite and positive
                  & E.childValueSource.isin(["wi_close", "event_value", "vendor_implied"]).to_numpy())
    unmarked_nan_cash = int((nan_cash & ~is_mk).sum())
    unmarked_no_child = int((no_child & ~is_mk & ~valued).sum())
    value_only_no_child = int((no_child & ~is_mk & valued).sum())
    out.update({"marker_column": True, "marker_rows": int(is_mk.sum()), "marker_by_reason": mk[is_mk].value_counts().to_dict(),
                "marker_bad_reason": bad_reason, "marker_reason_mismatch": reason_mismatch, "marker_not_unresolved": marker_not_unresolved,
                "marker_flagged_estimate": marker_flagged_estimate, "marker_with_value": marker_with_value,
                "cash_rows_without_amount_unmarked": unmarked_nan_cash, "value_rows_without_child_unmarked": unmarked_no_child,
                "value_rows_without_child_valued": value_only_no_child,
                "marker_examples": E[is_mk][["eventId", "markerReason"]].head(5).astype(str).to_dict("records")})
    out["ok"] = bool(dup == 0 and excl == 0 and bad_reason == 0 and reason_mismatch == 0 and marker_not_unresolved == 0 and marker_flagged_estimate == 0
                     and marker_with_value == 0 and unmarked_nan_cash == 0 and unmarked_no_child == 0)
    return out


def q12_export_vs_events(export_path, events_path) -> dict:
    """E1 §9: export 에 부모 정산일 이후 events 승계사와 같은 stock leg / events 현금 leg 중복 cash leg 가 없어야 한다(events 파일이 담당).
    추가 자회사 leg(SLM 2번째 자회사, DISC.K, TROX …)는 남아 있어야 하며 목록으로 보고한다."""
    if export_path is None or not Path(export_path).exists() or events_path is None or not Path(events_path).exists():
        return {"ok": True, "skipped": "export or events file missing"}
    E = pd.read_parquet(export_path)
    ev = pd.read_parquet(events_path, columns=["tradingItemId", "settlementDate", "endDate", "successorTradingItemId", "hasCashLeg", "hasStockLeg"])
    ev = ev[ev.hasCashLeg.astype("boolean").fillna(False).astype(bool) | ev.hasStockLeg.astype("boolean").fillna(False).astype(bool)].copy()
    ev["settle"] = pd.to_datetime(ev.settlementDate).fillna(pd.to_datetime(ev.endDate))
    ev = ev.dropna(subset=["settle"]).sort_values("settle").drop_duplicates("tradingItemId", keep="last")
    ev["tradingItemId"] = ev.tradingItemId.astype(str)
    m = E.merge(ev[["tradingItemId", "settle", "successorTradingItemId", "hasCashLeg"]], on="tradingItemId", how="inner")
    after = pd.to_datetime(m.exDate) >= m.settle
    succ = after & (m.legType == "stock") & m.successorTradingItemId.notna() & (m.childTradingItemId.astype(str) == m.successorTradingItemId.astype("Int64").astype(str))
    cash_dup = after & (m.legType == "cash") & m.hasCashLeg.astype("boolean").fillna(False).astype(bool)
    extra = after & (m.legType == "stock") & ~succ
    return {"ok": bool(succ.sum() == 0 and cash_dup.sum() == 0), "export_rows_with_events_row": int(len(m)), "rows_on_or_after_settlement": int(after.sum()),
            "successor_duplicates_in_export": int(succ.sum()), "cash_duplicates_in_export": int(cash_dup.sum()),
            "additional_child_legs_kept": int(extra.sum()), "additional_child_examples": m[extra][["eventId", "childTradingItemId"]].head(30).astype(str).to_dict("records"),
            "events_file": str(events_path)}


def q13_stitched_copies(L: pd.DataFrame, export_path) -> dict:
    """리드 결정: stitched_copy 행은 keeper(거래 id 행)와 leg·값이 같고(tradingItemId 만 다름) exportExclude=true, export 에 없어야 한다."""
    c = L[L.source == "stitched_copy"]
    if c.empty:
        return {"ok": True, "stitched_copies": 0}
    by = L[L.source != "stitched_copy"].set_index("eventId")
    missing_keeper = c[~c.keeperEventId.astype(str).isin(by.index)]
    cc = c[c.keeperEventId.astype(str).isin(by.index)]
    k = by.loc[cc.keeperEventId.astype(str)]
    cmp_cols = ["exDate", "eventType", "legType", "parentFactor", "childRatio", "childValuePerOldShare", "cashPerShare", "status", "isEstimate"]
    mism = 0
    for col in cmp_cols:
        a, b = cc[col].to_numpy(), k[col].to_numpy()
        if col in ("parentFactor", "childRatio", "childValuePerOldShare", "cashPerShare"):
            a, b = a.astype(float), b.astype(float)
            bad = ~((np.isnan(a) & np.isnan(b)) | (np.abs(a - b) <= 1e-12 * np.maximum(1.0, np.abs(b))))
        else:
            bad = a != b
        mism += int(bad.sum())
    child_bad = int((cc.childTradingItemId.astype("Int64").fillna(-1).to_numpy() != k.childTradingItemId.astype("Int64").fillna(-1).to_numpy()).sum())
    same_id = int((cc.tradingItemId.to_numpy() == k.tradingItemId.to_numpy()).sum())
    not_flagged = int((~c.exportExclude.fillna(False).astype(bool)).sum())
    in_export = 0
    if export_path is not None and Path(export_path).exists():
        E = pd.read_parquet(export_path, columns=["eventId"])
        in_export = int(E.eventId.isin(set(c.eventId)).sum())
    return {"ok": bool(len(missing_keeper) == 0 and mism == 0 and child_bad == 0 and same_id == 0 and not_flagged == 0 and in_export == 0),
            "stitched_copies": int(len(c)), "keeper_missing": int(len(missing_keeper)), "leg_field_mismatches": mism, "child_mismatch": child_bad,
            "copy_on_same_id_as_keeper": same_id, "not_exportExclude": not_flagged, "copies_in_export": in_export,
            "status_of_copies": c.status.value_counts().to_dict()}


def _same_security_closure(lineage: pd.DataFrame) -> dict:
    """id → rename/relist(+override) 링크로 2 hop 안에 닿는 id 집합(ledger_build._lineage_neighbors 와 같은 정의)."""
    nb = {}
    for a, b in same_security_pairs(lineage):
        nb.setdefault(a, set()).add(b)
        nb.setdefault(b, set()).add(a)
    out = {}
    for k, v in nb.items():
        ext = set(v)
        for x in v:
            ext |= nb.get(x, set())
        ext.discard(k)
        out[k] = ext
    return out


def q14_cross_company_attribution(L: pd.DataFrame, lineage: pd.DataFrame, dropped: pd.DataFrame | None, stitched: pd.DataFrame | None) -> dict:
    """R1c ①(V2 §4-2 ①): 사건을 다른 id 로 옮기거나(notes reassigned_from=), 다른 id 의 복사본으로 버리거나(dropped history_appended_to_successor /
    history_duplicate_no_trading_id), 계보 복사본을 만든(stitched_copy ↔ keeper) 모든 (src, dst) 쌍은 rename/relist(+SAME_SECURITY_LINK_OVERRIDES) 링크로
    2 hop 안에 연결돼야 한다 — merger/acquisition 링크만으로 연결된 쌍은 실패(TFC←TOBC 36 배당, JCI←TYC TEL leg …)."""
    allowed = _same_security_closure(lineage)
    merger_pairs = set()
    for a, b in zip(lineage.oldId.astype(int), lineage.newId.astype(int)):
        merger_pairs.add((a, b)); merger_pairs.add((b, a))
    bad, n_pairs = [], 0

    def check(src, dst, kind, ref):
        nonlocal n_pairs
        n_pairs += 1
        if dst not in allowed.get(src, set()):
            bad.append({"kind": kind, "src": int(src), "dst": int(dst), "ref": ref, "direct_lineage_link": (src, dst) in merger_pairs})
    ev = L[L.notes.str.contains("reassigned_from=", na=False) & (L.source != "stitched_copy")]
    for eid, tid, note in zip(ev.eventId, ev.tradingItemId, ev.notes):
        m = re.search(r"reassigned_from=(\d+)", str(note))
        if m:
            check(int(m.group(1)), int(tid), "reassigned", eid)
    cp = L[(L.source == "stitched_copy") & L.keeperEventId.notna()]
    keeper_id = cp.keeperEventId.astype(str).str.split("_").str[0]
    for eid, tid, k, note in zip(cp.eventId, cp.tradingItemId, keeper_id, cp.notes):
        if TRANSITION_COPY_REASON in str(note):
            continue                                         # 전환일 미러(④)는 lineage 승계 링크(merger 포함) 기반 — Q17 이 검사
        if k.isdigit():
            check(int(k), int(tid), "stitched_copy", eid)
    if dropped is not None and len(dropped):
        d = dropped[dropped.reason.astype(str).str.contains("history_appended_to_successor|history_duplicate_no_trading_id", na=False)]
        for tid, reason, ex in zip(d.tradingItemId, d.reason.astype(str), d.exDate):
            m = re.search(r"(\d+)$", reason)
            if m:
                check(int(tid), int(m.group(1)), "dropped_copy", f"{int(tid)}@{ex}")
    override = {(a, b) for a, b, _ in SAME_SECURITY_LINK_OVERRIDES} | {(b, a) for a, b, _ in SAME_SECURITY_LINK_OVERRIDES}
    n_override = 0
    for tid, note in zip(ev.tradingItemId, ev.notes):
        m = re.search(r"reassigned_from=(\d+)", str(note))
        if m and (int(m.group(1)), int(tid)) in override:
            n_override += 1
    kept_nt = int((L.notes.str.contains("no_same_security_lineage_link", na=False) & (L.source != "stitched_copy")).sum())
    return {"ok": len(bad) == 0, "pairs_checked": n_pairs, "cross_company_pairs(merger/acquisition-only link)": len(bad),
            "same_security_overrides": [f"{a}->{b}" for a, b, _ in SAME_SECURITY_LINK_OVERRIDES], "rows_on_override_links": n_override,
            "rows_kept_on_non_trading_id_without_same_security_link": kept_nt,
            "examples": bad[:6]}


def _parse_frac(notes: pd.Series, key: str) -> np.ndarray:
    v = notes.str.extract(rf"{key}=(-?[0-9.eE+-]+|nan|NA)")[0]
    return pd.to_numeric(v, errors="coerce").to_numpy(dtype=float)


def q15_spin_leg_price_check(L: pd.DataFrame, out_dir: Path | None = None) -> dict:
    """R1c ②(V2 §4-2 ②): P10 ①(child_close_ex)·③(event_value) 값 leg 의 가격 정합 — notes 의 checkFrac(사건 가치/전일 종가)·dropFrac(부모 배당락 하락)에서
    checkFrac ≥ 0.15 이고 (dropFrac < 0.05 또는 checkFrac − dropFrac > 0.40) 인 leg 가 status ok/estimated 로 남아 있으면 실패(전환일 leg·manual_override·
    merger 제외). 약한 불일치(값 ≥ 5% & 부모 상승)는 정보로 목록(qa/spin_leg_price_check.csv)."""
    v = L[L.legType.isin(["stock", "rights"]) & (L.source != "stitched_copy") & ~L.eventType.isin(MERGER_TYPES)
          & L.childValueSource.isin(["child_close_ex", "event_value"]) & (L.status != "unresolved")].copy()
    v = v[~v.notes.str.contains("transition_day_leg", na=False) & (v.source != "manual_override")]
    if v.empty:
        return {"ok": True, "legs_checked": 0}
    v["checkFrac"] = _parse_frac(v.notes, "checkFrac")
    v["dropFrac"] = _parse_frac(v.notes, "dropFrac")
    # 사건 단위(같은 id, exDate) 합계: child_close_ex 는 leg 값 합, event_value 는 notes 의 checkFrac 이 사건 전체값
    ev_check = v[v.childValueSource == "child_close_ex"].groupby(["tradingItemId", "exDate"]).checkFrac.sum()
    v["checkEvent"] = [float(ev_check.get((t, d), np.nan)) if s == "child_close_ex" else c for t, d, s, c in zip(v.tradingItemId, v.exDate, v.childValueSource, v.checkFrac)]
    fin = np.isfinite(v.checkEvent.to_numpy(float)) & np.isfinite(v.dropFrac.to_numpy(float))
    strong = fin & (v.checkEvent >= IMPLAUSIBLE_CHECK_MIN) & ((v.dropFrac < IMPLAUSIBLE_DROP_MAX) | (v.checkEvent - v.dropFrac > IMPLAUSIBLE_JUMP))
    weak = fin & ~strong & (v.checkEvent >= 0.05) & (v.dropFrac < 0)
    v["kind"] = np.where(strong, "strong", np.where(weak, "weak", ""))
    listed = v[strong | weak][["eventId", "tradingItemId", "exDate", "childTradingItemId", "childRatio", "childValuePerOldShare", "childValueSource", "status",
                               "checkFrac", "dropFrac", "checkEvent", "kind"]]
    if out_dir is not None:
        listed.to_csv(Path(out_dir) / "spin_leg_price_check.csv", index=False)
    corrected = int((L.notes.str.contains("exDate_corrected_from=", na=False) & (L.source != "stitched_copy")).sum())
    unres = int((L.notes.str.contains("implausible_child_close_ex|implausible_event_value", na=False) & (L.source != "stitched_copy")).sum())
    return {"ok": int(strong.sum()) == 0, "legs_checked": int(len(v)), "legs_without_frac": int((~fin).sum()), "strong_inconsistent_ok_or_estimated": int(strong.sum()),
            "weak(info)": int(weak.sum()), "exDate_corrected_by_price_gap": corrected, "set_unresolved_as_implausible": unres,
            "examples": listed[listed.kind == "strong"].head(5).astype(str).to_dict("records")}


def q16_split_row_on_quoting_old_id(L: pd.DataFrame, closes) -> dict:
    """R1c ③(V2 §4-2 ③): rename/relist 행이 분할 step 을 대표(notes represents_split_step)하면 옛 id 는 경계일 이후 호가하지 않아야 한다 —
    옛 id 가 exDate 당일·이후 raw 종가를 가지면 그 id 에 같은 계수의 quantity 행이 있어야 한다(옛 id 잔여 행의 가격 step 설명).
    closes 없으면(base_data 미지정) 구조 검사만: represents_split_step 행의 옛 id 에 quantity 행이 없는 것이 정상, 정보 출력."""
    lin = L[L.eventType.isin(["rename", "relist"]) & (L.source != "stitched_copy") & np.isfinite(L.parentFactor.to_numpy(float)) & (np.abs(L.parentFactor - 1.0) > 1e-9)]
    rep = lin[lin.notes.str.contains("represents_split_step", na=False)]
    kept = lin[lin.notes.str.contains("split_row_kept_on_old_id", na=False)]
    qty = set(zip(L[L.legType == "quantity"].tradingItemId.astype(int), pd.to_datetime(L[L.legType == "quantity"].exDate)))
    kept_missing = int(sum((int(t), pd.Timestamp(d)) not in qty for t, d in zip(kept.tradingItemId, kept.exDate)))
    if closes is None:
        return {"ok": kept_missing == 0, "skipped_price_check": "no base_data", "rename_rows_ratio_ne_1": int(len(lin)), "represents_split_step": int(len(rep)),
                "split_row_kept_on_quoting_old_id": int(len(kept)), "kept_without_quantity_row": kept_missing}
    bad = []
    if len(rep):
        c_after, d_after = closes.on_or_after(rep.tradingItemId.to_numpy(np.int64), rep.exDate.to_numpy(), max_days=100000)
        for (eid, t, d), c, da in zip(zip(rep.eventId, rep.tradingItemId, rep.exDate), c_after, d_after):
            if np.isfinite(c) and (int(t), pd.Timestamp(d)) not in qty:
                bad.append({"eventId": eid, "oldId": int(t), "exDate": str(pd.Timestamp(d).date()), "first_close_on_or_after": str(pd.Timestamp(da).date())})
    # 정보: 비율 ≠ 1 인 rename 행의 옛 id 가 경계 이후 호가하는데 split 행이 없는 경우(계수 테이블에 step 이 없는 전환 — R3 검토 목록)
    others = lin[~lin.notes.str.contains("represents_split_step|split_row_kept_on_old_id", na=False)]
    n_quoting_no_split = 0
    if len(others):
        c_after, _ = closes.on_or_after(others.tradingItemId.to_numpy(np.int64), others.exDate.to_numpy(), max_days=100000)
        n_quoting_no_split = int(sum(np.isfinite(c) and (int(t), pd.Timestamp(d)) not in qty for t, d, c in zip(others.tradingItemId, others.exDate, c_after)))
    return {"ok": len(bad) == 0 and kept_missing == 0, "rename_rows_ratio_ne_1": int(len(lin)), "represents_split_step": int(len(rep)),
            "represented_but_old_id_quotes_after_boundary": len(bad), "split_row_kept_on_quoting_old_id": int(len(kept)), "kept_without_quantity_row": kept_missing,
            "ratio_ne_1_old_id_quoting_without_split_row(info, no factor step)": n_quoting_no_split, "examples": bad[:5]}


def q17_transition_mirror(L: pd.DataFrame, lifecycle: pd.DataFrame, closes, out_dir: Path | None = None) -> dict:
    """R1c ④(V2 §4-2 ④): 옛 id 의 전환일 value leg(notes transition_day_leg(...successor=S)) 는 S 가 전환일 직전 종가를 이어붙인 과거(lifecycle firstTradeDate
    이전)로 가질 때 S 에 stitched_copy(keeperEventId = 그 leg) 로 미러돼 있어야 한다. 정보: S 전환일 행의 현금식 수익률 화면(가격만 vs leg 반영)."""
    src = L[(L.source != "stitched_copy") & L.legType.isin(["cash", "stock", "rights"]) & L.notes.str.contains("transition_day_leg", na=False)]
    if src.empty:
        return {"ok": True, "transition_legs": 0}
    succ = src.notes.str.extract(r"transition_day_leg\((?:old_id_keyed;successor=(\d+)|rekeyed_from_successor=(\d+))")
    sid = succ[0].fillna(succ[1])
    copies = L[(L.source == "stitched_copy") & L.notes.str.contains(TRANSITION_COPY_REASON, na=False)]
    have = set(zip(copies.tradingItemId.astype(int), copies.keeperEventId.astype(str)))
    if closes is None:
        return {"ok": True, "skipped": "no base_data", "transition_legs": int(len(src)), "transition_copies": int(len(copies))}
    first = lifecycle.set_index("tradingItemId").firstTradeDate.to_dict()
    s_ids = sid.fillna(-1).astype(int).to_numpy(np.int64)
    p_before, d_before = closes.before(np.where(s_ids > 0, s_ids, 1), src.exDate.to_numpy())
    missing, n_stitched, n_diff, screen = [], 0, 0, []
    qty = L[L.legType == "quantity"].set_index(["tradingItemId", "exDate"]).parentFactor
    same_ok = {}
    for k, (eid, tid, ex, leg, cash, cval) in enumerate(zip(src.eventId, src.tradingItemId, src.exDate, src.legType, src.cashPerShare, src.childValuePerOldShare)):
        s_ = int(s_ids[k])
        if s_ <= 0 or not np.isfinite(p_before[k]):
            continue
        f = first.get(s_, pd.NaT)
        if pd.notna(f) and pd.Timestamp(d_before[k]) >= pd.Timestamp(f):
            continue
        same, why = stitched_same_series(closes, int(tid), s_, ex, f)
        same_ok[s_] = same
        if not same:
            n_diff += 1                                       # 이어붙인 과거가 옛 id 시계열이 아님 → 미러 대상 아님(정보)
            continue
        n_stitched += 1
        if (s_, eid) not in have:
            missing.append({"eventId": eid, "successor": s_, "exDate": str(pd.Timestamp(ex).date())})
    # 정보 화면: 승계 id 전환일 행의 수익률(가격만 / 옛 id leg 반영), 원장 leg 합계로 재계산
    if n_stitched:
        for s_, g in src.assign(_s=s_ids).groupby("_s"):
            if s_ <= 0:
                continue
            ex = pd.Timestamp(g.exDate.iloc[0])
            pb, db = closes.before([s_], [ex])
            pe, de = closes.on_or_after([s_], [ex], 7)
            if not (np.isfinite(pb[0]) and np.isfinite(pe[0])):
                continue
            f = first.get(s_, pd.NaT)
            if pd.notna(f) and pd.Timestamp(db[0]) >= pd.Timestamp(f):
                continue
            if not same_ok.get(int(s_), False):
                continue
            pf = float(qty.get((s_, ex), 1.0)) if (s_, ex) in qty.index else 1.0
            cash_sum = float(np.nansum(g.cashPerShare.to_numpy(float))); child_sum = float(np.nansum(g.childValuePerOldShare.to_numpy(float)))
            screen.append({"successor": s_, "oldId": int(g.tradingItemId.iloc[0]), "exDate": str(ex.date()), "prevClose": float(pb[0]), "closeAtEx": float(pe[0]),
                           "ret_price_only": pf * float(pe[0]) / float(pb[0]) - 1, "ret_with_legs": (pf * float(pe[0]) + cash_sum + child_sum) / float(pb[0]) - 1,
                           "legs": int(len(g)), "unresolved_legs": int((g.status == "unresolved").sum())})
    sc = pd.DataFrame(screen)
    if out_dir is not None and len(sc):
        sc.to_csv(Path(out_dir) / "stitched_successor_transition_returns.csv", index=False)
    big = int((sc.ret_price_only.abs() > 0.2).sum()) if len(sc) else 0
    return {"ok": len(missing) == 0, "transition_legs": int(len(src)), "legs_on_stitched_successor_lines(same series)": n_stitched,
            "legs_on_stitched_lines_with_a_different_series(not mirrored, info)": n_diff, "transition_copies": int(len(copies)),
            "copies_missing": len(missing), "successor_lines_screened": int(len(sc)), "successor_rows_price_only_abs_ret_gt_20pct": big,
            "examples": (missing[:5] or (sc.sort_values("ret_price_only").head(4).round(4).astype(str).to_dict("records") if len(sc) else []))}


def status_tables(L: pd.DataFrame, membership: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    L = L[L.source != "stitched_copy"]                  # 복사본은 keeper 와 동일 → 상태표·unresolved 집계에서 제외(별도 Q13)
    uf = universe_flags(L, membership)
    d = L.assign(Top500=uf.Top500.values, Top1000=uf.Top1000.values)
    d["universe"] = np.where(d.Top500, "Top500", np.where(d.Top1000, "Top1000(ex500)", "other"))
    t1 = d.pivot_table(index="eventType", columns="status", values="eventId", aggfunc="count", fill_value=0).reset_index()
    t2 = d.pivot_table(index=["eventType", "universe"], columns="status", values="eventId", aggfunc="count", fill_value=0).reset_index()
    t3 = d[d.legType.isin(["stock", "rights"])].pivot_table(index="childValueSource", columns="status", values="eventId", aggfunc="count", fill_value=0).reset_index()
    return t1, t2, t3, d


def unresolved_reasons(d: pd.DataFrame) -> pd.DataFrame:
    u = d[d.status == "unresolved"].copy()
    def reason(n, et, lt):
        n = str(n)
        for key in ("artifact:no_price_counterpart", "factor_step_without_price_move", "child_not_in_base_data", "no_child_close_on_exDate",
                    "ratio_unknown", "child_unknown", "fx_missing", "no_terms", "vendor_uncorroborated"):
            if key in n:
                return key
        if lt == "cash" and et in ("merger_cash", "other"):
            return "merger_cash_no_amount"
        return "other"
    u["reason"] = [reason(n, et, lt) for n, et, lt in zip(u.notes, u.eventType, u.legType)]
    return u


def run(ledger_path: Path, root: Path, out_dir: Path, base_data=None) -> dict:
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    L = pd.read_parquet(ledger_path)
    split = pd.read_parquet(root / "factors" / "split.parquet")
    dv = pd.read_parquet(root / "dividends" / "dividends_long.parquet")
    membership = pd.read_parquet(root / "universe" / "membership.parquet")
    cal = Calendar.from_root(root)
    rep_path = Path(ledger_path).parent / "ca_ledger_repairs.csv"
    repairs = pd.read_csv(rep_path) if rep_path.exists() else None
    drop_path = Path(ledger_path).parent / "ca_ledger_dropped_duplicates.csv"
    dropped = pd.read_csv(drop_path) if drop_path.exists() else None
    info_path = Path(ledger_path).parent / "ca_ledger_build_info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    lineage_p = info.get("inputs", {}).get("lineage")
    lifecycle_p = info.get("inputs", {}).get("lifecycle")
    lineage = pd.read_parquet(lineage_p) if lineage_p and Path(lineage_p).exists() else pd.read_parquet(root / "master" / "lineage.parquet")
    lifecycle = pd.read_parquet(lifecycle_p) if lifecycle_p and Path(lifecycle_p).exists() else pd.read_parquet(root / "master" / "lifecycle.parquet")
    gap_path = info.get("inputs", {}).get("gap_candidates")
    st_path = Path(ledger_path).parent / "ca_ledger_stitched_copies.csv"
    stitched = pd.read_csv(st_path) if st_path.exists() else None
    res = {
        "Q1_unique_key": q1_unique_key(L), "Q2_schema": q2_schema(L), "Q3_split_repro": q3_split_repro(L, split, cal),
        "Q4_cash_sum": q4_cash_sum(L, dv, cal, repairs, dropped, out_dir, stitched), "Q5_same_day_legs": q5_same_day(L), "Q6_calendar": q6_calendar(L, cal),
        "Q7_oracles": q7_oracles(L), "Q8_repairs_log": q8_repairs(L, repairs),
        "Q9_attribution": q9_attribution(L, lifecycle, lineage), "Q10_gap_markers": q10_gap_markers(L, dv, gap_path if gap_path and gap_path != "None" else None, dropped, rc2_rules=str(info.get("rules", "")) == "rc2"),
    }
    res["Q9b_dropped_copies_have_keeper"] = q9b_dropped_copies_have_keeper(stitched, dropped, repairs)
    export_p = Path(ledger_path).parent.parent / "engine" / "ca_ledger_cache.parquet"
    res["Q11_export_keys"] = q11_export_keys(export_p)
    man_p = Path(ledger_path).parent.parent / "engine" / "ca_ledger_cache_manifest.json"
    ev_p = json.loads(man_p.read_text()).get("events_file") if man_p.exists() else None
    res["Q12_export_vs_events"] = q12_export_vs_events(export_p, ev_p)
    res["Q13_stitched_copies"] = q13_stitched_copies(L, export_p)
    res["Q14_cross_company_attribution"] = q14_cross_company_attribution(L, lineage, dropped, stitched)
    res["Q15_spin_leg_price_check"] = q15_spin_leg_price_check(L, out_dir)
    closes = None
    if base_data is not None and Path(base_data).exists():
        need = {pid for _, pid, _, _, _, _ in ORACLE_RETURNS} | {tid for _, _, _, ap, _, _ in ORACLE_RETURNS for tid, _ in ap}
        need |= set(int(x) for x in L[L.tradingItemId.isin(need) & L.childTradingItemId.notna()].childTradingItemId)
        lin_ids = L[L.eventType.isin(["rename", "relist"])]
        need |= set(lin_ids.tradingItemId.astype(int)) | set(int(x) for x in lin_ids.childTradingItemId.dropna())
        tl = L[L.notes.str.contains("transition_day_leg", na=False)]
        need |= set(tl.tradingItemId.astype(int)) | set(int(x) for x in tl.notes.str.extract(r"successor=(\d+)")[0].dropna()) \
            | set(int(x) for x in tl.notes.str.extract(r"rekeyed_from_successor=(\d+)")[0].dropna())
        aux = info.get("inputs", {}).get("aux_base_data")
        closes = RawCloses([base_data, aux if aux and aux != "None" else None], need)
        res["Q7b_oracle_returns"] = q7b_oracle_returns(L, closes)
    res["Q16_split_row_on_quoting_old_id"] = q16_split_row_on_quoting_old_id(L, closes)
    res["Q17_transition_mirror"] = q17_transition_mirror(L, lifecycle, closes, out_dir)
    # R1b(리뷰 N4, 정보성): exDate 시세가 없고 다음 관측 종가도 10 일 창 밖이라 raw 점프 검증 없이 ok 로 남은 분할 행 목록(status ok 만; estimated/
    # unresolved 는 상태 자체가 표시). CELH 30852064 2006-12-26 등 — 계수는 factors/split 재현(Q3), 검증 없음 표시는 notes no_price_around_exDate
    unq = L[(L.legType == "quantity") & L.eventType.isin(["split", "stock_dividend"]) & L.notes.str.contains("no_price_around_exDate", na=False)]
    unq_ok = unq[unq.status == "ok"]
    unq_ok[["eventId", "tradingItemId", "exDate", "eventType", "parentFactor", "source", "status", "notes"]].to_csv(out_dir / "split_rows_unquoted.csv", index=False)
    res["split_rows_unquoted_kept_ok"] = int(len(unq_ok))
    res["split_rows_unquoted_flagged"] = unq[unq.status != "ok"].status.value_counts().to_dict()
    t1, t2, t3, d = status_tables(L, membership)
    u = unresolved_reasons(d)
    u_top = u[u.universe != "other"].sort_values(["universe", "exDate"])
    u_top[["eventId", "tradingItemId", "exDate", "eventType", "legType", "childTradingItemId", "childRatio", "universe", "reason", "source", "notes"]].to_csv(
        out_dir / "unresolved_top500_top1000.csv", index=False)
    t2.to_csv(out_dir / "status_by_type_universe.csv", index=False)
    summary = {k: v["ok"] for k, v in res.items() if isinstance(v, dict) and "ok" in v}
    res["all_ok"] = all(summary.values())
    res["rows"] = int(len(L))
    res["stitched_copy_rows"] = int((L.source == "stitched_copy").sum())
    res["status_counts"] = L[L.source != "stitched_copy"].status.value_counts().to_dict()
    res["unresolved_reasons"] = u.groupby(["universe", "reason"]).size().rename("n").reset_index().to_dict("records")
    (out_dir / "ledger_qa.json").write_text(json.dumps(res, indent=1, default=str))
    # markdown
    md = [f"# ca_ledger QA — {ledger_path}", "", f"rows {len(L):,} (stitched_copy {res['stitched_copy_rows']:,} 제외 상태 집계) · status {res['status_counts']} · **all_ok = {res['all_ok']}**", "",
          "## 1. 불변식", "", _md(pd.DataFrame([{"check": k, "ok": v["ok"], "detail": json.dumps({kk: vv for kk, vv in v.items() if kk not in ('ok', 'rows', 'examples')}, default=str)[:400]}
                                                for k, v in res.items() if isinstance(v, dict) and "ok" in v])),
          "", "## 2. 오라클 행", "", _md(pd.DataFrame(res["Q7_oracles"]["rows"] + res.get("Q7b_oracle_returns", {}).get("rows", []))),
          "", "## 3. 상태 분포 eventType × status", "", _md(t1),
          "", "## 4. 상태 분포 eventType × universe(exDate 기준 Top500 / Top1000 ex500 / other) × status", "", _md(t2),
          "", "## 5. childValueSource × status (stock/rights 행)", "", _md(t3),
          "", "## 6. unresolved 사유 × universe", "", _md(pd.DataFrame(res["unresolved_reasons"])),
          "", "## 7. unresolved Top500/Top1000 목록 (전체는 unresolved_top500_top1000.csv)", "",
          _md(u_top[["eventId", "eventType", "legType", "childTradingItemId", "childRatio", "universe", "reason"]].head(80))]
    (out_dir / "ledger_qa.md").write_text("\n".join(md))
    return res


# --------------------------------------------------------------------------------------
# self-test: 검사가 반례를 잡는지
# --------------------------------------------------------------------------------------
def _fixture() -> tuple[pd.DataFrame, pd.DataFrame, Calendar]:
    T = pd.Timestamp
    cal = Calendar(pd.bdate_range("2020-01-02", "2020-12-31").to_numpy().astype("datetime64[D]"))
    rows = [
        dict(eventId="1_20200203_cash_dividend_0", tradingItemId=1, exDate=T("2020-02-03"), eventType="cash_dividend", legType="cash", cashPerShare=1.0,
             cashCurrency="USD", fxToUSD=1.0, parentFactor=1.0, childTradingItemId=pd.NA, childRatio=np.nan, childValueSource="unknown",
             childValuePerOldShare=np.nan, subscriptionPrice=np.nan, expiryDate=pd.NaT, exerciseRatio=np.nan, payDate=pd.NaT, recordDate=pd.NaT,
             announcedDate=pd.NaT, source="ciqdividend", evidence="dividendId=1;types=1", status="ok", isEstimate=False, repairOf=None, notes=""),
        dict(eventId="1_20200203_split_0", tradingItemId=1, exDate=T("2020-02-03"), eventType="split", legType="quantity", cashPerShare=np.nan,
             cashCurrency=None, fxToUSD=np.nan, parentFactor=2.0, childTradingItemId=pd.NA, childRatio=np.nan, childValueSource="unknown",
             childValuePerOldShare=np.nan, subscriptionPrice=np.nan, expiryDate=pd.NaT, exerciseRatio=np.nan, payDate=pd.NaT, recordDate=pd.NaT,
             announcedDate=pd.NaT, source="ciqsplitcache", evidence="", status="ok", isEstimate=False, repairOf=None, notes=""),
        dict(eventId="1_20200203_spin_off_0", tradingItemId=1, exDate=T("2020-02-03"), eventType="spin_off", legType="stock", cashPerShare=np.nan,
             cashCurrency=None, fxToUSD=np.nan, parentFactor=1.0, childTradingItemId=2, childRatio=0.5, childValueSource="child_close_ex",
             childValuePerOldShare=20.0, subscriptionPrice=np.nan, expiryDate=pd.NaT, exerciseRatio=np.nan, payDate=pd.NaT, recordDate=pd.NaT,
             announcedDate=pd.NaT, source="distributions", evidence="", status="ok", isEstimate=False, repairOf=None, notes=""),
    ]
    for r in rows:
        r.setdefault("keeperEventId", None)
        r.setdefault("exportExclude", False)
    L = pd.DataFrame(rows)[LEDGER_COLUMNS]
    L["childTradingItemId"] = L.childTradingItemId.astype("Int64")
    L["exDate"] = L.exDate.astype("datetime64[ns]")
    for c in ("expiryDate", "payDate", "recordDate", "announcedDate"):
        L[c] = pd.to_datetime(L[c]).astype("datetime64[ns]")
    split = pd.DataFrame({"tradingItemId": [1, 1], "validFrom": pd.to_datetime(["1900-01-01", "2020-02-03"]),
                          "validTo": pd.to_datetime(["2020-02-03", None]), "factor": [2.0, 1.0],
                          "source": ["synthetic_pre_first", "ciqsplitcache"], "snapshotDate": T("2026-09-17")})
    return L, split, cal


def _write_no_marker(path: Path, cols: dict) -> Path:
    d = {k: v for k, v in cols.items() if k != "markerReason"}
    pd.DataFrame(d).to_parquet(path, index=False)
    return path


def self_test() -> list[dict]:
    res = []
    L, split, cal = _fixture()
    dv = pd.DataFrame({"dividendId": [1], "tradingItemId": [1], "exDate": [pd.Timestamp("2020-02-03")], "divAmount": [1.0], "netAmount": [np.nan],
                       "grossAmount": [np.nan], "currencyId": [160.0], "fxRate": [1.0], "dividendTypeId": [1], "supplementalTypeName": ["Regular cash dividend"]})

    def rec(name, expect_ok, got):
        res.append({"case": name, "expected": "PASS" if expect_ok else "FAIL", "got": "PASS" if got["ok"] else "FAIL", "ok": got["ok"] == expect_ok,
                    "detail": {k: v for k, v in got.items() if k not in ("ok", "examples", "rows")}})

    rec("Q1 valid fixture", True, q1_unique_key(L))
    rec("Q1 duplicate key (same stock leg twice)", False, q1_unique_key(pd.concat([L, L.iloc[[2]]], ignore_index=True)))
    rec("Q2 valid fixture", True, q2_schema(L))
    bad = L.copy(); bad.loc[2, "status"] = "unresolved"
    rec("Q2 unresolved value leg carrying a value", False, q2_schema(bad))
    bad = L.copy(); bad.loc[2, "childValueSource"] = "vendor_implied"
    rec("Q2 P10 ④ source without isEstimate", False, q2_schema(bad))
    bad = L.copy(); bad.loc[0, "parentFactor"] = 0.0
    rec("Q2 non-positive parentFactor", False, q2_schema(bad))
    rec("Q3 split boundary reproduced", True, q3_split_repro(L, split, cal))
    bad = L.copy(); bad.loc[1, "parentFactor"] = 1.5
    rec("Q3 ratio mismatch vs factor table", False, q3_split_repro(bad, split, cal))
    rec("Q3 boundary without ledger row", False, q3_split_repro(L.drop(index=1), split, cal))
    rec("Q4 cash sum equals ciqdividend", True, q4_cash_sum(L, dv, cal, None))
    bad = L.copy(); bad.loc[0, "cashPerShare"] = 1.5
    rec("Q4 cash amount differs", False, q4_cash_sum(bad, dv, cal, None))
    dv2 = dv.copy(); dv2.loc[0, ["netAmount", "grossAmount"]] = [1.0, 1.25]
    rec("Q4 after-tax source row (gross 1.25) vs ledger 1.0 → mismatch detected", False, q4_cash_sum(L, dv2, cal, None))
    dv3 = pd.concat([dv, dv.assign(dividendId=2, exDate=pd.Timestamp("2020-03-03"))], ignore_index=True)
    rec("Q4 dividendId absent from ledger and not in dropped file → detected", False, q4_cash_sum(L, dv3, cal, None, None))
    dropped = pd.DataFrame([{"reason": "history_appended_to_successor:9", "evidence": "dividendId=2;types=1"}])
    rec("Q4 dividendId absent but explained by dropped file (stitched copy)", True, q4_cash_sum(L, dv3, cal, None, dropped))
    # Q1: 같은 날 merger_cash 와 현금배당 — 금액이 다르면 두 행(허용), 같으면 진짜 중복(실패)
    mc = L.iloc[[0]].copy(); mc["eventId"] = "1_20200203_merger_cash_0"; mc["eventType"] = "merger_cash"; mc["cashPerShare"] = 50.0; mc["source"] = "corporate_actions"
    rec("Q1 merger_cash 50 next to dividend 1.0 (distinct legs) → allowed", True, q1_unique_key(pd.concat([L, mc], ignore_index=True)))
    mc["cashPerShare"] = 1.0
    rec("Q1 merger_cash with the same amount as the same-day dividend → true duplicate detected", False, q1_unique_key(pd.concat([L, mc], ignore_index=True)))
    # Q1(R1b): 벤더 마커(other/vendor_implied, 금액 NaN)가 같은 (id, exDate) 의 실제 현금 행과 공존 → 키 중복 실패(eventType 이 달라도)
    mk = L.iloc[[0]].copy(); mk["eventId"] = "1_20200203_other_0"; mk["eventType"] = "other"; mk["cashPerShare"] = np.nan; mk["source"] = "vendor_implied"; mk["status"] = "unresolved"
    rec("Q1 vendor marker next to a real cash row on the same key → detected", False, q1_unique_key(pd.concat([L, mk], ignore_index=True)))
    mk["eventType"] = "cash_dividend"; mk["cashPerShare"] = 0.9; mk["status"] = "estimated"; mk["isEstimate"] = True
    rec("Q1 vendor_implied estimated row next to a real cash row (same eventType) → detected", False, q1_unique_key(pd.concat([L, mk], ignore_index=True)))
    three = pd.concat([L, L.iloc[[0]].assign(eventId="1_20200203_merger_cash_0", eventType="merger_cash", cashPerShare=50.0, source="corporate_actions"),
                       L.iloc[[0]].assign(eventId="1_20200203_other_0", eventType="other", cashPerShare=13.75, source="corporate_actions")], ignore_index=True)
    rec("Q1 three rows on one contract key (dividend + merger_cash + other) → detected", False, q1_unique_key(three))
    # Q2: wi_close 는 isEstimate False, unresolved 는 isEstimate False
    bad = L.copy(); bad.loc[2, "childValueSource"] = "wi_close"; bad.loc[2, "isEstimate"] = True
    rec("Q2 wi_close with isEstimate → detected", False, q2_schema(bad))
    # Q9: 값 leg 가 거래되지 않던 id 에 표시 없이 있음
    lc = pd.DataFrame({"tradingItemId": [1, 2], "firstTradeDate": [pd.Timestamp("2020-01-02")] * 2, "lastTradeDate": [pd.Timestamp("2020-01-31"), pd.NaT], "status": ["confirmed", "active"]})
    ln = pd.DataFrame({"oldId": [1], "newId": [2], "relation": ["rename"], "effectiveDate": [pd.Timestamp("2020-02-03")], "ratio": [1.0]})
    rec("Q9 value leg on an id that stopped trading before exDate, unmarked → detected", False, q9_attribution(L, lc, ln))
    marked = L.copy(); marked["notes"] = "parent_not_tradable_on_exDate"
    rec("Q9 same rows marked in notes → pass", True, q9_attribution(marked, lc, ln))
    dup = L.copy(); dup.loc[0, "tradingItemId"] = 2
    both = pd.concat([L, dup.iloc[[0]].assign(eventId="2_20200203_cash_dividend_0")], ignore_index=True)
    rec("Q9 same cash event on both lineage ids → detected", False, q9_attribution(both.assign(notes="parent_not_tradable_on_exDate"), lc, ln))
    # Q13: stitched copy = keeper 복제(id 만 다름, exportExclude) → PASS; 값이 다르거나 exportExclude 아니면 FAIL
    cp = L.iloc[[0]].copy(); cp["tradingItemId"] = 9; cp["eventId"] = "9_20200203_cash_dividend_0"; cp["source"] = "stitched_copy"; cp["keeperEventId"] = "1_20200203_cash_dividend_0"; cp["exportExclude"] = True
    rec("Q13 stitched copy identical to keeper → pass", True, q13_stitched_copies(pd.concat([L, cp], ignore_index=True), None))
    bad = cp.copy(); bad["cashPerShare"] = 2.0
    rec("Q13 stitched copy with a different amount → detected", False, q13_stitched_copies(pd.concat([L, bad], ignore_index=True), None))
    bad = cp.copy(); bad["exportExclude"] = False
    rec("Q13 stitched copy without exportExclude → detected", False, q13_stitched_copies(pd.concat([L, bad], ignore_index=True), None))
    rec("Q9 stitched copies are not attribution violations", True, q9_attribution(pd.concat([marked, cp], ignore_index=True), lc, ln))
    # Q14(R1c ①): merger 링크만으로 연결된 id 로 옮긴 행 → 실패; rename 링크면 통과
    ln14 = pd.DataFrame({"oldId": [1, 5], "newId": [2, 1], "relation": ["rename", "merger"], "effectiveDate": [pd.Timestamp("2020-02-03")] * 2, "ratio": [1.0, 0.5]})
    mv = L.iloc[[0]].copy(); mv["eventId"] = "1_20200203_cash_dividend_9"; mv["notes"] = "reassigned_from=5(not_tradable_on_exDate)"
    rec("Q14 event reassigned across a merger link → detected", False, q14_cross_company_attribution(pd.concat([L, mv], ignore_index=True), ln14, None, None))
    mv["notes"] = "reassigned_from=2(not_tradable_on_exDate)"
    rec("Q14 event reassigned across a rename link → pass", True, q14_cross_company_attribution(pd.concat([L, mv], ignore_index=True), ln14, None, None))
    # Q15(R1c ②): ① 값이 전일 종가의 83% 인데 부모 하락 −1.7%(T 2001-08-13 형) 가 ok 로 남음 → 실패; unresolved 로 표시되면 통과
    bad = L.copy(); bad.loc[2, "notes"] = "childClose=16.25;checkFrac=0.8299;dropFrac=-0.01685;vendorFrac=NaN"
    rec("Q15 P10 ① value 83% of price with no parent drop, status ok → detected", False, q15_spin_leg_price_check(bad))
    bad.loc[2, "status"] = "unresolved"; bad.loc[2, "childValueSource"] = "unknown"; bad.loc[2, "childValuePerOldShare"] = np.nan; bad.loc[2, "isEstimate"] = False
    rec("Q15 same leg marked unresolved → pass", True, q15_spin_leg_price_check(bad))
    ok15 = L.copy(); ok15.loc[2, "notes"] = "childClose=40.88;checkFrac=0.2002;dropFrac=0.199;vendorFrac=NaN"
    rec("Q15 consistent ① leg (check 20% vs drop 19.9%) → pass", True, q15_spin_leg_price_check(ok15))
    # Q11(Y1): export 표시용 행 — markerReason 이 결손과 일치하고 unresolved·값 NaN 이면 통과, 표시 없는 결손·값 실린 표시용 행은 실패
    import tempfile
    ex_cols = dict(tradingItemId=["1", "1", "2"], exDate=pd.to_datetime(["2020-02-03", "2020-03-03", "2020-03-03"]), eventType=["cash_dividend", "other", "spin_off"],
                   legType=["cash", "cash", "stock"], cashPerShareCache=[1.0, np.nan, np.nan], childTradingItemId=pd.array([None, None, None], dtype="string"),
                   childRatioCache=[np.nan] * 3, childCloseAtExCache=[np.nan] * 3, parentCloseAtExCache=[10.0] * 3, parentCloseBeforeCache=[10.0] * 3,
                   parentFactorCache=[1.0] * 3, isEstimate=[False] * 3, status=["ok", "unresolved", "unresolved"], childValueSource=["unknown", "unknown", "vendor_implied"],
                   eventId=["1_20200203_cash_dividend_0", "1_20200303_other_0", "2_20200303_spin_off_0"],
                   markerReason=pd.array([None, "no_cash_amount", "no_child"], dtype="string"))
    with tempfile.TemporaryDirectory() as td:
        def export_case(name, **over):
            d = dict(ex_cols); d.update(over)
            path = Path(td) / f"{name}.parquet"
            pd.DataFrame(d).to_parquet(path, index=False)
            return path
        rec("Q11 marker rows (no_cash_amount / no_child, unresolved, NaN) → pass", True, q11_export_keys(export_case("ok")))
        rec("Q11 marker reason coacthist_stale_termination on a child-less stock row → pass (D1)", True,
            q11_export_keys(export_case("stale_term", markerReason=pd.array([None, "no_cash_amount", "coacthist_stale_termination"], dtype="string"))))
        rec("Q11 marker reason coacthist_stale_termination on a cash row → detected", False,
            q11_export_keys(export_case("stale_term_bad", markerReason=pd.array([None, "coacthist_stale_termination", "no_child"], dtype="string"))))
        rec("Q11 cash row without amount and without markerReason → detected", False,
            q11_export_keys(export_case("unmarked", markerReason=pd.array([None, None, "no_child"], dtype="string"))))
        rec("Q11 marker row with status ok → detected", False, q11_export_keys(export_case("status", status=["ok", "ok", "unresolved"])))
        rec("Q11 marker row carrying a value → detected", False, q11_export_keys(export_case("value", childRatioCache=[np.nan, np.nan, 0.5])))
        rec("Q11 marker reason on a bookable row → detected", False,
            q11_export_keys(export_case("mismatch", markerReason=pd.array(["no_cash_amount", "no_cash_amount", "no_child"], dtype="string"))))
        rec("Q11 duplicate key among marker rows → detected", False,
            q11_export_keys(export_case("dup", tradingItemId=["1", "1", "1"], legType=["cash", "cash", "cash"], exDate=pd.to_datetime(["2020-02-03", "2020-03-03", "2020-03-03"]),
                                        markerReason=pd.array([None, "no_cash_amount", "no_cash_amount"], dtype="string"), eventType=["cash_dividend", "other", "special_dividend"])))
        rec("Q11 export without the marker column keeps the old rule (NaN cash row → detected)", False,
            q11_export_keys(_write_no_marker(Path(td) / "old.parquet", ex_cols)))
        # Y1-B: a no-child row with a usable per-share value (status estimated) is a value leg, not a marker → pass
        rec("Q11 no-child row with childValuePerShareCache (estimated, vendor_implied) → pass", True,
            q11_export_keys(export_case("valued", markerReason=pd.array([None, "no_cash_amount", None], dtype="string"),
                                        status=["ok", "unresolved", "estimated"], isEstimate=[False, False, True], childValuePerShareCache=[np.nan, np.nan, 2.5])))
        rec("Q11 no-child row with a value but status unresolved and no marker → detected", False,
            q11_export_keys(export_case("valued_unresolved", markerReason=pd.array([None, "no_cash_amount", None], dtype="string"),
                                        childValuePerShareCache=[np.nan, np.nan, 2.5])))
    rec("Q5 same-day cash + stock are two rows", True, q5_same_day(L))
    rec("Q6 exDate on calendar", True, q6_calendar(L, cal))
    bad = L.copy(); bad["exDate"] = pd.Timestamp("2020-02-01")   # Saturday
    rec("Q6 exDate on a Saturday", False, q6_calendar(bad, cal))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--base-data", type=Path, default=DEFAULT_BASE, help="현금식 오라클 수익률 재현용 raw 종가")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        res = self_test()
        for r in res:
            print(("OK  " if r["ok"] else "BAD ") + r["case"], "| expected", r["expected"], "| got", r["got"], "|", json.dumps(r["detail"], default=str)[:160])
        n_bad = sum(not r["ok"] for r in res)
        print(f"self-test: {len(res) - n_bad}/{len(res)} cases behave as expected")
        return 1 if n_bad else 0
    from ledger_build import DEFAULT_ROOT, DEFAULT_STAGE
    ledger = a.ledger or (DEFAULT_STAGE / "events" / "ca_ledger.parquet")
    root = a.root or DEFAULT_ROOT
    out = a.out or (Path(ledger).parent.parent / "qa")
    res = run(ledger, root, out, base_data=a.base_data)
    print(json.dumps({k: (v["ok"] if isinstance(v, dict) and "ok" in v else v) for k, v in res.items() if k != "unresolved_reasons"}, indent=1, default=str))
    print("report:", out / "ledger_qa.md")
    return 0 if res["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
