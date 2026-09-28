# C++ Parquet 데이터 연결 예제

현재 thin-v1 예제는 `equities_monthly_afl.yaml`·`thin_rc2f3.yaml`(Top500)과 `compare_v2_all.yaml`(ALL)이다. 모두 같은 rc2f3 thin 캐시와 기존 월별 `AflData_M`을 읽는다. 이전 v1/v2/Altaba 비교 YAML은 [전환 계획의 부록](../../../docs/backtest_cache_simplification_plan_2026-09-23.md)에 원문으로 보존했다. 개인 폴더에 YAML과 `afl_catalog.json`을 함께 복사한 뒤 실행한다. C++ 빌드 환경 최초 설정은 [빌드 안내](../../../docs/module_discovery_build_2026-09-09.md)를 따른다.

thin 예제는 `master.parquet`의 `tradingItemId`와 `symbol`에서 UI 라벨을 읽는다(`symbol`이 없으면 `companyName`). 명시적인 `backtest.symbol_labels_path` JSON이 있으면 그 파일이 우선한다. ALL 예제는 옛 캐시의 라벨 파일에 의존하지 않는다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/my/equities_monthly_afl.yaml
```

## thin-v1 캐시

`base.cache/manifest.json`의 `layout: thin-v1`을 로더가 판별한다. 이 캐시의 `base_data.parquet`은 조정 전(raw) 가격이며, `returns.parquet`은 CRSP식 총수익률(배당·분할·분사·합병/상폐 정산 반영)로 기본 신호 `returns`이자 보유 손익 입력이다. `adj_returns.parquet`은 분할조정 가격 수익률로 신호 필드 `adj_returns`로 쓴다. `returns_total.parquet`이 남은 옛 thin 캐시(rc2f3 등)는 설정 로드에서 거부되므로 다시 빌드한다. `adj_factor.parquet`은 분할 단위 변환에 쓰고, `events.parquet`은 종료·승계·자회사 position 이동만 담는다. 현금·주식 대가를 엔진에서 다시 평가하지 않는다. `universe.parquet`은 구간형 멤버십이므로 `universe.path`와 함께 `universe.name: Top500` 또는 `ALL`을 지정한다. 이동 목적지 종목은 로더가 패널에 추가한다.

설정에는 최상위 `events:`나 `backtest.corporate_actions:`를 넣지 않는다. thin은 해당 값들을 `returns`(총수익률)와 이동 행에 반영하며, 현재 로더는 옛 설정 블록을 거부한다. 이 예제의 rc2f3 캐시는 전환 검증용으로, 미해결 사건의 임시 정산값과 대기 자회사 사례를 manifest에 기록한다. 새 결과를 확정된 정제 데이터로 해석하지 않는다. 다른 thin 캐시로 바꿀 때는 manifest·구간형 유니버스 이름·그룹 패널 범위를 확인한다. AFL은 별도의 기존 wide 입력으로 유지된다.

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

`EPSNumRevFY1C`와 `afl_963`은 모두 `963.parquet`을 가리킨다. 숫자로 시작하는 FactorCode는 `afl_` 접두사를 붙인다. 예를 들어 `10DMACD`는 `afl_10DMACD`, 숫자 ID로는 `afl_104`를 사용한다. `returns`는 wide에서는 종가 비율에서 파생되고 thin에서는 캐시 필드다. 외부 수익률 필드에는 `holding_return` 같은 별도 이름을 쓴다.

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

## 결측 종가와 일반 nonthin 데이터

`backtest.missing_close_policy: carry`는 보유 종목에 호가가 없을 때 position을 동결하고, 시세가 재개되면 누적 수익을 인식한다. `error`는 결측에서 실행을 중단한다. thin-v1에서는 종료 손익과 자회사 손익이 `returns.parquet`(총수익률)에 포함되고, `events.parquet`은 position 이동만 담는다. 이 파일은 최상위 `events:` 설정과 다른 형식이다.

일반 long/wide `datasets:`와 manifest가 thin-v1이 아닌 wide `base.cache`는 계속 읽을 수 있다. 이 경로에서 XML의 `returns`는 종가 비율에서 파생되며, 외부 수익률은 `holding_return` 같은 다른 필드 이름으로 등록한다. 이 일반 데이터 경로에는 thin의 총수익률 손익·이동 행이 없으므로 thin 기업행동 손익 계약을 제공하지 않는다. 기존 최상위 `events:`, `backtest.corporate_actions:`, `default_delisting_return`, `stale_quote_settle_bars` 설정은 현재 로더가 거부한다. 이전 계약과 검증 기록은 [과거 기업행동 엔진 문서](../../../docs/corporate_actions_engine_2026-09-17.md)에 남아 있다.
