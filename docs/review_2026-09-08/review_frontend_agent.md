# frontend 숫자 파이프라인 정합 리뷰

- 대상: /home/jgo/longshort_platform (main@6a70f37), 정적 분석만. 빌드/실행 안 함.
- 서버 ground truth는 cpp-engine 소스를 직접 대조해 확인함(추정 아님). 확인 못한 부분은 [미검증] 표기.

## 0. 결론 요약

| 등급 | 건수 | 핵심 |
|---|---|---|
| P0 | 2 | frontend/lib 6개 모듈 부재(빌드 불가), 테스트 픽스처 경로 삭제됨 |
| P1 | 5 | MDD 정의 불일치, 멀티알파/GA 시그널 3패널 영구 공백, simStart 무효, e2e stale |
| P2 | 13 | 라벨 오표기·툴팁 stale·상태 리셋 누락·미사용 서버 필드 등 |

- **단위(×100) 중복/누락은 사실상 없음.** 422cbca의 turnover pp 수정 이후 summary(pp)/yearly(pp)/timeseries(fraction·pp 혼합) 표시 경로는 §2 표대로 전부 일치함. 남은 단위 문제는 `dailyTvr` 단위 표기 누락(F13) 하나뿐임.
- 실제 잔존 버그는 단위가 아니라 **정의(MDD additive vs ratio)**·**필드 라우팅(batch 안 payload를 top-level에서 찾음)**·**데드 코드/데드 필드**에 몰려 있음.

## 1. 검증 방법과 미검증 범위

- 서버 규약은 다음 파일을 직접 읽어 확정함: `src/api/json_serializer.cpp`, `src/metrics/{timeseries_output,summary_metrics,yearly_metrics,metrics_engine}.cpp`, `src/engine/simulator.cpp`, `src/main.cpp`.
- **미검증**: `frontend/lib/{uplotUtils,uplotAdapters,themeColors,metricColors,snapshotColors,colorScale}.ts`가 워킹트리·git 양쪽에 없음(F01). 따라서 `toMultiSeriesData`/`toAlignedData`/`dateStrToUnix`의 실제 변환, `signalToColor`의 색 매핑, `getMetricCellColor`의 임계값은 이번 리뷰에서 확인 불가. 해당 함수 내부에 단위 버그가 있어도 잡히지 않음.

## 2. 단위 대조표 (항목 1)

서버 단위 → 프론트 표시. `pp` = ×100 완료된 값.

| 필드 | 서버 단위 (근거) | 프론트 표시 (근거) | 판정 |
|---|---|---|---|
| summary.return(=ann_ret) | pp (json_serializer.cpp:17) | `formatPnl` `+X.X%` (AlphaRankingTable.tsx:13-18,72) | OK |
| summary.mdd | pp, 음수 (json_serializer.cpp:19) | **어디에도 표시 안 함** | 데드(F20) |
| summary.turnover | pp (json_serializer.cpp:20) | `.toFixed(1)+'%'` (AlphaRankingTable.tsx:75) | OK |
| summary.maxWeight | pp (json_serializer.cpp:21) | `.toFixed(1)%` (AlphaRankingTable.tsx:69) | OK |
| summary.avgMaxBet | pp (json_serializer.cpp:29) | `.toFixed(1)+'%'` (AlphaRankingTable.tsx:68) | OK |
| summary.minBet | **평균 종목수(count)** (summary_metrics.cpp:85-93) | `.toFixed(2)`, 헤더 "avgBet" (AlphaRankingTable.tsx:41,67) | 값 OK / BacktestMetrics는 오표기(F11) |
| summary.minBetMin | 최소 종목수(count) (summary_metrics.cpp:160-169) | `.toFixed(2)`, 헤더 "minBet" (AlphaRankingTable.tsx:44,70) | OK |
| summary.nonzeroPosition | **포지션 보유 bar 개수** (summary_metrics.cpp:106-114) | 헤더 "Alpha"로 raw 출력 (BacktestMetrics.tsx:71,111) | 오표기(F11) |
| summary.sharpe/sharpe80/quality/margin | 그대로 | `.toFixed(2)`/`.toFixed(4)` | OK |
| summary.longRatio/shortRatio/winRate/minsoc/sharpe400 | fraction·pp | 매핑만 하고 미표시 (api.ts:58-60,165-166) | 데드(F20) |
| yearly.pnl / annRet / maxDD | pp (yearly_metrics.cpp:67,74,106) | `.toFixed(1)%` (DetailSummaryTable.tsx:45,46,48) | OK |
| yearly.longSM / shortSM | fraction, Σ\|w\|=1 정규화 (simulator.cpp:66-71 → yearly_metrics.cpp:150-151) | `(v*100).toFixed(1)%` (DetailSummaryTable.tsx:43-44) | OK |
| yearly.dailyTvr | fraction (yearly_metrics.cpp:127) | `(v*100).toFixed(1)`, **단위 없음** (DetailSummaryTable.tsx:49) | F13 |
| yearly.sharpe / margin | 그대로 | OK |
| ts.cumPnl | fraction, additive (timeseries_output.cpp:44,55) | diff→ret→×capital→USD (api.ts:96-102, DetailTimeSeries.tsx:95) | OK |
| ts.cumPnlGross | fraction (timeseries_output.cpp:47-48,56) | **미사용**, turnover로 재계산 (DetailTimeSeries.tsx:81-84) | F10 |
| ts.turnover | pp (timeseries_output.cpp:57) | 차트 `%` (DetailTimeSeries.tsx:141,481) / gross 계산 시 `/100` (81-83) | OK |
| ts.maxWeight | pp (timeseries_output.cpp:59) | 차트 `%` (DetailTimeSeries.tsx:144,520) | OK |
| ts.nonzeroCount | 정수 (timeseries_output.cpp:58) | `.toFixed(0)` (DetailTimeSeries.tsx:145) | OK |
| ts.drawdown | **fraction, additive** (timeseries_output.cpp:50-52) | **미파싱**, 프론트가 ratio로 재계산 (DetailTimeSeries.tsx:104-114) | **F03** |

## 3. 발견 표

| ID | 심각도 | 파일:줄 | 증상 | 재현 | 수정안 | 확신도 |
|---|---|---|---|---|---|---|
| F01 | P0 | `.gitignore:17`; `frontend/components/visualization/DetailTimeSeries.tsx:8,18,19`, `SignalChart.tsx:9,10,18,19`, `SignalHeatmap.tsx:6,7`, `BacktestMetrics.tsx:5`, `DetailSummaryTable.tsx:5`, `multi-alpha/AlphaRankingTable.tsx:5`, `SummaryMetrics.tsx:5` | `@/lib/{uplotUtils,uplotAdapters,themeColors,metricColors,snapshotColors,colorScale}` 6개 모듈이 워킹트리·git 양쪽에 없음. python 템플릿 gitignore의 `lib/` 규칙이 `frontend/lib/`도 삼킴(`git check-ignore -v frontend/lib/uplotUtils.ts` → `.gitignore:17:lib/`). `api.ts`만 force-add된 상태(`git log --diff-filter=A -- 'frontend/lib/*'` → 8563e14 api.ts 단독) | clean clone 후 `npm run build` / `tsc --noEmit` / `vitest run` → 모듈 해석 실패 | `.gitignore`에 `!frontend/lib/` 예외 추가(또는 `lib/`를 `/lib/`로 앵커) 후 6개 모듈 커밋 | 높음 |
| F02 | P0 | `frontend/__tests__/pipelineFixtures.ts:14-16` | 픽스처를 `../../python-engine/tests/fixtures/pipeline/*`에서 읽는데 python-engine이 6a70f37에서 통째로 삭제됨 | `vitest run` → `BacktestMetrics/SummaryMetrics/DetailSummaryTable/AlphaRankingTable` 렌더 테스트 4종이 모듈 로드 단계에서 ENOENT | 픽스처 JSON을 `frontend/__tests__/fixtures/`로 옮기고 경로 수정 | 높음 |
| F03 | P1 | `DetailTimeSeries.tsx:104-114`(재계산), `421-433`(분할), `api.ts:104-134`(drawdown 미파싱) ↔ `timeseries_output.cpp:50-52`, `summary_metrics.cpp:65-73`, `yearly_metrics.cpp:97-107` | 엔진 drawdown은 **additive** `cumPnL − peak`인데 프론트 차트는 **ratio** `equity/peak − 1`. 같은 화면의 Yearly "Max DD"(additive pp)와 "Drawdown" 차트가 서로 다른 정의. 누적수익이 클수록 차트가 (1+peak)배만큼 축소 표시 — IS 누적 +947%면 −10pp 낙폭이 −0.96%로 보임 | 누적 PnL이 큰 알파에서 Detail→Drawdown 탭과 Yearly Max DD 열 비교 | `dd = (cumRet − peakCumRet)*100`으로 교체, 또는 `buildSplitSeriesFromTimeseries`에서 `ts.drawdown`을 슬라이스해 ×100해 그대로 사용(서버가 이미 계산함) | 높음 |
| F04 | P1 | `api.ts:194-196`, `api.ts:224`, `types/index.ts:58-66` ↔ `main.cpp:238,456-517,565-570,598-640` | 멀티알파에서 히트맵/시그널차트/분포가 **영구 null**. 원인 2중: (a) 서버는 signalPlot/signalRange/distribution을 **batch 객체 안**에 넣는데(main.cpp:565-570) 프론트는 top-level만 읽음, 타입에도 필드 없음. (b) `runSimulate`가 `collectSignals`를 안 보내고 서버 기본값이 `false`(main.cpp:238)라 애초에 계산도 안 됨 | 알파 2개 이상 XML로 Run → multi 뷰의 SignalDistribution/SignalHeatmap/SignalChart 3패널 전부 빈 화면(page.tsx:117-122) | `runSimulate` body에 `collectSignals:true, collectSignalsTopK:N` 추가 + `StarBatchEntry`에 3필드 추가 + 어댑터가 선택된 batch의 payload를 쓰도록 변경 | 높음 |
| F05 | P1 | `api.ts:200-202`, `useBacktest.ts:64-69,127-134` ↔ `main.cpp:822-830` | GA complete 이벤트에는 signalPlot/signalRange/distribution이 아예 없음 → 같은 3패널 공백. `useBacktest.ts:64-69`의 첫 셀 자동선택도 run 경로에만 있고 GA 경로엔 없음 | GA 실행 완료 후 화면 | 서버 GA complete에 signal payload 추가하거나, GA 종료 후 best XML로 `/api/simulate` 1회 재호출 | 높음 |
| F06 | P1 | `SettingsBar.tsx:30-41`, `api.ts:224` ↔ `main.cpp:472,601,707` | `simStart`가 **어떤 숫자에도 영향 없음**. 서버는 signal plot 시작 인덱스에만 사용하고 metric/timeseries는 항상 전체 데이터 구간. GA 경로에선 캡처만 하고 미사용(main.cpp:707). UI는 "Sim"을 IS/OS와 나란히 놓아 시뮬 구간처럼 보임 | Sim 날짜만 바꿔 Run → 모든 metric·PnL 차트 완전 동일 | 라벨을 "Plot start"로 바꾸거나, 서버가 simStart로 metric 구간을 자르도록 수정 | 높음 |
| F07 | P1 | `tests/e2e/metrics_pipeline.spec.ts:95,100` ↔ `AlphaRankingTable.tsx:147-149` | e2e가 `hasText:'alpha_0'`로 행을 찾는데 랭킹 테이블은 `String(item.rank-1)`(=`0`,`1`)만 렌더. 3c185be에서 유닛 테스트만 갱신되고 e2e는 방치 | `npm run test:e2e` | ID 셀 텍스트(`'0'`,`'1'`) 기준으로 셀렉터 교체 | 높음 |
| F08 | P2 | `store/alphaStore.ts:155-160` (GA 분기 147-153과 비교) | 일반 run 완료 시 `selectedAlphaIndices`만 비우고 `selectedAlphaIndexSet`은 리셋 안 함 → 재실행 후 체크박스는 켜져 보이는데(`AlphaRankingTable.tsx:117`) 선택 카운트는 0, Save 버튼은 비활성(`ControlButtons.tsx:17-19`) | 멀티 결과에서 몇 개 체크 → 다시 Run | 155-160 반환 객체에 `selectedAlphaIndexSet: new Set<number>()` 추가 | 높음 |
| F09 | P2 | `DetailTimeSeries.tsx:124-131`, `341-346` | `mergeIsOsSeries`가 OS 첫 날짜 슬롯에 **전날 IS 값**을 `isValue`로 복사(선 연결용). 툴팁이 OS 첫날에 "IS: <전날 값>"을 같이 띄움 → 그 날짜에 존재하지 않는 숫자를 표시 | Turnover/MaxWeight/NonZero/MDD 차트에서 OS 시작일에 커서 | 연결용 포인트는 렌더만 하고 툴팁에서 제외(경계 인덱스 플래그) | 높음 |
| F10 | P2 | `DetailTimeSeries.tsx:81-84,403` | 서버가 `cumPnlGross`를 이미 주는데 프론트가 `ret + (turnover/100)*feeRate`로 재계산. 수식은 서버(`timeseries_output.cpp:47`)와 동일하지만 `/api/config` 실패 시 feeRate가 0.001로 fallback되어 서버 실제 fee와 어긋남 | config 엔드포인트 실패 상태에서 PnL 차트 gross 선 | `buildSplitSeriesFromTimeseries`에서 `cumPnlGross`도 슬라이스해 그대로 사용 | 높음 |
| F11 | P2 | `BacktestMetrics.tsx:69,71,109,111,118` | 헤더 오표기 2건: "MinBet"←`minBet`(실제 **평균** 종목수), "Alpha"←`nonzeroPosition`(실제 **포지션 보유 bar 수**). 같은 필드를 `AlphaRankingTable.tsx:41`은 "avgBet"으로 불러 컴포넌트 간 라벨 충돌. 단 **BacktestMetrics/SummaryMetrics는 앱 어디에도 마운트되지 않음**(page.tsx에 미등장, 테스트만 참조) | 코드 리딩 | 헤더를 avgBet/nonZeroBars로 교정하거나 데드 컴포넌트 삭제 | 높음 |
| F12 | P2 | `DetailSummaryTable.tsx:40-52` ↔ `metrics_engine.cpp:46` | `compute_yearly`가 IS/OS를 무시하고 **전체 데이터 기간**에 대해 계산 → IS 시작 이전 연도 행이 그대로 노출. PnL 차트는 IS 시작부터 그려지므로 해당 연도가 서로 안 맞음 | isStart=2023, 데이터 2020~ → Yearly에 2020/2021/2022 행 존재 | IS 시작 이전 연도는 회색 처리하거나 서버에서 구간 절단 | 높음 |
| F13 | P2 | `DetailSummaryTable.tsx:49` | `(dailyTvr*100).toFixed(1)`인데 `%` 없음. 같은 표의 다른 % 열과 혼동 | Yearly 표 "Daily TVR" 열 | `%` 접미사 추가 | 높음 |
| F14 | P2 | `BacktestMetrics.tsx:75-76`, `AlphaRankingTable.tsx:63-77` ↔ `metrics_engine.cpp:62-66`, `summary_metrics.h:7-26` | OS 구간에 bar가 없으면 서버가 `compute_summary_range`를 호출하지 않고, `SummaryMetrics` 전 필드가 `= 0` 기본값이라 **전 필드 0**으로 나감. 프론트는 `isFiniteNumber(0)===true`라 "+0.0%"/"0.00"으로 표시 → "데이터 없음"과 구분 불가 | osStart를 데이터 끝 이후로 설정하고 Run | 서버가 null/flag를 주거나, 프론트가 `nonzeroPosition===0 && pnl===0`을 결측으로 간주해 `-` 표시 | 중간 |
| F15 | P2 | `api.ts:152`, `AlphaRankingTable.tsx:104`, `AlphaDetailView.tsx:44` | `resp.combined` 부재 시 `batches[0]`을 'combined'로 라벨(silent fallback) → 최상위 단일 알파가 "Combined"로 표시될 수 있음. 라벨 "batch 0"/"Combined (batch 0)"도 부정확 — 실제 combined는 전 알파 시그널 합산 재시뮬(main.cpp:519-535)이지 batch 0이 아님 | 서버가 combined를 안 보내는 응답 | fallback 제거(없으면 combined=null) + 라벨을 "Combined (Σ signals)"로 | 높음 |
| F16 | P2 | `app/page.tsx:86-89` ↔ `AlphaDetailView.tsx:33` | combined 상세 진입은 `selectedAlphaIndex=-1`인데 XML 패널은 `alphas[-1]`을 조회 → 항상 빈 패널 | 랭킹 테이블 "batch 0" 행 클릭 | `idx===-1`이면 combined 처리(또는 패널 숨김) | 높음 |
| F17 | P2 | `hooks/useBacktest.ts:122` ↔ `SettingsBar.tsx:124` | GA 진행률 `evaluated`가 항상 0 하드코딩 → 진행 표시가 "N : 0/50"으로 고정 | GA 실행 중 상단 우측 | 서버 generation_end에 evaluated를 싣고 반영, 또는 표시 제거 | 높음 |
| F18 | P2 | `SignalHeatmap.tsx:284-289,385` ↔ `simulator.cpp:66-71,78-88` | 부호 규약 반전 의심. 엔진은 `target = signal/Σ\|signal\|`이고 수익은 `prev_w*(close/prev_close−1)` → **양수 시그널 = 롱**. 그런데 툴팁은 `value>0`을 `text-metric-negative`(빨강), 주석은 "positive = short bias (bearish)"로 명시 | 히트맵 셀 호버 | 색·주석을 양수=롱으로 교정. 히트맵 본체 색은 `colorScale.ts` 부재로 [미검증] — 같은 반전이 있는지 확인 필요 | 중간(엔진측 높음, 색 매핑 미검증) |
| F19 | P2 | `api.ts:121-133`(미생성) ↔ `DetailTimeSeries.tsx:443-446,456-459,499-510` | `isMinBetSeries`/`osMinBetSeries`를 어댑터가 한 번도 채우지 않음 → minBet 차트는 항상 빈 차트. 현재 `AlphaDetailView.tsx:8-14` METRICS에 노출 안 돼 도달 불가(데드) | 코드 리딩 | 서버 timeseries에 min_bet이 없으므로 차트 분기 자체를 삭제하거나 서버에 필드 추가 | 높음 |
| F20 | P2 | `api.ts:104-134,194-196` | 서버가 계산해 보내는데 화면에 전혀 안 나오는 값: `ts.drawdown`, `ts.cumPnlGross`, `distribution.bins/min/max`, `summary.{mdd,winRate,quality,minsoc,sharpe400,longRatio,shortRatio}`, `isEquitySeries/osEquitySeries`(생성만 하고 미소비), `config.barsPerYear`(types/index.ts:85에 타입만) | 코드 리딩 | 표시하거나 어댑터에서 제거해 계약 축소 | 높음 |
| F21 | P2 | `SignalDistribution.tsx:27-29,35-48` ↔ `json_serializer.cpp:138,150-153,168-173`, `main.cpp:605-620` | x축 라벨은 `signalRange.min/max`(**모든 유한값** 기준)인데 히스토그램 bin 범위는 `distribution.min/max`(**\|v\|>1e-12만** 기준). 0이 극단값인 시그널(예: 전량 양수 + 0 다수)에서 라벨과 실제 bin 지지구간이 어긋남. bins 배열(중심값)은 버리고 counts만 씀(api.ts:196) — 등간격이라 막대 배치 자체는 정확 | 0을 많이 포함한 단측 시그널 | 라벨을 `distribution.min/max`로 바꾸거나 bins 배열로 축을 그림 | 중간 |
| F22 | P2 | `SignalHeatmap.tsx:135-140,158-208,211` | 주식 스케일에서 렌더 비용 폭발: `fillRect`가 T×N회(4000×500 = 200만), 컬럼마다 정렬 4000회, 마지막에 `getImageData`로 4000×1000px ImageData(≈64MB)를 캐시 | 심볼 수백 이상 데이터 연결 시 | 오프스크린 ImageData 직접 픽셀 쓰기 + 컬럼 다운샘플링 | 중간(추정 규모는 산식 기반) |

## 4. 항목별 결론

**항목 2 — `buildSplitSeriesFromTimeseries` 인덱스 정합: 정상.**
서버 `is_start_index = max(is_idx,1)−1`, `os_start_index = os_idx−1`은 **timeseries 좌표계에서 각 구간의 첫 bar를 가리킴**(`ts[k]=raw[k+1]`이므로 ts 인덱스 `is_for_summary−1` ≡ raw `is_for_summary`). 따라서 `api.ts:113-116`의 `slice(isStart, osStart)` / `slice(osStart)`는 서버 IS 요약 구간 `raw[is_for_summary, os_idx)` / OS `raw[os_idx, T)`와 **정확히 일치**함. 경계 원소(첫 OS bar)는 양쪽 다 OS에 들어감.
`cumPnlToDailyReturns`의 `prevCumPnl`(api.ts:118-119)도 맞음 — `cumPnl[isStart]−cumPnl[isStart−1] = ret[raw is_for_summary]`로 IS 첫 bar의 일수익이 정확히 나옴. `isStart===0`이면 `prev=0`이고 `cumPnl[0]=ret[1]`이라 역시 정확함. OS가 없을 때(`os_idx===T` → `os_start_index===T−1===upper`) `osStart=upper`로 클램프되어 빈 OS + `osStartDate===undefined`가 되고, 소비처(`DetailTimeSeries.tsx:426`, `169`)가 undefined를 방어함.

**항목 3 — 파생계산: 복리 잔재는 MDD 한 곳.**
PnL/equity는 additive 규약대로임(`cumRet += r`, `net = cumRet*capital`, `equity = capital + cumRet*capital`; DetailTimeSeries.tsx:86,95,98). rolling sharpe류 파생은 프론트에 없음. **`buildMddDataFromEquity`만 ratio 방식**이라 엔진과 불일치(F03). 서버 `drawdown` 필드는 파싱조차 안 함.

**항목 4 — GA 경로:** stale 데이터는 없음(`useBacktest.ts:95`에서 `setResult(null)` 선행, 그 시점 `gaProgress`가 아직 null이라 `alphaStore.ts:155-160` 일반 분기를 타 viewMode도 리셋됨). 대신 **combined 없음 → `single=true` → combined=null**로 정상 동작하고, 문제는 signalPlot 부재로 인한 3패널 공백(F05)과 evaluated 고정(F17)임. `alphas[0].xml = event.bestXml`(useBacktest.ts:130)은 서버 batch.xml과 동일 값이라 무해함.

**항목 5 — multi-alpha 정렬:** 서버가 IS sharpe desc로 `stable_sort`(main.cpp:420-433)한 순서를 어댑터가 그대로 `rank=i+1`로 부여(api.ts:171-188)하고, UI도 배열 순서대로 렌더(AlphaRankingTable.tsx:191-193)함 → **정렬·인덱스 혼동 없음**. `enterDetailView(index)`/`toggleAlphaSelection(index)`가 쓰는 index는 모두 `alphas` 배열 위치로 일관되고, combined만 `-1` 센티넬(AlphaRankingTable.tsx:98 ↔ AlphaDetailView.tsx:33)로 분리돼 있어 충돌 없음. 표시 ID는 `rank−1`(0-based)이고 원래 batch 순번은 `alpha_${batch.index}` 안에만 남아 UI에 안 보임 — 원본 순번을 보고 싶으면 별도 열 필요. `sortedBy`는 어댑터가 채우지 않아 "sortedBy=" 표시가 항상 빈 문자열(AlphaRankingTable.tsx:158,181).

**항목 6 — distribution:** bins는 등간격 중심값이라 counts만 써도 막대 배치는 정확함. 문제는 x축 라벨 출처가 다른 통계(F21).

**항목 7 — 날짜 검증:** `enforceSettingsDates`(alphaStore.ts:81-109)는 clamp → `simStart>isStart`면 simStart를 내리고 → `isStart>=osStart`면 osStart를 다음날로 미는 순서라 불변식 `simStart ≤ isStart < osStart`를 항상 만족함. 다만 (a) osStart를 미는 `nextDay`가 `maxDate`를 넘으면 clamp되어 다시 `isStart===osStart`가 될 수 있음(isStart가 maxDate일 때) → `enforceSettingsDates` 재적용 없이 그대로 반환됨, (b) SettingsBar의 IS 입력 `min`이 `config.simStart`라 UI상 `isStart < simStart` 입력이 가능하고 스토어가 사후에 simStart를 끌어내림(SettingsBar.tsx:49-50 ↔ alphaStore.ts:103) — 사용자가 안 건드린 Sim 값이 조용히 바뀜. 둘 다 P2 수준.

## 5. 주식 데이터 연결 대비 하드코딩 목록 (항목 8)

심볼 수백~수천, T≈4000, barsPerYear 252 가정.

| 파일:줄 | 하드코딩 | 주식 전환 시 영향 |
|---|---|---|
| `store/alphaStore.ts:70-72` | 기본 날짜 `2020-01-01/2023-01-01/2024-01-01` | config fetch 실패 시 이 값으로 요청 → 주식 패널 범위 밖일 수 있음 |
| `store/alphaStore.ts:226` | out-of-range 시 IS/OS 70/30 **캘린더일** 분할 | 거래일 기준이 아니라 휴장일 포함, 252 규약과 어긋남 |
| `store/alphaStore.ts:74-75` | `nJobs:5`, `gaPopulation:50` | 스레드 수는 서버 코어에 맞춰야 함 |
| `store/alphaStore.ts:248`, `SignalChart.tsx:443` | 스냅샷 상한 8 | — |
| `hooks/useBacktest.ts:114` | GA generations `20` 고정(UI 노출 없음) | 탐색공간 커지면 조정 불가 |
| `hooks/useBacktest.ts:122,103` | `evaluated: 0` 고정 | F17 |
| `components/visualization/DetailTimeSeries.tsx:41` | `DEFAULT_CAPITAL = 100000` | PnL($) 축이 항상 명목 10만 |
| `components/visualization/DetailTimeSeries.tsx:403` | `feeRate ?? 0.001` | 주식 수수료/세금과 다름, config 실패 시 gross 왜곡 |
| `components/visualization/SignalChart.tsx:265-266` | 초기 뷰포트 **최근 365 bar** | 252 규약에서 1.45년치가 기본 노출 |
| `components/visualization/SignalChart.tsx:29,55-56` | row 높이 18px 가정 스크롤 | T=4000행 테이블 — 스크롤 위치 계산은 되나 DOM 4000행 |
| `components/visualization/SignalHeatmap.tsx:69` | 주석 "17 symbols max" → `filteredSymbols`를 **useMemo 없이** 매 렌더 필터 | 심볼 수천이면 렌더마다 O(N) 문자열 비교 |
| `components/visualization/SignalHeatmap.tsx:9-10,135-136` | `MIN_CELL_WIDTH=1`, `MIN_CELL_HEIGHT=2` | N=500이면 캔버스 높이 1000px, T=4000이면 폭 4000px |
| `components/visualization/SignalHeatmap.tsx:158-208,211` | T×N `fillRect` + 컬럼별 정렬 + 전면 `getImageData` | F22 (200만 rect, ≈64MB ImageData) |
| `components/visualization/SignalHeatmap.tsx:350` | 컨테이너 `max-h-48`(192px) | 심볼 500개면 셀 높이 2px로 스크롤, 라벨 없음 |
| `components/visualization/SignalDistribution.tsx:34-48` | bin 개수를 서버(50)에 위임, 높이 `h-8` | 문제 없음 |
| `components/visualization/BacktestMetrics.tsx:92`, `app/page.tsx:147` | 표 높이 `h-32` / `max-h-[40%]` | 알파 수 많으면 스크롤만 |
| `components/visualization/SettingsBar.tsx:94-95,110-111` | nJobs 1~32, population 10~500 | — |
| `types/index.ts:85` ↔ 프론트 전역 | `config.barsPerYear`를 받기만 하고 **어디서도 안 씀** | 252 전환 시 프론트가 반영할 지점 자체가 없음 |
| `lib/api.ts:16` | `API_BASE` 기본 `http://localhost:8000` | 배포 시 env 필요 |
| (서버) `src/main.cpp:203,294,307,320,533,544,741,753,791,803` | `bars_per_year = 365` **10곳 하드코딩**, `/api/config`도 365 고정 | **주식 연결 시 필수 수정 지점**. sharpe·ann_ret·yearly 전부 √(365/252)=1.20배 과대 |
| (서버) `src/main.cpp:234-236,671-672` | 요청 기본 `isStart 2021-01-01 / osStart 2024-01-01`, `simStart ""` | 프론트가 항상 명시 전송하므로 현재는 무해 |
| (서버) `src/metrics/summary_metrics.h:5` | `constexpr unsigned BARS_PER_YEAR = 365` | 전 metric 기본값의 뿌리 |
| (서버) `src/metrics/yearly_metrics.h:24`, `metrics_engine.h:31` | `bars_per_year = 365` 기본 인자 | 동일 |

## 6. 테스트 공백

| 공백 | 근거 | 위험 |
|---|---|---|
| 렌더 테스트 4종이 **실행 불가** | `pipelineFixtures.ts:14-16` → 삭제된 python-engine (F02) | 표시 포맷 회귀를 전혀 못 잡음 |
| `uplotAdapters.test.ts`가 **삭제된 함수**를 테스트 | `:6,45-55` `toIsOsAlignedData` — 422cbca에서 사용처 제거됨. 게다가 `@/lib/uplotUtils` 자체가 부재(F01) | 파일 전체가 실행 불가 |
| e2e가 **구 UI 셀렉터** 사용 | `metrics_pipeline.spec.ts:95,100` (F07) | e2e 상시 실패 → 그린 신호 자체가 없음 |
| 어댑터 테스트의 **단위가 서버와 다름** | `adaptSimulateResponse.test.ts:11-12` — `turnover:[0.1,0.2,...]`(fraction)와 `maxWeight:[20,30,...]`(pp)가 한 픽스처에 혼재. 서버 turnover는 pp | 422cbca류 100배 오류를 픽스처가 못 잡음 |
| **MDD 정의**를 ratio로 고정하는 테스트 | `DetailTimeSeries.derived.test.ts:50-60`이 `98980/101000−1 = −2%`를 단언 | F03을 고치면 이 테스트가 깨짐 — 엔진 additive 기준으로 재작성 필요 |
| `ts.drawdown` / `cumPnlGross` 검증 없음 | 픽스처에 값은 있으나(`adaptSimulateResponse.test.ts:10,14`) 단언 없음 | 서버 필드 미사용(F20)이 드러나지 않음 |
| 멀티알파 batch 내 signal payload 테스트 없음 | 어댑터가 못 읽으므로 테스트도 없음 (F04) | 3패널 공백이 회귀 아닌 "원래 그런 것"으로 굳음 |
| GA complete 경로의 heatmap null 단언 없음 | `useBacktest.pipeline.test.tsx`는 adaptSimulateResponse를 mock (F05) | GA 후 UI 공백 미검출 |
| `enforceSettingsDates` 경계 미검증 | `enforceSettingsDates.test.ts` 8케이스 중 `isStart===maxDate`(osStart를 밀 수 없는 경우) 없음 (§4 항목7-a) | 불변식 위반 케이스 통과 |
| `selectedAlphaIndexSet` 리셋 테스트 없음 | `alphaStore.test.ts`는 fetchConfig 중심 (F08) | — |
