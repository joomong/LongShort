# 2026-09-11 데이터 연결·빌드 검증

## 적용 범위

기존 long Parquet 입력을 유지하면서 필드별 wide Parquet 직접 읽기를 추가했다. 값 데이터를 변환하는 운영용 Python ETL은 추가하지 않았다. Python은 AFL 메타데이터 카탈로그 생성과 C++ 결과·원천 파일의 독립 검증에 사용한다.

- enabled dataset을 여러 개 합치며 공개 field 이름 중복과 catalog/inline mapping 충돌을 거절한다. 기존 use_col·fee bp·universe:[all] 설정도 읽는다.
- wide는 파일마다 독립적인 날짜/종목 좌표를 읽어 close 파일의 공통 거래일·종목 축으로 재배열한다. 원본 행·열의 순서가 같다고 가정하지 않는다.
- long은 요청한 값 컬럼만, wide는 요청 종목 컬럼을 최대 64개씩 읽는다. 필요한 원본 행이 속한 row group만 값 읽기에 포함한다. 필터를 찾고 중복을 검사하기 위한 좌표·footer는 별도로 읽는다. 하나의 큰 row group 안에서는 그 group 경계 밖 날짜 값도 디코드될 수 있다.
- timestamp[s/ms/us/ns], date32/date64와 기존 epoch_ms int64를 처리한다. null 좌표, 중복 날짜/종목, 모호한 컬럼 이름과 지원하지 않는 타입을 거절한다. sub-ms 정보를 조용히 버리지 않는다.
- signed/unsigned 정수와 float/double/bool을 float32 값으로 변환한다. null은 NaN으로 보존하고 유한 double이 float32 범위를 넘으면 오류를 반환한다.
- PIT membership은 별도의 uint8 universe_mask이며 원가격을 덮어쓰지 않는다. 필터 구간과 warmup에서 한 번이라도 활성인 종목을 모아 종목 축을 고정한다. loader는 mask를 shift하지 않는다.
- backtest.start_date/end_date를 실제 적재에 적용한다. warmup_bars만큼 이전 close bar를 남기고 trade_start_index로 거래 시작을 구분한다. warmup_bars=0은 사전 bar 없음이다.

## 설정 계약

```yaml
datasets:
  - name: prices
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/BaseData
    fields:
      close: close.parquet
      volume: volume.parquet
  - name: afl
    format: wide
    path: /home/base/cache/cache_v1_cash_1d/AflData_M
    catalog: afl_catalog.json
    lazy: true
    alignment: asof
universe:
  path: /home/base/cache/cache_v1_cash_1d/TopUniverseV1/Top500.parquet
  symbols: ['100306421', '100667466']
backtest:
  start_date: '2025-01-02'
  end_date: '2025-03-31'
  warmup_bars: 60
  bars_per_year: 252
  transaction_fee: 5
  combined_mode: normalized
  mode: 1
```

모든 상대 경로는 config 파일 위치를 기준으로 해석한다. fields의 wide 파일명은 dataset.path 기준이며 catalog 안의 파일명도 같은 기준이다. wide 기본 time_col은 __index_level_0__, long 기본값은 open_time/symbol이다. catalog는 JSON 또는 YAML의 fields mapping을 읽으며, catalog 사용 시 lazy 미지정도 true이다. 일반 dataset은 eager가 기본이다. backtest.mode는 기존 D0=0/D1=1만 허용하며 기본값은 1이다.

alignment 기본값 exact는 같은 날짜의 관측만 사용한다. asof는 해당 날짜 이하의 가장 최근 **관측 행**을 사용한다. 최신 행의 값이 null이면 과거의 non-null까지 추가로 채우지 않는다. 최초 관측보다 이전을 미래 값으로 backfill하지 않는다. 원천 index가 실제 관측 가능 시각을 나타낸다는 가정은 데이터 제공 쪽 계약이며, 새로운 발표 시차를 추정해서 추가하지 않았다.

returns는 close에서 계산하는 기존 파생 식별자이므로 입력 mapping에 사용할 수 없다. 원천 수익률을 노출할 때는 holding_return 같은 별도 이름을 사용한다. 주식 배당·trading leg·다른 체결 모델은 이 로더 변경에 포함하지 않는다.

## Lazy 데이터와 메모리

DataPanel의 eager arrays와 별도로 immutable lazy_columns map을 기동 전에 만든다. 각 LazyColumn은 call_once로 값 하나를 읽으며, 동일 파일·정렬 규칙을 가리키는 lazy aliases는 같은 버퍼를 공유한다. 읽기 실패 시 once_flag는 이후 요청에서 재시도할 수 있다. callback은 공통 날짜·종목 snapshot을 소유하므로 임시 reader의 수명에 의존하지 않는다.

has_column/column_names는 값 로딩 없이 metadata를 반환한다. loaded_field_count/loaded_bytes는 lazy aliases를 중복 계산하지 않는다. 컬럼을 사용할수록 값은 상주하므로 모든 AFL을 실제로 접근하는 긴 세션의 상한/LRU는 별도 과제다. 600개가 등록됐다는 이유만으로 600개의 값 행렬을 읽지 않는다.

## 재현 검증

새 tests/test_config_loader.cpp 4개와 tests/test_parquet_loader.cpp 7개는 실제 Arrow writer로 작은 파일을 만들어 검사한다. 날짜·종목 재배열, PIT 이탈 뒤 가격 보존, 누락 종목·날짜, 정수 타입, 모든 timestamp 단위, 중복/null/문자 타입, 선택 안 한 파일 미접근, asof seed/null 및 alias 8스레드 동시 읽기를 포함한다.

`data_load_probe CONFIG [--summary] [FIELD ...]`는 실제 C++ 로딩 결과, 적재 필드/바이트, 시간과 process peak RSS를 JSON으로 출력한다. `check_wide_data_oracle.py`는 실제 Top500에서 몇 종목을 선택하고 Arrow/NumPy로 독립 비교한다.

```bash
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/check_wide_data_oracle.py \
  --probe /tmp/longshort_module_server/build/data_load_probe \
  --output-dir /tmp/longshort_stock_oracle_20260911
```

최초 실측: 6종목, 2025 Q1 + warmup 5bar = 65bar. close/volume/AFL100와 그 alias가 float32 기준 exact 일치했고 날짜·PIT mask도 일치했다. 600 factor의 1,192개 AFL 이름과 가격 2개를 등록하되 값은 eager 2개 → 총 3개만 읽었다. 실제 값 버퍼는 4,680byte다. 최초 측정의 전체 process peak RSS는 약 494MB, 총 시간은 약 0.84초였다. 수만 개 컬럼을 가진 wide 파일의 footer/schema와 Arrow 임시 메모리는 값 버퍼보다 훨씬 크므로 패널 바이트만으로 프로세스 RAM을 추정하면 안 된다.

master reader 수명을 정리한 최종 재측정에서도 값·mask는 exact 일치했다. 총 로드 시간은 약 0.814초, process peak RSS는 492,539,904byte(약 493MB)로 이전과 비슷했다. 값 버퍼 축소와 wide footer/schema 메모리 비용은 별개임을 확인했다. 최종 결과는 /tmp/longshort_stock_oracle_final_20260911/report.json에 있다. 원본 캐시·체크포인트 fixture·팀 sim 코드는 수정하지 않았다.


## 최종 전체 검증

최신 mode 0/1 연결, 숫자 파서 예외 정규화 및 histogram 극값 처리까지 실제 CMake로 빌드했다. **CTest 25/25 target, C++ 184개와 Python 36개가 전부 통과했고 실패/skip은 0이다.** HTTP 13개, config/Parquet 11개, 기존 Boost checkpoint 6개, NumPy golden, SIMD 및 2,000알파 스트레스가 포함된다.

```bash
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/make_http_test_fixture.py \
  --output-dir /tmp/longshort_http_data_20260911
cmake --build /tmp/longshort_module_server/build --parallel 6
LONGSHORT_TEST_SERVER=/tmp/longshort_module_server/build/longshort-server \
LONGSHORT_TEST_CONFIG=/tmp/longshort_http_data_20260911/config.yaml \
ctest --test-dir /tmp/longshort_module_server/build --output-on-failure -j 4
```

집계 결과는 `/tmp/longshort_full_tests_final_20260911.json`, 원본 JUnit은 같은 이름의 `.xml`이다. 고립된 빌드 디렉터리에서도 golden/checkpoint fixture를 찾도록 CMake 테스트 작업 위치를 지정했다. HTTP는 명시적 server/config 환경변수를 사용해 누락 fixture로 인한 전체 skip 없이 실행했다.

기존 `run_logic_tests.py`는 가벼운 in-memory core 경로이며, 새 config/Parquet/HTTP 검증은 완전한 의존성을 갖춘 CTest에서 실행한다. `--with-tool-tests`에는 AFL catalog 생성기 검사도 포함했다. 코드와 원천 데이터 변경을 섞지 않았고, 데이터 loader와 별도 engine/API 담당자의 수정 및 독립 리뷰를 함께 검증했다.


## 후속: 동시 cold AFL 로딩의 메모리 겹침 제한

서로 다른 AFL을 최초 접근할 때 Parquet footer/schema 메모리가 worker 수만큼 동시에 겹칠 수 있어, `read_field` 전체 범위에 process-local mutex를 추가했다. 값 배열을 할당하기 전부터 reader가 해제될 때까지 잠금을 유지한다. 각 LazyColumn의 call_once와 이미 로드된 값 접근·시뮬레이션 계산은 변경하지 않았다.

원가격·비용을 바꾸지 않은 위 실제 주식 6종목/65bar config(1.5bp 검증 설정)에 `<data expr="afl_100|afl_963"/><zscore/>`, threads=2를 별도 새 서버 프로세스로 요청했다. 두 실행 모두 HTTP 200, loadedFields 2→4, 값 버퍼 6,240byte였고 cold/warm 및 변경 전후 전체 JSON 응답이 완전히 일치했다(동일한 key 정렬로 저장한 파일도 byte-identical).

| 단일 전후 측정 | gate 전 | gate 후 |
|---|---:|---:|
| 프로세스 peak RSS (VmHWM) | 630,636,544byte | 579,383,296byte |
| cold 요청 시간 | 320.5ms | 587.1ms |
| 이미 로드한 warm 요청 | 1.10ms | 1.32ms |

이 측정에서 peak는 약 51MB(8.1%) 줄었다. cold 로딩은 직렬화 때문에 느려졌고, allocator가 메모리를 유지하므로 RSS가 단일 필드 수준으로 떨어지는 것은 아니다. 이는 메모리 상한이나 모든 worker 수에 대한 보장이 아니다. 새 프로세스의 lazy field cache가 비어 있다는 뜻의 cold이며 OS 파일 캐시를 강제로 비우지 않았다.

변경 후 기존 config/Parquet 11개(동시 alias 접근 포함)가 모두 다시 통과했다. 관련 실제 HTTP 대조와 추가 측정은 `/tmp/longshort_cold_fields_20260911/{before,after,comparison}.json` 및 `benchmark.py`에 남겼다. 제품 수정은 `parquet_reader.cpp`의 mutex gate 5줄뿐이며 원천 값·원가격·비용·PnL 경로를 우회하지 않았다.
