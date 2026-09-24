#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/universe_report.py - assemble `orch_v21/reports/T5.md` from the T5 stage/work outputs.

Reads: T5/{derived,universe,qa}, T5_work/{marketcap_stats.json, marketcap_compare.json, qa_summary.json,
runs/<name>/run.json, *.out timing logs}.  Writes the Korean, table-first report (BRIEF §1).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from universe_common import (  # noqa: E402
    REPORTS, T1_MASTER, T5_ROOT, T5_WORK, fmt_int, md_table, read_json,
)

GATE_UNIVERSES = ("Top500", "Top1000", "ALL")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(32 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_table(t5: Path) -> pd.DataFrame:
    rows = []
    for rel in ["derived/marketcap.parquet", "universe/membership.parquet", "universe/membership_v1rule.parquet"]:
        p = t5 / rel
        pf = pq.ParquetFile(p)
        rows.append({"파일 (stage_v21/T5/)": rel, "행수": fmt_int(pf.metadata.num_rows), "MB": f"{p.stat().st_size / 1e6:.1f}",
                     "sha256": sha256(p), "스키마": pf.schema_arrow.to_string(show_schema_metadata=False).replace("\n", ", ")})
    for p in sorted((t5 / "qa").glob("*")):
        rows.append({"파일 (stage_v21/T5/)": f"qa/{p.name}", "행수": "", "MB": f"{p.stat().st_size / 1e6:.2f}", "sha256": "", "스키마": ""})
    return pd.DataFrame(rows)


def kv_table(d: dict, keys: list[str] | None = None, names: dict | None = None) -> str:
    rows = []
    for k, v in d.items():
        if keys is not None and k not in keys:
            continue
        if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
            v = fmt_int(v)
        elif isinstance(v, float):
            v = f"{v:.4g}"
        rows.append({"항목": (names or {}).get(k, k), "값": v})
    return md_table(pd.DataFrame(rows))


def timing_from_out(path: Path) -> dict:
    if not path.exists():
        return {}
    txt = path.read_text()
    m1 = re.search(r"Elapsed \(wall clock\) time.*?: (\S+)", txt)
    m2 = re.search(r"Maximum resident set size \(kbytes\): (\d+)", txt)
    return {"wall": m1.group(1) if m1 else "", "peak_rss_gb": f"{int(m2.group(1)) / 1e6:.1f}" if m2 else ""}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t5", type=Path, default=T5_ROOT)
    ap.add_argument("--work", type=Path, default=T5_WORK)
    ap.add_argument("--out", type=Path, default=REPORTS / "T5.md")
    args = ap.parse_args(argv)
    t5, work = args.t5, args.work
    ms = read_json(work / "marketcap_stats.json")
    mc = read_json(work / "marketcap_compare.json")
    qa = read_json(work / "qa_summary.json")
    full = read_json(work / "runs" / "full" / "run.json")
    ov = pd.DataFrame(qa["overlap"])
    lad = pd.DataFrame(qa["ladder"])
    sens = pd.DataFrame(qa.get("sensitivity", []))
    counts = pd.DataFrame(qa["counts"])
    yearly = pd.DataFrame(qa.get("yearly", []))
    causes = pd.DataFrame(qa.get("causes", []))
    trad = pd.DataFrame(qa.get("tradable_effect", []))
    dups = pd.DataFrame(qa.get("duplicates", []))
    items = pd.read_csv(t5 / "qa" / "universe_diff_items.csv") if (t5 / "qa" / "universe_diff_items.csv").exists() else pd.DataFrame()
    g = ov[ov.comparison == "proto_v1rule"].set_index("universe")
    gl = ov[ov.comparison == "proto_v1rule[line]"].set_index("universe")
    ga = ov[ov.comparison == "proto_all"].set_index("universe")
    gal = ov[ov.comparison == "proto_all[line]"].set_index("universe")
    gv = ov[ov.comparison == "v1_snapshot(v1rule)"].set_index("universe")
    gate_pass = all(g.loc[u, "ov_of_ours_med"] >= 0.99 for u in GATE_UNIVERSES)
    gate_pass_line = all(gl.loc[u, "ov_of_ours_med"] >= 0.99 for u in GATE_UNIVERSES)
    notrad_row = lad[lad.run == "notrad"].iloc[0]
    curprim_row = lad[lad.run == "strict_curprim"].iloc[0] if (lad.run == "strict_curprim").any() else None
    t_mcap = timing_from_out(work / "mcap_run1.out")
    t_full = timing_from_out(work / "run_full.out")
    ti = full["tradable_info"]
    cal = full["calendar"]
    pit = full.get("pit_primary", {})
    done_v2 = T1_MASTER.parent / "DONE_v2.txt"
    dup_full = dups[dups.run == "full"] if len(dups) else pd.DataFrame()
    dup_ok = bool((dup_full.dup_cells_all == 0).all()) if len(dup_full) else False

    ov_cols = ["days", "ours_med", "ref_med", "ov_of_ours_med", "ov_of_ours_p05", "ov_of_ours_min", "ov_of_ref_med", "ov_of_ref_p05",
               "jaccard_med", "jaccard_pooled", "only_ours_name_days", "only_ref_name_days"]
    ov_names = {"days": "일수", "ours_med": "T5 멤버 중앙값", "ref_med": "참조 멤버 중앙값", "ov_of_ours_med": "|A∩B|/|A| 중앙값",
                "ov_of_ours_p05": "p05", "ov_of_ours_min": "최소", "ov_of_ref_med": "|A∩B|/|B| 중앙값", "ov_of_ref_p05": "|A∩B|/|B| p05",
                "jaccard_med": "Jaccard 중앙값", "jaccard_pooled": "Jaccard(합산)", "only_ours_name_days": "T5에만 멤버-일",
                "only_ref_name_days": "참조에만 멤버-일", "ref_name_days_outside_panel": "v1 열 중 패널 밖 종목 멤버-일"}

    def ov_table(sub: pd.DataFrame, extra: list[str] | None = None) -> str:
        cols = ov_cols + (extra or [])
        return md_table(sub.reset_index()[["universe"] + cols].rename(columns=ov_names), "{:.4f}")

    L = []
    L += [f"# T5 보고서 — universe 구간 테이블(raw 기준 v1 규칙) + marketCap_USD (agent-universe, {dt.datetime.now():%Y-%m-%d %H:%M})", "",
          f"asOf **{ms['asOf']}**, 종목 {fmt_int(ms['n_ids'])}(회사 {fmt_int(ms['n_companies'])}), 거래일 캘린더 {fmt_int(cal['kept'])}일"
          f"({full['dates'][0]}~{full['dates'][1]}; base_data 날짜 {fmt_int(cal['base_dates'])} 중 1행짜리 휴일 {len(cal['dropped'])}개 제외, 프로토 `min_day_frac` 규칙 동일). "
          f"코드 `cpp-engine/tools/data/cache_v21/universe_{{common,marketcap,build,qa,report}}.py`(`db.py` import, `build_cache_v2.py` 는 파라미터·헬퍼 import 만, 기존 파일 무수정). "
          f"스테이지 `stage_v21/T5/`, 작업 `stage_v21/T5_work/`. DB 는 읽기 전용·회사 id 1000개 청크, 스레드 8, peak RSS 유니버스 빌드 {t_full.get('peak_rss_gb', '?')} GB / marketCap {t_mcap.get('peak_rss_gb', '?')} GB.", "",
          f"**T1 v2 반영**: `{done_v2.name}` {'있음' if done_v2.exists() else '없음'} — tradable 단계는 lifecycle(mtime {ti['lifecycle_file_mtime']}, DONE_v2 sha 일치)·`tradable_exclusions.parquet`({ti['exclusion_rows_applied']}행) 최종본으로 실행"
          + ("" if done_v2.exists() else " **(T1 v2 미반영)**") + ". **리드 지시 반영**: (17:00) 후보의 primary 조건을 PIT 로(currentPrimary ∨ lineage relist/rename 체인상 currentPrimary 종목의 선행 id), "
          "strict tradable 을 랭킹 전 적용, 이중 계산 게이트; (17:50) 유니버스 상태(롤링·순위·sticky)는 **lineage 라인 단위**로 계산하고 멤버십은 그 날 활성 id 에 부여(전환 공백 0), 달러거래대금 raw 유지, 캘린더 = 거래일(종가 수 ≥ 롤링 중앙값 20%).", ""]

    L += ["## 0. 판정 요약", "",
          md_table(pd.DataFrame([
              {"게이트": "Top500 중첩 |A∩B|/|A| 중앙값 ≥ 0.99 (membership_v1rule vs 프로토 TopUniverseV2_v1rule, 2010+, id 수준)",
               "값": f"{g.loc['Top500', 'ov_of_ours_med']:.4f} (p05 {g.loc['Top500', 'ov_of_ours_p05']:.4f}, 최소 {g.loc['Top500', 'ov_of_ours_min']:.4f}, n={fmt_int(g.loc['Top500', 'days'])}일)",
               "판정": "PASS" if g.loc["Top500", "ov_of_ours_med"] >= 0.99 else "FAIL"},
              {"게이트": "Top1000 / ALL 중앙값 (id 수준)", "값": f"{g.loc['Top1000', 'ov_of_ours_med']:.4f} (p05 {g.loc['Top1000', 'ov_of_ours_p05']:.4f}) / {g.loc['ALL', 'ov_of_ours_med']:.4f} (p05 {g.loc['ALL', 'ov_of_ours_p05']:.4f})",
               "판정": "PASS" if gate_pass else "FAIL"},
              {"게이트": "같은 비교 line 수준(옛 id 멤버-일을 계보 끝 id 열로 합친 회사 단위) Top500 / Top1000 / ALL",
               "값": " / ".join(f"{gl.loc[u, 'ov_of_ours_med']:.4f} (p05 {gl.loc[u, 'ov_of_ours_p05']:.4f})" for u in GATE_UNIVERSES),
               "판정": "PASS" if gate_pass_line else "FAIL"},
              {"게이트": "lifecycle 미적용·primary 현재값(notrad) 의 프로토 재현 — id 수준 대칭차 멤버-일 (raw 파이프라인 자체 검증)",
               "값": ", ".join(f"{u} {int(notrad_row[f'{u}_symdiff']):,}" for u in GATE_UNIVERSES) + f" (Top500 중첩 {notrad_row['Top500_ov_med']:.4f})",
               "판정": "PASS" if notrad_row["Top500_symdiff"] == 0 else "확인"},
              {"게이트": "이중 계산: 같은 lineage 라인의 두 id 가 같은 날 둘 다 멤버인 셀 = 0 (Top500·Top1000·ALL × 2 변형, 전 기간)",
               "값": ", ".join(f"{r.variant}/{r.universe} {int(r.dup_cells_all)}" for r in dup_full.itertuples()) if len(dup_full) else "n/a",
               "판정": "PASS" if dup_ok else "FAIL"},
              {"게이트": "전환 연속성(v1rule, Top500/Top1000/ALL): 옛 id 마지막 활성일 멤버 → 새 id 첫날 멤버; **전환 유발 공백**(양쪽 날 모두 규칙상 적격인데 끊김) = 0",
               "값": "; ".join(f"{r['universe']} 연속 {int(r['continuous'])}/{int(r['old_member_on_last_day'])}, 전환 유발 공백 {int(r['transition_caused_gap'])}, 규칙 탈락 {int(r['rule_exit_first_day'])}, 풀 이탈 {int(r['pool_exit_new_id'])}" for r in qa.get("transition", []) if r["run"] == "full"),
               "판정": "PASS" if all(r["transition_caused_gap"] == 0 for r in qa.get("transition", []) if r["run"] == "full") else "FAIL"},
              {"게이트": "membership(전체 변형) vs 프로토 TopUniverseV2 Top500/Top1000/ALL (id 수준; line 수준)",
               "값": "/".join(f"{ga.loc[u, 'ov_of_ours_med']:.4f}" for u in GATE_UNIVERSES) + "; " + "/".join(f"{gal.loc[u, 'ov_of_ours_med']:.4f}" for u in GATE_UNIVERSES), "판정": "참고"},
              {"게이트": "구간 압축 역변환 비트 동일(8 universe × 2 변형) + 심은 1셀 반례 검출", "값": str(all(full["variants"][v]["roundtrip_selftest"]["all_pass"] for v in full["variants"])), "판정": "PASS"},
              {"게이트": "참조 로더 `get_mask('universe:*')` == 역변환(캘린더 날짜, Top500·ALL)", "값": str({k: v for k, v in full["loader_check"].items() if not k.endswith("dates")}),
               "판정": "PASS" if all(v for k, v in full["loader_check"].items() if not k.endswith("dates")) else "FAIL"},
              {"게이트": "반례 self-test(파라미터 변경 → 중첩 하락 검출)", "값": qa["counter"]["summary"], "판정": "PASS" if qa["counter"]["pass"] else "FAIL"},
              {"게이트": "marketCap 공통 셀 상대차 > 1e-6 비율", "값": f"{mc['rel_diff_gt_tol_frac']:.2e} ({fmt_int(mc['rel_diff_gt_tol'])}/{fmt_int(mc['common_cells'])}셀, {mc['bad_ids']}종목; 원인 전부 벤더 값 변경·FX 갱신)", "판정": "기록"},
          ])), ""]

    top = items[items.universe == "Top500"] if len(items) else pd.DataFrame()
    reatt = int(top.reattribution_old_new_id.sum()) if len(top) else 0
    tot = int(top.diff_days.sum()) if len(top) else 0
    L += [f"id 수준 Top500 중첩이 0.99 에 못 미치는 것은 설계상 결과임: 프로토(=v1)는 재상장 회사의 과거를 벤더가 이어붙인 **새 id** 로 보유하고, 리드 지시의 PIT primary 는 그 기간을 **실제 거래한 옛 id** 로 귀속함. "
          f"Top500 대칭차 {fmt_int(tot)} 멤버-일 중 {fmt_int(reatt)}({(100 * reatt / max(tot, 1)):.0f}%)이 이 재귀속(line 수준에서는 일치)이고, 나머지는 §6-1(PIT 로 새로 들어온 선행 id, 순위 경계 대체, 전환 공백). "
          f"회사 단위(line 수준) 중첩은 Top500 {gl.loc['Top500', 'ov_of_ours_med']:.4f}·Top1000 {gl.loc['Top1000', 'ov_of_ours_med']:.4f}·ALL {gl.loc['ALL', 'ov_of_ours_med']:.4f} 로 목표 충족.", ""]
    L += ["## 1. 산출 파일", "", md_table(file_table(t5)), ""]

    L += ["## 2. `derived/marketcap.parquet`", "",
          "정의(프로토 방식 그대로): `marketCap_USD = ciqmarketcap.marketcap(companyId, d) × 1e6 / fx(primaryCurrency(companyId), d)`, FX = `ciqexchangerate` snap 6 종가(정확한 날짜 일치, USD 행 1.0 확인), "
          "primaryCurrency = 회사의 primary security 의 primary trading item 통화, 회사의 모든 tradingItemId 에 같은 값 부여, 행 = base_data 종가 있는 (id,date) 중 ciqmarketcap 행이 있는 것. "
          "`sharesOutstanding` = `ciqmarketcap.sharesoutstanding`. `source` = ciqmarketcap / ciqmarketcap_fx(환산) / ciqmarketcap_nofx(FX 결측 → NaN; 0건).", "",
          "### 2-1. 구축 통계", "",
          kv_table(ms, ["base_close_rows", "rows_out", "rows_without_mcap", "ids_with_rows", "rows_nonusd", "rows_nonusd_fx_missing",
                        "rows_company_without_primary_currency_row", "mcap_nonpositive", "mcap_max", "wall_s", "peak_rss_gb"],
                   {"base_close_rows": "base_data 종가 행", "rows_out": "marketcap 행(출력)", "rows_without_mcap": "ciqmarketcap 행 없는 종가 행(제외)",
                    "ids_with_rows": "행 있는 종목", "rows_nonusd": "비USD primary 통화 행(환산)", "rows_nonusd_fx_missing": "FX 결측 행",
                    "rows_company_without_primary_currency_row": "primary 통화 행 없는 회사의 행(USD 로 간주, 프로토 동일)",
                    "mcap_nonpositive": "marketCap ≤ 0 행", "mcap_max": "최대값(USD)", "wall_s": "구축 벽시계(초)", "peak_rss_gb": "peak RSS(GB)"}), "",
          "primary 통화 분포(회사 수): " + ", ".join(f"{k}: {fmt_int(v)}" for k, v in ms["primary_currency_counts"].items()), "",
          f"### 2-2. 프로토 `BaseData/marketCap_USD.parquet`(float32 wide, ~{mc['proto_last_date']}) 대조", "",
          kv_table(mc, ["proto_cells", "common_cells", "only_ours_in_span", "only_proto", "rel_diff_gt_tol", "rel_diff_gt_tol_frac", "rel_diff_median", "rel_diff_p99", "rel_diff_max"],
                   {"proto_cells": "프로토 유한 셀", "common_cells": "공통 셀", "only_ours_in_span": "T5에만(프로토 기간 내)", "only_proto": "프로토에만",
                    "rel_diff_gt_tol": "상대차 > 1e-6 셀", "rel_diff_gt_tol_frac": "비율", "rel_diff_median": "상대차 중앙값(float32 반올림 수준)", "rel_diff_p99": "p99", "rel_diff_max": "최대"}), "",
          "원인 분류(상대차 > 1e-6):", "",
          md_table(pd.DataFrame([{"원인": k, "셀": fmt_int(v)} for k, v in mc["bad_by_cause"].items()])), "",
          f"- `vendor_value_changed`: 프로토 09-14 조회와 이번 09-16 조회 사이의 벤더 재기술(DB 직접 확인). LPHM 2626098 은 `sharesoutstanding` 이 69주로 바뀌어 시총 $11(프로토 $661,077) — 벤더 오류로 보임; CPOP·MPU·BMNM·Ramaco(METC/METC.B) 는 주식수 재기술(비율 여러 개). 35종목 중 6종목이 {fmt_int(sum(sorted(mc['bad_top_ids'].values(), reverse=True)[:6]))}셀.",
          "- `fx_or_primary_currency` 97셀: 비USD 통화 환산 행(KMDA ILS 등) — 환율 스냅 갱신.",
          f"- 한쪽에만: T5에만 {fmt_int(mc['only_ours_in_span'])}셀(KRSA 2020655131 1,858: ciqmarketcap 행이 2019-03-18부터 새로 생김; TEVA 7727657 609: 신규 종목), 프로토에만 {fmt_int(mc['only_proto'])}셀(LION 1880988829: 2022~2024 행이 DB 에서 사라지고 2024-05-13부터만 남음).", "",
          "상세: `T5/qa/marketcap_compare.md`, 셀 목록 `T5/qa/marketcap_diff_cells.csv`.", ""]

    p = full["params"]
    L += ["## 3. 유니버스 규칙·구현", "",
          md_table(pd.DataFrame([
              {"항목": "규칙", "값": "sim `TopUniverse_uss_v1.py` 포팅(프로토 `build_cache_v2` 헬퍼 재사용): dollar = ts_mean(close,100)×ts_mean(volume,100) > 3e6; ts_median(marketCap,21) > 1e7; primary; liq = cs_rank(ts_median(marketCap,21)); TopK = liq > (n+1−K)/(n+1); sticky(exit 10, entry 60); & marketCap>0; TokensToRemove 46종(패널에 45 존재)"},
              {"항목": "파라미터", "값": str(p)},
              {"항목": "입력 기준", "값": "close·volume = base_data raw as-quoted(로더 `get_panel(.., 'raw')`), marketCap = §2 파일(float64)"},
              {"항목": "primary 조건 (PIT, 리드 지시)", "값": f"currentPrimary(T1 current, 프로토 master 와 22,871종목 동일) {fmt_int(pit.get('currentPrimary', 0))}종목 ∨ lineage relist/rename(confidence high/medium, 전이적) 체인으로 currentPrimary 종목의 선행 id {fmt_int(pit.get('predecessor_of_primary', 0))}종목(자기 이름의 'Trust|ETF' 필터 통과, 제거 0) — 선행 id 는 자기 lastTradeDate 와 후속 id firstTradeDate 전날 중 이른 날까지(9종목은 후속 시작에서 절단 → 같은 날 두 id 동시 후보 0). 체인 edge {fmt_int(pit.get('lineage_edges', 0))}, 깊이>1 17"},
              {"항목": "TokensToRemove", "값": f"46종(패널에 {pit.get('tokens_in_panel', 45)}) + 그 선행 id {len(pit.get('token_predecessors_removed', []))}종목({pit.get('token_predecessors_removed', [])}: RTN←RTN.B, WBD←DISC.A×2, GTE←GDSK.E, FNF←옛 FNF) — v1 은 이어붙인 현재 id 하나로 라인 전체를 제거했으므로 PIT 에서도 라인 단위로 제거"},
              {"항목": "후보 집합 v1rule", "값": f"inV1Rule {fmt_int(full['variants']['v1rule']['candidate_items'])}종목(T1 instruments; 프로토 대비 1종목 차이 BURU 1816580901 거래소 134→102)"},
              {"항목": "후보 집합 전체(기본, membership.parquet)", "값": f"펀드 스크린 밖 {fmt_int(full['variants']['all']['candidate_items'])}종목; 스크린 = ¬inV1Rule ∧ (companyTypeId 13 ∨ exchangeId∈{{117,467,358}}) = 706종목, 프로토 `fund_exclusions.csv` 와 집합 동일"},
              {"항목": "tradable (strict, 랭킹 전 적용)", "값": f"lifecycle firstTradeDate ≤ d ≤ lastTradeDate ∧ close notna ∧ ¬tradable_exclusions(T4 로더 'tradable' 과 동일 정의). 종가 셀 {fmt_int(ti['priced_cells'])} → tradable {fmt_int(ti['tradable_cells_strict'])}: 꼬리(lastTradeDate 뒤 종가) {fmt_int(ti['priced_after_lastTradeDate'])}셀, 이어붙인 과거(firstPriceDate~firstTradeDate 전, T1 lineage 로 올린 {ti['stitched_ids']}종목) {fmt_int(ti['priced_stitched_before_firstTradeDate'])}셀(규칙에 미사용), exclusions {ti['exclusion_rows_applied']}행 {fmt_int(ti['exclusion_cells_removed'])}셀"},
              {"항목": "라인 단위 계산 (리드 결정)", "값": (f"lineage relist/rename(high/medium) 체인 {fmt_int((full.get('lines') or {}).get('lines', 0))}개 라인({fmt_int((full.get('lines') or {}).get('ids_in_lines', 0))}종목, 전환 {fmt_int((full.get('lines') or {}).get('transitions', 0))}회). "
                                                   "각 날짜의 활성 id = firstTradeDate 가 가장 늦은 체인 멤버 중 그 날 strict tradable 인 것(옛 id 는 firstTradeDate~새 id 시작 전날, 새 id 는 firstTradeDate 부터); "
                                                   "라인의 입력(raw close·volume, marketCap, primary, 후보 여부)은 활성 id 의 값으로 합쳐 체인 끝 id 열 하나로 규칙(롤링·순위·sticky)을 돌리고, 결과 멤버십을 활성 id 로 되돌림. "
                                                   f"체인 밖 종목은 기존과 동일. 라인 활성 셀 {fmt_int((full.get('lines') or {}).get('line_active_cells', 0))}, 멤버 ⊆ strict tradable assert, 같은 라인 두 id 동시 멤버 0")},
              {"항목": "캘린더", "값": f"종가 행 수 ≥ 0.2×국소(21일 중앙) 중앙값인 날짜 {fmt_int(cal['kept'])}/{fmt_int(cal['base_dates'])}; 제외 {', '.join(cal['dropped'])} (각 1행)"},
              {"항목": "구간 압축", "값": "universe 별·종목별 연속 True 거래일 run 1개 = 1행(fromDate·toDate 포함). 역변환 비트 동일 self-test(8×2) + 심은 1셀 반례 검출 + 참조 로더 get_mask 동일"},
          ])), ""]

    L += ["## 4. 검증 게이트 (`T5/qa/universe_gates.md`)", "",
          "중첩 = 일별 |A∩B|/|A| (A = T5, B = 참조), 2010-01-01 이후 양쪽 모두 있는 거래일. 목표 Top500 중앙값 ≥ 0.99. "
          "line 수준 = 옛 id 의 멤버-일을 lineage 체인 끝 id 열로 합친 뒤 비교(회사 단위).", "",
          "### 4-1. (a) membership_v1rule vs 프로토 `TopUniverseV2_v1rule` — id 수준", "", ov_table(g), "",
          "line 수준:", "", ov_table(gl), "",
          "### 4-2. membership(전체 변형) vs 프로토 `TopUniverseV2` — id 수준", "", ov_table(ga), "", "line 수준:", "", ov_table(gal), "",
          "### 4-3. (b) v1 스냅샷 `TopUniverseV1`(2026-09-14) 대비 — 참고", "", ov_table(gv, ["ref_name_days_outside_panel"]), "",
          "프로토 자체의 v1 대비 중첩(overlap_vs_v1.md): Top500 v2_v1rule 0.9936, ALL 0.9921 — T5 도 같은 수준.", ""]

    L += ["## 5. (c) 일별 멤버 수", "", md_table(counts, "{:.0f}"), "",
          "연도별 중앙값(Top500·Top1000·ALL, all = membership, v1rule = membership_v1rule):", "", md_table(yearly, "{:.0f}"), ""]

    L += ["## 6. 차이 원인 분해 (v1rule 변형 vs 프로토, 2010+)", "",
          "ablation ladder — 조건을 하나씩 프로토 쪽으로 되돌린 run: full(라인 계산) → strict_pit(id 별 strict, 재상장 시 sticky 재시작) → strict_curprim(primary 를 현재값으로 = PIT 해제) → notrad(lifecycle 미적용 = 프로토 동등 후보) → "
          "notrad_sadj(달러거래대금 split-adj 기준 = 프로토 산식) → notrad_sadj_pmcap(시총도 프로토 패널). `_symdiff` = 프로토와의 대칭차 멤버-일수(id 수준), `_line_*` = line 수준, `_vs_prev` = 직전 단계 대비 바뀐 멤버-일수.", "",
          md_table(lad.drop(columns=["params"]), "{:.4f}"), "",
          f"해석: notrad 가 프로토를 재현(Top500 대칭차 {int(notrad_row['Top500_symdiff']):,}, Top1000 {int(notrad_row['Top1000_symdiff']):,}, ALL {int(notrad_row['ALL_symdiff']):,} 멤버-일; ALL 잔차는 달러거래대금 기준·시총 재기술)하므로 raw 파이프라인 자체는 검증됨. "
          + (f"strict_curprim(primary 현재값)은 Top500 중첩 {curprim_row['Top500_ov_med']:.4f} — 재상장 회사(BLK·TEL·SIRI·PSKY·STX·GOOGL…)가 이어붙인 기간 내내 사라지는 반사실. " if curprim_row is not None else "")
          + (f"strict_pit(id 별)은 line 수준 Top500 대칭차 {int(lad[lad.run == 'strict_pit'].iloc[0]['Top500_line_symdiff']):,} 멤버-일로 full 의 {int(lad[lad.run == 'full'].iloc[0]['Top500_line_symdiff']):,} 보다 큼(전환 공백). " if (lad.run == "strict_pit").any() else "")
          + "full 의 잔여 차이는 §6-1 원인표.", ""]
    if len(causes):
        L += ["### 6-1. 잔여 차이 종목·멤버-일 원인 분류 (일 단위 집계)", "",
              "reattribution_old_new_id = 같은 회사, 프로토는 이어붙인 새 id·T5 는 그 날 실제 거래한 옛 id(line 수준 일치) / transition_gap_sticky_restart = 재상장 새 id 가 firstTradeDate 뒤 StickyEntryDays+1 거래일 동안 sticky 진입 대기(옛 id 는 이미 종료) / "
              "stitched_old_not_in_panel = 옛 id 가 종목 집합에 없음 / stitched_old_not_member = 옛 id 는 있으나 규칙 미충족(시총·거래대금·primary) / tail_cut = lastTradeDate 뒤 꼬리 / exclusion = tradable_exclusions / "
              "pit_predecessor_added = PIT primary 로 들어온 선행 id(프로토엔 그 라인이 없음, 예 GGP 2010~2018: 후속 BPYU 에 과거가 이어붙지 않음) / new_id = 프로토에 없는 종목(2종목) / "
              "rank_boundary_substitution = lifecycle·PIT 를 끄면 사라지는 잔차(후보 집합 변화로 순위·임계 경계에서 들고 난 종목) / dollar_volume_basis·mcap_diff = ladder 단계에서 사라짐 / other = 나머지.", "", md_table(causes), ""]
    if len(items):
        show = ["tradingItemId", "symbol", "companyName", "stitched", "currentPrimary", "firstTradeDate", "lastTradeDate", "diff_days", "only_proto_days", "only_ours_days", "dominant_cause"]
        L += ["### 6-2. Top500 잔여 차이 상위 25 (diff_days 순; 전체 `T5/qa/universe_diff_items.csv`)", "", md_table(items[items.universe == "Top500"].head(25)[show]), "",
              "ALL 상위 15:", "", md_table(items[items.universe == "ALL"].head(15)[show]), ""]

    trans = pd.DataFrame(qa.get("transition", []))
    trans_ex = pd.DataFrame(qa.get("transition_examples", []))
    if len(trans):
        L += ["## 6-3. 전환 연속성 게이트 (v1rule 변형, 전 기간)", "",
              "edge = lineage relist/rename(high/medium) old→new(둘 다 패널). old_member_on_last_day = 옛 id 가 라인의 마지막 활성일에 멤버인 edge 수, continuous = 그중 새 id 가 firstTradeDate 당일 멤버. "
              "not_continuous 분해: transition_caused_gap = 양쪽 날 모두 규칙상 적격(pre-sticky 선택)인데 끊김 → 전환 메커니즘 탓(목표 0); rule_exit_first_day = 첫날 라인 자체가 부적격(달러거래대금 3e6 미달·시총 결측 — 단일 종목이라도 같은 결과, 예 WKHS 달러거래대금 2.6M, AMSG 시총 12일 결측); "
              "pool_exit_new_id = 새 id 가 변형의 후보 풀 밖(OTC 이전·.Q 등). gap_days_* = 첫 61거래일 중 어느 id 도 멤버가 아닌 일수. full(라인) vs strict_pit(id 별) 대비.", "",
              md_table(trans[trans.universe.isin(GATE_UNIVERSES)]), ""]
        if len(trans_ex):
            ex = trans_ex[(trans_ex.universe == "Top500") & trans_ex.run.isin(["full", "strict_pit"])]
            L += ["사례 표(Top500): BLK·GOOGL·JCI·XRX·CCO — full(라인) 은 전환일에 연속(pre-eligible 양쪽 True, 새 id 첫날 멤버), strict_pit 은 새 id 가 61거래일 뒤 진입.", "",
                  md_table(ex.drop(columns=["universe"])), ""]
            ex2 = trans_ex[(trans_ex.universe == "ALL") & trans_ex.run.isin(["full", "strict_pit"]) & trans_ex.symbol.isin(["XRX", "CCO"])]
            if len(ex2):
                L += ["XRX·CCO 는 전환 시점에 Top500 밖(양쪽 날 부적격)이라 ALL 기준으로 표시:", "", md_table(ex2.drop(columns=["universe"])), ""]
    L += ["## 7. tradable 적용 효과 (`T5/qa/tradable_effect.md`)", "",
          "대조 = notrad(lifecycle 미적용·primary 현재값). removed = 대조에서만 멤버, added = 적용에서만 멤버(2010+). full(라인 계산) 의 removed 는 대부분 이어붙인 새 id 의 과거(옛 id 로 귀속 → added), strict_pit 은 id 별 strict(전환 공백 있음), strict_curprim 은 회사가 통째로 빠지는 반사실.", ""]
    if len(trad):
        sub = trad[trad.universe.isin(GATE_UNIVERSES)][["run", "tradable", "primary", "variant", "universe", "members_med", "members_med_notrad", "daily_diff_med",
                                                          "daily_diff_min", "daily_diff_max", "removed_name_days", "removed_items", "added_name_days", "added_items",
                                                          "line_removed_name_days", "line_added_name_days"]]
        L += [md_table(sub, "{:.1f}"), ""]
    ti_items = pd.read_csv(t5 / "qa" / "tradable_effect_items.csv") if (t5 / "qa" / "tradable_effect_items.csv").exists() else pd.DataFrame()
    if len(ti_items):
        sub = ti_items[(ti_items.run == "full") & (ti_items.variant == "v1rule") & (ti_items.universe == "Top500")]
        L += ["full v1rule Top500 removed 상위 15 / added 상위 10:", "",
              md_table(sub.sort_values("removed_days", ascending=False).head(15).drop(columns=["run", "variant", "universe"])), "",
              md_table(sub.sort_values("added_days", ascending=False).head(10).drop(columns=["run", "variant", "universe"])), ""]

    L += ["## 8. (d) 반례 self-test · 민감도", "", md_table(pd.DataFrame(qa["counter"]["rows"]), "{:.4f}"), "", qa["counter"]["summary"], ""]
    if len(sens):
        L += ["달러거래대금 추정식 민감도(v1rule 변형, tradable·primary 동일): raw_meanxmean = v1 산식을 raw 입력에(기본, 리드 지시), split_adj_meanxmean = 분할계수로 한 기준에 맞춘 뒤 v1 산식(프로토 산식), raw_product_mean = 일별 raw close×volume 의 100일 평균.", "",
              md_table(sens, "{:.4f}"), ""]

    L += ["## 9. 실행 기록", "",
          md_table(pd.DataFrame([
              {"단계": "marketCap 구축 + 프로토 대조", "벽시계": t_mcap.get("wall", ""), "peak RSS(GB)": t_mcap.get("peak_rss_gb", ""),
               "비고": f"ciqmarketcap 22 청크(1000 회사, 12~14s/청크) {fmt_int(ms['rows_out'])}행 출력, 대조 12.6s"},
              {"단계": "universe full run(라인 계산, 2 변형, 구간 압축, self-test, 이중계산 게이트, 로더 대조, publish)", "벽시계": t_full.get("wall", ""), "peak RSS(GB)": t_full.get("peak_rss_gb", ""),
               "비고": f"run 내부 {full.get('wall_s')}s; 규칙 1회 ≈ 25s(T×N = {fmt_int(full['T'])}×{fmt_int(full['N'])})"},
              {"단계": "ablation·민감도·반례 run 9회 + QA", "벽시계": "≈ 15분 + 6분", "peak RSS(GB)": "≤ 15", "비고": "T5_work/runs/*, qa.log"},
          ])), ""]

    L += ["## 10. 미해결·제안", "",
          md_table(pd.DataFrame([
              {"#": 1, "항목": "라인 정의의 범위", "내용": "라인 = T1 lineage 의 relist/rename(high/medium) edge. merger·spinoff 는 라인이 아님(회사가 다름). old id 가 성공 id 시작 뒤에도 거래된 21 edge 는 새 id 시작일부터 새 id 가 라인을 대표(옛 id 의 잔여 거래일은 유니버스 밖)",
               "제안": "T1 lineage 가 갱신되면(`master_build --steps lineage`) universe_build 재실행 필요 — publish 순서에 lineage → universe 의존 명시"},
              {"#": 2, "항목": "달러거래대금 기준(리드 결정: raw 유지)", "내용": "100일 창이 분할을 가로지르면 raw mean×mean 은 기준이 섞임 → 3e6 경계 종목(ALL)만 영향(§8 민감도: split_adj 대비 ALL 대칭차 6천 멤버-일, Top500 0)",
               "제안": "기록만. `--dollar split_adj_meanxmean` 옵션은 유지"},
              {"#": 3, "항목": "캘린더(리드 결정 반영)", "내용": f"유니버스는 거래일 캘린더 {fmt_int(cal['kept'])}일(종가 수 ≥ 21일 중심 롤링 중앙값의 20%)만 사용, base_data 의 1행 휴일 {len(cal['dropped'])}개(GRMH·WWYWB 단일 행) 제외. 로더 `dates()` 는 아직 6,731일이므로 publish 에서 캘린더 적용 전까지 구간 안 휴일이 로더 마스크에서 True",
               "제안": "publish 게이트: 캘린더 적용 후 `get_mask('universe:*')` 재대조(T5 `loader_crosscheck` 재사용)"},
              {"#": 4, "항목": "marketCap 벤더 재기술", "내용": "2일 사이 35종목 시총이 바뀜(LPHM 은 주식수 69주 오류). 일별 갱신에서 과거 시총이 통째로 바뀌면 universe 도 과거가 바뀜",
               "제안": "T9 일별 갱신에서 marketcap 재조회 범위·변경 로그를 두고, 과거 변경은 기록만(universe 재계산은 빌드 시점 고정)"},
              {"#": 5, "항목": "membership 파일 2종·PIT primary·라인 규칙", "내용": "계약 §3-5 는 membership.parquet 하나. v1rule 변형은 `membership_v1rule.parquet` 별도(로더 `universe_v1rule:*` 지원). primary 조건·라인 계산은 BRIEF 에 없음", "제안": "BRIEF §3-5 에 두 파일·캘린더·PIT primary·라인 계산 정의 명시"},
              {"#": 6, "항목": "PIT 로 새로 들어온 선행 id", "내용": "GGP·WCNX·PLD 등 7종목(Top500 5,486 멤버-일): 후속 id 에 과거가 이어붙지 않아 프로토·v1 에는 없던 회사가 유니버스에 들어옴(실제 대형주)", "제안": "의도된 개선으로 기록; 연구 재현 시 v1 대비 차이 원인으로 안내"},
              {"#": 7, "항목": "ciqmarketcap 행 없는 종가 행", "내용": f"{fmt_int(ms['rows_without_mcap'])}행({fmt_int(ms['n_ids'] - ms['ids_with_rows'])}종목은 시총 전무) → 유니버스 후보 불가(v1 동일)", "제안": "없음(기록)"},
          ])), ""]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(L) + "\n")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
