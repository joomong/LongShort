# 계산/API 논리 수정 — 2026-09-11

기준: clean main `3e663d6`. 기존 종가 체결·고정 북 단리·거래비용 산식을 유지하고, 결합·실행 시작 범위·지표와 raw signal 응답 경로를 수정했다. 소유 범위는 main/API/metrics 및 후속으로 할당된 Rank/Zscore다. 데이터 로더와 Simulator의 기본 전이는 별도 담당이 구현하고 인터페이스를 맞췄다.

## 결합 정책과 결정성

- `combinedMode: normalized`(config `combined_mode`, 기본값): 각 알파의 당일 신호를 동일한 `normalize_signal_row`로 L1 정규화한 후 합치고, combined simulator에서 마지막 L1 정규화를 수행한다. 각 신호의 유효 close와 **신호일 universe**를 동일하게 사용한다. 활성 신호의 원래 숫자 크기는 알파 가중치가 되지 않는다. 동일 비중 평균의 상수 1/K는 마지막 정규화에서 상쇄되므로 합산으로 구현했다.
- `combinedMode: raw`: 이전 raw 신호 합산 의미를 명시적으로 선택할 수 있다. 부동소수점 합산 순서는 아래와 같이 고정된다.
- `OrderedSignalAccumulator`는 각 날짜별로 다음에 더할 원래 alpha index를 보관한다. 병렬 worker의 완료 순서나 worker 수가 달라도 모든 날짜에서 alpha 0,1,2,… 순으로 더한다. 같은 seed/데이터의 결과가 스레드 스케줄에 따라 달라지지 않는다.
- 가장 이른 미완료 알파는 앞선 알파들이 모두 끝났으므로 계속 진행할 수 있다. 오류가 발생하면 cancel이 조건변수에서 기다리는 worker를 모두 깨워 교착을 방지한다.
- raw 유한 값 합이 float 범위를 넘으면 오류를 반환한다. Inf를 새로운 결측값으로 취급하고 다음 알파에서 이전 합을 버리던 경로는 차단했다.
- 완료된 모듈의 실행 상태를 해제하고 top-K plot만 확정된 expanded XML로 다시 실행한다. 모든 알파의 T×N 원시 행렬을 보관하지 않는다. 결합 임시 저장은 float T×N 및 날짜별 순서 배열 T에 비례한다.

## simStart와 지표 기간

- 서버 적재는 `DataPanel::load(g_config)`를 사용한다. `trade_start_index`보다 이른 적재 행은 signal warmup에만 사용할 수 있다.
- 요청의 simStart는 ISO 날짜로 엄격히 검사하고 lower_bound로 실제 거래일에 맞춘다. config 거래 시작보다 앞선 요청은 config 하한을 유지한다. 데이터 종료 이후 simStart는 400 오류다.
- Simulator는 시작 전 module/target queue를 갱신하지만 보유 포지션·손익·turnover는 0이다. D1에서는 시작일의 첫 종가에 warmup 마지막 target으로 진입한다. 그날 이전 보유 손익은 없고 실제 진입 비용만 발생한다.
- metrics/yearly/timeseries는 시작 index부터 계산하며, IS/OS는 이 범위와 교집합을 사용한다. 첫 진입일 비용은 누락하지 않는다. JSON의 isStartIndex/osStartIndex는 반환한 timeseries 안의 index다.
- API의 `mode`는 기존 종가 체결 D0=0, D1=1만 받으며 config 기본 1을 유지한다. 소수/음수/다른 숫자는 400으로 거절한다. 일반 실행과 GA 모두 같은 mode를 사용하고 config/응답에도 mode를 표시한다. UI의 Jobs는 기존 threads 요청에 연결된다.
- 적재 시작이 바로 global bar 0인 D1 경로는 이전 신호가 없으므로 첫 지표 bar=`max(sim_start_index,1)`인 기존 관례를 유지한다. **D0는 첫 지표 bar=`sim_start_index`**여서 global bar 0에서 발생한 첫 종가 진입 비용까지 포함한다. 이전에 UI의 D0 선택을 서버가 무시하던 경로를 실제 기존 계산 모드에 연결한 변경이다.
- API와 GA의 연환산은 모두 `g_config.bars_per_year`를 전달한다. crypto 기본 365를 유지하고, 252 설정 시 그 값이 summary/yearly/Sharpe에 일관되게 적용된다. 수수료 값을 변경하지 않는다.

## Universe와 cross-sectional 계산

- PV 비교 기준의 팀 코드에서 `sig.where(valid)` 뒤 `shift(1)`을 적용한다. 따라서 target의 universe 제외와 신호는 D1 지연을 함께 받는다. 실행 당일 별도 강제 청산 가정은 추가하지 않았다. 보유 손익은 실제 보유분/원래 close로 평가한다.
- 최종 포지션 정규화만 mask하면 제외 종목의 값이 이미 Rank/Zscore 통계에 섞이는 문제가 있다. 두 builtin에 optional `panel`을 주입하고 계산 시점 membership 밖 값을 CS 표본에서 제외한다.
- null panel 또는 빈 mask인 기존 직접 모듈 사용은 이전 연산을 유지한다. 데이터 패널의 원시 가격, mask, TS 모듈의 과거 가격 자체는 수정하지 않는다.

## signal payload와 크기

- single은 기존 top-level raw signalPlot/signalRange/distribution을 제공한다.
- multi `collectSignals=true`는 IS Sharpe top-K 알파별 payload와 combined/top-level payload를 제공한다. K는 기본 1, 서버 최대 5다. rank 밖의 알파는 프론트에서 해당 expanded XML을 단일 실행하여 signal payload만 받아 캐시한다.
- GA complete는 best batch 및 top-level에 동일한 raw signal payload를 제공한다.
- signalPlot은 최대 250,000 cells/1,000 날짜 행으로 제한한다. `signalPlotMaxCells` 요청으로 더 작게 지정할 수 있다. 날짜 수가 많으면 stride로, 종목만으로 budget을 넘으면 종목도 stride로 고른다. 마지막 관측은 항상 남기며 실제 timestamps/symbols와 raw 값을 함께 선택한다.
- metadata `rowStride`, `symbolStride`, `originalRows`, `originalSymbols`, `sampled`로 downsampling 여부를 알린다. 개별 알파 신호값은 재정규화·집계하지 않는다. histogram 및 signalRange는 전체 선택 기간의 원시 신호를 기준으로 유지한다.
- 이 budget은 응답 plot에 대한 제한이다. 각 plot 수집 실행에서 일시적으로 보관하는 T×N 원시 행렬이나 데이터 패널 전체 메모리까지 고정하는 변경은 아니다.

## GA Stop과 worker 수명

기존 async stream callback 안의 동기 GA 실행은 Drogon I/O loop를 막아 progress 전송과 disconnect 인식도 지연시킬 수 있었다. GA controller를 별도 소유 worker로 옮겼다.

- 각 후보의 raw signal bar callback에서 `req->connected()`를 확인한다. 연결이 끊기면 현재 후보 계산을 중단하고 이후 후보는 즉시 건너뛴다. progress callback에서 다음 generation으로 넘어가지 않고 종료한다.
- 완료 worker는 다음 launch 때 join/reap하며 서버 종료 시 stop flag를 세우고 전부 join한 후 공유 데이터가 해제된다. thread 생성 실패도 처리하여 stream 소유권이 누출되지 않는다.
- 취소는 bar 경계에서 확인한다. 일반 simulate 요청의 서버 계산까지 즉시 취소한다고 주장하는 변경은 아니다. 프론트의 Abort/stale-response 차단은 별도다.

## config API

`columns`는 lazy catalog를 포함한 `g_data.column_names()`다. `loadedFields`는 현재 실제 적재된 float field **개수(number)**, `loadedDataBytes`는 그 float 값 배열의 byte 수다. universe mask·모듈 상태·plot JSON·프로세스 RSS는 포함하지 않는다.

## 검증 및 독립 리뷰

- 별도 metrics/serializer 실행: 30/30 통과. 첫 진입일 비용/기간 앞 warmup/연도 절단/OS 시작 clamp 및 bounded plot의 원시값·최신 날짜를 확인했다.
- 결합 테스트: raw/normalized 모드의 동일 비중 예제, 1/2/4 worker 정확 동일 결과, 실패 시 대기 해제, 신호일 universe, float overflow를 추가했다.
- HTTP 테스트: 실제 simStart 첫 비용과 날짜 정렬, signal budget·잘못된 날짜, combined 모드와 top-K payload, GA complete raw payload를 추가했다. 기존 반복 결정성 검사는 worker 수 1~4를 바꾸도록 보강했다. 후속 D0/D1 검사는 현재/직전 raw signal에서 손계산한 첫 target maxWeight와 D0 첫 날짜, 잘못된 mode 거절을 확인한다.
- root Simulator/expr parser를 독립 리뷰했다. product transition은 합의와 일치했다. 새 expression 테스트가 마지막 Delay만 실행하던 검증 문제를 발견하여 root가 pipeline 전체 실행으로 정정했다.
- frontend snapshot 리뷰에서 `loadedFields`를 string[]로 선언한 wire 타입 불일치 1건을 발견했고 담당자가 number로 정정했다. signal 선택/combined fallback/on-demand 요청 기간 보존/revision과 Abort guard는 계약과 일치했다.
- 최종 프론트 delta에서 raw signal payload의 LRU 5개 상한·지표 보존, single/combined payload 참조 공유, Save의 detail XML 우선순위를 재검토했다. 독립 실행한 signalSelection/SaveAlphaModal/adaptSimulateResponse 3개 파일의 15개 검사가 통과했다. mode/threads 추가 후 on-demand도 원 실행의 날짜·mode·threads를 보존하도록 담당자가 확장했다.
- 최신 실제 C++ 서버와 독립 synthetic config로 HTTP 12/12를 통과했다. 초기 1개 실패는 기존 테스트가 지원되지 않는 singular `return`을 사용한 오류였으며, 원래 의도인 derived `returns`로 정정했다. HTTP 테스트에는 `LONGSHORT_TEST_SERVER`/`LONGSHORT_TEST_CONFIG`를 명시할 수 있다. 명시한 경로가 없으면 skip하지 않고 실패한다.
- GA의 실제 소켓 연결을 닫은 뒤 OS thread 수가 baseline 7 → 실행 중 9 → 7로 돌아왔다(이 fixture 관측 약 50ms). 활성 GA 중 SIGTERM도 약 7ms 후 exit 0으로 종료했다. 다른 데이터·모듈의 취소 지연을 이 수치로 보장하지 않는다.
- raw histogram에 유한 ±FLT_MAX가 들어올 때 float `max-min` overflow로 잘못된 정수변환이 생기는 경로를 보완했다. 이 경우에만 double 폭/중심을 사용하고 일반 float bin 계산은 유지한다. 회귀 테스트를 추가했다.
- root의 실제 팀 StatsHandler oracle 비교 4건은 delayed exit, simStart 진입, 빈 universe exit에서 PnL 차이 0 및 청산 close 결측 오류를 확인했다(`/tmp/longshort_team_parity_20260911.json`).


### 재현 자료

```bash
LONGSHORT_TEST_SERVER=/tmp/longshort_module_server/build/longshort-server \
LONGSHORT_TEST_CONFIG=/tmp/longshort_engine_http_20260911/config.yaml \
/tmp/longshort_engine_http_20260911/http_tests --gtest_color=no
python3 /tmp/longshort_engine_http_20260911/check_ga_disconnect.py
```

이 세션의 결과는 `/tmp/longshort_engine_http_20260911/results.json`과 `ga_lifecycle_results.json`에 저장했다. temp fixture/도구는 배포 의존성이 아니다. 공식 CMake 전체 시험 결과는 빌드 담당의 최종 기록과 함께 취합한다.


### 최종 통합 결과

빌드 담당의 최신 전체 CMake/CTest 실행은 **25/25 targets, C++ 184개 + Python 36개, 실패·skip 0**이다. 이번 범위의 HTTP 13개(D0/D1 포함), serializer 10개(유한 float 극값 포함), combined 9개(raw overflow 포함), modules 14개(CS mask 포함)가 모두 통과했다. 최종 결과 파일은 `/tmp/longshort_full_tests_final_20260911.json`과 `.xml`이다.

최종 frontend LRU·Save 우선순위·signal 선택 계약의 독립 리뷰에서 미해결 차단 사항은 없었다. `loadedFields` 타입 불일치는 정정됐다. source와 문서는 미커밋 상태이며 루트 담당이 통합·커밋한다.
