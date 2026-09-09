# cpp-engine 손익·메트릭 경로 적대적 리뷰

- 대상: `/home/jgo/longshort_platform/cpp-engine` (main@6a70f37), 정적 분석 전용(빌드 미실행)
- 수치 검증: `/home/jgo/usr/venv-3.11/bin/python`으로 시뮬레이터 루프 재현 및 항등식 확인(부록 B)
- 라벨 규약: **버그**(의도와 코드가 다름) / **설계가정**(의도된 모델 단순화) / **규약차이**(팀 시뮬레이터와의 차이)

---

## 0. 결론 요약

| # | 판정 | 한 줄 |
|---|------|------|
| 1 | 설계가정 | trading leg(Δpos·tr) 누락은 "체결가=종가" 가정 하에서 tr≡0이므로 **자체 정합**. 버그 아님 |
| 2 | 규약차이 | 다만 그 값은 정확히 `½×(1바 알파 감쇠)`이며 무시할 수 없음 (부록 B에서 항등식 확인) |
| 3 | **규약차이(중대)** | 프론트 기본 XML `<data expr="close" delay="1"/>` + mode=1 = **이중 지연**. 수익 인식 바 기준 정보 시점이 팀 규약보다 **1바 더 낡음** |
| 4 | **버그** | 멀티알파 top-K signalPlot 경로에서 동일 모듈 객체 2회 실행 → `init()`이 상태를 리셋하지 않아 신호 플롯이 오염 |
| 5 | 버그 | `min_bet`/`min_bet_min` 계약(=min_bet 시리즈)과 코드(=nonzero_count) 불일치. 시뮬레이터의 `min_bet` 시리즈는 계산만 되고 **전혀 쓰이지 않음** |

---

## 1. 섹션 1 — Q1/Q2 판정

### 1.1 Q1. 타이밍 구조 (di 인덱스 추적)

**상태 갱신 규칙** (`src/engine/simulator.cpp:114-120`, mode=1)

```
루프 di 시작 시점 상태:  prev_weights = A_di ,  prev_target = B_di
루프 di 종료 시:         prev_weights ← B_di   (swap)
                         prev_target  ← target_di
∴  B_di = target_{di-1},   A_di = B_{di-1} = target_{di-2}      (A_0=A_1=B_0=0)
```

수치로 확인: `prev_weights[t] == target[t-2]` 전 구간 일치 (부록 B-1, `np.allclose` True).

**mode=1, `<data expr="close" delay="d"/>` 조합 추적표**

| di | 사용 데이터 시점 (signal) | target_di 근거 | exec_w (=B_di) | prev_weights (=A_di) | ret[di] 산식 |
|----|--------------------------|----------------|----------------|----------------------|--------------|
| 0 | close_{0-d} → NaN | 0 | 0 | 0 | 0 (tvr=0, fee 0) |
| 1 | close_{1-d} | target_1 | target_0 | 0 | `−|target_0−0|·fee` (최초 진입 수수료) |
| 2 | close_{2-d} | target_2 | target_1 | target_0 | `Σ target_0·(c_2/c_1−1) − |target_1−target_0|·fee` |
| t | close_{t-d} | target_t | target_{t-1} | target_{t-2} | `Σ target_{t-2}·(c_t/c_{t-1}−1) − |target_{t-1}−target_{t-2}|·fee` |

**해석**: bar t 구간 `(c_{t-1}, c_t]`의 수익을 버는 가중치는 `target_{t-2}`이고, 그 근거 데이터는 **bar t−2−d**. 체결은 `close_{t-1}`(= exec_w가 다음 바부터 수익을 버는 시점)에서 이루어지고 수수료는 그 바에 부과된다.

**팀 규약 대응**

| 항목 | 팀 규약 | 엔진 mode=1, d=0 | 엔진 mode=1, d=1 (프론트 기본값) |
|------|---------|------------------|-------------------------------|
| `hr[t]`를 버는 가중치 | `pos[t−1]` | `target_{t−2}` | `target_{t−2}` |
| 그 가중치의 정보 시점 | **t−2** | **t−2** ✅ 일치 | **t−3** ❌ 1바 초과 |
| 체결 시점 | `mid_t` (당일 장중) | `close_{t−1}` | `close_{t−1}` |
| trading leg | `Δpos[t]·tr[t]` 존재 | 없음 (tr≡0) | 없음 |

- **`delay` 속성과 mode=1은 가산적으로 중첩된다 = 이중 지연이 맞음.** `delay` 속성은 MDelay 모듈로 신호 파이프라인 앞단에 삽입되고(`src/compiler/dag_builder.cpp:259-273`), mode=1은 체결 지연을 별도로 1바 더 준다. 총 정보→수익 지연 = `2 + d` 바.
- 프론트 기본 XML은 `frontend/store/alphaStore.ts:78`에서 `<data expr="close" delay="1"/>` → 기본 상태에서 이미 1바 손해.
- **팀 규약과 정확히 대응시키려면**: `delay` 속성 제거(=0) + `mode=1` (현재 main.cpp:293 고정값 그대로). 등가 조합으로 `delay=1` + `mode=0`도 동일한 수익 타이밍이지만 mode는 API로 노출되지 않는다.
- 잔여 차이는 체결가 뿐(종가 vs midPrice) → 그것이 Q2의 trading leg.

**라벨**: `delay=1` 기본값은 **규약차이**(look-ahead는 없으므로 버그 아님). 다만 사용자가 "delay=1이 팀 규약의 t−1 정보"라고 오해할 소지가 크고, 실제로는 t−3 정보다.

**손해 규모** (부록 B-3, halflife 5일 신호):

| 구성 | 연환산 gross | 팀 대비 |
|------|--------------|---------|
| 팀 규약 `½(μ_lag1+μ_lag2)` | 204.6% | — |
| 엔진 `delay=0` (= μ_lag2) | 191.2% | −6.5% |
| 엔진 `delay=1` (= μ_lag3) | 166.5% | **−18.6%** |

### 1.2 Q2. trading leg (Δw·tr) 판정

**(a) 엔진에 항이 없음 — 확인됨.** `src/engine/simulator.cpp:79-94` 전체가 수익 = `prev_weights · (close/prev_close − 1)`, 비용 = `turnover · fee` 두 항뿐이다. `Δw × 체결가수익` 항은 존재하지 않으며, DataPanel에 체결가(mid/vwap) 컬럼 개념 자체가 없다(`src/data/data_panel.h:13`은 컬럼명 문자열 맵이지만 시뮬레이터는 `"close"`만 하드코딩: simulator.cpp:58, 80).

**(b) 자체 정합성 — 정합함.** 체결가 = 종가이면 `tr[t] = close_t/close_t − 1 ≡ 0`이므로 `Δpos·tr ≡ 0`. 즉 엔진 산식은 "종가 체결" 모델의 **완전한** 표현이다. → **설계가정**, 버그 아님.

**(c) 언제 결과가 달라지는가 — 정확한 항등식**

`mid_t`를 당일 기하평균 중간가 `√(c_{t-1}·c_t)`로 놓으면 `tr[t] = c_t/mid_t − 1 ≈ ½·r_t`. 이때

```
E[trade leg] = E[Σ Δpos_t · tr_t] = ½ · ( μ(lag1) − μ(lag2) )
팀 총수익      = ½ · ( μ(lag1) + μ(lag2) )
엔진 총수익    =        μ(lag2)
```

여기서 `μ(lag k)` = 가중치를 k바 지연시켜 보유했을 때의 일평균 손익. 부록 B-3에서 수치로 4자리 일치 확인 (예측 13.389 vs 실측 13.385).

> **trading leg = 알파 1바 감쇠분의 정확히 절반.**

| 알파 성격 | trade leg 부호 | 엔진 결과 |
|-----------|----------------|-----------|
| 단기 자기상관 방향과 **같은 편**(정상 작동하는 리버설/모멘텀) | **+** | **과소평가** |
| 단기 자기상관 방향과 **반대 편**(부호가 틀린 알파) | **−** | **과대평가** |
| 신호가 느리게 감쇠(회전율 낮음) | ≈0 | 차이 없음 |

부호 직관: 리버설 알파는 `pos ∝ −r_{t−1}`, 시장이 반전(φ<0)이면 `pos_t`와 `r_t`가 양의 상관 → `Δpos·tr > 0`. 모멘텀 알파는 φ>0에서 같은 논리로 양수. 즉 **알파가 맞는 방향이면 항상 엔진이 손해를 본다.** 회전율이 높고 감쇠가 빠른 알파일수록 손해가 크다(부록 B-2: halflife 1일 알파에서 trade leg가 hold leg 전체와 맞먹음).

**(d) 추가 시 최소 변경 위치 (설명만)**

| 위치 | 필요 변경 |
|------|-----------|
| `SimulatorConfig` (`simulator.h:7-17`) | `std::string exec_price_col = "close";` 필드 추가. 기본값 "close"면 tr≡0이라 현재 결과와 **비트 단위 동일** → 무해한 확장 |
| `simulator.cpp:58` 근처 | `const float* exec_row = data_.row(config_.exec_price_col, di);` 추가 |
| `simulator.cpp:79-87` (수익 루프) | 항 추가: `+ (exec_w[ii] − prev_weights[ii]) * (close_row[ii]/exec_row[ii] − 1.0)`. 가드: `isfinite(exec_row[ii]) && exec_row[ii] > 0` 아니면 해당 종목 tr=0 |
| `simulator.cpp:89-94` (수수료) | 변경 불필요 — `turnover`가 이미 동일한 Δ를 쓰고 있어 `Δpos`와 정합 |
| `simulator.cpp:174-182` (`run_from_signals`) | 동일 항 중복 삽입 필요. **루프 2벌이 복붙 상태라 한쪽만 고치면 combined alpha가 조용히 어긋난다** → 공통 함수로 추출 권장 |
| `DataPanel` / config.yaml | `use_col`에 체결가 컬럼(mid/vwap/twap) 추가. 없으면 `open` 대용 → tr = close/open − 1 (당일 전체를 새 포지션이 먹는 극단 가정이므로 과대평가 주의) |
| `compute_timeseries` (`timeseries_output.cpp:47`) | `cum_pnl_gross` 복원식은 fee만 되돌리므로 변경 불필요 |

주의: mode=1에서 exec_w가 실제로 체결되는 바는 `di`이고 그 종목이 다음 바부터 hr을 번다. 따라서 위 항을 `di` 바에 더하면 팀 산식 `pos[t−1]·hr[t] + Δpos[t]·tr[t]`와 정확히 1:1 대응한다(`pos[t] ≡ exec_w[t] = target_{t−1}`).

---

## 1-B. Q3~Q7 질의응답

### Q3. 수수료 / 회전율

| 항목 | 판정 |
|------|------|
| fee 단위 | `config.yaml:17` `transaction_fee: 5 # per trade bps` → `config_loader.cpp:40-41`에서 `×1e-4` = 0.0005. plan.md:831 "편도 비용이며 turnover에 1회만 곱함". `turnover = Σ|Δw|`는 매도측·매수측 절대변화를 **모두** 더하므로 `fee·Σ|Δw|`는 총 거래대금 × 편도요율 = 표준. **정합** |
| turnover 정의 | `Σ|exec_w − prev_weights|` (simulator.cpp:90-93). gross=1 정규화이므로 이론 상한 2. plan.md:271 표와 일치 |
| 첫 진입 fee 위치 | **mode=1 → di=1**, mode=0 → di=0. mode=1에서 `ret[0]=0, turnover[0]=0` (exec_w=prev_target=0). timeseries는 bar0을 버리므로 최초 진입 비용은 차트/summary에 **포함**됨 (정상) |
| `cum_pnl_gross` 복원식 | `timeseries_output.cpp:47` `r_gross = r + tvr·fee_rate` — 시뮬레이터가 `ret -= tvr·fee_rate`만 뺐으므로 **정확**. 단 `fee_rate`가 시뮬레이터와 다른 값으로 전달되면 조용히 틀린다(호출부 main.cpp:307 등은 동일 값 사용, 현재는 OK) |
| 리밸런싱 없는 날 | raw signal이 완전히 동일하고 tradable 마스크도 동일하면 target 동일 → turnover=0. 그러나 **가격 드리프트로 인한 실제 리밸런싱 거래가 0으로 계산된다** (prev_weights가 목표비중 그대로 유지되고 실현비중으로 갱신되지 않음). 설계가정이지만 비용 과소계상: 17종목·일변동성 4% 가정 시 드리프트 회전율 ≈ `E|r_i − r_p| ≈ 0.032/일` → 5bps 기준 **연 0.58pp 비용 누락** [추정] |

### Q4. NaN close / 상장폐지 / 유니버스 이탈

정규화 게이트(`simulator.cpp:62,70`)는 `isfinite`, 수익 게이트(`simulator.cpp:82`)는 `prev_close > 0 && close > 0` — **서로 다른 조건**이다. 이것이 편향의 근원.

| 케이스 | target | prev_weights 보유분 | 수익 | turnover | 결과 |
|--------|--------|---------------------|------|----------|------|
| close 유한, signal NaN | 0 | 유지 | 정상 계산 | 다음 바에 청산비용 | 정상 |
| **close NaN, signal 유한** | **0** | **유지(0 아님)** | **0으로 처리(드랍)** | 다음 바에 청산비용 | **손익 소실** |
| close=0 (유한) | 0 아님(tradable) | — | 0 (게이트 탈락) | 정상 | 무수익 포지션 |
| close=+inf | 0 (isfinite 탈락) | 유지 | **inf > 0 통과 → ret=±inf** | — | cum_pnl 전체 오염 |

상폐 시퀀스 (bar k에서 NaN 시작):

```
bar k   : ret 항 드랍 (close_k = NaN)          ← [k-1, k] 구간 손익 소실
bar k+1 : ret 항 드랍 (prev_close = NaN)       ← [k, k+1] 구간 손익 소실
          exec_w = target_k = 0 → 청산 turnover 부과
bar k+2 : prev_weights = 0, 완전 이탈
```

- **주식 데이터에서의 편향**: 상장폐지 직전 급락 2바가 **항상 0으로 인식**된다. 손실만 비대칭적으로 잘려 나가므로 성과가 상향 편향된다. 유니버스 이탈(리밸런싱 탈락)도 같은 경로로 "무손실 청산"된다.
- 크립토(현 데이터셋)에서는 `parquet_reader.cpp:131-135`가 미존재 (symbol,time)을 NaN으로 채우므로 상장 전 구간은 정상적으로 weight 0이 되고, 상폐/거래정지 종목이 거의 없어 영향은 작다.
- NaN이 중간에 끼었다가 복귀하는 경우(데이터 결측)에도 결측 구간 손익이 통째로 사라지고 청산·재진입 수수료만 두 번 부과된다.

### Q5. 메트릭 정합 — 섹션 3 표 참조

핵심만: `min_bet`/`min_bet_min` 계약 위반(코드는 nonzero_count), 시뮬레이터의 `min_bet` 시리즈 완전 미사용, plan.md Phase 5 전체(복리 equity 기준)가 코드(단리)와 불일치, `win_rate`가 math_contract에 미기재, yearly 전체가 math_contract에 미기재, summary mdd와 timeseries drawdown의 peak 초기화 불일치. `is_start_index`/`os_start_index` ↔ 프론트 `buildSplitSeriesFromTimeseries`(`frontend/lib/api.ts:104-134`) 대조는 **일치**(ts index k = raw k+1, isStartIndex = max(is_idx,1)−1 → 슬라이스 시작 raw = max(is_idx,1)).

### Q6. combined alpha (`main.cpp:375-385`, `522-545`)

- **정규화 없는 raw 합산이 맞다** — `accumulate_combined_row`는 `c = isfinite(c) ? c+s : s`만 한다(main.cpp:378-384). 스케일 보정·표준화·개수 나눗셈 없음.
- 왜곡 실측 (N=17, 부록 B-4):

| 조합 | 순노출(net/gross) | long_ratio | 최대 종목비중 |
|------|-------------------|------------|---------------|
| `<rank/>` 단독 | **+1.000** | 1.000 | — |
| `<zscore/>` 단독 | 0.000 | 0.500 | 0.156 |
| `rank + zscore` 합산 | **+0.578** | **0.789** | — |
| `raw close + zscore` 합산 | — | — | **0.500** (BTC 1종목이 북의 절반) |

  즉 rank 계열 알파 1개만 섞여도 결합 포트폴리오가 **강한 순매수 방향성**을 얻고, 정규화되지 않은 raw 컬럼 알파는 절대 스케일로 북을 지배한다. 합산 전 `Σ|s|` 정규화 또는 z-score 표준화가 없으면 "결합"이 사실상 "가장 스케일 큰 알파 선택"이 된다.
- **NaN 처리**: 한 알파만 유한이면 그 값만 사용 = pandas `sum(skipna=True)` 동작. 평균이 아니므로 warmup 구간(긴 window 알파가 아직 NaN)에는 짧은 window 알파만으로 구성된 전혀 다른 포트폴리오가 된다. 알파 개수로 나누지 않기 때문에 커버리지가 낮은 종목이 자동으로 축소되지도 않는다.
- **결정성**: 스레드 완료 순서에 따라 float 덧셈 순서가 바뀐다(비결합성) → 같은 요청이 다른 결과를 낼 수 있다. `docs/remediation_backlog_2026-03-05.md:36`의 P0-2 DoD("threads>1에서도 combined 누적 순서가 알파 인덱스 기준으로 결정론 보장")를 **현재 코드는 만족하지 않는다**.

### Q7. 그 외 — 섹션 2 표 참조

look-ahead는 모듈 레벨에서 **없음**(전수 확인: MData=현재 바, MDelay/MTsMean/MTsZscore/MDecay=ring buffer 과거만, MTsRank=자체 버퍼 과거만, MRank/MZscore=동일 바 횡단면, MEvent=동일 바 캐시). 정수 오버플로는 MDecay의 `days*(days+1)/2`에서 이론적 가능(일봉 T≈2000에서는 도달 불가). GA 평가 경로와 simulate 경로는 시뮬레이터 설정(mode=1, fee, 365)이 동일하나 **fitness가 IS sharpe이고 IS가 빈 구간이면 0을 반환**해 손실 알파보다 우대된다.

---

## 2. 섹션 2 — 발견 목록 (심각도 순)

| ID | 심각도 | 파일:줄 | 증상 | 재현 시나리오 | 영향 | 수정안 | 확신도 |
|----|--------|---------|------|---------------|------|--------|--------|
| **F1** | **P0** | `src/modules/TsMean.h:6-11`, `TsZscore.h:6-14`, `Decay.h:7-20`, `Delay.h:4-7` + `src/main.cpp:467` | `init()`이 상태를 **리셋하지 않음**. `StarVector = std::vector`(include/star_vector.h:8)이므로 `resize(numAssets, 0.0)`는 이미 같은 크기면 **기존 값을 보존**한다. `MDelay::init()`은 `maxLookBack`만 세팅하고 ring을 안 비우며, `ModuleInput::push`는 `ring_flat_.empty()`일 때만 초기화(include/module.h:51-59) | 멀티알파 + `collectSignals=true` 요청. 워커가 `run_one(alphas[i],…)`로 전 알파를 1회 실행(main.cpp:396) → 이후 top-K에 대해 **같은 모듈 객체로** `run_signals_only(alphas[idx])` 재실행(main.cpp:467) | 2회차 실행에서 `running_sum`/`running_count`가 1회차 잔재를 그대로 이어받아 **전 구간 신호가 틀림**(예: tsmean 출력 ≈ (직전런 잔여합 + 현재합)/(2w)). `MDelay`는 첫 `days`바가 직전 런의 꼬리값. 결과: signalPlot·signalRange·distribution 전부 오염. 메트릭은 1회차 값이라 영향 없음 → **화면과 숫자가 서로 다른 알파를 보여줌** | (a) 각 `init()`을 `assign()`/명시적 0-fill로 교체하고 `ModuleInput::init(maxLookBack, numAssets)`를 강제 호출, 또는 (b) 재실행 시 `compile_xml`로 새 파이프라인을 만들어 쓰기 | 확인됨 (코드 경로·리셋 누락 모두 확인, 실행 미검증) |
| **F2** | **P1** | `frontend/store/alphaStore.ts:78` + `src/engine/simulator.cpp:114-120` | 기본 XML의 `delay="1"`과 mode=1의 체결 지연이 **가산 중첩**되어 수익 인식 바 기준 정보 시점이 t−3 | 기본 상태로 백테스트 실행 | 팀 규약(t−2) 대비 1바 추가 지연. halflife 5일 신호에서 gross 알파의 **−18.6%**(부록 B-3). 빠른 알파일수록 손해 확대. look-ahead는 없으므로 "안전한 쪽 오차" | 기본 XML에서 `delay` 제거, 또는 `<data>`의 `delay`가 mode 지연과 중첩된다는 사실을 UI/문서에 명시. 근본적으로는 mode를 API 파라미터로 노출 | 확인됨 (부록 B-1 수치 재현) |
| **F3** | **P1** | `src/metrics/summary_metrics.cpp:85-93`, `160-169` vs `docs/math_contract.md:37,46` | `min_bet` = `mean(nonzero_count)`, `min_bet_min` = `min(nonzero_count)`. 계약은 "min_bet 시리즈의 평균/최솟값". 시뮬레이터가 만드는 `out.min_bet`(simulator.cpp:108, `Σ|w|/N`)은 함수 인자로 전달만 되고 **한 번도 읽히지 않는다** | `compute_summary_range` 호출 시 `min_bet` 인자에 임의 값을 넣어도 결과 불변 | JSON `minBet`은 "종목 수"(json_serializer.cpp:22 주석 인정), 계약 문서 독자는 "평균 베팅 비중"으로 읽음. `min_bet` 시리즈는 dead code. golden 오라클(generate_golden.py:259,290)이 **코드에 맞춰 작성**되어 테스트로는 절대 안 잡힘 | 계약을 코드에 맞추거나(권장: 이름을 `nonzero_avg`/`nonzero_min`으로 변경), 코드를 계약에 맞추고 별도 카운트 필드 신설. 어느 쪽이든 `out.min_bet` dead 여부 정리 | 확인됨 |
| **F4** | **P1** | `src/main.cpp:375-385` | 스레드 완료 순서 의존 float 누적 → **비결정성** | `threads>1`, 알파 수가 많고 신호 크기 편차가 큰 경우 반복 요청 | combined 지표가 재현 안 될 수 있음. backlog P0-2 DoD 미충족. 기존 테스트(`test_http_contract.cpp:439`)는 4개 알파·동일 스케일이라 우연히 통과할 가능성이 높음 | 알파별 신호를 인덱스 순서로 누적(워커는 알파 인덱스별 버퍼에 쓰고 join 후 순차 합산), 또는 double 누적 | 확인됨(비결합성) / 실제 차이 발생 여부는 추정 |
| **F5** | **P1** | `src/main.cpp:375-385`, `522-545` | combined alpha가 **정규화 없이 raw signal 합산** | `<data expr="close"/>` 계열과 `<zscore/>` 계열을 파이프 확장으로 함께 제출 | 결합 포트폴리오가 최대 스케일 알파에 지배됨. rank 계열 혼입 시 순노출 +0.58(부록 B-4). 사용자가 기대하는 "동일가중 결합"이 아님 | 합산 전 알파별 `s/Σ|s|` 정규화(또는 cross-sectional z) 후 합산. 최소한 API 문서에 raw sum임을 명시 | 확인됨 |
| **F6** | **P1** | `src/engine/simulator.cpp:79-87` | close NaN 구간의 보유 포지션 손익이 **소실**(0으로 처리)되고 청산 수수료만 부과 | 상폐/거래정지/데이터 결측 종목 보유 중 close가 NaN이 되는 경우 | 주식 유니버스 적용 시 상폐 손실을 구조적으로 회피 → 성과 상향 편향. 크립토 현 데이터셋에서는 영향 미미 | 결측 진입 시 마지막 유효가 대비 손실 인식 또는 명시적 강제청산 로직 추가. 최소한 결측 보유 바 수를 진단 지표로 노출 | 확인됨 (편향 크기는 데이터 의존) |
| **F7** | **P2** | `src/metrics/metrics_engine.cpp:25-31` | 모든 timestamp가 `is_timestamp_ms`보다 이르면 `is_idx`가 초기값 **0으로 남음** (루프에서 한 번도 갱신되지 않음) | `isStart`를 데이터 종료일 이후로 지정 (예: 2030-01-01) | IS 구간이 비어야 하는데 **전체 기간이 IS로 계산**된다. GA fitness(IS sharpe)가 조용히 전기간 sharpe가 됨 | `bool found` 플래그를 두고 미발견 시 `is_idx = T`로 설정 | 확인됨 |
| **F8** | **P2** | `src/main.cpp:471`, `609`, `612` vs `src/metrics/metrics_engine.h:12` | `is_start_index`는 **timeseries 상대 인덱스**(raw−1)인데 raw 인덱스인 `signals`/`timestamps` 슬라이싱에 그대로 사용 | `simStart` 없이 단일 알파 실행 → signalPlot | 신호 플롯이 IS 시작보다 **1바 먼저** 시작. off-by-one, 표시상 경미 | `plot_start = is_start_index + 1` 또는 raw 인덱스를 별도 필드로 반환 | 확인됨 |
| **F9** | **P2** | `src/metrics/summary_metrics.cpp:66` vs `src/metrics/timeseries_output.cpp:33-35` | summary mdd의 peak는 `cum_pnl[0]`(첫 수익), timeseries drawdown의 peak는 `0.0`으로 초기화 | 구간 첫 바 수익이 음수인 경우 (mode=1에서 bar1은 최초 진입 수수료라 **항상 음수**) | 전기간 기준 `min(timeseries.drawdown) ≠ summary.mdd`. 차트와 표가 어긋남 | 양쪽 모두 `peak = max(0, cum)` 규약으로 통일하고 math_contract에 명시 | 확인됨 |
| **F10** | **P2** | `src/metrics/timeseries_output.cpp:44,47-48` | `cp_net += r`에 `isfinite` 가드 없음. summary는 비유한 수익을 건너뜀(summary_metrics.cpp:30) | ret에 NaN/inf가 한 번이라도 발생 (F11 경로 또는 향후 확장) | 해당 시점 이후 cum_pnl 전체가 NaN → 차트 전소, summary는 멀쩡 → 두 출력 불일치 | `if (std::isfinite(r))` 가드 추가하고 규약 통일 | 확인됨 |
| **F11** | **P2** | `src/engine/simulator.cpp:62,70` vs `82` | 정규화는 `isfinite`, 수익은 `> 0.0f` — `+inf` close가 수익 게이트를 통과 | parquet에 inf가 섞인 경우 | `ret = ±inf` → F10과 결합해 전체 시계열 파괴 | 수익 게이트를 `isfinite(x) && x > 0`으로 강화 | 확인됨 |
| **F12** | **P2** | `src/ga/parallel_evaluator.cpp:18-21,78` | 평가 캐시 키가 **문자열이 아니라 `std::hash<std::string>` 값** | 서로 다른 XML이 같은 64-bit 해시를 갖는 경우 | 다른 알파의 fitness가 반환됨. 발생 확률은 극히 낮으나 조용히 틀림 | 키를 `std::string`으로 (또는 해시 충돌 시 문자열 재확인) | 확인됨(구조) / 실발생 추정 |
| **F13** | **P2** | `src/main.cpp:729-759` | GA fitness = `metrics.is_summary.sharpe`. IS 구간이 비면 기본 생성 `SummaryMetrics{}`의 sharpe=**0** 반환 | `isStart`가 데이터 범위 밖이거나 F7과 결합 | 손실 알파(sharpe<0)보다 "IS가 없는" 후보가 우대되어 GA가 퇴화 해로 수렴 가능 | IS 표본 수가 임계 미만이면 `-1e18` 반환 | 확인됨 |
| **F14** | **P2** | `src/modules/Decay.h:14` | `int n = days*(days+1)/2 + 1` — `days` 상한이 panel T(dag_builder.cpp:202) | T > ~65,535인 데이터(분봉 등)로 `decay days=T` 지정 | int 오버플로 → 음수 → `div.resize()`가 거대/음수 크기 → bad_alloc/크래시 | `long long` 또는 `days` 상한을 별도로(예: 1024) 제한 | 확인됨(산술) / 현 일봉 데이터에서는 도달 불가 |
| **F15** | **P2** | `src/engine/simulator.cpp:126-219` | `run_from_signals`가 `run`의 정규화·수익·수수료·통계 로직 **전체 복사본** (58줄 중복) | 한쪽만 수정하는 향후 변경 | 단일 알파와 combined alpha가 조용히 다른 산식을 쓰게 됨. 회귀 테스트 `test_combined_alpha.cpp:124`가 현재는 동치를 지켜주지만 산식 변경 시 함께 수정해야 함 | 공통 `step(di, signal_row, …)` 함수로 추출 | 확인됨 |
| **F16** | **P2** | `src/engine/simulator.cpp:90-93` | 가격 드리프트에 의한 리밸런싱 거래가 turnover에 포함되지 않음(`prev_weights`가 목표비중으로 고정) | 항상 | 비용 과소계상 ≈ 연 0.58pp [추정: 17종목·일변동성 4%·5bps] | `prev_weights`를 실현비중으로 갱신하거나, 드리프트 turnover를 별도 진단 지표로 산출 | 설계가정(의도) / 규모는 추정 |
| **F17** | **P2** | `src/api/json_serializer.cpp:138` | 신호 분포 히스토그램이 `|v| > 1e-12` 조건으로 **정확히 0인 신호를 제외** | MEvent(betAll)의 미보유 0, MTsZscore warmup의 0이 다수인 알파 | 분포가 실제와 다르게 보임(0 비중이 안 보임) | 0을 포함하거나 "nonzero only" 라벨 표기 | 확인됨 |
| **F18** | **P2** | `src/engine/simulator.cpp:68` | `abs_sum > 1e-12` 게이트가 **절대 스케일 기준** | 신호 전체가 1e-14 규모인 알파 | 정규화가 스케일 불변이어야 하는데 북이 전부 flat이 되고 청산비용까지 부과됨 | 상대 기준(최대 절대값 대비) 또는 게이트 완화 | 확인됨 |
| **F19** | **P2** | `src/metrics/summary_metrics.cpp:185-219` | `sharpe400`/`sharpe80`이 **슬라이스 내부** 꼬리 구간 | IS summary 조회 | IS의 sharpe80은 "OS 직전 80바"를 의미. "최근 80바"로 오해하기 쉬움 | 계약 문서에 슬라이스 기준임을 명시 | 확인됨 |
| **F20** | **P2** | `src/metrics/summary_metrics.cpp:188,206` | 내부 `size_t start`가 함수 파라미터 `start`를 **섀도잉** | — | 현재 동작은 의도대로(`finite_r`는 이미 슬라이스됨)이나 변경 시 사고 유발 | 변수명 변경 | 확인됨 |
| **F21** | **P2** | `src/metrics/yearly_metrics.cpp:74` + `docs/math_contract.md` | yearly `ann_ret = mean·365·100`으로 **부분 연도도 무조건 연환산**. yearly 메트릭 전체가 math_contract에 미기재 | 마지막 부분 연도(2025) 조회 | 표본 며칠짜리 연도가 극단적 연환산 수치로 표시됨 | 최소 표본 수 게이트 + 계약 문서화 | 확인됨 |
| **F22** | **P2** | `src/api/json_serializer.cpp:25` + `frontend/.../BacktestMetrics.tsx:72` | `margin` = per-bar 수익/turnover가 그대로 4자리 소수 표시 | 항상 | 0.0017처럼 판독 불가한 숫자. STAR 관례는 bps | ×10000 (bps) 표기 | 확인됨(표시) / 의도 단위는 추정 |

**논-이슈로 확인된 것** (적대적 검토했으나 문제 없음):
- `std::vector<BatchResult> batches` 동시 쓰기(main.cpp:340,350): 서로 다른 인덱스 = 별도 메모리 위치이므로 데이터 레이스 아님.
- `batch_signal_payloads[bi]` ↔ `append_batch(batches[bi], bi)` 인덱싱: 정렬 후 위치 기준으로 **일관**.
- MEvent/MSignalSave/MSignalLoad 상태: `assign()` 사용이라 재실행 시 정상 리셋(F1 대상 아님). backlog P0-4의 재계산 제거는 반영됨.
- 모듈 내 미래 참조(look-ahead): 전 모듈 확인, 없음.
- `is_start_index`/`os_start_index` ↔ 프론트 슬라이싱: 일치.
- 알파 경로 traversal(`resolve_alpha_path`, main.cpp:95-106): 방어됨.

---

## 3. 섹션 3 — 계약 문서 vs 코드 불일치

| # | 계약 위치 | 계약 내용 | 코드 | 판정 |
|---|-----------|-----------|------|------|
| C1 | `math_contract.md:37` | `min_bet`: min_bet 시리즈 finite 평균 | `summary_metrics.cpp:85-93` = `mean(nonzero_count)` | **불일치**(F3) |
| C2 | `math_contract.md:46` | `min_bet_min = min(min_bet finite)` | `summary_metrics.cpp:160-169` = `min(nonzero_count)` | **불일치**(F3) |
| C3 | `math_contract.md` 전체 | `win_rate` 항목 **없음** | `summary_metrics.cpp:116-124`, JSON `winRate` | 계약 누락 |
| C4 | `math_contract.md` 전체 | yearly 메트릭 계약 **없음** | `yearly_metrics.cpp` 전체 (pnl_pct/ann_ret/sharpe/max_dd/win_lose/daily_tvr/margin/long_sm/short_sm) | 계약 누락 |
| C5 | `math_contract.md` 전체 | timeseries `drawdown` peak 초기화 규약 **없음** | summary는 `cum[0]`, timeseries는 `0.0` | 계약 누락 + 코드 내부 불일치(F9) |
| C6 | `plan.md:645` (Phase 5-A) | `pnl = equity_last − 1`, `equity = cumprod(1+r)` | `summary_metrics.cpp:36-43` 단리 누적 | **plan.md stale** |
| C7 | `plan.md:646` | `cagr = equity_last^(365/n)−1` | `ann_ret = mean·365` (cagr 미구현) | **plan.md stale** |
| C8 | `plan.md:648` (mdd) | `min(equity/cummax−1)` | additive `min(cum − peak)` | **plan.md stale**, math_contract:34-36과는 일치 |
| C9 | `plan.md:672-679` | `TimeseriesOutput { equity, pnl_gross, drawdown = equity/cummax−1 }` | `{ cum_pnl, cum_pnl_gross, drawdown = cum − peak }` | **plan.md stale** |
| C10 | `plan.md:665-668` (5-B) | yearly는 각 연도 equity 1.0 시작 복리 | 연도별 단리 누적 | **plan.md stale** |
| C11 | `plan.md:652-653` | `expt_per_alpha`, `maxexp` 메트릭 | `SummaryMetrics`에 필드 없음(quality 계산 중 지역변수로만 사용) | 미구현 |
| C12 | `plan.md:236-237` | D1 = 시그널 t, 체결 t+1, 수익 t+2 | 코드와 **일치** (부록 B-1) | 일치 |
| C13 | `plan.md:831` | fee는 편도, turnover에 1회 곱함 | `simulator.cpp:94` 일치 | 일치 |
| C14 | `math_contract.md:49-50` | sharpe80/400 = "finite returns의 tail window" | 슬라이스(IS/OS) 내부 tail | 모호 → 명확화 필요(F19) |
| C15 | `math_contract.md:19-22` | D1 실행 규칙 서술 | 일치하나 **`<data delay>` 속성과의 중첩을 언급하지 않음** | 계약 누락(F2의 근원) |
| C16 | `math_contract.md:15-27` | Simulation Conventions에 **NaN close 보유분 손익 처리 규칙 없음** | `simulator.cpp:82`에서 조용히 드랍 | 계약 누락(F6) |
| C17 | `plan.md:271` 단위표 | turnover 범위 [0,2] | 일치 | 일치 |
| C18 | `CLAUDE.md` 전체 | python-engine 기준 서술 | python-engine 삭제됨 | **문서 stale**(기지) |

---

## 4. 섹션 4 — 테스트가 못 잡는 것

| # | 공백 | 왜 못 잡는가 | 제안 테스트 |
|---|------|--------------|-------------|
| T1 | **오라클이 코드의 복사본** | `tests/golden/generate_golden.py:259` `# min_bet: ... (matches C++ which uses nonzero_count)`, `:290` 동일. `test_metrics_oracle.cpp:129-133,174-181`도 구현을 그대로 재작성. 계약 문서로부터 독립적으로 유도되지 않음 | 오라클을 `math_contract.md` 문장에서 직접 유도하고, 계약과 코드가 다르면 **실패하도록** 둘 것 |
| T2 | **모듈 재실행(state pollution) 미검증** | 모든 모듈 테스트가 새 객체 1회 실행. `test_combined_alpha.cpp:145-151`도 sim1/sim2가 서로 다른 경로(run vs run_from_signals) | 동일 파이프라인으로 `sim.run()` 2회 호출 후 두 결과가 완전히 동일한지 검증 (F1 직격) |
| T3 | **타이밍 계약(정보→수익 지연) 미검증** | `test_simulator.cpp:60-89`는 `ret[0]==0`, `turnover[1]>0`만 확인. `prev_weights[t] == target[t−2]` 같은 구조적 항등식 검증 없음 | 합성 패널에서 `ret[t] == Σ target[t−2]·hr[t]`를 전 구간 대조. `delay=0/1/2`별 지연 바 수 명시 검증 |
| T4 | **trading leg / 체결가 모델 부재** | 개념 자체가 테스트에 없음 | tr≡0일 때 현재 결과와 동일, tr≠0일 때 `½(μ_lag1+μ_lag2)` 항등식 성립을 검증 |
| T5 | **NaN close 보유분 처리 미검증** | `test_simulator.cpp`·`test_pipeline_contract.cpp` 패널에 NaN close 없음. `test_extreme_alpha_stress.cpp`도 신호 극단값 위주 | 중간에 close가 NaN이 되는 종목을 넣고 손익 소실·청산 수수료 시퀀스를 명시적으로 고정 |
| T6 | **signalPlot 값 검증 부재** | `test_http_contract.cpp:388-396`은 `signalPlot` 존재/객체 여부만 확인, 값 대조 없음 → F1이 통과 | top-K signalPlot 값과 단일 알파 실행 signalPlot 값이 일치하는지 대조 |
| T7 | **combined 결정성 테스트가 약함** | `test_http_contract.cpp:439-473`은 4개 알파·동일 스케일(tsmean+rank) → 부동소수 순서 차이가 드러나기 어렵고, 서버 미기동 시 `GTEST_SKIP` | 스케일 편차가 큰 알파 다수(예: raw close + zscore + rank) × threads 1 vs 8 결과 비교 |
| T8 | **combined 스케일 왜곡 미검증** | `test_combined_alpha.cpp:32-76`은 합산 로직을 테스트 안에 **재구현**해서 검증(프로덕션 `accumulate_combined_row` 미호출). 스케일 다른 알파 조합 케이스 없음 | rank+zscore 조합의 순노출이 0이 아님을 명시적 계약으로 고정하거나, 정규화 후 0에 가까움을 검증 |
| T9 | **IS/OS 경계 이상 입력 미검증** | `test_metrics_oracle.cpp:303-370`은 정상 범위만. `isStart > 데이터 종료일` 케이스 없음 | F7 재현 케이스 추가 |
| T10 | **summary mdd ↔ timeseries drawdown 정합 미검증** | 각각 별도 테스트만 존재 | 전기간에서 `min(timeseries.drawdown) == summary.mdd` 검증 (현재는 F9로 실패해야 정상) |
| T11 | **HTTP 계약 테스트가 조건부 skip** | `test_http_contract.cpp` 전 케이스가 `if (!available_) GTEST_SKIP()` | CI에서 서버 기동 필수화 또는 skip 시 CI 실패 처리 |
| T12 | **fee/turnover 수치 계약 미검증** | `test_simulator.cpp:49`의 `EXPECT_LT(ret[0], 0)` 수준 | `turnover[1] == Σ|target_0|`, `ret[1] == −turnover[1]·fee` 등 정확값 대조 |

---

## 부록 A. 검증에 사용한 코드 참조 (핵심 줄)

| 사실 | 근거 |
|------|------|
| mode=1 상태 갱신 = 이중 swap | `src/engine/simulator.cpp:115-117` |
| 수익은 `prev_weights`, 체결가중치는 `prev_target` | `simulator.cpp:76, 83` |
| 수익 게이트가 `isfinite`가 아닌 `> 0.0f` | `simulator.cpp:82` |
| `"close"` 하드코딩 (체결가 컬럼 개념 없음) | `simulator.cpp:58, 80` |
| `min_bet` 시리즈 생성 후 미사용 | 생성 `simulator.cpp:108` / 소비처 없음(`summary_metrics.cpp:85-93`이 nonzero_count 사용) |
| fee = 5bps 편도 | `config.yaml:17`, `src/data/config_loader.cpp:40-41`, `docs/plan.md:831` |
| 기본 XML의 delay=1 | `frontend/store/alphaStore.ts:78` |
| `init()` 리셋 누락 | `src/modules/TsMean.h:8-10`, `TsZscore.h:10-12`, `Decay.h:10-13`, `Delay.h:6`, `include/module.h:51` |
| `StarVector = std::vector` | `include/star_vector.h:8` |
| 동일 파이프라인 2회 실행 | `src/main.cpp:396` (1회차) → `src/main.cpp:467` (2회차) |
| combined 비정규화 합산 | `src/main.cpp:378-384` |
| `is_idx` 기본값 0 잔존 | `src/metrics/metrics_engine.cpp:25-31` |
| ts 인덱스 매핑 | `src/metrics/metrics_engine.cpp:50-52` ↔ `frontend/lib/api.ts:110-119` |
| backlog의 결정성 DoD | `docs/remediation_backlog_2026-03-05.md:36` |

## 부록 B. 수치 검증 (`/home/jgo/usr/venv-3.11/bin/python`)

**B-1. 타이밍 항등식** (`verify_timing.py`): 시뮬레이터 루프를 그대로 재현. `delay=0/1` 모두에서
`prev_weights[t] == target[t−2]` → `np.allclose` **True**,
엔진 `ret[t]`와 팀 산식(`tr=0`) 차이 **0.0** (기계 정밀도).

**B-2. trading leg 규모** (halflife별, φ=±0.03, N=17, fee 5bps):

| 신호 halflife | 일평균 회전율 | 엔진(net) 연 | 팀(net) 연 | trade leg 연 |
|---------------|---------------|--------------|------------|--------------|
| 1일 | 1.02 | −27.3% | −2.2% | **+25.1%** |
| 3일 | 0.67 | −5.7% | +12.3% | +18.0% |
| 10일 | 0.39 | +0.2% | +9.5% | +9.3% |

→ 회전율이 높고 감쇠가 빠를수록 trade leg가 hold leg 전체와 맞먹는다.

**B-3. 항등식 `trade leg = ½(μ_lag1 − μ_lag2)`** (halflife 5일 예측자, T=20000):

```
mu(lag1)=217.970%/y   mu(lag2)=191.191%/y   mu(lag3)=166.451%/y
엔진 hold  = 191.191%   (== mu_lag2)                    ← delay=0
trade leg  =  13.385%   예측 ½(mu1−mu2) = 13.389%       ← 4자리 일치
팀 총합    = 204.576%   예측 ½(mu1+mu2) = 204.580%
delay=1 엔진 = mu_lag3 = 166.451%  →  팀 대비 −18.6%
```

**B-4. combined alpha 스케일 왜곡** (N=17, 20만 바 시뮬레이션):

| 조합 | 순노출(net) | long_ratio | 최대 종목비중 |
|------|-------------|------------|---------------|
| `<rank/>` 단독 | +1.000 | 1.000 | — |
| `<zscore/>` 단독 | 0.000 | 0.500 | 0.156 |
| rank + zscore | **+0.578** | **0.789** | — |
| raw close + zscore | — | — | **0.500** |

스크립트: `scratchpad/{verify_timing,trading_leg,trading_leg2,trading_leg3,combo_scale}.py`
