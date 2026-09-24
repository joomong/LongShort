# 그룹 패널 (`sector`, `industry`) 생성

`<neutralize group="sector|industry"/>`가 읽는 그룹 코드 패널을 만드는 도구다
(`cpp-engine/tools/data/build_group_panels.py`). 설계 근거는 [neutralize 계획](../../../docs/neutralize_plan_2026-09-16.md) §2~§4.1.

## 1. 사용법

```bash
# v1 캐시 (base sector.parquet과 대조까지)
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/data/build_group_panels.py \
  --master /home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet \
  --cache  /home/base/cache/cache_v1_cash_1d \
  --output data/groups/cache_v1_cash_1d \
  --sector-index /home/base/cache/cache_v1_cash_1d/sector/sectorIx.parquet \
  --check-against /home/base/cache/cache_v1_cash_1d/sector/sector.parquet

# v2 캐시 (코드 순서를 v1과 맞추려고 같은 sectorIx를 넘긴다)
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/data/build_group_panels.py \
  --master /home/jgo/61/data2/temp_stock/cache_v2_cash_1d/Master/master.parquet \
  --cache  /home/jgo/61/data2/temp_stock/cache_v2_cash_1d \
  --output data/groups/cache_v2_cash_1d \
  --sector-index /home/base/cache/cache_v1_cash_1d/sector/sectorIx.parquet
```

| 옵션 | 뜻 |
|---|---|
| `--master` | 마스터 parquet. `tradingItemId`가 인덱스(v1)여도 컬럼(v2)이어도 된다. `sector`·`simpleIndustryDescription`만 읽는다 |
| `--cache` | 가격 캐시 루트. **`BaseData/close.parquet`의 index/columns가 달력·심볼 축**이다(값은 읽지 않는다) |
| `--output` | 출력 디렉토리. `data/`는 gitignore 대상 |
| `--sector-index` | base `sectorIx.parquet`. sector 코드 순서를 고정한다. 생략하면 라벨 정렬 순 |
| `--industry-index` | 기존 `industryIx.parquet`. 재생성 때 industry 코드를 고정하고 싶을 때만 |
| `--check-against` | 기존 wide sector 패널과 셀 단위 대조. 결과는 `build_report.md` §6 |
| `--row-group-rows` | parquet row group 행 수(0 = 단일 row group, `close.parquet`과 같음) |
| `--check-batch-columns` | 대조 시 한 번에 읽는 열 수(기본 1000) |

설정에서는 `base.groups`가 이 출력 디렉토리를 가리킨다. **`base.cache`와 짝이 맞는 디렉토리를 써야 한다** — 다른 캐시로 만든 그룹 패널을 가리키면 달력·심볼이 어긋나 조용히 NaN이 늘어난다(어떤 캐시로 만들었는지는 `MANIFEST.json`의 `inputs.cache`에 남는다).

```yaml
base:
  cache: /home/base/cache/cache_v1_cash_1d
  groups: ../../../data/groups/cache_v1_cash_1d
```

## 2. 산출물과 규약

| 파일 | 내용 |
|---|---|
| `sector.parquet`, `industry.parquet` | wide **float32**. index·columns·순서가 같은 캐시의 `BaseData/close.parquet`과 **동일**. 인덱스 컬럼 이름은 엔진 wide 로더 기본값 `__index_level_0__`(timestamp[ns]) |
| `sectorIx.parquet`, `industryIx.parquet` | 코드→라벨. 컬럼 `sectorIx`/`industryIx`, **0행 None**(base `sector/sectorIx.parquet`과 같은 구조) |
| `build_report.md` | 축·매칭·NaN·라벨표·대조 결과·sha256 |
| `MANIFEST.json` | 같은 수치를 JSON으로(파일별 sha256 포함) |

- 코드는 정수 `1..K`를 float로 저장하고 **미분류는 NaN**이다. 엔진은 "유한하고 ≥ 0"이면 그룹으로 보므로
  (팀 `cs_group_neut_1d`와 같은 규약) NaN 심볼은 중립화 출력이 NaN이 된다.
- `sector` K=11, 코드 순서는 `--sector-index` 그대로(1 Energy, 2 Information Technology, 3 Industrials,
  4 Materials, 5 Financials, 6 Consumer Discretionary, 7 Consumer Staples, 8 Real Estate,
  9 Communication Services, 10 Health Care, 11 Utilities).
- `industry` = 마스터 `simpleIndustryDescription`, K=74, 라벨 **정렬 순** 1..74.
- 중첩 검사: 한 industry가 두 sector에 걸치면 **exit 1**이고 아무 파일도 쓰지 않는다.
- 마스터의 `tradingItemId` 중복은 첫 행만 쓴다. 라벨 null·빈 문자열은 미분류(NaN)다.
- 쓰기는 결정적이다(고정 pyarrow 옵션, 출력 어디에도 시각 없음). 같은 입력을 다시 돌리면 네 parquet의
  sha256이 같다 — 실데이터 v1(28,210열)에서 확인함.
- 축은 **`close.parquet`만** 기준으로 삼는다. v1 캐시의 `BaseData/tradingItemId.parquet`은 심볼 집합은
  같지만 **순서가 다르고**(16713번째부터), base `sector/sector.parquet`의 열 순서 또한 다르다(17840번째부터).
  위치로 짝지으면 어긋나므로 이름으로 맞춰야 한다. 일치 여부는 `build_report.md` §2에 기록된다.

## 3. 한계

- **정적 분류**다. 현재 마스터 라벨을 전 기간에 방송하므로 과거 구간에는 look-ahead가 있다.
  base `sector/sector.parquet`도 같은 방식이고, PIT 분류 이력은 마스터 아카이브에 2025년 말부터만 있어
  현재로선 만들 수 없다(계획 §2.2). 이력이 생기면 같은 wide 포맷으로 패널만 교체하면 된다.
- 그래서 `--check-against` 결과도 100%가 아니다. v1 기준 셀 일치 97.87%이고 불일치 612심볼은
  ① 기준 패널이 달력 뒷부분만 채운 심볼 607개(코드 자체는 동일 — 기준의 증분 갱신 아티팩트),
  ② 기준 코드가 시간에 따라 변하는 심볼 5개(252셀)뿐이다. 코드가 상수인데 다른 심볼은 0개다.
- 마스터에 없는 심볼과 라벨이 없는 심볼(v1 624, v2 526)은 전 기간 NaN이다.
- sector와 industry는 같은 마스터 행에서 나오므로 NaN 심볼 집합이 서로 같다.
- 파이썬(pandas)으로 읽으면 이 패널의 컬럼 라벨은 **문자열**이고 base `BaseData/close.parquet`·`TopUniverseV1/*.parquet`의 컬럼 라벨은 **int64**로 복원된다. 그대로 `reindex`하면 조용히 전부 NaN이 되므로 `columns.astype(str)`로 맞춰 쓴다(C++ 로더는 이름 문자열로 정렬하므로 영향 없음).

## 4. 재생성 시점

- **캐시를 재구축한 뒤**(달력·심볼이 바뀌므로 필수). 이벤트 파일 재생성과 같은 규약이다.
- 마스터를 새로 받은 뒤(분류가 갱신되므로). `refresh_master.py` 뒤에 돌리면 된다.
- 새 캐시를 추가할 때(`--output data/groups/<cache_name>`을 캐시마다 하나씩 만든다).
- 코드 순서는 캐시 간 같아야 한다. 새 캐시에도 **같은 `--sector-index`**를 넘기고, industry 코드를
  고정해야 하면 기존 `industryIx.parquet`을 `--industry-index`로 넘긴다.

## 5. 테스트

`cpp-engine/tests/test_build_group_panels.py` (합성 fixture만 사용, 실데이터 미사용). CTest의 tool_test
목록에 `test_build_group_panels`로 등록되어 있다.

```bash
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tests/test_build_group_panels.py
```
