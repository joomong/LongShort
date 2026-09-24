# 일별 롱숏 캐시·엔진 단순화 계획 (2026-09-23 확정 방향 반영)

> 2026-09-23 검토 개정 1: 포지션 이동 행을 받은 주식·종료로 축소(§3), 미해결 발행 게이트를 보유 가능 범위로 한정하고 엔진의 미확정 처리를 제거(§5), AFL 점 표기·분류 가용 시점을 범위 밖으로 이동(§9).
>
> 2026-09-23 검토 개정 2: 수익률을 두 개로 분리해 둘 다 빌더가 캐시로 만든다. 신호용 `returns`는 분할조정 종가 변화율, 손익용 `returns_total`은 모든 사건을 반영한 수익률이며 엔진은 계산하지 않고 읽기만 한다(§4). 가격 필드 `close`는 조정 전 raw 값이다. 신호 전환 단계는 없앴고, Airflow 정리는 후순위로 미뤘으며, 1단계(데이터 정합성)부터 진행한다(§7).

## 1. 이번에 바꾸는 부분

| 기존 계획·구현 | 변경할 계획 |
|---|---|
| 보통일·배당·분사·합병의 손익 계산 경로가 나뉨 | 데이터 생성기에서 하나의 평가식으로 최종 `returns_total` 작성 |
| C++이 close 변화율을 구하고 배당·자회사·합병 손익을 추가 | C++ 손익은 전일 포지션 × `returns_total` 한 번 |
| events에 원주 단위 금액·교환비와 앵커를 넘겨 C++에서 재평가 | `returns_total`에 사용한 동일 평가값으로 받은 주식의 가치 비율과 종료 여부만 생성 |
| 부모의 최종 수익률과 자회사 추가의 가치가 중복될 위험 | events는 손익을 추가하지 않고 받은 주식 포지션 생성과 종료만 표시 |
| XML returns를 엔진이 close 변화율로 계산 | 신호용 `returns`(분할조정 종가 변화율)도 빌더가 캐시로 생성. 엔진은 읽기만 함 |
| 미확정 사건을 엔진에서 추정·기본값으로 처리하거나 보유 시 중단 | 빌더가 보유 가능 범위의 미해결 0건을 발행 조건으로 확인. 엔진은 미확정 상태를 받지 않음 |
| 원천·중간물·여러 사건 표현을 모두 발행 | 실행 데이터 한 벌만 발행. 조사·교정 근거는 빌더 쪽에 보관 |

일별 고정 한도·단리 성과 집계는 유지한다. 아래에서 position은 해당 보유 구간 시작 시점의 부호 있는 평가금액이며, 가중치를 쓰는 경우 고정 기준금액으로 나눈 값이다. 배당·기업행동 때문에 다음 목표 한도를 자동 확대하지 않는다.

엔진의 고정 북 규칙은 사건일에도 그대로 적용한다. 보통날 가격이 움직여도 position은 바뀌지 않고(`simulator.cpp:226`), 회전은 목표와 전일 position의 차이다(`simulator.cpp:828`).

## 2. 손익용 수익률 공식 하나

`returns_total` 계산은 분할조정 가격에서 시작한다.

    A[i,t] = raw_close[i,t] / adj_factor[i,t]

    returns_total[i,t] = (V_self[i,t] + C[i,t] + Σ V_child[i,t]) / B[i,t] - 1

- B: 전일 보유의 기준 가치. 연속 종목은 전일 분할조정 종가 A[i,t-1]. 시세가 없는 날에는 정산일을 제외하고 행을 만들지 않는다(패널에서 NaN). 시세가 재개된 날의 값은 마지막 유효 시세일(prevDate)의 A를 B로 쓴 누적 수익률이다. 현행 returns_total과 같은 규칙이다.
- V_self: 오늘 남아 있는 같은 종목의 가치. 보통일·분할·일반 배당일에는 A[i,t]. 종목이 종료되어 다른 자산으로 전환되면 0.
- C: 오늘 새로 귀속되는 세전 현금 대가.
- V_child: 오늘 새로 귀속되는 자회사·승계사 등 다른 주식의 평가가치. 여러 주식이면 합산.
- 네 값은 모두 같은 통화와 같은 분할조정 주식 단위로 맞춘다. 원주 단위 현금·교부 주수를 조정가격에 그대로 더하지 않는다.
- 배당이 이미 포함된 벤더 divAdjFactor로 A를 만들지 않는다. adj_factor는 분할·병합·주식 수 단위 변환용이다.

교부 조건이 사건 직전 원주 1주 기준이고 당시 부모 계수가 F_prev이면, 현금 D는 D/F_prev, 받은 자회사 r주가 raw 가격 P_child이면 그 가치는 r×P_child/F_prev로 위 식에 넣는다. 분할과 배당·분사가 같은 날이면 먼저 조건의 주식 단위를 사건 직전 1주 기준으로 정규화한다.

| 사건 | B | V_self | C | V_child | returns_total |
|---|---:|---:|---:|---:|---:|
| 보통 거래 | 100 | 103 | 0 | 0 | +3% |
| 현금배당 | 100 | 98 | 2 | 0 | 0% |
| 주식배분 분사 | 100 | 70 | 0 | 30 | 0% |
| 현금합병 | 100 | 0 | 105 | 0 | +5% |
| 주식합병 | 100 | 0 | 0 | 108 | +8% |

순수 2:1 분할은 raw 100→50, factor 2→1이므로 A가 50→50이다. 수익률 0%이며 사건 손익을 따로 추가하지 않는다.

현금배당은 배당락일에 한 번 포함한다. 지급일에는 같은 금액을 수익으로 다시 넣지 않는다. 합병·청산은 검증된 경제적 전환 날짜에 최종 대가를 반영한다. 최종 거래일·전환일·현금 지급일을 무조건 같은 날짜로 취급하지 않으며, 지급까지 대가를 받을 권리가 남으면 그 상태를 데이터 생성기에서 정리한다.

## 3. 포지션 이동: 받은 주식과 종료만

빌더는 `returns_total`과 같은 평가값으로 세 비율을 모두 계산해 스스로 검증한다.

    self_ratio  = V_self / B
    cash_ratio  = C / B
    child_ratio = V_child / B

    self_ratio + cash_ratio + Σ child_ratio = 1 + returns_total

이 비율은 주식 교환비율이 아니라 전일 평가금액 대비 오늘 목적지 자산의 평가금액 비율이다. 원주당 교환비는 원천 교정 자료에 남긴다. 엔진에 넘기는 것은 이 중 엔진이 스스로 알 수 없는 두 가지뿐이다.

- 받은 주식: valueRatio = child_ratio. 자회사·승계사·권리마다 한 행.
- 종료: source 종목이 이날로 소멸함(sourceEnds).

엔진은 다음만 수행한다. C++에서 벤더 계수와 기업행동 원단위를 다시 해석하지 않는다.

    당일 손익 = 이전 position × returns_total

    받은 주식 position += 이전 position × valueRatio
    sourceEnds이면 source position = 0
    그 밖의 source position은 바꾸지 않음

계속 존재하는 부모의 position은 보통날과 똑같이 그대로 둔다. 고정 북에서는 보통날 30% 하락한 종목도 position을 유지하고, 다시 맞추는 비용을 받지 않는다. 사건일에만 부모를 self_ratio로 줄이면 보통날과 규칙이 달라지고, 보유 종목의 배당일마다 그날 가격 변화분을 되사는 가짜 주문·수수료가 생긴다. 현금은 이미 손익에 들어가 옮길 곳이 없으므로 현금 행도 두지 않는다. 현재 원장 기준으로 현금 레그 457,681건은 행이 필요 없고, 주식·권리 레그 3,832건과 종료 사건(현행 종료 표 14,892행)만 행이 된다. 사건일에도 가치 보존(부모 70 + 자회사 30)을 원하면 모든 날에 가격 드리프트를 반영하도록 엔진 규칙 전체를 바꿔야 하며, 이번 범위가 아니다.

| 사건 | returns_total | 발행 행 | 사건 뒤 position (이전 100) |
|---|---:|---|---|
| 현금배당: 98 + 현금 2 | 0% | 없음 | 부모 100 |
| 분사: 70 + 자회사 30 | 0% | 자회사 0.3 | 부모 100, 자회사 30 |
| 현금합병: 현금 105 | +5% | 종료 | 없음 |
| 주식합병: 승계사 108 | +8% | 승계사 1.08, 종료 | 승계사 108 |
| 혼합합병: 현금 50 + 승계사 55 | +5% | 승계사 0.55, 종료 | 승계사 55 |

손익을 계산한 뒤 위 규칙으로 position을 바꾼다. 손익과 목적지 금액을 둘 다 별도 수익으로 더하지 않는다. 롱·숏은 position의 부호만 다르며, 기존 자회사 보유분이 있으면 합산한다. 여러 사건을 처리할 때도 모두 사건 직전 보유를 기준으로 계산한 뒤 결과를 한 번에 반영하여 행 순서에 따라 새 포지션에 당일 수익률이 다시 적용되지 않게 한다.

자회사의 사건일 평가가격은 새 포지션의 최초 가격이며, 그 새 물량의 별도 당일 손익은 0이다. 이미 보유했던 자회사 물량은 자신의 당일 `returns_total`을 정상적으로 적용한다. 새 물량은 다음 보유 구간부터 자회사의 `returns_total`을 따른다.

받은 주식은 자동 현금화하지 않는다. 사건을 반영한 뒤 전략의 다음 리밸런싱이 보유·매도·커버를 결정하고, 실제 주문에는 일반 거래비가 붙는다. 자회사가 투자 대상 밖이어도 포지션과 평가가격은 로딩하며, 신규 목표가 0이면 일반 청산 경로를 사용한다. 받은 주식의 가격 자료가 아예 없고 근거 있는 평가값만 있으면 이동 행을 두지 않으며, 그 가치는 `returns_total`에만 들어간다. 현행 엔진의 value-only 처리와 같다.

권리·워런트도 받은 주식으로 다룬다. 만료되면 만료 다음 거래일 `returns_total`을 −1로 쓰고 종료 행을 둔다. 현재 C++의 권리 만료 처리(`rights_expiry_`)를 대체한다.

기존 주주에게 주식이 실제 배분된 사건만 이 규칙을 적용한다. 물적분할·자회사 IPO만으로 자회사 포지션을 만들지 않는다. 사건명 대신 배분 조건과 비율을 원천 자료로 확인한다.

## 4. 신호에 쓰는 값

신호와 손익은 서로 다른 수익률을 쓴다. 둘 다 빌더가 캐시로 만들고, 엔진은 계산하지 않고 읽기만 한다.

| 필드 | 용도 | 내용 |
|---|---|---|
| close (open·high·low·vwap 동일) | 신호 | 조정 전 실제 가격(raw). 분할조정 가격은 식에서 `close/adj_factor` |
| volume | 신호 | 조정 전 실제 거래량. 날짜 간 비교는 `volume*adj_factor` |
| returns | 신호 | 분할조정 종가 변화율. 배당·분사 가치는 넣지 않는다 |
| returns_total | 손익 | 배당·분사·정산을 모두 반영한 수익률(§2) |

신호용 `returns`는 현행 엔진 식(`close / Delay(close, 1) - 1`, 이때 close는 분할조정 가격)과 같은 값을 빌더가 만든다. 전 거래일의 분할조정 종가를 기준으로 하며, 그 값이 없으면 NaN이다. 배당락·분사일 하락은 신호에 그대로 보인다. 거래대금(가격×거래량)은 조정 계수가 약분되어 조정이 필요 없다. 절대 가격 수준(가격 하한 등)은 raw close를 쓴다. 분할조정 가격에는 미래 분할이 들어 있어, 예를 들어 AAPL 2000-01-03 종가 111.94달러는 분할조정으로 약 1달러가 된다.

신호용 `returns`의 뜻이 현행과 같으므로 신호 전환 단계는 없다. 다만 close가 raw로 바뀌므로, 날짜 간 비교에 가격·거래량을 쓰는 기존 알파 식은 조정 계수를 적용하도록 바꾸고, 바꾼 식의 신호가 현행과 같은지 확인한다(3단계).

참고로 신호에 사건 가치를 넣을지는 알파 설계 선택이다. 손익은 어느 쪽 신호를 쓰든 `returns_total`로 정확하다. Top500 부모 종목의 분사일 195건(2000~2026)을 보면, 분할조정 종가 변화율 기준으로 74%가 그날 Top500 하위 1%였다. 가격 변화율이 −5% 이하인 139건의 이후 시장 대비 초과수익은 다음날 +0.49%(t=1.7), 5일 +0.46%(t=0.9)였다.

## 5. 데이터 확정과 보관

백테스트 전에 발행 대상의 사건을 클리닝·검증한다. 미해결은 값을 만들 근거가 없는 사건이다. rc2f에서 근거와 함께 추정값으로 분류한 사건(estimate_evidence)은 미해결이 아니며, 값으로 발행하고 근거는 빌더 쪽에 남긴다. 근거가 없어 해결되지 않은 값을 임의로 0이나 특정 비율로 채우지 않는다.

발행 게이트는 보유 가능 범위에 건다. 보유 가능 범위는 유니버스 멤버십 기간에 주문 지연과 시세 중단 뒤 종료까지를 더한 구간, 그리고 그 종목에서 받은 자회사·승계사다. 전략은 신호일 유니버스 안에서만 목표를 만들므로, 이 범위 밖의 사건은 어떤 포지션의 손익에도 닿지 않는다.

- 범위 안 미해결이 1건이라도 있으면 새 버전을 발행하지 않고 검증된 이전 버전을 유지한다.
- 범위 밖 미해결은 해당 날짜의 값을 만들지 않고 조사 목록으로 관리한다.
- 엔진은 미확정 상태를 받지 않으며 따로 처리하지 않는다.

자회사 사건일 가격이 없으면 미래 첫 거래가격을 과거에 당겨 쓰지 않는다. 범위 안이면 해당일의 가격·교부 조건을 조사하고, 해결 전에는 발행하지 않는다. 현행 v2.1이 새 기준을 이미 통과했다고 간주하지 않는다.

현재 수치는 다음과 같다. 2026-09-18 일별 빌드 기준이며, 보유 가능 범위는 ALL 유니버스 멤버십이 사건 7일 전(시세 중단 시 마지막 시세 7일 전)부터 사건일까지와 겹치는 경우로 계산했다(추정: 받은 자회사로 넓히기 전 수치).

| 항목 | 전체 | 범위 안 | 비고 |
|---|---:|---:|---|
| rc2f 자료 부족(unresolved_insufficient) | 803 | 27 | 범위 안: 분배 17, 계보 10 |
| 그중 공시까지 확인하고도 근거 없음 | 85 | 15 | |
| 그중 xfeed·가격까지만 확인(인터넷 미확인) | 718 | 12 | 범위 안: FAF·CLWR·GME 워런트 등 |
| 대가 조건·정산 수익률이 모두 없는 종료(현행 엔진은 기본 정산값 처리) | 7,153 | 401 | 범위 안: ACQ 230, 사유 코드 없음 137, 기타 34 |

- data_note.md: 어떤 종목·날짜·필드를 왜 수정했는지와 근거 링크. registry_final.csv(4,711건)에서 생성하며 수기 장부를 따로 두지 않는다.
- 교정 파일·코드: rc2f override CSV(19개 파일, 3,327행)를 그대로 재사용해 매일 재생성 시 같은 수정이 적용되게 한다.
- 원천 스냅샷·revisions·상세 QA: 빌더 작업공간에 보존.
- 발행 캐시: 실행할 값과 최소 버전 정보만 포함.

## 6. 최종 발행 폴더

    cache/
      manifest.json
      calendar.parquet
      base_data.parquet
      adj_factor.parquet
      returns.parquet
      returns_total.parquet
      events.parquet
      universe.parquet
      master.parquet
      AFL/

| 파일 | 내용 |
|---|---|
| base_data | date, tradingItemId, raw OHLCV·VWAP 및 실제 사용하는 기본 필드 |
| adj_factor | 종목·기간별 분할계수. 로더가 필요한 날짜로 대응 |
| returns | date, tradingItemId, 신호용 분할조정 종가 변화율 |
| returns_total | date, tradingItemId, 손익용 최종 수익률. 시세 없는 날은 정산일을 빼고 행이 없다 |
| events | eventId, date, sourceId, destinationId, valueRatio, sourceEnds. 받은 주식(자회사·승계사·권리) 행과 종료 행만 기록. 현금만 받는 종료는 destinationId·valueRatio 없이 sourceEnds=true 한 행 |
| universe | 날짜별/기간별 멤버십과 거래 가능 여부 |
| master | tradingItemId와 표시용 이름. 소멸 종목도 포함 |
| AFL/ | 가용일·ID 기준 AFL 입력. 참조 방식은 현행 유지 |
| manifest/calendar | 빌드·원천·코드·교정 버전, 파일 해시와 공통 거래일 |

source position은 sourceEnds일 때만 0이 되고, 그 밖에는 유지된 채 받은 주식 position이 더해진다. 순수 분할·주식배당·현금배당은 최종 수익률에 이미 반영되어 이동 행이 필요 없고, raw 주수가 필요한 주문 변환에는 adj_factor를 사용한다. 정산일에 raw 가격 행이 없어도 `returns_total`과 events 행은 존재할 수 있다. 시세가 없는 날에는 신규 주문을 허용하지 않고 보유 손익은 0이며, 재개일 `returns_total`이 그 사이 변화를 한 번에 반영한다.

sector/industry는 이번 범위에서 현행(현재 분류를 전 기간에 적용한 그룹 패널)을 유지하고, 과거 구간은 look-ahead가 있는 근사임을 manifest와 문서에 적는다. master/classification_history의 관측 이력은 2025-02-03부터라, 가용 시점 기준으로 바꾸면 그 이전 분류가 비어 neutralize 결과가 전부 바뀐다. 가용 시점 전환은 신호 변경이므로 별도 작업으로 한다. 기간별 ID 매핑·계보·산업분류 원천 이력은 빌더 내부에 유지한다.

## 7. 단계별 작업

진행 순서(2026-09-23 결정): 0단계(기준 보존) → 2~5단계(로직, rc2f3 데이터 고정) → 1단계(데이터 정합성) → 6단계(Airflow 정리). 로직을 고정 데이터에서 먼저 바꿔야 5단계 대조의 차이가 전부 로직 탓으로 설명된다. 클리닝 전까지 미해결 사건은 빌더가 현행 엔진과 같은 전환값을 쓰고 목록으로 남기며, 그 캐시는 발행하지 않는다. 0단계는 커밋 대신 작업 트리 스냅샷(`~/longshort/simplify/baseline/`)과 기준 엔진 빌드로 보존했고, 커밋은 사용자 확인 후 한다.

### 0단계 — 현재 기준 보존

- 현재 발행 캐시(61의 rc2f3와 `.prev-` 사본)와 코드·교정 목록을 기준 버전으로 보존한다. 코드는 `2e77af1` 이후 미커밋 변경 129개 경로가 있으므로 커밋·태그가 곧 기준 보존이다. 커밋은 사용자 확인 후 진행한다.
- 전환용 새 출력 경로를 정한다. 기존 캐시를 덮어쓰거나 코드부터 삭제하지 않는다.
- 비교용으로 일반일·분할·배당·분사·합병·청산 사례와 동일 포지션 북을 확보한다.

완료 조건: 기존 결과를 재현할 기준 입력과 출력이 남아 있다.

### 1단계 — 보유 가능 범위의 데이터 정합성

- 먼저 전체 종목에 가격·거래량·분할계수·시총·ID·거래기간의 저비용 기본 검사를 적용한다. 비정상 단위·분할 경계·중복 ID·상장 종료 뒤 가격은 후보 선정 전에 조사하거나 의심 대상으로 표시한다.
- 이 기본 입력으로 첫 **잠정 유니버스**를 만든다. TopK만 바로 고정하지 않고 순위 경계 주변과 이상값 때문에 순위가 불확실한 종목도 후보에 포함한다. 잠정 유니버스는 발행용 확정 멤버십이 아니다.
- 보유 가능 범위를 코드로 정의한다(유니버스 멤버십 + 주문 지연 + 시세 중단 뒤 종료 + 받은 자회사·승계사).
- 후보의 100일 거래대금·21일 시총 입력 기간, 실제 멤버십 기간, 이후 보유·정산과 받은 자회사·승계사까지 이어지는 구간을 심층 클리닝한다.
- 범위 안의 수익률·사건·가격을 전수 점검해 미해결·추정·이상값 목록을 만든다.
- 교정 원천은 rc2f override CSV와 registry_final.csv를 그대로 쓴다. 이미 조사한 4,711건을 처음부터 다시 조사하지 않는다.
- 보유 가능 범위의 미해결을 원천 자료로 확정한다. 현재 확인된 대상은 rc2f 자료 부족 27건(인터넷 미확인 12건 포함)과 대가 조건 없는 종료 401건이다.
- 대가 조건 없는 인수 종료는 rc2f 규칙대로 마지막 거래일 종가가 정지·stale 가격이 아닌지 확인해 마지막 종가 정산으로 확정한다. 파산·청산은 OTC 가격이나 회수 결과의 근거를 찾는다.
- 주식 배분이 없는 분리/IPO, 중복 사건, 다른 회사 배당의 오귀속을 제거한다.
- 각 교정을 재적용 가능한 교정 파일에 넣고, data_note.md를 registry에서 다시 생성한다.
- 기존 잠정 집계에서 범위 밖이었던 776건도 조사 목록으로 유지한다. 유니버스 재계산으로 범위 안에 들어오면 심층 검사 대상으로 옮긴다. 원천이 없으면 값을 만들지 않는다.
- 교정된 가격·거래량·시총·ID로 유니버스를 다시 계산한다. 새로 후보가 된 종목을 심층 검사하고 재계산하는 과정을 반복한다. 후보 집합과 최종 멤버십이 더 이상 바뀌지 않을 때 확정한다. 이전의 범위 안/밖 숫자는 잠정값이므로 매회 다시 집계한다.

완료 조건: 확정 유니버스의 순위 입력과 보유 가능 범위의 미해결이 0건이고, 가격·대가·주식 단위가 근거로 확인된다. 범위 밖의 미해결을 검증 완료로 표시하지 않는다. 실제 신규 종목의 첫 가격처럼 구조적으로 전일값이 없는 경우는 오류와 구분한다.

### 2단계 — 단일 수익률 함수와 이동 정보 생성

- 모든 사건을 B, V_self, C, V_child라는 같은 입력으로 정규화한다.
- 한 함수로 `returns_total`과 같은 평가값의 valueRatio·종료 표시를 계산한다.
- 현재 returns_total에 완전히 포함되지 않은 합병·상폐 최종 정산과 권리 만료(−1)를 추가한다.
- 같은 날 여러 대가·분할이 겹치면 주식 단위를 먼저 맞추고 합산한다.
- 수익률 계산과 포지션 이동의 평가가격을 동일하게 유지한다.
- 시세 없는 날·재개일 규칙은 현행 returns_total을 따른다.
- 신호용 `returns`를 분할조정 종가의 전 거래일 대비 변화율로 함께 만든다.

완료 조건: 모든 사건에서 self_ratio + cash_ratio + Σ child_ratio = 1 + returns_total이 허용 오차 내에서 성립하며, 중복 대가·미래 가격 사용이 없다.

### 3단계 — 얇은 캐시 생성과 로더 연결

- 위 최소 폴더만 새 경로에 생성한다. base_data는 long 한 벌로 유지한다.
- master는 식별자·표시용 이름, universe는 PIT 멤버십·거래 가능 여부로 정리한다.
- C++은 기본 데이터를 로딩하고 AFL은 식에서 참조하는 필드만 로딩한다.
- XML의 close·open·high·low·vwap·volume은 raw 값을 그대로 읽는다. 조정은 식에서 adj_factor로 한다.
- `returns`는 캐시 필드로 읽는다. `expr_parser.cpp`의 returns 특수 처리(`make_returns`)와 `config_loader.cpp`의 이름 예약을 없앤다.
- `returns_total`은 손익 전용으로 로딩한다.
- 날짜 간 비교에 가격·거래량을 쓰는 기존 알파 식을 조정 계수를 적용한 식으로 바꾼다.

완료 조건: 캐시 `returns`가 현행 엔진 계산값과 허용 오차 내에서 같고, 바꾼 알파 식의 신호가 현행과 같으며, 날짜·ID 정렬과 정보 가용 시점이 맞는다.

### 4단계 — C++ 손익·포지션 경로 단순화

- 보유 손익을 오직 이전 position × `returns_total`로 계산한다.
- events로 받은 주식 position을 만들고 sourceEnds인 source position을 0으로 한다. 계속되는 source position은 바꾸지 않는다. 추가 배당·분사·합병 손익을 계산하지 않는다.
- 기존 자회사/승계사 포지션은 그날의 기존 물량 손익을 먼저 계산한 뒤 받은 물량과 합산한다.
- 시세 없는 날의 보유·주문 규칙(손익 0, 주문 불가)은 현행대로 유지한다.
- 전략의 신호 지연·고정 한도·리밸런싱·일반 비용 규칙은 유지한다.
- 사건일 손익, 포지션 이동, 일반 주문의 순서를 고정한다. 자회사 전용 자동매도 규칙을 만들지 않는다.

완료 조건: 엔진이 원천 사건 유형이나 벤더 계수로 수익률을 재계산하지 않고, 미확정 상태 처리 없이 일반 수익률·포지션 이동 연산만 수행한다.

### 5단계 — 손익 경로 검증과 기존 경로 제거

- 합성 사례: 보통 가격변동, 2:1 분할, 현금배당, 분사, 현금합병, 주식/혼합 합병, 가치 소멸, 권리 만료.
- 실제 사례: AAPL·DD·HON·TYC·Lionsgate 등 기존에 근거를 확보한 사건.
- 각 사례에서 롱·숏, 기존 자회사 보유, 소수주, 다중 대가를 대조한다.
- 포지션 × `returns_total` 손익과 원가격·실제 수량·현금의 전후 가치 차이를 독립 계산해 일치시킨다. 전환 전 원장 회계 전용 하니스는 thin 검증을 대신할 수 없으므로 별도 thin 대조 결과로 확인한다.
- 동일 포지션 북에서 새로 생긴 차이 전부를 사건·정책으로 설명한다. 부모 position 규칙이 현행 엔진과 같으므로, 차이는 1단계에서 확정한 값(기본값으로 처리하던 정산, 첫 시세를 기다리던 자회사)에서 나와야 한다.
- 통과 후 아래 제거 목록의 실행 경로를 걷어낸다. 데이터 조사 근거는 삭제하지 않는다.

완료 조건: 한 경제적 대가가 한 번만 손익에 반영되며, 새 주식은 최초 가격에서 시작하고 이후 일반 주문으로 처리된다. 이전 결과와 단순 비트 일치만을 목표로 삼지 않는다.

### 6단계 — 일별 갱신·발행 명령을 Airflow에서 호출 가능하게 정리 (후순위)

1~5단계가 끝난 뒤 진행한다.

- 기존 일별 갱신(`update_daily.py`, `ledger_build.py --incremental`)에서 원천 조회·교정 재적용·검증 게이트는 재사용한다.
- 최종 산출물 생성을 위 `returns`·`returns_total`·events 생성으로 교체한다.
- Airflow가 기준일·이전 빌드·출력 경로를 지정해 생성 → 검증 → 발행을 호출하게 한다.
- 주간 전체 재조회로 일별 조회 범위 밖 벤더 정정을 반영한다.
- 새 미해결 사건이 보유 가능 범위 안이면 조사 목록에 남기고 새 버전 발행을 보류한다. 범위 밖이면 값 없이 발행하고 목록에 추가한다.
- 실제 Airflow 등록은 해당 DAG 위치와 운영 환경에서 진행한다.

완료 조건: 같은 입력으로 같은 데이터가 만들어지고, 검증 실패 시 현재 버전이 교체되지 않는다.

### 진행 기록 (2026-09-23)

오케스트레이션 기록은 `~/longshort/simplify/orch/`(BRIEF_S.md, TASKS.md, reports/)에 있다.

| 단계 | 결과 |
|---|---|
| 0 | 커밋 대신 작업 트리 스냅샷(`~/longshort/simplify/baseline/`)과 기준 엔진 빌드로 보존. CTest 28/28 |
| 2 | `cache_v21/thin_build.py`. returns_total은 현행 엔진의 1단위 보유 기장과 같은 값으로 계산(4,834만 셀 비트 동일). 이동 행 15,472(종료 14,887, 승계 2,729, 자회사 585). 빌드 57초 |
| 3 | thin-v1 로더(manifest 판별, raw 가격, `returns` 캐시 필드, `adj_factor`, 구간형 유니버스 `universe.name`, 이동 목적지 자동 로딩, 패널 시작 전 종료는 bar 0 은퇴). 옛 레이아웃 로딩은 해시 동일 |
| 4 | 엔진 thin 경로(`포지션 × returns_total`, 승계 이동은 주문 전·자회사는 주문 뒤). 옛 경로 동작은 그대로 |
| 5 검증 | 같은 북(combo000014 2016~2026)·전 종목 롱숏 북(2000~2026)에서 현행 대비 설명되지 않은 차이 0. 차이는 전부 대기 자회사 21건(누적 −0.24bp / −2.39bp). 신호 3종은 순위 동률 외 동일. 실제 사례 13건과 권리 value-only 1건은 현행 엔진과 비트 동일 |
| 5 제거 | thin 설정·문서·전환 전용 도구 정리. nonthin 일반 데이터 로더와 close 기반 `returns` fallback은 유지; 구식 사건/원장 설정은 제거 |

데이터 정합성(1단계)으로 넘길 항목:
- 대기 자회사 21건(엔진 값·날짜 기록, `thin_work/rc2f3/transitional.csv`)
- 대가 조건 없는 종료 7,892건 R=0, 파일 수익률 166건
- v2와 현행 엔진의 정의 차이(`thin_work/rc2f3/v2_diff.csv` 26,850행: 무시세 ex-date 레그 시점 16,537, v2에만 있는 배당 9,502, 신규 id 연결 수익률 428, 기타 302, v2 분할 결함 81. 예: 6981557 2003-09-18 엔진 +1275%는 상장 전 이력의 분할 기준 결함)
- 사건 파일 successorRatioCache의 1e-8 수준 저장값 출처

## 8. 제거·이동 대상과 시점

| 대상 | 조치 | 시점 |
|---|---|---|
| C++ close 변화율 기반 보유 손익 + 별도 CA 손익 | thin 레이아웃에서는 `returns_total` 기반 손익으로 교체; nonthin 일반 입력은 close 기반 보유 손익·신호용 `returns`를 유지하되 별도 CA 회계는 제거 | 4–5단계 |
| book_corporate_actions·book_settlements·정산(settle)의 금액 재계산, 기본 정산값(default_delisting_return), 자회사 별도 손익 가산 | thin에서는 받은 주식 position 생성과 종료 표시로 대체; 구식 설정은 현재 로더가 거부 | 5단계 |
| 권리 만료 처리(`rights_expiry_`) | 빌더 `returns_total`(−1)과 종료 행으로 대체 | 5단계 |
| 미확정 기록·strict 중단(corporateActions.unconfirmed, pnlUnconfirmed, unresolved_policy) | 빌더 발행 게이트로 대체 | 5단계 |
| returns 특수 처리(`make_returns`)·이름 예약 | thin에서는 캐시 `returns` 필드를 사용; nonthin의 close 기반 `returns` fallback과 사용자 필드명 예약은 유지 | 3단계 |
| engine/ca_ledger_cache·engine/events_cache·이중 로더/앵커 설정 | thin 발행물에서는 단일 events로 대체; 옛 설정 블록은 제거 | 5단계 |
| dividends/·distributions/·원천 ca_ledger·corporate_actions | 발행물에서 제거, 빌더 입력·근거로 보관 | 3단계부터 새 발행물에서 제외 |
| dividend factor·adjustment_factor_daily | 발행물에서 제거, 빌더 QA에 보관 | 3단계 |
| 큰 master 이력·계보 파일 | 계산은 빌더에서 수행, 실행에 필요한 필드만 발행 | 3단계 |
| wide 가격·수익률 복사본 | 새 소비자가 long 파일을 읽으면 생성 중단 | 5단계 |
| 발행물의 _work/·상세 qa/ | 빌드 작업공간으로 이동 | 3단계 |
| 별도 quality 파일·상세 CA 진단 UI | 정제 완료 데이터 계약에 불필요한 중복은 제거; 기본 스키마 오류 검사는 유지 | 5단계 |
| 크론 초안·v1 전환 전용 비교 산출물 | Airflow용 명령과 새 검증으로 대체 후 정리 | 6단계 |

## 9. 이번 범위에서 추가하지 않는 것

- DataPanel 전체 교체와 chunk streaming.
- 연구·실거래·현금화 등 여러 손익 계산 모드.
- 모든 날 가격 드리프트를 반영하는 보유 규칙. 고정 북을 유지한다.
- 새 원천 DB 시스템이나 거대한 사건 프레임워크.
- GS별 원천징수·계약 세율.
- 빌더 조사·QA 자료를 백테스트 캐시에 복제하는 구조.
- AFL_US.필드명 점 표기와 필드 등록. 회계 단순화와 무관한 파서 기능이므로 별도 작업으로 한다.
- sector/industry 가용 시점 전환. 신호를 바꾸므로 별도 작업으로 한다.

thin 발행물에 필요한 데이터 정합성은 빌더가 책임지고, C++은 캐시에 있는 `returns`·`returns_total`과 간단한 포지션 이동만 처리한다. 일반 nonthin 데이터는 close 기반 손익과 XML `returns` 계산 경로를 유지한다.

`build_events.py`는 `cache_v21/events_build.py`가 import·호출하므로 유지한다. `build_distributions.py`는 원장 빌더의 정적 분배 입력을 다시 생성할 때 사용하므로 유지한다. 전환 전 엔진 원장 대조 전용 `cache_v21/tests/recon_engine_ca.py`는 현재 thin 검증 경로와 빌더에서 쓰지 않아 제거했다.

## 부록: thin 전환 전 예제 설정 기록

아래 YAML은 이전 cache_v1/cache_v2 비교 입력의 원문 기록이다. 실행용 예제는 `cpp-engine/examples/datasets/`의 thin-v1 YAML을 사용한다. 이 기록은 과거 결과의 입력 경로·유니버스·Altaba override를 추적하기 위한 것이며 현재 엔진에서 실행할 수 없다. 일반 nonthin long/wide 데이터와 close 기반 `returns` fallback은 계속 지원되지만, 구식 `events:`·원장 설정 블록은 현재 로더가 거부한다.

### compare_v1_snapshot.yaml (전환 전)

```yaml
# compare_v1_v2 L0: frozen v1 snapshot (2026-09-14) prices + TopUniverseV1 masks + v1 events file.
# Derived from equities_monthly_afl.yaml; only the three paths below differ. AFL stays on the live AflData_M (read-only).
# Daily prices plus the existing monthly AFL cache. Paths are local server examples.
# Copy this file and afl_catalog.json into your personal configuration directory.
# Base data. `cache` holds BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
# (public names open high low close volume vwap cap); `groups` holds sector.parquet and
# industry.parquet, built by cpp-engine/tools/data/build_group_panels.py. All nine fields are
# read at startup and a missing file fails the start. See README.md and
# cpp-engine/tools/data/README_group_panels.md.
base:
  cache: /home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14
  groups: ../../../data/groups/cache_v1_cash_1d

datasets:
  - name: AFL
    enabled: true
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/AflData_M
    catalog: afl_catalog.json
    lazy: true
    # Keep the most recent observation at/before each price timestamp.
    # This does not turn monthly source observations into daily vendor updates.
    alignment: asof

universe:
  path: /home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14/TopUniverseV1/Top500.parquet

# Administrative delisting/settlement dates, one row per security.
# The panel matches `id_col` (int64 or string) against the price symbols.
events:
  # Built by cpp-engine/tools/data/build_events.py from co_acthist plus the override table.
  path: /home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14/events/events.parquet
  id_col: tradingItemId
  end_date_col: endDate
  return_col: settlementReturn              # explicit total return; NaN falls back to the successor or the default
  successor_col: successorTradingItemId     # surviving security after a merger or rename
  # Cash and ratio are restated in this price cache's share units, so the file is
  # only valid for the cache it was built against: rebuild the cache, regenerate
  # the events. The two guard columns let the loader refuse a stale file.
  successor_ratio_col: successorRatioCache  # successor shares per old share, in cache units
  cash_col: cashPerShareCache               # cash per old share, in cache units
  stock_leg_col: hasStockLeg                # the deal structure, independent of what this panel can price
  cash_leg_col: hasCashLeg
  # Guard anchors: the loader re-reads both from this cache on the settlement bar.
  successor_price_col: successorCacheClose      # successor close on the settlement bar
  successor_price_date_col: successorCacheCloseDate
  target_price_col: targetCacheMark             # last cache mark of the settling security
  target_price_date_col: targetCacheMarkDate

backtest:
  start_date: '2010-01-01'
  end_date: latest
  warmup_bars: 252
  initial_capital: 1000000
  transaction_fee: 5  # one-way bps; existing fee default retained
  bars_per_year: 252
  combined_mode: normalized
  mode: 1
  # Hold a position through a missing close instead of failing; 'error' keeps the legacy fail-fast.
  missing_close_policy: carry
  # Settlement return used when the events file carries no explicit return. 0 is an assumption, not a measurement.
  default_delisting_return: 0.0
  # Settle a frozen holding that has no event after this many consecutive missing bars. 0 disables it.
  stale_quote_settle_bars: 0
```

### compare_v2.yaml (전환 전)

```yaml
# compare_v1_v2 L1/L2: cache_v2_cash_1d prices (float32, single share basis) + TopUniverseV2_v1rule masks (L1)
# + v2-anchored events file. L2 swaps universe.path to TopUniverseV2/<name>.parquet via the harness --universe-path.
# Derived from equities_monthly_afl.yaml; only the three paths below differ. AFL stays on the live AflData_M (read-only).
# v2 DivData/Distributions are not read by the engine.
# Daily prices plus the existing monthly AFL cache. Paths are local server examples.
# Copy this file and afl_catalog.json into your personal configuration directory.
# Base data. `cache` holds BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
# (public names open high low close volume vwap cap); `groups` holds sector.parquet and
# industry.parquet, built by cpp-engine/tools/data/build_group_panels.py. All nine fields are
# read at startup and a missing file fails the start. See README.md and
# cpp-engine/tools/data/README_group_panels.md.
base:
  cache: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d
  groups: ../../../data/groups/cache_v2_cash_1d

datasets:
  - name: AFL
    enabled: true
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/AflData_M
    catalog: afl_catalog.json
    lazy: true
    # Keep the most recent observation at/before each price timestamp.
    # This does not turn monthly source observations into daily vendor updates.
    alignment: asof

universe:
  path: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d/TopUniverseV2_v1rule/Top500.parquet

# Administrative delisting/settlement dates, one row per security.
# The panel matches `id_col` (int64 or string) against the price symbols.
events:
  # Built by cpp-engine/tools/data/build_events.py from co_acthist plus the override table.
  path: /home/jgo/61/data2/temp_stock/compare_v1_v2/events_v2/events.parquet
  id_col: tradingItemId
  end_date_col: endDate
  return_col: settlementReturn              # explicit total return; NaN falls back to the successor or the default
  successor_col: successorTradingItemId     # surviving security after a merger or rename
  # Cash and ratio are restated in this price cache's share units, so the file is
  # only valid for the cache it was built against: rebuild the cache, regenerate
  # the events. The two guard columns let the loader refuse a stale file.
  successor_ratio_col: successorRatioCache  # successor shares per old share, in cache units
  cash_col: cashPerShareCache               # cash per old share, in cache units
  stock_leg_col: hasStockLeg                # the deal structure, independent of what this panel can price
  cash_leg_col: hasCashLeg
  # Guard anchors: the loader re-reads both from this cache on the settlement bar.
  successor_price_col: successorCacheClose      # successor close on the settlement bar
  successor_price_date_col: successorCacheCloseDate
  target_price_col: targetCacheMark             # last cache mark of the settling security
  target_price_date_col: targetCacheMarkDate

backtest:
  start_date: '2010-01-01'
  end_date: latest
  warmup_bars: 252
  initial_capital: 1000000
  transaction_fee: 5  # one-way bps; existing fee default retained
  bars_per_year: 252
  combined_mode: normalized
  mode: 1
  # Hold a position through a missing close instead of failing; 'error' keeps the legacy fail-fast.
  missing_close_policy: carry
  # Settlement return used when the events file carries no explicit return. 0 is an assumption, not a measurement.
  default_delisting_return: 0.0
  # Settle a frozen holding that has no event after this many consecutive missing bars. 0 disables it.
  stale_quote_settle_bars: 0
```

### compare_v2_altaba.yaml (전환 전)

```yaml
# compare_v1_v2 L1/L2 with the Altaba (2665340) LIQ override: identical to compare_v2.yaml except events.path -> events_v2_altaba/events.parquet.
# compare_v1_v2 L1/L2: cache_v2_cash_1d prices (float32, single share basis) + TopUniverseV2_v1rule masks (L1)
# + v2-anchored events file. L2 swaps universe.path to TopUniverseV2/<name>.parquet via the harness --universe-path.
# Derived from equities_monthly_afl.yaml; only the three paths below differ. AFL stays on the live AflData_M (read-only).
# v2 DivData/Distributions are not read by the engine.
# Daily prices plus the existing monthly AFL cache. Paths are local server examples.
# Copy this file and afl_catalog.json into your personal configuration directory.
# Base data. `cache` holds BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
# (public names open high low close volume vwap cap); `groups` holds sector.parquet and
# industry.parquet, built by cpp-engine/tools/data/build_group_panels.py. All nine fields are
# read at startup and a missing file fails the start. See README.md and
# cpp-engine/tools/data/README_group_panels.md.
base:
  cache: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d
  groups: ../../../data/groups/cache_v2_cash_1d

datasets:
  - name: AFL
    enabled: true
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/AflData_M
    catalog: afl_catalog.json
    lazy: true
    # Keep the most recent observation at/before each price timestamp.
    # This does not turn monthly source observations into daily vendor updates.
    alignment: asof

universe:
  path: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d/TopUniverseV2_v1rule/Top500.parquet

# Administrative delisting/settlement dates, one row per security.
# The panel matches `id_col` (int64 or string) against the price symbols.
events:
  # Built by cpp-engine/tools/data/build_events.py from co_acthist plus the override table.
  path: /home/jgo/61/data2/temp_stock/compare_v1_v2/events_v2_altaba/events.parquet
  id_col: tradingItemId
  end_date_col: endDate
  return_col: settlementReturn              # explicit total return; NaN falls back to the successor or the default
  successor_col: successorTradingItemId     # surviving security after a merger or rename
  # Cash and ratio are restated in this price cache's share units, so the file is
  # only valid for the cache it was built against: rebuild the cache, regenerate
  # the events. The two guard columns let the loader refuse a stale file.
  successor_ratio_col: successorRatioCache  # successor shares per old share, in cache units
  cash_col: cashPerShareCache               # cash per old share, in cache units
  stock_leg_col: hasStockLeg                # the deal structure, independent of what this panel can price
  cash_leg_col: hasCashLeg
  # Guard anchors: the loader re-reads both from this cache on the settlement bar.
  successor_price_col: successorCacheClose      # successor close on the settlement bar
  successor_price_date_col: successorCacheCloseDate
  target_price_col: targetCacheMark             # last cache mark of the settling security
  target_price_date_col: targetCacheMarkDate

backtest:
  start_date: '2010-01-01'
  end_date: latest
  warmup_bars: 252
  initial_capital: 1000000
  transaction_fee: 5  # one-way bps; existing fee default retained
  bars_per_year: 252
  combined_mode: normalized
  mode: 1
  # Hold a position through a missing close instead of failing; 'error' keeps the legacy fail-fast.
  missing_close_policy: carry
  # Settlement return used when the events file carries no explicit return. 0 is an assumption, not a measurement.
  default_delisting_return: 0.0
  # Settle a frozen holding that has no event after this many consecutive missing bars. 0 disables it.
  stale_quote_settle_bars: 0
```

### compare_v2_all.yaml (전환 전)

```yaml
# compare_v1_v2 L1/L2 with the Altaba (2665340) LIQ override: identical to compare_v2.yaml except events.path -> events_v2_altaba/events.parquet.
# compare_v1_v2 L1/L2: cache_v2_cash_1d prices (float32, single share basis) + TopUniverseV2_v1rule masks (L1)
# + v2-anchored events file. L2 swaps universe.path to TopUniverseV2/<name>.parquet via the harness --universe-path.
# Derived from equities_monthly_afl.yaml; only the three paths below differ. AFL stays on the live AflData_M (read-only).
# v2 DivData/Distributions are not read by the engine.
# Daily prices plus the existing monthly AFL cache. Paths are local server examples.
# Copy this file and afl_catalog.json into your personal configuration directory.
# Base data. `cache` holds BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
# (public names open high low close volume vwap cap); `groups` holds sector.parquet and
# industry.parquet, built by cpp-engine/tools/data/build_group_panels.py. All nine fields are
# read at startup and a missing file fails the start. See README.md and
# cpp-engine/tools/data/README_group_panels.md.
base:
  cache: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d
  groups: ../../../data/groups/cache_v2_cash_1d

datasets:
  - name: AFL
    enabled: true
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/AflData_M
    catalog: afl_catalog.json
    lazy: true
    # Keep the most recent observation at/before each price timestamp.
    # This does not turn monthly source observations into daily vendor updates.
    alignment: asof

universe:
  path: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d/TopUniverseV2/ALL.parquet

# Administrative delisting/settlement dates, one row per security.
# The panel matches `id_col` (int64 or string) against the price symbols.
events:
  # Built by cpp-engine/tools/data/build_events.py from co_acthist plus the override table.
  path: /home/jgo/61/data2/temp_stock/compare_v1_v2/events_v2_altaba/events.parquet
  id_col: tradingItemId
  end_date_col: endDate
  return_col: settlementReturn              # explicit total return; NaN falls back to the successor or the default
  successor_col: successorTradingItemId     # surviving security after a merger or rename
  # Cash and ratio are restated in this price cache's share units, so the file is
  # only valid for the cache it was built against: rebuild the cache, regenerate
  # the events. The two guard columns let the loader refuse a stale file.
  successor_ratio_col: successorRatioCache  # successor shares per old share, in cache units
  cash_col: cashPerShareCache               # cash per old share, in cache units
  stock_leg_col: hasStockLeg                # the deal structure, independent of what this panel can price
  cash_leg_col: hasCashLeg
  # Guard anchors: the loader re-reads both from this cache on the settlement bar.
  successor_price_col: successorCacheClose      # successor close on the settlement bar
  successor_price_date_col: successorCacheCloseDate
  target_price_col: targetCacheMark             # last cache mark of the settling security
  target_price_date_col: targetCacheMarkDate

backtest:
  symbol_labels_path: /home/jgo/61/data2/temp_stock/cache_v2_cash_1d/Master/symbol_labels.json  # tradingItemId -> ticker for the UI
  start_date: '2010-01-01'
  end_date: latest
  warmup_bars: 252
  initial_capital: 1000000
  transaction_fee: 5  # one-way bps; existing fee default retained
  bars_per_year: 252
  combined_mode: normalized
  mode: 1
  # Hold a position through a missing close instead of failing; 'error' keeps the legacy fail-fast.
  missing_close_policy: carry
  # Settlement return used when the events file carries no explicit return. 0 is an assumption, not a measurement.
  default_delisting_return: 0.0
  # Settle a frozen holding that has no event after this many consecutive missing bars. 0 disables it.
  stale_quote_settle_bars: 0
```

### equities_monthly_afl.yaml (전환 전)

```yaml
# Daily prices plus the existing monthly AFL cache. Paths are local server examples.
# Copy this file and afl_catalog.json into your personal configuration directory.
# Base data. `cache` holds BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet
# (public names open high low close volume vwap cap); `groups` holds sector.parquet and
# industry.parquet, built by cpp-engine/tools/data/build_group_panels.py. All nine fields are
# read at startup and a missing file fails the start. See README.md and
# cpp-engine/tools/data/README_group_panels.md.
base:
  cache: /home/base/cache/cache_v1_cash_1d
  groups: ../../../data/groups/cache_v1_cash_1d

datasets:
  - name: AFL
    enabled: true
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/AflData_M
    catalog: afl_catalog.json
    lazy: true
    # Keep the most recent observation at/before each price timestamp.
    # This does not turn monthly source observations into daily vendor updates.
    alignment: asof

universe:
  path: /home/base/cache/cache_v1_cash_1d/TopUniverseV1/Top500.parquet

# Administrative delisting/settlement dates, one row per security.
# The panel matches `id_col` (int64 or string) against the price symbols.
events:
  # Built by cpp-engine/tools/data/build_events.py from co_acthist plus the override table.
  path: ../../../data/events/events.parquet
  id_col: tradingItemId
  end_date_col: endDate
  return_col: settlementReturn              # explicit total return; NaN falls back to the successor or the default
  successor_col: successorTradingItemId     # surviving security after a merger or rename
  # Cash and ratio are restated in this price cache's share units, so the file is
  # only valid for the cache it was built against: rebuild the cache, regenerate
  # the events. The two guard columns let the loader refuse a stale file.
  successor_ratio_col: successorRatioCache  # successor shares per old share, in cache units
  cash_col: cashPerShareCache               # cash per old share, in cache units
  stock_leg_col: hasStockLeg                # the deal structure, independent of what this panel can price
  cash_leg_col: hasCashLeg
  # Guard anchors: the loader re-reads both from this cache on the settlement bar.
  successor_price_col: successorCacheClose      # successor close on the settlement bar
  successor_price_date_col: successorCacheCloseDate
  target_price_col: targetCacheMark             # last cache mark of the settling security
  target_price_date_col: targetCacheMarkDate

backtest:
  start_date: '2010-01-01'
  end_date: latest
  warmup_bars: 252
  initial_capital: 1000000
  transaction_fee: 5  # one-way bps; existing fee default retained
  bars_per_year: 252
  combined_mode: normalized
  mode: 1
  # Hold a position through a missing close instead of failing; 'error' keeps the legacy fail-fast.
  missing_close_policy: carry
  # Settlement return used when the events file carries no explicit return. 0 is an assumption, not a measurement.
  default_delisting_return: 0.0
  # Settle a frozen holding that has no event after this many consecutive missing bars. 0 disables it.
  stale_quote_settle_bars: 0
```
