# `<neutralize group="sector|industry"/>` 구현 계획 (2026-09-16)

WorldQuant XML 규약을 충실히 따르는 리팩토링의 한 단계. 목표는 파이프라인 모듈 `neutralize`를 추가해 `sector`·`industry` 두 그룹으로 신호를 그룹 중립화하는 것이다. 계획 단계이며 코드는 아직 바꾸지 않았다.

## 1. WQ 규약 (근거: `docs/wq_xml.jpeg`)

이미지의 알파는 `neutralize`를 두 위치에서 쓴다.

| 위치 | XML | 의미 |
|---|---|---|
| `<choice>` 안 대안 | `<neutralize group="sector\|industry\|country"/>` | `tsrank`·`rank`·`tsmean` 등과 같은 급의 **cross-sectional 연산 모듈** |
| 파이프라인 마지막 | `<nop input="position"/>` → `<neutralize group="sector\|industry\|country"/>` | 최종 포지션을 그룹별로 demean |

따라서 `neutralize`는 (a) 직전 출력을 입력으로 받는 stateless 모듈이고, (b) `group` 속성이 문자열이며, (c) 기본형은 단일값 `<neutralize group="sector"/>`이고 `|` 파이프는 그 위의 확장 문법이다. `<choice>`는 어느 모듈에나 적용되는 선택 기능이라 `neutralize`와 무관하다. 세 성질 모두 현재 엔진 규약과 맞는다: 파이프 확장은 속성 종류와 무관하게 `expansion.cpp`가 처리하고 GA 유전자도 문자열 대안을 받는다(`search_space.cpp` `is_pipe`). 이미지의 `<nop input="position"/>` 뒤 배치는 우리 엔진에서 `<neutralize group="sector" input="position"/>`(컴파일러가 `input=`을 `MSignalLoad`로 해석)로 같은 뜻이 된다.

## 1.1 참고: 팀 sim의 neutralize 구현

| 함수 | 위치 | 동작 |
|---|---|---|
| `cs_group_neut_1d` | `/home/base/sim/utils/func_util/lib_src/cppFuncs.cpp:2302` | 행마다 그룹코드 `gp >= 0`이고 값이 유한한 원소만 그룹 합·개수에 넣고 `x − mean_g`; 그 밖(코드 음수·NaN, 값 NaN)은 출력 NaN. 스케일·최소개수 조건 없음 |
| `cs_neut_1d` | 같은 파일 `:2674` | 전체 횡단면 demean(WQ의 `market`에 해당). `lib_funcs.neutralize()`가 이것 |
| 파이썬 래퍼 `cs_group_neut` | `lib_funcs.py:193` | `group[group >= 0]`로 걸러 `int`로 캐스팅. 모듈에서는 `sector.fillna(-1).astype(int)`로 미분류를 −1 처리(`pv_champ_ind.py`) |

프로덕션 사용 형태(`/home/base/portfolio/portfolio_v1_0/modules/AlphaSample000001/000006/000012.py`):

```python
alpha = scale(cs_group_neut(cs_rank(df[self.valid]), self.sector))   # 그 뒤 shift(1)
```

즉 **유니버스 마스크 → rank → 그룹 demean → scale(Σ|w|=1) → 지연** 순서로 최종 신호에 적용한다. 아카이브 연구(`61:research/00_grave/pv/docs/industry_neut_results.md` §1b)는 tail/decay 앞에서 demean하면 책이 섹터중립이 되지 않고(섹터 tilt 0.55→0.37), 최종 포지션에 걸어야 tilt 0이 됨을 확인했다. WQ 이미지가 파이프라인 끝에 `neutralize`를 두는 것과 같은 결론이다. 우리 엔진에서는 모듈 안 마스크 적용이 `[self.valid]`, `normalize_signal_row`의 Σ|w|=1이 `scale`, `mode=1`이 `shift(1)`에 대응한다.

## 2. 현황

### 2.1 엔진

| 항목 | 상태 |
|---|---|
| 그룹 연산 모듈 | 없음. 내장 모듈은 `data delay tsmean tsrank tszscore decay event rank zscore signal` + expr 연산자 |
| 문자열 속성 모듈 | 없음. 자동 발견 모듈(`modules/`)은 `IntParameter`/`FloatParameter`만 바인딩(`generate_modules.py`) → `group="…"`는 **내장 모듈**로 만들어야 함 |
| cross-sectional 관례 | `Rank.h`/`Zscore.h`: `panel->universe_mask`를 신호 날짜에 먼저 적용하고 마스크 밖은 NaN, 비유한 값은 통계에서 제외 |
| 데이터 접근 | `panel->row(column, di)` — `DataPanel`은 T×N float 패널만 가짐. 심볼 속성 테이블 같은 별도 구조는 없음 |

### 2.2 데이터

| 소스 | 내용 | 비고 |
|---|---|---|
| `/home/base/cache/cache_v1_cash_1d/sector/sector.parquet` | wide T×N double, 코드 1~11, 미분류 NaN. 행·열이 `BaseData/close.parquet`와 동일(11,816행×28,210심볼) | exact 정렬로 바로 읽힘. 코드 순서는 벤더 순서(1 Energy, 2 IT, …, `sectorIx.parquet`) |
| `…/sector/sectorIx.parquet` | 코드→라벨 | 0행은 None |
| 마스터 v1 `uss_cash_master_acc.parquet`, v2 `Master/master.parquet` | `sector`(11) + `simpleIndustryDescription`(74) | 74개 simple industry는 11개 sector 안에 완전히 중첩(교차 0). null 2.2% |
| xfeed(`xfl_target`) | `ciqsimpleindustry`(74행)뿐 | GICS sub-industry·industry group 테이블 **없음**. Compustat GICS 컬럼(`gsubind`/`gind`/`ggroup`/`gsector`)을 가진 테이블 0개 |
| AFL 사전 | `IndRel_*`(Industry Group Relative) 파생 팩터만 | 분류 코드 컬럼 없음 |
| 마스터 일별 아카이브 `master/history/` | 12,073일 스냅샷이나 `sector`·`simpleIndustryDescription` 컬럼은 2025년 말 이후 파일에만 존재(2025-02 파일에는 없음) | PIT 분류 이력으로 쓰기엔 구간이 짧음. 2026-01→2026-09 변경은 5,766종목 중 industry 3건·sector 1건 |
| industry 패널 | **어디에도 없음** | 새로 만들어야 함 |

### 2.3 "sub-industry" 탐색 결과

| 찾은 곳 | 내용 |
|---|---|
| `61:research/00_grave/pv/docs/industry_neut_results.md`, `00_grave/pv/src/modules/pv_champ_ind.py` | "industry neut" 실험은 **`sector.sector`(11그룹)**를 그룹으로 썼음. `secneut`(pre-tail) / `secneut_POST`(최종 포지션) / `dualneut`(sector×cap 5분위) / `secrank` 변형 |
| `00_grave/pv/docs/{README,workflow,champions,lead_review}.md` | "Subindustry group_rank(BRAIN 데모 SR 1.7)"는 가설 H7로만 존재. "subindustry(~50) 데이터 확보되면", "현 캐시 부재"라고 명시 → **데이터가 있었던 적 없음** |
| `/home/base/analysis/long_short_review_20260907/analyze.py:42`, `industry_exposures.csv` | 팀 북 리뷰가 **`simpleIndustryDescription`을 "industry"**로 사용(노출·손익 집계) |
| `research/glasso`, `research/alpha_zoo`, `research/analyst_alpha` | 전부 sector(11) 기준 중립화(`cs_group_neut`, `sector_neut=True`, `sector_neutral`) |
| `00_grave/neutralize/notebook/style_neut.ipynb` | 섹터 더미 + 스타일 회귀 잔차(스타일 중립화), industry 없음 |

결론: 우리 데이터에서 sector 아래 유일한 분류는 CIQ simple industry(74)이고, 팀 분석도 이미 이를 industry로 부른다. sector 패널은 현재 마스터 분류를 전 기간에 방송한 정적 값이다(28,210심볼 중 5개만 시간에 따라 코드가 바뀌며, 이는 base의 증분 갱신 흔적). 분류 이력이 사실상 없으므로 industry도 같은 방식(정적)으로 만들고, 사후 분류 방송에 따른 look-ahead는 sector와 동일한 알려진 한계로 기록한다.

## 3. 설계 결정

| # | 결정 | 근거 |
|---|---|---|
| D1 | `industry` = `simpleIndustryDescription`(74그룹) | 지시 "industry는 sub industry 사용". §2.3대로 마스터·DB·AFL·아카이브 연구 어디에도 GICS sub-industry는 없고, sector 하위 분류는 이 컬럼뿐(GICS industry 74개 수준과 일치, 11 sector에 완전 중첩). 팀 북 리뷰도 이를 industry로 사용 |
| D1' | 중립화 대상은 **최종 신호**(파이프라인 끝, 또는 `input="position"`) | 팀 sim 프로덕션 순서(`scale(cs_group_neut(cs_rank(x), sector))`)와 아카이브 검증(§1.1). 모듈 자체는 어느 위치에서든 동작하며 위치는 XML 작성자의 선택 |
| D2 | 그룹 데이터는 **wide 패널 필드**(`sector`, `industry`)이며, 가격과 함께 **기본 필드로 자동 등록·항상 로드**(§4.3 `base:` 블록). 필드마다 `datasets:`에 적지 않음 | 사용자 지시(2026-09-16): open/close/returns/sector/industry 같은 기본 데이터는 설정에 안 올려도 기본으로 올라가야 함. 로더·`DataPanel`은 그대로 재사용(설정 전개만 추가). 나중에 분류 이력이 생기면 같은 포맷으로 시변 패널 교체 가능 |
| D3 | 모듈은 내장 `MNeutralize`(`src/modules/Neutralize.h`) + `dag_builder.cpp` 팩토리 | 문자열 속성은 자동 발견 모듈이 지원하지 않음 |
| D4 | `group` 허용값은 `sector`, `industry`. 그 외(`country`, `subindustry`, `market`)는 `CompileError` | 데이터가 있는 것만. WQ 어휘와 이름을 맞추되 없는 것은 명시적으로 거부 |
| D5 | 계산: 신호 날짜 유니버스 마스크 적용 → 그룹별 유한값 평균 → `x − mean_g` | `rank`/`zscore`와 같은 마스크 규약(CLAUDE.md "signal-date mask") |
| D6 | 그룹 코드 NaN인 심볼 → 출력 NaN; 단독 그룹 → 0; 신호 NaN → NaN | 팀 `cs_group_neut_1d`(§1.1: `gp>=0 && isfinite` 아니면 NaN, 최소개수 조건 없음)와 동일. 최종 정규화가 NaN을 0 목표로 다루므로 안전 |
| D7 | 그룹 코드는 float 정확 비교(정수 코드) | 코드 수 K≤74라 정렬된 고유코드 + 이진탐색으로 충분 |
| D8 | 그룹 패널 생성 도구는 `cpp-engine/tools/data/build_group_panels.py`, 출력 `data/groups/<cache>/` | 이벤트 파일과 같은 위치 규약(`data/`는 gitignore) |

## 4. 구현 단계

### 4.1 데이터 도구 — `cpp-engine/tools/data/build_group_panels.py`

입력: 마스터 parquet(v1 또는 v2), 캐시의 `BaseData/TimeStamp.parquet`·`BaseData/tradingItemId.parquet`.
출력(`data/groups/<cache_name>/`):

| 파일 | 형식 |
|---|---|
| `sector.parquet`, `industry.parquet` | wide, index=캐시 달력, columns=캐시 tradingItemId 전부(마스터에 없으면 NaN), float32 코드 |
| `sectorIx.parquet`, `industryIx.parquet` | 코드→라벨(0행 None). sector는 base `sectorIx.parquet` 순서를 그대로 채택, industry는 라벨 정렬 순 1~74 |
| `build_report.md` | 심볼 수·NaN 수·라벨 표·sha256 |

검사: v1 출력 `sector.parquet` vs base `sector/sector.parquet` 셀 일치율(기대: 시간 변동 5심볼 외 100%). industry는 마스터 대조로 자기검사.

### 4.2 엔진

| 파일 | 변경 |
|---|---|
| `src/modules/Neutralize.h` (신규) | `MNeutralize : Module`. 필드 `std::string column_name; std::string group; const DataPanel* panel`. `calculate(di, out)`: 마스크 → `panel->row(column_name, di)` 코드 읽기 → 2-pass(합·개수, 빼기). STAR 규약대로 헤더에 `#include`는 `../data/data_panel.h`만 |
| `src/compiler/dag_builder.cpp` | `neutralize` 팩토리: `group` 필수, 허용값 표(`sector`→`sector`, `industry`→`industry`), `panel->has_column()` 검사 실패 시 "group 'industry' needs data field 'industry' (register it under datasets:)" 형태의 `CompileError` |
| `src/data/config_loader.cpp` | `base:` 블록 파싱 → 기본 필드 목록을 `DatasetConfig` 두 개(wide, eager; 가격은 exact, 그룹은 **asof**)로 전개(§4.3). `load_config`에는 그룹 패널 가드만 추가 |
| `src/main.cpp` | 변경 없음(`/api/config` `columns`에 `sector`/`industry`가 자동 노출) |

### 4.3 설정 — 기본 데이터 자동 등록

지금은 `close` 하나만 필수이고 나머지는 전부 `datasets:`에 필드별로 적어야 읽는다. 기본 데이터는 적지 않아도 올라가야 하므로 캐시 위치만 주는 `base:` 블록을 추가한다.

```yaml
base:
  cache: /home/base/cache/cache_v1_cash_1d        # BaseData/*.parquet
  groups: ../../../data/groups/cache_v1_cash_1d   # 1단계 산출물 sector.parquet, industry.parquet
```

| 기본 필드 | 파일 | 로딩 |
|---|---|---|
| `open` `high` `low` `close` `volume` `vwap` `cap` | `<cache>/BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet` | 시작 시 전부 읽음(`cap`은 WQ 이름) |
| `sector` `industry` | `<groups>/{sector,industry}.parquet` | 시작 시 읽음 |
| `returns` | `close`에서 파생(기존 expr 규칙) | — |

- `base:`가 있으면 `datasets:`는 추가분(AFL 등)만 적으며 비워도 된다. 기본 파일이 하나라도 없으면 **기동 실패**(이벤트 파일과 같은 규약, 조용한 부재 방지).
- 그룹 패널은 **asof 정렬**: 가격 캐시가 매일 늘어나도 마지막 분류가 이어지므로 일간 재생성이 필요 없다(2026-09-16 리뷰에서 exact 정렬이 하루 뒤 기동 실패를 만드는 문제를 잡아 변경). 새 심볼은 재생성 전까지 미분류. 그룹 파일이 늦게 시작하거나 다른 캐시용이면 코드 없는 bar가 생겨 로더 가드가 기동을 거부한다.
- `datasets:`만 있는 기존 설정(크립토 long parquet)은 그대로 동작한다. 그 경우 `neutralize`는 `sector`/`industry` 필드가 없어 컴파일 에러를 낸다.
- 메모리(필드당 T×N×4B): Top500 패널(1,056종목×4,449일) 19MB, ALL(22,871×6,713) 614MB → 기본 9필드는 Top500 0.17GB, ALL 5.5GB. 현 예제도 가격 5필드를 eager로 읽어 ALL에서 3GB를 쓰고 있다.
- 예제 갱신: `equities_monthly_afl.yaml`·`compare_v2*.yaml`을 `base:` 형태로 바꾸고 `examples/datasets/README.md`에 기본 필드 표와 그룹 파일 생성 절을 추가.

### 4.4 테스트 계획

층별로 "무엇을, 어떤 입력으로, 무엇이 나와야 하는지"를 고정한다. 기대값은 손계산 가능한 크기로 두고, 실데이터 검사는 팀 sim 함수를 기준(oracle)으로 삼는다.

**(a) 데이터 도구** — `cpp-engine/tests/test_build_group_panels.py`(pytest, 기존 `test_afl_catalog.py` 방식)

| 검사 | 입력 | 기대 |
|---|---|---|
| 모양·정렬 | 합성 마스터 6종목(sector 2개·industry 3개·미분류 1개·마스터에 없는 심볼 1개) + 합성 달력 5일 | 출력 T×N이 달력·심볼 순서와 같음, float32 |
| 코드 매핑 | 위와 같음 | 라벨표(`*Ix.parquet`) 역참조가 마스터 라벨과 일치, sector 코드는 base `sectorIx` 순서 유지 |
| 결측 규칙 | 미분류·마스터 부재 심볼 | 전 기간 NaN |
| 중첩 검사 | industry 하나가 두 sector에 걸친 합성 마스터 | 도구가 실패(exit≠0) |
| 재현성 | 같은 입력 2회 | 파일 sha256 동일 |
| 실데이터 대조 | 도구 옵션 `--check-against /home/base/cache/cache_v1_cash_1d/sector/sector.parquet` | 셀 일치율·불일치 심볼 목록을 `build_report.md`에 기록. 기대: 시간 변동 5심볼 외 100% |

**(b) 설정·로더** — `tests/test_config_loader.cpp`, `tests/test_parquet_loader.cpp`(gtest, 임시 디렉토리 fixture)

| 검사 | 입력 | 기대 |
|---|---|---|
| `base:` 전개 | `base.cache`·`base.groups`만 있는 YAML | 기본 필드 9종이 등록되고 `datasets:` 없이도 로드 성공 |
| 파일 부재 | groups 디렉토리에 `industry.parquet` 없음 | 기동 실패, 메시지에 파일 경로 |
| 정렬 | 심볼 순서를 섞은 합성 wide `sector.parquet` | `panel.row("sector", t)`가 `symbols` 순서로 재정렬됨 |
| 구설정 호환 | 기존 `datasets:` 전용 YAML(크립토 예제) | 결과 불변(기존 테스트 그대로 통과) |

**(c) 모듈 단위** — `tests/test_modules.cpp`(gtest, `EXPECT_NEAR` 1e-6)

| # | 검사 | 입력(값 / 그룹 / 마스크) | 기대 |
|---|---|---|---|
| 1 | 두 그룹 demean | [1,2,3,10,20] / [1,1,1,2,2] | [−1,0,1,−5,5] |
| 2 | 그룹 NaN | [1,3,100] / [1,1,NaN] | [−1,1,NaN] (100은 평균에 안 들어감) |
| 3 | 신호 NaN | [1,NaN,3] / [1,1,1] | [−1,NaN,1] |
| 4 | 단독 그룹 | [7,1,3] / [2,1,1] | [0,−1,1] |
| 5 | 유니버스 마스크 | [1,3,1000] / [1,1,1] / mask [1,1,0] | [−1,1,NaN] (1000은 제외) |
| 6 | 전부 NaN 행 | [NaN,NaN] / [1,1] | [NaN,NaN], 예외 없음 |
| 7 | 시변 그룹 | 2일치 패널, 둘째 날 그룹이 바뀜 | `di`별 해당 행의 그룹으로 계산 |
| 8 | 그룹합 0 | 무작위 200종목·11그룹 | 그룹별 Σ출력 절대값 < 1e-5 |

**(d) 컴파일러** — `tests/test_xml_compiler.cpp`, `tests/test_pipeline_contract.cpp`

| 검사 | XML | 기대 |
|---|---|---|
| 속성 누락 | `<neutralize/>` | `CompileError`, 메시지에 `group` |
| 미지원 값 | `group="country"` | `CompileError`, 메시지에 허용값 목록 |
| 필드 미등록 | `group="industry"`인데 패널에 `industry` 없음 | `CompileError`, 메시지에 필드명과 `base:` 안내 |
| 파이프 확장 | `group="sector\|industry"` | 알파 2개, `source_xml`이 각각 `group="sector"`, `group="industry"` |
| choice 안 | `<choice><rank/><neutralize group="sector"/></choice>` | 알파 2개, 둘째가 `MNeutralize` |
| 저장 신호 입력 | `<signal id="p"/>…<neutralize group="sector" input="p"/>` | `p`를 입력으로 계산(`MSignalLoad` 선행) |
| 시뮬레이터 통과 | `<data expr="close"/><rank/><neutralize group="sector"/>` mode 0/1 | 완주, 날짜별 그룹별 목표비중 합 = 0(±1e-9), Σ\|w\| = 1, 마스크 밖 0 |

**(e) 골든 cross-val** — `tests/golden/generate_golden.py` → `modules.json` → `test_golden_crossval.cpp`

- 기존 6×3 fixture에 그룹 벡터 `[1,1,2]`를 붙여 numpy 기준 구현(그룹 demean, NaN 규칙 동일)으로 `neutralize_sector` 골든을 만든다.
- 생성 스크립트 안에서 같은 fixture를 팀 `lib_funcs.cs_group_neut`에도 넣어 numpy 기준 구현과 일치함을 assert한다(기준 구현 자체의 검증).

**(f) HTTP 계약** — `tests/test_http_contract.cpp`(fixture 서버, 기존 skip 규약)

| 검사 | 기대 |
|---|---|
| `/api/config` | `columns`에 `sector`·`industry` 포함 |
| `/api/simulate` 정상 XML | 200, 파이프 시 알파 2개 |
| `/api/simulate` 오류 XML(`group="country"`) | 400, 메시지에 허용값 |

**(g) 실데이터 oracle** — `cpp-engine/tools/check_group_neutralize_oracle.py`(신규)

- 엔진 `/api/simulate collectSignals`로 `<data expr="returns"/><rank/>`(A)와 `<data expr="returns"/><rank/><neutralize group="sector"/>`(B)의 raw 신호를 **전체 유니버스**(Top500 패널 전 심볼, 2010~)에 대해 받고, 팀 `cs_group_neut(A, sector)`와 B를 비교한다(그룹 평균은 전체 유니버스 통계라 부분집합으로 계산하면 안 됨). industry도 같은 방식(기준은 도구가 만든 `industry.parquet`). 팀 체인 `cs_group_neut(cs_rank(returns[valid]), sector)`는 rank 규약 차이(엔진 `r/n`, 팀 `(r−1)/(n−1)`)가 섞이므로 참고 수치로만 보고한다.
- 보고: `max|diff|`, 일치 셀 비율, 날짜별 그룹합 최대값, NaN 위치 일치율. 통과 기준 `max|diff| < 1e-5`(float32), 그룹합 `< 1e-4`. 결과는 markdown 표로 저장하고 exit code로 판정.

**(h) 무중단 실행** — Top500/Top1000/ALL 전기간, XML 3종(`sector`, `industry`, 파이프) → exit 0, 소요 시간·RSS 기록.

**결과 기록 방식**

| 층 | 산출 | 기록 위치 |
|---|---|---|
| gtest (b)(c)(d)(e)(f) | CTest 통과 수(현재 25개 실행 파일 + 신규 없음, 케이스는 기존 파일에 추가) | 완료 문서에 "CTest N/N", 로그는 `docs/neutralize_2026-09-16/ctest.log` |
| pytest (a) | exit code | 같은 디렉토리 `pytest.log` |
| 도구 리포트 (a)(g)(h) | markdown 표 + sha256 | `docs/neutralize_2026-09-16/{build_report,oracle_report,run_report}.md` |
| 요약 | 검사 항목·입력·기대·실측·판정 표 | `docs/neutralize_completion_2026-09-16.md` |

### 4.5 문서

`README.md` XML 예제에 한 줄, `CLAUDE.md` 엔진 계약에 한 줄(마스크·NaN 규칙·정적 분류), 완료 시 `docs/logic_completion_2026-09-11.md`에 절 추가.

## 5. 완료 게이트

1. CTest 전체 PASS(기존 25 + 신규), `run_logic_tests.py` PASS.
2. 골든 cross-val PASS, 실데이터 oracle `max|diff| < 1e-5`(float32), 날짜별 그룹합 `|Σ| < 1e-4`.
3. v1 `sector.parquet` 재생성본 vs base 파일 셀 대조: 코드가 다른 셀은 base 쪽이 시간에 따라 바뀌는 5심볼(252셀)뿐이어야 하며, 나머지 불일치는 base의 증분 갱신이 비워 둔 NaN(우리 쪽만 코드 있음)이어야 함(실측 2026-09-16: 일치 97.865%, partial 607심볼 + varies 5심볼, 상수-오코드 0).
4. Top500/Top1000/ALL 전기간 `<data expr="returns"/><neutralize group="sector|industry"/>` 무중단 실행(exit 0).
5. 프론트 변경 없음 → `npm test` 기존 통과 유지 확인만.

## 6. 범위 밖

`country`·`subindustry`(데이터 없음), `market`(전체 demean, 필요하면 후속), 분류 이력(PIT) 패널, GA·프론트 편집기 변경, `nop`/`tsfill`/`abs`/`rank_sharpe`/`pair_*` 등 이미지의 다른 WQ 태그.

## 7. 미결·가정

| 항목 | 상태 |
|---|---|
| industry = `simpleIndustryDescription` | §2.3 탐색 결과 유일한 후보. 사용자 확정 대기. 다른 분류를 뜻했다면 데이터 소스만 바뀌고 설계는 동일 |
| 미분류(NaN 그룹) 심볼 처리 | 출력 NaN(팀 함수와 동일)으로 잡음. WQ가 미분류를 단일 그룹으로 묶는지는 미확인 |
| `market` 그룹 추가 여부 | 제안만. 팀 `cs_neut`에 대응하며 구현 비용은 작음(데이터 불필요) |
| 기존 base `sector.parquet` 직접 참조 vs 재생성본 사용 | 재생성본 기본(v2와 같은 규약). base 파일은 parity 대조용 |
| PIT 분류 이력 | 마스터 아카이브에 분류 컬럼이 2025년 말부터만 있어 보류. 나중에 이력이 생기면 같은 wide 포맷으로 시변 패널 교체 |
