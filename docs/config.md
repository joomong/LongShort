# 설정 YAML과 데이터 연결

개인 설정은 `~/longshort_workspace/config.yaml`에 둔다. `longshort start`가 설정이 없는 workspace에 [설정 예제](../config.example.yaml)를 복사한다. `LONGSHORT_WORKSPACE`로 위치를 바꿀 수 있다. 모든 상대경로는 YAML이 있는 디렉터리를 기준으로 해석한다.

```yaml
base:
  cache: /home/base/cache/cache_v2_cash_1d
  groups: /home/base/cache/cache_v2_cash_1d/groups

afl:
  path: /home/base/cache/cache_v2_cash_1d/AflData
  catalog: /home/base/cache/cache_v2_cash_1d/afl_catalog.json

universe:
  path: /home/base/cache/cache_v2_cash_1d/universe.parquet
  names: [TOP3000, TOP500_CAP]

backtest:
  start_date: '2000-01-01'
  end_date: latest
  initial_capital: 1000000
  transaction_fee: 1.5
  bars_per_year: 252
  execution_price: vwap
  missing_close_policy: carry
```

`base.cache`는 캐시 루트다. `base.groups`는 sector·industry 패널 디렉터리이며 생략하면 `<base.cache>/groups`다. `afl` 블록을 지정하면 AFL을 사용한다. 블록 안의 기본 경로는 `path: <base.cache>/AflData`, `catalog: <base.cache>/afl_catalog.json`이며 `afl: {}`로 이 경로를 사용할 수 있다. 카탈로그의 `fields`에 있는 파일 경로는 `afl.path` 기준이다.

## 필드와 메모리

| 공개 필드 | 읽는 파일 |
|---|---|
| `open high low close volume vwap cap` | `<base.cache>/base_data.parquet` |
| `returns` | `<base.cache>/returns.parquet` |
| `adj_returns` | `<base.cache>/adj_returns.parquet` |
| `adj_factor` | `<base.cache>/adj_factor.parquet` |
| `sector industry` | `<base.groups>/{sector,industry}.parquet` |
| `AFL.<이름>` | AFL 카탈로그가 가리키는 필드 파일 |

`returns`는 총수익률이며 보유 손익에도 사용한다. `adj_returns`는 분할조정 가격수익률이다. `adj_factor`는 분할 기준 계수다. 기업행동의 position 이동은 `<base.cache>/events.parquet`에서 읽는다.

AFL·그룹 입력은 해당 날짜 이하의 마지막 관측을 사용한다. 관측값이 null이면 null을 유지하고 미래 관측으로 과거를 채우지 않는다. AFL은 카탈로그에 이름을 등록하고 `DataPanel::row()`/`at()`에서 처음 참조할 때만 값을 읽는다. 같은 파일을 가리키는 필드는 값을 공유한다. 사용한 필드는 엔진 프로세스가 종료될 때까지 유지하며, 별도 엔진 프로세스는 별도 RAM을 사용한다.

```xml
<data expr="AFL.EPSNumRevFY1C" delay="1"/>
<rank/>
<zscore/>
```

XML에서는 `AFL.EPSNumRevFY1C`, `AFL.10DMACD`처럼 카탈로그의 필드 이름 앞에 `AFL.`을 붙인다. 이름과 파일의 대응은 캐시 빌더가 발행하는 카탈로그를 따른다.

## 기간·유니버스·지연

`universe.path`의 기본값은 `<base.cache>/universe.parquet`다. `universe.name`은 사용할 집합 하나를, `universe.names`는 실행 중 선택할 집합 목록을 지정한다. `name`을 생략한 목록의 기본 집합은 첫 이름이다. `name`과 `names`를 모두 생략하면 가격에 있는 전체 종목을 사용한다. `universe.symbols`는 읽을 종목 ID를 문자열 목록으로 제한할 때 사용한다. 유니버스 마스크는 가격과 별도로 보관하고 가격 원본을 바꾸지 않는다.

sector·industry·모든 universe 마스크는 로딩 시 이전 거래일 기준으로 구성한다. 화요일 실행에는 월요일 기준 분류·멤버십을 사용한다. 첫 로드 bar는 그룹 NaN·멤버십 비포함이다. XML의 `delay=0`, `1`, `5` 중 무엇을 사용하더라도 이 메타데이터 규칙은 같다. 가격·수익률·이벤트는 발생 날짜 그대로 보관한다.

`backtest.start_date`는 로드할 데이터의 시작일이다. 각 실행의 Sim period는 UI/API에서 별도로 선택한다. 계산은 simStart 하루 전 bar부터 시작하며 그 이전 bar는 건드리지 않는다. 첫 매매는 첫 IS bar(`max(isStart, simStart)`)에서 일어나며, 그 전 bar는 의도와 평가가격만 준비하고 매매·보유 손익·비용은 만들지 않는다. 신호는 `max(isStart, simStart)` 전 bar까지 유효해야 하며 아니면 `Insufficient history`로 거부된다. `delay="1"`이나 lookback이 있으면 isStart를 simStart보다 그만큼 뒤로 잡는다.

`end_date: latest` 또는 생략은 엔진 시작 시 선택한 종목들의 종가가 전부 NaN인 끝행을 제외하고 종료일을 정한다. 명시적인 종료일은 그 날짜까지 읽는다. 캐시가 갱신되면 엔진을 재시작해 새 데이터를 읽고 화면을 새로고침한다.

`initial_capital`은 설정상의 초기 자본이다. 화면의 성과 금액은 결과 context의 `reportNotional`($20M 전체 gross 명목금액)을 기준으로 표시한다. 두 값은 서로 독립적이다.

## 체결과 저장

화면의 Execution 선택창에서 실행별로 Close/OHLC4/VWAP를 고른다. 설정 예제는 `vwap`이며 설정 기본값은 `close`다. VWAP는 raw 당일 `vwap`, OHLC4는 raw `(open + high + low + close) / 4`로 체결한다. 시뮬레이션은 고정 명목금액 근사이며 정확한 주식 수·현금 원장은 아니다.

`missing_close_policy: carry`는 체결할 수 없는 주문을 보류하고 기존 노출을 동결·예약한다. `error`는 필요한 거래를 수행할 수 없으면 실행을 실패시킨다. 지연과 체결·수수료의 상세 규칙은 [계산 계약](math_contract.md)을 따른다.

`alphas_dir`는 웹에서 저장할 알파 디렉터리이며 기본값은 설정 옆 `alphas`다. 저장 형태는 `<name>/{alpha.xml, metadata.yml, results/, positions/}`다. `meta_dir`는 `<meta expr="이름"/>`이 읽을 구성원 XML 디렉터리이며 기본값은 설정 옆 `meta`다. 저장 결과는 실행 당시 context를 보존한다. 현재 엔진에 원래 유니버스가 없으면 그 기준의 상세 조회를 사용할 수 없으며, 저장한 지표에 다른 유니버스의 신호를 결합하지 않는다. 다른 설정으로 실행하면 별도의 새 결과가 된다.

웹에서 저장하는 `alpha.xml`은 태그별 줄바꿈과 2칸 들여쓰기를 사용한다. `<meta>`를 사용하는 알파도 같은 형식으로 저장하며 주석과 수식의 의미를 유지한다. 기존 저장 파일을 일괄 재작성하지 않는다.

`alpha_pool`은 승인 알파 풀 경로다. 엔진이 `<이름>/alpha.xml`을 읽어 풀의 동일가중 일별 손익과 실행 결과의 상관관계를 계산하고 화면의 `Corr`에 표시한다. 풀에는 쓰지 않는다. `alpha_pool_universe`는 풀 평가 유니버스이며 생략하면 기본 유니버스를 사용한다.

승인 알파는 `/home/base/longshort/alpha_pool/`에 둔다. Git은 `longshort_platform/` 코드 저장소에서만 사용한다. 예제의 상대 경로는 설정 파일 위치를 기준으로 해석한다. `longshort`가 새 workspace 설정을 만들 때는 플랫폼 옆 `../alpha_pool/`의 절대 경로를 기록한다. `meta_pool/`과 `portfolio/`는 전략 정의를 둘 디렉터리이며 현재 실행 경로에는 연결하지 않았다.

UI 종목 라벨은 캐시의 `master.parquet`에서 `symbol`, 없으면 `companyName`을 사용한다. `backtest.symbol_labels_path` JSON을 지정하면 해당 라벨을 우선한다.

```bash
./longshort start --port 50017 --ui-port 50016
```

빌드와 시작 방법은 [빌드 안내](build_and_validation.md), 파일별 의미는 [데이터 계약](data_contract.md)을 따른다.
