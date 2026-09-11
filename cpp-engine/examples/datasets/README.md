# C++ wide Parquet 연결 예제

`equities_monthly_afl.yaml`은 서버의 일별 가격·Top500 마스크와 기존 `AflData_M`을 직접 읽는다. 개인 폴더에 YAML과 `afl_catalog.json`을 함께 복사한 뒤 실행한다. C++ 빌드 환경 최초 설정은 [빌드 안내](../../../docs/module_discovery_build_2026-09-09.md)를 따른다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/my/equities_monthly_afl.yaml
```

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
- 보유 중인 종목의 평가·청산 종가가 없으면 실행이 실패한다. 유니버스 이탈과 실제 가격 결측은 다른 상태다.
- `alignment: exact`는 같은 시각의 관측만 사용한다. `asof`는 해당 시각 이전의 마지막 관측을 유지하고, 최초 관측 이전은 NaN이다. 미래 값을 과거로 채우지 않는다. 파일의 날짜가 실제로 사용 가능한 시각이라는 데이터 계약이 필요하며, 이 옵션이 사후 수정 데이터의 과거 버전을 복원하지는 않는다.
- 이 예제의 `AflData_M`은 월별 관측 캐시다. 2026-09-10 확인 당시 963번 팩터의 마지막 관측은 2026-08-28이었다. `asof` 결과가 매일 있어도 매일 갱신된 AFL 데이터는 아니다. 일별 AFL을 쓰려면 일별 원천 데이터와 사용 가능 날짜를 검증한 파일로 경로를 바꾼다.

`GET /api/config`의 `columns`는 사용할 수 있는 이름 목록, `loadedFields`와 `loadedDataBytes`는 읽은 수치 필드 수와 값 배열 크기다. 후자는 유니버스 마스크, 모듈 상태, 시그널 플롯, Arrow 읽기 버퍼를 포함한 전체 RSS와 다르다. 현재 설정과 파일은 엔진 재시작 때 다시 읽는다.
