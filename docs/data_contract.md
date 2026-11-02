# 현재 데이터 계약

설정 로더는 일반 long/wide Parquet과 thin-v1 캐시를 읽는다. 레이아웃을 구분하고 가격, 신호 입력, 보유 손익, position move, 유니버스의 역할을 따로 확인한다. 정확한 계산 순서는 [엔진 계산 계약](../cpp-engine/docs/math_contract.md), 역사적 검증과 교정 근거는 데이터와 손익 노트북에 있다.

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

옛 발행 이름 `returns_total.parquet`이 남은 thin 캐시는 다시 빌드해야 한다. 설정 로더는 읽지 않는 모든 키를 `Unknown config key '<경로>'` 오류로 거부하며, 옛 최상위 `events:` 및 `backtest.corporate_actions:` 설정도 thin·nonthin 모두 그 규칙으로 거부된다(2026-10-15).

현행 실제 캐시는 설정 YAML의 `base.cache`와 manifest를 확인한다. `config.example.yaml` 같은 예제 파일명이 현재 발행 build의 이름을 보장하지 않는다. source 종료 후 승계와 비종료 분사는 엔진 적용 순서가 다르며, 기업행동 이동 자체를 시장 거래비나 execution leg로 가산하지 않는다.

## 일반 long과 wide 데이터

일반 `datasets:` 및 nonthin wide `base.cache`도 지원한다. 이 경로의 `returns`는 close에서 파생되며 thin의 총수익률 회계와 position move를 제공하지 않는다. 원천 가격을 유니버스 밖에서 NaN으로 덮어쓰지 않는다.

상대 경로는 설정 파일 위치 기준이다. 필드 파일과 catalog의 경로는 dataset.path 기준이며 공개 field 이름 충돌, 중복·null 좌표, 잘못된 timestamp와 지원하지 않는 타입을 거부한다. catalog 등록은 모든 값을 즉시 읽는다는 뜻이 아니다. AFL catalog(`afl_catalog.json`)는 `schema_version: 2`와 `fields: {이름: dataset.path 기준 파일}`, `factors: [{id, code, name, field, file}]`를 가진다. 팩터당 이름은 하나이며 FactorCode에서 `[A-Za-z0-9_]` 밖 문자를 `_`로 바꾼 것이다(숫자로 시작하는 코드도 그대로, 숫자 ID 별칭 없음). schema 1은 시작 시 거부된다. `format: wide` 데이터셋의 모든 필드는 `<데이터셋 name>.<필드>`로만 등록되며(name 필수, `[A-Za-z][A-Za-z0-9_]*`; 필드는 `[A-Za-z0-9_]+`, 전부 숫자 불가), 이름 충돌은 데이터셋 안에서만 검사한다. base 필드와 long `use_col` 필드는 접두사 없는 이름 그대로다. 생성기는 데이터 빌더(`universe_builder/afl/build_afl_catalog.py`) 소유이고 캐시와 함께 발행된다.

숫자 필드의 ±inf 값은 로딩 시 NaN(결측)으로 읽는다. 가격이면 결측 가격과 같은 carry/error 규약을 따르고, 유니버스 멤버십 열이면 비멤버가 된다.

`alignment: exact`는 같은 timestamp만 사용한다. `asof`는 현재 시점 이하의 마지막 관측 행을 사용하며 그 행의 null은 유지한다. 최초 관측 전을 미래 값으로 채우지 않는다. timestamp가 실제 정보 가용 시점을 나타내는지는 공급 데이터의 계약이다.

## 유니버스와 그룹

유니버스 파일은 캐시 빌더(`universe_builder` 저장소)가 캐시와 함께 발행하며 엔진은 읽기만 한다.

구간 멤버십은 가격과 별도의 mask이며, bar t에는 `backtest.pit_lag`(기본 1) bar 전 종가까지로 정해진 멤버십과 그룹 코드를 쓴다(2026-10-29; 그 전에는 t일 종가로 결정된 멤버십을 t에 그대로 썼다). 빌더는 구간의 `fromDate`를 결정일(그날 종가 기준 시가총액 순위)로 적으므로 지연은 로더가 건다. XML이 출력한 현재 bar 신호를 마스킹·정규화하여 같은 bar에 실행하며, 추가 target 지연은 없다. 원천 파일을 미리 shift하지 않는다. `pit_lag: 0`이 옛 동작이다. 현행 정책과 과거 제안의 차이는 유니버스 설계 노트북을 따른다.

`base: {cache, groups}`는 기본 가격·그룹 필드를 등록한다. 필요한 기본 파일이나 유한 그룹 값이 없으면 시작을 거부한다. 날짜·ID가 바뀌면 해당 캐시 축으로 그룹 패널을 다시 생성한다.

`neutralize`는 신호 날짜 mask 적용 후 유한 신호와 유한·0 이상 그룹 코드만 그룹 평균에 포함한다. 그 외는 NaN, singleton은 0이며 추가 scale은 하지 않는다. 신호 중립화가 이후 합성·비중 제한·기업행동 뒤 최종 북의 그룹 중립성을 보장하지는 않는다.

## 손익 체결과 기간

고정 명목금액 근사의 보유 손익에 실제 시장 매매의 execution leg를 더하고 편도 turnover 비용을 뺀다. 정확한 share/cash 원장은 아니다. 체결은 close·OHLC4·VWAP를 지원하며 지연은 XML에 명시한 delay만 적용한다. OHLC4/VWAP는 필요한 exact quote를 요구하고 잘못된 quote를 종가로 대체하지 않는다.

`missing_close_policy: carry`는 관측 불가능한 보유의 거래를 동결하며 가격 재개 때 해당 경로의 누적 손익을 인식한다. thin은 캐시 보유 수익률을 읽고 일반 데이터는 close 회복 규약을 사용한다. `error`는 필요한 거래를 수행할 수 없으면 실패한다. warmup은 보유·손익·비용을 만들지 않고 거래 기간 준비만 한다.

API 성과 지표와 GA fitness는 gross 기준이고 시계열에 gross/net이 함께 있다. 누적손익과 drawdown은 단리이며 초기 0 고점을 포함한다. 화면의 `avgBet`은 평균 held-symbol 수, `minBet`은 최소 held-symbol 수다. 레거시 JSON은 평균을 `minBet`, 최소를 `minBetMin`으로 보내므로 wire 키와 화면 이름을 구분한다. 저장된 과거 연구 표가 net Sharpe 또는 gross 분모를 사용하면 해당 표의 조건을 따로 확인한다.

체결·실제 원장 차이는 전략 재현과 실제 운용 노트북, 알파 가용성은 알파 정의와 정보 시점, 합성·제한은 포트폴리오와 위험에 정리되어 있다.
