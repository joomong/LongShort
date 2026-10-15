# 현재 데이터 계약

설정 로더는 일반 long/wide Parquet과 thin-v1 캐시를 읽는다. 레이아웃을 구분하고 가격, 신호 입력, 보유 손익, position move, 유니버스의 역할을 따로 확인한다. 정확한 계산 순서는 [엔진 계산 계약](../cpp-engine/docs/math_contract.md), 역사적 검증과 교정 근거는 [데이터와 손익 노트북](../../longshort_workspace/research/notebooks/data_and_pnl_contract.ipynb)에 있다.

## Thin 캐시

`base.cache/manifest.json`이 thin-v1 레이아웃을 선언한다.

| 입력 | 역할 |
|---|---|
| `base_data.parquet` | raw OHLC·VWAP·volume·cap |
| `returns.parquet` | 총수익률, 기본 신호 `returns`와 보유 손익 입력 |
| `adj_returns.parquet` | 분할조정 가격수익률, 별도 신호 `adj_returns` |
| `adj_factor.parquet` | 분할 기준 계수 |
| `events.parquet` | 종료와 받은 주식의 position move |
| `universe.parquet` | named universe의 구간 멤버십 |
| `groups/` | sector·industry 그룹 패널 |

옛 발행 이름 `returns_total.parquet`이 남은 thin 캐시는 다시 빌드해야 한다. cache_v2.1 builder의 `derived/returns_total.parquet`은 과거 원장 경로의 중간 결과이므로 발행 thin의 파일명과 구분한다. 설정 로더는 읽지 않는 모든 키를 `Unknown config key '<경로>'` 오류로 거부하며, 옛 최상위 `events:` 및 `backtest.corporate_actions:` 설정도 thin·nonthin 모두 그 규칙으로 거부된다(2026-10-15). 기존 `build_events.py`는 과거 캐시 빌더의 의존 도구로 남아 있지만 현행 엔진의 사건별 현금 원장 경로를 의미하지 않는다.

현행 실제 캐시는 설정 YAML의 `base.cache`와 manifest를 확인한다. `us_equity.yaml` 같은 예제 파일명이 현재 발행 build의 이름을 보장하지 않는다. source 종료 후 승계와 비종료 분사는 엔진 적용 순서가 다르며, 기업행동 이동 자체를 시장 거래비나 execution leg로 가산하지 않는다.

## 일반 long과 wide 데이터

일반 `datasets:` 및 nonthin wide `base.cache`도 지원한다. 이 경로의 `returns`는 close에서 파생되며 thin의 총수익률 회계와 position move를 제공하지 않는다. 원천 가격을 유니버스 밖에서 NaN으로 덮어쓰지 않는다.

상대 경로는 설정 파일 위치 기준이다. 필드 파일과 catalog의 경로는 dataset.path 기준이며 공개 field 이름 충돌, 중복·null 좌표, 잘못된 timestamp와 지원하지 않는 타입을 거부한다. catalog 등록은 모든 값을 즉시 읽는다는 뜻이 아니다.

`alignment: exact`는 같은 timestamp만 사용한다. `asof`는 현재 시점 이하의 마지막 관측 행을 사용하며 그 행의 null은 유지한다. 최초 관측 전을 미래 값으로 채우지 않는다. timestamp가 실제 정보 가용 시점을 나타내는지는 공급 데이터의 계약이다.

## 유니버스와 그룹

유니버스 생성 도구는 별도 [Universe Builder](../../universe_builder/README.md)에 있다. 원천 조회·캐시 교정·ADR 편입·유니버스 생성 코드를 연구 폴더와 분리했으며, `pipeline.py`가 단계별 진입점이다. 유니버스 결과는 `outputs/universe.parquet`에 작성한다. 현재 서버의 캐시 교체는 생성과 별도로 수행한다.

구간 멤버십은 가격과 별도의 mask이며 signal 날짜에 적용한다. XML이 출력한 현재 bar 신호를 마스킹·정규화하여 같은 bar에 실행하며, 추가 target 지연은 없다. 원천 멤버십을 미리 한 번 더 shift하지 않는다. 현행 정책과 과거 제안의 차이는 [유니버스 설계 노트북](../../longshort_workspace/research/notebooks/universe/membership.ipynb)을 따른다.

`base: {cache, groups}`는 기본 가격·그룹 필드를 등록한다. 필요한 기본 파일이나 유한 그룹 값이 없으면 시작을 거부한다. 날짜·ID가 바뀌면 해당 캐시 축으로 그룹 패널을 다시 생성한다.

`neutralize`는 신호 날짜 mask 적용 후 유한 신호와 유한·0 이상 그룹 코드만 그룹 평균에 포함한다. 그 외는 NaN, singleton은 0이며 추가 scale은 하지 않는다. 신호 중립화가 이후 합성·비중 제한·기업행동 뒤 최종 북의 그룹 중립성을 보장하지는 않는다.

## 손익 체결과 기간

고정 명목금액 근사의 보유 손익에 실제 시장 매매의 execution leg를 더하고 편도 turnover 비용을 뺀다. 정확한 share/cash 원장은 아니다. 체결은 close·OHLC4·VWAP를 지원하며 지연은 XML에 명시한 delay만 적용한다. OHLC4/VWAP는 필요한 exact quote를 요구하고 잘못된 quote를 종가로 대체하지 않는다.

`missing_close_policy: carry`는 관측 불가능한 보유의 거래를 동결하며 가격 재개 때 해당 경로의 누적 손익을 인식한다. thin은 캐시 보유 수익률을 읽고 일반 데이터는 close 회복 규약을 사용한다. `error`는 필요한 거래를 수행할 수 없으면 실패한다. warmup은 보유·손익·비용을 만들지 않고 거래 기간 준비만 한다.

API 성과 지표와 GA fitness는 gross 기준이고 시계열에 gross/net이 함께 있다. 누적손익과 drawdown은 단리이며 초기 0 고점을 포함한다. 화면의 `avgBet`은 평균 held-symbol 수, `minBet`은 최소 held-symbol 수다. 레거시 JSON은 평균을 `minBet`, 최소를 `minBetMin`으로 보내므로 wire 키와 화면 이름을 구분한다. 저장된 과거 연구 표가 net Sharpe 또는 gross 분모를 사용하면 해당 표의 조건을 따로 확인한다.

체결·실제 원장 차이는 [전략 재현과 실제 운용 노트북](../../longshort_workspace/research/notebooks/strategy_reproduction_and_live.ipynb), 알파 가용성은 [알파 정의와 정보 시점](../../longshort_workspace/research/notebooks/alpha_definition_and_timing.ipynb), 합성·제한은 [포트폴리오와 위험](../../longshort_workspace/research/notebooks/portfolio_construction_and_risk.ipynb)에 정리되어 있다.
