# 현재 실행 중인 ALL universe 점검 — 2026-09-21

실행 설정: `cpp-engine/examples/datasets/compare_v2_all.yaml`. 가격 캐시는 `cache_v2_cash_1d`, 유니버스는 `TopUniverseV2/ALL.parquet`이다. 최신 `cache_v21` 유니버스를 사용하고 있지 않다. 이번 점검은 읽기 전용이며 마스크·엔진 설정은 변경하지 않았다.

## 실제 파일/API 대조

| 항목 | 수 |
|---|---:|
| v2 가격/마스크 파일 전체 열 | 22,871 |
| 2000년 이후 한 번이라도 ALL 편입 | 9,300 |
| 엔진 로딩 종목 합집합(2009-01-02 warmup부터) | 7,065 |
| 실제 거래기간(2010-01-04 이후) 편입 합집합 | 6,965 |
| warmup에서만 편입된 로딩 종목 | 100 |
| 마지막 날짜 2026-09-11 편입 | 2,819 |
| 2010년 이후 일별 최소 / 중앙값 / 최대 | 1,755 / 2,243 / 3,093 |

API의 7,065개 심볼과 파일에서 warmup 포함 편입 합집합으로 계산한 심볼이 정확히 일치했다. 매일 7,065개가 편입되는 것이 아니다. 현재 파일은 2026-09-14 생성, 마지막 거래일 2026-09-11인 정적 빌드다.

## 편입 규칙

`cpp-engine/tools/data/build_cache_v2.py:97, 761, 772`를 확인했다.

- 후보군: v1 현재 거래소 규칙 또는 과거 NYSE/AMEX/Nasdaq 이력이 있는 USD 보통주, 기간 내 종가 존재.
- 21일 시가총액 중앙값 > $10M.
- **100일 평균 종가 × 100일 평균 거래량 > $3M**. `mean(close * volume)`이나 당일 거래대금 조건과는 다르다.
- currentPrimary 참: 스냅샷의 primaryFlag/primaryFlagTi 모두 1, 회사명이 대소문자 구분 `Trust|ETF`와 일치하지 않음.
- 현재 시가총액 양수.
- sticky: 조건 공백을 최대 10거래일 이어 준 뒤 연속 60일 **초과**(61번째)부터 편입. 최종적으로 당일 시가총액 양수 재검사.
- ALL은 TopN 순위 제한 없음. TopN 마스크는 별도로 존재한다.

currentPrimary는 당시 날짜별(PIT) primary 이력이 아니라 **현재 스냅샷 정보를 과거에 적용**한다. 현재 TopUniverseV2는 완전한 PIT 유니버스가 아니다.

## 명시적 제외

### TokensToRemove

v1 METADATA.yml과 v2 MANIFEST의 46개 ID 목록은 정확히 같았다. v2 캐시에 45개가 존재하고 1개(27860642)는 캐시에 없다. 존재하는 45개 전부 ALL의 어떤 날짜에도 편입되지 않았다.

같은 규칙에서 수동 제외만 제거해 재계산한 결과, 45개 모두 2010년 이후 편입 가능한 날짜가 있고, 마지막 날짜에는 40개가 편입 조건을 만족한다. 예: GS, JPM, FIS, HOOD, CRWV, BKNG, BRK.A, FDX, HON, SPGI. 제외 사유는 목록에 기록되어 있지 않아 추정하지 않았다.

- [전체 수동 제외 목록](manual_blacklist.csv)
- [수동 제외가 없었을 때 편입 조건 계산](manual_blacklist_counterfactual.csv)

### 추가 펀드/거래소 필터

`NOT inV1Rule AND (companyTypeId == 13 OR exchangeId in {117,467,358})` 후보 706개를 제거한다. 모두 현재 ALL 편입 이력이 0임을 확인했다. 빌드 기록에서 이 중 필터 적용 전 ALL 편입 이력이 있었던 것은 15개다(전체 2000년 이후 기간). 예: DBC, DJP, DBV, USL, GSP.

현재 v1 규칙 내부의 종목은 이 추가 필터 대상이 아니므로 모든 ETF/펀드를 일괄 배제한 마스크로 설명하면 안 된다. 거래소 규칙에는 DSL/Downey Financial, AX/Archipelago 같은 오래된 사업회사도 포함된다. 기존 AUDIT는 해당 사례를 명시한다.

- [추가 제외 후보 706개](fund_screen_exclusions.csv)
- [과거 ALL 편입 이력이 있던 15개](fund_screen_affected_ALL.csv)

## 현재 v1 파일과 같은 날짜의 비교

현재 v1 파일은 2026-09-21까지 갱신된 파일이므로, v2의 마지막 날짜인 **2026-09-11** 및 공통 2010년 이후 4,198일로 비교했다. 최신 공통 날짜에서 v1 2,735개, v2 2,819개이며 v1-only 5개 / v2-only 89개다. 이는 동일한 종목 ID 기준 비교이며, 회사·증권 lineage 단위 비교는 아니다.

| ID | v1에만 있는 종목 | 현재 v2 미충족 조건 |
|---|---|---|
| 1854830766 | LZMH | 거래대금 기준 0.718M < 3M |
| 1856785919 | NAKA | 거래대금 기준 1.424M < 3M |
| 1979399313 | FAC | sticky 연속 60일: 60일 초과 조건 미충족 |
| 1990989904 | HQ | sticky 연속 59일: 60일 초과 조건 미충족 |
| 553814981 | LNAI | 거래대금 기준 2.174M < 3M |

- [최신 v1-only 진단값](latest_v1_only_diagnostics.csv)
- [최신 v2-only 종목](latest_v2_only.csv)
- [공통 기간 중 v1에만 편입된 날짜가 있는 종목](v1_members_lost_dates_in_v2.csv)

## 엔진 적용

`parquet_reader.cpp:1017`은 mask=1만 편입하고 0/NaN/해당 날짜 없음은 미편입 처리한다. warmup을 포함한 로딩 기간 중 한 번이라도 mask=1인 종목만 로딩한다. `simulator.cpp:899`에서 mask와 유효 신호를 교집합으로 적용한 목표 비중을 만들며 D1은 전날 목표를 실행한다. 정상 종가가 있으면 목표 0으로 청산하지만 `carry` 상태에서 종가가 없으면 기존 포지션이 동결될 수 있으므로 탈락 당일 즉시 청산을 보장하지 않는다.

## 원시 점검 결과

- [요약 JSON](summary.json)
- [종목별 전체 상태](membership_by_instrument.csv)
- [최신 ALL 편입 종목](latest_ALL_members.csv)
- [최신 ALL 미편입 종목](latest_not_in_ALL.csv)
- [날짜별 편입 수](daily_ALL_counts.csv)

근거: 실행 프로세스 명령행, `/api/config`, 현재 mask parquet·master·MANIFEST·fund_exclusions, v1 live mask·METADATA, builder·loader·simulator 소스. 수동 제외 반사실 계산은 45개 컬럼에 대해 동일 rolling/sticky 규칙을 재현했으며 파일을 발행하거나 적용하지 않았다.
