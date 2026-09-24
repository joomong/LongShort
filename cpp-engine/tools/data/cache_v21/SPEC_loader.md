# cache_v2.1 로더 스펙 (C++ 팀용) — 파이썬 참조 구현 `loader.py` 기준

작성 2026-09-16 (T4 agent-loader). 이 문서는 BRIEF §3 계약(파일·컬럼·타입)을 **소비자 관점**에서 다시 쓰고, 참조 로더가 고정한 산식·연산 순서·경계 규칙·parity 절차를 적는다. 계약 자체가 바뀌면 이 문서가 아니라 BRIEF §3 가 우선한다.

## 0. 범위

| 항목 | 내용 |
|---|---|
| 입력 | `base_data.parquet`(long), `factors/{split,dividend}.parquet`(구간 계수), `derived/returns_total.parquet`(long), `derived/marketcap.parquet`(long), `master/{instruments,lifecycle,lineage,tradable_exclusions,classification_history}.parquet`, `universe/{membership,membership_v1rule}.parquet` |
| 출력 | 고정 tradingItemId 순서의 wide 패널: 가격 6필드 × 3기준, marketCap_USD·sharesOutstanding, 총수익률, 계수, 마스크(tradable·priced·universe·universe_v1rule), 그룹 코드 패널(sector·industry; broadcast_current/pit) |
| **엔진 입력 패널 = lifecycle 절단본** | 엔진(및 T6 events)에 주는 가격·수익률 패널은 `lifecycle.lastTradeDate` **이후 셀을 NaN 으로 절단**한 것이다(`cut='lifecycle'`, get_panel·export_wide 기본). events 는 절단 패널 기준으로 생성됐고 리드 독립검사가 절단본에서만 통과함(미절단 시 ③b 28·④b 92 위반). firstTradeDate 이전(이어붙인 과거)은 워밍업용으로 유지, tradable_exclusions 구간은 절단하지 않고 마스크로만 처리. 미절단(연구용)은 `cut=None` / CLI `--no-cut` |
| 참조 구현 | `cpp-engine/tools/data/cache_v21/loader.py` `CacheV21`, 테스트 `tests/test_loader.py`(22 케이스), fixture `tests/make_fixture.py` |
| 비범위 | events 정산 계산(엔진), universe 생성(T5) |

## 1. 파일·컬럼·타입 (로더가 읽는 것만)

| 파일 | 컬럼 | 타입 | 로더 사용 |
|---|---|---|---|
| `base_data.parquet` | date | timestamp[ns] 자정 | 달력 축. 자정이 아니면 오류 |
| | tradingItemId | int64 | 열 축 |
| | open, high, low, close, vwap | float32 (null=결측) | raw 가격. 다른 타입이면 경고 후 float32 로 캐스팅 |
| | volume | float64 | raw 주수 |
| | source, qualityFlag | int8 | 진단용 패널(int16, 없는 셀 −1) |
| `factors/split.parquet` | tradingItemId, validFrom, validTo, factor | int64, ts[ns], ts[ns](NaT=열림), float64 | splitFactor 구간 |
| `factors/dividend.parquet` | 〃 | 〃 | divAdjFactor 구간 |
| `derived/returns_total.parquet` | date, tradingItemId, ret, prevDate | ts[ns], int64, float64, ts[ns] | 총수익률 wide(ret), 갭 진단(prevDate) |
| `derived/marketcap.parquet` | date, tradingItemId, marketCap_USD, sharesOutstanding | ts[ns], int64, float64, float64 | `get_panel('marketCap_USD')`·`('sharesOutstanding')`, raw 만. 달력 밖 날짜·패널 밖 id 행은 버리고 개수 기록 |
| `master/lifecycle.parquet` | tradingItemId, firstTradeDate, lastTradeDate, firstPriceDate, firstTradeSource, ciqLastCloseDate | int64, ts[ns]×2, ts[ns], str, ts[ns] | tradable 은 firstTradeDate~lastTradeDate, priced 는 firstPriceDate~ciqLastCloseDate. NaT = 열림 |
| `master/tradable_exclusions.parquet` | tradingItemId, fromDate, toDate(포함), reason, preferredItemId | int64, ts[ns], ts[ns], str, int64 | tradable 에서 (id, 구간) False. **파일 없으면 경고 없이 통과** |
| `master/instruments.parquet` | tradingItemId | int64 | `ids='instruments'` 열 순서 옵션 |
| `master/lineage.parquet` | oldId, newId, relation, effectiveDate | int64, int64, str, ts[ns] | 옵션 `apply_lineage`(rename/relist 의 newId 를 effectiveDate 전까지 False) |
| `universe/membership.parquet` | universe, tradingItemId, fromDate, toDate | str, int64, ts[ns], ts[ns] | `universe:<name>` 마스크(toDate **포함**). 기본 위치 `<root>/universe/`, `universe_root=` 로 T5 스테이지 지정 가능 |
| `universe/membership_v1rule.parquet` | 〃 | 〃 | `universe_v1rule:<name>` 마스크(v1 규칙 변형) |
| `master/classification_history.parquet` | tradingItemId, sector, simpleIndustryDescription, effectiveFrom, effectiveTo(NaT=현재), observedAt, source, historyQuality | int64, str, str, ts[ns]×3, str, str | 그룹 코드 패널(§6-1). 라벨 null/빈 문자열 = 미분류 |

정렬 전제: base_data 는 (tradingItemId, date). 정렬돼 있지 않아도 결과는 같지만(느림) 경고한다. (id, date) 중복 행은 오류.

## 2. 축(달력·열 순서) 규칙

| 규칙 | 정의 |
|---|---|
| 달력 `dates()` | 요청 날짜 범위 안에서 base_data 에 **행이 하나라도 있는 날짜**의 오름차순 집합. id 부분집합을 요청해도 달력은 전체 행 기준(부분집합에 행이 없는 날도 유지) |
| 열 순서 `ids()` 기본 | base_data 에 등장하는 tradingItemId 의 **int64 오름차순** |
| 열 순서 옵션 | `ids='instruments'` → `master/instruments.tradingItemId` 파일 순서. `ids=[...]` → 준 순서 그대로(중복 금지). base_data 에 없는 id 는 전부 NaN 열, 목록 밖 id 의 행은 버림 |
| C++ 대응 | C++ `DataPanel` 은 심볼을 문자열 키(`std::map`)로 들고 있으므로 열 순서는 parity 비교 대상이 아님. 비교는 (date, symbol) 셀 단위 |
| 행 인덱스 | `ri = 달력에서 date 의 위치`, `ci = 열 순서에서 id 의 위치`. 참조 구현은 date→일 수(ns÷86400e9) 로 바꿔 LUT 로, id 는 정렬 후 이진탐색으로 매핑 |

## 3. 구간 계수 lookup 알고리즘

계약: 같은 id 의 구간은 `[validFrom, validTo)` 반개구간, validTo NaT = 열림, 1900-01-01 부터 빈틈·겹침 없이 이어짐. 모든 (id, date) 에 정확히 1개 구간.

```
load(table):
  day(x) = x_ns / 86_400e9                      # 자정 timestamp → 정수 일 수 (1900-01-01 = −25567)
  to_day = validTo is NaT ? +INF : day(validTo)
  rows ← 패널 열 순서에 없는 id 제거
  sort rows by (col_index, from_day)
  key[r] = (col_index[r] << 32) | (from_day[r] + 2^20)   # 2^20 오프셋으로 음수 일 수를 양수화, 사전식 정렬 보존
  validate: 같은 id 의 연속 행에 대해 from_day[r+1] == to_day[r] (== 아니면 빈틈(>) / 겹침(<)), from_day < to_day, factor > 0 유한
            첫 행 from_day == day(1900-01-01), 마지막 행 to_day == +INF  (계약 확인용 카운트)

lookup(col, day):
  k = (col << 32) | (day + 2^20)
  pos = upper_bound(key, k) − 1                  # key ≤ k 인 마지막 구간
  valid = pos ≥ 0 && col_index[pos] == col && day < to_day[pos]
  valid ? factor[pos] : ERROR(FactorGap)          # 계약상 발생하면 안 됨 → 로드 실패
```

| 성질 | 값 |
|---|---|
| 복잡도 | O((행 수 + 구간 수)·log 구간 수). 48M 행 × 2 계수 ≈ 1.2초(참조 구현, numpy) |
| 대체 구현 | id 별 구간 배열에서 `upper_bound(validFrom, date) − 1` 뒤 `date < validTo` 확인(`merge_asof` 와 동치). 결과는 비트 동일해야 함 |
| 비거래일 | 주말·휴장일 날짜도 lookup 가능(dense 옵션). 엔진은 base 행이 있는 셀만 필요 |
| 실데이터 확인 | T2 `factors/*.parquet`(2026-09-16 15:23, 38,118 id): split 65,252 구간·dividend 750,200 구간 모두 빈틈 0·겹침 0·전 id 1900 시작·마지막 열림(참조 구현 `validate_factors` 통과) |

## 4. 필드 산식 (연산 순서·dtype 고정)

| 필드·기준 | 산식 | dtype | 비고 |
|---|---|---|---|
| price raw | `raw` | float32 | open/high/low/close/vwap. 저장값 그대로 |
| marketCap_USD, sharesOutstanding | `derived/marketcap` 값 그대로 | float64 | basis 인자는 raw 만(시총은 기준 무관, 주수는 as-reported). 분할조정 주수가 필요하면 `sharesOutstanding × splitFactor` |
| price split_adj | `float32( double(raw) / sf )` | float32 | sf = splitFactor(그 날 구간). 신호용 기본 |
| price div_adj | `float32( (double(raw) * df) / sf )` | float32 | df = divAdjFactor. **곱 먼저, 나눗셈 다음**(순서를 바꾸면 float32 로 캐스팅 후 다른 비트가 나올 수 있음 — fixture 2,562 셀 중 41 셀이 float32 경로와 달랐음) |
| volume raw | `raw_volume` | float64 | 실제 거래 주수 |
| volume split_adj | `double(raw_volume) * sf` | float64 | 분할조정 주수(가격과 반대 방향) |
| volume div_adj | **정의 없음 → 오류** | | 배당 계수는 주수를 바꾸지 않음. 필요하면 split_adj 를 쓴다 |
| 달러거래대금 | `raw close × raw volume` | | 기준 무관 |
| returns_total | 파일 `ret` 그대로 | float64 | §5 |
| splitFactor / divAdjFactor 패널 | lookup 값 | float64 | 기본은 base 행이 있는 셀만, dense 옵션은 모든 (date, id) |

모든 조정 연산은 IEEE double 로 하고 마지막에 float 로 한 번 캐스팅한다(C++ `static_cast<float>(static_cast<double>(raw) * df / sf)` 에서 `*` 가 `/` 보다 먼저 평가되도록 괄호를 명시).

**절단(cut)**: 위 값 계산 뒤 마지막 단계로 `date > lifecycle.lastTradeDate(id)` 인 셀을 NaN(flag 필드는 −1) 으로 만든다(`cut='lifecycle'` 기본). lastTradeDate 당일은 유지, NaT(활성)·lifecycle 에 없는 id 는 절단 없음(개수 기록), lifecycle 파일이 없으면 경고 후 미절단. 마스크 계산(tradable·priced)은 항상 미절단 close 를 쓴다(priced 는 꼬리를 True 로 둬야 함). 실데이터: 15,622 id 절단, 제거된 유효 셀 close 10,118·open 10,076·high/low 10,114·vwap 8,241·volume 10,079·returns 10,115(총 68,857) — 승계사 가격 꼬리.

## 5. returns_total 사용 규칙

| 규칙 | 내용 |
|---|---|
| 정의 | `ret_t = adj_t / adj_prev − 1`, `adj = double(close_raw) / sf × df`, prev = 같은 id 의 **직전 관측 행**(close notna 인 행; 무행 갭·close null 행은 건너뜀). 첫 관측 행 NaN. 규약명 "vendor-factor total return" |
| 배당락일 값 | `1 + ret = (1 + ret_price) / (1 − D/P_prev)` = `P_t / (P_prev − D) − 1`. 현금식 `(P_t + D)/P_prev − 1` 과는 2차항(`g/(1−x) − g − x`, x=D/P_prev, g=P_t/P_prev)만큼 다름. 엔진은 계약 정의를 쓰고 현금식으로 바꾸지 않는다 |
| 금지 | close 비율로 재계산 금지: raw 비율은 분할일에 −50%/−75%, split_adj 비율은 배당 누락. 재계산은 `compute_returns()`(대조용) 만 |
| 결측 | 행이 없는 (date, id) 는 NaN. 엔진 `carry` 규약(직전 관측 대비 인식)과 정의가 같으므로 갭 뒤 첫 값이 갭 전체 수익률 |
| lifecycle | 파일은 lastTradeDate 이후 꼬리에도 값이 있음. `get_returns()` 기본은 파일 그대로(미절단), `get_returns(cut='lifecycle')`·export_wide 기본은 절단본(엔진 입력) |
| 대조 | `check_returns()` : 파일 vs 재계산 셀 단위 최대차·개수. fixture 0 / 0 |

## 6. 마스크 정의

| 마스크 | 정의 | 경계 |
|---|---|---|
| `tradable` | `firstTradeDate ≤ d ≤ lastTradeDate` (lifecycle, 양끝 포함, NaT=열림) `∧ close(raw) notna` `∧ ¬exclusions` | 승계사 가격 꼬리(lastTradeDate 이후 close 존재)는 False. close null 행(qualityFlag 3)은 False. `tradable_exclusions` 의 `[fromDate, toDate]`(포함) 구간 False — T1 최종본 10 쌍(같은 securityId 이중 트레이딩아이템) → 실데이터 7,547 셀 |
| `priced` | `firstPriceDate ≤ d ≤ ciqLastCloseDate` (양끝 포함) `∧ close(raw) notna` | 벤더 가격이 있는 모든 구간: 이어붙인 과거(lineage)·승계사 가격 꼬리 포함. 신호 워밍업용. `tradable ⊆ priced`. 컬럼이 없는 옛 lifecycle 이면 firstTradeDate/lastTradeDate 로 대체하고 경고 |
| `universe:<name>` | membership 의 `[fromDate, toDate]` **폐구간** 합집합 | 같은 id 의 복수 구간 허용 |
| `universe_v1rule:<name>` | membership_v1rule 의 같은 규칙 | v1 규칙 변형(T5) |
| 교집합 | `get_mask('tradable&universe:Top500')` = 두 마스크의 AND. `tradable_universe(name, v1rule=)` 헬퍼 | |
| lifecycle 에 없는 id | 기본 `open`(경고 + 열림 처리 = close 있으면 True), 옵션 `exclude`(False)/`error` | **위험**: 없는 id 가 소멸 종목이면 꼬리가 True 로 남음. publish 게이트에서 `missing = 0` 을 요구할 것(§11 제안) |
| 옛/새 id 겹침 | T1 최종본은 이어붙인 802 종목의 firstTradeDate 를 lineage effectiveDate 로 올렸으므로(firstTradeSource='lineage') lifecycle 만으로 동시 tradable 0. 옵션 `apply_lineage=True` 는 같은 자름을 lineage 파일로 한 번 더 적용(무해) | 옛 id 는 자기 lastTradeDate 로 잘림. priced 는 새 id 의 이어붙인 과거를 True 로 둠 |

C++: 마스크 파일은 v1 과 같은 bool wide(`__index_level_0__` + 심볼 열)로 export 되므로 기존 `universe:` 로더가 그대로 읽는다(값 0/1/null 만 허용).

### 6-1. 그룹 코드 패널 (`<neutralize group="sector|industry"/>` 용, `build_group_panels.py` 형식 그대로)

| 항목 | 규칙 |
|---|---|
| 원천 | `master/classification_history.parquet`(T1). 기존 도구는 현재 마스터 라벨만 썼음 |
| 코드 체계 | sector 1..11 = v1 `sectorIx` 순서 고정(Energy, Information Technology, Industrials, Materials, Financials, Consumer Discretionary, Consumer Staples, Real Estate, Communication Services, Health Care, Utilities; 목록 밖 라벨은 오류), industry = 이력 전체 라벨 정렬 순 1..K(실데이터 K=74). 미분류(라벨 null) NaN. 한 industry 가 두 sector 에 걸치면 오류 |
| `get_group_panel(kind, mode)` | wide **float32**, 축 = 가격 패널과 동일, close 유무로 마스킹하지 않음 |
| mode `broadcast_current`(기본) | id 별 최신 행(effectiveTo NaT, 없으면 최대 effectiveFrom)의 라벨을 전 기간 방송 = v1·기존 도구와 동일 의미(look-ahead 있음) |
| mode `pit` | 날짜 d 에 `effectiveFrom ≤ d ≤ effectiveTo`(포함; NaT=열림) 인 행의 라벨. lookup = 그 id 에서 effectiveFrom ≤ d 인 마지막 행 + `d ≤ effectiveTo` 확인(연속 구간에선 반개구간 해석과 동일 결과). 관측 이전(첫 effectiveFrom 전)·`current_only`(effectiveFrom = observedAt 2026-09-16) 행은 패널 기간 전부 NaN |
| 엔진 기동 조건 | "종가 있는 bar 에 유한 코드 0 개면 실패": 실데이터 pit 는 6,325/6,731 bar 가 코드 0 개(관측 이력이 2025-02-03 부터) → **export 기본은 broadcast_current**, pit 는 옵션(`groups_mode='pit'`) |
| export | `export_wide(out, groups=True)` 또는 `export_group_panels(dir, mode)` → `<out>/groups/{sector,industry}.parquet` + `{sectorIx,industryIx}.parquet`(0 행 None) + `groups_manifest.json`. 그룹 패널은 절단하지 않음(정적 방송 규약, 엔진이 asof 로 읽음). 쓰기 옵션 = 기존 도구와 동일(snappy·dictionary·통계 없음·parquet 2.6, 결정적 → 같은 입력 sha256 동일). 엔진 YAML: `base: {cache: <out>, groups: <out>/groups}` |
| 실데이터(T1 최종·T2 축, 6,731×22,873) | broadcast: 22,347 id 분류(526 라벨 null), close 셀의 99.69% 분류, 코드 0 개 bar 0; pit: 6,952 id·close 셀 5.25%, 첫 분류 bar 2025-02-03. 생성 sector/industry 각 0.2~1.1 s, export 5.5 s, 파일 10.8 MB 각 |
| 실행 확인 | 기존 빌드(2026-09-15) 엔진에 `datasets:` 로 `groups/sector.parquet`·`industry.parquet`(alignment asof) + `marketCap_USD.parquet`(cap) 을 얹어 기동 → `Data loaded: T=501 N=6`, columns 9 개(cap·sector·industry 포함). `base:` 문법 자체는 그 빌드가 아직 몰라(“datasets must be a non-empty sequence”) 현행 소스 빌드에서 재확인 필요 |

## 7. events(raw 단위) 사용법

| 항목 | 규칙 |
|---|---|
| 단위 | v2.1 events 는 raw(as-quoted) 단위: `cashPerShare`, `successorRatio` 를 **그대로** 쓴다. 프로토타입의 `*Cache` 환산(k 앵커)은 raw 기준에서 항등(k=1) |
| 정산식(엔진) | `R = (cashPerShare + successorRatio · close_raw_succ(t)) / mark_raw − 1`, `mark_raw` = 대상의 마지막 관측 raw close, t = 정산 bar |
| 가드 | 이벤트 파일의 `successorClose`(정산 bar 승계사 raw close) 와 패널의 raw close 를 상대오차 1e-4 로 대조. 패널이 split_adj 라면 가드 대상도 split_adj 로 환산해 대조(아래) |
| split_adj 패널에서 쓸 때 | `cash_adj = cashPerShare / sf_T(mark 날짜)`, `ratio_adj = successorRatio × sf_S(t) / sf_T(mark 날짜)` (sf_T = 대상 splitFactor, sf_S = 승계사 splitFactor). 점 lookup 은 `factor_at('split', ids, dates)` |
| 권장 | 엔진 정산은 raw 패널 + raw 조건으로 계산하고 보유 손익은 returns_total 로 — 환산이 전혀 필요 없음 |

## 8. 경계 사례 (fixture 로 고정, `tests/test_loader.py`)

| 사례 | fixture | 기대 동작 | 테스트 |
|---|---|---|---|
| 분할일 | 101: 2022-07-01 2:1, 2023-03-15 3:2 | raw 는 1/비율 점프, split_adj 는 진짜 수익률로 연속. 첫 구간 이전(1900~) 계수 3.0 = csf×lsf | `test_split_adj_continuous_raw_jumps` |
| 배당락 | 102: 0.50×4 + 특별 3.00 | 배당락 첫 거래일 `1+ret = (1+ret_price)/(1−D/P_prev)`; 마지막 구간 1.0; 첫 구간 이전 = 첫 구간값 | `test_div_adj_exdate_total_return` |
| 배당락이 휴장일 | 102: ex 2023-01-02(휴장) | 구간은 ex 당일부터 시작, 효과는 다음 거래일 2023-01-03 수익률에, P_prev = 2022-12-30 | 〃 |
| 무행 갭 | 103: 2022-08-08~12 | 패널·수익률 NaN, 재개일 ret = 갭 직전 관측 종가 대비, prevDate = 갭 직전일 | `test_gap_and_null_close`, `test_returns_total_equals_manual` |
| close null 행 | 103: 2023-02-01 (qualityFlag 3) | 다른 필드는 값 있음, close NaN, returns 파일에 행 없음, 다음 날 ret 은 그 행을 건너뜀, tradable False | `test_gap_and_null_close` |
| 승계사 가격 꼬리 | 104: lastTradeDate 2023-06-15 + 10일 | base_data·returns 에 값 존재, tradable False | `test_tradable_tail_cut_by_lifecycle` |
| 재상장(옛/새 id) | 105→106, effectiveDate 2022-10-03 | 새 id 에 옛 이력이 그대로 있음(동일 값). 기본 마스크 둘 다 True, 옛 id 는 2022-09-30 이후 False. `apply_lineage` 로 새 id 는 10-03 부터 | `test_relist_overlap_lifecycle_and_lineage_option` |
| universe 경계 | 102: 두 구간 | fromDate·toDate 당일 True, 그 전후 False | `test_universe_membership_bounds` |
| 반개구간 | 101 | validFrom 당일 새 값, 전날 옛 값, validTo 당일은 다음 구간 값 | `test_halfopen_interval_boundaries` |
| 빈틈·겹침·첫 구간 누락 | variant fixture | 빈틈/누락 → FactorGapError, 겹침 → FactorOverlapError. raw 패널은 계수 없이 가능 | `test_factor_gap_and_overlap_fixtures_raise` |
| 중복 행·달력 밖 수익률 | 조작 fixture | ContractError | `test_duplicate_rows_rejected` |
| 스키마 편차 | close double | 경고 + `diagnostics['schema_notes']` | `test_contract_schema` |
| 부분 로딩 | 9 row group 파일, id 부분집합·비거래일 경계 범위 | 전체 로딩의 조각과 비트 동일, row group 프루닝은 결과에 영향 없음 | `test_row_group_pruning_consistency` |

## 9. 성능 목표·실측

### 9-1. 실데이터 (T2 최종 `base_data.parquet` 48,020,121 행·867MB zstd·39 row group, `returns_total` 48,007,027 행·383MB, T1 최종 master; gqdata, pyarrow 8 스레드, 새 프로세스, 2026-09-16 16:40)

패널 6,731 일(2000-01-03 ~ 2026-09-15) × 22,873 id.

| 단계 | 시간 | 누적 peak RSS | 비고 |
|---|---|---|---|
| 좌표(달력·id·행/열 인덱스) | 1.5 s | 1.65 GB | date·id 컬럼 1 회 |
| 계수 검증(split 65k·dividend 750k 구간) | 0.2 s | | 겹침 0·빈틈 0 |
| close raw | 0.5 s | 1.65 GB | |
| close split_adj | 2.9 s | 3.4 GB | split lookup 48M 행 1.9 s 포함 |
| close div_adj | 1.9 s | 4.8 GB | dividend lookup 1.2 s 포함 |
| returns_total wide | 2.7 s | 4.8 GB | row group 스트리밍 |
| check_returns(재계산 대조) | 5.1 s | 8.3 GB | 47,984,154 셀 최대차 9.1e-13, 1e-9 초과 0, 한쪽만 있는 셀 0 |
| tradable(+exclusions 10 행 → 7,547 셀) | 0.4 s | | lifecycle 누락 id 0 |
| priced | 0.2 s | | |
| open/high/low/vwap/volume split_adj | 0.3~0.6 s 각 | | export 안에서 read+scatter |
| close split_adj lifecycle 절단 | 0.7 s | | 15,622 id, 유효 셀 10,118 제거 |
| export_wide 6 필드 + returns + tradable/priced + groups (절단본) | 49.2 s | 10.4 GB(release=True) | 파일 1,354 MB: 가격 149~153 MB 각, volume 171, returns 350, mask 28.5×2, groups 10.8×2. 절단 셀 합계 68,857 |
| 부분 로딩 3 id × 2014~2021 (AAPL·COST·EBAY) | 2.1 s | | row group 프루닝 |

BRIEF 검증값 재현(로더 경로): AAPL 2014-06-06 raw 645.57·vol 12,517,270, 06-09 raw 93.70, split_adj 23.056→23.425(ret_total +1.60%, sf 28→4); COST 2020-12-01 raw 387.56·divAdj 스텝 0.974475(ret_total +1.52% vs price −1.07%); EBAY 2015-07-20 divAdj 스텝 0.420878(ret_total +2.40% vs price −56.9%).

### 9-2. 실데이터 규모 합성(22,871 × 6,713, 47,988,596 행) — 참고

| 단계 | 시간 | peak RSS |
|---|---|---|
| 6 필드 + returns + mask 전체(전 패널 상주) | ≈ 13 s | 8.2 GB |
| export_wide 6 필드+returns+mask | 31 s | 11.0 GB(release 없이) |
| 부분 로딩 1,056 id × 2010+ close split_adj | 1.9 s | |

목표(C++ 포함): 전체 로딩 ≤ 60 s, 메모리 ≤ 8 GB → 파이썬 참조 구현 실측 **로딩 ≈ 10 s(6 필드+returns+마스크), 상주 ≈ 8 GB**(전 패널을 동시에 들 때; lazy·해제 시 4 GB 아래). 상주 산식 = 가격 필드당 `T×N×4B`(616MB) + volume `×8B`(1.23GB) + returns `×8B`(1.23GB) + 계수 행별 `48M×8B×2`(0.77GB) + 좌표 `48M×4B×2`(0.38GB). 재실행: `loader.py bench --root <T2> --master-root <T1> --out <dir>`.

## 10. parity 절차 (파이썬 export_wide ↔ C++ DataPanel)

| 단계 | 내용 |
|---|---|
| P1 | 파이썬: `CacheV21(root, master_root).export_wide(out, fields=6, basis='split_adj', masks=('tradable',), universes=('Top500',), factors=True)` → `EXPORT.json`(sha256·행·열·`cut.cells_removed`). 기본 절단본; C++ 도 같은 절단(lastTradeDate 이후 NaN)을 적용한 뒤 비교. 미절단 비교는 `cut=None`/`--no-cut` |
| P2 | C++: 같은 root 에서 long → wide 를 자체 구현으로 만들고, 셀 단위 비교: 두 값이 모두 NaN 이거나 **비트 동일**(float32 는 `memcmp`, volume·returns 는 double 비트). 허용 오차 없음 — 산식·순서를 §4 대로 하면 달성 가능. C++ 쪽 결과를 같은 wide parquet 으로 내보내면 `loader.py compare --a A.parquet --b B.parquet [--rtol 0]`(= `CacheV21.compare_wide`) 가 공통 (date, symbol) 셀의 불일치 수·최대 상대차·예시 5 개·한쪽에만 있는 날짜/심볼 수를 낸다 |
| P3 | 비교 축: (date, str(tradingItemId)). 열 순서 차이는 무시. 달력은 두 쪽 모두 base_data 행 존재 날짜의 집합이어야 함 |
| P4 | 마스크: bool 셀 단위 동일. returns: `Returns/returns_total.parquet` 와 동일 |
| P5 | 재현성: 같은 입력에서 파이썬 export 두 번의 sha256 이 같음(테스트 확인). C++ 산출물도 parquet 바이트가 아니라 **값**을 비교(인코딩·row group 차이는 무시) |
| P6 | 경계 fixture 로 먼저: `tests/make_fixture.py <dir>` 로 6종목 fixture 를 만들어 C++ 가 §8 의 기대값을 내는지 확인한 뒤 실데이터로 |

기존 엔진 v1 경로로 바로 쓰기: `base: {cache: <export_dir>, groups: <export_dir>/groups}` — `export_wide(fields=6+marketCap_USD, groups=True)` 가 필요한 파일을 전부 만든다(marketCap 은 `derived/marketcap.parquet` 도착 후). 2026-09-15 빌드는 `base:` 를 몰라 `datasets:` 로 9 필드를 얹어 기동 확인함(§6-1). `datasets:` 로 필드를 개별 선언하면 지금도 읽는다 — 실행 확인(2026-09-16, 기존 빌드 `/tmp/longshort_module_server/build/longshort-server`, 별도 포트 18999, fixture export):

```yaml
datasets:
  - {name: v21_export, format: wide, path: <export>/BaseData, lazy: false, use_col: [open, high, low, close, volume], fields: {vwap: VWAP.parquet}}
  - {name: v21_returns, format: wide, path: <export>/Returns, lazy: false, fields: {holding_return: returns_total.parquet}}
universe: {path: <export>/Mask/tradable_Top500.parquet}
```
→ `Data loaded: T=501 N=6`, `/api/config` columns = close·high·holding_return·low·open·volume·vwap, symbols "101"~"106", universe bool 마스크 정상. 즉 export_wide 산출물은 C++ 기본 wide 리더(`__index_level_0__` ts[ns] + 문자열 열)와 그대로 호환된다. 남는 것은 값 parity(P2) 뿐이다.

## 11. 애매한 점·제안 (리드 결정 요청)

| # | 항목 | 관찰 | 제안 |
|---|---|---|---|
| 1 | marketCap·그룹 패널 | 엔진 `base:` 는 `marketCap_USD.parquet`·`sector/industry` 를 필수로 읽음 | **해소**: marketcap 은 `derived/marketcap.parquet` → export(실데이터 파일 T5 대기), 그룹 패널은 `export_wide(groups=True)`(§6-1, classification_history 원천, broadcast_current 기본). 남은 것: 현행 소스 빌드로 `base:` 기동 1 회 확인, pit 모드는 분류 이력이 쌓인 뒤 |
| 2 | volume div_adj | 계약에 정의 없음 | 정의 없음(오류)으로 확정. 문서에 명시 |
| 3 | lifecycle 커버리지 | 최종본은 일치(T2 base_data 22,873 id = T1 lifecycle 22,873, 누락 0). T2 계수 테이블은 38,118 id(초과분은 로더가 버림) | publish 게이트 "lifecycle 에 없는 base id = 0" 유지. 로더 기본 `open` 은 경고 |
| 4 | 재상장 새 id 의 firstTradeDate | **해소**: T1 최종본이 이어붙인 802 종목의 firstTradeDate 를 lineage effectiveDate 로 올림(firstTradeSource='lineage'), firstPriceDate 로 원래 시작 보존 | 로더는 tradable=firstTradeDate, priced=firstPriceDate 로 구분. `apply_lineage` 는 무해한 이중 안전장치 |
| 5 | returns 와 마스크의 NaN 규칙 | returns 파일은 lifecycle 마스킹 없음, 로더도 안 함 | publish 시 `Returns` wide 는 tradable False 셀을 NaN 으로 만들지 말고(연구 재현성) 마스크를 함께 배포. 엔진은 `tradable` 을 universe 에 AND |
| 6 | factor source 값 | T2 dividend 에 계약 외 `synthetic_post_last`(7,009)·`synthetic_gap_fill`(92) 존재, snapshotDate 가 timestamp[us] | 계약 §3-2 source 목록에 추가하고 의미 정의(post_last = 마지막 구간 이후 1.0 고정?), snapshotDate ns 로 통일 |
| 7 | split factor 극단값 | T2 split factor 최소 1.5e-12, 최대 3,301 | 로더는 양수·유한만 요구. 극단값 종목 목록은 T2 QA 에서 확인 권고(역분할 연쇄인지 오류인지) |
| 8 | 달력 정의 | "행이 있는 날짜" 로 정의하면 하루 전체 결측일은 달력에서 빠짐 | 거래일 달력 파일(NYSE) 을 계약에 추가하면 C++·파이썬이 같은 축을 외부에서 받음. 현재는 base_data 로 충분 |
