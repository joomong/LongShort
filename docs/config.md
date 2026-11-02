# 설정 YAML과 데이터 연결

현재 thin-v1 설정 예제는 `config.example.yaml` 하나이며 실제 `base.cache` 경로와 manifest를 확인한다. 예제 파일명은 발행 build 이름을 보장하지 않는다. 플랫폼 루트의 `longshort start`는 `~/longshort_workspace/config.yaml`이 없으면 이 파일을 복사하므로, backtest 블록은 운영 workspace와 같게(2000-01-01 시작, warmup 0, vwap 체결) 유지한다. 직접 쓸 때는 개인 폴더에 YAML을 복사한 뒤 실행한다. AFL 카탈로그는 캐시 폴더의 `afl_catalog.json`(캐시 빌더가 함께 발행)을 절대경로로 가리킨다. 현재 계약은 [데이터 계약](data_contract.md), 최초 빌드는 [빌드 안내](build_and_validation.md)를 따른다.

thin 예제는 `master.parquet`의 `tradingItemId`와 `symbol`에서 UI 라벨을 읽는다(`symbol`이 없으면 `companyName`). 명시적인 `backtest.symbol_labels_path` JSON이 있으면 그 파일이 우선한다. 옛 캐시의 라벨 파일에는 의존하지 않는다.

최상위 `alphas_dir`(선택, 기본 `alphas`)는 웹에서 저장하는 알파 폴더이며 상대경로는 YAML 폴더 기준이다. 최상위 `alpha_pool`(선택, 예: `/home/base/alpha_pool`)은 승인 알파 풀 폴더로, 엔진이 그 안의 `<이름>/alpha.xml`만 읽어 `alpha_pool_universe`(선택, 기본은 `universe.name`; 북은 `TOP500_CAP`)와 기본 체결로 한 번 돌려 두고 결과표의 `Corr` 열(풀 동일가중 손익과의 상관)을 만든다. 없으면 열이 나오지 않는다. 최상위 `meta_dir`(선택, 기본 `meta`, 엔진이 만들지 않음)는 포트폴리오 조합용 멤버 폴더로, `meta/<이름>/**/*.xml`을 `<meta expr="이름"/>`로 불러온다.

```bash
python3 cpp-engine/tools/run_engine.py --config ~/longshort_workspace/config.yaml
```

## thin-v1 캐시

`base.cache/manifest.json`의 `layout: thin-v1`을 로더가 판별한다. 이 캐시의 `base_data.parquet`은 조정 전(raw) 가격이며, `returns.parquet`은 CRSP식 총수익률(배당·분할·분사·합병/상폐 정산 반영)로 기본 신호 `returns`이자 보유 손익 입력이다. `adj_returns.parquet`은 분할조정 가격 수익률로 신호 필드 `adj_returns`로 쓴다. `returns_total.parquet`이 남은 옛 thin 캐시(rc2f3 등)는 설정 로드에서 거부되므로 다시 빌드한다. `adj_factor.parquet`은 분할 단위 변환에 쓰고, `events.parquet`은 종료·승계·자회사 position 이동만 담는다. 현금·주식 대가를 엔진에서 다시 평가하지 않는다. `universe.parquet`은 구간형 멤버십이므로 `universe.path`와 함께 `universe.name`(또는 `universe.names`)을 지정한다. 구간의 `fromDate`는 빌더가 그날 종가로 결정한 날이므로 로더가 `backtest.pit_lag`(기본 1) bar만큼 늦춰 적용한다. 그룹 코드도 같은 지연을 받는다. 이름은 해당 캐시 `universe.parquet`의 `universe` 열에 있는 구간 이름 중 하나여야 한다(예: `TOP3000`). 대소문자까지 정확히 일치해야 하며, 없는 이름은 시작 시 실패한다. 이동 목적지 종목은 로더가 패널에 추가한다.

설정에는 최상위 `events:`나 `backtest.corporate_actions:`를 넣지 않는다. thin은 해당 값들을 `returns`(총수익률)와 이동 행에 반영하며, 현재 로더는 옛 설정 블록을 포함해 읽지 않는 모든 키를 `Unknown config key` 오류로 거부한다. 미해결 사건의 임시 정산값과 대기 자회사 사례는 캐시 manifest의 `transitional`에 기록된다(현재 캐시에도 있음). 새 결과를 확정된 정제 데이터로 해석하지 않는다. 다른 thin 캐시로 바꿀 때는 manifest·구간형 유니버스 이름·그룹 패널 범위를 확인한다. AFL은 별도의 기존 wide 입력으로 유지된다.

아래 `BaseData/*.parquet` 설명은 manifest가 thin-v1이 아닌 기존 wide 캐시에 적용한다. 일반 long/wide `datasets:` 경로도 계속 지원한다.

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
| `returns` | 없음(wide) / `<cache>/returns.parquet`(thin-v1) | wide에서는 `close`에서 파생, thin에서는 캐시의 총수익률 |
| `adj_returns` | `<cache>/adj_returns.parquet`(thin-v1만) | 분할조정 가격 수익률 |

- nonthin wide 캐시는 `base_prices`(7필드, wide)와 `base_groups`(2필드, wide)로 전개된다. thin-v1은 `base_prices`와 `base_returns`(총수익률)·`base_adj_returns`(분할조정)를 long 형식으로 읽고, 같은 `base_groups`를 사용한다. 모두 eager이며 사용자 `datasets:`보다 앞에 놓인다. 이 이름들은 예약되어 있다.
- `cache`·`groups`는 둘 다 필수다. 경로 규약은 다른 항목과 같아서 상대경로는 이 YAML이 있는 디렉토리 기준으로 푼다.
- `base:`가 있으면 `datasets:`는 생략하거나 빈 시퀀스로 둘 수 있다. 둘 다 없으면 기존처럼 `datasets must be a non-empty sequence` 오류다.
- `datasets:`에서 `close` 같은 기본 필드를 다시 선언하면 로드가 `Duplicate public data field: close`로 실패한다. 추가 데이터셋에는 다른 이름의 필드만 적는다.
- 기본 필드는 시작 시 전부 읽는다(lazy 아님). 파일이 하나라도 없으면 `Cannot open parquet <파일 경로>`로 기동이 실패한다. 이벤트 파일과 같은 규약이며 조용한 부재를 막는다.
- 그룹 코드 규약과 `sector.parquet`·`industry.parquet` 생성은 그룹 패널 도구(`universe_builder/cache/README_build_groups.md`, 캐시 빌더 저장소에 있음)를 따른다. 가격 캐시를 다시 만들면 그룹 파일도 다시 만든다.
- 그룹 패널은 `alignment: asof`라 가격 캐시가 그룹 파일보다 뒤로 늘어나면 마지막 분류가 그대로 이어진다(일간 캐시 갱신마다 재생성할 필요 없음). 단 그룹 파일에 없는 **새 심볼**은 재생성 전까지 미분류(NaN)로 남아 `neutralize`에서 빠지고, 그룹 파일이 가격 달력보다 늦게 시작하거나 다른 캐시용이면 코드 없는 bar가 생겨 로더가 기동을 거부한다(`Group panel '<field>' has no codes on <date>`). 캐시를 재구축했거나 심볼이 늘었으면 그룹 파일을 다시 생성한다.

메모리는 필드당 `T×N×4B`다.

| 패널 | 크기 | 필드 1개 | 기본 9필드 |
|---|---|---|---|
| Top500 | 1,056종목 × 4,449일 | 19MB | 0.17GB |
| ALL | 22,871종목 × 6,713일 | 614MB | 5.5GB |

전환 전 예제도 가격 5필드를 eager로 읽어 ALL에서 3GB를 썼다. `base:`는 여기에 `vwap`·`cap`·`sector`·`industry` 4필드를 더 읽는다.

가격·유니버스는 시작 시 읽는다. AFL은 600개 팩터를 팩터당 이름 하나(`AFL.<이름>`)로 등록하고, `DataPanel::row()`/`at()`에서 처음 참조하는 필드만 읽는다. XML뿐 아니라 사용자 C++ 모듈에서도 같은 경로를 사용한다. 동일 파일을 가리키는 이름들은 한 엔진 프로세스 안에서 값을 공유한다. 별도 엔진 프로세스는 별도 RAM을 사용한다. 사용한 필드는 프로세스 종료까지 유지되므로, 장시간 여러 팩터를 탐색하면 캐시 사용량도 늘어난다.

```xml
<data expr="AFL.EPSNumRevFY1C" delay="1"/>
<rank/>
<zscore/>
```

XML에서는 `AFL.EPSNumRevFY1C`(`963.parquet`)처럼 데이터셋 이름을 붙여 쓴다. 숫자로 시작하는 FactorCode도 그대로 쓰고(`AFL.10DMACD`), `PM-style`처럼 `[A-Za-z0-9_]` 밖 문자는 `_`로 바뀐다(`AFL.PM_style`). 숫자 ID로는 가리키지 않는다(`AFL.104`, `afl_104` 없음). 접두사 없이 쓰면 오류 메시지가 `AFL.<이름>`을 제안한다. 접두사 없는 `returns`는 wide에서는 종가 비율에서 파생되고 thin에서는 캐시 필드다.

카탈로그에는 별칭을 넣지 않는다. `number_of_revision` 같은 팀 별칭이 필요하면 개인 config의 AFL 데이터셋에 `fields: {number_of_revision: 963.parquet}`처럼 어떤 원본 파일인지 직접 적는다(**EPSNumRevFY1C(963)**, 조정 팩터 **AdjEPSNumRevFY1C(960)**와 별개). XML에서는 `AFL.number_of_revision`이 되고 `AFL.EPSNumRevFY1C`와 값을 공유한다.

카탈로그 생성기는 데이터 빌더(`universe_builder/afl/build_afl_catalog.py`) 소유이며 캐시 발행 때 함께 만들어진다. 엔진은 카탈로그를 읽기만 한다. 발행된 카탈로그에는 원본 사전의 SHA-256이 기록돼 있다.

## 시간·유니버스 규칙

실행 중인 `config.example.yaml`의 기본 체결가는 `close`다. 화면의 Execution 선택창에서 실행마다 Close/OHLC4/VWAP를 바꿀 수 있다. 일반 설정에서 생략하면 `close`이며 허용값은 `close|ohlc4|vwap`뿐이다. VWAP는 raw 당일 `vwap`이 체결가이고 exact `vwap/high/low`가 필요하며, 결측·범위 불일치(vwap/close가 low/high 밖)·오류 규칙은 OHLC4와 같다. OHLC4는 raw `(open + high + low + close) / 4`다. `open/high/low/close` 모두 exact 정렬 원천이 필요하고, `selected_fields`를 제한하거나 lazy로 선언해도 체결 필드는 시작 시 읽는다. 필드·파일·컬럼이 없거나 asof 가격 원천이면 로딩이 실패하며 종가로 대체하지 않는다. 원본 OHLC는 바꾸지 않는다. `/api/config`는 기본 `executionPrice`와 지원하는 `executionPrices`를 제공한다. `/api/simulate`와 `/api/ga/stream`은 선택한 `executionPrice`를 요청마다 적용하며, 서버 기본값을 바꾸지 않는다.

계산은 원래 팀의 고정 명목금액 근사(fixed-notional approximation)로, 정확한 주식 수 원장은 아니다. OHLC4는 기존 보유 손익에 실제 시장 매매 비중 × `(close / OHLC4 - 1)`의 추가 매매 손익을 더한다. 기업행동 position 이동 자체는 이 추가 손익이나 시장 매매 수수료를 만들지 않는다. 수수료와 목표 명목금액 규칙은 유지하며, 지연은 XML의 명시적 delay만 적용한다. 단일·combined·GA 모두 같은 체결 설정을 사용한다.

개별 OHLC 셀이 결측이면 `missing_close_policy: carry`는 해당 매매를 막고 기존 노출을 동결·예약한다. 이때 관측 종가 표식과 보유 수익률은 정상 반영한다. `error`는 필요한 매매를 실행할 수 없으면 실패한다. 유한하지만 잘못된 OHLC도 관련 매매를 거부한다. 셀 결측 처리는 원천 필드 자체가 없는 경우의 로딩 오류와 별개다.

- `end_date: latest` 또는 생략은 엔진 시작 시 선택한 종목들의 종가가 전부 NaN인 끝행을 제외하고 종료일을 정한다. Parquet이 갱신된 뒤 엔진을 재시작하면 새 날짜를 읽는다. 실행 중 자동 갱신과 버전 관리 기능은 없다. 열린 웹 화면은 새로고침해 날짜 범위를 갱신한다.
- 날짜를 직접 지정한 `end_date: '2026-07-31'`은 기존처럼 그 날짜까지만 읽으며, 마지막 행이 전부 결측이어도 임의로 기간을 줄이지 않는다.
- `latest`는 수집 완료를 보증하는 기준이 아니다. 중간의 전 종목 결측, 마지막 행의 일부 종목 결측, 0·음수·Inf 가격은 그대로 남는다. 거래 시작일 이후 종가가 전부 결측이면 로딩 오류를 반환한다.

- `warmup_bars`는 `start_date` 이전에 추가로 읽을 가격 bar 수다. `0`은 추가 과거 구간 없음이다. 사용하는 시계열 연산의 누적 lookback과 지연을 감안해 지정한다. 이 구간에서는 신호·모듈 상태만 준비하고 매매·비용·보유손익은 발생하지 않는다.
- 유니버스는 날짜별 별도 마스크다. 가격 원본을 NaN으로 덮지 않는다. `rank`와 `zscore`는 해당 신호 날짜의 유니버스 안에서 계산하고, 최종 목표 포지션도 같은 마스크를 적용한다.
- Interval/`mode`와 암묵적 목표 포지션 지연은 제거했다. XML 출력 신호는 현재 날짜의 마스크로 정규화·체결한다. `delay="1"`은 XML에서 명시한다. 데이터 지연과 유니버스 마스크 지연은 서로 다르며, 마스크를 함께 shift하지 않는다.
- 보유 중인 종목의 종가가 없을 때의 처리는 `backtest.missing_close_policy`가 정한다. 기본 `carry`는 보유를 동결하고, `error`는 기존처럼 실행을 실패시킨다. 유니버스 이탈과 실제 가격 결측은 여전히 다른 상태다.
- `alignment: exact`는 같은 시각의 관측만 사용한다. `asof`는 해당 시각 이전의 마지막 관측을 유지하고, 최초 관측 이전은 NaN이다. 미래 값을 과거로 채우지 않는다. 파일의 날짜가 실제로 사용 가능한 시각이라는 데이터 계약이 필요하며, 이 옵션이 사후 수정 데이터의 과거 버전을 복원하지는 않는다.
- AFL 데이터셋은 `alignment: asof`로 읽는다. `asof` 결과가 매일 있어도 원천이 매일 갱신된다는 뜻은 아니다(월별 관측 캐시 `AflData_M`은 월 1회 값을 유지한다). 원천의 관측 주기와 사용 가능 날짜를 확인한 뒤 경로를 고른다.

## 결측 종가와 일반 nonthin 데이터

`backtest.missing_close_policy: carry`는 보유 종목에 호가가 없을 때 position을 동결하고, 시세가 재개되면 누적 수익을 인식한다. `error`는 결측에서 실행을 중단한다. thin-v1에서는 종료 손익과 자회사 손익이 `returns.parquet`(총수익률)에 포함되고, `events.parquet`은 position 이동만 담는다. 이 파일은 최상위 `events:` 설정과 다른 형식이다.

일반 long/wide `datasets:`와 manifest가 thin-v1이 아닌 wide `base.cache`는 계속 읽을 수 있다. 이 경로에서 XML의 `returns`는 종가 비율에서 파생되며, 외부 수익률은 `holding_return` 같은 다른 필드 이름으로 등록한다. 이 일반 데이터 경로에는 thin의 총수익률 손익·이동 행이 없으므로 thin 기업행동 손익 계약을 제공하지 않는다. 기존 최상위 `events:`, `backtest.corporate_actions:`, `default_delisting_return`, `stale_quote_settle_bars` 설정은 현재 로더가 거부한다. 현재 계약은 [데이터 계약](data_contract.md), 전환과 검증 근거는 데이터·손익 연구 노트북에 있다.

OHLC4의 O/H/L 셀 결측 또는 유한한 범위 불일치(open/close가 low/high 밖)는 `carry`에서 주문을 보류하고 기존 노출을 예약한다. `error`는 필요한 체결을 거부한다. 0 이하·무한대 가격은 관련 포지션에서 오류다.
