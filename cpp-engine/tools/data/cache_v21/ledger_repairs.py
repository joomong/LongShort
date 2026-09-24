#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/ledger_repairs.py - R1: 원장 교정 규칙(알려진 벤더 오류·GS 관측 기반 수동 교정). ledger_build 가 호출한다.

모든 교정은 events/ca_ledger_repairs.csv(tradingItemId, exDate, field, before, after, evidence, source) 에 남기고,
교정된 원장 행은 repairOf 에 대체한 벤더 인공물 식별자를 적는다(BRIEF §3-1). 근거가 없는 값은 추정하지 않고
status=unresolved 로 남긴다(P12: "자료 없음" 과 "대가 0" 구분).

규칙
  gross_first            : 세전 원칙(P1). netAmount == divAmount 이고 grossAmount > divAmount(비율 ≤ 2) 이면 벤더 divAmount 는
                           세후값(CRH 아일랜드 25%, ICL, CMBT, UBS 35%, AU 20%, BWMX 10%, STLA…) → grossAmount 사용.
                           supplementalType 'Net amount'(HOLDRs 수탁수수료 차감액) 은 보유자 실수령이 net 이므로 유지하고 notes 만.
                           grossAmount 가 divAmount 의 2 배 초과(FBC 10×, GF 113×, UYG 70×…) 는 벤더 오류로 보고 divAmount 유지.
  apply_overrides        : GS TRS 관측(BRIEF §2)·사이블링 라인·공개 조건으로 확인된 사건: HON 2026-06-29 HONA 0.5, FNF 2025-12-17 FG 0.06,
                           BDX 2026-02-10 WAT 0.135, CTRA→DVN 0.7 2026-05-07, EXAS/EA 정산일 +1, TYC 314828302 2007-07-02 TEL leg
                           (벤더 child 2659913 = Tyco Toys 오매칭 → TEL 옛 라인 34969541), GME 2025-10-03 워런트 조건 보존(P11).
  divfactor_scale_repairs: 배당계수 스케일 오류 55 종목(qa/T7/J_gdiv_systematic_scale_ids.csv): 벤더 계수 스텝 implied 배당
                           (1 − f_before/f_after) × P_prev 와 실제 ciqdividend 사건을 대조해 스텝마다 교정 로그(원장 현금 행은 항상
                           실제 사건값; 계수 역산 금지 P12).
  split_artifact_check   : 분할 인공물 97 건(qa/T2/factor_artifacts.csv): ciqsplit·ciqsplitcache 의 실제 분할비·효력일과 raw 종가
                           점프(±60 일) 를 대조해 효력일 교정 제안(split.validFrom) 또는 가격 대응 없음(split.factor) 을 로그.
                           원장 split 행은 factors/split 경계를 그대로 재현(엔진 split_adj 패널과 일치)하되 status/repairOf 로 표시.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("cache_v21.ledger_repairs")

GS_EVIDENCE = "GS TRS observation 2025-06-27~2026-09-15 (rc2 BRIEF §2; GS DB off-limits, lead-extracted ../gs/ca.parquet)"
AFTER_TAX_MAX_RATIO = 2.0
SCALE_TOL = 0.05            # implied/actual 비율이 1 에서 이만큼 벗어나면 스케일 오류 스텝
JUMP_MATCH_TOL = 0.20       # 인공물 효력일 탐색: raw close 일간비 × parentFactor 가 1 ± 이 값 안이면 "그 날 분할 반영"
JUMP_SEARCH_DAYS = 60

# 수동 교정(사건별 근거). match: 기존 행을 찾는 키, set: 바꿀 값, add: 없으면 새 행 추가.
MANUAL_OVERRIDES = [
    {"name": "HON 2026-06-29 spin HONA 0.5/old share (with 1:2 reverse split, GS -204 -> -102 HON + -102 HONA)",
     "tradingItemId": 2618950, "exDate": "2026-06-29", "legType": "stock", "frame": "stock",
     "set": {"childTradingItemId": 2005551375, "childRatio": 0.5, "eventType": "spin_off"},
     "evidence": GS_EVIDENCE + "; HONA 2005551375 (Honeywell Aerospace) first trade 2026-06-15; ciqdividendcache 110.505 vs HONA close basis 110.095"},
    {"name": "FNF 2025-12-17 distribution of F&G 0.06/share (GS FG 0.6 on 10 FNF; ciqdividendcache Multiple 2.5888 = cash 0.52 + 0.06 x FG)",
     "tradingItemId": 266673936, "exDate": "2025-12-17", "legType": "stock", "frame": "stock",
     "set": {"childTradingItemId": 1814968902, "childRatio": 0.06, "eventType": "spin_off"},
     "evidence": GS_EVIDENCE + "; FG 1814968902 active line"},
    {"name": "BDX 2026-02-10 Biosciences/Diagnostics RMT with Waters: 0.135 WAT per BDX share (GS short -0.135 WAT)",
     "tradingItemId": 2593888, "exDate": "2026-02-10", "legType": "stock", "frame": "stock",
     "set": {"childTradingItemId": 2663488, "childRatio": 0.135, "eventType": "spin_off"},
     "evidence": GS_EVIDENCE + "; ciqdividendcache 44.41148 = 0.135 x WAT"},
    {"name": "TYC 314828302 2007-07-02 leg 1: TE Connectivity 0.25/share (vendor matched 2659913 = Tyco Toys, ended 1997; sibling line 34969540 has TEL)",
     "tradingItemId": 314828302, "exDate": "2007-07-02", "legType": "stock", "frame": "stock", "match_child": 2659913,
     "set": {"childTradingItemId": 34969541, "childRatio": 0.25, "eventType": "spin_off"},
     "evidence": "qa/T7/returns_known_issues.csv (sibling 34969540 leg TEL web:sec.gov a2178368zex-99_1.htm); TEL old line 34969541 (rename -> 1876975607 2024-09-30)"},
    {"name": "TYC 314828302 2012-10-01 ADT+Pentair: vendor cache covers only part (10.584 vs sibling 28.68); ratios kept, valued by child closes",
     "tradingItemId": 314828302, "exDate": "2012-10-01", "legType": "stock", "frame": "stock", "match_child": 217292918,
     "set": {"repairOf": "divfactor_partial:314828302:2012-10-01"},
     "evidence": "qa/T7/returns_known_issues.csv; ciqdividendcache 314828302 10.584 vs 34969540 28.6824"},
    {"name": "TYC 314828302 2012-10-01 PNR leg (same partial-vendor note)",
     "tradingItemId": 314828302, "exDate": "2012-10-01", "legType": "stock", "frame": "stock", "match_child": 2638398,
     "set": {"repairOf": "divfactor_partial:314828302:2012-10-01"},
     "evidence": "qa/T7/returns_known_issues.csv; ciqdividendcache 314828302 10.584 vs 34969540 28.6824"},
    {"name": "TYC 314828302 2007-07-02 COV leg (vendor cache 10.775 covers one leg; sibling 20.54)",
     "tradingItemId": 314828302, "exDate": "2007-07-02", "legType": "stock", "frame": "stock", "match_child": 34969538,
     "set": {"repairOf": "divfactor_partial:314828302:2007-07-02"},
     "evidence": "qa/T7/returns_known_issues.csv; ciqdividendcache 314828302 10.775 vs 34969540 20.54"},
    {"name": "GME 2025-10-03 warrant dividend: 1 warrant (GME.WS 1962567035) per 10 shares, exercise price $32.00, expiry 2026-10-30 (terms preserved, P11)",
     "tradingItemId": 24268335, "exDate": "2025-10-03", "legType": "stock", "frame": "stock",
     "set": {"eventType": "warrant", "legType": "rights", "childTradingItemId": 1962567035, "childRatio": 0.1,
             "subscriptionPrice": 32.0, "expiryDate": "2026-10-30", "exerciseRatio": 1.0},
     "evidence": "GameStop press release 2025-09-30 (warrant dividend terms; not in xfeed); ciqdividend type 20 row; GS observed as Spin, undelivered; "
                 "GME.WS 1962567035 = ciqtradingitem Equity Warrant NYSE from 2025-10-08 (no ciqpriceequity rows)"},
    {"name": "LCAP.A 2008-03-04 reclassification: 4 Liberty Entertainment (LMDI.A 41863782, listed 2008-03-03) per Liberty Capital share (rc1 web override ratio 4.0, child unresolved)",
     "tradingItemId": 27059655, "exDate": "2008-03-04", "legType": "stock", "frame": "stock",
     "set": {"childTradingItemId": 41863782, "childRatio": 4.0, "eventType": "spin_off"},
     "evidence": "rc1 distributions web:sec.gov a08-6860_1ex99d1.htm (ratio 4.0 per pre-event share); master/current LMDI.A 41863782 securityStartDate 2008-03-03; ciqdividendcache 104.0 = 4 x LMDIA close basis"},
    {"name": "IAC 2008-08-21 spin-off leg ILG 0.2/share (co_acthist text '.2 OF A SHARE OF HSNI, IILG AND TKTM'; parser left ratio NaN)",
     "tradingItemId": 47439862, "exDate": "2008-08-21", "legType": "stock", "frame": "stock", "match_child": 47440078,
     "set": {"childRatio": 0.2}, "evidence": "co_acthist SPN text 2008-08-21: SHAREHOLDERS RECEIVED .2 OF A SHARE OF HSNI, IILG AND TKTM, AND 1/30 OF A SHARE OF TREE"},
    {"name": "IAC 2008-08-21 spin-off leg TKTM 0.2/share (same text)",
     "tradingItemId": 47439862, "exDate": "2008-08-21", "legType": "stock", "frame": "stock", "match_child": 47440956,
     "set": {"childRatio": 0.2}, "evidence": "co_acthist SPN text 2008-08-21: SHAREHOLDERS RECEIVED .2 OF A SHARE OF HSNI, IILG AND TKTM, AND 1/30 OF A SHARE OF TREE"},
    {"name": "VIA.B 2662339 -> CBS class B (24993622) 2006-01-03: 1 old Viacom B = 0.5 CBS B (line continuation) + 0.5 new Viacom B (spin leg 24993638); lineage ratio NaN -> 0.5",
     "tradingItemId": 2662339, "exDate": "2006-01-03", "legType": "quantity", "frame": "lineage", "match_child": 24993622,
     "set": {"parentFactor": 0.5, "status": "ok", "isEstimate": False},
     "evidence": "Viacom/CBS separation 2005-12-31 public terms: each Viacom class B share -> 0.5 CBS Corp class B + 0.5 new Viacom class B; price check (0.5x26.20 + 0.5x41.59)/32.60 = 1.04"},
    {"name": "VIA.B 2006-01-03 'CBS CORP' leg 0.5 (child 2596144 = pre-2000 CBS id) is the parent's own conversion into CBS class B (lineage row) -> dropped",
     "tradingItemId": 2662339, "exDate": "2006-01-03", "legType": "stock", "frame": "stock", "match_child": 2596144, "drop": True,
     "evidence": "self-conversion leg (rc1 newIdMethod name:tier4_scored(11) to defunct CBS id 2596144); covered by lineage 2662339->24993622 ratio 0.5"},
    {"name": "CTRA -> DVN stock merger 0.7 DVN per CTRA, settlement 2026-05-07 (events row had no terms, endDate gap_estimate 2026-05-06)",
     "tradingItemId": 2597012, "exDate": "2026-05-07", "legType": "stock", "frame": "merger", "add": True,
     "set": {"eventType": "merger_stock", "legType": "stock", "childTradingItemId": 2605788, "childRatio": 0.7, "payDate": "2026-05-07",
             "source": "manual_override", "notes": "actionCode=ACQ(GS);endDate=2026-05-06;settlementDate=2026-05-07"},
     "evidence": GS_EVIDENCE + "; corporate_actions 2597012 endDate 2026-05-06 gap_estimate no_terms"},
    {"name": "EXAS cash merger $105: settlement 2026-03-24 (events 2026-03-23; GS Merger row 2026-03-24)",
     "tradingItemId": 2610459, "exDate": "2026-03-23", "legType": "cash", "frame": "merger",
     "set": {"exDate": "2026-03-24", "payDate": "2026-03-24"}, "evidence": GS_EVIDENCE},
    {"name": "EA cash merger $210: settlement 2026-08-05 (events 2026-08-04; GS Merger row 2026-08-05)",
     "tradingItemId": 2608773, "exDate": "2026-08-04", "legType": "cash", "frame": "merger",
     "set": {"exDate": "2026-08-05", "payDate": "2026-08-05"}, "evidence": GS_EVIDENCE},
]


def price_ids(root: Path) -> set:
    """교정 규칙이 raw 종가를 필요로 하는 id(55 스케일 종목 + 97 인공물 + 수동 교정 부모·자회사)."""
    root = Path(root)
    ids = set()
    p = root / "qa" / "T7" / "J_gdiv_systematic_scale_ids.csv"
    if p.exists():
        ids |= set(pd.read_csv(p).tradingItemId.astype(int))
    p = root / "qa" / "T2" / "factor_artifacts.csv"
    if p.exists():
        ids |= set(pd.read_csv(p).tradingItemId.astype(int))
    for o in MANUAL_OVERRIDES:
        ids.add(int(o["tradingItemId"]))
        if "childTradingItemId" in o.get("set", {}):
            ids.add(int(o["set"]["childTradingItemId"]))
        if "match_child" in o:
            ids.add(int(o["match_child"]))
    return ids


# --------------------------------------------------------------------------------------
# 세전 규칙
# --------------------------------------------------------------------------------------
TRUST_NAME_RE = r"HOLDR|TRUST|ETF|FUND"      # 수탁수수료 차감(net) 이 실수령인 발행체(HOLDRs·ETF·펀드): 세후 규칙 적용 안 함
RESCINDED_SUPP = ("Dividend rescinded", "Dividend canceled")


def gross_first(c: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """ciqdividend 행 → amountLocal(세전, 원통화), amountNote, repairOf, amountRule 컬럼 추가. 반환 (df, repairs).

    벤더 3 값(divAmount·netAmount·grossAmount) 에서 세전 금액을 고르는 규칙(원천 컬럼별, 우선순위 순):
      R0 rescinded    : supplemental 'Dividend rescinded/canceled' → 지급 없음, 행 제외(교정 로그, F12)
      R1 after_tax    : netAmount == divAmount < grossAmount ≤ 2×div, 발행체가 HOLDRs/Trust/ETF/Fund 가 아님 → grossAmount
                        (CRH 25%, ICL, CMBT 30%, UBS 35%, STLA…)
      R1b             : grossAmount 있음, netAmount 없음, 0.6 ≤ div/gross ≤ 0.99, 비-트러스트 → grossAmount
                        ('Net amount' 표기 외국 ADR: AU 20%, LOGI 35%, KLMR·BWMX·AEHL·CSTE·ELTK; 리뷰 F3②)
      R2 inverted_net : grossAmount NaN, netAmount > divAmount, 0.6 ≤ div/net ≤ 0.99, 비-트러스트 → netAmount 가 세전 선언액
                        (벤더 표기 역전: RDPL·CBI·CNH 0.75, DB 0.79, KLMR 0.85, AZN·MCH 0.90 …)
      R3 net_eq_gross : netAmount == grossAmount < divAmount → net/gross(USD 지급액) 채택 (BCE 2020-06-12 divAmount 는 CAD 액면, GRP.U)
      R4 trust_net    : 트러스트/ETF 의 'Net amount'·역전 행 → divAmount(수수료 차감 후 실수령) 유지, notes net_of_trust_fees
      R5 default      : divAmount. grossAmount 가 2×div 초과(FBC 10×, GF 113×…) 또는 gross < div(SPIP·PFE·VER…) 는 벤더 오류로 보고
                        divAmount 유지 + notes(gross_amount_implausible / gross_lt_div)."""
    c = c.copy()
    div = c.divAmount.astype(float)
    net = c.netAmount.astype(float)
    gross = c.grossAmount.astype(float)
    ratio = gross / div
    names = c.companyName if "companyName" in c.columns else pd.Series("", index=c.index)
    trust = names.fillna("").astype(str).str.upper().str.contains(TRUST_NAME_RE, regex=True)
    rescinded = c.supplementalTypeName.isin(RESCINDED_SUPP)
    net_eq_div = net.notna() & (np.abs(net - div) < 1e-9)
    after_tax = ~rescinded & net_eq_div & gross.notna() & (gross > div * (1 + 1e-9)) & (ratio <= AFTER_TAX_MAX_RATIO) & ~trust
    # R1b(리뷰 F3②): 'Net amount' 표기 ADR 등 — gross 있음, net 없음, div/gross 0.6~0.99, 비-트러스트 → gross(원천징수 전)
    after_tax_b = ~rescinded & ~after_tax & gross.notna() & net.isna() & (div / gross >= 0.6) & (div / gross <= 0.99) & ~trust
    after_tax = after_tax | after_tax_b
    inverted = ~rescinded & ~after_tax & gross.isna() & net.notna() & (net > div * (1 + 1e-9)) & (div / net >= 0.6) & (div / net <= 0.99) & ~trust
    net_eq_gross = ~rescinded & ~after_tax & ~inverted & net.notna() & gross.notna() & (np.abs(net - gross) < 1e-9) & (gross < div * (1 - 1e-9))
    trust_net = ~rescinded & trust & ((gross.notna() & (gross > div * (1 + 1e-9))) | (net.notna() & (net > div * (1 + 1e-9))))
    implaus = ~rescinded & ~after_tax & ~inverted & ~net_eq_gross & ~trust & gross.notna() & (ratio > AFTER_TAX_MAX_RATIO)
    gross_lt = ~rescinded & ~net_eq_gross & gross.notna() & (gross < div * (1 - 1e-9))
    amount = np.where(after_tax, gross, np.where(inverted, net, np.where(net_eq_gross, gross, div)))
    rule = np.select([rescinded, after_tax & ~after_tax_b, after_tax_b, inverted, net_eq_gross, trust_net, implaus, gross_lt],
                     ["R0_rescinded", "R1_after_tax", "R1b_after_tax_net_nan", "R2_inverted_net", "R3_net_eq_gross", "R4_trust_net", "R5_gross_implausible", "R5_gross_lt_div"],
                     "R5_default")
    note = np.select([after_tax, inverted, net_eq_gross, trust_net, implaus, gross_lt],
                     ["after_tax_vendor_amount->gross", "inverted_net_gross->net", "net==gross<div->net(usd_paid)",
                      "net_of_trust_fees(gross=" + gross.round(6).astype(str) + ",net=" + net.round(6).astype(str) + ")",
                      "gross_amount_implausible(ratio=" + ratio.round(3).astype(str) + ")",
                      "gross_lt_div(gross=" + gross.round(6).astype(str) + ")"], "")
    tag = c.tradingItemId.astype(str) + ":" + c.exDate.dt.strftime("%Y-%m-%d")
    c["amountLocal"] = amount
    c["amountRule"] = rule
    c["amountNote"] = note
    c["repairOf"] = np.select([after_tax, inverted, net_eq_gross], ["after_tax_amount:" + tag, "inverted_net_amount:" + tag, "net_gross_amount:" + tag], None)
    c["rescinded"] = rescinded
    repairs = []
    for m, label in ((after_tax & ~after_tax_b, "R1 after_tax: netAmount==divAmount < grossAmount (withholding {w:.1%}), pre-tax = gross (P1)"),
                     (after_tax_b, "R1b after_tax(net NaN): grossAmount present, divAmount/gross in [0.6,0.99] (withholding {w:.1%}), non-trust -> pre-tax = gross (P1)"),
                     (inverted, "R2 inverted_net: grossAmount NaN, netAmount > divAmount (div/net {w:.3f}); vendor swapped net/gross, pre-tax = net"),
                     (net_eq_gross, "R3 net_eq_gross: netAmount == grossAmount < divAmount (divAmount = local face value); paid USD amount = net/gross")):
        for r in c[m].itertuples(index=False):
            w = 1 - r.divAmount / r.grossAmount if label.startswith("R1") else (r.divAmount / r.netAmount if label.startswith("R2") else r.divAmount / r.grossAmount)
            if label.startswith("R1b"):
                w = 1 - r.divAmount / r.grossAmount
            repairs.append({"tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.exDate), "field": "cashPerShare",
                            "before": float(r.divAmount), "after": float(r.amountLocal),
                            "evidence": f"dividendId={int(r.dividendId)} " + label.format(w=w) + f" [div={r.divAmount:.6g} net={r.netAmount:.6g} gross={r.grossAmount:.6g} supp={r.supplementalTypeName}]",
                            "source": "ciqdividend"})
    for r in c[rescinded].itertuples(index=False):
        repairs.append({"tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.exDate), "field": "row", "before": float(r.divAmount), "after": "excluded",
                        "evidence": f"dividendId={int(r.dividendId)} R0 supplemental '{r.supplementalTypeName}': dividend not paid, row excluded", "source": "ciqdividend"})
    log.info("gross_first: rules %s", pd.Series(rule).value_counts().to_dict())
    return c[~rescinded].copy(), repairs


# --------------------------------------------------------------------------------------
# 수동 교정
# --------------------------------------------------------------------------------------
def _rep(tid, ex, field, before, after, evidence, source="manual_override") -> dict:
    return {"tradingItemId": int(tid), "exDate": pd.Timestamp(ex), "field": field, "before": before, "after": after,
            "evidence": evidence, "source": source}


def apply_overrides(stock: pd.DataFrame, merger: pd.DataFrame, cash: pd.DataFrame, split: pd.DataFrame, inp: dict, cal,
                    lineage_rows: pd.DataFrame | None = None, frames_only=None) -> tuple:
    """MANUAL_OVERRIDES 적용. 기존 행을 찾으면 값 교체(+ source manual_override, repairOf), 없고 add=True 면 새 행, drop=True 면 행 제거(로그).
    반환 (stock, merger, cash, split, repairs) 또는 lineage_rows 를 주면 (stock, merger, cash, split, lineage, repairs)."""
    frames = {"stock": stock.copy(), "merger": merger.copy(), "cash": cash.copy(), "split": split.copy(),
              "lineage": lineage_rows.copy() if lineage_rows is not None else None}
    repairs = []
    for o in MANUAL_OVERRIDES:
        if frames_only is not None and o["frame"] not in frames_only:
            continue
        df = frames[o["frame"]]
        if df is None:
            continue
        ex = pd.Timestamp(o["exDate"])
        m = (df.tradingItemId == o["tradingItemId"]) & (df.exDate == ex) & (df.legType == o["legType"])
        if "match_child" in o:
            m &= df.childTradingItemId.map(lambda x: x is not None and pd.notna(x) and int(x) == o["match_child"])
        idx = df.index[m]
        tag = f"manual_override:{o['tradingItemId']}:{ex.date()}"
        if o.get("drop"):
            if len(idx):
                for i in idx:
                    repairs.append(_rep(o["tradingItemId"], ex, "row", f"child={o.get('match_child')} ratio={df.at[i, 'childRatio']}", "dropped", o["name"] + " | " + o["evidence"]))
                frames[o["frame"]] = df.drop(index=idx)
            else:
                repairs.append(_rep(o["tradingItemId"], ex, "row", "not_found", "skipped(drop)", o["name"] + " | " + o["evidence"]))
            continue
        if len(idx) == 0:
            if not o.get("add"):
                log.warning("override target not found: %s", o["name"])
                repairs.append(_rep(o["tradingItemId"], ex, "row", "not_found", "skipped", o["name"] + " | " + o["evidence"]))
                continue
            row = {c: None for c in df.columns}
            row.update({"tradingItemId": int(o["tradingItemId"]), "exDate": ex, "legType": o["legType"], "cashPerShare": np.nan,
                        "fxToUSD": np.nan, "parentFactor": 1.0, "childRatio": np.nan, "childValueSource": "unknown",
                        "childValuePerOldShare": np.nan, "subscriptionPrice": np.nan, "exerciseRatio": np.nan, "expiryDate": pd.NaT,
                        "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT, "status": "ok", "isEstimate": False,
                        "repairOf": tag, "notes": "", "evidence": o["evidence"], "source": "manual_override",
                        "_cashEquiv": np.nan, "_impliedFrac": np.nan, "_leg": 0})
            for k, v in o["set"].items():
                row[k] = pd.Timestamp(v) if k in ("exDate", "payDate", "expiryDate") else v
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            frames[o["frame"]] = df
            repairs.append(_rep(o["tradingItemId"], ex, "row", "absent", "added:" + ",".join(f"{k}={v}" for k, v in o["set"].items()), o["name"] + " | " + o["evidence"]))
            continue
        for i in idx:
            for k, v in o["set"].items():
                before = df.at[i, k] if k in df.columns else None
                val = pd.Timestamp(v) if k in ("exDate", "payDate", "expiryDate") else v
                df.at[i, k] = val
                if k not in ("notes",):
                    repairs.append(_rep(o["tradingItemId"], ex, k, before, val, o["name"] + " | " + o["evidence"]))
            df.at[i, "source"] = "manual_override"
            df.at[i, "evidence"] = (str(df.at[i, "evidence"]) + " | " + o["evidence"])[:600]
            prev = df.at[i, "repairOf"]
            df.at[i, "repairOf"] = tag if not prev else f"{prev};{tag}"
            df.at[i, "notes"] = (str(df.at[i, "notes"]) + ";" if df.at[i, "notes"] else "") + "manual_override"
    log.info("manual overrides: %d repair rows", len(repairs))
    if lineage_rows is not None:
        return frames["stock"], frames["merger"], frames["cash"], frames["split"], frames["lineage"], repairs
    return frames["stock"], frames["merger"], frames["cash"], frames["split"], repairs


# --------------------------------------------------------------------------------------
# R3 인계 사건(개명 거절 258 건 재분류 결과 중 원장 leg, stage_rc2/R3/for_R1_events.csv)
# --------------------------------------------------------------------------------------
def load_r3_events(path: Path, lineage: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """R3 hand-over → 원장 행 프레임(stock/merger/cash 프레임에 concat 할 수 있는 컬럼). quantity 행은 lineage v2 가 같은
    (oldId,newId) 를 갖고 있으면 건너뛴다(rename 행이 담당). merger 행의 parentFactor 0(라인 종료) 은 1.0 으로 정규화
    (원장 규약: 비-quantity 행 parentFactor 1; 종료는 events/lineage 담당). 반환 (rows, repairs)."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(), []
    f = pd.read_csv(path)
    f["exDate"] = pd.to_datetime(f.exDate)
    pairs = set(zip(lineage.oldId.astype(int), lineage.newId.astype(int)))
    rows, repairs, skipped = [], [], 0
    aux_div = f[f.source.astype(str) == "ciqdividend(aux)"]
    f = f[f.source.astype(str) != "ciqdividend(aux)"]
    # 보조 종목(aux) 현금배당: evidence 'ciqdividend id N type T div X gross Y net Z pay D' → 세전 규칙(R1/R2/R3) 을 같은 방식으로 적용
    n_aux_changed = 0
    for r in aux_div.itertuples(index=False):
        m = re.search(r"ciqdividend id (\d+) type (\d+) div ([0-9.eE+-]+|nan)?\s*gross\s*([0-9.eE+-]+)?\s*net\s*([0-9.eE+-]+)?", str(r.evidence))
        did = int(m.group(1)) if m else -1
        div = float(m.group(3)) if m and m.group(3) not in (None, "nan") else (float(r.cashPerShare) if pd.notna(r.cashPerShare) else np.nan)
        gross = float(m.group(4)) if m and m.group(4) else np.nan
        net = float(m.group(5)) if m and m.group(5) else np.nan
        amt, rule = float(r.cashPerShare) if pd.notna(r.cashPerShare) else div, "R5_default"
        if np.isfinite(net) and abs(net - div) < 1e-9 and np.isfinite(gross) and gross > div * (1 + 1e-9) and gross / div <= AFTER_TAX_MAX_RATIO:
            amt, rule = gross, "R1_after_tax"
        elif np.isfinite(gross) and not np.isfinite(net) and 0.6 <= div / gross <= 0.99:
            amt, rule = gross, "R1b_after_tax_net_nan"
        elif not np.isfinite(gross) and np.isfinite(net) and net > div * (1 + 1e-9) and 0.6 <= div / net <= 0.99:
            amt, rule = net, "R2_inverted_net"
        elif np.isfinite(net) and np.isfinite(gross) and abs(net - gross) < 1e-9 and gross < div * (1 - 1e-9):
            amt, rule = gross, "R3_net_eq_gross"
        if rule != "R5_default":
            n_aux_changed += 1
            # before = 벤더 divAmount(주 경로 gross_first 로그와 같은 기준; R3 인계값은 gross 가 있으면 이미 gross 라 R1/R1b 는 인계값 기준 무변경)
            repairs.append(_rep(r.tradingItemId, r.exDate, "cashPerShare", div if np.isfinite(div) else float(r.cashPerShare), amt,
                                f"dividendId={did} aux dividend {rule} [div={div:.6g} net={net:.6g} gross={gross:.6g}] (R3 hand-over; R3 value {float(r.cashPerShare):.6g})", "ciqdividend(aux)"))
        rows.append({"tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.exDate), "eventType": str(r.eventType), "legType": "cash",
                     "cashPerShare": amt, "cashCurrency": "USD", "fxToUSD": 1.0, "parentFactor": 1.0, "childTradingItemId": None, "childRatio": np.nan,
                     "childValueSource": "unknown", "childValuePerOldShare": np.nan, "subscriptionPrice": np.nan, "expiryDate": pd.NaT, "exerciseRatio": np.nan,
                     "payDate": pd.NaT, "recordDate": pd.NaT, "announcedDate": pd.NaT, "source": "ciqdividend(aux)",
                     "evidence": f"dividendId={did};R3 aux: " + str(r.evidence)[:200], "status": "ok", "isEstimate": False,
                     "repairOf": None if rule == "R5_default" else f"{rule.lower()}:{int(r.tradingItemId)}:{pd.Timestamp(r.exDate).date()}",
                     "notes": "R3_aux_dividend" + ("" if rule == "R5_default" else f";{rule}"), "_cashEquiv": np.nan, "_impliedFrac": np.nan, "_leg": 0, "_prio": 0.5,
                     "_r3_status": "ok"})
    if len(aux_div):
        # 같은 (id, exDate) 의 aux 배당은 합산(정기+특별 등, dividendId 별개). type 5(통화 옵션) 만으로 된 그룹은 1 행(지급일 있는 첫 행)
        aux_rows = [r for r in rows if r.get("source") == "ciqdividend(aux)"]
        rows = [r for r in rows if r.get("source") != "ciqdividend(aux)"]
        grp = {}
        for r in aux_rows:
            grp.setdefault((r["tradingItemId"], r["exDate"]), []).append(r)
        n_summed = n_opt = 0
        for k, g in grp.items():
            if len(g) == 1:
                rows.append(g[0])
                continue
            types = [int(re.search(r"type (\d+)", r["evidence"]).group(1)) if re.search(r"type (\d+)", r["evidence"]) else -1 for r in g]
            if all(t == 5 for t in types):
                pick = next((r for r in g if " pay 20" in r["evidence"] or " pay 19" in r["evidence"]), g[0])
                pick = dict(pick, notes=pick["notes"] + f";type5_currency_options={len(g)}_rows_kept_1",
                            evidence=pick["evidence"] + " | other option rows: " + ",".join(r["evidence"].split(";")[0].replace("dividendId=", "") for r in g if r is not pick))
                rows.append(pick)
                n_opt += 1
                continue
            first = dict(g[0])
            first["cashPerShare"] = float(np.nansum([r["cashPerShare"] for r in g]))
            first["eventType"] = "special_dividend" if any(r["eventType"] == "special_dividend" for r in g) else "cash_dividend"
            first["evidence"] = "dividendId=" + ",".join(r["evidence"].split(";")[0].replace("dividendId=", "") for r in g) + ";R3 aux merged " + str(len(g)) + " rows"
            first["notes"] = first["notes"] + f";merged_rows={len(g)}"
            first["repairOf"] = ";".join(sorted({r["repairOf"] for r in g if r.get("repairOf")})) or None
            rows.append(first)
            n_summed += 1
        log.info("R3 aux dividends: %d rows (%d ids), pre-tax rule changed %d, same-day summed %d, type-5 option groups collapsed %d",
                 len(aux_div), aux_div.tradingItemId.nunique(), n_aux_changed, n_summed, n_opt)
    for r in f.itertuples(index=False):
        child = int(r.childTradingItemId) if pd.notna(r.childTradingItemId) else None
        if r.legType == "quantity":
            if child is not None and (int(r.tradingItemId), child) in pairs:
                skipped += 1
                continue
            et = "rename"                                     # 라인 연속(계보) — lineage v2 에 없을 때만
        else:
            et = str(r.eventType)
        pf = float(r.parentFactor) if pd.notna(r.parentFactor) and r.parentFactor > 0 else 1.0
        if r.legType != "quantity":
            pf = 1.0
        rows.append({"tradingItemId": int(r.tradingItemId), "exDate": pd.Timestamp(r.exDate), "eventType": et, "legType": str(r.legType),
                     "cashPerShare": float(r.cashPerShare) if pd.notna(r.cashPerShare) else np.nan,
                     "cashCurrency": str(r.cashCurrency) if pd.notna(r.cashCurrency) else None,
                     "fxToUSD": 1.0 if pd.notna(r.cashPerShare) else np.nan, "parentFactor": pf, "childTradingItemId": child,
                     "childRatio": float(r.childRatio) if pd.notna(r.childRatio) else np.nan,
                     "childValueSource": str(r.childValueSource) if pd.notna(r.childValueSource) else "unknown", "childValuePerOldShare": np.nan,
                     "subscriptionPrice": np.nan, "expiryDate": pd.NaT, "exerciseRatio": np.nan, "payDate": pd.NaT, "recordDate": pd.NaT,
                     "announcedDate": pd.NaT, "source": str(r.source) if pd.notna(r.source) else "manual_override",
                     "evidence": ("R3 hand-over: " + str(r.evidence))[:600], "status": str(r.status) if pd.notna(r.status) else "ok",
                     "isEstimate": bool(r.isEstimate), "repairOf": f"r3_handover:{int(r.tradingItemId)}:{pd.Timestamp(r.exDate).date()}",
                     "notes": ("R3;" + str(r.notes)) if pd.notna(r.notes) else "R3", "_cashEquiv": np.nan, "_impliedFrac": np.nan, "_leg": 0, "_prio": 0.5,
                     "_r3_status": str(r.status) if pd.notna(r.status) else "ok"})
        repairs.append(_rep(r.tradingItemId, r.exDate, "row", "absent", f"added:{et}/{r.legType} child={child} ratio={r.childRatio} cash={r.cashPerShare}",
                            "R3 hand-over for_R1_events.csv | " + str(r.evidence)[:300], str(r.source) if pd.notna(r.source) else "manual_override"))
    log.info("R3 hand-over: %d rows added, %d quantity rows covered by lineage v2", len(rows), skipped)
    return pd.DataFrame(rows), repairs


# --------------------------------------------------------------------------------------
# 평가 뒤 수동 상태 교정(R1c 리드 지시): 값을 지우고 unresolved 로 표시(함의값·근거는 notes/evidence)
# --------------------------------------------------------------------------------------
VALUE_OVERRIDES = [
    {"name": "Lionsgate 412297487 2025-05-07 STRZ leg: 1.12 x STRZ close 11.20 = 12.544/old share (146% of the 8.59 parent price) vs successor +6.3% with the 1.12 conversion -> implausible",
     "tradingItemId": 412297487, "exDate": "2025-05-07", "legType": "stock", "childTradingItemId": 1937066655,
     "implied": "co_acthist: 1.12 PRE-SPLIT STRZ shares per old share, C/E $0.836267 (= 1.12/15 x 11.20; STRZ 1-for-15 reverse split at distribution) -> likely ratio 0.074667, value 0.836267 per old share",
     "evidence": "R1c lead instruction 2026-09-17: transition-day leg value 12.544 implies +152% on the stitched successor 1941153615 (prev 8.59, 05-07 close 8.15 x 1.12) — inconsistent with the parent's own move; set unresolved (value NaN), decision on the 1/15 ratio pending"},
]


def apply_value_overrides(stock: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """VALUE_OVERRIDES: 해당 stock/rights 행의 childValuePerOldShare·cashPerShare 를 NaN, childValueSource unknown, status unresolved, isEstimate False 로,
    notes 에 rejected_value·implied 기록. 반환 (stock, repairs)."""
    st = stock.copy()
    repairs = []
    for o in VALUE_OVERRIDES:
        ck = st.childTradingItemId.map(lambda x: -1 if x is None or pd.isna(x) else int(x))
        m = (st.tradingItemId.astype(int) == int(o["tradingItemId"])) & (pd.to_datetime(st.exDate) == pd.Timestamp(o["exDate"])) \
            & (st.legType == o["legType"]) & (ck == int(o.get("childTradingItemId", -1)))
        if not m.any():
            repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", "not_found", "value_override_skipped", o["name"] + " | " + o["evidence"]))
            continue
        for i in st.index[m]:
            before = float(st.at[i, "childValuePerOldShare"]) if pd.notna(st.at[i, "childValuePerOldShare"]) else np.nan
            st.at[i, "childValuePerOldShare"] = np.nan
            if "cashPerShare" in st.columns:
                st.at[i, "cashPerShare"] = np.nan
            st.at[i, "childValueSource"] = "unknown"
            st.at[i, "status"] = "unresolved"
            st.at[i, "isEstimate"] = False
            st.at[i, "notes"] = str(st.at[i, "notes"]) + f";value_override_unresolved(rejected_value={before:.6g};implied: {o['implied']})"
            st.at[i, "evidence"] = str(st.at[i, "evidence"]) + " | " + o["evidence"]
            st.at[i, "repairOf"] = (str(st.at[i, "repairOf"]) + ";" if pd.notna(st.at[i, "repairOf"]) and st.at[i, "repairOf"] else "") + f"value_override:{int(o['tradingItemId'])}:{o['exDate']}"
            repairs.append(_rep(o["tradingItemId"], o["exDate"], "childValuePerOldShare", before, "NaN(unresolved)", o["name"] + " | " + o["evidence"]))
    log.info("value overrides: %d rows set unresolved", sum(1 for r in repairs if r["field"] == "childValuePerOldShare"))
    return st, repairs


# --------------------------------------------------------------------------------------
# 배당계수 스케일 오류 55 종목
# --------------------------------------------------------------------------------------
def divfactor_scale_repairs(cash: pd.DataFrame, dividend_factor: pd.DataFrame, closes, root: Path, lineage: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list]:
    """55 종목 벤더 divadj 스텝 ↔ 실제 ciqdividend 사건 대조(정보성 로그, field=vendor_divadj_step; 원장 값은 바꾸지 않음).
    같은 날 사건을 먼저 대조하고, 없을 때만 ±3 일 사건 합계를 쓴다(연일 소액배당 HOLDRs 에서 합계 비교가 만든 거짓 불일치 제거).
    일치(±5%) 스텝은 로그에 남기지 않고 개수만 센다."""
    p = Path(root) / "qa" / "T7" / "J_gdiv_systematic_scale_ids.csv"
    if not p.exists():
        log.warning("scale id list missing: %s", p)
        return cash, []
    scale = pd.read_csv(p)
    ids = set(scale.tradingItemId.astype(int))
    d = dividend_factor[dividend_factor.tradingItemId.isin(ids)].sort_values(["tradingItemId", "validFrom"]).reset_index(drop=True)
    same = d.tradingItemId.to_numpy()[1:] == d.tradingItemId.to_numpy()[:-1]
    fp, fn = d.factor.to_numpy()[:-1], d.factor.to_numpy()[1:]
    step = same & (fp != fn)
    idx = np.flatnonzero(step) + 1
    b = pd.DataFrame({"tradingItemId": d.tradingItemId.to_numpy()[idx], "validFrom": d.validFrom.to_numpy()[idx],
                      "fBefore": fp[idx - 1], "fAfter": fn[idx - 1]})
    b = b[b.validFrom >= pd.Timestamp("2000-01-01")].reset_index(drop=True)
    b["impliedFrac"] = 1.0 - b.fBefore / b.fAfter
    p_prev, _ = closes.before(b.tradingItemId.to_numpy(np.int64), b.validFrom.to_numpy())
    b["pPrev"] = p_prev
    b["impliedD"] = b.impliedFrac * b.pPrev
    cash = cash.copy()
    # 사건은 exDate 에 거래되던 id 에 귀속되므로(F4) 계보 이웃(옛/새 id)의 현금 행도 같은 사건 후보로 본다
    fam = {i: {i} for i in ids}
    if lineage is not None and len(lineage):
        for a, bb in zip(lineage.oldId.astype(int), lineage.newId.astype(int)):
            if a in fam:
                fam[a].add(bb)
            if bb in fam:
                fam[bb].add(a)
    all_ids = set().union(*fam.values())
    cash_by = {tid: g for tid, g in cash[cash.tradingItemId.isin(all_ids)].groupby("tradingItemId")}
    repairs = []
    n_mis = n_nomatch = n_ok = n_same_day = 0
    for r in b.itertuples(index=False):
        parts = [cash_by[t] for t in fam.get(int(r.tradingItemId), {int(r.tradingItemId)}) if t in cash_by]
        g = pd.concat(parts) if parts else None
        near = None
        if g is not None:
            gap = (g.exDate - pd.Timestamp(r.validFrom)).dt.days
            same_day = g[gap == 0]
            if len(same_day):
                near = same_day
                n_same_day += 1
            else:
                near = g[gap.abs() <= 3]
        tag = f"divfactor_scale:{int(r.tradingItemId)}:{pd.Timestamp(r.validFrom).date()}"
        if near is None or near.empty:
            n_nomatch += 1
            repairs.append(_rep(r.tradingItemId, r.validFrom, "vendor_divadj_step", float(r.impliedD) if np.isfinite(r.impliedD) else None, None,
                                f"vendor divAdjFactor step {r.fBefore:.6g}->{r.fAfter:.6g} (implied {r.impliedFrac:.4%} x P_prev {r.pPrev:.4g}) has no ciqdividend cash event within ±3d; step not used (P5)",
                                "ciqpriceequitydivadjfactor"))
            continue
        actual = float(near.cashPerShare.sum())
        ratio = r.impliedD / actual if actual > 0 and np.isfinite(r.impliedD) else np.nan
        if np.isfinite(ratio) and abs(ratio - 1.0) <= SCALE_TOL:
            n_ok += 1
            continue
        n_mis += 1
        repairs.append(_rep(r.tradingItemId, r.validFrom, "vendor_divadj_step", float(r.impliedD) if np.isfinite(r.impliedD) else None, actual,
                            f"vendor divAdjFactor step {r.fBefore:.6g}->{r.fAfter:.6g} implies {r.impliedD:.6g} (= {r.impliedFrac:.4%} x P_prev {r.pPrev:.4g}), "
                            f"actual ciqdividend {actual:.6g} ({'same day' if len(near) and (near.exDate == pd.Timestamp(r.validFrom)).all() else '±3d sum'}, ratio {ratio:.3f}); "
                            f"ledger keeps the actual event (P12), vendor factor not used (P5)", "ciqpriceequitydivadjfactor"))
        for i in near.index:
            prev = cash.at[i, "repairOf"]
            cash.at[i, "repairOf"] = tag if not prev else f"{prev};{tag}"
            cash.at[i, "notes"] = (str(cash.at[i, "notes"]) + ";" if cash.at[i, "notes"] else "") + f"vendor_step_implied={r.impliedD:.6g}(ratio {ratio:.3f})"
    log.info("divfactor scale (55 ids): steps %d, same-day matched %d, mismatched %d, no cash event %d, consistent %d", len(b), n_same_day, n_mis, n_nomatch, n_ok)
    return cash, repairs


# --------------------------------------------------------------------------------------
# 분할 인공물 97 건
# --------------------------------------------------------------------------------------
def split_artifact_check(split: pd.DataFrame, ciqsplit: pd.DataFrame | None, ciqsplitcache: pd.DataFrame | None, closes, root: Path, cal) -> tuple[pd.DataFrame, list]:
    p = Path(root) / "qa" / "T2" / "factor_artifacts.csv"
    if not p.exists():
        log.warning("factor artifact list missing: %s", p)
        return split, []
    art = pd.read_csv(p)
    art = art[art.category.str.startswith("unexplained")].copy()
    art["date"] = pd.to_datetime(art.date)
    art["prevDate"] = pd.to_datetime(art.prevDate)
    split = split.copy()
    cs = ciqsplit.copy() if ciqsplit is not None else pd.DataFrame(columns=["tradingitemid", "exdate", "rate", "splittypeid"])
    cs["exdate"] = pd.to_datetime(cs.exdate)
    sc = ciqsplitcache.copy() if ciqsplitcache is not None else pd.DataFrame(columns=["tradingitemid", "splitdate", "latestsplitfactor"])
    sc["splitdate"] = pd.to_datetime(sc.splitdate)
    repairs = []
    stats = {"moved_date_found": 0, "no_price_counterpart": 0, "row_missing": 0}
    for a in art.itertuples(index=False):
        tid, date = int(a.tradingItemId), pd.Timestamp(a.date)
        pf = float(a.sf_prev) / float(a.sf)
        # 인공물 목록의 date 는 그 종목의 다음 관측일(수익률 행): 경계는 (prevDate, date] 안에 있음
        m = (split.tradingItemId == tid) & (split.exDate > pd.Timestamp(a.prevDate)) & (split.exDate <= date)
        if not m.any():
            stats["row_missing"] += 1
            repairs.append(_rep(tid, date, "split.validFrom", str(date.date()), None, "artifact listed but no ledger split boundary in (prevDate, date]", "factors/split"))
            continue
        i = split.index[m][0]
        if "csv_override:" in str(split.at[i, "repairOf"] or ""):
            # L1 최종: rc2f split CSV 로 확정된 경계 — status 는 CSV 가 정함(ok/estimated); 인공물 목록의 판정으로 되돌리지 않고 표시만
            split.at[i, "notes"] = str(split.at[i, "notes"]) + ";artifact_listed(csv_boundary_status_kept)"
            stats["csv_boundary_kept"] = stats.get("csv_boundary_kept", 0) + 1
            continue
        ex = pd.Timestamp(split.at[i, "exDate"])
        vf = str(split.at[i, "evidence"]).split("split.validFrom=")[-1][:10]      # 실제 계수 경계(validFrom)
        g = cs[(cs.tradingitemid == tid) & ((cs.exdate - date).abs().dt.days <= JUMP_SEARCH_DAYS)]
        cs_txt = ";".join(f"ciqsplit {d.date()} rate={r:.6g} type={int(t)}" for d, r, t in zip(g.exdate, g.rate, g.splittypeid)) or "no ciqsplit ±60d"
        h = sc[(sc.tradingitemid == tid) & ((sc.splitdate - date).abs().dt.days <= JUMP_SEARCH_DAYS)]
        sc_txt = ";".join(f"splitcache {d.date()} lsf={float(l):.6g}" for d, l in zip(h.splitdate, h.latestsplitfactor)) or "no splitcache row ±60d"
        s = closes.series(tid, date - pd.Timedelta(days=JUMP_SEARCH_DAYS), date + pd.Timedelta(days=JUMP_SEARCH_DAYS))
        found = None
        if len(s) >= 2:
            ratio = (s / s.shift(1)).iloc[1:]
            cand = ratio[(ratio * pf - 1.0).abs() <= JUMP_MATCH_TOL]
            if len(cand):
                j = cand.index[int(np.argmin(np.abs((cand.index - date).days)))]
                found = (pd.Timestamp(j), float(cand.loc[j]))
        tag = f"split_artifact:{tid}:{date.date()}"
        prev = split.at[i, "repairOf"]
        split.at[i, "repairOf"] = tag if not prev else f"{prev};{tag}"
        if found is not None and found[0] != ex:
            stats["moved_date_found"] += 1
            repairs.append(_rep(tid, date, "split.validFrom", vf, str(found[0].date()),
                                f"raw close jump x parentFactor({pf:.6g}) = {found[1] * pf:.3f} on {found[0].date()} (boundary day raw x pf = {a.r_raw + 1:.3f} x {pf:.3g} = {(a.r_raw + 1) * pf:.3f}); {cs_txt}; {sc_txt}",
                                "factors/split"))
            split.at[i, "status"] = "estimated"
            split.at[i, "isEstimate"] = True
            split.at[i, "notes"] = str(split.at[i, "notes"]) + f";artifact:effective_date_mismatch->{found[0].date()}"
        elif found is not None:
            split.at[i, "notes"] = str(split.at[i, "notes"]) + ";artifact_listed_but_raw_jump_matches"
        else:
            stats["no_price_counterpart"] += 1
            repairs.append(_rep(tid, date, "split.factor", f"{a.sf_prev:.6g}->{a.sf:.6g} @validFrom {vf}", "no_price_counterpart(proposal: drop step or fix raw)",
                                f"no raw close jump ≈ 1/{pf:.6g} within ±{JUMP_SEARCH_DAYS}d (boundary day raw ret {a.r_raw:+.3f}, split_adj ret {a.r_adj:+.3f}); {cs_txt}; {sc_txt}",
                                "factors/split"))
            split.at[i, "status"] = "unresolved"
            split.at[i, "isEstimate"] = True
            split.at[i, "notes"] = str(split.at[i, "notes"]) + ";artifact:no_price_counterpart"
    log.info("split artifacts (97): %s", stats)
    return split, repairs


# --------------------------------------------------------------------------------------
# rc2f: 교정 CSV(orch_rc2f/BRIEF.md §3 ledger_overrides.csv) — MANUAL_OVERRIDES 와 같은 메커니즘(match/set/add/drop)
# --------------------------------------------------------------------------------------
CSV_FRAME_OF_TYPE = {"cash_dividend": "cash", "special_dividend": "cash", "split": "split", "stock_dividend": "split", "spin_off": "stock",
                     "rights": "stock", "warrant": "stock", "merger_cash": "merger", "merger_stock": "merger", "rename": "lineage", "relist": "lineage"}
CSV_POST_KEYS = ("childValuePerOldShare", "childValueSource", "status", "isEstimate")     # 평가(value_children) 뒤에 재확정하는 키
CSV_VALUE_REL_TOL = 1e-4           # L1 3차: 빌더가 자회사 종가로 평가한 값(child_close_ex 등)과 CSV 의 childValuePerOldShare 가 이 안이면 빌더 정확값 유지
BUILDER_VALUE_SOURCES = ("child_close_ex", "wi_close")        # P10 ① / when-issued close: values the builder computed from prices (ledger_build.VALUE_SOURCES)
CSV_DATE_KEYS = ("exDate", "payDate", "expiryDate")
CSV_TAG = "csv_override"


def _csv_frame(event_type: str, leg_type: str, source: str) -> str:
    """원장 행 → ledger_build 프레임(행이 만들어진 곳). quantity: rename/relist → lineage, 그 밖 → split. cash: vendor_implied → gaps(평가 뒤 프레임),
    corporate_actions → merger, 그 밖 → cash. stock/rights: distributions/ciqdividend 원천은 eventType 이 merger_stock 이어도 stock 프레임
    (build_stock_rows 의 eventClass=merger 행), corporate_actions·R3 인계(lineage/co_acthist/ciqsplit) merger_* 는 merger, 그 밖 → stock."""
    src = str(source or "")
    if leg_type == "quantity":
        return "lineage" if event_type in ("rename", "relist") else "split"
    if leg_type == "cash":
        if src.startswith("vendor_implied"):
            return "gaps"
        return "merger" if src == "corporate_actions" else "cash"
    if src == "distributions" or src.startswith("ciqdividend"):
        return "stock"
    return "merger" if event_type in ("merger_cash", "merger_stock") else "stock"


def load_split_moves(path) -> dict:
    """rc2f_apply 가 쓴 split_boundary_moves.csv (tradingItemId, exDate, newExDate, overrideId): move_date 로 옮긴 경계 → {(id, old exDate): (new exDate, overrideId)}."""
    if path is None or not Path(path).exists():
        return {}
    m = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {(int(float(r.tradingItemId)), pd.Timestamp(r.exDate)): (pd.Timestamp(r.newExDate), r.overrideId) for r in m.itertuples(index=False) if r.exDate and r.newExDate}


def load_csv_overrides(paths, ref_ledger_path=None, split_moves: dict | None = None) -> tuple[list[dict], list[dict]]:
    """교정 CSV → 정규화 목록. 각 항목: {overrideId, caseId, frame, tradingItemId, exDate, legType, match_child(int|None), action,
    set(dict, 날짜는 Timestamp), csvIsEstimate, evidence, source, targetEventId}. eventId 로만 지정된 행은 ref_ledger(rc2 원장)로
    (id, exDate, legType, child, eventType, source) 를 푼다. 반환 (overrides, rejected[{overrideId, reason}])."""
    ref = None
    out, rejected = [], []
    for p in paths:
        p = Path(p)
        df = pd.read_csv(p, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8-sig")
        df.columns = [c.strip() for c in df.columns]
        for c in ("eventId", "match_tradingItemId", "match_exDate", "match_legType", "match_childTradingItemId", "frame", "caseId", "source", "isEstimate", "eventType"):
            if c not in df.columns:
                df[c] = ""
        for r in df.itertuples(index=False):
            oid = r.overrideId.strip()
            act = r.action.strip()
            try:
                setd = json.loads(r.set) if r.set.strip() else {}
            except json.JSONDecodeError as e:
                rejected.append({"overrideId": oid, "reason": f"set JSON: {e}"})
                continue
            for k in CSV_DATE_KEYS:
                if k in setd:
                    setd[k] = pd.Timestamp(setd[k]) if setd[k] not in (None, "", "NaT") else pd.NaT
            for k in ("childRatio", "childValuePerOldShare", "cashPerShare", "subscriptionPrice", "exerciseRatio", "parentFactor"):
                if k in setd:
                    setd[k] = np.nan if setd[k] in (None, "", "nan", "NaN", "null") else float(setd[k])
            if "childTradingItemId" in setd:
                setd["childTradingItemId"] = None if setd["childTradingItemId"] in (None, "", "null") else int(setd["childTradingItemId"])
            if "isEstimate" in setd and not isinstance(setd["isEstimate"], bool):
                setd["isEstimate"] = str(setd["isEstimate"]).strip().lower() in ("true", "1", "yes")
            csv_est = str(r.isEstimate).strip().lower() in ("true", "1", "yes")
            item = {"overrideId": oid, "caseId": r.caseId, "action": act, "set": setd, "csvIsEstimate": csv_est, "evidence": r.evidence,
                    "source": r.source or "csv_override", "targetEventId": r.eventId.strip(), "match_child": None, "frame": r.frame.strip() or None,
                    "eventType": r.eventType.strip() or None}
            if act in ("set", "drop"):
                if r.eventId.strip():
                    if ref is None:
                        if ref_ledger_path is None or not Path(ref_ledger_path).exists():
                            rejected.append({"overrideId": oid, "reason": "eventId given but no reference ledger to resolve it"})
                            continue
                        ref = pd.read_parquet(ref_ledger_path).set_index("eventId")
                    if r.eventId.strip() not in ref.index:
                        rejected.append({"overrideId": oid, "reason": f"eventId {r.eventId} not in reference ledger"})
                        continue
                    t = ref.loc[r.eventId.strip()]
                    if str(t.source) == "stitched_copy" and isinstance(t.keeperEventId, str) and t.keeperEventId in ref.index:
                        item["notes_redirect"] = f"redirected_from_stitched_copy:{r.eventId.strip()}"      # copies are regenerated from the keeper
                        t = ref.loc[t.keeperEventId]
                    item.update(tradingItemId=int(t.tradingItemId), exDate=pd.Timestamp(t.exDate), legType=str(t.legType),
                                match_child=None if pd.isna(t.childTradingItemId) else int(t.childTradingItemId),
                                frame=item["frame"] or _csv_frame(str(t.eventType), str(t.legType), str(t.source)), eventType=str(t.eventType),
                                targetSource=str(t.source), targetStatus=str(t.status), targetOrigin=_origin_source(t))
                    if split_moves and str(t.legType) == "quantity" and (int(t.tradingItemId), pd.Timestamp(t.exDate)) in split_moves:
                        # L1 최종: 같은 실행의 split move_date 가 이 경계를 옮겼다 → 새 날짜의 행이 대상(stitched_copies_map 의 재키잉과 같은 규칙)
                        new_ex, sid = split_moves[(int(t.tradingItemId), pd.Timestamp(t.exDate))]
                        item["exDate"] = new_ex
                        item["notes_redirect"] = (item.get("notes_redirect", "") + ";" if item.get("notes_redirect") else "") + f"redirected_by_split_move_date:{sid}:{pd.Timestamp(t.exDate).date()}->{new_ex.date()}"
                else:
                    if not (r.match_tradingItemId.strip() and r.match_exDate.strip() and r.match_legType.strip()):
                        rejected.append({"overrideId": oid, "reason": "set/drop needs eventId or match_tradingItemId/match_exDate/match_legType"})
                        continue
                    item.update(tradingItemId=int(float(r.match_tradingItemId)), exDate=pd.Timestamp(r.match_exDate), legType=r.match_legType.strip(),
                                match_child=None if not r.match_childTradingItemId.strip() else int(float(r.match_childTradingItemId)))
                    if split_moves and item["legType"] == "quantity" and (item["tradingItemId"], item["exDate"]) in split_moves:
                        new_ex, sid = split_moves[(item["tradingItemId"], item["exDate"])]
                        item["notes_redirect"] = f"redirected_by_split_move_date:{sid}:{item['exDate'].date()}->{new_ex.date()}"
                        item["exDate"] = new_ex
                    if item["frame"] is None:
                        if ref is None and ref_ledger_path is not None and Path(ref_ledger_path).exists():
                            ref = pd.read_parquet(ref_ledger_path).set_index("eventId")
                        if ref is not None:
                            m = ref[(ref.tradingItemId == item["tradingItemId"]) & (ref.exDate == item["exDate"]) & (ref.legType == item["legType"])]
                            if item["match_child"] is not None:
                                m = m[m.childTradingItemId.astype("Int64") == item["match_child"]]
                            if len(m) == 1:
                                item["frame"] = _csv_frame(str(m.eventType.iloc[0]), str(m.legType.iloc[0]), str(m.source.iloc[0]))
                                item["eventType"] = str(m.eventType.iloc[0])
                                item["targetSource"] = str(m.source.iloc[0]); item["targetStatus"] = str(m.status.iloc[0]); item["targetOrigin"] = _origin_source(m.iloc[0])
                        if item["frame"] is None:
                            item["frame"] = {"cash": "cash", "quantity": "split", "stock": "stock", "rights": "stock"}[item["legType"]]
            elif act == "add":
                et, lt = setd.get("eventType"), setd.get("legType")
                if not et or not lt or pd.isna(setd.get("exDate", pd.NaT)):
                    rejected.append({"overrideId": oid, "reason": "add needs set.eventType/legType/exDate"})
                    continue
                item.update(tradingItemId=int(float(r.tradingItemId)), exDate=pd.Timestamp(setd["exDate"]), legType=lt,
                            match_child=setd.get("childTradingItemId"), frame=item["frame"] or _csv_frame(et, lt, ""), eventType=et)
            else:
                rejected.append({"overrideId": oid, "reason": f"action '{act}' not in set/drop/add"})
                continue
            out.append(item)
    return out, rejected


def csv_override_ids(overrides: list[dict]) -> set:
    """가격 lookup 이 필요한 id(부모·자회사)."""
    ids = set()
    for o in overrides:
        ids.add(int(o["tradingItemId"]))
        for k in ("match_child",):
            if o.get(k) is not None:
                ids.add(int(o[k]))
        c = o["set"].get("childTradingItemId")
        if c is not None:
            ids.add(int(c))
    return ids


def _csv_match(df: pd.DataFrame, o: dict) -> pd.Index:
    m = (df.tradingItemId.astype(int) == int(o["tradingItemId"])) & (pd.to_datetime(df.exDate) == pd.Timestamp(o["exDate"])) & (df.legType == o["legType"])
    if o.get("match_child") is not None:
        m &= df.childTradingItemId.map(lambda x: x is not None and pd.notna(x) and int(x) == int(o["match_child"]))
    elif o["action"] != "add" and o["legType"] in ("stock", "rights"):
        m &= df.childTradingItemId.map(lambda x: x is None or pd.isna(x))
    if o["action"] != "add" and o.get("eventType") and "eventType" in df.columns:
        m2 = m & (df.eventType == o["eventType"])
        if m2.any():
            m = m2
    return df.index[m]


def _dropped_record(row: pd.Series, oid: str) -> dict:
    """ca_ledger_dropped_duplicates.csv 행(ledger_build dropped_all 형식): CSV 교정으로 버린 행(Q4 dividendId 회계용)."""
    ck = row.get("childTradingItemId")
    return {"tradingItemId": int(row.tradingItemId), "exDate": pd.Timestamp(row.exDate), "eventType": row.get("eventType"), "legType": row.get("legType"),
            "childKey": -1 if ck is None or pd.isna(ck) else int(ck), "cashPerShare": row.get("cashPerShare", np.nan), "childRatio": row.get("childRatio", np.nan),
            "source": row.get("source"), "status": row.get("status"), "reason": f"{CSV_TAG}:{oid}", "notes": row.get("notes", ""), "evidence": row.get("evidence", "")}


def _origin_source(t) -> str:
    """The source a reference-ledger row had *before* the post-valuation repair steps renamed it: a ciqdividend cash row that the settlement-like
    rule reclassified to merger_cash carries source corporate_actions in the final ledger but is regenerated (pre phase) as a ciqdividend row.
    Without this the CSV `set` on such a row looks superseded (X3-391 MMCE 34438170 2009-10-09) and the rule reclassifies it again."""
    src = str(t.source)
    if src == "corporate_actions" and "settlement_like_reclassified" in str(t.notes):
        return "ciqdividend"
    return src


def _superseded(df: pd.DataFrame, idx: pd.Index, o: dict) -> str:
    """X3 (g) idempotency: the CSV targeted a row of source S (from the reference ledger); the regenerated frame holds, at the same key, an `ok` row
    from a different real source (e.g. a vendor marker replaced by a ciqdividend row after update_daily) -> the correction is superseded (no-op).
    The target's origin source (before post-valuation renames, `_origin_source`) counts as the same source."""
    ts = o.get("targetSource")
    if not ts or ts in ("manual_override",):
        return ""
    same = {ts, o.get("targetOrigin") or ts, "manual_override", "stitched_copy"}
    for i in idx:
        src, st = str(df.at[i, "source"]), str(df.at[i, "status"])
        if src not in same and st == "ok":
            return f"superseded_by_source:{src}"
    return ""


def _duplicate_add(df: pd.DataFrame, idx: pd.Index, o: dict) -> bool:
    """X3 (a)/(g) idempotency: an `add` whose (id, exDate, legType, child) row already exists with the same amount (cash ±1e-6, or same child & ratio ±1e-6)."""
    setd = o["set"]
    for i in idx:
        if o["legType"] == "cash":
            a, b = float(df.at[i, "cashPerShare"]) if pd.notna(df.at[i, "cashPerShare"]) else np.nan, float(setd.get("cashPerShare", np.nan))
            if np.isfinite(a) and np.isfinite(b) and abs(a - b) <= 1e-6 * max(1.0, abs(b)):
                return True
        elif o["legType"] in ("stock", "rights"):
            a, b = float(df.at[i, "childRatio"]) if pd.notna(df.at[i, "childRatio"]) else np.nan, float(setd.get("childRatio", np.nan))
            if np.isfinite(a) and np.isfinite(b) and abs(a - b) <= 1e-6 * max(1.0, abs(b)):
                return True
        else:
            a, b = float(df.at[i, "parentFactor"]), float(setd.get("parentFactor", np.nan))
            if np.isfinite(b) and abs(a - b) <= 1e-6 * max(1.0, abs(b)):
                return True
    return False


def _csv_tagged(df: pd.DataFrame, oid: str) -> pd.Index:
    if df is None or "repairOf" not in df.columns:
        return pd.Index([])
    tag = f"{CSV_TAG}:{oid}"
    return df.index[df.repairOf.map(lambda x: isinstance(x, str) and tag in x.split(";"))]


def _absent_target_reason(dropped, o: dict) -> str:
    """L1 integration (X0 fix 2): why a set/drop target is absent from the rebuilt frames — the attribution step moved it to the trading successor
    (history_appended_to_successor / history_reassigned_copy / successor_copy_of_transition_leg) or another rule removed it (dropped record)."""
    if not dropped:
        return ""
    tid, ex, leg = int(o["tradingItemId"]), pd.Timestamp(o["exDate"]), o["legType"]
    child = o.get("match_child")
    for d in dropped:
        try:
            if int(d.get("tradingItemId")) != tid or pd.Timestamp(d.get("exDate")) != ex or str(d.get("legType")) != leg:
                continue
        except (TypeError, ValueError):
            continue
        ck = d.get("childKey", -1)
        if child is not None and ck not in (None, -1) and int(ck) != int(child):
            continue
        return str(d.get("reason", ""))
    return ""


def _result_row_exists(frames: dict, o: dict) -> str:
    """Post-state check for a `set` with an absent target: is there already a row at the key the set would produce
    (tradingItemId, set.exDate|exDate, set.legType|legType, set.childTradingItemId|match_child) with the set amount (cash +-1e-6 rel, ratio +-1e-6)?
    Returns a description ('' if none)."""
    setd = o["set"]
    tid = int(o["tradingItemId"])
    ex = pd.Timestamp(setd["exDate"]) if "exDate" in setd and pd.notna(setd["exDate"]) else pd.Timestamp(o["exDate"])
    leg = setd.get("legType", o["legType"])
    child = setd["childTradingItemId"] if "childTradingItemId" in setd else o.get("match_child")
    for name, df in frames.items():
        if df is None or not len(df) or "tradingItemId" not in df.columns:
            continue
        m = (df.tradingItemId.astype(int) == tid) & (pd.to_datetime(df.exDate) == ex) & (df.legType == leg)
        if child is not None and "childTradingItemId" in df.columns:
            m &= df.childTradingItemId.map(lambda x: x is not None and pd.notna(x) and int(x) == int(child))
        for i in df.index[m]:
            if leg == "cash":
                want = setd.get("cashPerShare")
                have = df.at[i, "cashPerShare"]
                if want is None or (pd.notna(have) and np.isfinite(float(want)) and abs(float(have) - float(want)) <= 1e-6 * max(1.0, abs(float(want)))):
                    return f"{name}:{tid}@{ex.date()}/{leg} cash {have} source {df.at[i, 'source']}"
            else:
                want = setd.get("childRatio")
                have = df.at[i, "childRatio"] if "childRatio" in df.columns else np.nan
                if want is None or (pd.notna(have) and abs(float(have) - float(want)) <= 1e-6 * max(1.0, abs(float(want)))):
                    return f"{name}:{tid}@{ex.date()}/{leg} child {child} ratio {have} source {df.at[i, 'source']}"
    return ""


def apply_csv_overrides(frames: dict, overrides: list[dict], phase: str, results: dict, dropped: list | None = None) -> tuple[dict, list]:
    """phase 'pre'(평가 전, MANUAL_OVERRIDES 직후): frame ∈ {stock, merger, cash, split, lineage} 에 set/add/drop 을 MANUAL_OVERRIDES 와
    같은 방식으로 적용(source csv_override, repairOf 'csv_override:<overrideId>', repairs 로그 source csv_override).
    phase 'post'(평가·교정 규칙 뒤, finalize 직전): CSV_POST_KEYS(status/isEstimate/childValuePerOldShare/childValueSource) 를 재확정
    (value_children 이 덮어쓴 값 복원), pre 에서 못 찾은 set/drop 을 최종 프레임(gaps 포함)에서 다시 시도, csv isEstimate=true 이고 status
    미지정이면 estimated 표시, status unresolved 면 값 NaN(값을 명시하지 않은 경우). results[overrideId] 에 상태를 누적.
    반환 (frames, repairs)."""
    repairs = []
    frames = dict(frames)
    for o in overrides:
        oid = o["overrideId"]
        res = results.setdefault(oid, {"overrideId": oid, "caseId": o.get("caseId", ""), "kind": "ledger", "action": o["action"], "status": "rejected",
                                       "reason": "not_found" + (f"({o['notes_redirect']})" if o.get("notes_redirect") else ""), "affected_rows": 0,
                                       "target": f"{o['tradingItemId']}@{pd.Timestamp(o['exDate']).date()}/{o['legType']}" + (" (keeper)" if o.get("notes_redirect") else "")})
        fr = o["frame"]
        ev_txt = f"overrideId={oid} (case {o.get('caseId', '')}) | {o['evidence']}"
        tag = f"{CSV_TAG}:{oid}"
        setd = dict(o["set"])
        if phase == "pre":
            if fr not in frames or frames[fr] is None:
                if fr != "gaps":
                    res["reason"] = f"frame {fr} unavailable"
                continue
            df = frames[fr]
            idx = _csv_match(df, o)
            if not len(idx) and o["action"] != "add":
                for f2 in [f for f in ("stock", "merger", "cash", "split", "lineage") if f != fr and frames.get(f) is not None and len(frames[f])]:
                    idx2 = _csv_match(frames[f2], o)
                    if len(idx2):
                        fr, df, idx = f2, frames[f2], idx2
                        res["reason"] = f"found_in_frame:{f2}"
                        break
            if o["action"] == "drop":
                if len(idx):
                    sup = _superseded(df, idx, o)
                    if sup:
                        res.update(status="applied", reason=sup + "(no-op)", affected_rows=0, noop=True)
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", o.get("targetSource"), sup, ev_txt, CSV_TAG))
                        continue
                    for i in idx:
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", f"{df.at[i, 'eventType']}/{o['legType']} child={o.get('match_child')}", "dropped", ev_txt, CSV_TAG))
                        res.setdefault("dropped_rows", []).append(_dropped_record(df.loc[i], oid))
                    frames[fr] = df.drop(index=idx)
                    res.update(status="applied", reason="dropped", affected_rows=int(len(idx)))
                continue
            if o["action"] == "add":
                if len(idx):
                    if _duplicate_add(df, idx, o):
                        res.update(status="applied", reason="duplicate_of_existing(no-op)", affected_rows=0, noop=True)
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", "exists(same amount)", "no-op", ev_txt, CSV_TAG))
                    else:
                        res.update(status="rejected", reason=f"add: row already exists in frame {fr} with a different amount ({len(idx)}); use set")
                    continue
                row = {c: None for c in df.columns}
                row.update({"tradingItemId": int(o["tradingItemId"]), "exDate": pd.Timestamp(o["exDate"]), "legType": o["legType"], "cashPerShare": np.nan,
                            "fxToUSD": np.nan, "parentFactor": 1.0, "childRatio": np.nan, "childValueSource": "unknown", "childValuePerOldShare": np.nan,
                            "subscriptionPrice": np.nan, "exerciseRatio": np.nan, "expiryDate": pd.NaT, "payDate": pd.NaT, "recordDate": pd.NaT,
                            "announcedDate": pd.NaT, "status": "estimated" if o["csvIsEstimate"] else "ok", "isEstimate": bool(o["csvIsEstimate"]),
                            "repairOf": tag, "notes": "csv_override_add", "evidence": ev_txt, "source": "manual_override", "cashCurrency": "USD",
                            "_cashEquiv": np.nan, "_impliedFrac": np.nan, "_leg": 0})
                if o["legType"] == "cash" and "cashCurrency" not in setd:
                    row["fxToUSD"] = 1.0
                for k, v in setd.items():
                    row[k] = v
                if "csv_override_add" not in str(row.get("notes") or ""):      # L1 fix 3: a CSV `notes` value must not erase the marker (Q9 marks deliberate placements)
                    row["notes"] = (str(row["notes"]) + ";" if row.get("notes") else "") + "csv_override_add"
                if row.get("cashCurrency") == "USD" and not np.isfinite(row.get("fxToUSD", np.nan) if row.get("fxToUSD") is not None else np.nan):
                    row["fxToUSD"] = 1.0
                if "_prio" in df.columns:
                    row["_prio"] = 0.0
                frames[fr] = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
                repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", "absent", "added:" + ",".join(f"{k}={v}" for k, v in setd.items()), ev_txt, CSV_TAG))
                res.update(status="applied", reason="added", affected_rows=1)
                continue
            # set
            if not len(idx):
                continue
            sup = _superseded(df, idx, o)
            if sup:
                res.update(status="applied", reason=sup + "(no-op)", affected_rows=0, noop=True)
                repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", o.get("targetSource"), sup, ev_txt, CSV_TAG))
                continue
            for i in idx:
                for k, v in setd.items():
                    before = df.at[i, k] if k in df.columns else None
                    df.at[i, k] = v
                    if k != "notes":
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], k, before, v, ev_txt, CSV_TAG))
                df.at[i, "source"] = "manual_override"
                df.at[i, "evidence"] = (str(df.at[i, "evidence"]) + " | " + ev_txt)[:600]
                prev = df.at[i, "repairOf"] if "repairOf" in df.columns else None
                df.at[i, "repairOf"] = tag if not (isinstance(prev, str) and prev) else f"{prev};{tag}"
                df.at[i, "notes"] = (str(df.at[i, "notes"]) + ";" if df.at[i, "notes"] else "") + "csv_override"
            frames[fr] = df
            res.update(status="applied", reason="set:" + ",".join(setd.keys()), affected_rows=int(len(idx)), pre_set={k: v for k, v in setd.items() if k not in CSV_POST_KEYS and k != "notes"})
            continue
        # ---- post phase ----
        if res.get("noop") or (o["action"] == "add" and res["status"] != "applied"):
            continue
        found_fr, idx = None, pd.Index([])
        order = ([fr] if fr in frames else []) + [f for f in frames if f != fr]
        for f2 in order:
            df = frames.get(f2)
            if df is None or not len(df):
                continue
            idx = _csv_tagged(df, oid)
            if len(idx):
                found_fr = f2
                break
        if found_fr is None and o["action"] in ("set", "drop"):
            for f2 in order:
                df = frames.get(f2)
                if df is None or not len(df):
                    continue
                idx = _csv_match(df, o)
                if len(idx):
                    found_fr = f2
                    break
        if found_fr is None:
            if res["status"] != "applied":
                if o["action"] == "drop":
                    # L1 integration (X0 fix 2): the target was moved/removed by the attribution rules (X3-c hands the old id's copy to the trading
                    # successor) or is otherwise absent -> the desired end state holds: no-op, not a rejection
                    why = _absent_target_reason(dropped, o)
                    res.update(status="applied", reason=("no_op(already_moved_or_removed: " + why + ")") if why else "no_op(target_absent_in_rebuild)", affected_rows=0, noop=True)
                    repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", o.get("targetSource"), "no-op(absent)", ev_txt + (f" [absent: {why}]" if why else " [absent]"), CSV_TAG))
                else:
                    # a `set` whose target is absent but whose intended result row already exists (same key after the set, same amount; e.g. a vendor
                    # marker the rebuild no longer creates because the ciqdividend source is now found, while the stitched copy of the keeper carries
                    # the real row) -> no-op (idempotent end state); otherwise rejected not_found
                    hit = _result_row_exists(frames, o)
                    if hit:
                        res.update(status="applied", reason=f"no_op(target_absent; result_row_exists: {hit})", affected_rows=0, noop=True)
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", o.get("targetSource"), "no-op(absent, result present)", ev_txt + f" [result row: {hit}]", CSV_TAG))
                    else:
                        res["reason"] = "not_found(pre+post)"
            continue
        df = frames[found_fr]
        if o["action"] == "drop":
            if res["status"] != "applied":
                for i in idx:
                    repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", f"{df.at[i, 'eventType']}/{o['legType']}", "dropped(post)", ev_txt, CSV_TAG))
                    res.setdefault("dropped_rows", []).append(_dropped_record(df.loc[i], oid))
                frames[found_fr] = df.drop(index=idx)
                res.update(status="applied", reason="dropped(post)", affected_rows=int(len(idx)))
            continue
        # R-X0 P2-8: keys set in the pre phase that a later repair step changed (eventType reclassification, amount rule, exDate correction) are logged
        if res.get("pre_set") and len(idx):
            drift = []
            for k, v in res["pre_set"].items():
                if k not in df.columns:
                    continue
                cur = df.at[idx[0], k]
                num = isinstance(cur, (int, float, np.integer, np.floating)) and isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(cur, bool) and not isinstance(v, bool)
                same = (cur is v) or (num and (float(cur) == float(v) or (np.isnan(float(cur)) and np.isnan(float(v))))) or (not num and not isinstance(cur, float) and cur == v) \
                    or (k in CSV_DATE_KEYS and pd.notna(cur) and pd.notna(v) and pd.Timestamp(cur) == pd.Timestamp(v))
                if not same:
                    drift.append(f"{k}:{v}->{cur}")
            if drift:
                res["reason"] += ";post_drift(" + ",".join(drift) + ")"
                repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", "pre-set keys", "changed by a later repair step: " + ",".join(drift), ev_txt + " [post-valuation drift]", CSV_TAG))
                if "eventType" in res["pre_set"] and any(d.startswith("eventType:") for d in drift):
                    # X3 follow-up: the CSV classification (e.g. special_dividend instead of the settlement-like merger_cash) is deliberate -> re-assert
                    for i in idx:
                        before = df.at[i, "eventType"]
                        df.at[i, "eventType"] = res["pre_set"]["eventType"]
                        df.at[i, "notes"] = (str(df.at[i, "notes"]) + ";" if df.at[i, "notes"] else "") + f"csv_eventType_reasserted(was {before})"
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], "eventType", before, res["pre_set"]["eventType"], ev_txt + " [post re-assert]", CSV_TAG))
                    res["reason"] += ";post_reasserted(eventType)"
        late = res["status"] != "applied"          # pre 에서 못 찾아 post 에서 처음 적용(gaps 프레임 등): 모든 키 적용
        if late:
            sup = _superseded(df, idx, o)
            if sup:
                res.update(status="applied", reason=sup + "(no-op)", affected_rows=0)
                repairs.append(_rep(o["tradingItemId"], o["exDate"], "row", o.get("targetSource"), sup, ev_txt + " [post]", CSV_TAG))
                continue
        post = {k: v for k, v in setd.items() if late or k in CSV_POST_KEYS}
        if "status" not in setd and o["csvIsEstimate"]:
            post["status"] = "estimated"
            post.setdefault("isEstimate", True)
        if post.get("status") == "unresolved":
            for k in ("childValuePerOldShare", "cashPerShare") if o["legType"] != "quantity" else ():
                if k not in setd and k in df.columns:
                    post[k] = np.nan
            post["isEstimate"] = False
            if "childValueSource" not in setd and "childValueSource" in df.columns:
                post["childValueSource"] = "unknown"           # ③④ 표시는 값이 있는 추정에만(Q2 P10_③④_without_isEstimate)
        if post.get("status") == "ok" and "isEstimate" not in setd:
            post["isEstimate"] = False
        if post.get("status") == "estimated":
            post.setdefault("isEstimate", True)
        n_changed = 0
        for i in idx:
            if post.get("status") == "estimated" and str(df.at[i, "status"]) == "unresolved" and "status" not in setd:
                # 값이 없어 unresolved 인 행은 csv isEstimate 만으로 estimated 로 올리지 않는다(값 없는 추정 금지)
                val_ok = np.isfinite(float(df.at[i, "childValuePerOldShare"])) if o["legType"] in ("stock", "rights") and "childValuePerOldShare" in df.columns else True
                if o["legType"] == "cash" and "cashPerShare" in df.columns:
                    val_ok = np.isfinite(float(df.at[i, "cashPerShare"]))
                if not val_ok:
                    res["reason"] = (res["reason"] + ";post:kept_unresolved(no_value)") if res["status"] == "applied" else "kept_unresolved(no_value)"
                    continue
            for k, v in post.items():
                before = df.at[i, k] if k in df.columns else None
                same = (before is v) or (isinstance(before, float) and isinstance(v, float) and (before == v or (np.isnan(before) and np.isnan(v)))) \
                    or (not isinstance(before, float) and before == v)
                if same:
                    continue
                if k == "childValuePerOldShare" and "childValueSource" in df.columns and not late:
                    # L1 3rd build (X0 fix): the builder valued this leg from the child's close (P10 ① etc.) -> the CSV value is a check, not the
                    # value: within CSV_VALUE_REL_TOL the exact builder value stays (a rounded CSV value like 10.192779 vs 0.239943 x 42.48 =
                    # 10.19277864 would break the returns oracle at 1e-9); beyond it the CSV value wins and the conflict is logged
                    src_now = str(df.at[i, "childValueSource"])
                    src_after = str(setd.get("childValueSource", src_now))
                    if src_now in BUILDER_VALUE_SOURCES and src_after in BUILDER_VALUE_SOURCES and isinstance(before, (float, np.floating)) and np.isfinite(before) \
                            and isinstance(v, (float, np.floating, int)) and np.isfinite(float(v)):
                        if abs(float(before) - float(v)) <= CSV_VALUE_REL_TOL * max(1.0, abs(float(v))):
                            res["reason"] += f";csv_value_check_ok(childValuePerOldShare csv {float(v):.8g} vs builder {float(before):.10g}, builder kept)"
                            continue
                        res["reason"] += f";csv_value_conflict(childValuePerOldShare csv {float(v):.8g} vs builder {float(before):.10g} ({src_now}), csv adopted)"
                        repairs.append(_rep(o["tradingItemId"], o["exDate"], "childValuePerOldShare", before, v, ev_txt + f" [post-valuation: CSV value conflicts with the builder's {src_now} value beyond {CSV_VALUE_REL_TOL:g} relative — CSV adopted]", CSV_TAG))
                        df.at[i, k] = v
                        n_changed += 1
                        continue
                df.at[i, k] = v
                n_changed += 1
                if k != "notes":
                    repairs.append(_rep(o["tradingItemId"], o["exDate"], k, before, v, ev_txt + " [post-valuation]", CSV_TAG))
            if late:
                df.at[i, "evidence"] = (str(df.at[i, "evidence"]) + " | " + ev_txt)[:600]
                prev = df.at[i, "repairOf"] if "repairOf" in df.columns else None
                df.at[i, "repairOf"] = tag if not (isinstance(prev, str) and prev) else f"{prev};{tag}"
                df.at[i, "notes"] = (str(df.at[i, "notes"]) + ";" if df.at[i, "notes"] else "") + "csv_override(post)"
                if str(df.at[i, "source"]) != "vendor_implied":
                    df.at[i, "source"] = "manual_override"
            # 벤더 스텝 마커(source vendor_implied, notes vendor_only_step): status 규약(Q10) — unresolved/estimated 는 source vendor_implied 유지,
            # ok 로 승격되면 확인된 사건이므로 source manual_override + notes 'vendor_only_step' 표시를 지움(마커가 아니라 실제 행)
            if str(df.at[i, "source"]) == "vendor_implied" and str(df.at[i, "status"]) == "ok":
                df.at[i, "source"] = "manual_override"
                df.at[i, "notes"] = str(df.at[i, "notes"]).replace("vendor_only_step", "vendor_step_confirmed_by_csv_override")
                if str(df.at[i, "eventType"]) == "other" and o["legType"] == "cash" and "eventType" not in setd:
                    df.at[i, "eventType"] = "cash_dividend"
                    repairs.append(_rep(o["tradingItemId"], o["exDate"], "eventType", "other", "cash_dividend", ev_txt + " [marker promoted to ok]", CSV_TAG))
                if "childValueSource" not in setd and "childValueSource" in df.columns and str(df.at[i, "childValueSource"]) == "vendor_implied":
                    df.at[i, "childValueSource"] = "unknown"
        frames[found_fr] = df
        if late:
            res.update(status="applied", reason="set(post):" + ",".join(setd.keys()), affected_rows=int(len(idx)))
        elif n_changed:
            res["reason"] += f";post_reasserted={n_changed}"
    return frames, repairs
