#!/usr/bin/env python3
"""cache_v21/master_report.py - T1: compose `reports/T1.md` from the stage outputs and QA summaries.

Tables only reflect files under the stage; the free-text sections (해석·미해결·제안) live in NOTES below and are
kept short.  Run after master_build.py finished all steps:  python master_report.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import master_build as mb  # noqa: E402


def md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def fmt_int(x) -> str:
    try:
        return f"{int(x):,}"
    except Exception:
        return str(x)


def file_table() -> str:
    rows = []
    for p in sorted(mb.MASTER_DIR.glob("*.parquet")):
        md = pq.read_metadata(p)
        rows.append([f"master/{p.name}", fmt_int(md.num_rows), md.num_columns, f"{p.stat().st_size / 1e6:.2f}"])
    for p in sorted(mb.QA_DIR.iterdir()):
        if p.suffix in (".csv", ".md", ".json"):
            n = ""
            if p.suffix == ".csv":
                try:
                    n = fmt_int(sum(1 for _ in open(p)) - 1)
                except Exception:
                    n = ""
            rows.append([f"qa/{p.name}", n, "", f"{p.stat().st_size / 1e6:.2f}"])
    return md_table(["파일", "행수", "열수", "MB"], rows)


def schema_table() -> str:
    rows = []
    for p in sorted(mb.MASTER_DIR.glob("*.parquet")):
        sch = pq.read_schema(p)
        rows.append([p.stem, ", ".join(f"{f.name}:{f.type}" for f in sch)])
    return md_table(["파일", "컬럼:타입"], rows)


def coverage_section() -> str:
    t = mb.load_json(mb.QA_DIR / "id_mapping_coverage.json", {})
    names = {"A_no_mapping": "A. 매핑 없음", "B_gap_in_priced_period": "B. 가격 기간 중 매핑 공백",
             "C_same_day_conflict": "C. 같은 날 복수 gvkey/iid", "D_no_TIC_EXCHG_history": "D. 매핑 있으나 TIC/EXCHG 이력 없음"}
    rows = []
    for cls, nm in names.items():
        for scope in ("ALL", "everTop500", "everTop1000"):
            b = t[scope][cls]
            rows.append([nm, f"{scope} ({fmt_int(t[scope]['n_items'])}종목 / {fmt_int(t[scope]['trading_days'])}일)",
                         fmt_int(b["items"]), b["items_pct"], fmt_int(b["days"]), b["days_pct"]])
    md = md_table(["분류", "범위", "종목", "종목 %", "거래일", "거래일 %"], rows)
    cov = (mb.QA_DIR / "id_mapping_coverage.md").read_text()
    # Top500 conflict list + reverse conflicts are already tabulated in the qa markdown; pull those blocks
    blocks = cov.split("## ")
    extra = "\n\n".join("## " + b.strip() for b in blocks[1:] if b.startswith("Top500 충돌") or b.startswith("역방향"))
    return md + "\n\n" + extra


def lifecycle_section() -> str:
    s = mb.load_json(mb.QA_DIR / "lifecycle_summary.json", {})
    lc = pd.read_parquet(mb.MASTER_DIR / "lifecycle.parquet")
    piv = lc.pivot_table(index="endReason", columns="status", values="tradingItemId", aggfunc="count", fill_value=0)
    piv["total"] = piv.sum(axis=1)
    piv.loc["total"] = piv.sum()
    rows = [[idx] + [fmt_int(v) for v in r] for idx, r in zip(piv.index, piv.to_numpy())]
    t1 = md_table(["endReason \\ status"] + list(piv.columns), rows)
    piv2 = lc.pivot_table(index="lastTradeSource", columns="status", values="tradingItemId", aggfunc="count", fill_value=0)
    rows2 = [[idx] + [fmt_int(v) for v in r] for idx, r in zip(piv2.index, piv2.to_numpy())]
    t2 = md_table(["lastTradeSource \\ status"] + list(piv2.columns), rows2)
    ex = s.get("exceptions_by_kind", {})
    t3 = md_table(["예외 종류", "건수"], [[k, fmt_int(v)] for k, v in sorted(ex.items(), key=lambda kv: -kv[1])])
    st = s.get("self_test", {})
    t4 = md_table(["self-test", "결과"], [
        [f"asOf−1 종가 종목 {st.get('n_test_items')}개를 asOf−3({st.get('d_m3')})으로 이동 → active 유지", f"{st.get('moved_to_asOf_minus3_still_active')}/{st.get('n_test_items')} → {'PASS' if st.get('test1_pass') else 'FAIL'}"],
        [f"같은 종목을 asOf−15({st.get('d_m15')})로 이동(마커 없음) → active 아님(판정기 작동 확인)", f"{st.get('moved_to_asOf_minus15_status_counts')} → {'PASS' if st.get('test2_can_fail') else 'FAIL'}"],
        ["합성 합병(secEnd=마지막종가=asOf−2, status 8) 20개 → confirmed/merger", f"{st.get('synthetic_merger_confirmed')}/20 → {'PASS' if st.get('test3_pass') else 'FAIL'}"],
        ["롤링 마커 반례(secEnd=asOf−2, 종가는 asOf−1까지) 20개 → active 유지", f"{st.get('rolling_marker_stays_active')}/20 → {'PASS' if st.get('test4_pass') else 'FAIL'}"],
    ])
    hdr = (f"- asOf(ciq 최대 pricingdate) = {s.get('asOf_ciq_max_pricingdate')}, observedAt = {s.get('observedAt')}, n = {fmt_int(s.get('n'))}\n"
           f"- 확정 마커 = securityEndDate(≤asOf이고 그 뒤 종가 없음) / status {s.get('end_statuses_used')} / status 2(In bankruptcy)는 10거래일 이상 무종가일 때만 / events 종료. "
           f"**마커 없이는 무종가 기간만으로 confirmed 하지 않음(리드 결정 2026-09-16)** → 마커 없는 무종가 종목은 provisional/data_gap. 최근 판정 = {s.get('recent_tdays')} 거래일\n"
           f"- lifecycle 컬럼 추가(리드 결정): `firstPriceDate`(ciq 첫 종가일, 이어붙인 과거 포함, 워밍업용) · `firstTradeSource`('ciq'/'lineage'/'mi') · `firstTradeDate`는 lineage 이어붙임 쌍에서 올려진 값\n"
           f"- events 원천: `{s.get('events_source')}`")
    return hdr + "\n\n### status × endReason\n\n" + t1 + "\n\n### status × lastTradeSource\n\n" + t2 + \
        "\n\n### 예외 목록(`qa/lifecycle_exceptions.csv`) 종류별 건수\n\n" + t3 + "\n\n### self-test\n\n" + t4


def lift_section() -> str:
    fd = mb.load_json(mb.QA_DIR / "lifecycle_first_date_summary.json", {})
    g = mb.load_json(mb.QA_DIR / "gates.json", {})
    b = g.get("G1b_lineage_same_line_after_lift", {})
    rows = [["대상: relation∈{relist,rename}·confidence∈{high,medium}·gvkeyiid/events 근거·lifecycle 구간 겹침·양쪽 종목 집합 내", fmt_int(fd.get("pairs_window_overlap"))],
            ["  이어붙임 확인(공통 종가일의 as-quoted 종가 일치 ≥90% 또는 상수비율 ≤6단계로 ≥90% 설명)", f"{fmt_int(fd.get('pairs_stitched_copy'))} = {fd.get('stitch_test_counts')}"],
            ["  미적용: 공통 종가일에서 가격이 다름(서로 다른 라인, MCO/DNB·MEE/FLR·TRY.B/WEN…) / 공통 종가일 없음", f"{fd.get('pairs_not_lifted')} → `qa/lineage_not_lifted.csv`, lineage confidence를 'low'로 내림"],
            ["  역할 교환(옛 id가 새 id보다 늦게 끝남 → 늦게 끝나는 쪽을 새 id로)", fmt_int(fd.get("pairs_role_swapped"))],
            ["firstTradeDate 올린 새 id 수 / 근거", f"{fmt_int(fd.get('new_ids_adjusted'))} / {fd.get('by_source')} (ciq가 최댓값인 경우 {fmt_int(fd.get('kept_ciq_first'))})"],
            ["  올린 값이 자기 lastTradeDate를 넘어 미적용", fmt_int(fd.get("skipped_first_after_last"))],
            ["  옛 id 종료와 새 id 시작 사이 빈 거래일(mi 첫 종가가 늦은 경우, CCO 23일 등)", f"{fmt_int(fd.get('gap_days_between_old_and_new_total'))}일 / {fmt_int(fd.get('gap_days_gt0_ids'))}종목"],
            ["적용 후 같은 라인 쌍 잔여 동시 tradable", f"{fmt_int(fd.get('residual_same_line_overlap_pairs'))}쌍 / {fmt_int(fd.get('residual_same_line_overlap_days'))}거래일 → 원천별 {b.get('residual_by_source')}"],
            ["  그중 tradable_exclusions로 덮이지 않는 잔여", f"{b.get('residual_not_covered_by_exclusions')} (가격이 다른 별개 라인 31쌍 + 공통일 없음 6 + 자기 종료 초과 3; 목록 `qa/lineage_residual_overlap.csv`)"]]
    t1 = md_table(["항목", "값"], rows)
    adj = pd.read_csv(mb.QA_DIR / "lifecycle_first_date_adjustments.csv") if (mb.QA_DIR / "lifecycle_first_date_adjustments.csv").exists() else pd.DataFrame()
    t2 = ""
    if len(adj):
        cur = pd.read_parquet(mb.MASTER_DIR / "current.parquet").set_index("tradingItemId").symbol
        a = adj[adj.applied].copy()
        a["lift_days"] = (pd.to_datetime(a.firstTradeDate) - pd.to_datetime(a.ciqFirst)).dt.days
        a = a.sort_values("lift_days", ascending=False).head(12)
        t2 = md_table(["새 id", "symbol", "옛 id", "ciq 첫 종가(firstPriceDate)", "옛 last+1", "mi 첫 종가", "firstTradeDate", "근거"],
                      [[r.tradingItemId, cur.get(r.tradingItemId), r.oldId, str(r.ciqFirst)[:10], str(r.oldLastPlus1)[:10], str(r.miFirst)[:10] if pd.notna(r.miFirst) else "",
                        str(r.firstTradeDate)[:10], r.firstTradeSource] for r in a.itertuples()])
    return t1 + "\n\n### 올림 폭 상위 12\n\n" + t2


def exclusions_section() -> str:
    p = mb.QA_DIR / "tradable_exclusions_detail.csv"
    g = mb.load_json(mb.QA_DIR / "gates.json", {}).get("G1_after_exclusions", {})
    if not p.exists():
        return "없음"
    d = pd.read_csv(p)
    hdr = (f"- `master/tradable_exclusions.parquet` {fmt_int(len(d))}행 (tradingItemId, fromDate, toDate, reason, preferredItemId). 같은 securityId 두 id가 동시 tradable인 구간에서 "
           f"달러거래대금(ciq priceclose×volume 합 = raw close×raw volume) 작은 쪽을 제외. 동률(둘 다 같은 값)은 OTC(134/133) 아닌 쪽 → 종가일 많은 쪽 → 작은 id 우선.\n"
           f"- 적용 후 G1 잔여 {g.get('pairs_left')}쌍 / {g.get('days_left')}일, 제외 거래일 합 {fmt_int(g.get('excluded_days_total'))}.")
    t = md_table(["securityId", "제외 id", "sym", "exch", "선호 id", "sym", "exch", "from", "to", "거래일", "$거래대금 제외/선호", "근거"],
                 [[r.securityId, r.excludedId, r.excludedSymbol, r.excludedExchange, r.preferredItemId, r.preferredSymbol, r.preferredExchange, str(r.fromDate)[:10], str(r.toDate)[:10],
                   fmt_int(r.overlapTradingDays), f"{r.dollarVolumeExcluded:.3g} / {r.dollarVolumePreferred:.3g}", r.basis] for r in d.itertuples()])
    return hdr + "\n\n" + t


def lineage_section() -> str:
    s = mb.load_json(mb.QA_DIR / "lineage_summary.json", {})
    rows = [["후보 행(원천별)", str(s.get("by_source"))], ["병합 후 쌍", fmt_int(s.get("n_pairs"))], ["relation", str(s.get("by_relation"))],
            ["confidence", str(s.get("by_confidence"))], ["원천 조합", str(s.get("by_sources"))],
            ["ratio 알려진 쌍", fmt_int(s.get("ratio_known"))],
            ["같은 라인(rename/relist) 쌍", fmt_int(s.get("same_line_pairs"))],
            ["  그중 가격 겹침 > 0일 / > 250일", f"{fmt_int(s.get('same_line_pairs_overlap_gt0'))} / {fmt_int(s.get('same_line_pairs_overlap_gt250'))}"],
            ["필수 포착 쌍", str(s.get("must_catch"))]]
    t1 = md_table(["항목", "값"], rows)
    top = s.get("top_overlap_same_line", [])
    t2 = md_table(["oldId", "old", "newId", "new", "relation", "겹침 거래일", "겹침 시작", "겹침 끝", "원천"],
                  [[r["oldId"], r["oldSymbol"], r["newId"], r["newSymbol"], r["relation"], fmt_int(r["overlapDays"]) if r["overlapDays"] == r["overlapDays"] else "",
                    str(r["overlapFirst"])[:10], str(r["overlapLast"])[:10], r["sources"]] for r in top])
    return t1 + "\n\n### 같은 라인 쌍 중 가격 겹침 상위 15 (ciq가 새 id에 과거를 이어붙인 경우)\n\n" + t2


def symbolset_section() -> str:
    g = mb.load_json(mb.QA_DIR / "gates.json", {}).get("symbol_set", {})
    rows = [["instruments 종목 수", fmt_int(g.get("n_instruments"))],
            ["T2 공유 파일", f"{g.get('shared_file')} → {'있음' if g.get('shared_present') else '없음: ' + str(g.get('status'))}"]]
    if g.get("shared_present"):
        rows += [["T2 종목 수 / isNew", f"{fmt_int(g.get('n_shared'))} / {g.get('shared_isNew_count')} (mtime {g.get('shared_mtime')})"],
                 ["T1에만 있음 / T2에만 있음", f"{fmt_int(g.get('n_T1_only'))} / {fmt_int(g.get('n_T2_only'))} → `qa/symbolset_diff.csv`"]]
    pf = g.get("per_file", {})
    for name, v in pf.items():
        rows.append([f"master/{name}", ", ".join(f"{k}={val}" for k, val in v.items())])
    rows.append(["마스터 파일 종목 집합 정합", "PASS" if g.get("all_files_consistent") else "FAIL"])
    return md_table(["항목", "값"], rows)


def gates_section() -> str:
    g = mb.load_json(mb.QA_DIR / "gates.json", {})
    st1, st2 = g.get("self_test_overlap_checker", {}), g.get("self_test_same_security_checker", {})
    g1, g2 = g.get("G1_same_security_both_tradable", {}), g.get("G2_interval_overlaps", {})
    rows = [["구간 겹침 검사기 반례", f"깨끗한 프레임 {st1.get('clean_frame_overlaps')}건 / 심은 겹침 {st1.get('planted_frame_overlaps')}건 → {'PASS' if st1.get('pass') else 'FAIL'}"],
            ["같은 securityId 동시 tradable 검사기 반례", f"깨끗한 쌍 {st2.get('clean_pairs')} / 심은 쌍 {st2.get('planted_pairs')} → {'PASS' if st2.get('pass') else 'FAIL'}"],
            ["G1 같은 securityId 두 id 동시 tradable (lifecycle만)", f"{fmt_int(g1.get('pairs'))} 쌍, 겹침 거래일 합 {fmt_int(g1.get('total_overlap_days'))} (목록 `qa/gate_same_security_overlap.csv`)"],
            ["G1 tradable_exclusions 적용 후", f"{fmt_int(g.get('G1_after_exclusions', {}).get('pairs_left'))} 쌍 / {fmt_int(g.get('G1_after_exclusions', {}).get('days_left'))} 일 (목표 0)"],
            ["G1b lineage 같은 라인 쌍 동시 tradable (firstTradeDate 올림 후)", f"{fmt_int(g.get('G1b_lineage_same_line_after_lift', {}).get('residual_same_line_overlap_pairs'))} 쌍 / {fmt_int(g.get('G1b_lineage_same_line_after_lift', {}).get('residual_same_line_overlap_days'))} 일; exclusions로 덮이지 않는 잔여 {g.get('G1b_lineage_same_line_after_lift', {}).get('residual_not_covered_by_exclusions')} = 가격이 다른 별개 라인(의도적 미적용)"],
            ["G2 identifier_history 같은 (id,item,value) 구간 겹침", fmt_int(g2.get("identifier_history_same_item_same_value"))],
            ["G2 identifier_history 같은 (id,item) 구간 겹침(값 무관)", f"{fmt_int(g2.get('identifier_history_same_item_any_value'))} (quality별 {g2.get('identifier_history_same_item_any_value_by_quality')})"],
            ["G2 classification_history 구간 겹침", fmt_int(g2.get("classification_history"))],
            ["G2 primary_history 구간 겹침", fmt_int(g2.get("primary_history"))],
            ["G2 id_mapping(conflictFlag=False) 구간 겹침", fmt_int(g2.get("id_mapping_nonconflict_rows"))]]
    t1 = md_table(["게이트 / self-test", "결과"], rows)
    top = g1.get("top", [])
    t2 = md_table(["securityId", "idA", "symbolA", "exchA", "idB", "symbolB", "exchB", "겹침 시작", "겹침 끝", "겹침 거래일"],
                  [[r["securityId"], r["idA"], r.get("symbolA"), r.get("exchA"), r["idB"], r.get("symbolB"), r.get("exchB"),
                    str(r["overlapFrom"])[:10], str(r["overlapTo"])[:10], fmt_int(r["overlapTradingDays"])] for r in top])
    return t1 + "\n\n### G1 상위 20 쌍\n\n" + t2


def history_section() -> str:
    s = mb.load_json(mb.QA_DIR / "history_summary.json", {})
    rows = [["사용 파일", f"{fmt_int(s.get('n_files_used'))}개 ({s.get('first_file')} ~ {s.get('last_file')}, mtime > 2025-02-06), 읽기 불가 건너뜀 {len(s.get('skipped_unreadable', []))}개 {s.get('skipped_unreadable')}"],
            ["long 관측 행 / 종목", f"{fmt_int(s.get('n_long_rows'))} / {fmt_int(s.get('n_ids_in_v1'))}"],
            ["sector 컬럼 첫 관측 파일", str(s.get("first_file_with_sector_column"))],
            ["classification_history 행", f"{fmt_int(s.get('classification_rows'))} (quality {s.get('classification_by_quality')}, source {s.get('classification_by_source')})"],
            ["  sector 컬럼 없는 초기 구간 채움(리드 결정 (4))", f"{s.get('classification_fill')} → 다음 v1 관측 업종으로 채운 구간은 source 'v1_daily_master+industry_map'/observed, DB 현재 업종으로 채운 구간은 'db_current+industry_map'/current_only; 남은 sector NaN 행 {fmt_int(s.get('classification_sector_nan_rows'))}"],
            ["  sector/industry 값 변화 관측 종목", fmt_int(s.get("ids_with_sector_change"))],
            ["primary_history 행", f"{fmt_int(s.get('primary_rows'))} (quality {s.get('primary_by_quality')})"],
            ["  primary 플래그 변화 관측 종목", fmt_int(s.get("ids_with_primary_change"))]]
    return md_table(["항목", "값"], rows)


def symbol_section() -> str:
    sym = mb.load_json(mb.WORK / "symbol_set.json", {})
    fin = mb.load_json(mb.WORK / "final_ids.json", {})
    inst = pd.read_parquet(mb.MASTER_DIR / "instruments.parquet")
    cur = pd.read_parquet(mb.MASTER_DIR / "current.parquet")
    idm = pd.read_parquet(mb.MASTER_DIR / "id_mapping.parquet")
    rows = [["프로토타입 master", fmt_int(sym.get("n_proto"))], ["오늘 규칙 union 후보", fmt_int(sym.get("n_candidates"))],
            ["프로토 ∖ 후보 (규칙에서 빠진 프로토 종목)", fmt_int(sym.get("n_proto_not_candidate"))],
            ["후보 ∖ 프로토 (가격 유무 확인 대상)", fmt_int(sym.get("n_candidate_not_proto"))],
            ["최종 종목 집합(프로토 ∪ 2000-01-03 이후 종가 1개 이상 신규)", fmt_int(fin.get("n_ids"))],
            ["  신규(inProtoV2=False)", fmt_int((~inst.inProtoV2).sum())],
            ["current: currentPrimary True / sector 결측 / gvkey 결측", f"{fmt_int(cur.currentPrimary.sum())} / {fmt_int(cur.sector.isna().sum())} / {fmt_int(cur.gvkey.isna().sum())}"],
            ["current: securityEndDate 있음 / tradingItemStatusId 분포", f"{fmt_int(cur.securityEndDate.notna().sum())} / {cur.tradingItemStatusId.value_counts().to_dict()}"],
            ["id_mapping 행 / 매핑된 종목 / conflictFlag 행", f"{fmt_int(len(idm))} / {fmt_int(idm.tradingItemId.nunique())} / {fmt_int(idm.conflictFlag.sum())}"],
            ["id_mapping 날짜 상태 (start,end)", str(idm.assign(s=idm.symbolStartDate.notna(), e=idm.symbolEndDate.notna()).groupby(['s', 'e']).size().to_dict())]]
    new = inst[~inst.inProtoV2]
    t = md_table(["항목", "값"], rows)
    if len(new):
        k = cur.set_index("tradingItemId")
        rows2 = [[r.tradingItemId, k.symbol.get(r.tradingItemId), r.companyName, k.exchangeId.get(r.tradingItemId), r.inV1Rule, r.everMainExchange]
                 for r in new.head(30).itertuples()]
        t += "\n\n신규 종목(상위 30):\n\n" + md_table(["tradingItemId", "symbol", "companyName", "exchangeId", "inV1Rule", "everMainExchange"], rows2)
    return t


NOTES = """
## 9. 해석·미해결·제안

### 9-0. 리드 결정(2026-09-16 후속) 반영 내역
1. 마커 없는 무종가 종목은 `provisional/data_gap`(352건, 332건이 Pink Sheets status 15). 마커 = securityEndDate·status 8/11/4/5·status 2는 10거래일 이상 무종가일 때·events. lastTradeDate는 그대로.
2. 이어붙임 중복 해소: lineage relist/rename(high/medium, gvkeyiid·events 근거) 중 lifecycle 구간이 겹치는 842쌍 → **이어붙임 복사본으로 확인된 805쌍**(공통 종가일 as-quoted 종가 일치 ≥90% 749 + 상수비율(분할·교환비 재계산) 56)에서 새 id의 `firstTradeDate = max(ciq 첫 종가, 옛 id lastTradeDate+1거래일, mi 첫 종가)`로 올림(802종목: lineage 798·mi 4). 원래 ciq 첫 종가는 `firstPriceDate`, 근거는 `firstTradeSource`. 가격이 다른 31쌍(MCO/DNB, MEE/FLR, TRY.B/WEN, USW/Q, VTR/VC 등 Compustat gvkey만 공유하는 별개 회사)은 올리지 않고 lineage confidence를 'low'로 내려 evidence에 검사값 기록 — 이 부분은 리드 규칙에 "가격 동일성 검사"를 더한 해석임(올리면 무디스 1998~2019 실거래가 tradable에서 사라짐). `--lift-all`은 두지 않았음; 필요하면 `STITCH_MIN_EQUAL_SHARE=0`으로 전부 올릴 수 있음.
3. 같은 securityId 이중 트레이딩아이템 10쌍 → `master/tradable_exclusions.parquet`(달러거래대금 작은 쪽 제외, 14,725거래일). 적용 후 G1 = 0.
4. classification_history 2025-02-03~02-27 NaN 구간 6,316개 채움: 다음 v1 관측 업종으로 6,248(source 'v1_daily_master+industry_map'·observed), DB 현재 업종으로 48('db_current+industry_map'·current_only), 업종 정보가 어디에도 없는 20구간은 NaN 유지. 구간은 병합하지 않고 출처를 남김.
5. identifier_history effFrom/effThru timestamp[ms]는 계약 편차로 허용됨(아래 9-1).

### 9-1. 계약과 다르게 둔 것(허용된 편차) / 해석
- 검증: `ciqgvkeyiid.objectid` = tradingItemId 확인(AAPL 2590360→001690/01, RIG 51757943→028338/01, SIVB 22534033→017120/01, 각 1행·activeflag 1·기간 NULL). id_mapping은 DB 값 그대로(NULL 기간 유지, 종료일 임의 채움 없음). v1 일별 파일 인덱스 = tradingItemId(int64, 유일) 확인.
- `identifier_history.effFrom/effThru`는 계약의 열림 sentinel 2900-01-01을 담기 위해 **timestamp[ms]**(ns는 2262년까지만 표현; 리드 허용). 나머지 날짜 컬럼은 모두 timestamp[ns]. 로더는 `effThru >= 2262-01-01`을 열림으로 취급할 것.
- `lifecycle` 추가 컬럼 `firstPriceDate`·`firstTradeSource`는 계약 §3-4에 없던 컬럼(리드 결정 (2)). `master/tradable_exclusions.parquet`는 9번째 마스터 파일(리드 결정 (3)).
- `mappingQuality`: 'period_match' = 매핑 행에 symbolStartDate/EndDate 중 하나라도 있는 기간형 행(4,383행), 'open_period' = 두 날짜 모두 NULL인 열린 행(17,455행; ciqgvkeyiid 전체 216,087행 중 183,944행이 이 형태), 'ambiguous' = 같은 (gvkey,iid,item,efffrom) 이력이 겹치는 기간에 둘 이상 tradingItemId에 붙거나 conflictFlag인 경우(0건). 이력 구간은 매핑 기간으로 잘라 넣었음(CCO: 옛 id 2005-08-22~2019-05-01, 새 id 2019-05-02~).
- provisional 352건 = 마커 없이 ciq·mi 둘 다 10거래일 이상 무종가(전부 bothSilent; 한쪽만 무종가인 종목 0 — 상장 중 종목의 ciq·mi 마지막 종가일 전부 일치). lastTradeSource 'mi' 472건은 ciq가 securityEndDate까지 승계사 가격을 게시한 꼬리를 mi가 자른 경우(BHI 2017-07-31→07-03), 'event' 1건.
- lifecycle 예외의 해석: securityEndDate가 lastTradeDate보다 5일 넘게 늦은 1,036건(중앙값 52일, 최대 7,395일 — 종료일은 증권 단위라 마지막 거래 뒤에 찍힘, min 규칙이 자름), ciq·mi 마지막 종가 5일 초과 차이 523건(ciq가 늦음 389·mi가 늦음 134, 40일 초과 175), event가 ciq보다 이른 159건(BHI형 승계사 가격 꼬리, event가 자름), mi 종가 전무 128건(ciq만으로 판정), ciq 첫 종가가 mi보다 250일 이상 이른 80건(RIG 5,684일·CCO 4,954·OA 8,896 — RIG처럼 옛 라인 id가 DB에 없는 단일 id 이어붙임은 건드리지 않음, 이 목록으로 식별), null-close 꼬리 7건. 2026-09-11 롤링 마커·미래 종료일·종료일 뒤 거래는 0건(securityEndDate 최대 2026-09-08).
- `endReason`: merger = status 8 또는 events ACQ/MGD/승계자 있음, bankruptcy = status 2 또는 BK11/BKR/LIQ, delist_other = 종료일·status 4/5/11·N/C·PVT·SEC·OTH·DFF·SPN, unknown = 힌트 없음(현재 0건), data_gap = provisional. 우선순위 merger > bankruptcy > delist_other.
- `classification_history`/`primary_history`: 첫 구간 effectiveFrom(2025-02-03)은 관측 시작일이라 좌측 절단값. 관측 사이 8일 이상 비면(v1 마스터에서 빠졌다 돌아온 경우, 55구간·최대 589일) effectiveTo를 마지막 관측일로 닫아 미관측 구간을 비워 둠(겹침 0·빈틈 허용). db_current 추가 행: classification 3종목(실제 업종 변경 LOGC 1, ciqcompany 행이 사라져 DB 업종이 NULL이 된 BBT 1959688085·SUNC 1967976996), primary 30종목(상폐 뒤 DB primaryFlag 1→0 등) — 이 행들의 effectiveFrom=asOf는 관측일이며 실제 변경일은 미관측 구간 안에 있음.
- v1 일별 파일의 identity 필드는 파일 날짜가 아니라 생성 시점(mtime, 보통 D+1·최대 D+6)의 DB 값임. 계약대로 observedAt=effectiveFrom=파일 날짜로 두었음 → 제안: observedAt을 mtime 날짜로.
- 거래일 계산은 IBM·XOM·KO·JPM·AAPL·MSFT·GE의 ciq 종가일 합집합(1968-01-02~2026-09-15, 14,774일)을 캘린더로 씀[추정: 거래소 휴장 파일 없음]. 10영업일 판정·firstTradeDate 올림(+1거래일)·커버리지 일수가 이 캘린더 기준.
- lineage: 체인은 인접 쌍만(A→B→C에서 A→C 제외). 같은 라인 쌍 1,464 중 875쌍이 가격 구간이 겹치고 631쌍은 250일 초과(ciq 이어붙임). merger 쌍의 겹침은 두 회사가 동시에 거래된 기간이라 별도 의미 없음(`overlapKind=coexist_two_companies`). ratio는 events가 준 1,702쌍만, 나머지 NaN. events에 승계자=자기자신인 행 4건(AK Steel 2586152 등, 생성기 아티팩트) 제외. 승계자가 종목 집합 밖인 쌍 72(해외·비상장 인수자).
- `current.sector` 결측 526 = simpleIndustryDescription 결측 526(그중 440은 ciqcompany 행 없음); sector_info.csv 미매핑 업종 0.

### 9-2. 미해결
- `sec_idhist` ISIN 이력은 미국 iid(01…)에는 없고 해외 iid(01W·90W…)에만 있어 우리 종목 ISIN 행 0건(unlicensed ISIN 패키지 범위). identifier_history는 TIC·EXCHG만.
- Compustat 기업행사·식별자 패키지가 2026-02-19에서 멈춰 있음(xfeed_source_findings §7) → sec_idhist·ciqgvkeyiid의 그 이후 변화(EQR→VMRK 등 최근 개명·재상장) 미반영. 매핑 없는 종목 1,156(everTop1000 0)에는 2026년 신규 상장(CBRS·FDXF·SPCX 등)도 포함.
- events.parquet은 프로토타입 캐시 기준(웨이브 2 재생성 예정). lifecycle.eventEndDate·lineage merger 행의 원천 버전을 evidence(sha256 앞 12자)와 `qa/*_summary.json`에 기록. 재생성 후 `master_build.py --steps lifecycle,lineage,adjust,qa`(≈30s, 집계 캐시 `_work/` 재사용) + `master_report.py` 재실행 필요.
- v1 일별 마스터 20260428 파일은 권한(600, airflow01)으로 읽을 수 없어 건너뜀(1일 결손).
- 같은 라인인데 올리지 못한 6쌍(공통 종가일 없음: AZAC 1,479거래일 등 — 두 id의 종가일이 서로 배타적이라 중복은 없음)과 자기 종료를 넘는 3쌍(A→B→A형 매핑)은 `qa/lineage_residual_overlap.csv`에 남김. 리드 판단 필요 시 exclusions 규칙(달러거래대금)으로 넘길 수 있음.
- id_mapping B(가격 기간 중 매핑 공백) Top500 113종목·490,100일은 거의 전부 새 id에 이어붙인 과거(매핑 symbolStartDate 이전: DINO 1968~2022, APA, TPL, XRX, DD, DXC…)로, firstTradeDate 올림 뒤에는 `firstPriceDate~firstTradeDate−1` 워밍업 구간에 해당함. 계약대로 매핑은 채우지 않았음.
"""


def main() -> int:
    parts = ["# T1 보고서 — 마스터 이력·ID 매핑·lifecycle·lineage (cache_v2.1)", "",
             f"작성 {mb.now()} · 코드 `cpp-engine/tools/data/cache_v21/master_{{build,history,lifecycle,lineage,qa,report}}.py` · 스테이지 `stage_v21/T1/`", "",
             "## 1. 종목 집합·current·id_mapping 요약", "", symbol_section(), "",
             "### 종목 집합 정합 (T2 shared/symbols_v21.parquet 대조, 마스터 8파일 + tradable_exclusions vs instruments)", "", symbolset_section(), "",
             "## 2. 산출 파일", "", file_table(), "", "### 스키마", "", schema_table(), "",
             "## 3. ID 매핑 커버리지 4분류 (전체 / everTop500 / everTop1000)", "", coverage_section(), "",
             "## 4. lifecycle", "", lifecycle_section(), "",
             "### 4-1. lineage 이어붙임 중복 해소 (firstTradeDate 올림, 리드 결정 (2))", "", lift_section(), "",
             "### 4-2. 같은 securityId 이중 트레이딩아이템 → tradable_exclusions (리드 결정 (3))", "", exclusions_section(), "",
             "## 5. lineage", "", lineage_section(), "",
             "## 6. 게이트·self-test", "", gates_section(), "",
             "## 7. classification_history / primary_history (v1 일별 마스터)", "", history_section(), "",
             "## 8. 실행 기록", "", run_section(), NOTES]
    mb.REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    mb.REPORT_PATH.write_text("\n".join(parts))
    print(f"wrote {mb.REPORT_PATH}")
    return 0


def run_section() -> str:
    log = mb.STAGE / "build.log"
    lines = log.read_text().splitlines() if log.exists() else []
    keys = ("chunk 1/", "chunk 39/", "wrote ciq_agg", "wrote mi_agg", "trading calendar", "candidates:", "symbol set:", "proceeding",
            "start steps", "done in", "not readable")
    keep, seen = [], set()
    for l in lines:
        if any(k in l for k in keys):
            key = l.split(" INFO ")[-1].split(" WARNING ")[-1][:80]
            if key in seen and "start steps" not in l and "done in" not in l:
                continue
            seen.add(key)
            keep.append(l)
    note = ("집계는 ciqpriceequity 39청크(1000 id, 17~24s/청크, 833s)·miprice 39청크(973s)를 세션 1개로 순차 실행(T2가 COPY 3세션을 쓰고 있어 "
            "`wait_for_quiet` 600s 후 진행, 워커 1로 낮춤). 집계 뒤 단계(instruments~qa)는 DB 19s·총 45s; history~qa 재실행 20.7s, "
            "peak RSS 1.94GB(/usr/bin/time -v). 스레드 ≤ 8(OMP 등 환경변수), 스테이지 총 용량 약 20MB.")
    return note + "\n\n```\n" + "\n".join(keep[-30:]) + "\n```"


if __name__ == "__main__":
    sys.exit(main())
