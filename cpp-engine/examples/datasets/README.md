# C++ wide Parquet 연결 예제

`equities_monthly_afl.yaml`은 서버의 일별 가격·Top500 마스크와 기존 `AflData_M`을 직접 읽는다. 개인 폴더에 YAML과 `afl_catalog.json`을 함께 복사한 뒤 실행한다. C++ 빌드 환경 최초 설정은 [빌드 안내](../../../docs/module_discovery_build_2026-09-09.md)를 따른다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/my/equities_monthly_afl.yaml
```

## 기본 데이터(`base:`)

`base:`는 가격 캐시 하나와 그룹 패널 디렉토리 하나만 적어 기본 9개 필드를 등록한다. 필드를 `datasets:`에 나열하지 않는다.

```yaml
base:
  cache: /home/base/cache/cache_v1_cash_1d        # <cache>/BaseData/*.parquet
  groups: ../../../data/groups/cache_v1_cash_1d   # <groups>/{sector,industry}.parquet
```

| 공개 필드 | 파일 | 비고 |
|---|---|---|
| `open` `high` `low` `close` `volume` | `<cache>/BaseData/{open,high,low,close,volume}.parquet` | |
| `vwap` | `<cache>/BaseData/VWAP.parquet` | 공개 이름은 소문자 |
| `cap` | `<cache>/BaseData/marketCap_USD.parquet` | 시가총액(USD) |
| `sector` `industry` | `<groups>/{sector,industry}.parquet` | 정수 코드를 float로 저장, 미분류는 NaN |
| `returns` | 없음 | 기존대로 `close`에서 파생되며 필드로 등록하지 않는다 |

- 전개 결과는 `base_prices`(7필드, `alignment: exact`)·`base_groups`(2필드, `alignment: asof`) 두 데이터셋이고 `format: wide`, `lazy: false`이며 `datasets:`보다 **앞**에 놓인다. 두 이름은 예약되어 `datasets:`에서 다시 쓸 수 없다.
- `cache`·`groups`는 둘 다 필수다. 경로 규약은 다른 항목과 같아서 상대경로는 이 YAML이 있는 디렉토리 기준으로 푼다.
- `base:`가 있으면 `datasets:`는 생략하거나 빈 시퀀스로 둘 수 있다. 둘 다 없으면 기존처럼 `datasets must be a non-empty sequence` 오류다.
- `datasets:`에서 `close` 같은 기본 필드를 다시 선언하면 로드가 `Duplicate public data field: close`로 실패한다. 추가 데이터셋에는 다른 이름의 필드만 적는다.
- 9개 필드는 시작 시 전부 읽는다(lazy 아님). 파일이 하나라도 없으면 `Cannot open parquet <파일 경로>`로 기동이 실패한다. 이벤트 파일과 같은 규약이며 조용한 부재를 막는다.
- 그룹 코드 규약과 `sector.parquet`·`industry.parquet` 생성은 [그룹 패널 도구](../../tools/data/README_group_panels.md)를 따른다. 가격 캐시를 다시 만들면 그룹 파일도 다시 만든다.
- 그룹 패널은 `alignment: asof`라 가격 캐시가 그룹 파일보다 뒤로 늘어나면 마지막 분류가 그대로 이어진다(일간 캐시 갱신마다 재생성할 필요 없음). 단 그룹 파일에 없는 **새 심볼**은 재생성 전까지 미분류(NaN)로 남아 `neutralize`에서 빠지고, 그룹 파일이 가격 달력보다 늦게 시작하거나 다른 캐시용이면 코드 없는 bar가 생겨 로더가 기동을 거부한다(`Group panel '<field>' has no codes on <date>`). 캐시를 재구축했거나 심볼이 늘었으면 그룹 파일을 다시 생성한다.

메모리는 필드당 `T×N×4B`다.

| 패널 | 크기 | 필드 1개 | 기본 9필드 |
|---|---|---|---|
| Top500 | 1,056종목 × 4,449일 | 19MB | 0.17GB |
| ALL | 22,871종목 × 6,713일 | 614MB | 5.5GB |

전환 전 예제도 가격 5필드를 eager로 읽어 ALL에서 3GB를 썼다. `base:`는 여기에 `vwap`·`cap`·`sector`·`industry` 4필드를 더 읽는다.

가격·유니버스는 시작 시 읽는다. AFL은 600개 팩터의 1,192개 이름을 등록하고, `DataPanel::row()`/`at()`에서 처음 참조하는 필드만 읽는다. XML뿐 아니라 사용자 C++ 모듈에서도 같은 경로를 사용한다. 동일 파일을 가리키는 이름들은 한 엔진 프로세스 안에서 값을 공유한다. 별도 엔진 프로세스는 별도 RAM을 사용한다. 사용한 필드는 프로세스 종료까지 유지되므로, 장시간 여러 팩터를 탐색하면 캐시 사용량도 늘어난다.

```xml
<data expr="EPSNumRevFY1C" delay="0"/>
<rank/>
<zscore/>
```

`EPSNumRevFY1C`와 `afl_963`은 모두 `963.parquet`을 가리킨다. 숫자로 시작하는 FactorCode는 `afl_` 접두사를 붙인다. 예를 들어 `10DMACD`는 `afl_10DMACD`, 숫자 ID로는 `afl_104`를 사용한다. `returns`는 기존처럼 종가 비율에서 파생되므로 외부 수익률 필드에는 `holding_return` 같은 별도 이름을 쓴다.

`number_of_revision` 같은 팀 별칭은 어떤 원본 팩터인지 명시해야 한다. 다음 명령은 이를 **EPSNumRevFY1C(963)**로 선택하는 예제이며, 조정된 팩터 **AdjEPSNumRevFY1C(960)**와는 별개다. 기본 카탈로그에는 이 모호한 별칭을 자동으로 넣지 않는다.

```bash
python3 cpp-engine/tools/build_afl_catalog.py \
  --dictionary /home/base/cache/cache_v1_cash_1d/AFL_Dictionary.xlsx \
  --data-dir /home/base/cache/cache_v1_cash_1d/AflData_M \
  --alias number_of_revision=EPSNumRevFY1C \
  --output /path/to/my/afl_catalog.json
```

카탈로그 생성은 팩터 값에 접근하지 않는다. XLSX 사전을 읽을 때만 Python의 `openpyxl`이 필요하며, 생성된 카탈로그와 Parquet은 C++이 직접 읽는다. 현재 예제 카탈로그에는 원본 사전의 SHA-256이 기록돼 있다.

## 시간·유니버스 규칙

- `end_date: latest` 또는 생략은 엔진 시작 시 선택한 종목들의 종가가 전부 NaN인 끝행을 제외하고 종료일을 정한다. Parquet이 갱신된 뒤 엔진을 재시작하면 새 날짜를 읽는다. 실행 중 자동 갱신과 버전 관리 기능은 없다. 열린 웹 화면은 새로고침해 날짜 범위를 갱신한다.
- 날짜를 직접 지정한 `end_date: '2026-07-31'`은 기존처럼 그 날짜까지만 읽으며, 마지막 행이 전부 결측이어도 임의로 기간을 줄이지 않는다.
- `latest`는 수집 완료를 보증하는 기준이 아니다. 중간의 전 종목 결측, 마지막 행의 일부 종목 결측, 0·음수·Inf 가격은 그대로 남는다. 거래 시작일 이후 종가가 전부 결측이면 로딩 오류를 반환한다.

- `warmup_bars`는 `start_date` 이전에 추가로 읽을 가격 bar 수다. `0`은 추가 과거 구간 없음이다. 사용하는 시계열 연산의 누적 lookback과 지연을 감안해 지정한다. 이 구간에서는 신호·모듈 상태만 준비하고 매매·비용·보유손익은 발생하지 않는다.
- 유니버스는 날짜별 별도 마스크다. 가격 원본을 NaN으로 덮지 않는다. `rank`와 `zscore`는 해당 신호 날짜의 유니버스 안에서 계산하고, 최종 목표 포지션도 같은 마스크를 적용한다.
- `mode=1`에서는 목표 포지션과 마스크가 함께 하루 지연된다. 이는 비교에 사용한 팀 `pv_champ_peer`의 `sig.where(valid)` 뒤 `shift(1)` 순서를 따른다. 공통 `StatsHandler` 자체에는 별도 유니버스 강제청산 규칙이 없다.
- 보유 중인 종목의 종가가 없을 때의 처리는 `backtest.missing_close_policy`가 정한다. 기본 `carry`는 보유를 동결하고, `error`는 기존처럼 실행을 실패시킨다. 유니버스 이탈과 실제 가격 결측은 여전히 다른 상태다.
- `alignment: exact`는 같은 시각의 관측만 사용한다. `asof`는 해당 시각 이전의 마지막 관측을 유지하고, 최초 관측 이전은 NaN이다. 미래 값을 과거로 채우지 않는다. 파일의 날짜가 실제로 사용 가능한 시각이라는 데이터 계약이 필요하며, 이 옵션이 사후 수정 데이터의 과거 버전을 복원하지는 않는다.
- 이 예제의 `AflData_M`은 월별 관측 캐시다. 2026-09-10 확인 당시 963번 팩터의 마지막 관측은 2026-08-28이었다. `asof` 결과가 매일 있어도 매일 갱신된 AFL 데이터는 아니다. 일별 AFL을 쓰려면 일별 원천 데이터와 사용 가능 날짜를 검증한 파일로 경로를 바꾼다.

## 결측 종가·상폐 정산 규칙

최상위 `events:`는 증권별 행정 종료일을 한 행씩 담은 파일을 가리킨다. 계약 원문은 [결측·상폐 회계 검증 문서](../../../docs/missing_data_validation_2026-09-11.md)다.

```yaml
events:
  path: /home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet
  id_col: tradingItemId        # int64 또는 문자열. 문자열로 정규화해 가격 심볼과 맞춘다
  end_date_col: securityEndDate # timestamp(s/ms/us/ns)·date32·date64·'YYYY-MM-DD' 문자열
  # cash_col: cashPerShareCache            # 선택. 주당 현금 대가(캐시 주식 단위)
  # successor_col: successorTradingItemId    # 선택. 승계(인수사) 심볼
  # successor_ratio_col: successorRatioCache # 선택. 구주 1주당 승계사 주식 수(캐시 주식 단위)
  # stock_leg_col: hasStockLeg               # 선택. 주식 다리 존재 여부(구조)
  # cash_leg_col: hasCashLeg                 # 선택. 현금 다리 존재 여부(구조)
  # successor_price_col: successorCacheClose      # 선택. 스테일 가드용 승계사 종가(정산 bar 당일)
  # successor_price_date_col: successorCacheCloseDate  # 선택. 그 종가의 날짜
  # target_price_col: targetCacheMark             # 선택. 대상의 마지막 유효 캐시 종가
  # target_price_date_col: targetCacheMarkDate    # 선택. 그 종가의 날짜
  # return_col: settlementReturn             # 선택. 정산 bar에서 값을 매길 수 없을 때만 쓰는 총 수익률
```

- **다리 구조와 평가 가능성은 별개다**: `stock_leg_col`/`cash_leg_col`은 대가의 **구조**(주식 다리·현금 다리 존재 여부)를 선언한다. 컬럼이 없으면 주식 다리는 `successor_ratio_col` 유한 **또는** 승계사 id 지정(패널 밖 포함), 현금 다리는 `cash_col` 유한으로 유도한다. 엔진은 **선언된 다리를 전부 이 패널에서 매길 수 있을 때만** 계산값을 쓴다: 주식 다리가 있는데 승계사를 쓸 수 없거나 캐시 교환비가 없으면 현금 다리만으로 값을 매기지 않고 `return_col` > 기본값으로 간다. 생성기가 승계사 환산에 실패해 `successorRatioCache`만 NaN이 된 행(실제 32건, 예: IWA 12678069)을 현금 전용 딜로 오인해 −52.6%를 계상하던 문제를 막는다.
- **단위와 스테일 가드**: `cash_col`·`successor_ratio_col`은 **가격 캐시의 주식 단위**로 환산된 값이어야 한다. 캐시 종가는 이후 분할까지 반영해 재작성되므로, 당시 기준 현금·교환비를 캐시 종가에 그대로 곱하면 단위가 섞여 수익률이 크게 틀린다(예: NVLS→LRCX 2012, GOOG 2015). 따라서 **가격 캐시를 다시 만들면 이벤트 파일도 다시 만들어야 한다.** 가드는 **이 패널이 실제로 정산하는 이벤트를 건너뛰지 않는다**: 로더가 이벤트마다 엔진의 정산 bar `s*`(종료일 다음 bar부터 대상 종가가 처음 끊기는 bar)를 계산하고, `s*`가 패널 안이면 승계사 앵커는 `s*` 당일, 대상 앵커는 `s*` 직전 마지막 유효 종가일이어야 하며 값은 상대오차 `1e-4` 안이어야 한다. 날짜가 어긋나거나, 앵커가 NaN/Inf인데 그 앵커로 매길 평가 입력이 유한하면 **로드를 실패**시킨다(`Events file is stale against the price cache: ...`). 건너뛰는 경우는 **`s*`가 패널 밖(이 패널이 그 이벤트를 쓰지 않음)이거나 승계사가 패널 밖일 때뿐**이며, 적재 구간(warmup 포함)을 바꿔도 같은 판정이 나온다. 앵커 날짜가 자정이면 일 단위, 시각이 붙어 있으면 그 bar와 정확히 일치해야 한다. 가드 결과는 `/api/config`에서 세 갈래로 보고한다: `eventsPriceChecks`(앵커를 다시 읽어 대조함), `eventsPriceChecksOutOfRange`(정산 bar가 패널 밖이라 이 패널이 쓰지 않는 이벤트), `eventsPriceChecksNotEvaluable`(캐시 단위 평가 입력이 없어 대조할 것이 없음). **`eventsLoaded`는 이벤트 파일에서 행을 찾은 패널 심볼 수**이고 **`settlementSymbols`는 그중 정산이 이 패널 안에서 열리는(종료일이 마지막 bar 이전인) 심볼 수**이며, 세 가드 값은 매칭된 행을 **한 행당 정확히 한 버킷**으로 나누므로 가드를 선언한 config에서는 `eventsPriceChecks + OutOfRange + NotEvaluable = eventsLoaded`가 항상 성립한다.

- **정산 시점**: 종료일 당일까지는 그대로 보유하고, 그 다음 패널 bar부터 정산이 열리며 **호가가 끊긴 첫 bar**에 정산한다. 종료일 이후에도 호가가 있으면 확인된 호가가 우선해 정상 평가·체결하고 `postEndDateQuoteBars`로 집계하며, 패널 끝까지 호가가 이어지면 정산하지 않는다(`pendingSettlements`). 종료일에 시각이 붙어 있으면 일 단위로 내림해 그 날 끝까지 보유한 것으로 본다. 종료일이 패널 첫 bar 이전이면 그 종목은 첫 bar부터 정산 완료 상태이고, 정산 bar가 패널 마지막 bar를 넘으면 이 패널 안에서는 이벤트가 없는 것으로 둔다. 종료일 셀이 null인 증권, 파일에 없는 심볼도 이벤트 없음이다.
- **기본 정산 수익률**: `backtest.default_delisting_return`(기본 `0.0`)을 쓴다. **상폐 손익 0은 명시적 가정이지 측정값이 아니다.** 민감도는 같은 이벤트 집합으로 `-1.0`을 넣어 다시 실행해 비교한다. 우선순위는 **정산 bar에서 계산한 값 > `return_col` > 기본값**이다: `cash_col`·`successor_ratio_col`로 주당 대가 `V = cash + ratio·close_succ(t)`를 정산 bar `t`에서 매길 수 있으면 `R = V/mark − 1`을 엔진이 계산하고(`computedSettlementReturns`), 승계사가 패널 밖이라 매길 수 없을 때만 파일의 `return_col`을 쓴다. 현금 전용 딜은 `cash_col`만으로 계산되므로 파일 수익률이 필요 없다.
- 정산은 행정 처리라 시장 체결이 아니다. turnover와 수수료가 붙지 않고, 정산된 금액은 별도로 집계한다. 정산된 종목은 이후 가격이 다시 나와도 재진입하지 않는다.
- **`stale_quote_settle_bars`**: 이벤트가 없는 보유 종목이 이 값만큼 연속으로 결측이면 기본 수익률로 정산한다. `0`은 이 경로를 끄고, 그런 보유는 마지막 bar까지 동결 상태로 남는다. 예제는 `0`을 쓴다(휴리스틱이라 기본 꺼짐).
- **`missing_close_policy`**: `carry`는 결측 bar에서 보유를 동결하고(손익 항 0, 체결·수수료 없음) 유효 가격이 돌아오면 그 구간 전체 수익률을 한 번에 인식한다. `error`는 기존 fail-fast다. 이벤트 정산은 가격이 필요 없으므로 두 정책에서 모두 동작한다.
- id 중복, 지원하지 않는 컬럼 타입, 지정한 컬럼 부재는 명시적 오류다. `path`가 비어 있으면 이벤트 없이 로드하고, **설정된 파일이 없으면 로드 실패**다(오탈자 경로가 조용히 정산을 끄는 것을 막는다).
- 2026-09-11 기준 예제 설정(2010-01-01 시작, warmup 252) 실측: 패널 1,056종목·4,449 bar 중 종료일이 있어 패널 안에서 정산되는 종목 235개, 이벤트 없음 821개, 패널 시작 전 종료 0개. 첫 정산은 `2660009`(2009-01-07)다.

`GET /api/config`의 `columns`는 사용할 수 있는 이름 목록, `loadedFields`와 `loadedDataBytes`는 읽은 수치 필드 수와 값 배열 크기다. 후자는 유니버스 마스크, 모듈 상태, 시그널 플롯, Arrow 읽기 버퍼를 포함한 전체 RSS와 다르다. 현재 설정과 파일은 엔진 재시작 때 다시 읽는다.

### stale 정산은 휴리스틱이다

`stale_quote_settle_bars`는 종료일 이벤트가 없는 종목의 **보유분**이 N bar 연속 결측일 때 기본 상폐 수익률로 정산하는 선택 옵션이다. 가격이 오래 없다는 사실이 상폐를 확정하지 않으므로 기본값은 0(사용 안 함)이며, 주식 예제도 0으로 둔다. 종료일이 없는 종목은 원천 마스터의 종료일을 보정하는 것이 우선이다([종료일 없는 종목 목록](../../../docs/missing_close_2026-09-11/no_end_date_symbols.md)). 옵션을 켜면 `missingData.staleSettlements`로 건수가 보고된다.

### 승계(successor) 이벤트

이벤트 파일에 `successor_col`/`successor_ratio_col`이 있으면 정산 시 주식 몫을 승계한다: 정산 bar `t`에서 `R = (cash + ratio·close_succ(t))/mark − 1`로 손익을 인식한 뒤, **주식 몫 `h·ratio·close_succ(t)/mark`만** 승계사 보유 가중치에 더하고 현금 몫은 실현되어 장부에서 빠진다(다음 체결에서 목표를 채우면 그만큼 정상적인 회전·비용이 생긴다). 승계는 시장 체결이 아니므로 회전·비용이 없고, 그날 승계사의 가격 변동은 승계분에 반영하지 않는다. 승계사가 패널에 없거나 그 bar에 호가가 없거나 같은 bar에 정산되는 종목이면 전액 현금 정산으로 돌아간다(이때만 `return_col`이 쓰인다). 선언된 다리(현금·주식)를 **모두** 그 bar에서 매길 수 있어야 계산값을 쓴다: 교환비가 있는 딜의 승계사를 쓸 수 없으면 현금 몫만으로 값을 매기지 않고 `return_col` > 기본값으로 간다. 즉 "승계사 미지정 + 현금"(계산)과 "승계사 지정했으나 해결 실패 + 현금"(파일 R)은 다르게 동작한다. 진단은 두 기준을 쓴다: `settledGross`·`defaultDelistingGross`는 Phase 1과 같은 **노출 기준 `|h|`**(혼합 딜은 `|h|·cash/V` 몫)이고, `convertedGross`는 **실제 이전된 가중치** `|h·ratio·close_succ/mark|`다. 두 값의 합은 `|h|`가 아니며, `successorConversions`·`computedSettlementReturns`와 함께 `missingData`에 보고한다.

승계 계산에 쓰는 `cash_col`·`successor_ratio_col` 값은 **가격 캐시의 주식 단위**여야 한다(위 "단위와 스테일 가드"). 캐시를 재구축했으면 이벤트 파일을 재생성하고, `successor_price_col`/`successor_price_date_col` 가드를 켜 둔 채로 실행한다.

이벤트 파일 생성: `cpp-engine/tools/data/refresh_master.py`(마스터 종료일 갱신) → `cpp-engine/tools/data/build_events.py`(co_acthist 조건 파싱 → `cashPerShare`·승계 심볼·교환비, 패널 밖 승계자용 `settlementReturn`) → `data/events/events.parquet`. 생성기의 인수사 가격은 정산 bar 이후를 절대 보지 않으며 실제 사용한 날짜를 `acquirerPriceDate`에 남긴다. DB(`ciqsecurity.securityenddate`)는 증권 단위 날짜라 트레이딩아이템의 마지막 DB 가격과 맞을 때(10일 이내)만 채택하고, 아니면 로컬 마스터 날짜를 쓴다(`endDateSource`, `data/events/enddate_conflicts.csv`). 규칙과 통계는 `docs/missing_close_2026-09-11/phase2/events_report.md`.
